import json
import os
import socket
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from keybridge.broker import Broker
from keybridge.config import Policy, private_directory, read_settings, write_settings
from keybridge.errors import BridgeError
from keybridge.transport import Server, request


class TransportTests(unittest.TestCase):
    def setUp(self):
        # Relative socket addresses also work under long runner scratch paths.
        self.temp = tempfile.TemporaryDirectory(prefix="kb-", dir=".")
        self.addCleanup(self.temp.cleanup)
        self.directory = private_directory(os.path.relpath(self.temp.name))
        self.path = self.directory / "session.sock"
        self.policy = Policy("moonshot", ("demo-model",))
        self.broker = Broker(self.policy, "fake-local-value", lambda *a: "demo response", demo=True)
        self.server = Server(self.path, self.broker)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05})
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def test_status_generate_lock_round_trip(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertTrue(request(self.path, {"operation": "status"})["demo"])
        result = request(self.path, {"operation": "generate", "model": "demo-model", "prompt": "hello"})
        self.assertEqual(result["text"], "demo response")
        self.assertTrue(request(self.path, {"operation": "lock"})["ok"])
        self.assertFalse(request(self.path, {"operation": "generate", "model": "demo-model", "prompt": "hello"})["ok"])

    def test_invalid_json_and_oversized_frame_are_rejected(self):
        for payload in (b"not-json\n", b"x" * 65537 + b"\n", b'{"operation":NaN}\n'):
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(3)
                client.connect(str(self.path))
                client.sendall(payload)
                result = json.loads(client.recv(4096))
                self.assertFalse(result["ok"])

    def test_no_socket_replacement(self):
        with self.assertRaises(BridgeError):
            Server(self.path, self.broker)

    def test_119_byte_path_has_actionable_error_without_creating_socket(self):
        size = 119 - len(os.fsencode(str(self.directory))) - 1
        path = self.directory / ("x" * size)
        self.assertEqual(len(os.fsencode(str(path))), 119)
        with self.assertRaisesRegex(BridgeError, "119 bytes.*shorter.*--directory"):
            Server(path, self.broker)
        self.assertFalse(path.exists())
        with self.assertRaisesRegex(BridgeError, "119 bytes"):
            request(path, {"operation": "status"})

    def test_chmod_failure_cleans_owned_socket(self):
        other = self.directory / "failure.sock"
        with patch("keybridge.transport.os.chmod", side_effect=OSError):
            with self.assertRaises(BridgeError):
                Server(other, self.broker)
        self.assertFalse(other.exists())

    def test_socket_limit_counts_encoded_bytes(self):
        path = self.directory / ("é" * 50)
        self.assertLess(len(str(path)), 103)
        self.assertGreater(len(os.fsencode(path)), 103)
        with self.assertRaisesRegex(BridgeError, "bytes.*shorter"):
            Server(path, self.broker)

    def test_cleanup_preserves_replaced_path(self):
        self.path.unlink()
        self.path.write_text("replacement belongs to someone else")
        self.server.server_close()
        self.assertEqual(self.path.read_text(), "replacement belongs to someone else")

    def test_failed_bind_does_not_delete_another_sessions_path(self):
        other = self.directory / "racing.sock"
        def failed_bind(instance):
            other.write_text("another session owns this path")
            raise OSError("Address in use")
        with patch("socketserver.UnixStreamServer.server_bind", failed_bind):
            with self.assertRaises(BridgeError):
                Server(other, self.broker)
        self.assertEqual(other.read_text(), "another session owns this path")

    def test_settings_round_trip_contains_references_only(self):
        write_settings(self.directory, self.policy, "item-reference", "login.password")
        saved = (self.directory / "connection.json").read_text()
        self.assertNotIn("fake-local-value", saved)
        policy, item, field = read_settings(self.directory)
        self.assertEqual(policy.provider, "moonshot")
        self.assertEqual(item, "item-reference")
        self.assertEqual(field, "login.password")

    def test_settings_symlink_is_rejected(self):
        target = self.directory / "other.json"
        target.write_text("unchanged")
        (self.directory / "connection.json").symlink_to(target)
        with self.assertRaises(OSError):
            write_settings(self.directory, self.policy, "item", "notes")
        with self.assertRaises(BridgeError):
            read_settings(self.directory)
        self.assertEqual(target.read_text(), "unchanged")

    def test_service_expiry_clears_key_without_client_request(self):
        self.broker.deadline = 0
        self.server.service_actions()
        self.assertIsNone(self.broker._key)

    def test_invalid_policy(self):
        for extra in ({"max_calls": True}, {"max_calls": 0}, {"duration_seconds": 0},
                      {"models": ["../bad"]}, {"max_output_tokens": 100000}):
            with self.assertRaises(BridgeError):
                Policy(**{"provider": "moonshot", "models": ("demo-model",), **extra})


if __name__ == "__main__":
    unittest.main()
