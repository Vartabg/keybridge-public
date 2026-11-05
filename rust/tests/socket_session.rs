//! The real Unix socket, with a fake-key broker behind it.

use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::net::Shutdown;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use keybridge::broker::{Broker, SESSION_ENDED};
use keybridge::demo::{PrivateTempDir, demo_broker};
use keybridge::transport::{Server, private_directory, request, validate_socket_path};
use serde_json::{Value, json};

/// A demo session served from a background thread until the test ends.
struct Session {
    directory: PrivateTempDir,
    socket: PathBuf,
    broker: Arc<Broker>,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl Session {
    fn start() -> Session {
        let directory = PrivateTempDir::create(".kb-test-").unwrap();
        let socket = directory.path().join("session.sock");
        let broker = Arc::new(demo_broker());
        let server = Server::bind(&socket, Arc::clone(&broker)).unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let flag = Arc::clone(&stop);
        // The server moves into the thread and is dropped when the loop ends.
        let thread = thread::spawn(move || server.serve_until(&flag));
        Session {
            directory,
            socket,
            broker,
            stop,
            thread: Some(thread),
        }
    }

    fn stop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            thread.join().unwrap();
        }
    }

    fn send(&self, message: Value) -> Value {
        request(&self.socket, &message).unwrap()
    }

    fn send_raw(&self, payload: &[u8]) -> Value {
        let mut client = UnixStream::connect(&self.socket).unwrap();
        client
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        // The server may answer and hang up before reading everything.
        let _ = client.write_all(payload);
        let _ = client.shutdown(Shutdown::Write);
        // Read exactly one reply line. On Linux, reading further can report a
        // reset when the server hung up with part of the request unread.
        let mut reply = String::new();
        BufReader::new(client).read_line(&mut reply).unwrap();
        serde_json::from_str(&reply).unwrap()
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        self.stop();
    }
}

fn ask() -> Value {
    json!({"operation": "generate", "model": "demo-model", "prompt": "hello"})
}

fn mode(path: &Path) -> u32 {
    fs::symlink_metadata(path).unwrap().permissions().mode() & 0o7777
}

#[test]
fn status_generate_lock_round_trip() {
    let session = Session::start();
    assert_eq!(mode(&session.socket), 0o600);
    assert_eq!(mode(session.directory.path()), 0o700);
    assert_eq!(session.send(json!({"operation": "status"}))["demo"], true);
    let reply = session.send(ask());
    assert_eq!(
        reply["text"],
        "Demo response. Your prompt was not sent to an API provider."
    );
    assert_eq!(session.send(json!({"operation": "lock"}))["ok"], true);
    assert_eq!(
        session.send(ask()),
        json!({"ok": false, "error": SESSION_ENDED})
    );
}

#[test]
fn invalid_json_and_oversized_frames_are_rejected() {
    let session = Session::start();
    let mut oversized = vec![b'x'; 65537];
    oversized.push(b'\n');
    for payload in [
        &b"not-json\n"[..],
        &oversized,
        b"{\"operation\":NaN}\n",
        b"{\"operation\":\"status\"}",
    ] {
        let reply = session.send_raw(payload);
        assert_eq!(
            reply,
            json!({"ok": false, "error": "Send one valid, size-limited JSON request."})
        );
    }
    assert_eq!(
        session.send(json!({"operation": "status"}))["state"],
        "ready"
    );
}

#[test]
fn an_existing_socket_is_never_replaced() {
    let session = Session::start();
    let error = Server::bind(&session.socket, Arc::new(demo_broker()))
        .err()
        .unwrap();
    assert!(error.message().contains("already exists"));
    // The first session is untouched by the refused second one.
    assert_eq!(
        session.send(json!({"operation": "status"}))["state"],
        "ready"
    );
}

#[test]
fn stopping_the_server_locks_the_broker_and_removes_the_socket() {
    let mut session = Session::start();
    assert!(session.broker.holds_key());
    session.stop();
    assert!(!session.broker.holds_key());
    assert!(!session.socket.exists());
    let error = request(&session.socket, &json!({"operation": "status"})).unwrap_err();
    assert!(error.message().contains("unavailable"));
}

#[test]
fn cleanup_leaves_a_replaced_path_alone() {
    let mut session = Session::start();
    fs::remove_file(&session.socket).unwrap();
    fs::write(&session.socket, "replacement belongs to someone else").unwrap();
    session.stop();
    assert_eq!(
        fs::read_to_string(&session.socket).unwrap(),
        "replacement belongs to someone else"
    );
}

#[test]
fn an_overlong_path_gets_a_clear_error_and_no_socket() {
    let directory = PrivateTempDir::create(".kb-test-").unwrap();
    let used = directory.path().as_os_str().len() + 1;
    let path = directory.path().join("x".repeat(119 - used));
    assert_eq!(path.as_os_str().len(), 119);
    let error = Server::bind(&path, Arc::new(demo_broker())).err().unwrap();
    assert!(error.message().contains("119 bytes"));
    assert!(error.message().contains("shorter --directory"));
    assert!(!path.exists());
    assert!(
        request(&path, &json!({"operation": "status"}))
            .unwrap_err()
            .message()
            .contains("119 bytes")
    );
}

#[test]
fn the_path_limit_counts_bytes_not_characters() {
    let path = PathBuf::from("é".repeat(60));
    assert!(path.to_str().unwrap().chars().count() < 103);
    assert!(validate_socket_path(&path).is_err());
    assert!(validate_socket_path(Path::new("short/session.sock")).is_ok());
}

#[test]
fn the_session_folder_must_be_private_and_not_a_link() {
    let root = PrivateTempDir::create(".kb-test-").unwrap();
    let created = private_directory(Some(&root.path().join("new/nested"))).unwrap();
    assert_eq!(mode(&created), 0o700);

    let open = root.path().join("open");
    fs::create_dir(&open).unwrap();
    fs::set_permissions(&open, fs::Permissions::from_mode(0o755)).unwrap();
    assert!(
        private_directory(Some(&open))
            .unwrap_err()
            .message()
            .contains("permission 700")
    );

    let link = root.path().join("link");
    symlink("new", &link).unwrap();
    assert!(
        private_directory(Some(&link))
            .unwrap_err()
            .message()
            .contains("symbolic link")
    );
}
