//! Offline walkthrough through the real broker and Unix-socket protocol.

use std::fs;
use std::os::unix::fs::DirBuilderExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};

use crate::broker::{Broker, SESSION_ENDED};
use crate::error::{BridgeError, Stop};
use crate::output::{ascii_json, say};
use crate::policy::{Policy, Provider};
use crate::providers::Upstream;
use crate::secret::Secret;
use crate::transport::{Server, private_directory, request};

/// Not a real key. The demo holds it the same way a real one would be held.
pub const DEMO_KEY: &str = "demonstration-value-not-a-real-key";
pub const DEMO_MODEL: &str = "demo-model";

/// Stands in for a provider. It never opens a network connection.
struct DemoUpstream;

impl Upstream for DemoUpstream {
    fn send(
        &self,
        _: Provider,
        _: &Secret,
        _: &str,
        _: &str,
        _: u32,
    ) -> Result<String, BridgeError> {
        Ok("Demo response. Your prompt was not sent to an API provider.".to_string())
    }
}

pub fn demo_broker() -> Broker {
    let policy = Policy::new(Provider::Moonshot, vec![DEMO_MODEL.to_string()])
        .expect("the built-in demo policy is valid");
    Broker::demo(
        policy,
        Secret::new(DEMO_KEY.to_string()),
        Box::new(DemoUpstream),
    )
}

/// A new private folder inside the current folder, deleted when dropped.
///
/// The path is relative, so the socket address stays short even when the
/// checkout's full path is long.
pub struct PrivateTempDir {
    path: PathBuf,
}

impl PrivateTempDir {
    pub fn create(prefix: &str) -> Result<PrivateTempDir, BridgeError> {
        static COUNTER: AtomicU32 = AtomicU32::new(0);
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |since| since.subsec_nanos());
        let count = COUNTER.fetch_add(1, Ordering::Relaxed);
        let path = PathBuf::from(format!(
            "{prefix}{}-{nanos:08x}-{count}",
            std::process::id()
        ));
        // `create` fails if the name exists, so an existing folder is never reused.
        fs::DirBuilder::new().mode(0o700).create(&path).map_err(|_| {
            BridgeError::new(
                "The guided demo needs a writable current folder. Run it from a writable checkout.",
            )
        })?;
        let created = PrivateTempDir { path };
        private_directory(Some(created.path()))?;
        Ok(created)
    }

    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for PrivateTempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

/// Sets a flag when dropped. Used so the demo's server thread is told to
/// stop however the walkthrough ends, even if it crashes.
struct SetOnDrop<'a>(&'a AtomicBool);

impl Drop for SetOnDrop<'_> {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Relaxed);
    }
}

/// Print a line, or stop the demo if nobody is reading the output any more.
fn show(line: impl std::fmt::Display) -> Result<(), Stop> {
    if say(line) {
        Ok(())
    } else {
        Err(Stop::Cancelled)
    }
}

/// Run the whole walkthrough in one process: start a fake-key session in a
/// new temporary folder, make the five requests over the real socket, clean up.
pub fn guided(stop: &AtomicBool) -> Result<(), Stop> {
    show("[Keybridge] DEMONSTRATION: fake credentials; no vault, provider or cloud access.")?;
    show("Attempts and output-token limits are not dollar caps. This demo costs nothing.")?;
    // Never reuse or clean an owner session: the folder is always new.
    let directory = PrivateTempDir::create(".kb-demo-")?;
    let shown =
        std::path::absolute(directory.path()).unwrap_or_else(|_| directory.path().to_path_buf());
    show(format!("Demo directory: {}", shown.display()))?;
    let socket = directory.path().join("session.sock");
    let server = Server::bind(&socket, Arc::new(demo_broker()))?;

    let finished = AtomicBool::new(false);
    let result = thread::scope(|scope| {
        scope.spawn(|| server.serve_until(&finished));
        let _stop_server = SetOnDrop(&finished);
        walkthrough(&socket, stop)
    });
    drop(server);
    drop(directory);
    result?;
    show("[Keybridge] Demo state removed. Run the same command to try again.")
}

/// The five steps. Each reply is printed, then checked.
pub fn walkthrough(socket: &Path, stop: &AtomicBool) -> Result<(), Stop> {
    let ask =
        |prompt: &str| json!({"operation": "generate", "model": DEMO_MODEL, "prompt": prompt});
    let steps = [
        (
            "1/5 Approved model, attempts remaining, expiry in seconds and output-token cap per call",
            json!({"operation": "status"}),
        ),
        (
            "2/5 Ask the approved model using demonstration data",
            ask("Explain this demonstration."),
        ),
        (
            "3/5 History contains request metadata only",
            json!({"operation": "history"}),
        ),
        ("4/5 Lock this demo session", json!({"operation": "lock"})),
        (
            "5/5 Try another request after lock (expected denial)",
            ask("Try again after lock."),
        ),
    ];
    let last = steps.len() - 1;
    for (index, (label, message)) in steps.iter().enumerate() {
        if stop.load(Ordering::Relaxed) {
            return Err(Stop::Cancelled);
        }
        show(format!("[Keybridge] {label}"))?;
        let reply = request(socket, message)?;
        show(ascii_json(&reply))?;
        if !step_passed(message, &reply, index == last) {
            return Err(Stop::Error(BridgeError::new(
                "The demonstration did not complete as expected. Stop and report this step.",
            )));
        }
    }
    show(
        "[Keybridge] Post-lock request denied. A live session requires the owner's private unlock.",
    )
}

/// A generic failure does not prove revocation. The lock reply must confirm
/// `locked`, and the final denial must carry the broker's own reason.
fn step_passed(message: &Value, reply: &Value, expect_denial: bool) -> bool {
    if expect_denial {
        return reply["ok"] == false && reply["error"] == SESSION_ENDED;
    }
    let lock_confirmed = message["operation"] != "lock" || reply["state"] == "locked";
    reply["ok"] == true && lock_confirmed
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn denial_needs_the_brokers_own_reason() {
        let ask = json!({"operation": "generate"});
        assert!(step_passed(
            &ask,
            &json!({"ok": false, "error": SESSION_ENDED}),
            true
        ));
        assert!(!step_passed(
            &ask,
            &json!({"ok": false, "error": "Something unrelated."}),
            true
        ));
        assert!(!step_passed(&ask, &json!({"ok": false}), true));
        assert!(!step_passed(
            &ask,
            &json!({"ok": true, "text": "answered"}),
            true
        ));
    }

    #[test]
    fn lock_needs_a_confirmed_state() {
        let lock = json!({"operation": "lock"});
        assert!(step_passed(
            &lock,
            &json!({"ok": true, "state": "locked"}),
            false
        ));
        assert!(!step_passed(
            &lock,
            &json!({"ok": true, "state": "ready"}),
            false
        ));
        assert!(!step_passed(&lock, &json!({"ok": true}), false));
        assert!(!step_passed(
            &lock,
            &json!({"ok": false, "state": "locked"}),
            false
        ));
    }
}
