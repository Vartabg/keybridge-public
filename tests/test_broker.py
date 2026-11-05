import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from keybridge.broker import Broker
from keybridge.config import Policy


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.calls = []
        self.secret = "example-credential-for-tests-only"
        self.policy = Policy("moonshot", ("test-model",), 2, 60, 256)
        self.broker = Broker(self.policy, self.secret, self.send, clock=lambda: self.now[0])

    def send(self, provider, key, model, prompt, tokens):
        self.calls.append((provider, key, model, prompt, tokens))
        return "Response: " + key

    def request(self, **extra):
        return {"operation": "generate", "model": "test-model", "prompt": "Hello", **extra}

    def test_generate_redacts_key_and_counts_attempt(self):
        result = self.broker.handle(self.request())
        self.assertTrue(result["ok"])
        self.assertNotIn(self.secret, json.dumps(result))
        self.assertIn("[redacted]", result["text"])
        self.assertEqual(self.calls[0][-1], 256)
        self.assertEqual(self.broker.handle({"operation": "status"})["remaining_calls"], 1)

    def test_agent_cannot_supply_headers_url_or_change_limits(self):
        for extra in ({"url": "https://example.org"}, {"headers": {}},
                      {"max_tokens": 99999}, {"provider": "other"}):
            self.assertFalse(self.broker.handle(self.request(**extra))["ok"])
        self.assertEqual(self.calls, [])

    def test_unknown_model_or_operation_and_invalid_shapes(self):
        for request in (self.request(model="other"), self.request(prompt=[]),
                        self.request(prompt="x" * 16001), {"operation": "get_secret"},
                        [], None, {"operation": []}, {"operation": "lock", "extra": True}):
            self.assertFalse(self.broker.handle(request)["ok"])
        self.assertEqual(self.calls, [])

    def test_invalid_prompts_never_reach_provider_or_consume_allowance(self):
        for prompt in ("", " " * 16000, "\t\n", " " * 16000 + "x", "x" + " " * 16000):
            with self.subTest(length=len(prompt)):
                result = self.broker.handle(self.request(prompt=prompt))
                self.assertFalse(result["ok"])
                self.assertIn("between 1 and 16000 characters", result["error"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.broker.attempts, 0)
        self.assertEqual(self.broker.handle({"operation": "history"})["events"], [])

    def test_prompt_boundary_preserves_original_whitespace(self):
        prompt = " " + "x" * 15998 + " "
        result = self.broker.handle(self.request(prompt=prompt))
        self.assertTrue(result["ok"])
        self.assertEqual(self.calls[0][3], prompt)
        self.assertEqual(self.broker.attempts, 1)

    def test_expiry_is_enforced_and_secret_reference_cleared(self):
        self.now[0] += 61
        self.assertFalse(self.broker.handle(self.request())["ok"])
        self.assertEqual(self.broker.handle({"operation": "status"})["state"], "expired")
        self.assertIsNone(self.broker._key)

    def test_lock_blocks_calls(self):
        self.assertTrue(self.broker.handle({"operation": "lock"})["ok"])
        self.assertFalse(self.broker.handle(self.request())["ok"])
        self.assertIsNone(self.broker._key)

    def test_limit_holds_under_concurrent_requests(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(self.broker.handle, [self.request()] * 20))
        self.assertEqual(sum(r["ok"] for r in results), 2)
        self.assertEqual(len(self.calls), 2)

    def test_errors_do_not_echo_key_or_refund_attempts(self):
        def fail(*args):
            raise RuntimeError(self.secret)
        self.broker.send = fail
        for _ in range(2):
            response = self.broker.handle(self.request())
            self.assertFalse(response["ok"])
            self.assertNotIn(self.secret, json.dumps(response))
        self.assertFalse(self.broker.handle(self.request())["ok"])

    def test_excess_request_does_not_cancel_reserved_calls(self):
        entered, released = threading.Event(), threading.Event()
        def slow(*args):
            entered.set()
            released.wait(2)
            return "approved answer"
        self.broker.send = slow
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.broker.handle, self.request())
            self.assertTrue(entered.wait(1))
            second = pool.submit(self.broker.handle, self.request())
            # Wait for both reservations without sleeping or a network call.
            for _ in range(1000):
                with self.broker.mutex:
                    reserved = self.broker.attempts == 2
                if reserved:
                    break
                threading.Event().wait(.001)
            self.assertTrue(reserved)
            self.assertFalse(self.broker.handle(self.request())["ok"])
            released.set()
            self.assertTrue(first.result()["ok"])
            self.assertTrue(second.result()["ok"])
        self.assertIsNone(self.broker._key)
        self.assertEqual(self.broker.handle({"operation": "status"})["state"], "exhausted")

    def test_revocation_withholds_inflight_response(self):
        entered, released = threading.Event(), threading.Event()
        def slow(*args):
            entered.set()
            released.wait(2)
            return "late answer"
        self.broker.send = slow
        with ThreadPoolExecutor(max_workers=1) as pool:
            call = pool.submit(self.broker.handle, self.request())
            self.assertTrue(entered.wait(1))
            self.broker.handle({"operation": "lock"})
            released.set()
            self.assertFalse(call.result()["ok"])

    def test_history_contains_metadata_only(self):
        self.broker.handle(self.request(prompt="private prompt"))
        record = json.dumps(self.broker.handle({"operation": "history"}))
        self.assertNotIn("private prompt", record)
        self.assertNotIn(self.secret, record)
        self.assertNotIn("Response:", record)

    def test_demo_is_identified(self):
        self.broker.demo = True
        result = self.broker.handle(self.request())
        self.assertTrue(result["demo"])

    def test_status_exposes_configured_output_cap(self):
        status = self.broker.handle({"operation": "status"})
        self.assertEqual(status["max_output_tokens"], 256)
        self.assertEqual(status["models"], ("test-model",))
        self.assertEqual(status["remaining_calls"], 2)
        self.assertEqual(status["expires_in_seconds"], 60)


if __name__ == "__main__":
    unittest.main()
