//! The session rules, tested without any network or socket.
//! Every key here is a made-up value.

use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use keybridge::broker::{Broker, Clock, SESSION_ENDED};
use keybridge::error::BridgeError;
use keybridge::policy::{Policy, Provider};
use keybridge::providers::Upstream;
use keybridge::secret::Secret;
use serde_json::{Value, json};

const FAKE_KEY: &str = "example-credential-for-tests-only";

/// What the fake provider saw for one call.
#[derive(Debug, Clone, PartialEq)]
struct Call {
    model: String,
    prompt: String,
    max_output_tokens: u32,
}

type Answer = Box<dyn Fn(&Secret) -> Result<String, BridgeError> + Send + Sync>;

/// A fake provider: records each call and answers with a closure.
struct FakeUpstream {
    calls: Arc<Mutex<Vec<Call>>>,
    answer: Answer,
}

impl Upstream for FakeUpstream {
    fn send(
        &self,
        _: Provider,
        key: &Secret,
        model: &str,
        prompt: &str,
        max_output_tokens: u32,
    ) -> Result<String, BridgeError> {
        let call = Call {
            model: model.to_string(),
            prompt: prompt.to_string(),
            max_output_tokens,
        };
        self.calls.lock().unwrap().push(call);
        (self.answer)(key)
    }
}

/// A broker allowing 2 attempts for 60 seconds, plus handles to inspect it.
struct Fixture {
    broker: Broker,
    calls: Arc<Mutex<Vec<Call>>>,
    elapsed: Arc<Mutex<Duration>>,
}

impl Fixture {
    /// The default fake provider echoes the key, to prove it gets redacted.
    fn new() -> Fixture {
        Fixture::answering(|key| Ok(format!("Response: {}", key.expose())))
    }

    fn answering(
        answer: impl Fn(&Secret) -> Result<String, BridgeError> + Send + Sync + 'static,
    ) -> Fixture {
        let calls = Arc::new(Mutex::new(Vec::new()));
        let elapsed = Arc::new(Mutex::new(Duration::ZERO));
        let start = Instant::now();
        let shared = Arc::clone(&elapsed);
        let clock: Clock = Box::new(move || start + *shared.lock().unwrap());
        let policy = Policy::with_limits(
            Provider::Moonshot,
            vec!["test-model".to_string()],
            2,
            60,
            256,
        )
        .unwrap();
        let upstream = FakeUpstream {
            calls: Arc::clone(&calls),
            answer: Box::new(answer),
        };
        let broker = Broker::with_clock(
            policy,
            Secret::new(FAKE_KEY.to_string()),
            Box::new(upstream),
            false,
            clock,
        );
        Fixture {
            broker,
            calls,
            elapsed,
        }
    }

    fn advance(&self, seconds: u64) {
        *self.elapsed.lock().unwrap() += Duration::from_secs(seconds);
    }

    fn ask(&self) -> Value {
        self.broker.handle(&ask("Hello"))
    }

    fn status(&self) -> Value {
        self.broker.handle(&json!({"operation": "status"}))
    }

    fn history(&self) -> Value {
        self.broker.handle(&json!({"operation": "history"}))
    }

    fn call_count(&self) -> usize {
        self.calls.lock().unwrap().len()
    }
}

fn ask(prompt: &str) -> Value {
    json!({"operation": "generate", "model": "test-model", "prompt": prompt})
}

/// A fake provider that waits until the test lets it finish.
fn slow_fixture(text: &'static str) -> (Fixture, Arc<AtomicBool>) {
    let release = Arc::new(AtomicBool::new(false));
    let wait = Arc::clone(&release);
    let fixture = Fixture::answering(move |_| {
        let give_up = Instant::now() + Duration::from_secs(5);
        while !wait.load(Ordering::Relaxed) && Instant::now() < give_up {
            thread::sleep(Duration::from_millis(1));
        }
        Ok(text.to_string())
    });
    (fixture, release)
}

fn wait_until(condition: impl Fn() -> bool) {
    let give_up = Instant::now() + Duration::from_secs(5);
    while !condition() {
        assert!(Instant::now() < give_up, "timed out waiting");
        thread::sleep(Duration::from_millis(1));
    }
}

// --- The key never appears in replies, errors or history ---

#[test]
fn an_echoed_key_is_redacted_and_the_attempt_is_counted() {
    let fixture = Fixture::new();
    let reply = fixture.ask();
    assert_eq!(reply["ok"], true);
    assert_eq!(reply["text"], "Response: [redacted]");
    assert!(!reply.to_string().contains(FAKE_KEY));
    assert_eq!(fixture.calls.lock().unwrap()[0].max_output_tokens, 256);
    assert_eq!(fixture.status()["remaining_calls"], 1);
}

#[test]
fn errors_do_not_echo_the_key_or_refund_attempts() {
    let fixture =
        Fixture::answering(|key| Err(BridgeError::new(format!("failed with {}", key.expose()))));
    for _ in 0..2 {
        let reply = fixture.ask();
        assert_eq!(reply["ok"], false);
        assert!(!reply.to_string().contains(FAKE_KEY));
    }
    assert_eq!(fixture.ask()["ok"], false);
    assert_eq!(
        fixture.call_count(),
        2,
        "a failed attempt must not be refunded"
    );
    assert!(!fixture.history().to_string().contains(FAKE_KEY));
}

#[test]
fn a_crashing_provider_call_gives_a_fixed_error() {
    let fixture = Fixture::answering(|key| panic!("crash mentioning {}", key.expose()));
    let reply = fixture.ask();
    assert_eq!(
        reply["error"],
        "The request failed. Check connection status before trying again."
    );
    assert_eq!(fixture.history()["events"][0]["state"], "failed");
    assert_eq!(fixture.status()["remaining_calls"], 1);
}

#[test]
fn history_is_metadata_only() {
    let fixture = Fixture::new();
    fixture.broker.handle(&ask("private prompt"));
    let history = fixture.history();
    let record = history.to_string();
    assert!(!record.contains("private prompt"));
    assert!(!record.contains(FAKE_KEY));
    assert!(!record.contains("Response:"));
    let event = history["events"][0].as_object().unwrap();
    let fields: Vec<&str> = event.keys().map(String::as_str).collect();
    assert_eq!(fields, ["attempt", "provider", "model", "time", "state"]);
    assert_eq!(event["state"], "completed");
}

#[test]
fn status_never_contains_the_key() {
    let fixture = Fixture::new();
    fixture.ask();
    assert!(!fixture.status().to_string().contains(FAKE_KEY));
}

// --- Denial after lock ---

#[test]
fn lock_blocks_later_requests_and_drops_the_key() {
    let fixture = Fixture::new();
    assert!(fixture.broker.holds_key());
    let locked = fixture.broker.handle(&json!({"operation": "lock"}));
    assert_eq!(locked, json!({"ok": true, "state": "locked"}));
    assert!(!fixture.broker.holds_key());
    let reply = fixture.ask();
    assert_eq!(reply, json!({"ok": false, "error": SESSION_ENDED}));
    assert_eq!(
        fixture.call_count(),
        0,
        "a locked session must not contact the provider"
    );
    assert_eq!(fixture.status()["state"], "locked");
}

#[test]
fn lock_withholds_an_in_flight_response() {
    let (fixture, release) = slow_fixture("late answer");
    thread::scope(|scope| {
        let call = scope.spawn(|| fixture.ask());
        wait_until(|| fixture.call_count() == 1);
        fixture.broker.lock();
        release.store(true, Ordering::Relaxed);
        let reply = call.join().unwrap();
        assert_eq!(reply["ok"], false);
        assert!(!reply.to_string().contains("late answer"));
    });
    assert_eq!(fixture.history()["events"][0]["state"], "failed");
}

#[test]
fn a_crash_while_changing_the_session_ends_it() {
    // A clock that can be made to crash, which happens while the session is locked for an update.
    let crash = Arc::new(AtomicBool::new(false));
    let flag = Arc::clone(&crash);
    let start = Instant::now();
    let clock: Clock = Box::new(move || {
        assert!(
            !flag.load(Ordering::Relaxed),
            "the test clock was told to crash"
        );
        start
    });
    let policy = Policy::new(Provider::Moonshot, vec!["test-model".to_string()]).unwrap();
    let upstream = FakeUpstream {
        calls: Arc::default(),
        answer: Box::new(|_| Ok("answer".to_string())),
    };
    let broker = Broker::with_clock(
        policy,
        Secret::new(FAKE_KEY.to_string()),
        Box::new(upstream),
        false,
        clock,
    );

    crash.store(true, Ordering::Relaxed);
    let status = json!({"operation": "status"});
    assert!(catch_unwind(AssertUnwindSafe(|| broker.handle(&status))).is_err());
    crash.store(false, Ordering::Relaxed);

    assert!(!broker.holds_key());
    assert_eq!(broker.handle(&status)["state"], "locked");
    assert_eq!(
        broker.handle(&ask("Hello")),
        json!({"ok": false, "error": SESSION_ENDED})
    );
}

// --- Attempt count and expiry ---

#[test]
fn expiry_is_enforced_and_the_key_is_dropped() {
    let fixture = Fixture::new();
    fixture.advance(59);
    assert_eq!(fixture.status()["state"], "ready");
    fixture.advance(2);
    assert_eq!(fixture.ask(), json!({"ok": false, "error": SESSION_ENDED}));
    assert_eq!(fixture.status()["state"], "expired");
    assert_eq!(fixture.status()["expires_in_seconds"], 0);
    assert!(!fixture.broker.holds_key());
    assert_eq!(fixture.call_count(), 0);
}

#[test]
fn expiry_withholds_an_in_flight_response() {
    let (fixture, release) = slow_fixture("late answer");
    thread::scope(|scope| {
        let call = scope.spawn(|| fixture.ask());
        wait_until(|| fixture.call_count() == 1);
        fixture.advance(61);
        release.store(true, Ordering::Relaxed);
        let reply = call.join().unwrap();
        assert_eq!(reply["ok"], false);
        assert!(reply["error"].as_str().unwrap().contains("withheld"));
    });
    assert_eq!(fixture.status()["state"], "expired");
    assert!(!fixture.broker.holds_key());
}

#[test]
fn expiry_applies_without_any_client_request() {
    let fixture = Fixture::new();
    fixture.advance(61);
    assert!(fixture.broker.holds_key());
    fixture.broker.tick();
    assert!(!fixture.broker.holds_key());
}

#[test]
fn attempts_stop_at_the_limit() {
    let fixture = Fixture::new();
    assert_eq!(fixture.ask()["ok"], true);
    assert_eq!(fixture.ask()["ok"], true);
    assert_eq!(fixture.ask()["ok"], false);
    assert_eq!(fixture.call_count(), 2);
    assert_eq!(fixture.status()["state"], "exhausted");
    assert_eq!(fixture.status()["remaining_calls"], 0);
    assert!(!fixture.broker.holds_key());
}

#[test]
fn attempt_limit_holds_under_concurrent_requests() {
    let fixture = Fixture::new();
    let succeeded = thread::scope(|scope| {
        let workers: Vec<_> = (0..20).map(|_| scope.spawn(|| fixture.ask())).collect();
        let replies = workers.into_iter().map(|worker| worker.join().unwrap());
        replies.filter(|reply| reply["ok"] == true).count()
    });
    assert_eq!(succeeded, 2);
    assert_eq!(fixture.call_count(), 2);
}

#[test]
fn an_excess_request_does_not_cancel_reserved_calls() {
    let (fixture, release) = slow_fixture("approved answer");
    thread::scope(|scope| {
        let first = scope.spawn(|| fixture.ask());
        let second = scope.spawn(|| fixture.ask());
        wait_until(|| fixture.call_count() == 2);
        let third = fixture.ask();
        assert_eq!(third["ok"], false);
        assert!(third["error"].as_str().unwrap().contains("call allowance"));
        release.store(true, Ordering::Relaxed);
        assert_eq!(first.join().unwrap()["ok"], true);
        assert_eq!(second.join().unwrap()["ok"], true);
    });
    assert!(!fixture.broker.holds_key());
    assert_eq!(fixture.status()["state"], "exhausted");
}

// --- Callers cannot widen what the owner approved ---

#[test]
fn callers_cannot_supply_headers_urls_or_limits() {
    let fixture = Fixture::new();
    for (name, value) in [
        ("url", json!("https://example.org")),
        ("headers", json!({})),
        ("max_tokens", json!(99999)),
        ("provider", json!("other")),
    ] {
        let mut request = ask("Hello");
        request[name] = value;
        assert_eq!(fixture.broker.handle(&request)["ok"], false, "{name}");
    }
    assert_eq!(fixture.call_count(), 0);
}

#[test]
fn unknown_models_operations_and_shapes_are_refused() {
    let fixture = Fixture::new();
    let requests = [
        json!({"operation": "generate", "model": "other", "prompt": "Hello"}),
        json!({"operation": "generate", "model": "test-model", "prompt": []}),
        ask(&"x".repeat(16001)),
        json!({"operation": "get_secret"}),
        json!([]),
        json!(null),
        json!({"operation": []}),
        json!({"operation": "lock", "extra": true}),
    ];
    for request in requests {
        assert_eq!(fixture.broker.handle(&request)["ok"], false, "{request}");
    }
    assert_eq!(fixture.call_count(), 0);
    assert_eq!(
        fixture.status()["state"],
        "ready",
        "a refused lock must not lock the session"
    );
}

#[test]
fn invalid_prompts_never_reach_the_provider_or_use_an_attempt() {
    let fixture = Fixture::new();
    let spaces = " ".repeat(16000);
    for prompt in [
        "",
        spaces.as_str(),
        "\t\n",
        &format!("{spaces}x"),
        &format!("x{spaces}"),
    ] {
        let reply = fixture.broker.handle(&ask(prompt));
        assert_eq!(reply["ok"], false);
        assert!(
            reply["error"]
                .as_str()
                .unwrap()
                .contains("between 1 and 16000 characters")
        );
    }
    assert_eq!(fixture.call_count(), 0);
    assert_eq!(fixture.status()["remaining_calls"], 2);
    assert_eq!(fixture.history()["events"], json!([]));
}

#[test]
fn a_prompt_at_the_limit_keeps_its_whitespace() {
    let fixture = Fixture::new();
    let prompt = format!(" {} ", "x".repeat(15998));
    assert_eq!(fixture.broker.handle(&ask(&prompt))["ok"], true);
    assert_eq!(fixture.calls.lock().unwrap()[0].prompt, prompt);
}

#[test]
fn status_shows_the_configured_limits() {
    let fixture = Fixture::new();
    let status = fixture.status();
    assert_eq!(status["demo"], false);
    assert_eq!(status["provider"], "moonshot");
    assert_eq!(status["models"], json!(["test-model"]));
    assert_eq!(status["remaining_calls"], 2);
    assert_eq!(status["max_output_tokens"], 256);
    assert_eq!(status["expires_in_seconds"], 60);
}

#[test]
fn demo_replies_are_labelled() {
    let broker = keybridge::demo::demo_broker();
    let reply =
        broker.handle(&json!({"operation": "generate", "model": "demo-model", "prompt": "Hello"}));
    assert_eq!(reply["demo"], true);
    assert_eq!(broker.handle(&json!({"operation": "status"}))["demo"], true);
    assert!(!reply.to_string().contains(keybridge::demo::DEMO_KEY));
}
