//! The owner starts a private session; agents use status, ask, history and lock.

use std::io::{IsTerminal, Read};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use serde_json::json;
use signal_hook::consts::{SIGHUP, SIGINT, SIGTERM};

use crate::broker::{Broker, MAX_PROMPT_CHARS};
use crate::demo;
use crate::error::{BridgeError, Stop};
use crate::output::{ascii_json, say, warn};
use crate::policy::{Policy, Provider};
use crate::providers::HttpUpstream;
use crate::secret::Secret;
use crate::transport::{Server, private_directory, request, validate_socket_path};

pub const USAGE: &str = "\
Use approved APIs without copying keys into agent conversations.

Usage: keybridge [--directory DIR] <command>

Commands:
  demo --guided    Run an isolated fake-key walkthrough and clean up
  demo             Start a fake-key session; makes no provider calls
  start --provider <moonshot|openrouter|gemini> --model ID [--model ID]...
                   Enter one API key at a hidden prompt and start a session
  status           Show connection and session limits
  ask --model ID   Read a prompt from stdin and return JSON
  history          Show this session's request metadata
  lock             Revoke this session; cannot undo already-sent requests

Options:
  --directory DIR  Private session folder (default: ~/.local/share/keybridge)";

#[derive(Debug, PartialEq, Eq)]
pub enum Command {
    Help,
    Demo {
        guided: bool,
    },
    Start {
        provider: Provider,
        models: Vec<String>,
    },
    Status,
    History,
    Lock,
    Ask {
        model: String,
    },
}

#[derive(Debug, PartialEq, Eq)]
pub struct Invocation {
    pub directory: Option<PathBuf>,
    pub command: Command,
}

/// Turn the words typed after `keybridge` into a `Command`.
pub fn parse(arguments: &[String]) -> Result<Invocation, BridgeError> {
    let usage = || BridgeError::new("Unknown or incomplete command. Run: keybridge help");
    let mut words = arguments.iter().map(String::as_str);
    let mut directory = None;
    let mut name = words.next().ok_or_else(usage)?;
    if name == "--directory" {
        directory = Some(PathBuf::from(words.next().ok_or_else(usage)?));
        name = words.next().ok_or_else(usage)?;
    }

    let mut guided = false;
    let mut provider = None;
    let mut models = Vec::new();
    while let Some(option) = words.next() {
        match (name, option) {
            ("demo", "--guided") => guided = true,
            ("start", "--provider") => {
                provider = Some(words.next().and_then(Provider::from_name).ok_or_else(|| {
                    BridgeError::new(
                        "Choose a supported API service: moonshot, openrouter or gemini.",
                    )
                })?);
            }
            ("start" | "ask", "--model") => {
                models.push(words.next().ok_or_else(usage)?.to_string())
            }
            _ => return Err(usage()),
        }
    }

    let command = match name {
        "help" | "--help" | "-h" => Command::Help,
        "demo" => Command::Demo { guided },
        "start" => Command::Start {
            provider: provider.ok_or_else(usage)?,
            models,
        },
        "status" => Command::Status,
        "history" => Command::History,
        "lock" => Command::Lock,
        "ask" if models.len() == 1 => Command::Ask {
            model: models.remove(0),
        },
        _ => return Err(usage()),
    };
    Ok(Invocation { directory, command })
}

/// Run one command and return the process exit code: 0 for success, 1 otherwise.
pub fn run(arguments: &[String]) -> i32 {
    match parse(arguments).map_err(Stop::Error).and_then(execute) {
        Ok(code) => code,
        Err(Stop::Error(error)) => {
            warn(ascii_json(&json!({"ok": false, "error": error.message()})));
            1
        }
        Err(Stop::Cancelled) => {
            warn("[Keybridge] Cancelled.");
            1
        }
    }
}

fn execute(invocation: Invocation) -> Result<i32, Stop> {
    let Invocation { directory, command } = invocation;
    match command {
        Command::Help => {
            say(USAGE);
            Ok(0)
        }
        Command::Demo { guided: true } => {
            if directory.is_some() {
                return Err(Stop::Error(BridgeError::new(
                    "Run demo --guided without --directory; it creates its own isolated state.",
                )));
            }
            let stop = stop_flag()?;
            demo::guided(&stop)?;
            Ok(0)
        }
        Command::Demo { guided: false } => {
            let directory = private_directory(directory.as_deref())?;
            let stop = stop_flag()?;
            say("[Keybridge] DEMO: fake credentials, no vault access or paid calls.");
            serve(&directory, demo::demo_broker(), &stop)
        }
        Command::Start { provider, models } => {
            let directory = private_directory(directory.as_deref())?;
            // Listen for Control-C before the key prompt, so cancelling there
            // leaves the terminal as it was found.
            let stop = stop_flag()?;
            let broker = live_broker(&directory, provider, models, &stop)?;
            serve(&directory, broker, &stop)
        }
        Command::Status => send(directory, json!({"operation": "status"})),
        Command::History => send(directory, json!({"operation": "history"})),
        Command::Lock => send(directory, json!({"operation": "lock"})),
        Command::Ask { model } => {
            let prompt = read_prompt()?;
            send(
                directory,
                json!({"operation": "generate", "model": model, "prompt": prompt}),
            )
        }
    }
}

/// Client commands: send one request to the running session, print its reply.
fn send(directory: Option<PathBuf>, message: serde_json::Value) -> Result<i32, Stop> {
    let directory = private_directory(directory.as_deref())?;
    let reply = request(&directory.join("session.sock"), &message)?;
    say(ascii_json(&reply));
    Ok(if reply["ok"] == true { 0 } else { 1 })
}

fn read_prompt() -> Result<String, BridgeError> {
    const MAX_CHARS: usize = MAX_PROMPT_CHARS;
    let too_large = || BridgeError::new("The prompt is too large. Limit it to 16000 characters.");
    // A character is at most 4 bytes, so more bytes than this is always too many characters.
    let byte_limit = MAX_CHARS as u64 * 4;
    let mut raw = Vec::new();
    std::io::stdin()
        .take(byte_limit + 1)
        .read_to_end(&mut raw)
        .map_err(|_| BridgeError::new("The prompt could not be read from standard input."))?;
    if raw.len() as u64 > byte_limit {
        return Err(too_large());
    }
    let prompt =
        String::from_utf8(raw).map_err(|_| BridgeError::new("Send the prompt as UTF-8 text."))?;
    if prompt.chars().count() > MAX_CHARS {
        return Err(too_large());
    }
    Ok(prompt)
}

/// Ask the owner for one key at a hidden prompt and build a live session.
fn live_broker(
    directory: &Path,
    provider: Provider,
    models: Vec<String>,
    stop: &AtomicBool,
) -> Result<Broker, Stop> {
    if !std::io::stdin().is_terminal() || !std::io::stdout().is_terminal() {
        return Err(Stop::Error(BridgeError::new(
            "Start must run in your own private terminal, outside the agent's captured terminal.",
        )));
    }
    let socket = directory.join("session.sock");
    validate_socket_path(&socket)?;
    if std::fs::symlink_metadata(&socket).is_ok() {
        return Err(Stop::Error(BridgeError::new(
            "Stop the existing session before starting another.",
        )));
    }
    // Check the permissions before asking for the key.
    let policy = Policy::new(provider, models)?;
    say(format!(
        "Service: {}. Allowed model IDs: {}.",
        provider.name(),
        policy.models().join(", ")
    ));
    say(format!(
        "Allow up to {} attempts per session, {} minutes, and {} output tokens per call.",
        policy.max_calls(),
        policy.duration_seconds() / 60,
        policy.max_output_tokens()
    ));
    say("Text will go to this provider. Its usual charges and data policies apply.");
    say("These are request limits, not a dollar spending cap. All local agents share them.");
    say("Typing is hidden: no characters or dots will appear. Paste the key, then press Return.");
    let typed = rpassword::prompt_password("API key (hidden input): ");
    // Control-C at the prompt sets the flag; the prompt then restores the terminal.
    if stop.load(Ordering::Relaxed) {
        return Err(Stop::Cancelled);
    }
    let typed = typed
        .map_err(|_| BridgeError::new("The key could not be read. No session was started."))?;
    let key = Secret::from_owner_input(typed)?;
    say("[Keybridge] The key is held in memory only. It is not saved or shown.");
    Ok(Broker::new(policy, key, Box::new(HttpUpstream::new())))
}

/// Keep a session open until Control-C, then lock it and remove its socket.
fn serve(directory: &Path, broker: Broker, stop: &AtomicBool) -> Result<i32, Stop> {
    let server = Server::bind(&directory.join("session.sock"), Arc::new(broker))?;
    say("[Keybridge] Session started. Agents can use status, ask, history, and lock.");
    say("[Keybridge] Leave this terminal open. Control-C stops the service.");
    server.serve_until(stop);
    drop(server);
    // The terminal may already be closed; `say` ignores that.
    say("\n[Keybridge] Session stopped.");
    Ok(0)
}

/// A flag that turns true on Control-C, termination or terminal closure, so
/// the program can clean up instead of being killed mid-step.
fn stop_flag() -> Result<Arc<AtomicBool>, BridgeError> {
    let stop = Arc::new(AtomicBool::new(false));
    for signal in [SIGINT, SIGTERM, SIGHUP] {
        signal_hook::flag::register(signal, Arc::clone(&stop))
            .map_err(|_| BridgeError::new("Keybridge could not prepare a clean shutdown."))?;
    }
    Ok(stop)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn words(line: &str) -> Vec<String> {
        line.split_whitespace().map(str::to_string).collect()
    }

    #[test]
    fn commands_parse() {
        let parsed = parse(&words("--directory ./demo-session ask --model demo-model")).unwrap();
        assert_eq!(parsed.directory, Some(PathBuf::from("./demo-session")));
        assert_eq!(
            parsed.command,
            Command::Ask {
                model: "demo-model".to_string()
            }
        );
        assert_eq!(
            parse(&words("demo --guided")).unwrap().command,
            Command::Demo { guided: true }
        );
        assert_eq!(
            parse(&words("demo")).unwrap().command,
            Command::Demo { guided: false }
        );
        assert_eq!(parse(&words("lock")).unwrap().command, Command::Lock);
        assert_eq!(
            parse(&words("start --provider gemini --model a --model b"))
                .unwrap()
                .command,
            Command::Start {
                provider: Provider::Gemini,
                models: vec!["a".to_string(), "b".to_string()]
            }
        );
    }

    #[test]
    fn unknown_or_incomplete_commands_are_refused() {
        for line in [
            "",
            "get-secret",
            "ask",
            "ask --model",
            "ask --model a --model b",
            "status --guided",
            "start --model a",
            "start --provider other --model a",
            "--directory",
            "lock extra",
        ] {
            assert!(parse(&words(line)).is_err(), "{line:?}");
        }
    }
}
