use poleposition_telemetry::{Frame, unix_ns};
use serde::Serialize;
use std::{
    fs, io,
    path::{Path, PathBuf},
};

pub fn create_session(root: &Path) -> io::Result<(String, PathBuf)> {
    fs::create_dir_all(root)?;
    for counter in 0..1000 {
        let id = format!("{}-{}-{counter}", unix_ns(), std::process::id());
        let path = root.join(&id);
        match fs::create_dir(&path) {
            Ok(()) => return Ok((id, path)),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(e),
        }
    }
    Err(io::Error::new(
        io::ErrorKind::AlreadyExists,
        "Cannot allocate unique session",
    ))
}

#[derive(Debug, Clone, Serialize)]
pub struct Event {
    pub elapsed_ns: u64,
    pub segment_id: u64,
    pub kind: String,
    pub detail: String,
}
#[derive(Debug, Clone)]
pub struct Row {
    pub sequence: u64,
    pub segment_id: u64,
    pub interval_ns: u64,
    pub queue_drops: u64,
    pub frame: Frame,
}
pub struct Tracker {
    pub segment: u64,
    previous: Option<Frame>,
    last_received: Option<u64>,
    pending: Option<String>,
    margin: f64,
    timer_reset: Option<(i32, u64)>,
    last_boundary: Option<u64>,
}
impl Tracker {
    pub fn new(margin: f64) -> Self {
        Self {
            segment: 0,
            previous: None,
            last_received: None,
            pending: Some("recording_start".into()),
            margin,
            timer_reset: None,
            last_boundary: None,
        }
    }
    pub fn interrupt(&mut self, reason: &str) {
        self.pending = Some(reason.into());
        self.previous = None;
        self.timer_reset = None;
        self.last_boundary = None;
    }
    pub fn accept(&mut self, f: Frame, sequence: u64, drops: u64) -> (Row, Vec<Event>) {
        let mut events = Vec::new();
        let mut reason = self.pending.take();
        if let Some(p) = &self.previous {
            let a = &p.sample;
            let b = &f.sample;
            if p.identity != f.identity
                || a.session_type != b.session_type
                || a.session_index != b.session_index
            {
                reason = Some("session_change".into());
            } else if b.completed_laps < a.completed_laps {
                reason = Some("lap_counter_reset".into());
            } else if b.completed_laps > a.completed_laps {
                if b.completed_laps == a.completed_laps + 1
                    && (a.progress > 0.8 || b.progress < 0.2)
                {
                    events.push(Event {
                        elapsed_ns: f.elapsed_ns,
                        segment_id: self.segment,
                        kind: "lap_boundary".into(),
                        detail: format!("completed_laps={}", b.completed_laps),
                    });
                } else {
                    reason = Some("lap_counter_jump".into());
                }
            }
            let distance = ((b.position_x - a.position_x) as f64)
                .hypot((b.position_y - a.position_y) as f64)
                .hypot((b.position_z - a.position_z) as f64);
            let dt = f.elapsed_ns.saturating_sub(p.elapsed_ns) as f64 / 1e9;
            let bound = f64::from(a.speed_kmh.max(b.speed_kmh)) / 3.6 * dt * 2.0 + self.margin;
            if distance > bound {
                reason = Some("suspected_teleport".into());
            }
            if b.lap_time_ms + 2000 < a.lap_time_ms && b.completed_laps == a.completed_laps {
                // ACC hotlap timer can reset before completedLaps advances.
                if a.progress > 0.8 || b.progress < 0.2 {
                    self.timer_reset = Some((a.completed_laps, f.elapsed_ns));
                } else {
                    reason = Some("lap_timer_reset".into());
                }
            }
            if let Some((lap, at)) = self.timer_reset {
                if a.progress > 0.8 && b.progress < 0.2 {
                    // Invalid hotlaps can cross the line without completedLaps advancing.
                    // Require both wrap and a recent lap timer reset, not progress alone.
                    events.push(Event {
                        elapsed_ns: f.elapsed_ns,
                        segment_id: self.segment,
                        kind: "lap_boundary".into(),
                        detail: format!("timer_and_progress;completed_laps={}", b.completed_laps),
                    });
                    self.timer_reset = None;
                } else if b.completed_laps != lap {
                    self.timer_reset = None;
                } else if f.elapsed_ns.saturating_sub(at) >= 1_000_000_000 {
                    reason = Some("unconfirmed_lap_timer_reset".into());
                    self.timer_reset = None;
                }
            }
        }
        if let Some(reason) = reason {
            self.timer_reset = None;
            self.last_boundary = None;
            self.segment += 1;
            events.clear();
            events.push(Event {
                elapsed_ns: f.elapsed_ns,
                segment_id: self.segment,
                kind: "segment_start".into(),
                detail: reason,
            });
        } else if !events.is_empty() {
            if self
                .last_boundary
                .is_some_and(|t| f.elapsed_ns.saturating_sub(t) < 5_000_000_000)
            {
                events.clear();
            } else {
                events.truncate(1);
                self.last_boundary = Some(f.elapsed_ns);
            }
        }
        let interval_ns = self
            .last_received
            .map_or(0, |t| f.elapsed_ns.saturating_sub(t));
        self.last_received = Some(f.elapsed_ns);
        self.previous = Some(f.clone());
        (
            Row {
                sequence,
                segment_id: self.segment,
                interval_ns,
                queue_drops: drops,
                frame: f,
            },
            events,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use poleposition_telemetry::decode::{Identity, Sample};
    pub fn frame() -> Frame {
        Frame {
            received_unix_ns: 1,
            elapsed_ns: 1,
            physics_changed_ns: 1,
            graphics_changed_ns: 1,
            identity: Identity {
                sm_version: "1".into(),
                game_version: "1".into(),
                car: "test".into(),
                track: "test".into(),
            },
            sample: Sample {
                physics_packet_id: 1,
                graphics_packet_id: 1,
                speed_kmh: 0.0,
                rpm: 0,
                gear_raw: 1,
                throttle: 0.0,
                brake: 0.0,
                steering_raw: 0.0,
                position_x: 0.0,
                position_y: 0.0,
                position_z: 0.0,
                heading_rad: 0.0,
                completed_laps: 0,
                lap_time_ms: 5000,
                progress: 0.99,
                sector: 2,
                lap_valid: true,
                session_type: 0,
                session_index: 0,
                in_pit: false,
                fuel_litres: 20.0,
                clock_seconds: 43200.0,
                rain_intensity: 0,
            },
        }
    }
    #[test]
    fn wrap_is_not_teleport() {
        let mut t = Tracker::new(30.0);
        let f = frame();
        t.accept(f.clone(), 1, 0);
        let mut g = f;
        g.elapsed_ns += 10_000_000;
        g.sample.progress = 0.01;
        g.sample.completed_laps = 1;
        g.sample.lap_time_ms = 100;
        let (r, e) = t.accept(g, 2, 0);
        assert_eq!(r.segment_id, 1);
        assert_eq!(e[0].kind, "lap_boundary");
    }
    #[test]
    fn hotlap_timer_precedes_lap_counter() {
        let mut tracker = Tracker::new(30.0);
        let f = frame();
        tracker.accept(f.clone(), 1, 0);
        let mut next = f;
        next.elapsed_ns += 10_000_000;
        next.sample.progress = 0.001;
        next.sample.lap_time_ms = 0;
        let (row, events) = tracker.accept(next.clone(), 2, 0);
        assert_eq!(row.segment_id, 1);
        assert_eq!(events[0].kind, "lap_boundary");
        next.elapsed_ns += 20_000_000;
        next.sample.completed_laps += 1;
        let (row, events) = tracker.accept(next, 3, 0);
        assert_eq!(row.segment_id, 1);
        assert!(events.is_empty());
    }
    #[test]
    fn invalid_hotlap_can_repeat_with_same_counter() {
        let mut tracker = Tracker::new(30.0);
        let mut f = frame();
        f.sample.lap_valid = false;
        tracker.accept(f.clone(), 1, 0);
        f.elapsed_ns += 10_000_000;
        f.sample.progress = 0.001;
        f.sample.lap_time_ms = 0;
        assert_eq!(tracker.accept(f.clone(), 2, 0).1[0].kind, "lap_boundary");
        f.elapsed_ns += 90_000_000_000;
        f.sample.progress = 0.99;
        f.sample.lap_time_ms = 90000;
        tracker.accept(f.clone(), 3, 0);
        f.elapsed_ns += 10_000_000;
        f.sample.progress = 0.001;
        f.sample.lap_time_ms = 0;
        let (r, e) = tracker.accept(f, 4, 0);
        assert_eq!(r.segment_id, 1);
        assert_eq!(e[0].kind, "lap_boundary");
    }
    #[test]
    fn resets_and_interrupts_start_segments() {
        let mut t = Tracker::new(30.0);
        let f = frame();
        t.accept(f.clone(), 1, 0);
        let mut g = f.clone();
        g.elapsed_ns += 10_000_000;
        g.sample.position_x = 200.0;
        let (r, e) = t.accept(g, 2, 0);
        assert_eq!(r.segment_id, 2);
        assert_eq!(e[0].detail, "suspected_teleport");
        t.interrupt("paused");
        assert_eq!(t.accept(f, 3, 0).0.segment_id, 3);
    }
    #[test]
    fn unique_directories() {
        let root = std::env::temp_dir().join(format!("pp-session-{}", unix_ns()));
        let (_, a) = create_session(&root).unwrap();
        let (_, b) = create_session(&root).unwrap();
        assert_ne!(a, b);
        fs::remove_dir_all(root).unwrap();
    }
}
