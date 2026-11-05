import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from keybridge.deployment import (Destination, Vercel, VARIABLE, cli_environment,
                                 load_state, new_state, owner_lock, reconcile,
                                 save_state, state_path, status)
from keybridge.errors import BridgeError


def entry(destination, operation="a" * 32):
    return {"id": "env_fixture", "key": VARIABLE, "type": "sensitive",
            "target": list(destination.targets), "gitBranch": None,
            "comment": "Keybridge TypeSafe connection " + operation}


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = Destination(self.root, "prj_fixture", "team_fixture", ("preview", "production"))
        self.which = patch("keybridge.deployment.shutil.which", return_value="/trusted/vercel")
        self.which.start()
        self.addCleanup(self.which.stop)
        self.vercel = Vercel(self.destination)

    def test_cli_sends_sensitive_value_only_on_stdin(self):
        with patch.dict(os.environ, {"VERCEL_TOKEN": "fixture-credential", "NODE_OPTIONS": "untrusted"}), \
                patch("keybridge.deployment.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, json.dumps({"created": entry(self.destination), "failed": []}), "")
            self.assertEqual(self.vercel.create("fixture-credential", "a" * 32), "env_fixture")
            run.assert_called_once()
            args, kwargs = run.call_args
            command = args[0]
            self.assertNotIn("fixture-credential", str(command))
            self.assertEqual(command[2], "/v10/projects/prj_fixture/env?teamId=team_fixture")
            self.assertEqual(command[-4:], ["--method", "POST", "--input", "-"])
            body = json.loads(kwargs["input"])
            self.assertEqual(body["value"], "fixture-credential")
            self.assertEqual(body["type"], "sensitive")
            self.assertEqual(set(body["target"]), {"preview", "production"})
            self.assertNotIn("upsert", str(command) + kwargs["input"])
            self.assertEqual(kwargs["env"], cli_environment())
            self.assertNotIn("fixture-credential", str(kwargs["env"]))
            self.assertNotIn("NODE_OPTIONS", kwargs["env"])
            self.assertEqual(kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(kwargs["stderr"], subprocess.PIPE)

    def test_project_identity_must_match_both_project_and_account(self):
        for data in ({"id": "prj_wrong", "accountId": "team_fixture", "name": "site"},
                     {"id": "prj_fixture", "accountId": "team_wrong", "name": "site"}, []):
            with patch.object(self.vercel, "_api", return_value=data), self.assertRaises(BridgeError):
                self.vercel.project()

    def test_incomplete_inventory_is_rejected_and_values_are_discarded(self):
        for data in ({"envs": [], "hiddenProductionEnvCount": 1},
                     {"envs": [], "pagination": {"next": 123}},
                     {"envs": [None]}, {"envs": [{"key": VARIABLE, "target": "production"}]}):
            with patch.object(self.vercel, "_api", return_value=data), self.assertRaises(BridgeError):
                self.vercel.entries()
        with patch.object(self.vercel, "_api", return_value={"envs": [{**entry(self.destination), "value": "fixture-credential"}]}):
            self.assertNotIn("fixture-credential", json.dumps(self.vercel.entries()))

    def test_cli_failure_is_sanitized_and_not_retried(self):
        for result in (subprocess.CompletedProcess([], 1, "fixture-credential", "fixture-credential"),
                       subprocess.CompletedProcess([], 0, "fixture-credential", "")):
            with patch("keybridge.deployment.subprocess.run", return_value=result) as run:
                with self.assertRaises(BridgeError) as caught:
                    self.vercel.project()
                self.assertNotIn("fixture-credential", str(caught.exception))
                run.assert_called_once()
        with patch("keybridge.deployment.subprocess.run", side_effect=subprocess.TimeoutExpired("fixture-credential", 60)):
            with self.assertRaisesRegex(BridgeError, "may have succeeded"):
                self.vercel.create("fixture-credential", "a" * 32)

    def test_read_failures_identify_the_check_without_suggesting_a_write(self):
        failures = [subprocess.TimeoutExpired("fixture-credential", 60), OSError("fixture-credential"),
                    subprocess.CompletedProcess([], 1, "fixture-credential", "fixture-credential"),
                    subprocess.CompletedProcess([], 0, "fixture-credential", "")]
        for check, label in ((self.vercel.project, "project access check"), (self.vercel.entries, "variable status check")):
            for failure in failures:
                with self.subTest(check=label, failure=type(failure).__name__), patch("keybridge.deployment.subprocess.run") as run:
                    if isinstance(failure, Exception):
                        run.side_effect = failure
                    else:
                        run.return_value = failure
                    with self.assertRaises(BridgeError) as caught:
                        check()
                    message = str(caught.exception)
                    self.assertIn(label, message)
                    self.assertIn("read-only", message)
                    self.assertNotIn("may have succeeded", message)
                    self.assertNotIn("fixture-credential", message)
                    run.assert_called_once()

    def test_receipt_is_private_contains_no_key_and_rejects_wrong_scope_or_symlink(self):
        state = new_state(self.destination)
        state["value"] = "fixture-credential"
        save_state(self.root, self.destination, state)
        path = state_path(self.root, self.destination)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn("fixture-credential", path.read_text())
        self.assertNotIn("value", load_state(self.root, self.destination))
        other = Destination(self.root, "prj_fixture", "team_fixture", ("preview",))
        with self.assertRaises(BridgeError):
            load_state(self.root, other)
        path.chmod(0o644)
        with self.assertRaises(BridgeError):
            load_state(self.root, self.destination)
        path.unlink()
        path.symlink_to(self.root / "missing")
        with self.assertRaises(BridgeError):
            load_state(self.root, self.destination)

    def test_uncertain_write_can_be_reconciled_by_metadata_only(self):
        state = new_state(self.destination)
        state.update(phase="submitting", checked_at=123)
        with patch.object(self.vercel, "entries", return_value=[entry(self.destination, state["operation"])]):
            self.assertTrue(reconcile(self.vercel, state, self.root))
        stored = load_state(self.root, self.destination)
        self.assertEqual(stored["phase"], "configured")
        self.assertEqual(stored["env_id"], "env_fixture")
        with patch("keybridge.deployment.Vercel", return_value=self.vercel), \
                patch.object(self.vercel, "project", return_value={"name": "site"}), \
                patch.object(self.vercel, "entries", return_value=[entry(self.destination, state["operation"])]):
            metadata = status(self.root, self.destination)
        self.assertTrue(metadata["managed_connection"])
        self.assertEqual(metadata["provider_checked_at"], 123)
        self.assertNotIn("operation", metadata)
        self.assertNotIn("item_id", metadata)

    def test_existing_or_mismatched_variable_is_never_adopted(self):
        state = new_state(self.destination)
        state.update(phase="submitting", checked_at=123)
        for saved, changed in ((None, {}), (state, {"type": "encrypted"}),
                               (state, {"comment": "unrelated"}), (state, {"gitBranch": "feature"}),
                               (state, {"target": ["preview"]}), (state, {"id": "bad\nID"})):
            remote = {**entry(self.destination, state["operation"]), **changed}
            with patch.object(self.vercel, "entries", return_value=[remote]), self.assertRaises(BridgeError):
                reconcile(self.vercel, saved, self.root)

    def test_concurrent_setup_refuses_second_owner_then_releases(self):
        with owner_lock(self.root):
            with self.assertRaisesRegex(BridgeError, "Another"):
                with owner_lock(self.root):
                    self.fail("Second lock should not be acquired")
        with owner_lock(self.root):
            pass
