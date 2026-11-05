"""Session policy and revocation; upstream credentials never enter responses."""
import base64
import threading
import time
from urllib.parse import quote

from .errors import BridgeError
from .providers import generate


class Broker:
    def __init__(self, policy, key, send=generate, *, clock=time.monotonic, demo=False):
        self.policy, self._key, self.send = policy, key, send
        self.clock, self.demo = clock, demo
        self.deadline = clock() + policy.duration_seconds
        self.attempts = 0
        self.active = 0
        self.events = []
        self.mutex = threading.Lock()
        self.state = "ready"

    def _refresh(self):
        if self.state == "ready" and self.clock() >= self.deadline:
            self.state, self._key = "expired", None

    def handle(self, request):
        try:
            return self._handle(request)
        except BridgeError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            # Do not format arbitrary exceptions: provider/library failures can
            # include request headers. Errors are fail-closed, not tracebacks.
            return {"ok": False, "error": "The request failed. Check connection status before trying again."}

    def _handle(self, request):
        if not isinstance(request, dict) or not isinstance(request.get("operation"), str):
            raise BridgeError("Send a supported Keybridge request.")
        operation = request["operation"]
        fields = {"operation", "model", "prompt"} if operation == "generate" else {"operation"}
        if set(request) != fields:
            raise BridgeError("This operation does not accept those fields.")
        with self.mutex:
            self._refresh()
            if operation == "lock":
                self.state, self._key = "locked", None
                return {"ok": True, "state": "locked"}
            if operation == "status":
                return {"ok": True, "state": self.state, "demo": self.demo,
                        "provider": self.policy.provider, "models": self.policy.models,
                        "remaining_calls": max(0, self.policy.max_calls - self.attempts),
                        "max_output_tokens": self.policy.max_output_tokens,
                        "expires_in_seconds": max(0, int(self.deadline - self.clock()))}
            if operation == "history":
                return {"ok": True, "events": list(self.events)}
            if operation != "generate":
                raise BridgeError("This operation is not available.")
            if self.state != "ready":
                raise BridgeError("This session is locked or expired. Unlock it in your private terminal.")
            if self.attempts >= self.policy.max_calls:
                raise BridgeError("This session has used its call allowance. Start a new approved session.")
            model, prompt = request["model"], request["prompt"]
            if not isinstance(model, str) or model not in self.policy.models:
                raise BridgeError("That model was not approved for this session.")
            if not isinstance(prompt, str) or not 1 <= len(prompt) <= 16000 or not prompt.strip():
                raise BridgeError("Send a text prompt between 1 and 16000 characters.")
            self.attempts += 1  # Reserve before network access; failures still count.
            self.active += 1
            key = self._key
            event = {"attempt": self.attempts, "provider": self.policy.provider,
                     "model": model, "time": int(time.time()), "state": "pending"}
            self.events.append(event)
        try:
            text = self.send(self.policy.provider, key, model, prompt, self.policy.max_output_tokens)
            if not isinstance(text, str):
                raise BridgeError("The service did not return supported text.")
            for value in (key, quote(key, safe=""), base64.b64encode(key.encode()).decode()):
                text = text.replace(value, "[redacted]")
            with self.mutex:
                self._refresh()
                if self.state != "ready":
                    raise BridgeError("The session ended during this request. Its response was withheld.")
                event["state"] = "completed"
            return {"ok": True, "demo": self.demo, "text": text}
        except Exception:
            with self.mutex:
                event["state"] = "failed"
            raise
        finally:
            with self.mutex:
                self.active -= 1
                if self.active == 0 and self.attempts >= self.policy.max_calls and self.state == "ready":
                    self.state, self._key = "exhausted", None
