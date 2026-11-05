//! Runs the built `keybridge` program the way a person or an agent would.
//! "Logs" here means everything the program prints: stdout and stderr.

use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::Path;
use std::process::{Child, Command, ExitStatus, Output, Stdio};
use std::sync::atomic::AtomicBool;
use std::thread;
use std::time::{Duration, Instant};

use keybridge::demo::{DEMO_KEY, PrivateTempDir, guided};
use keybridge::error::Stop;
use serde_json::Value;

/// Cargo builds the program before these tests and puts its path here.
const KEYBRIDGE: &str = env!("CARGO_BIN_EXE_keybridge");

fn run(folder: &Path, arguments: &[&str], stdin: &str) -> Output {
    let mut child = Command::new(KEYBRIDGE)
        .args(arguments)
        .current_dir(folder)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    // The program may exit without reading its input; that is fine.
    let _ = child.stdin.take().unwrap().write_all(stdin.as_bytes());
    child.wait_with_output().unwrap()
}

fn text(bytes: &[u8]) -> String {
    String::from_utf8(bytes.to_vec()).unwrap()
}

fn json(bytes: &[u8]) -> Value {
    serde_json::from_slice(bytes).unwrap()
}

fn leftovers(folder: &Path, prefix: &str) -> Vec<String> {
    let names = fs::read_dir(folder)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().into_string().unwrap());
    names.filter(|name| name.starts_with(prefix)).collect()
}

/// A running `keybridge` program. If a test fails part-way, dropping this
/// stops the program so nothing is left running.
struct Running(Child);

impl Running {
    fn signal(&self, name: &str) {
        let status = Command::new("kill")
            .args([name, &self.0.id().to_string()])
            .status()
            .unwrap();
        assert!(status.success());
    }

    /// Wait for the program to exit by itself; fail the test if it hangs.
    fn finish(&mut self) -> (ExitStatus, String) {
        let give_up = Instant::now() + Duration::from_secs(10);
        let status = loop {
            if let Some(status) = self.0.try_wait().unwrap() {
                break status;
            }
            assert!(Instant::now() < give_up, "the program did not exit");
            thread::sleep(Duration::from_millis(10));
        };
        let mut stderr = String::new();
        if let Some(mut pipe) = self.0.stderr.take() {
            pipe.read_to_string(&mut stderr).unwrap();
        }
        (status, stderr)
    }
}

impl Drop for Running {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

/// A place to send output that nobody reads: what a closed terminal or a
/// finished `| head` looks like to the program.
fn closed_output() -> Stdio {
    let (reader, writer) = std::io::pipe().unwrap();
    drop(reader);
    Stdio::from(writer)
}

fn wait_for(path: &Path) {
    let give_up = Instant::now() + Duration::from_secs(10);
    while !path.exists() {
        assert!(
            Instant::now() < give_up,
            "{} never appeared",
            path.display()
        );
        thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn guided_demo_is_labelled_isolated_and_cleans_up() {
    let folder = PrivateTempDir::create(".kb-test-").unwrap();
    let output = run(folder.path(), &["demo", "--guided"], "");
    assert!(output.status.success());
    let stdout = text(&output.stdout);
    let logs = format!("{stdout}{}", text(&output.stderr));

    assert!(stdout.starts_with("[Keybridge] DEMONSTRATION: fake credentials"));
    assert!(stdout.contains("not dollar caps"));
    assert!(stdout.contains("Post-lock request denied"));
    assert!(stdout.contains("Demo state removed"));
    assert!(!logs.contains(DEMO_KEY), "the held key value was printed");
    assert!(!logs.contains("demonstration-value"));

    let shown = stdout
        .lines()
        .find_map(|line| line.strip_prefix("Demo directory: "))
        .unwrap();
    assert!(!Path::new(shown).exists());
    assert!(leftovers(folder.path(), ".kb-demo-").is_empty());

    let replies: Vec<Value> = stdout
        .lines()
        .filter(|line| line.starts_with('{'))
        .map(|line| json(line.as_bytes()))
        .collect();
    assert_eq!(replies.len(), 5);
    let status = &replies[0];
    assert_eq!(status["demo"], true);
    assert_eq!(status["models"], serde_json::json!(["demo-model"]));
    assert_eq!(status["remaining_calls"], 10);
    assert_eq!(status["max_output_tokens"], 1024);
    assert!(status["expires_in_seconds"].as_u64().unwrap() > 0);
    assert_eq!(replies[1]["demo"], true);
    let history = &replies[2];
    assert_eq!(history["events"].as_array().unwrap().len(), 1);
    assert!(!history.to_string().contains("prompt"));
    assert!(!history.to_string().contains("Explain this demonstration"));
    assert_eq!(replies[3]["state"], "locked");
    assert_eq!(replies[4]["ok"], false);
}

#[test]
fn guided_demo_refuses_a_supplied_folder() {
    let folder = PrivateTempDir::create(".kb-test-").unwrap();
    let output = run(
        folder.path(),
        &["--directory", "existing", "demo", "--guided"],
        "",
    );
    assert_eq!(output.status.code(), Some(1));
    assert!(
        json(&output.stderr)["error"]
            .as_str()
            .unwrap()
            .contains("without --directory")
    );
    assert!(!folder.path().join("existing").exists());
}

#[test]
fn a_cancelled_guided_demo_still_removes_its_folder() {
    // The stop flag is already set, as if Control-C arrived at the start.
    let result = guided(&AtomicBool::new(true));
    assert!(matches!(result, Err(Stop::Cancelled)));
    let mine = format!(".kb-demo-{}-", std::process::id());
    assert!(leftovers(Path::new("."), &mine).is_empty());
}

#[test]
fn demo_session_serves_status_ask_history_and_lock() {
    let folder = PrivateTempDir::create(".kb-test-").unwrap();
    let session = ["--directory", "session"];
    let call = |arguments: &[&str], stdin: &str| {
        run(folder.path(), &[&session[..], arguments].concat(), stdin)
    };

    let mut server = Running(
        Command::new(KEYBRIDGE)
            .args(session)
            .arg("demo")
            .current_dir(folder.path())
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap(),
    );
    let mut started = BufReader::new(server.0.stdout.take().unwrap());
    let mut line = String::new();
    started.read_line(&mut line).unwrap();
    assert!(line.contains("DEMO"));
    // The next line is printed only after the socket is listening.
    line.clear();
    started.read_line(&mut line).unwrap();
    assert!(line.contains("Session started"));

    let status = json(&call(&["status"], "").stdout);
    assert_eq!(status["demo"], true);
    assert_eq!(status["max_output_tokens"], 1024);

    // A second session cannot take over the first one's socket.
    assert_eq!(call(&["demo"], "").status.code(), Some(1));
    assert_eq!(json(&call(&["status"], "").stdout)["state"], "ready");

    let answer = call(&["ask", "--model", "demo-model"], "a private test prompt");
    assert!(answer.status.success());
    assert!(
        json(&answer.stdout)["text"]
            .as_str()
            .unwrap()
            .contains("Demo response")
    );

    let refused = call(&["ask", "--model", "other-model"], "hello");
    assert_eq!(refused.status.code(), Some(1));
    assert!(
        json(&refused.stdout)["error"]
            .as_str()
            .unwrap()
            .contains("not approved")
    );

    let too_long = call(&["ask", "--model", "demo-model"], &"x".repeat(16001));
    assert_eq!(too_long.status.code(), Some(1));
    assert!(
        json(&too_long.stderr)["error"]
            .as_str()
            .unwrap()
            .contains("too large")
    );

    let history = call(&["history"], "");
    assert!(!text(&history.stdout).contains("private test prompt"));
    // Only the approved request was attempted; the refused ones never count.
    assert_eq!(json(&history.stdout)["events"].as_array().unwrap().len(), 1);

    assert!(call(&["lock"], "").status.success());
    let denied = call(&["ask", "--model", "demo-model"], "again");
    assert_eq!(denied.status.code(), Some(1));
    assert!(
        json(&denied.stdout)["error"]
            .as_str()
            .unwrap()
            .contains("locked or expired")
    );

    // A live start needs a real terminal; a captured one is refused before any key prompt.
    let start = call(
        &["start", "--provider", "moonshot", "--model", "demo-model"],
        "",
    );
    assert_eq!(start.status.code(), Some(1));
    assert!(
        json(&start.stderr)["error"]
            .as_str()
            .unwrap()
            .contains("private terminal")
    );

    server.signal("-TERM");
    let (status, stderr) = server.finish();
    assert!(status.success());
    assert_eq!(stderr, "");
    assert!(!folder.path().join("session/session.sock").exists());
}

#[test]
fn closing_the_terminal_stops_the_session_cleanly() {
    // The terminal is gone: output goes nowhere, then the hang-up signal arrives.
    let folder = PrivateTempDir::create(".kb-test-").unwrap();
    let socket = folder.path().join("session/session.sock");
    let mut server = Running(
        Command::new(KEYBRIDGE)
            .args(["--directory", "session", "demo"])
            .current_dir(folder.path())
            .stdin(Stdio::null())
            .stdout(closed_output())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap(),
    );
    wait_for(&socket);
    server.signal("-HUP");
    let (status, stderr) = server.finish();
    assert_eq!(
        status.code(),
        Some(0),
        "the session must exit normally, not crash"
    );
    assert_eq!(stderr, "");
    assert!(!socket.exists());
}

#[test]
fn guided_demo_with_closed_output_exits_and_leaves_nothing() {
    let folder = PrivateTempDir::create(".kb-test-").unwrap();
    let mut demo = Running(
        Command::new(KEYBRIDGE)
            .args(["demo", "--guided"])
            .current_dir(folder.path())
            .stdin(Stdio::null())
            .stdout(closed_output())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap(),
    );
    let (status, stderr) = demo.finish();
    assert_eq!(status.code(), Some(1));
    assert_eq!(stderr, "[Keybridge] Cancelled.\n");
    assert!(leftovers(folder.path(), ".kb-demo-").is_empty());
}

#[test]
fn a_missing_session_has_a_safe_error() {
    let folder = PrivateTempDir::create(".kb-test-").unwrap();
    let output = run(folder.path(), &["--directory", "session", "status"], "");
    assert_eq!(output.status.code(), Some(1));
    assert!(
        json(&output.stderr)["error"]
            .as_str()
            .unwrap()
            .contains("unavailable")
    );
}

#[test]
fn unknown_commands_print_a_safe_error() {
    let folder = PrivateTempDir::create(".kb-test-").unwrap();
    let output = run(folder.path(), &["get-secret"], "");
    assert_eq!(output.status.code(), Some(1));
    assert!(
        json(&output.stderr)["error"]
            .as_str()
            .unwrap()
            .contains("keybridge help")
    );
    assert!(text(&run(folder.path(), &["help"], "").stdout).contains("demo --guided"));
}
