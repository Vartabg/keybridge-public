# Rust port of the session daemon

The crate in [`rust/`](../rust/) is a Rust version of the Keybridge session
daemon and its client commands. The Python package is unchanged and remains
the only place for Bitwarden and TypeSafe/Vercel work.

It is a developer prototype with the same [security boundary](../SECURITY.md).
It reduces accidental exposure of a key. It does not isolate the key from
hostile software running as the same OS user.

## Layout

One crate in `rust/`, not a workspace at the repository root. A workspace
groups several crates; there is one. The Python package keeps the root.

| File | What it holds |
| --- | --- |
| `src/secret.rs` | `Secret`: the key, wiped when dropped, never printable |
| `src/policy.rs` | Providers, model-ID check, session limits |
| `src/broker.rs` | Session rules, attempts, expiry, lock, history, redaction |
| `src/providers.rs` | Fixed provider URLs, request bodies, HTTP client, text extraction |
| `src/transport.rs` | Unix-socket server and client, private folder checks |
| `src/demo.rs` | Fake-key session and the guided walkthrough |
| `src/output.rs` | Printing that cannot crash; ASCII-only JSON |
| `src/cli.rs` | Commands and argument parsing |
| `tests/` | Rules, socket, and whole-program tests |

## Same as Python

- Protocol: one JSON object per line over `<directory>/session.sock`.
  Operations `status`, `generate`, `history`, `lock`, with the same fields,
  replies and error messages. A Python client can use a Rust daemon and the
  reverse; this was checked by hand with the fake-key demo on both sides.
- Limits: 10 attempts, one hour, 1024 output tokens per call. Prompt of 1 to
  16000 characters. Request line up to 65536 bytes.
- Attempts are reserved before any network access and are not refunded.
- Lock, expiry and a used-up allowance deny later requests. A response that
  arrives after lock or expiry is withheld.
- History holds attempt number, provider, model, time and outcome only.
- The socket has permission 600 in a folder with permission 700. An existing
  socket is never replaced. Cleanup removes only the socket this process made.
- Provider requests: fixed URLs, no redirects, no proxy from the environment,
  45 second timeout, 512 KiB response cap, fixed error messages.

## Deliberate differences

| Topic | Python | Rust |
| --- | --- | --- |
| How a live key enters | Bitwarden unlock, then a saved vault reference | `start --provider P --model M`: typed at a hidden prompt in a real terminal. Nothing is saved. |
| Session settings | `connection.json` chosen in onboarding | Flags on `start`; limits are the fixed defaults |
| Key in memory | Reference cleared; not zeroized | Keybridge's copy is overwritten with zeros when dropped |
| Environment | Clears inherited variables at startup | Does not. It never reads a key from the environment and starts no child process. Proxy variables are ignored by the HTTP client. |
| Plain HTTP | Not possible with the fixed URLs | Also refused by the HTTP client setting |
| Crash output | Fixed message | Fixed message; the default panic report is replaced |
| JSON output | ASCII with `\u` escapes, spaces after separators | ASCII with `\u` escapes, no spaces |
| Request on the socket | ASCII with `\u` escapes | UTF-8, so a long non-English prompt fits in one request line |
| Bad command line | argparse usage, exit code 2 | JSON error on stderr, exit code 1 |
| Option forms | `--model ID` and `--model=ID` | `--model ID` only |
| Rejected key message | Points to Bitwarden | "Check the key for this connection." |
| Closed output | Not handled specially | A closed terminal or pipe never crashes the daemon; the guided demo stops and cleans up |
| Provider connections | New connection per request | Same; connection reuse is turned off |
| Signals | Control-C and SIGTERM stop a session | Control-C, SIGTERM and SIGHUP stop a session. At the key prompt, Control-C cancels at once; SIGTERM takes effect when Return is pressed. |
| Whitespace-only prompt | Python's `str.strip()` | Rust's `str::trim()`; they differ only on a few rare control characters |
| Gemini "thought" parts | Skipped when `thought` is truthy | Skipped when `thought` is anything but `false` or `null` |

## What zeroizing does and does not cover

`Secret` wraps the key in `zeroize::Zeroizing`. When the last holder lets go
(lock, expiry, used-up allowance, or the daemon stopping), that memory is
overwritten with zeros before it is freed. The `Authorization` text built for
a request is wiped the same way.

Not covered:

- Copies inside the HTTP and TLS libraries (header buffers, encrypted records).
- The terminal and the hidden-prompt library's handling of typed input. That
  library grows its buffer as you type and wipes only the final one.
- Provider text that echoes the key, before redaction replaces it.
- Kernel socket buffers, swap, core dumps and hibernation images.
- A request already in flight: it keeps the key until the provider call returns.

So this narrows how long Keybridge's own copy stays readable. It is not a
claim of forensic memory erasure.

## Dependencies

| Crate | Used for |
| --- | --- |
| `zeroize` | Wiping the key |
| `serde_json` | JSON on the socket and for providers |
| `ureq` (rustls) | HTTPS requests to the provider |
| `rpassword` | Hidden key prompt |
| `signal-hook` | Clean stop on Control-C and termination |
| `rustix` | Reading the current user ID without `unsafe` |
| `base64`, `percent-encoding` | Redacting encoded forms of the key |

The crate itself contains no `unsafe` code; the compiler enforces that with
`#![forbid(unsafe_code)]`. The dependencies have not been audited.

## Not established

- A live provider call from the Rust client. The HTTP path is tested against a
  local test server only.
- Real use of the hidden prompt beyond one manual run with a made-up value.
- Linux behavior outside CI. Development was on macOS.
- Expiry while the computer sleeps. The one-hour limit counts awake time on
  macOS, in both versions.
- Anything about hostile same-user software. That is outside the boundary.
