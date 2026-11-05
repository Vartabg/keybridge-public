import copy
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler

from keybridge.errors import BridgeError
from keybridge.providers import NoRedirect
from keybridge.typesafe import ENDPOINT, MODEL, check_connection


def response():
    return {"model": MODEL, "answers": {"connection_check": {
        "type": "choice", "choice": "reading", "confidence": .9,
        "probabilities": {"reading": .95, "other": .05}}}}


class TypeSafeTests(unittest.TestCase):
    def test_one_fixed_check_returns_metadata_only(self):
        body = response()
        body["debug"] = "fixture-credential"
        with patch("keybridge.typesafe.build_opener") as factory:
            factory.return_value.open.return_value = io.BytesIO(json.dumps(body).encode())
            self.assertEqual(check_connection("fixture-credential"), {"model": MODEL, "authenticated": True})
            factory.return_value.open.assert_called_once()
            request = factory.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, ENDPOINT)
            self.assertEqual(request.get_header("Authorization"), "Bearer fixture-credential")
            self.assertNotIn(b"fixture-credential", request.data)
            self.assertEqual(json.loads(request.data)["state"], "A reader opens a book.")
            self.assertEqual(factory.return_value.open.call_args.kwargs["timeout"], 20)
            handlers = factory.call_args.args
            self.assertIsInstance(handlers[0], ProxyHandler)
            self.assertEqual(handlers[0].proxies, {})
            self.assertIsInstance(handlers[1], NoRedirect)

    def test_errors_discard_upstream_details_and_never_retry(self):
        for code in (301, 401, 403, 429, 500):
            with self.subTest(code=code), patch("keybridge.typesafe.build_opener") as factory:
                factory.return_value.open.side_effect = HTTPError(
                    "fixture-credential", code, "fixture-credential", {}, io.BytesIO(b"fixture-credential"))
                with self.assertRaises(BridgeError) as caught:
                    check_connection("fixture-credential")
                self.assertNotIn("fixture-credential", str(caught.exception))
                factory.return_value.open.assert_called_once()
        with patch("keybridge.typesafe.build_opener") as factory:
            factory.return_value.open.side_effect = TimeoutError("fixture-credential")
            with self.assertRaisesRegex(BridgeError, "may have been billed"):
                check_connection("fixture-credential")
            factory.return_value.open.assert_called_once()

    def test_untrusted_or_inconsistent_answers_fail_closed(self):
        cases = [b"x" * 32769, b"[]", b"{}", b"not JSON"]
        for change in ({"type": "score"}, {"choice": "injected"}, {"confidence": True},
                       {"probabilities": {"reading": float("nan"), "other": .05}},
                       {"probabilities": {"reading": .1, "other": .9}},
                       {"probabilities": {"reading": .7}},
                       {"probabilities": {"reading": .95, "other": .95}},
                       {"probabilities": []}):
            body = copy.deepcopy(response())
            body["answers"]["connection_check"].update(change)
            cases.append(json.dumps(body).encode())
        body = response()
        body["model"] = "unexpected-model"
        cases.append(json.dumps(body).encode())
        for raw in cases:
            with self.subTest(raw=raw[:100]), patch("keybridge.typesafe.build_opener") as factory:
                factory.return_value.open.return_value = io.BytesIO(raw)
                with self.assertRaises(BridgeError):
                    check_connection("fixture-credential")
