"""Fixed Vercel credential provisioning, separate from the agent broker protocol."""
import json
import fcntl
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass

from .config import private_directory
from .errors import BridgeError

VARIABLE = "TYPESAFE_API_KEY"
ENVIRONMENTS = {"preview", "production"}
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")


def cli_environment():
    # Existing Vercel CLI authentication is read by the CLI itself. No inherited
    # provider keys, debug flags, node hooks, proxies, or token overrides.
    return {"HOME": str(Path.home()), "PATH": os.defpath + ":/opt/homebrew/bin:/usr/local/bin",
            "LANG": "en_US.UTF-8", "NO_COLOR": "1", "DO_NOT_TRACK": "1"}


@dataclass(frozen=True)
class Destination:
    site: Path
    project: str
    team: str
    targets: tuple[str, ...]

    def __post_init__(self):
        if (not isinstance(self.project, str) or not isinstance(self.team, str)
                or not IDENTIFIER.fullmatch(self.project) or not self.project.startswith("prj_")
                or not IDENTIFIER.fullmatch(self.team) or not self.team.startswith("team_")
                or not self.targets or len(set(self.targets)) != len(self.targets)
                or not set(self.targets) <= ENVIRONMENTS):
            raise BridgeError("Choose a linked Vercel project and Preview and/or Production.")

    @classmethod
    def from_site(cls, site, targets):
        try:
            root = Path(site).expanduser().resolve(strict=True)
            data = json.loads((root / ".vercel/project.json").read_text())
            return cls(root, data["projectId"], data["orgId"], tuple(sorted(targets)))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise BridgeError("The site must already be linked to its Vercel project. Run vercel link in that site first.") from None


class Vercel:
    def __init__(self, destination):
        self.destination = destination
        self.binary = shutil.which("vercel", path=cli_environment()["PATH"])
        if not self.binary:
            raise BridgeError("Install and sign in to the official Vercel CLI first.")

    def _api(self, version, suffix="", body=None):
        d = self.destination
        endpoint = f"/{version}/projects/{d.project}{suffix}?teamId={d.team}"
        command = [self.binary, "api", endpoint, "--raw", "--non-interactive"]
        if body is not None:
            command += ["--method", "POST", "--input", "-"]
        operation = "variable write" if body is not None else (
            "project access check" if not suffix else "variable status check")
        recovery = ("The write may have succeeded. Run deployment-status before trying to send the key again."
                    if body is not None else
                    "This read-only check did not change any Vercel variables. Check network connectivity and Vercel account access.")
        try:
            result = subprocess.run(command, input=json.dumps(body) if body is not None else None,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    cwd=d.site, env=cli_environment(), timeout=60)
        except subprocess.TimeoutExpired:
            raise BridgeError(f"Vercel {operation} timed out. {recovery}") from None
        except OSError:
            raise BridgeError(f"Vercel {operation} could not finish. {recovery}") from None
        if result.returncode:
            raise BridgeError(f"Vercel {operation} failed. {recovery} No automatic retry was made.")
        try:
            if len(result.stdout) > 2_000_000:
                raise ValueError
            return json.loads(result.stdout)
        except (ValueError, TypeError):
            raise BridgeError(f"Vercel {operation} returned an unreadable result. {recovery}") from None

    def project(self):
        data = self._api("v9")
        if (not isinstance(data, dict) or data.get("id") != self.destination.project
                or data.get("accountId") != self.destination.team
                or not isinstance(data.get("name"), str)
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", data["name"])):
            raise BridgeError("Vercel did not confirm the linked project and account. Nothing was written.")
        return {"project": data["id"], "team": data["accountId"], "name": data["name"]}

    def entries(self):
        data = self._api("v10", "/env")
        if (not isinstance(data, dict) or not isinstance(data.get("envs"), list)
                or data.get("hiddenProductionEnvCount", 0)
                or not isinstance(data.get("pagination") or {}, dict)
                or (data.get("pagination") or {}).get("next")):
            raise BridgeError("Vercel did not return a complete variable inventory. Check project access; this read-only check could not verify the saved variables.")
        result = []
        for entry in data["envs"]:
            if not isinstance(entry, dict):
                raise BridgeError("Vercel returned an invalid variable inventory.")
            if entry.get("key") == VARIABLE:
                targets = entry.get("target")
                if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
                    raise BridgeError("Vercel returned invalid variable targets.")
                # Discard secret-bearing fields here; callers get metadata only.
                result.append({k: entry.get(k) for k in ("id", "key", "type", "target", "gitBranch", "comment")})
        return result

    def create(self, key, operation):
        d = self.destination
        body = {"key": VARIABLE, "value": key, "type": "sensitive", "target": list(d.targets),
                "comment": "Keybridge TypeSafe connection " + operation}
        # No upsert: an existing key is never overwritten implicitly.
        data = self._api("v10", "/env", body)
        created = data.get("created") if isinstance(data, dict) else None
        if (not isinstance(created, dict) or data.get("failed") != []
                or not isinstance(created.get("id"), str)):
            raise BridgeError("Vercel did not confirm creation. Check deployment-status before retrying; the write may have succeeded.")
        self.confirm(created, operation)
        return created["id"]

    def confirm(self, entry, operation):
        if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                or not IDENTIFIER.fullmatch(entry["id"])
                or not isinstance(entry.get("target"), list)
                or not all(isinstance(t, str) for t in entry["target"])
                or len(entry["target"]) != len(set(entry["target"]))
                or entry.get("key") != VARIABLE or entry.get("type") != "sensitive"
                or set(entry["target"]) != set(self.destination.targets)
                or entry.get("gitBranch")
                or entry.get("comment") != "Keybridge TypeSafe connection " + operation):
            raise BridgeError("Vercel metadata did not match the requested sensitive variable. Check the dashboard before retrying.")


def state_path(directory, destination):
    return private_directory(directory) / f"typesafe-{destination.team}-{destination.project}.json"


def load_state(directory, destination):
    path = state_path(directory, destination)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise BridgeError("The saved connection record could not be read safely.") from None
    try:
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError
            state = json.load(stream)
        if (not isinstance(state, dict) or state.get("project") != destination.project
                or state.get("team") != destination.team or state.get("variable") != VARIABLE
                or state.get("targets") != list(destination.targets)
                or state.get("phase") not in ("prepared", "submitting", "configured")
                or not isinstance(state.get("operation"), str)
                or not re.fullmatch(r"[a-f0-9]{32}", state["operation"])
                or ("checked_at" in state and (type(state["checked_at"]) is not int or state["checked_at"] <= 0))
                or ("env_id" in state and (not isinstance(state["env_id"], str) or not IDENTIFIER.fullmatch(state["env_id"])) )
                or (state["phase"] in ("submitting", "configured") and not state.get("checked_at"))):
            raise ValueError
        return state
    except (OSError, ValueError, TypeError):
        raise BridgeError("The saved connection record is invalid or its scope differs. Use the original target environments or review the record privately.") from None


def save_state(directory, destination, state):
    path = state_path(directory, destination)
    # Atomic replacement avoids half-written records after interruption. Values
    # are an explicit allowlist, never a copied provider/CLI response.
    allowed = ("project", "team", "variable", "targets", "operation", "phase", "item_id", "field", "model", "checked_at", "env_id")
    clean = {key: state[key] for key in allowed if key in state}
    fd, temporary = tempfile.mkstemp(prefix=".typesafe-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(clean, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def new_state(destination):
    return {"project": destination.project, "team": destination.team, "variable": VARIABLE,
            "targets": list(destination.targets), "operation": uuid.uuid4().hex, "phase": "prepared"}


def matching_entries(entries, destination):
    return [entry for entry in entries if set(entry["target"]) & set(destination.targets)]


def reconcile(vercel, state, directory):
    """Recover an uncertain write without repeating it or reading its value."""
    entries = matching_entries(vercel.entries(), vercel.destination)
    if not entries:
        return False
    if len(entries) != 1 or not state or state.get("phase") not in ("submitting", "configured"):
        raise BridgeError("TYPESAFE_API_KEY already exists for this scope. Keybridge will not replace it; review the Vercel variable first.")
    entry = entries[0]
    vercel.confirm(entry, state["operation"])
    if not isinstance(entry.get("id"), str) or (state.get("env_id") and state["env_id"] != entry["id"]):
        raise BridgeError("The remote variable identity changed. Review the Vercel dashboard.")
    state.update(phase="configured", env_id=entry["id"])
    save_state(directory, vercel.destination, state)
    return True


def status(directory, destination):
    vercel = Vercel(destination)
    project = vercel.project()
    entries = vercel.entries()
    relevant = matching_entries(entries, destination)
    state = load_state(directory, destination)
    managed = False
    if len(relevant) == 1 and state and state.get("phase") in ("submitting", "configured"):
        try:
            vercel.confirm(relevant[0], state["operation"])
            managed = not state.get("env_id") or state["env_id"] == relevant[0].get("id")
        except BridgeError:
            pass
    return {"ok": True, **project, "variable": VARIABLE, "requested_environments": list(destination.targets),
            "present": bool(relevant), "managed_connection": managed,
            "sensitive": bool(relevant) and all(e["type"] == "sensitive" for e in relevant),
            "configured_environments": sorted({t for e in relevant if not e["gitBranch"] for t in e["target"] if t in ENVIRONMENTS}),
            "provider_checked_at": state.get("checked_at") if managed else None,
            "activation": "A new deployment is required; variable presence does not verify the live site."}


@contextmanager
def owner_lock(directory):
    path = private_directory(directory) / "provisioning.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise BridgeError("The provisioning lock could not be acquired safely.") from None
    with os.fdopen(fd, "w") as lock:
        info = os.fstat(lock.fileno())
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise BridgeError("The provisioning lock must be private to your account.")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeError("Another Keybridge connection setup is running. Finish it first.") from None
        yield
