"""Local Unix-socket transport. This does not isolate same-user processes."""
import json
import os
import socket
import socketserver
from pathlib import Path

from .errors import BridgeError

FRAME_LIMIT = 65536
RESPONSE_LIMIT = 3200000  # Accommodates JSON's Unicode escaping of provider text.
SOCKET_PATH_LIMIT = 103  # Portable pathname budget for macOS and Linux, excluding NUL.


def validate_socket_path(path):
    length = len(os.fsencode(path))
    if length > SOCKET_PATH_LIMIT:
        raise BridgeError(
            f"The local socket path is too long ({length} bytes; maximum {SOCKET_PATH_LIMIT}). "
            "Use a shorter --directory path for every command, or run demo --guided."
        )


def _reject_constant(value):
    raise ValueError


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        try:
            raw = self.rfile.readline(FRAME_LIMIT + 1)
            if len(raw) > FRAME_LIMIT or not raw.endswith(b"\n"):
                raise ValueError
            message = json.loads(raw, parse_constant=_reject_constant)
            result = self.server.broker.handle(message)
        except (ValueError, TimeoutError, OSError):
            result = {"ok": False, "error": "Send one valid, size-limited JSON request."}
        try:
            self.wfile.write(json.dumps(result).encode() + b"\n")
        except OSError:
            return  # Client disconnected; no retry or secret-bearing traceback.


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path, broker):
        self.path = Path(path)
        self.broker = broker
        self._owns_path = False
        validate_socket_path(self.path)
        if self.path.exists() or self.path.is_symlink():
            raise BridgeError("A session socket already exists. Stop that session before starting another.")
        try:
            super().__init__(str(self.path), Handler)
        except OSError:
            raise BridgeError("The local session could not start. Check the folder and stop any existing session.") from None
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            self.server_close()
            raise BridgeError("The local socket could not be made private. Check the folder permissions.") from None

    def server_bind(self):
        super().server_bind()
        self._identity = self.path.lstat().st_ino
        self._owns_path = True

    def service_actions(self):
        with self.broker.mutex:
            self.broker._refresh()

    def handle_error(self, request, client_address):
        # socketserver's default prints arbitrary tracebacks, including input.
        return

    def server_close(self):
        self.broker.handle({"operation": "lock"})
        super().server_close()
        if self._owns_path:
            try:
                if self.path.lstat().st_ino == self._identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass
            self._owns_path = False


def request(path, message):
    validate_socket_path(path)
    encoded = json.dumps(message).encode() + b"\n"
    if len(encoded) > FRAME_LIMIT:
        raise BridgeError("The prompt is too large. Shorten it and try again.")
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(55)
            client.connect(str(path))
            client.sendall(encoded)
            with client.makefile("rb") as stream:
                raw = stream.readline(RESPONSE_LIMIT + 1)
            if len(raw) > RESPONSE_LIMIT or not raw.endswith(b"\n"):
                raise ValueError
            return json.loads(raw)
    except (OSError, ValueError):
        raise BridgeError("Keybridge is unavailable. Start it in your private terminal; avoid automatic retries.") from None
