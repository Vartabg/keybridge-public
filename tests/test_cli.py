import json
import os
import selectors
import subprocess
import sys
import tempfile
import unittest

from keybridge.config import clean_process_environment
from unittest.mock import patch


class CliTests(unittest.TestCase):
    def test_environment_credentials_are_removed(self):
        with patch.dict(os.environ, {"UNRELATED_SERVICE_SECRET": "fixture-only", "HOME": "/tmp"}):
            clean_process_environment()
            self.assertNotIn("UNRELATED_SERVICE_SECRET", os.environ)
            self.assertEqual(os.environ["HOME"], "/tmp")

    def test_demo_status_generate_history_lock_and_cleanup(self):
        with tempfile.TemporaryDirectory(prefix="kb-cli-", dir=".") as directory:
            directory = os.path.relpath(directory)
            command = [sys.executable, "-m", "keybridge", "--directory", directory]
            server = subprocess.Popen([*command, "demo"], stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True)
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(server.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(timeout=5), "Demo did not start")
                self.assertIn("DEMO", server.stdout.readline())
                # The next two lines are written only after the socket binds.
                self.assertIn("Session started", server.stdout.readline())
                server.stdout.readline()
                def call(*args, prompt=None):
                    return subprocess.run([*command, *args], input=prompt, text=True,
                                          capture_output=True, timeout=5)
                status = json.loads(call("status").stdout)
                self.assertTrue(status["demo"])
                self.assertEqual(status["max_output_tokens"], 1024)
                self.assertEqual(call("demo").returncode, 1)
                self.assertEqual(json.loads(call("status").stdout)["state"], "ready")
                result = call("ask", "--model", "demo-model", prompt="a private test prompt")
                self.assertEqual(result.returncode, 0)
                self.assertIn("Demo response", json.loads(result.stdout)["text"])
                history = call("history").stdout
                self.assertNotIn("private test prompt", history)
                self.assertEqual(len(json.loads(history)["events"]), 1)
                self.assertEqual(call("lock").returncode, 0)
                self.assertEqual(call("ask", "--model", "demo-model", prompt="again").returncode, 1)
                self.assertEqual(call("start").returncode, 1)
            finally:
                server.terminate()
                stdout, stderr = server.communicate(timeout=5)
                self.assertNotIn("Traceback", stderr)
            self.assertEqual(server.returncode, 0)
            self.assertFalse(os.path.exists(os.path.join(directory, "session.sock")))

    def test_missing_server_has_safe_error(self):
        with tempfile.TemporaryDirectory(prefix="kb-cli-", dir=".") as directory:
            directory = os.path.relpath(directory)
            result = subprocess.run([sys.executable, "-m", "keybridge", "--directory", directory, "status"],
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertIn("unavailable", json.loads(result.stderr)["error"])


if __name__ == "__main__":
    unittest.main()
