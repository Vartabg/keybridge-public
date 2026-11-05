"""Owner-only connection setup. This module is never exposed on the agent socket."""
import json
import shutil
import subprocess
import sys
import time

from .config import MISSING_BITWARDEN, private_directory, vault_environment
from .deployment import (Destination, Vercel, load_state, new_state, reconcile,
                         save_state, owner_lock)
from .errors import BridgeError
from .onboarding import choose
from .typesafe import MODEL, check_connection
from .vault import Vault, field_choices


def ensure_login():
    private_directory(vault_environment()["BITWARDENCLI_APPDATA_DIR"])
    binary = shutil.which("bw", path=vault_environment()["PATH"])
    if not binary:
        raise BridgeError(MISSING_BITWARDEN)
    try:
        checked = subprocess.run([binary, "status"], capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL, env=vault_environment(), timeout=20)
        status = json.loads(checked.stdout).get("status") if checked.returncode == 0 else None
        if status not in ("unauthenticated", "locked", "unlocked"):
            raise ValueError
        if status == "unauthenticated":
            print("First use: sign into Bitwarden with your email and master password in this private terminal.", flush=True)
            print("If asked for an emailed verification code, enter it only at that sign-in prompt.", flush=True)
            # The interactive CLI owns its password/2FA prompts. Never run this
            # path in an agent-captured terminal; guard lives at entry below.
            login = subprocess.run([binary, "login", "--quiet"], env=vault_environment(), timeout=600)
            if login.returncode:
                raise BridgeError("Bitwarden sign-in did not finish. No provider check or Vercel write was made.")
        # Lock only Keybridge's profile before using its existing private unlock.
        locked = subprocess.run([binary, "lock"], capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, env=vault_environment(), timeout=20)
        if locked.returncode:
            raise BridgeError("Keybridge could not lock its Bitwarden profile. Finish setup in your private terminal.")
        if status == "unauthenticated":
            print("Sign-in succeeded. Keybridge locked the vault after signing in.", flush=True)
            print("Next, enter the same master password once more to open the vault for this connection.", flush=True)
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        raise BridgeError("Bitwarden setup did not finish. Check its CLI in your private terminal; no key was transferred.") from None


def select_reference(vault):
    items = vault.items()
    preferred = [item for item in items if any(word in str(item.get("name", "")).lower() for word in ("typesafe", "type safe", "jev"))]
    choices = preferred or items
    if not choices:
        raise BridgeError("Save the TypeSafe key in Bitwarden first, then run this connection again.")
    print("Choose the TypeSafe API credential. Only names and field labels are shown here.")
    if preferred:
        print("Showing entries whose names mention TypeSafe or Jev.")
        # Offer all entries without guessing that a similarly named one is right.
        choices = [*preferred, None]
    item = choose("Vault item number:", choices, lambda entry: "Show all entries" if entry is None else entry.get("name", "Unnamed item"))
    if item is None:
        item = choose("Vault item number:", items, lambda entry: entry.get("name", "Unnamed item"))
    fields = field_choices(item)
    field = fields[0] if len(fields) == 1 else choose("API-key field number:", fields)
    return item["id"], field


def connect_typesafe(directory, site, targets, choose_again=False, retry_unconfirmed=False):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise BridgeError("Run connect-typesafe in your own private Terminal. Never enter vault credentials in an agent-captured terminal.")
    with owner_lock(directory):
        return _connect_typesafe(directory, site, targets, choose_again, retry_unconfirmed)


def _connect_typesafe(directory, site, targets, choose_again, retry_unconfirmed):
    print("[1/5] Checking the Vercel project and existing connection. The vault stays closed for this check.", flush=True)
    destination = Destination.from_site(site, targets)
    vercel = Vercel(destination)
    project = vercel.project()
    state = load_state(directory, destination)
    if reconcile(vercel, state, directory):
        print("Connection already verified. No vault unlock, TypeSafe call or new key transfer was needed.", flush=True)
        return {"ok": True, "result": "already_configured", "project": project["name"],
                "environments": list(destination.targets), "next": "Create a new deployment to activate the server variable."}
    if state and state["phase"] == "submitting" and not retry_unconfirmed:
        raise BridgeError("The previous write is unresolved and no matching variable is visible yet. Check deployment-status and the Vercel dashboard. Only after confirming no variable exists, rerun with --retry-unconfirmed.")
    state = state or new_state(destination)
    print(f"Connect TypeSafe to Vercel project {project['name']} ({project['project']}).")
    print(f"Account: {project['team']}. Environments: {', '.join(destination.targets)}.")
    print("The key goes only to TypeSafe for one small connection check, then to Vercel as sensitive TYPESAFE_API_KEY.")
    print("The TypeSafe check can incur its normal API charge. No retries, deployment, or existing-key replacement happen here.")
    print("After setup, the server can use the credential until you revoke it in Vercel or TypeSafe; a local Keybridge lock does not revoke it.")
    print("[2/5] Sign in or unlock Bitwarden here, then choose your TypeSafe entry.", flush=True)
    ensure_login()
    key = None
    try:
        with Vault() as vault:
            if choose_again or not state.get("item_id") or not state.get("field"):
                state["item_id"], state["field"] = select_reference(vault)
            key = vault.key(state["item_id"], state["field"])
        # Vault is locked before either external service receives the key.
        state["phase"] = "prepared"
        for previous in ("env_id", "checked_at", "model"):
            state.pop(previous, None)
        save_state(directory, destination, state)
        print("[3/5] Bitwarden is locked. Checking the selected key with TypeSafe once.", flush=True)
        check_connection(key)
        state.update(model=MODEL, checked_at=int(time.time()))
        # Recheck destination/inventory after the owner's interactive unlock.
        print("[4/5] TypeSafe accepted the key. Rechecking the destination before sending it to Vercel.", flush=True)
        vercel.project()
        if reconcile(vercel, state, directory):
            raise BridgeError("The destination changed during setup. Review its metadata before proceeding.")
        state["phase"] = "submitting"
        save_state(directory, destination, state)
        print("Sending the key to Vercel as a sensitive server variable.", flush=True)
        state["env_id"] = vercel.create(key, state["operation"])
        print("[5/5] Vercel accepted the write. Verifying the saved variable's metadata.", flush=True)
        try:
            confirmed = reconcile(vercel, state, directory)
        except BridgeError:
            raise BridgeError("Vercel accepted the write, but verification could not finish. Run deployment-status before any retry. The saved receipt can recover this connection without another key transfer.") from None
        if not confirmed:
            raise BridgeError("Vercel accepted the write, but verification is pending. Run deployment-status before any retry; do not repeat the write.")
        print("Connected: the sensitive TypeSafe variable is verified. A new deployment is still required to activate it.", flush=True)
        return {"ok": True, "result": "configured", "project": project["name"],
                "environments": list(destination.targets), "provider_check": "passed",
                "next": "Create a new deployment to activate the server variable."}
    finally:
        key = None  # Reference clearing, not a claim of memory zeroization.
