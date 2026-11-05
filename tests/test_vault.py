import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from keybridge.config import vault_environment
from keybridge.errors import BridgeError
from keybridge.owner import ensure_login
from keybridge.vault import Vault, extract_key, field_choices


class VaultTests(unittest.TestCase):
    def test_only_selected_field_is_read(self):
        item = {"login": {"password": "example-password"},
                "fields": [{"name": "api-key", "value": "example-api-key"}]}
        self.assertEqual(extract_key(item, "login.password"), "example-password")
        self.assertEqual(extract_key(item, "field:api-key"), "example-api-key")
        self.assertEqual(field_choices(item), ["login.password", "field:api-key"])

    def test_empty_ambiguous_or_multiline_credentials_fail(self):
        cases = [({}, "login.password"), ({"notes": "prose\nnot a key"}, "notes"),
                 ({"login": {"password": "bad\r\nheader"}}, "login.password"),
                 ({"fields": [{"name": "x", "value": "one"},
                              {"name": "x", "value": "two"}]}, "field:x")]
        for item, field in cases:
            with self.assertRaises(BridgeError):
                extract_key(item, field)

    def test_vault_child_does_not_inherit_agent_environment(self):
        with patch.dict("os.environ", {"NODE_OPTIONS": "evil", "HTTPS_PROXY": "evil",
                                       "OPENROUTER_API_KEY": "private", "BW_SESSION": "old"}):
            env = vault_environment(session="new")
        self.assertEqual(env["BW_SESSION"], "new")
        self.assertNotIn("OPENROUTER_API_KEY", env)
        self.assertNotIn("NODE_OPTIONS", env)
        self.assertNotIn("HTTPS_PROXY", env)

    def test_owner_and_vault_find_user_npm_cli_without_inheriting_path(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            binary = home / ".npm-global/bin/bw"
            binary.parent.mkdir(parents=True)
            binary.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "status) echo '{\"status\":\"locked\"}' ;;\n"
                "lock) exit 0 ;;\n"
                "--version) echo fixture-cli ;;\n"
                "*) exit 1 ;;\n"
                "esac\n"
            )
            binary.chmod(0o700)
            untrusted = home / "inherited-path"
            untrusted.mkdir()
            decoy = untrusted / "bw"
            decoy.write_text("#!/bin/sh\nexit 99\n")
            decoy.chmod(0o700)
            system = home / "system-bin"
            system.mkdir()
            with patch("keybridge.config.Path.home", return_value=home), \
                    patch("keybridge.config.CLI_SYSTEM_PATH", str(system)), \
                    patch.dict(os.environ, {"PATH": str(untrusted)}):
                ensure_login()
                vault = Vault()
                self.assertEqual(vault.binary, str(binary))
                self.assertEqual(vault._run(["--version"]).strip(), "fixture-cli")
                self.assertNotIn(str(untrusted), vault_environment()["PATH"])

    @patch("keybridge.vault.shutil.which", return_value="/trusted/bw")
    @patch("keybridge.vault.subprocess.run")
    @patch("keybridge.vault.getpass.getpass", return_value="test-master-password")
    def test_unlock_get_lock_never_put_secrets_in_arguments(self, password, run, which):
        run.side_effect = [subprocess.CompletedProcess([], 0, '{"status":"locked"}', ""),
                           subprocess.CompletedProcess([], 0, 'test-session-value', ""),
                           subprocess.CompletedProcess([], 0, json.dumps({"login": {"password": "example-api-value"}}), ""),
                           subprocess.CompletedProcess([], 0, "", "")]
        with Vault() as vault:
            self.assertEqual(vault.key("00000000-0000-0000-0000-000000000001", "login.password"), "example-api-value")
        for call in run.call_args_list:
            self.assertNotIn("test-master-password", str(call.args))
            self.assertNotIn("test-session-value", str(call.args))
            self.assertTrue(call.kwargs["capture_output"])
        self.assertEqual(run.call_args_list[-1].args[0][-1], "lock")
        self.assertIsNone(vault.session)

    @patch("keybridge.vault.shutil.which", return_value="/trusted/bw")
    @patch("keybridge.vault.subprocess.run")
    def test_missing_login_does_not_unlock(self, run, which):
        run.return_value = subprocess.CompletedProcess([], 0, '{"status":"unauthenticated"}', "")
        with self.assertRaisesRegex(BridgeError, "Log in"):
            with Vault():
                self.fail("Must not enter")
        self.assertEqual(run.call_count, 1)

    @patch("keybridge.vault.shutil.which", return_value="/trusted/bw")
    @patch("keybridge.vault.subprocess.run")
    @patch("keybridge.vault.getpass.getpass", return_value="")
    def test_empty_password_does_not_attempt_unlock(self, password, run, which):
        run.return_value = subprocess.CompletedProcess([], 0, '{"status":"locked"}', "")
        with patch("builtins.print"), self.assertRaisesRegex(BridgeError, "No master password was entered"):
            with Vault():
                self.fail("Must not enter")
        self.assertEqual(run.call_count, 1)

    @patch("keybridge.vault.shutil.which", return_value="/trusted/bw")
    @patch("keybridge.vault.subprocess.run")
    @patch("keybridge.vault.getpass.getpass", return_value="fixture-master-password")
    def test_rejected_unlock_explains_next_step_without_echoing_or_retrying(self, password, run, which):
        run.side_effect = [subprocess.CompletedProcess([], 0, '{"status":"locked"}', ""),
                           subprocess.CompletedProcess([], 1, "fixture-private-output", "fixture-private-error")]
        vault = Vault()
        with patch("builtins.print") as output, self.assertRaises(BridgeError) as error:
            with vault:
                self.fail("Must not enter")
        self.assertIn("Bitwarden master password", str(error.exception))
        self.assertNotIn("fixture-", str(error.exception) + str(output.call_args_list))
        self.assertEqual(run.call_count, 2)
        self.assertIsNone(vault.session)


if __name__ == "__main__":
    unittest.main()
