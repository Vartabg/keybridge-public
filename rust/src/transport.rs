//! Local Unix-socket transport. This does not isolate same-user processes.

use std::fs;
use std::io::{BufRead, BufReader, ErrorKind, Read, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::Duration;

use serde_json::{Value, json};

use crate::broker::Broker;
use crate::error::BridgeError;
use crate::output::ascii_json;

/// Largest request line a client may send, in bytes.
pub const FRAME_LIMIT: usize = 65536;
/// Largest reply line a client will read, in bytes.
pub const RESPONSE_LIMIT: usize = 3_200_000;
/// Portable socket-path budget for macOS and Linux, in bytes.
pub const SOCKET_PATH_LIMIT: usize = 103;

const IDLE_POLL: Duration = Duration::from_millis(50);

pub fn validate_socket_path(path: &Path) -> Result<(), BridgeError> {
    let length = path.as_os_str().as_encoded_bytes().len();
    if length > SOCKET_PATH_LIMIT {
        return Err(BridgeError::new(format!(
            "The local socket path is too long ({length} bytes; maximum {SOCKET_PATH_LIMIT}). \
             Use a shorter --directory path for every command, or run demo --guided."
        )));
    }
    Ok(())
}

/// The session folder: created if missing, owned by you, permission 700.
pub fn private_directory(path: Option<&Path>) -> Result<PathBuf, BridgeError> {
    let directory = match path {
        Some(path) => path.to_path_buf(),
        None => {
            let home = std::env::var_os("HOME")
                .map(PathBuf::from)
                .filter(|home| home.is_absolute())
                .ok_or_else(|| {
                    BridgeError::new("Set HOME, or choose a folder with --directory.")
                })?;
            home.join(".local/share/keybridge")
        }
    };
    if fs::symlink_metadata(&directory).is_ok_and(|info| info.file_type().is_symlink()) {
        return Err(BridgeError::new(
            "The Keybridge folder must not be a symbolic link.",
        ));
    }
    let not_private =
        || BridgeError::new("The Keybridge folder must belong to you with permission 700.");
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&directory)
        .map_err(|_| not_private())?;
    let info = fs::metadata(&directory).map_err(|_| not_private())?;
    let mine = info.uid() == rustix::process::getuid().as_raw();
    if !mine || info.mode() & 0o7777 != 0o700 {
        return Err(not_private());
    }
    Ok(directory)
}

/// A listening session socket. Dropping it locks the broker and removes the
/// socket file, but only if the file is still the one this server created.
pub struct Server {
    listener: UnixListener,
    path: PathBuf,
    inode: u64,
    broker: Arc<Broker>,
}

impl Server {
    pub fn bind(path: &Path, broker: Arc<Broker>) -> Result<Server, BridgeError> {
        validate_socket_path(path)?;
        // Never replace an existing socket: it may be another session.
        if fs::symlink_metadata(path).is_ok() {
            return Err(BridgeError::new(
                "A session socket already exists. Stop that session before starting another.",
            ));
        }
        let failed = || {
            BridgeError::new(
                "The local session could not start. Check the folder and stop any existing session.",
            )
        };
        let listener = UnixListener::bind(path).map_err(|_| failed())?;
        let inode = fs::symlink_metadata(path).map_err(|_| failed())?.ino();
        // From here on `server` owns the socket file; an early return cleans it up.
        let server = Server {
            listener,
            path: path.to_path_buf(),
            inode,
            broker,
        };
        fs::set_permissions(path, fs::Permissions::from_mode(0o600)).map_err(|_| {
            BridgeError::new(
                "The local socket could not be made private. Check the folder permissions.",
            )
        })?;
        server
            .listener
            .set_nonblocking(true)
            .map_err(|_| failed())?;
        Ok(server)
    }

    /// Answer clients until `stop` becomes true. Each connection gets its own
    /// thread, so a slow provider call does not block status or lock.
    pub fn serve_until(&self, stop: &AtomicBool) {
        while !stop.load(Ordering::Relaxed) {
            self.broker.tick();
            match self.listener.accept() {
                Ok((stream, _)) => {
                    let broker = Arc::clone(&self.broker);
                    // If the system cannot start a thread, this one
                    // connection is dropped; the session keeps running.
                    let _ = thread::Builder::new().spawn(move || answer(stream, &broker));
                }
                Err(error) if error.kind() == ErrorKind::WouldBlock => thread::sleep(IDLE_POLL),
                Err(_) => thread::sleep(IDLE_POLL),
            }
        }
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        self.broker.lock();
        let still_ours =
            fs::symlink_metadata(&self.path).is_ok_and(|info| info.ino() == self.inode);
        if still_ours {
            let _ = fs::remove_file(&self.path);
        }
    }
}

/// Read one request line, ask the broker, write one reply line.
fn answer(stream: UnixStream, broker: &Broker) {
    let _ = stream.set_nonblocking(false);
    let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(3)));
    let reply = match read_request(&stream) {
        Some(request) => broker.handle(&request),
        None => json!({"ok": false, "error": "Send one valid, size-limited JSON request."}),
    };
    // If the client has gone away there is nothing to retry and nothing to log.
    let _ = (&stream).write_all(format!("{}\n", ascii_json(&reply)).as_bytes());
}

fn read_request(stream: &UnixStream) -> Option<Value> {
    let raw = read_line(stream, FRAME_LIMIT)?;
    serde_json::from_slice(&raw).ok()
}

/// Read up to `limit` bytes ending in a newline. Anything else is refused.
fn read_line(stream: &UnixStream, limit: usize) -> Option<Vec<u8>> {
    let mut raw = Vec::new();
    let mut reader = BufReader::new(stream).take(limit as u64 + 1);
    reader.read_until(b'\n', &mut raw).ok()?;
    if raw.len() > limit || raw.last() != Some(&b'\n') {
        return None;
    }
    Some(raw)
}

/// Client side: send one request to a running session and return its reply.
pub fn request(path: &Path, message: &Value) -> Result<Value, BridgeError> {
    validate_socket_path(path)?;
    let encoded = format!("{message}\n");
    if encoded.len() > FRAME_LIMIT {
        return Err(BridgeError::new(
            "The prompt is too large. Shorten it and try again.",
        ));
    }
    let unavailable = || {
        BridgeError::new(
            "Keybridge is unavailable. Start it in your private terminal; avoid automatic retries.",
        )
    };
    let stream = UnixStream::connect(path).map_err(|_| unavailable())?;
    let timeout = Some(Duration::from_secs(55));
    stream
        .set_read_timeout(timeout)
        .map_err(|_| unavailable())?;
    stream
        .set_write_timeout(timeout)
        .map_err(|_| unavailable())?;
    (&stream)
        .write_all(encoded.as_bytes())
        .map_err(|_| unavailable())?;
    let raw = read_line(&stream, RESPONSE_LIMIT).ok_or_else(unavailable)?;
    serde_json::from_slice(&raw).map_err(|_| unavailable())
}
