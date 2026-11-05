"""Fixed provider requests: no arbitrary URLs, headers, tools, or redirects."""
import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .config import PROVIDERS, valid_model
from .errors import BridgeError

MAX_RESPONSE = 524288


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def make_request(provider, key, model, prompt, tokens):
    if provider not in PROVIDERS or not valid_model(model):
        raise BridgeError("The requested service or model is not supported.")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if provider == "gemini":
        if "/" in model or ":" in model:
            raise BridgeError("Use a Gemini model ID without a path or URL.")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers["x-goog-api-key"] = key
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": tokens}}
    else:
        url = {"moonshot": "https://api.moonshot.ai/v1/chat/completions",
               "openrouter": "https://openrouter.ai/api/v1/chat/completions"}[provider]
        headers["Authorization"] = "Bearer " + key
        body = {"model": model, "messages": [{"role": "user", "content": prompt}],
                "max_tokens": tokens, "stream": False}
    return Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")


def generate(provider, key, model, prompt, tokens):
    request = make_request(provider, key, model, prompt, tokens)
    # Empty ProxyHandler ignores inherited proxy settings. Default HTTPS
    # handling validates certificates; redirects fail instead of forwarding keys.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=45) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise BridgeError("The service response was too large. Try a smaller request.")
        data = json.loads(raw)
        if provider == "gemini":
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p["text"] for p in parts if "text" in p and not p.get("thought"))
        else:
            text = data["choices"][0]["message"]["content"]
        if not isinstance(text, str) or not text:
            raise ValueError
        return text
    except HTTPError as exc:
        exc.close()
        if exc.code in (401, 403):
            raise BridgeError("The service rejected authentication. Check this connection in Bitwarden.") from None
        if exc.code == 429:
            raise BridgeError("The service reached a usage limit. Check your provider account before retrying.") from None
        raise BridgeError("The service rejected the request. Check the model and provider account.") from None
    except (URLError, TimeoutError, OSError):
        raise BridgeError("The service could not be reached. This attempt was counted; retry only if appropriate.") from None
    except (ValueError, KeyError, IndexError, TypeError):
        raise BridgeError("The service did not return supported text. Check the chosen model.") from None
