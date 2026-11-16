//! Bounded JSON requests shared by backend consumers on Darwin and Linux.

use std::{
    fmt,
    io::{self, Read},
    os::{fd::OwnedFd, unix::{net::UnixStream, process::CommandExt}},
    process::{Child, Command, ExitStatus, Stdio},
    sync::atomic::{AtomicBool, Ordering},
    thread,
    time::{Duration, Instant},
};

use serde_json::Value;

#[derive(Clone, Copy, Debug)]
pub struct Limits {
    pub timeout: Duration,
    pub stdout_bytes: usize,
    pub stderr_bytes: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            timeout: Duration::from_secs(120),
            stdout_bytes: 8 * 1024 * 1024,
            stderr_bytes: 256 * 1024,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ErrorKind {
    Startup,
    Io,
    Protocol,
    Timeout,
    OutputLimit,
    Backend,
    Cancelled,
}

/// A valid response is retained even when the process or business operation failed.
#[derive(Clone, Debug)]
pub struct JsonResponse {
    pub payload: Value,
    pub status: ExitStatus,
    pub stderr: String,
}

impl JsonResponse {
    pub fn is_success(&self) -> bool {
        self.status.success() && self.payload.get("ok").and_then(Value::as_bool) != Some(false)
    }

    /// Use for mutations. Read-only reports may inspect the response directly.
    pub fn into_success(self, fallback: &str) -> Result<Value, TransportError> {
        if self.is_success() {
            Ok(self.payload)
        } else {
            Err(TransportError {
                kind: ErrorKind::Backend,
                message: format!("{} ({})", failure_message(&self.payload, fallback), self.status),
                stderr: self.stderr.clone(),
                response: Some(Box::new(self)),
            })
        }
    }
}

#[derive(Debug)]
pub struct TransportError {
    pub kind: ErrorKind,
    pub message: String,
    /// Backend failures retain data, errors, warnings, and the original exit status.
    pub response: Option<Box<JsonResponse>>,
    pub stderr: String,
}

impl TransportError {
    fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Self { kind, message: message.into(), response: None, stderr: String::new() }
    }
}

impl fmt::Display for TransportError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:?}: {}", self.kind, self.message)
    }
}

impl std::error::Error for TransportError {}

pub fn failure_message(payload: &Value, fallback: &str) -> String {
    let error = payload.get("error");
    let message = error.and_then(|error| error.get("message")).and_then(Value::as_str)
        .unwrap_or(fallback);
    match error.and_then(|error| error.get("code")).and_then(Value::as_str) {
        Some(code) => format!("{code}: {message}"),
        None => message.to_string(),
    }
}

/// Validate envelopes centrally while allowing existing top-level JSON reports.
/// `expected_command` is the exact wire command, including Storage's space separator.
pub fn validate_payload(payload: &Value, expected_command: &str) -> Result<(), TransportError> {
    if !payload.is_object() {
        return Err(TransportError::new(ErrorKind::Protocol, "JSON response must be an object"));
    }
    if let Some(version) = payload.get("schemaVersion") {
        if version.as_u64() != Some(1) {
            return Err(TransportError::new(ErrorKind::Protocol, "unsupported JSON schemaVersion"));
        }
    }
    // Legacy reports have schemaVersion but no command/ok/data envelope.
    if payload.get("command").is_none() && payload.get("ok").is_none() {
        return Ok(());
    }
    validate_envelope(payload, expected_command)
}

/// Mutations must carry an envelope; legacy report compatibility does not apply.
pub fn validate_envelope(payload: &Value, expected_command: &str) -> Result<(), TransportError> {
    if payload.get("schemaVersion").and_then(Value::as_u64) != Some(1)
        || payload.get("command").and_then(Value::as_str) != Some(expected_command)
        || payload.get("ok").and_then(Value::as_bool).is_none()
        || payload.get("data").is_none()
        || payload.get("warnings").is_some_and(|value| !value.is_array())
    {
        return Err(TransportError::new(
            ErrorKind::Protocol,
            format!("invalid JSON envelope for {expected_command}: expected schemaVersion 1, matching command, boolean ok and data"),
        ));
    }
    Ok(())
}

struct Capture {
    stream: UnixStream,
    bytes: Vec<u8>,
    eof: bool,
    limit: usize,
    name: &'static str,
}

impl Capture {
    fn new(limit: usize, name: &'static str) -> io::Result<(Self, Stdio)> {
        // Separate socket endpoints let only the reader be nonblocking. Unlike
        // reader threads, inherited output handles cannot outlive the deadline.
        let (reader, writer) = UnixStream::pair()?;
        reader.set_nonblocking(true)?;
        Ok((Self { stream: reader, bytes: Vec::new(), eof: false, limit, name },
            Stdio::from(OwnedFd::from(writer))))
    }

    fn drain(&mut self) -> Result<(), TransportError> {
        // Bound work per poll so continuous output cannot starve the deadline
        // or the other stream. Never retain more than the configured limit.
        let mut chunk = [0; 8192];
        for _ in 0..16 {
            match self.stream.read(&mut chunk) {
                Ok(0) => { self.eof = true; break; }
                Ok(count) => {
                    if count > self.limit.saturating_sub(self.bytes.len()) {
                        return Err(TransportError::new(ErrorKind::OutputLimit,
                            format!("envy {} exceeded {} bytes", self.name, self.limit)));
                    }
                    self.bytes.extend_from_slice(&chunk[..count]);
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(TransportError::new(ErrorKind::Io,
                    format!("could not read envy {}: {error}", self.name))),
            }
        }
        Ok(())
    }
}

struct RunningChild(Option<Child>);

impl Drop for RunningChild {
    fn drop(&mut self) {
        let Some(mut child) = self.0.take() else { return; };
        // A dedicated process group also covers descendants holding stdout or
        // stderr after the immediate child exited. Never join a blocked reader.
        unsafe { kill(-(child.id() as i32), 9); }
        let _ = child.kill();
        let deadline = Instant::now() + Duration::from_millis(250);
        loop {
            match child.try_wait() {
                Ok(Some(_)) => return,
                _ if Instant::now() >= deadline => break,
                _ => thread::sleep(Duration::from_millis(5)),
            }
        }
        // Reap a slow-to-terminate child without extending the caller's bound.
        thread::spawn(move || { let _ = child.wait(); });
    }
}

// POSIX kill is available on both supported platforms; no extra crate is needed.
extern "C" {
    fn kill(pid: i32, signal: i32) -> i32;
}

/// Spawn a non-interactive command and read one complete, bounded JSON document.
/// A nonzero exit with valid JSON is a response, not a protocol error. Call
/// `into_success` before accepting a mutation; reports can retain partial data.
pub fn run_json(
    command: &mut Command,
    expected_command: &str,
    limits: Limits,
) -> Result<JsonResponse, TransportError> {
    run_json_controlled(command, expected_command, limits, &AtomicBool::new(false), |_| {})
}

/// Long-running JSON reports use the same bounds and cleanup as ordinary reads.
/// Cancellation drops the entire process group; stderr progress is observable
/// while stdout remains a single complete JSON document.
pub fn run_json_controlled(
    command: &mut Command,
    expected_command: &str,
    limits: Limits,
    cancelled: &AtomicBool,
    mut progress: impl FnMut(&str),
) -> Result<JsonResponse, TransportError> {
    let started = Instant::now();
    let setup_error = |error| TransportError::new(ErrorKind::Startup,
        format!("could not prepare envy output: {error}"));
    let (mut stdout, stdout_writer) = Capture::new(limits.stdout_bytes, "stdout").map_err(setup_error)?;
    let (mut stderr, stderr_writer) = Capture::new(limits.stderr_bytes, "stderr").map_err(setup_error)?;
    command.stdin(Stdio::null()).stdout(stdout_writer).stderr(stderr_writer).process_group(0);
    let spawned = command.spawn();
    // Command retains configured descriptors after spawn. Drop its writer copies
    // now so EOF depends only on the child (and any descendants).
    command.stdout(Stdio::null()).stderr(Stdio::null());
    let mut child = RunningChild(Some(spawned.map_err(|error|
        TransportError::new(ErrorKind::Startup, format!("could not start envy: {error}")))?));
    let mut status = None;
    let result = (|| { loop {
        if cancelled.load(Ordering::Relaxed) {
            return Err(TransportError::new(ErrorKind::Cancelled, "envy request cancelled; child process group stopped"));
        }
        if started.elapsed() >= limits.timeout {
            return Err(TransportError::new(ErrorKind::Timeout,
                format!("envy request exceeded {:?}", limits.timeout)));
        }
        stdout.drain()?;
        let previous = stderr.bytes.len();
        stderr.drain()?;
        if stderr.bytes.len() > previous {
            progress(&String::from_utf8_lossy(&stderr.bytes[previous..]));
        }
        if status.is_none() {
            status = child.0.as_mut().unwrap().try_wait().map_err(|error|
                TransportError::new(ErrorKind::Io, format!("could not poll envy: {error}")))?;
        }
        if let Some(status) = status.filter(|_| stdout.eof && stderr.eof) {
            child.0 = None;
            let stderr = String::from_utf8_lossy(&stderr.bytes).into_owned();
            let payload: Value = serde_json::from_slice(&stdout.bytes).map_err(|error| {
                TransportError::new(ErrorKind::Protocol,
                    format!("envy returned invalid JSON ({status}): {error}; stderr: {}", stderr.trim()))
            })?;
            validate_payload(&payload, expected_command)?;
            return Ok(JsonResponse { payload, status, stderr });
        }
        thread::sleep(Duration::from_millis(5));
    } })();
    result.map_err(|mut error: TransportError| {
        error.stderr = String::from_utf8_lossy(&stderr.bytes).into_owned();
        error
    })
}

#[cfg(test)]
mod tests;
