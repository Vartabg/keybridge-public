"""One fixed, non-personal TypeSafe connection check; never returns response text."""
import json
import math
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .errors import BridgeError
from .providers import NoRedirect

MODEL = "jev-1.13.0"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"


def check_connection(key):
    body = {"model": MODEL, "state": "A reader opens a book.", "questions": {
        "connection_check": {"type": "choice", "instructions": "Classify the topic of this sentence.",
                             "criteria": {"reading": "Reading a book", "other": "Another topic"}}
    }}
    request = Request(ENDPOINT, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=20) as response:
            raw = response.read(32769)
        if len(raw) > 32768:
            raise ValueError
        data = json.loads(raw)
        answer = data["answers"]["connection_check"]
        probabilities = answer["probabilities"]
        valid = lambda value: type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
        if (data["model"] != MODEL or answer["type"] != "choice"
                or answer["choice"] not in ("reading", "other")
                or set(probabilities) != {"reading", "other"}
                or not all(valid(p) for p in probabilities.values())
                or abs(sum(probabilities.values()) - 1) > .02
                or not valid(answer["confidence"])
                or probabilities[answer["choice"]] != max(probabilities.values())):
            raise ValueError
        return {"model": MODEL, "authenticated": True}
    except HTTPError as error:
        code = error.code
        error.close()
        if code in (401, 403):
            raise BridgeError("TypeSafe rejected this credential. Check the selected Bitwarden entry.") from None
        if code == 429:
            raise BridgeError("TypeSafe reached a usage limit. No Vercel variable was written.") from None
        raise BridgeError("TypeSafe rejected the connection check. No Vercel variable was written.") from None
    except (URLError, TimeoutError, OSError):
        raise BridgeError("The TypeSafe check could not finish; it may have been billed. No Vercel variable was written. There was no retry.") from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise BridgeError("TypeSafe returned an unexpected response. No Vercel variable was written.") from None
