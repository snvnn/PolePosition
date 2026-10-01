use crate::Result;
use serde::Serialize;
use std::{
    fs::OpenOptions,
    io::BufWriter,
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};
#[derive(Debug, Clone, Serialize)]
pub struct CommandRow {
    pub now_ns: u64,
    pub sequence: Option<u64>,
    pub created_ns: Option<u64>,
    pub expires_ns: Option<u64>,
    pub observation_sequence: Option<u64>,
    pub tracking_epoch: Option<u64>,
    pub requested_steering: Option<f64>,
    pub requested_throttle: Option<f64>,
    pub requested_brake: Option<f64>,
    pub applied_steering: Option<f64>,
    pub applied_throttle: Option<f64>,
    pub applied_brake: Option<f64>,
    pub axis_x: Option<i32>,
    pub axis_y: Option<i32>,
    pub axis_z: Option<i32>,
    pub send_success: bool,
    pub mode: String,
    pub reason: String,
    pub validation_ns: Option<u64>,
    pub ipc_ns: Option<u64>,
    pub driver_ns: Option<u64>,
    pub update_interval_ns: Option<u64>,
}
#[derive(Debug, Clone, Serialize)]
pub struct TelemetryRow {
    pub now_ns: u64,
    pub sequence: u64,
    pub segment_id: u64,
    pub tracking_epoch: u64,
    pub observation_ns: u64,
    pub physics_ns: u64,
    pub graphics_ns: u64,
    pub physics_packet_id: i32,
    pub graphics_packet_id: i32,
    pub speed_kmh: f64,
    pub throttle_observed: f64,
    pub brake_observed: f64,
    pub steering_raw: f64,
    pub position_x: f64,
    pub position_y: f64,
    pub position_z: f64,
    pub lap_valid: bool,
    pub in_pit: bool,
    pub state_status: String,
    pub state_reason: String,
    pub gear_raw: i32,
    pub rpm: i32,
    pub path_s_m: Option<f64>,
    pub lateral_error_m: Option<f64>,
    pub heading_error_rad: Option<f64>,
    pub vehicle_heading_rad: Option<f64>,
    pub projection_distance_m: Option<f64>,
    pub accumulated_m: Option<f64>,
    pub reference_elevation_m: Option<f64>,
    pub reference_grade_pct: Option<f64>,
    pub completed_laps: i32,
    pub lap_time_ms: i32,
    pub progress: f64,
    pub sector: i32,
}
#[derive(Debug, Clone, Serialize)]
pub struct ControllerRow {
    pub distance_travelled_m: Option<f64>,
    pub lap_fraction: Option<f64>,
    pub now_ns: u64,
    pub observation_sequence: u64,
    pub observation_ns: u64,
    pub tracking_epoch: u64,
    pub target_speed_kmh: f64,
    pub target_x: Option<f64>,
    pub target_z: Option<f64>,
    pub requested_curvature: Option<f64>,
    pub steering_unclamped: Option<f64>,
    pub reason: String,
    pub abort: bool,
    pub complete: bool,
}
impl ControllerRow {
    pub fn new(now_ns: u64, c: &crate::core::Command, o: &poleposition_baseline::Output) -> Self {
        Self {
            distance_travelled_m: o.distance_travelled_m,
            lap_fraction: o.lap_fraction,
            now_ns,
            observation_sequence: c.observation_sequence,
            observation_ns: c.observation_ns,
            tracking_epoch: c.tracking_epoch,
            target_speed_kmh: o.target_speed_kmh,
            target_x: o.target_x,
            target_z: o.target_z,
            requested_curvature: o.requested_curvature,
            steering_unclamped: o.steering_unclamped,
            reason: o.reason.clone(),
            abort: o.abort,
            complete: o.complete,
        }
    }
}
#[derive(Debug, Clone, Serialize)]
pub struct EventRow {
    pub now_ns: u64,
    pub kind: String,
    pub detail: String,
}
pub enum Message {
    Policy(crate::policy::Diagnostic),
    Controller(ControllerRow),
    Command(CommandRow),
    Telemetry(TelemetryRow),
    Event(EventRow),
}
#[derive(Debug, Default, Serialize)]
pub struct Counts {
    #[serde(skip_serializing_if = "is_zero")]
    pub policy: u64,
    #[serde(skip_serializing_if = "is_zero")]
    pub controller: u64,
    pub commands: u64,
    pub telemetry: u64,
    pub events: u64,
}
fn is_zero(n: &u64) -> bool {
    *n == 0
}
pub struct Sink {
    tx: Option<mpsc::SyncSender<Message>>,
    worker: Option<thread::JoinHandle<Result<Counts>>>,
    pub failed: Arc<AtomicBool>,
    pub error: Arc<Mutex<Option<String>>>,
    pub dropped: u64,
    pub display: Arc<Mutex<String>>,
    shutdown: Arc<AtomicBool>,
    display_worker: Option<thread::JoinHandle<()>>,
}
fn writer(path: &Path) -> Result<csv::Writer<BufWriter<std::fs::File>>> {
    Ok(csv::WriterBuilder::new()
        .has_headers(false)
        .from_writer(BufWriter::new(
            OpenOptions::new().write(true).create_new(true).open(path)?,
        )))
}
pub const COMMAND_FIELDS: &[&str] = &[
    "now_ns",
    "sequence",
    "created_ns",
    "expires_ns",
    "observation_sequence",
    "tracking_epoch",
    "requested_steering",
    "requested_throttle",
    "requested_brake",
    "applied_steering",
    "applied_throttle",
    "applied_brake",
    "axis_x",
    "axis_y",
    "axis_z",
    "send_success",
    "mode",
    "reason",
    "validation_ns",
    "ipc_ns",
    "driver_ns",
    "update_interval_ns",
];
pub const TELEMETRY_FIELDS: &[&str] = &[
    "now_ns",
    "sequence",
    "segment_id",
    "tracking_epoch",
    "observation_ns",
    "physics_ns",
    "graphics_ns",
    "physics_packet_id",
    "graphics_packet_id",
    "speed_kmh",
    "throttle_observed",
    "brake_observed",
    "steering_raw",
    "position_x",
    "position_y",
    "position_z",
    "lap_valid",
    "in_pit",
    "state_status",
    "state_reason",
    "gear_raw",
    "rpm",
    "path_s_m",
    "lateral_error_m",
    "heading_error_rad",
    "vehicle_heading_rad",
    "projection_distance_m",
    "accumulated_m",
    "reference_elevation_m",
    "reference_grade_pct",
    "completed_laps",
    "lap_time_ms",
    "progress",
    "sector",
];
impl Sink {
    pub fn start(path: &Path, capacity: usize, display_hz: Option<f64>) -> Result<Self> {
        Self::start_inner(path, capacity, display_hz, None)
    }
    fn start_inner(
        path: &Path,
        capacity: usize,
        display_hz: Option<f64>,
        fail_after: Option<u64>,
    ) -> Result<Self> {
        if capacity == 0 {
            return Err("Invalid log capacity".into());
        }
        let mut commands = writer(&path.join("commands.csv"))?;
        let mut telemetry = writer(&path.join("telemetry.csv"))?;
        let mut events = writer(&path.join("events.csv"))?;
        let mut controller = writer(&path.join("controller.csv"))?;
        controller.write_record([
            "distance_travelled_m",
            "lap_fraction",
            "now_ns",
            "observation_sequence",
            "observation_ns",
            "tracking_epoch",
            "target_speed_kmh",
            "target_x",
            "target_z",
            "requested_curvature",
            "steering_unclamped",
            "reason",
            "abort",
            "complete",
        ])?;
        controller.flush()?;
        commands.write_record(COMMAND_FIELDS)?;
        telemetry.write_record(TELEMETRY_FIELDS)?;
        events.write_record(["now_ns", "kind", "detail"])?;
        commands.flush()?;
        telemetry.flush()?;
        events.flush()?;
        let (tx, rx) = mpsc::sync_channel(capacity);
        let mut policy = writer(&path.join("policy.csv"))?;
        policy.write_record([
            "now_ns",
            "observation_sequence",
            "observation_ns",
            "tracking_epoch",
            "control_source",
            "policy_reason",
            "features_json",
            "prediction_u",
            "model_throttle",
            "model_brake",
            "pid_throttle",
            "pid_brake",
            "feature_ns",
            "inference_ns",
            "model_elapsed_ns",
        ])?;
        let failed = Arc::new(AtomicBool::new(false));
        let fail = failed.clone();
        let error = Arc::new(Mutex::new(None));
        let err = error.clone();
        let worker = thread::spawn(move || {
            let result = (|| -> Result<Counts> {
                let mut counts = Counts::default();
                let mut flushed = Instant::now();
                while let Ok(row) = rx.recv() {
                    if fail_after.is_some_and(|n| {
                        counts.commands
                            + counts.telemetry
                            + counts.events
                            + counts.controller
                            + counts.policy
                            >= n
                    }) {
                        return Err("Injected storage failure".into());
                    }
                    match row {
                        Message::Policy(v) => {
                            policy.serialize(v)?;
                            counts.policy += 1;
                        }
                        Message::Controller(v) => {
                            controller.serialize(v)?;
                            counts.controller += 1;
                        }
                        Message::Command(v) => {
                            commands.serialize(v)?;
                            counts.commands += 1;
                        }
                        Message::Telemetry(v) => {
                            telemetry.serialize(v)?;
                            counts.telemetry += 1;
                        }
                        Message::Event(v) => {
                            events.serialize(v)?;
                            counts.events += 1;
                        }
                    }
                    if flushed.elapsed() >= Duration::from_millis(250) {
                        commands.flush()?;
                        telemetry.flush()?;
                        events.flush()?;
                        controller.flush()?;
                        policy.flush()?;
                        flushed = Instant::now();
                    }
                }
                commands.flush()?;
                telemetry.flush()?;
                events.flush()?;
                controller.flush()?;
                policy.flush()?;
                policy.get_ref().get_ref().sync_all()?;
                controller.get_ref().get_ref().sync_all()?;
                commands.get_ref().get_ref().sync_all()?;
                telemetry.get_ref().get_ref().sync_all()?;
                events.get_ref().get_ref().sync_all()?;
                Ok(counts)
            })();
            if let Err(e) = &result {
                fail.store(true, Ordering::Release);
                if let Ok(mut slot) = err.lock() {
                    *slot = Some(e.to_string());
                }
            }
            result
        });
        let display = Arc::new(Mutex::new(String::new()));
        let shutdown = Arc::new(AtomicBool::new(false));
        let display_worker = display_hz.map(|hz| {
            let line = display.clone();
            let stop = shutdown.clone();
            thread::spawn(move || {
                while !stop.load(Ordering::Acquire) {
                    if let Ok(value) = line.try_lock()
                        && !value.is_empty()
                    {
                        eprintln!("{value}");
                    }
                    thread::sleep(Duration::from_secs_f64(1. / hz));
                }
            })
        });
        Ok(Self {
            tx: Some(tx),
            worker: Some(worker),
            failed,
            error,
            dropped: 0,
            display,
            shutdown,
            display_worker,
        })
    }
    pub fn push(&mut self, row: Message) -> bool {
        match self.tx.as_ref().unwrap().try_send(row) {
            Ok(()) => true,
            Err(_) => {
                self.dropped += 1;
                self.failed.store(true, Ordering::Release);
                false
            }
        }
    }
    pub fn finish(mut self) -> Result<Counts> {
        self.tx.take();
        self.shutdown.store(true, Ordering::Release);
        if let Some(w) = self.display_worker.take() {
            let _ = w.join();
        }
        self.worker
            .take()
            .unwrap()
            .join()
            .map_err(|_| "Log writer panicked")?
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn temp() -> std::path::PathBuf {
        let p = std::env::temp_dir().join(format!(
            "poleposition-log-{}-{}",
            std::process::id(),
            crate::platform::now_ns()
        ));
        std::fs::create_dir(&p).unwrap();
        p
    }
    #[test]
    fn drain_and_collision() {
        let p = temp();
        let mut s = Sink::start(&p, 64, None).unwrap();
        for n in 0..20 {
            s.push(Message::Event(EventRow {
                now_ns: n,
                kind: "test".into(),
                detail: "x".into(),
            }));
        }
        assert_eq!(s.finish().unwrap().events, 20);
        assert!(Sink::start(&p, 1, None).is_err());
        std::fs::remove_dir_all(p).unwrap();
    }
    #[test]
    fn storage_failure() {
        let p = temp();
        let mut s = Sink::start_inner(&p, 64, None, Some(0)).unwrap();
        s.push(Message::Event(EventRow {
            now_ns: 0,
            kind: "fail".into(),
            detail: String::new(),
        }));
        for _ in 0..100 {
            if s.failed.load(Ordering::Acquire) {
                break;
            }
            thread::sleep(Duration::from_millis(2));
        }
        assert!(s.failed.load(Ordering::Acquire));
        assert!(s.finish().is_err());
        std::fs::remove_dir_all(p).unwrap();
    }
    #[test]
    fn queue_full_is_latched() {
        let p = temp();
        let mut sink = Sink::start(&p, 1, None).unwrap();
        let (tx, _rx) = mpsc::sync_channel(1);
        drop(sink.tx.replace(tx));
        let row = || {
            Message::Event(EventRow {
                now_ns: 1,
                kind: "full".into(),
                detail: String::new(),
            })
        };
        assert!(sink.push(row()));
        assert!(!sink.push(row()));
        assert_eq!(sink.dropped, 1);
        assert!(sink.failed.load(Ordering::Acquire));
        sink.finish().unwrap();
        std::fs::remove_dir_all(p).unwrap();
    }
}
