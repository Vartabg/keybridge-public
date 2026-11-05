# Keybridge

**Connect once. Let your coding agents use an API without handing them its key.**

A standalone local prototype built from Garo's actual API setup history.
Supports text requests to Moonshot/Kimi, OpenRouter, or Gemini, one connection
per session. Python 3.11+ on macOS/Linux; no Python runtime dependencies.

An owner-only setup command also connects a Bitwarden TypeSafe/Jev credential
to one linked Vercel project. See [the private connection guide](docs/typesafe-vercel.md).

This is a developer prototype, not a released security product. It reduces
accidental exposure; it does not isolate secrets from hostile software running
as your OS user. Read [the security boundary](SECURITY.md) before using real keys.

## Try it without credentials

From this checkout, run the complete guided walkthrough (Python 3.11+;
no installation):

```sh
python3 -m keybridge demo --guided
```

On macOS, open `scripts/demo.command` in Terminal for the same walkthrough.
It labels all results as demonstration data, shows the approved model, remaining
attempts, expiry and configured output-token cap, makes one fake request, shows
metadata-only history, then locks and demonstrates denial of a later request.
Attempts and token limits are **not dollar caps**. No vault, provider or cloud
access occurs.

Each walkthrough creates a new private temporary folder in the current directory
and removes it on completion or Control-C. It leaves existing owner sessions
alone. Run it from a writable checkout; do not supply `--directory`. The short
relative socket address also works when the checkout's absolute path is long.

For a manual two-terminal demonstration, choose a new, short private folder
and use the same `--directory` value for every command. In the first terminal:

```sh
python3 -m keybridge --directory ./demo-session demo
```

In another terminal, from the same checkout:

```sh
python3 -m keybridge --directory ./demo-session status
printf 'Explain the purpose of this connection.' | python3 -m keybridge --directory ./demo-session ask --model demo-model
python3 -m keybridge --directory ./demo-session history
python3 -m keybridge --directory ./demo-session lock
```

Stop the manual demo with Control-C before starting another session. Keybridge
never replaces an existing socket. Socket addresses have a portable limit of
103 encoded bytes; if a path is too long, use a shorter `--directory` value in
both terminals, or use the guided demo. An abrupt process kill may leave a stale socket;
verify no session is running before removing only that socket and restarting.

Optional installation, from this checkout:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
```

Then an agent can use the absolute path to `.venv/bin/keybridge` from any project.
All commands above also work with that executable instead of `python3 -m keybridge`.

## Rust version

The session daemon is being rewritten in Rust in [`rust/`](rust/). It uses the
same socket protocol, so either client works with either daemon. It is at the
same prototype stage and has the same [security boundary](SECURITY.md).

Build and test it. This needs a Rust toolchain (`brew install rust`, or
[rustup](https://rustup.rs)):

```sh
cd rust
cargo test
```

Run the guided demo. It uses fake data and no credentials, and labels its
output as demonstration data:

```sh
cargo run -- demo --guided
```

The manual two-terminal demo above also works: replace `python3 -m keybridge`
with `cargo run --` from the `rust/` folder.

| Part | In Rust |
| --- | --- |
| Session rules: approved models, attempt count, expiry, output-token cap, lock | Ported |
| Metadata-only history | Ported |
| Unix-socket daemon; `status`, `ask`, `history`, `lock` | Ported |
| `demo` and `demo --guided` | Ported |
| Moonshot, OpenRouter and Gemini requests | Ported; not yet run against a live provider |
| `start` | Different: the key is typed at a hidden prompt |
| Bitwarden unlock and saved connection settings | Not ported |
| TypeSafe/Vercel setup and `deployment-status` | Not ported; Python only |

The Rust daemon overwrites its own copy of the key with zeros when the session
locks, expires, runs out of attempts or stops. That does not cover copies made
by the HTTP and TLS libraries, the terminal or the operating system. Details
and every deliberate difference from the Python version are in
[docs/rust-port.md](docs/rust-port.md). New to Rust? Start with
[rust/LEARN.md](rust/LEARN.md).

## Private Bitwarden setup

Do this only in **your own Terminal**, not an agent-captured terminal. Don't paste
keys, master passwords, session keys or login output into chat.

Install Bitwarden's [official CLI](https://bitwarden.com/help/cli/):

```sh
npm install -g @bitwarden/cli
```

Keybridge searches the system executable paths (`/bin`, `/usr/bin`),
`/opt/homebrew/bin`, `/usr/local/bin`, and `~/.npm-global/bin` for `bw`.
It uses these same paths for Bitwarden's child processes, including Node.js.
It does not inherit extra directories from your shell's `PATH`. If Bitwarden
is installed but cannot be found, run `command -v bw` in your own Terminal and
compare the location with this list before reinstalling. Other custom npm
prefixes and version-manager directories are not automatically discovered.

Create and log into a separate CLI profile. For EU or self-hosted vaults, first
configure the server using `bw config server` with the same profile variable.

```sh
mkdir -p "$HOME/.local/share/keybridge/bitwarden"
chmod 700 "$HOME/.local/share/keybridge" "$HOME/.local/share/keybridge/bitwarden"
BITWARDENCLI_APPDATA_DIR="$HOME/.local/share/keybridge/bitwarden" bw login --quiet
BITWARDENCLI_APPDATA_DIR="$HOME/.local/share/keybridge/bitwarden" bw lock
python3 -m keybridge start
```

The first start asks you to unlock Bitwarden, select the service and vault item,
choose the key field and allowed model IDs, and save the permissions. A key can
be a login password, a custom field, or a secure note containing only the key.
The key is never displayed. The saved connection contains references, not keys.

Later sessions need `keybridge start` and your private vault unlock. The agent
uses `status` and `ask`; it does not need a custom secret-entry program.

Default permission: **10 attempts, one hour, 1024 output tokens per call**.
Failed API attempts count. Those limits are not a dollar cap. Provider calls
can cost money and send prompt text to the selected provider. No real call is
made merely by connecting. Use `start --configure` to choose a different service
or set of permitted models. Restarting requires unlock; agents cannot extend
the running session via its protocol.

`lock` stops future requests and withholds late results. Already-transmitted
requests may finish and be billed. Stop the private terminal with Control-C
when finished. Session settings persist; API keys and request history do not.

## Agent contract

- Use `status` to learn which service/model is approved and whether it is demo.
- Send text on stdin to `ask --model <approved-model-id>`; parse its JSON result.
- Do not request or inspect the vault, settings beyond references, environment
  secrets, session state in memory, or key values.
- On failure, report the safe error. Do not retry automatically or build a new
  secret-entry workaround. Ask the owner to unlock only when status requires it.
- `history` gives metadata for the current session. `lock` revokes it.
- `deployment-status --site <linked-site> --target preview --target production`
  returns TypeSafe/Vercel connection metadata. Use the same targets chosen at
  setup. Presence does not prove that a deployment is using the variable.
- Only the owner runs `connect-typesafe` in a private terminal. Never launch it
  in an agent-captured PTY or capture its screen. It is not a broker operation.

The CLI is the first integration surface. No MCP server or agent installation
is configured by this prototype.

## Checks

```sh
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

Provider contracts were checked against official [Moonshot/Kimi](https://platform.kimi.ai/docs/overview),
[OpenRouter](https://openrouter.ai/docs/quickstart), and [Gemini](https://ai.google.dev/api/generate-content)
documentation on September 23, 2026. Model access and pricing still depend on
your account; old preview model names from chats are not defaults.
