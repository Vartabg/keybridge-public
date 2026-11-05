from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from keybridge.broker import Broker
from keybridge.config import Policy
from keybridge.deployment import Destination, Vercel, load_state, new_state, save_state
from keybridge.errors import BridgeError
from keybridge.owner import connect_typesafe, ensure_login, select_reference


class OwnerFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.site = self.root / "site"
        (self.site / ".vercel").mkdir(parents=True)
        (self.site / ".vercel/project.json").write_text(json.dumps({"projectId": "prj_fixture", "orgId": "team_fixture"}))
        self.destination = Destination.from_site(self.site, ["preview", "production"])
        self.events, self.remote = [], []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.output = self.stack.enter_context(patch("builtins.print"))
        self.stack.enter_context(patch("keybridge.owner.sys.stdin.isatty", return_value=True))
        self.stack.enter_context(patch("keybridge.owner.sys.stdout.isatty", return_value=True))
        with patch("keybridge.deployment.shutil.which", return_value="/trusted/vercel"):
            self.vercel = Vercel(self.destination)
        self.stack.enter_context(patch("keybridge.owner.Vercel", return_value=self.vercel))
        self.project = self.stack.enter_context(patch.object(self.vercel, "project", return_value={
            "name": "site", "project": "prj_fixture", "team": "team_fixture"}))
        self.stack.enter_context(patch.object(self.vercel, "entries", side_effect=lambda: self.remote))
        self.create = self.stack.enter_context(patch.object(self.vercel, "create", side_effect=self.upload))
        self.login = self.stack.enter_context(patch("keybridge.owner.ensure_login"))
        self.choose = self.stack.enter_context(patch("keybridge.owner.select_reference", return_value=("0" * 36, "login.password")))
        self.vault = MagicMock()
        self.vault.__enter__.side_effect = lambda: self.vault
        self.vault.__exit__.side_effect = lambda *args: self.events.append("vault-locked")
        self.vault.key.return_value = "fixture-credential"
        self.factory = self.stack.enter_context(patch("keybridge.owner.Vault", return_value=self.vault))
        self.check = self.stack.enter_context(patch("keybridge.owner.check_connection", side_effect=self.check_provider))

    def check_provider(self, key):
        self.assertEqual(key, "fixture-credential")
        self.assertEqual(self.events, ["vault-locked"])
        self.events.append("provider-checked")

    def upload(self, key, operation):
        self.assertEqual(key, "fixture-credential")
        self.assertEqual(self.events, ["vault-locked", "provider-checked"])
        state = load_state(self.root, self.destination)
        self.assertEqual(state["phase"], "submitting")
        self.remote.append({"id": "env_fixture", "key": "TYPESAFE_API_KEY", "type": "sensitive",
                            "target": ["preview", "production"], "gitBranch": None,
                            "comment": "Keybridge TypeSafe connection " + operation})
        return "env_fixture"

    def connect(self, **kwargs):
        return connect_typesafe(self.root, self.site, ["preview", "production"], **kwargs)

    def test_flow_locks_vault_before_one_check_and_write_and_is_idempotent(self):
        self.assertEqual(self.connect()["result"], "configured")
        state = load_state(self.root, self.destination)
        self.assertEqual(state["phase"], "configured")
        self.assertNotIn("fixture-credential", json.dumps(state))
        self.assertEqual(self.connect()["result"], "already_configured")
        self.factory.assert_called_once()
        self.login.assert_called_once()
        self.check.assert_called_once()
        self.create.assert_called_once()

    def test_rejected_credential_never_reaches_vercel(self):
        self.check.side_effect = BridgeError("TypeSafe rejected this credential.")
        with self.assertRaisesRegex(BridgeError, "rejected"):
            self.connect()
        self.create.assert_not_called()
        self.assertEqual(self.events, ["vault-locked"])
        state = load_state(self.root, self.destination)
        self.assertEqual(state["phase"], "prepared")
        self.assertNotIn("checked_at", state)

    def test_failed_unlock_stops_before_selection_provider_or_write(self):
        self.vault.__enter__.side_effect = BridgeError("Bitwarden did not accept the unlock request.")
        with self.assertRaisesRegex(BridgeError, "unlock"):
            self.connect()
        self.choose.assert_not_called()
        self.vault.key.assert_not_called()
        self.check.assert_not_called()
        self.create.assert_not_called()
        self.assertIsNone(load_state(self.root, self.destination))

    def test_preflight_failure_reports_progress_before_unlock_or_write(self):
        self.project.side_effect = BridgeError("Vercel project access check timed out.")
        with self.assertRaises(BridgeError):
            self.connect()
        messages = [call.args[0] for call in self.output.call_args_list]
        self.assertTrue(messages[0].startswith("[1/5]"))
        self.assertTrue(self.output.call_args_list[0].kwargs["flush"])
        self.assertFalse(any(message.startswith("[2/5]") for message in messages))
        self.login.assert_not_called()
        self.check.assert_not_called()
        self.create.assert_not_called()

    def test_readback_failure_keeps_write_receipt_and_recovers_without_resending(self):
        original_upload = self.upload
        with patch.object(self.vercel, "entries", side_effect=lambda: self.remote) as inventory:
            def upload_then_readback_fails(key, operation):
                result = original_upload(key, operation)
                inventory.side_effect = BridgeError("Vercel variable status check timed out.")
                return result
            self.create.side_effect = upload_then_readback_fails
            with self.assertRaisesRegex(BridgeError, "accepted the write, but verification"):
                self.connect()
            self.assertEqual(load_state(self.root, self.destination)["phase"], "submitting")
            messages = [call.args[0] for call in self.output.call_args_list]
            self.assertTrue(any(message.startswith("[5/5]") for message in messages))
            self.assertFalse(any(message.startswith("Connected:") for message in messages))
            self.assertNotIn("fixture-credential", str(messages))
            inventory.side_effect = lambda: self.remote
            self.assertEqual(self.connect()["result"], "already_configured")
        self.factory.assert_called_once()
        self.check.assert_called_once()
        self.create.assert_called_once()

    def test_uncertain_write_is_recovered_without_vault_provider_or_repeated_write(self):
        def interrupted(key, operation):
            self.upload(key, operation)
            raise BridgeError("Vercel did not finish")
        self.create.side_effect = interrupted
        with self.assertRaises(BridgeError):
            self.connect()
        self.assertEqual(load_state(self.root, self.destination)["phase"], "submitting")
        self.assertEqual(self.connect()["result"], "already_configured")
        self.factory.assert_called_once()
        self.check.assert_called_once()
        self.create.assert_called_once()

    def test_pending_missing_variable_requires_explicit_retry_and_reuses_reference(self):
        state = new_state(self.destination)
        state.update(phase="submitting", checked_at=123, item_id="0" * 36, field="login.password")
        save_state(self.root, self.destination, state)
        with self.assertRaisesRegex(BridgeError, "retry-unconfirmed"):
            self.connect()
        self.factory.assert_not_called()
        self.check.assert_not_called()
        self.create.assert_not_called()
        self.assertEqual(self.connect(retry_unconfirmed=True)["result"], "configured")
        self.choose.assert_not_called()
        self.create.assert_called_once()

    def test_conflicting_existing_key_refuses_before_unlock(self):
        self.remote.append({"key": "TYPESAFE_API_KEY", "target": ["preview"], "comment": "unrelated"})
        with self.assertRaisesRegex(BridgeError, "already exists"):
            self.connect()
        self.factory.assert_not_called()
        self.check.assert_not_called()
        self.create.assert_not_called()

    def test_remote_change_while_unlocking_refuses_write(self):
        def check_then_changed(key):
            self.check_provider(key)
            self.remote.append({"key": "TYPESAFE_API_KEY", "target": ["preview"]})
        self.check.side_effect = check_then_changed
        with self.assertRaises(BridgeError):
            self.connect()
        self.create.assert_not_called()

    def test_saved_reference_can_be_reselected_after_failed_check(self):
        state = new_state(self.destination)
        state.update(item_id="1" * 36, field="notes")
        save_state(self.root, self.destination, state)
        self.connect(choose_again=True)
        self.choose.assert_called_once()
        self.vault.key.assert_called_once_with("0" * 36, "login.password")

    def test_captured_terminal_is_refused_before_external_actions(self):
        with patch("keybridge.owner.sys.stdin.isatty", return_value=False):
            with self.assertRaisesRegex(BridgeError, "private Terminal"):
                self.connect()
        self.project.assert_not_called()
        self.factory.assert_not_called()


class OwnerBoundaryTests(unittest.TestCase):
    def test_real_cli_refuses_captured_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-m", "keybridge", "--directory", directory,
                                     "connect-typesafe", "--site", directory], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertIn("private Terminal", json.loads(result.stderr)["error"])
            self.assertNotIn("Traceback", result.stderr)

    def test_broker_has_no_provisioning_or_vault_operation(self):
        broker = Broker(Policy("moonshot", ("fixture-model",)), "fixture-credential")
        for operation in ("connect-typesafe", "deployment-status", "vault-list", "get-key", "vercel"):
            result = broker.handle({"operation": operation})
            self.assertFalse(result["ok"])
            self.assertNotIn("fixture-credential", json.dumps(result))

    def test_initial_login_is_quiet_uses_dedicated_profile_and_locks(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("keybridge.owner.vault_environment", return_value={"PATH": "/trusted", "BITWARDENCLI_APPDATA_DIR": directory}), \
                patch("keybridge.owner.shutil.which", return_value="/trusted/bw"), \
                patch("keybridge.owner.subprocess.run") as run, patch("builtins.print"):
            run.side_effect = [subprocess.CompletedProcess([], 0, '{"status":"unauthenticated"}', ""),
                               subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0, "", "")]
            ensure_login()
            self.assertEqual([call.args[0][1:] for call in run.call_args_list], [["status"], ["login", "--quiet"], ["lock"]])
            for call in run.call_args_list:
                self.assertEqual(call.kwargs["env"]["BITWARDENCLI_APPDATA_DIR"], directory)
            self.assertTrue(run.call_args_list[0].kwargs["capture_output"])
            self.assertTrue(run.call_args_list[-1].kwargs["capture_output"])

    def test_selection_shows_labels_never_values(self):
        vault = MagicMock()
        vault.items.return_value = [{"id": "0" * 36, "name": "TypeSafe", "login": {"password": "fixture-credential"}}]
        with patch("builtins.input", return_value="1"), patch("builtins.print") as output:
            self.assertEqual(select_reference(vault), ("0" * 36, "login.password"))
            self.assertNotIn("fixture-credential", str(output.call_args_list))
