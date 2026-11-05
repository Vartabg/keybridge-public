import contextlib
import io
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from keybridge import cli, demo


class GuidedDemoTests(unittest.TestCase):
    def assert_rejected_reply(self, operation, replacement, *, denied_only=False):
        from keybridge.errors import BridgeError
        broker = demo.demo_broker()

        def reply(path, message):
            result = broker.handle(message)
            if message["operation"] == operation and (not denied_only or result.get("ok") is False):
                return replacement
            return result

        output = io.StringIO()
        with patch("keybridge.demo.request", side_effect=reply), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(BridgeError, "did not complete as expected"):
                demo.walkthrough(Path("unused-synthetic-socket"))
        self.assertNotIn("Post-lock request denied", output.getvalue())

    def test_unrelated_final_failure_is_not_proof_of_lock_denial(self):
        for response in ({"ok": False, "error": "The service is unavailable."}, {"ok": False}):
            with self.subTest(response=response):
                self.assert_rejected_reply("generate", response, denied_only=True)

    def test_lock_reply_must_confirm_locked_state(self):
        for response in ({"ok": True}, {"ok": True, "state": "ready"},
                         {"ok": True, "state": "expired"}):
            with self.subTest(response=response):
                self.assert_rejected_reply("lock", response)

    def test_earlier_failure_cannot_reach_success_message(self):
        self.assert_rejected_reply("status", {"ok": False, "error": "Synthetic status failure."})

    def test_guided_demo_is_isolated_offline_and_cleans_up(self):
        with tempfile.TemporaryDirectory(prefix="kb-test-") as root:
            home = Path(root) / "home"
            home.mkdir()
            existing = home / ".local/share/keybridge"
            existing.mkdir(parents=True)
            sentinel = existing / "session.sock"
            sentinel.write_text("leave this owner session untouched")
            output = io.StringIO()
            with patch("pathlib.Path.home", return_value=home), \
                    patch("keybridge.cli.Vault", side_effect=AssertionError("No vault")), \
                    patch("subprocess.run", side_effect=AssertionError("No subprocess")), \
                    patch("urllib.request.urlopen", side_effect=AssertionError("No HTTP")), \
                    contextlib.redirect_stdout(output):
                result = cli.run(cli.parser().parse_args(["demo", "--guided"]))
            self.assertEqual(result, 0)
            text = output.getvalue()
            self.assertIn("DEMONSTRATION", text)
            self.assertIn("not dollar caps", text)
            self.assertIn("Post-lock request denied", text)
            self.assertIn("Demo state removed", text)
            self.assertEqual(sentinel.read_text(), "leave this owner session untouched")
            state_line = next(line for line in text.splitlines() if line.startswith("Demo directory: "))
            self.assertFalse(Path(state_line.removeprefix("Demo directory: ")).exists())
            payloads = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
            status = payloads[0]
            self.assertEqual(status["models"], ["demo-model"])
            self.assertEqual(status["remaining_calls"], 10)
            self.assertEqual(status["max_output_tokens"], 1024)
            self.assertGreater(status["expires_in_seconds"], 0)
            history = next(item for item in payloads if "events" in item)
            self.assertEqual(len(history["events"]), 1)
            self.assertNotIn("prompt", json.dumps(history))
            self.assertNotIn("demonstration-value", text)

    def test_guided_demo_rejects_supplied_session_directory(self):
        with tempfile.TemporaryDirectory(prefix="kb-test-") as root:
            untouched = Path(root) / "existing"
            result = subprocess.run([sys.executable, "-m", "keybridge", "--directory", str(untouched),
                                     "demo", "--guided"], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertIn("without --directory", result.stderr)
            self.assertFalse(untouched.exists())

    def test_interruption_cleans_isolated_state(self):
        output = io.StringIO()
        with patch("keybridge.demo.walkthrough", side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(output):
            with self.assertRaises(KeyboardInterrupt):
                cli.run(cli.parser().parse_args(["demo", "--guided"]))
        state_line = next(line for line in output.getvalue().splitlines()
                          if line.startswith("Demo directory: "))
        self.assertFalse(Path(state_line.removeprefix("Demo directory: ")).exists())

    def test_long_live_path_is_rejected_before_vault_access(self):
        with tempfile.TemporaryDirectory(prefix="kb-test-") as root:
            directory = Path(root) / ("x" * 120)
            with patch("sys.stdin.isatty", return_value=True), \
                    patch("sys.stdout.isatty", return_value=True), \
                    patch("keybridge.cli.Vault") as vault:
                from keybridge.errors import BridgeError
                with self.assertRaisesRegex(BridgeError, "too long"):
                    cli.run(cli.parser().parse_args(["--directory", str(directory), "start"]))
                vault.assert_not_called()

    def test_unwritable_folder_has_actionable_error_and_restores_handlers(self):
        from keybridge.errors import BridgeError
        previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGHUP)}
        with patch("keybridge.demo.tempfile.TemporaryDirectory", side_effect=PermissionError), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(BridgeError, "writable current folder"):
                cli.run(cli.parser().parse_args(["demo", "--guided"]))
        self.assertEqual(previous, {number: signal.getsignal(number) for number in previous})

    def test_termination_and_terminal_closure_clean_isolated_state(self):
        script = """
import signal
import sys
from keybridge import cli, demo
def wait_for_signal(path):
    print("ready", file=sys.stderr, flush=True)
    signal.pause()
demo.walkthrough = wait_for_signal
cli.main()
"""
        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number), tempfile.TemporaryDirectory(prefix="kb-signal-") as root:
                env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
                process = subprocess.Popen([sys.executable, "-c", script, "demo", "--guided"],
                                           cwd=root, env=env, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True)
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stderr, selectors.EVENT_READ)
                        self.assertTrue(selector.select(timeout=5), "Guided demo did not start")
                    self.assertEqual(process.stderr.readline(), "ready\n")
                    process.send_signal(number)
                    stdout, stderr = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 1)
                    self.assertIn("Cancelled", stderr)
                    self.assertNotIn("Traceback", stderr)
                    self.assertEqual(list(Path(root).glob(".kb-demo-*")), [])
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
