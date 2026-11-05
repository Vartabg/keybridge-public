"""Human starts a private session; agents use status, ask, history, and lock."""
import argparse
import json
import signal
import sys

from .broker import Broker
from .config import clean_process_environment, private_directory, read_settings
from .demo import demo_broker, guided_demo
from .errors import BridgeError
from .onboarding import configure
from .transport import Server, request, validate_socket_path
from .vault import Vault


def parser():
    cli = argparse.ArgumentParser(description="Use approved APIs without copying keys into agent conversations.")
    cli.add_argument("--directory", help="Private Keybridge state folder (default: ~/.local/share/keybridge)")
    commands = cli.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="Privately unlock Bitwarden and start an approved session")
    start.add_argument("--configure", action="store_true", help="Choose a connection and model permissions")
    demo = commands.add_parser("demo", help="Start a fake-key session; makes no provider calls")
    demo.add_argument("--guided", action="store_true", help="Run an isolated walkthrough and clean up automatically")
    commands.add_parser("status", help="Show connection and session limits")
    commands.add_parser("history", help="Show this session's request metadata")
    commands.add_parser("lock", help="Revoke this session; cannot undo already-sent requests")
    ask = commands.add_parser("ask", help="Read a prompt from stdin and return JSON")
    ask.add_argument("--model", required=True)
    for name, help_text in [("connect-typesafe", "Privately connect a Bitwarden TypeSafe key to one linked Vercel project"),
                            ("deployment-status", "Show Vercel connection metadata, never key values")]:
        connection = commands.add_parser(name, help=help_text)
        connection.add_argument("--site", required=True, help="Directory containing the site's .vercel/project.json")
        connection.add_argument("--target", action="append", choices=("preview", "production"), help="Repeat for both; default is preview")
        if name == "connect-typesafe":
            connection.add_argument("--retry-unconfirmed", action="store_true", help="Retry an uncertain write only after privately checking that Vercel has no variable")
            connection.add_argument("--choose-again", action="store_true", help="Choose a different vault reference before first successful setup")
    return cli


def serve(directory, broker):
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    with Server(directory / "session.sock", broker) as server:
        print("[Keybridge] Session started. Agents can use status, ask, history, and lock.", flush=True)
        print("[Keybridge] Leave this terminal open. Control-C stops the service.", flush=True)
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            print("\n[Keybridge] Session stopped.", flush=True)


def run(args):
    if args.command == "demo" and args.guided:
        if args.directory:
            raise BridgeError("Run demo --guided without --directory; it creates its own isolated state.")
        return guided_demo()
    directory = private_directory(args.directory)
    if args.command in ("connect-typesafe", "deployment-status"):
        targets = args.target or ["preview"]
        if args.command == "connect-typesafe":
            from .owner import connect_typesafe
            result = connect_typesafe(directory, args.site, targets, args.choose_again, args.retry_unconfirmed)
        else:
            from .deployment import Destination, status
            result = status(directory, Destination.from_site(args.site, targets))
        print(json.dumps(result, ensure_ascii=True))
        return 0
    if args.command == "demo":
        print("[Keybridge] DEMO: fake credentials, no vault access or paid calls.", flush=True)
        return serve(directory, demo_broker())
    if args.command == "start":
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise BridgeError("Start must run in your own private terminal, outside the agent's captured terminal.")
        validate_socket_path(directory / "session.sock")
        if (directory / "session.sock").exists() or (directory / "session.sock").is_symlink():
            raise BridgeError("Stop the existing session before starting another.")
        with Vault() as vault:
            if args.configure or not (directory / "connection.json").exists():
                policy, item_id, field = configure(directory, vault)
            else:
                policy, item_id, field = read_settings(directory)
            key = vault.key(item_id, field)
        # Bitwarden is locked before the agent interface opens. Keep only the
        # selected API credential, never the vault session or master password.
        broker = Broker(policy, key)
        del key
        return serve(directory, broker)
    message = {"operation": args.command}
    if args.command == "ask":
        prompt = sys.stdin.read(16001)
        if len(prompt) > 16000:
            raise BridgeError("The prompt is too large. Limit it to 16000 characters.")
        message = {"operation": "generate", "model": args.model, "prompt": prompt}
    result = request(directory / "session.sock", message)
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get("ok") else 1


def main():
    clean_process_environment()
    try:
        code = run(parser().parse_args())
    except BridgeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        code = 1
    except (KeyboardInterrupt, EOFError):
        print("[Keybridge] Cancelled.", file=sys.stderr)
        code = 1
    except Exception:
        print(json.dumps({"ok": False, "error": "Keybridge could not finish. Check setup and try again in your private terminal."}), file=sys.stderr)
        code = 1
    raise SystemExit(code or 0)
