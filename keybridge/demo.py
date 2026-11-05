"""Offline walkthrough through the real broker and Unix-socket protocol."""
import json
import signal
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from .broker import Broker
from .config import Policy, private_directory
from .errors import BridgeError
from .transport import Server, request


def demo_broker():
    return Broker(Policy("moonshot", ("demo-model",)), "demonstration-value-not-a-real-key",
                  lambda *a: "Demo response. Your prompt was not sent to an API provider.", demo=True)


@contextmanager
def demo_directory():
    """Own temporary state and unwind it on terminal closure or termination."""
    previous = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGHUP)}

    def stop(signum, frame):
        raise KeyboardInterrupt

    try:
        for number in previous:
            signal.signal(number, stop)
        try:
            temporary = tempfile.TemporaryDirectory(prefix=".kb-demo-", dir=".")
        except OSError:
            raise BridgeError("The guided demo needs a writable current folder. Run it from a writable checkout.") from None
        with temporary as name:
            # Relative addresses fit even when the checkout has a long path.
            yield private_directory(Path(name).absolute().relative_to(Path.cwd()))
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def guided_demo():
    print("[Keybridge] DEMONSTRATION: fake credentials; no vault, provider or cloud access.")
    print("Attempts and output-token limits are not dollar caps. This demo costs nothing.")
    # Never reuse or clean an owner session.
    with demo_directory() as directory:
        print(f"Demo directory: {directory.absolute()}")
        with Server(directory / "session.sock", demo_broker()) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05})
            thread.start()
            try:
                walkthrough(directory / "session.sock")
            finally:
                server.shutdown()
                thread.join()
    print("[Keybridge] Demo state removed. Run the same command to try again.")
    return 0


def walkthrough(path):
    steps = [
        ("1/5 Approved model, attempts remaining, expiry in seconds and output-token cap per call",
         {"operation": "status"}),
        ("2/5 Ask the approved model using demonstration data",
         {"operation": "generate", "model": "demo-model", "prompt": "Explain this demonstration."}),
        ("3/5 History contains request metadata only",
         {"operation": "history"}),
        ("4/5 Lock this demo session",
         {"operation": "lock"}),
        ("5/5 Try another request after lock (expected denial)",
         {"operation": "generate", "model": "demo-model", "prompt": "Try again after lock."}),
    ]
    for index, (label, message) in enumerate(steps):
        print(f"[Keybridge] {label}")
        result = request(path, message)
        print(json.dumps(result, ensure_ascii=True))
        expected_success = index < len(steps) - 1
        # A generic failure does not prove revocation. Confirm the lock reply
        # and the broker's denial reason before reporting demonstration success.
        if (result.get("ok") is not expected_success
                or (message["operation"] == "lock" and result.get("state") != "locked")
                or (not expected_success and result.get("error") !=
                    "This session is locked or expired. Unlock it in your private terminal.")):
            raise BridgeError("The demonstration did not complete as expected. Stop and report this step.")
    print("[Keybridge] Post-lock request denied. A live session requires the owner's private unlock.")
