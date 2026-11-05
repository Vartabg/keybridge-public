"""Private-terminal Bitwarden adapter; all CLI output is captured in process."""
import getpass
import json
import re
import shutil
import subprocess

from .config import MISSING_BITWARDEN, vault_environment
from .errors import BridgeError


def field_choices(item):
    choices = []
    if (item.get("login") or {}).get("password"):
        choices.append("login.password")
    for field in item.get("fields") or []:
        if field.get("name") and field.get("value"):
            choices.append("field:" + field["name"])
    if item.get("notes"):
        choices.append("notes")
    return choices


def extract_key(item, field):
    if field == "login.password":
        value = (item.get("login") or {}).get("password")
    elif field == "notes":
        value = item.get("notes")
    elif field.startswith("field:"):
        matches = [f.get("value") for f in item.get("fields") or [] if f.get("name") == field[6:]]
        value = matches[0] if len(matches) == 1 else None
    else:
        value = None
    if not isinstance(value, str) or not 8 <= len(value) <= 4096 or re.search(r"[^\x21-\x7e]", value):
        raise BridgeError("Choose a field containing only the API key, with no spaces or explanatory text.")
    return value


class Vault:
    def __init__(self):
        self.binary = shutil.which("bw", path=vault_environment()["PATH"])
        self.session = None
        if not self.binary:
            raise BridgeError(MISSING_BITWARDEN)

    def _run(self, args, *, password=None):
        try:
            result = subprocess.run([self.binary, *args], capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL, timeout=120,
                                    env=vault_environment(self.session, password))
        except (OSError, subprocess.TimeoutExpired):
            raise BridgeError("Bitwarden could not complete the request. Check its CLI in your private terminal.") from None
        if result.returncode:
            if args[0] == "unlock":
                raise BridgeError("Bitwarden did not accept the unlock request. Run setup again and enter your Bitwarden master password at the password prompt. The emailed verification code is only for sign-in.")
            raise BridgeError("Bitwarden rejected the request. Check your login or unlock in your private terminal.")
        return result.stdout

    def __enter__(self):
        try:
            status = json.loads(self._run(["status"]))
        except ValueError:
            raise BridgeError("Bitwarden returned an unreadable status. Check the CLI installation.") from None
        if status.get("status") == "unauthenticated":
            raise BridgeError("Log in to Keybridge's dedicated Bitwarden CLI profile first. See the README's private setup command.")
        print("Enter your Bitwarden master password here. The emailed verification code is only for sign-in.", flush=True)
        print("Typing is hidden: no characters or dots will appear. Press Return when finished.", flush=True)
        password = getpass.getpass("Bitwarden master password (hidden input): ")
        if not password:
            raise BridgeError("No master password was entered. No unlock request was sent. Run setup again and enter your Bitwarden master password, then press Return.")
        self.session = self._run(["unlock", "--passwordenv", "KEYBRIDGE_MASTER_PASSWORD", "--raw"], password=password).strip()
        if not self.session:
            raise BridgeError("Bitwarden did not unlock. Try again in your private terminal.")
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            self._run(["lock"])
        finally:
            self.session = None

    def items(self):
        try:
            items = json.loads(self._run(["list", "items"]))
            if not isinstance(items, list):
                raise ValueError
            return [item for item in items if isinstance(item, dict) and field_choices(item)]
        except (ValueError, TypeError, AttributeError):
            raise BridgeError("Bitwarden items could not be read. Check your vault in its app.") from None

    def key(self, item_id, field):
        if not re.fullmatch(r"[a-fA-F0-9-]{36}", item_id):
            raise BridgeError("The saved vault reference is invalid. Run start --configure.")
        try:
            item = json.loads(self._run(["get", "item", item_id]))
            return extract_key(item, field)
        except (ValueError, TypeError, AttributeError):
            raise BridgeError("The selected Bitwarden item is unavailable. Run start --configure.") from None
