//! Session policy and revocation. The key never enters a reply.

use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use base64::Engine;
use percent_encoding::{AsciiSet, NON_ALPHANUMERIC, utf8_percent_encode};
use serde_json::{Value, json};
use zeroize::Zeroizing;

use crate::error::BridgeError;
use crate::policy::Policy;
use crate::providers::Upstream;
use crate::secret::Secret;

/// The denial every request gets once a session is locked, expired or used up.
pub const SESSION_ENDED: &str =
    "This session is locked or expired. Unlock it in your private terminal.";

/// Longest prompt accepted, counted in characters.
pub const MAX_PROMPT_CHARS: usize = 16000;

/// A function that says what time it is. Tests pass a fake one so they can
/// move time forward without waiting.
pub type Clock = Box<dyn Fn() -> Instant + Send + Sync>;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum State {
    Ready,
    Locked,
    Expired,
    Exhausted,
}

impl State {
    fn name(self) -> &'static str {
        match self {
            State::Ready => "ready",
            State::Locked => "locked",
            State::Expired => "expired",
            State::Exhausted => "exhausted",
        }
    }
}

/// One line of history: metadata only. No prompt, no answer, no key.
struct Event {
    attempt: u32,
    model: String,
    time: u64,
    state: &'static str,
}

/// Everything that changes during a session. It sits behind one `Mutex`, so
/// two requests can never update it at the same moment.
struct Session {
    state: State,
    key: Option<Arc<Secret>>,
    deadline: Instant,
    attempts: u32,
    active: u32,
    events: Vec<Event>,
}

impl Session {
    /// Leave the ready state and let go of the key. The key's memory is wiped
    /// as soon as no in-flight request is still using it.
    fn end(&mut self, state: State) {
        self.state = state;
        self.key = None;
    }
}

pub struct Broker {
    policy: Policy,
    demo: bool,
    upstream: Box<dyn Upstream>,
    clock: Clock,
    session: Mutex<Session>,
}

impl Broker {
    /// A live session using the real clock.
    pub fn new(policy: Policy, key: Secret, upstream: Box<dyn Upstream>) -> Broker {
        Broker::with_clock(policy, key, upstream, false, Box::new(Instant::now))
    }

    /// A session whose replies are labelled `"demo": true`.
    pub fn demo(policy: Policy, key: Secret, upstream: Box<dyn Upstream>) -> Broker {
        Broker::with_clock(policy, key, upstream, true, Box::new(Instant::now))
    }

    pub fn with_clock(
        policy: Policy,
        key: Secret,
        upstream: Box<dyn Upstream>,
        demo: bool,
        clock: Clock,
    ) -> Broker {
        let deadline = clock() + Duration::from_secs(policy.duration_seconds());
        let session = Session {
            state: State::Ready,
            key: Some(Arc::new(key)),
            deadline,
            attempts: 0,
            active: 0,
            events: Vec::new(),
        };
        Broker {
            policy,
            demo,
            upstream,
            clock,
            session: Mutex::new(session),
        }
    }

    /// Answer one request. Every failure becomes `{"ok": false, "error": ...}`
    /// with a fixed message.
    pub fn handle(&self, request: &Value) -> Value {
        match self.try_handle(request) {
            Ok(reply) => reply,
            Err(error) => json!({"ok": false, "error": error.message()}),
        }
    }

    /// Stop future requests and drop the key.
    pub fn lock(&self) {
        self.session().end(State::Locked);
    }

    /// Apply expiry even when no client is asking. The server calls this
    /// while it waits for connections.
    pub fn tick(&self) {
        let mut session = self.session();
        self.refresh(&mut session);
    }

    /// Whether the broker still holds the key. Says nothing about its value.
    pub fn holds_key(&self) -> bool {
        self.session().key.is_some()
    }

    /// Take the session lock. If another thread crashed while holding it, the
    /// session is ended instead of trusting half-updated state.
    fn session(&self) -> MutexGuard<'_, Session> {
        match self.session.lock() {
            Ok(session) => session,
            Err(poisoned) => {
                let mut session = poisoned.into_inner();
                session.end(State::Locked);
                session
            }
        }
    }

    fn refresh(&self, session: &mut Session) {
        if session.state == State::Ready && (self.clock)() >= session.deadline {
            session.end(State::Expired);
        }
    }

    fn try_handle(&self, request: &Value) -> Result<Value, BridgeError> {
        let fields = request.as_object();
        let operation = fields
            .and_then(|fields| fields.get("operation"))
            .and_then(Value::as_str);
        let (Some(fields), Some(operation)) = (fields, operation) else {
            return Err(BridgeError::new("Send a supported Keybridge request."));
        };
        // Only these field names are accepted. There is no field for a URL,
        // a header, a provider or a limit.
        let allowed: &[&str] = if operation == "generate" {
            &["operation", "model", "prompt"]
        } else {
            &["operation"]
        };
        if fields.len() != allowed.len() || !allowed.iter().all(|name| fields.contains_key(*name)) {
            return Err(BridgeError::new(
                "This operation does not accept those fields.",
            ));
        }

        // Step 1, under the lock: check the rules and reserve one attempt.
        let (attempt, key, model, prompt) = {
            let mut session = self.session();
            self.refresh(&mut session);
            match operation {
                "lock" => {
                    session.end(State::Locked);
                    return Ok(json!({"ok": true, "state": "locked"}));
                }
                "status" => return Ok(self.status(&session)),
                "history" => return Ok(self.history(&session)),
                "generate" => {}
                _ => return Err(BridgeError::new("This operation is not available.")),
            }
            if session.state != State::Ready {
                return Err(BridgeError::new(SESSION_ENDED));
            }
            if session.attempts >= self.policy.max_calls() {
                return Err(BridgeError::new(
                    "This session has used its call allowance. Start a new approved session.",
                ));
            }
            let model = fields["model"]
                .as_str()
                .filter(|model| self.policy.models().iter().any(|m| m == model));
            let Some(model) = model else {
                return Err(BridgeError::new(
                    "That model was not approved for this session.",
                ));
            };
            let prompt = fields["prompt"].as_str().filter(|prompt| {
                prompt.chars().count() <= MAX_PROMPT_CHARS && !prompt.trim().is_empty()
            });
            let Some(prompt) = prompt else {
                return Err(BridgeError::new(
                    "Send a text prompt between 1 and 16000 characters.",
                ));
            };
            let Some(key) = session.key.clone() else {
                return Err(BridgeError::new(SESSION_ENDED));
            };
            session.attempts += 1; // Reserved before any network access; failures still count.
            session.active += 1;
            let attempt = session.attempts;
            let time = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_or(0, |since| since.as_secs());
            session.events.push(Event {
                attempt,
                model: model.to_string(),
                time,
                state: "pending",
            });
            (attempt, key, model, prompt)
        };

        // Step 2, lock released: talk to the service. This can take a while,
        // and status, history and lock keep working meanwhile.
        let provider = self.policy.provider();
        let max_tokens = self.policy.max_output_tokens();
        let sent = catch_unwind(AssertUnwindSafe(|| {
            self.upstream
                .send(provider, &key, model, prompt, max_tokens)
        }));
        let outcome = match sent {
            Ok(Ok(text)) => Ok(redact(&text, &key)),
            Ok(Err(error)) => Err(BridgeError::new(redact(error.message(), &key))),
            // The upstream code crashed. Say nothing about why.
            Err(_) => Err(BridgeError::new(
                "The request failed. Check connection status before trying again.",
            )),
        };
        drop(key); // This request no longer needs the key.

        // Step 3, under the lock again: record the outcome.
        let mut session = self.session();
        self.refresh(&mut session);
        let outcome = match outcome {
            Ok(_) if session.state != State::Ready => Err(BridgeError::new(
                "The session ended during this request. Its response was withheld.",
            )),
            other => other,
        };
        if let Some(event) = session.events.get_mut(attempt as usize - 1) {
            event.state = if outcome.is_ok() {
                "completed"
            } else {
                "failed"
            };
        }
        session.active -= 1;
        let used_up = session.active == 0 && session.attempts >= self.policy.max_calls();
        if used_up && session.state == State::Ready {
            session.end(State::Exhausted);
        }
        outcome.map(|text| json!({"ok": true, "demo": self.demo, "text": text}))
    }

    fn status(&self, session: &Session) -> Value {
        let remaining = self.policy.max_calls().saturating_sub(session.attempts);
        let expires = session
            .deadline
            .saturating_duration_since((self.clock)())
            .as_secs();
        json!({
            "ok": true,
            "state": session.state.name(),
            "demo": self.demo,
            "provider": self.policy.provider().name(),
            "models": self.policy.models(),
            "remaining_calls": remaining,
            "max_output_tokens": self.policy.max_output_tokens(),
            "expires_in_seconds": expires,
        })
    }

    fn history(&self, session: &Session) -> Value {
        let provider = self.policy.provider().name();
        let events: Vec<Value> = session
            .events
            .iter()
            .map(|event| {
                json!({
                    "attempt": event.attempt,
                    "provider": provider,
                    "model": event.model,
                    "time": event.time,
                    "state": event.state,
                })
            })
            .collect();
        json!({"ok": true, "events": events})
    }
}

/// Characters Python's `quote(key, safe="")` leaves alone.
const URL_ENCODED: &AsciiSet = &NON_ALPHANUMERIC
    .remove(b'_')
    .remove(b'.')
    .remove(b'-')
    .remove(b'~');

/// Replace the key, and its URL-encoded and base64 forms, with `[redacted]`.
/// Defense in depth: a service should never echo the key, but if it does,
/// the echo stops here.
fn redact(text: &str, key: &Secret) -> String {
    let plain = key.expose();
    if plain.is_empty() {
        return text.to_string();
    }
    let url_form = Zeroizing::new(utf8_percent_encode(plain, URL_ENCODED).to_string());
    let base64_form = Zeroizing::new(base64::engine::general_purpose::STANDARD.encode(plain));
    text.replace(plain, "[redacted]")
        .replace(url_form.as_str(), "[redacted]")
        .replace(base64_form.as_str(), "[redacted]")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redaction_covers_plain_url_encoded_and_base64_forms() {
        let key = Secret::new("fake/value+for=tests".to_string());
        let echoed =
            "a fake/value+for=tests b fake%2Fvalue%2Bfor%3Dtests c ZmFrZS92YWx1ZStmb3I9dGVzdHM= d";
        assert_eq!(
            redact(echoed, &key),
            "a [redacted] b [redacted] c [redacted] d"
        );
    }
}
