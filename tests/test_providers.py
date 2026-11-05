import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from keybridge.errors import BridgeError
from keybridge.providers import NoRedirect, make_request, generate


class ProviderTests(unittest.TestCase):
    def test_fixed_urls_and_auth_header(self):
        expected = {"moonshot": "https://api.moonshot.ai/v1/chat/completions",
                    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
                    "gemini": "https://generativelanguage.googleapis.com/v1beta/models/test-model:generateContent"}
        for provider, url in expected.items():
            request = make_request(provider, "example-value", "test-model", "hello", 256)
            self.assertEqual(request.full_url, url)
            self.assertNotIn("example-value", request.full_url)
            body = json.loads(request.data)
            self.assertNotIn("example-value", json.dumps(body))
            if provider == "gemini":
                self.assertEqual(body["generationConfig"]["maxOutputTokens"], 256)
                self.assertEqual(request.get_header("X-goog-api-key"), "example-value")
            else:
                self.assertEqual(body["max_tokens"], 256)
                self.assertEqual(request.get_header("Authorization"), "Bearer example-value")

    def test_path_escape_and_invalid_provider_rejected(self):
        for model in ("../../secrets", "model?key=x", "https://example.org", "x\r\ny"):
            with self.assertRaises(BridgeError):
                make_request("gemini", "example-value", model, "hi", 256)
        with self.assertRaises(BridgeError):
            make_request("unknown", "example-value", "model", "hi", 256)

    def test_redirects_are_not_followed(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "found", {}, "https://evil.example"))

    def test_only_text_is_returned(self):
        body = {"choices": [{"message": {"content": "hello"}}], "debug": "discard me"}
        with patch("keybridge.providers.build_opener") as factory:
            factory.return_value.open.return_value = io.BytesIO(json.dumps(body).encode())
            self.assertEqual(generate("moonshot", "example-value", "test-model", "hi", 256), "hello")

    def test_gemini_text_extraction(self):
        body = {"candidates": [{"content": {"parts": [{"text": "hello"}, {"text": " world"}]}}]}
        with patch("keybridge.providers.build_opener") as factory:
            factory.return_value.open.return_value = io.BytesIO(json.dumps(body).encode())
            self.assertEqual(generate("gemini", "example-value", "test-model", "hi", 256), "hello world")

    def test_http_error_is_generic(self):
        with patch("keybridge.providers.build_opener") as factory:
            factory.return_value.open.side_effect = HTTPError("secret-url", 401, "secret", {}, None)
            with self.assertRaisesRegex(BridgeError, "authentication") as result:
                generate("moonshot", "example-value", "test-model", "hi", 256)
            self.assertNotIn("secret", str(result.exception))

    def test_oversized_or_invalid_response_rejected(self):
        for body in (b"x" * 524289, b"not json", b"{}", b"[]"):
            with patch("keybridge.providers.build_opener") as factory:
                factory.return_value.open.return_value = io.BytesIO(body)
                with self.assertRaises(BridgeError):
                    generate("moonshot", "example-value", "test-model", "hi", 256)


if __name__ == "__main__":
    unittest.main()
