//! Management commands never carry driving inputs. GUI output cannot block polling.
pub mod platform;
pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;

#[derive(Debug)]
pub struct PreparationStopped(pub &'static str);

impl std::fmt::Display for PreparationStopped {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.0)
    }
}

impl std::error::Error for PreparationStopped {}

/// Checked setup reports do not own devices or replace the runtime guard.
/// Phase transitions are discrete; periodic runtime updates remain limited to 5Hz.
pub fn prepare<T>(
    managed: Option<&Managed>,
    check: &str,
    operation: impl FnOnce() -> Result<T>,
) -> Result<T> {
    let report = |state: &str| {
        if let Some(manager) = managed {
            manager.emit(json!({"event":"state", "phase":"preparing",
                "preparation":{"check":check,"state":state}}));
        }
    };
    if let Some(reason) = managed.and_then(|m| m.control.stop_reason()) {
        report("cancelled");
        return Err(Box::new(PreparationStopped(reason)));
    }
    report("checking");
    let value = match operation() {
        Ok(value) => value,
        Err(error) => {
            report("failed");
            return Err(error);
        }
    };
    if let Some(reason) = managed.and_then(|m| m.control.stop_reason()) {
        report("cancelled");
        return Err(Box::new(PreparationStopped(reason)));
    }
    report("verified");
    Ok(value)
}

/// Completion of a managed job is distinct from a completed lap or performance gate.
pub fn outcome(reason: &str) -> &'static str {
    match reason {
        "time_limit" | "scenario_complete" => "completed",
        "user_stop" | "f8" | "ctrl_c" => "stopped",
        "connection_closed" | "heartbeat_timeout" | "protocol_error" | "output_error" => "failed",
        _ => "needs_attention",
    }
}
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    io::{self, BufRead, Read, Write},
    sync::{
        Arc,
        atomic::{AtomicU8, AtomicU64, Ordering},
        mpsc,
    },
    thread,
    time::Instant,
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Command {
    version: u32,
    command: String,
}

#[derive(Clone)]
pub struct Control {
    started: Instant,
    heartbeat_ms: Arc<AtomicU64>,
    limit_ms: Arc<AtomicU64>,
    reason: Arc<AtomicU8>,
}
impl Default for Control {
    fn default() -> Self {
        Self {
            started: Instant::now(),
            heartbeat_ms: Arc::new(AtomicU64::new(0)),
            limit_ms: Arc::new(AtomicU64::new(7_200_000)),
            reason: Arc::new(AtomicU8::new(0)),
        }
    }
}
impl Control {
    pub fn clear_time_limit(&self) {
        self.limit_ms.store(u64::MAX, Ordering::Release);
    }
    pub fn set_time_limit(&self, seconds: f64) -> Result<()> {
        if !seconds.is_finite() || seconds <= 0. || seconds > 7200. {
            return Err("Managed duration must be in (0, 7200]".into());
        }
        self.limit_ms
            .store((seconds * 1000.).ceil() as u64, Ordering::Release);
        Ok(())
    }
    pub fn cancel(&self, reason: u8) {
        let _ = self
            .reason
            .compare_exchange(0, reason, Ordering::AcqRel, Ordering::Relaxed);
    }
    pub fn stop_reason(&self) -> Option<&'static str> {
        if (self.started.elapsed().as_millis() as u64)
            .saturating_sub(self.heartbeat_ms.load(Ordering::Acquire))
            >= 3000
        {
            self.cancel(3);
        }
        if self.started.elapsed().as_millis() as u64 >= self.limit_ms.load(Ordering::Acquire) {
            self.cancel(6);
        }
        match self.reason.load(Ordering::Acquire) {
            0 => None,
            1 => Some("user_stop"),
            2 => Some("connection_closed"),
            3 => Some("heartbeat_timeout"),
            4 => Some("protocol_error"),
            6 => Some("time_limit"),
            _ => Some("output_error"),
        }
    }
    pub fn accept(&self, line: &str) -> Result<()> {
        let command: Command = serde_json::from_str(line)?;
        if command.version != 1 {
            return Err("Unsupported management version".into());
        }
        match command.command.as_str() {
            "heartbeat" => self
                .heartbeat_ms
                .store(self.started.elapsed().as_millis() as u64, Ordering::Release),
            "stop" => self.cancel(1),
            _ => return Err("Unknown management command".into()),
        }
        Ok(())
    }
    pub fn listen(&self, mut input: impl BufRead) {
        loop {
            let mut line = String::new();
            // Bound malformed input without allocating an unbounded line.
            let read = input.by_ref().take(4097).read_line(&mut line);
            match read {
                Ok(0) => {
                    self.cancel(2);
                    return;
                }
                Ok(n) if n <= 4096 && self.accept(&line).is_ok() => (),
                _ => {
                    self.cancel(4);
                    return;
                }
            }
        }
    }
}

pub struct Managed {
    pub control: Control,
    sender: mpsc::SyncSender<Value>,
    writer: Option<thread::JoinHandle<()>>,
}
impl Managed {
    pub fn start() -> Self {
        let control = Control::default();
        let input = control.clone();
        thread::spawn(move || input.listen(io::stdin().lock()));
        let (sender, receiver) = mpsc::sync_channel::<Value>(32);
        let output_control = control.clone();
        let writer = thread::spawn(move || {
            let mut output = io::stdout().lock();
            for mut value in receiver {
                value["version"] = json!(1);
                let terminal = value["event"] == "finished";
                if serde_json::to_writer(&mut output, &value).is_err()
                    || output.write_all(b"\n").is_err()
                    || output.flush().is_err()
                {
                    output_control.cancel(5);
                    return;
                }
                if terminal {
                    return;
                }
            }
        });
        Self {
            control,
            sender,
            writer: Some(writer),
        }
    }
    /// Nonblocking, bounded progress output, invoked at most five times a second.
    pub fn emit(&self, value: Value) {
        if let Err(mpsc::TrySendError::Disconnected(_)) = self.sender.try_send(value) {
            self.control.cancel(5);
        }
    }
    /// Called only after vehicle release / recording flush; never in the control loop.
    pub fn finish(mut self, state: &str, reason: &str, path: Option<&std::path::Path>) {
        let _ = self
            .sender
            .send(json!({"event":"finished","state":state,"reason":reason,"result_dir":path}));
        if let Some(writer) = self.writer.take() {
            let _ = writer.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn preparation_reports_actual_result_and_preserves_failure() {
        let (sender, receiver) = mpsc::sync_channel(32);
        let manager = Managed {
            control: Control::default(),
            sender,
            writer: None,
        };
        assert_eq!(prepare(Some(&manager), "path", || Ok(42)).unwrap(), 42);
        assert_eq!(receiver.recv().unwrap()["preparation"]["state"], "checking");
        assert_eq!(receiver.recv().unwrap()["preparation"]["state"], "verified");
        let failure = prepare::<()>(Some(&manager), "profile", || Err("bad hash".into()));
        assert_eq!(failure.unwrap_err().to_string(), "bad hash");
        assert_eq!(receiver.recv().unwrap()["preparation"]["check"], "profile");
        assert_eq!(receiver.recv().unwrap()["preparation"]["state"], "failed");
    }

    #[test]
    fn preparation_cancel_blocks_later_operations() {
        let (sender, receiver) = mpsc::sync_channel(32);
        let manager = Managed {
            control: Control::default(),
            sender,
            writer: None,
        };
        let result = prepare(Some(&manager), "path", || {
            manager.control.cancel(1);
            Ok(())
        });
        assert!(result.unwrap_err().is::<PreparationStopped>());
        assert_eq!(receiver.recv().unwrap()["preparation"]["state"], "checking");
        assert_eq!(
            receiver.recv().unwrap()["preparation"]["state"],
            "cancelled"
        );
        let result = prepare::<()>(Some(&manager), "device", || panic!("Must not acquire"));
        assert_eq!(result.unwrap_err().to_string(), "user_stop");
        assert_eq!(
            receiver.recv().unwrap()["preparation"]["state"],
            "cancelled"
        );
    }
    #[test]
    fn outcomes_distinguish_stop_failure_and_attention() {
        assert_eq!(outcome("f8"), "stopped");
        assert_eq!(outcome("scenario_complete"), "completed");
        assert_eq!(outcome("heartbeat_timeout"), "failed");
        assert_eq!(outcome("foreground_lost"), "needs_attention");
    }
    #[test]
    fn commands_cannot_start_or_drive() {
        let c = Control::default();
        for line in [
            r#"{"version":1,"command":"start"}"#,
            r#"{"version":1,"command":"heartbeat","steering":1}"#,
            r#"{"version":2,"command":"stop"}"#,
        ] {
            assert!(c.accept(line).is_err());
        }
    }
    #[test]
    fn eof_and_malformed_stop() {
        for (input, reason) in [("", "connection_closed"), ("invalid\n", "protocol_error")] {
            let c = Control::default();
            c.listen(io::Cursor::new(input));
            assert_eq!(c.stop_reason(), Some(reason));
        }
    }
    #[test]
    fn stop_is_latched_across_heartbeat_and_eof() {
        let c = Control::default();
        c.listen(io::Cursor::new(
            "{\"version\":1,\"command\":\"stop\"}\n{\"version\":1,\"command\":\"heartbeat\"}\n",
        ));
        assert_eq!(c.stop_reason(), Some("user_stop"));
    }
    #[test]
    fn frozen_gui_expires() {
        let c = Control {
            started: Instant::now() - std::time::Duration::from_secs(4),
            ..Control::default()
        };
        assert_eq!(c.stop_reason(), Some("heartbeat_timeout"));
    }
    #[test]
    fn oversized_command_is_bounded() {
        let c = Control::default();
        c.listen(io::Cursor::new(" ".repeat(5000)));
        assert_eq!(c.stop_reason(), Some("protocol_error"));
    }
    #[test]
    fn duration_includes_waiting() {
        let c = Control {
            started: Instant::now() - std::time::Duration::from_millis(100),
            ..Control::default()
        };
        c.set_time_limit(0.05).unwrap();
        assert_eq!(c.stop_reason(), Some("time_limit"));
        assert!(Control::default().set_time_limit(f64::NAN).is_err());
        assert!(Control::default().set_time_limit(7201.).is_err());
    }
    #[test]
    #[ignore = "Temporarily owns global F7/F8; run explicitly without a live driving job"]
    fn hotkey_conflict_and_release() {
        let first = platform::Hotkeys::register().unwrap();
        assert!(platform::Hotkeys::register().is_err());
        drop(first);
        let recovered = platform::Hotkeys::register().unwrap();
        drop(recovered);
    }
}
