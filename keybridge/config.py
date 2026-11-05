"""Nonsecret settings. Never import environment credentials into the broker."""
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import BridgeError

PROVIDERS = ("moonshot", "openrouter", "gemini")
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}\Z")
CLI_SYSTEM_PATH = os.pathsep.join((os.defpath, "/opt/homebrew/bin", "/usr/local/bin"))
MISSING_BITWARDEN = (
    "Keybridge could not find the Bitwarden CLI (bw) in its supported locations. "
    "If it is already installed, check its location against the README; "
    "otherwise install the official CLI: npm install -g @bitwarden/cli"
)


def clean_process_environment():
    # Drop inherited credentials without inspecting or printing their values.
    keep = {"HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR"}
    for name in list(os.environ):
        if name not in keep:
            del os.environ[name]


def valid_model(model):
    return (isinstance(model, str) and bool(MODEL.fullmatch(model))
            and ".." not in model and "//" not in model)


@dataclass(frozen=True)
class Policy:
    provider: str
    models: tuple[str, ...]
    max_calls: int = 10
    duration_seconds: int = 3600
    max_output_tokens: int = 1024

    def __post_init__(self):
        if self.provider not in PROVIDERS:
            raise BridgeError("Choose a supported API service.")
        if not isinstance(self.models, (list, tuple)) or not 1 <= len(self.models) <= 8:
            raise BridgeError("Choose between one and eight model IDs.")
        if not all(valid_model(m) for m in self.models):
            raise BridgeError("Use model IDs from your service's model list.")
        if self.provider == "gemini" and any("/" in m or ":" in m for m in self.models):
            raise BridgeError("Use the Gemini model ID without a path or URL.")
        for value, low, high in ((self.max_calls, 1, 100),
                                 (self.duration_seconds, 60, 28800),
                                 (self.max_output_tokens, 1, 8192)):
            if type(value) is not int or not low <= value <= high:
                raise BridgeError("Session limits are outside the allowed range. Run setup again.")


def private_directory(path=None):
    directory = Path(path) if path else Path.home() / ".local" / "share" / "keybridge"
    if directory.is_symlink():
        raise BridgeError("The Keybridge folder must not be a symbolic link.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise BridgeError("The Keybridge folder must belong to you with permission 700.")
    return directory


def write_settings(directory, policy, item_id, field):
    target = directory / "connection.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(target, flags, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump({"policy": asdict(policy), "item_id": item_id, "field": field}, stream, indent=2)


def read_settings(directory):
    try:
        fd = os.open(directory / "connection.json", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise BridgeError("Connection settings must be private to your user (permission 600).")
            value = json.load(stream)
        policy = Policy(**value["policy"])
        if not isinstance(value["item_id"], str) or not isinstance(value["field"], str):
            raise ValueError
        return policy, value["item_id"], value["field"]
    except (OSError, ValueError, KeyError, TypeError):
        raise BridgeError("Connection settings are missing or invalid. Run setup first.") from None


def vault_environment(session=None, password=None):
    # Minimal child environment: no inherited provider keys, BW_SESSION, debug
    # flags, NODE_OPTIONS, proxies, or caller-supplied CLI application directory.
    # Support the per-user npm prefix without trusting the caller's PATH.
    cli_path = os.pathsep.join((CLI_SYSTEM_PATH, str(Path.home() / ".npm-global/bin")))
    result = {"PATH": cli_path,
              "HOME": str(Path.home()), "LANG": "en_US.UTF-8",
              "BITWARDENCLI_APPDATA_DIR": str(Path.home() / ".local/share/keybridge/bitwarden")}
    if session is not None:
        result["BW_SESSION"] = session
    if password is not None:
        result["KEYBRIDGE_MASTER_PASSWORD"] = password
    return result
