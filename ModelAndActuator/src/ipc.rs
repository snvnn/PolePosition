//! Anonymous local pipes; both ends consume a single latest-value slot, not a backlog.
use crate::{
    Axes, Config, Result,
    core::{Command, Health},
    platform::now_ns,
};
use serde::{Deserialize, Serialize};
use std::{
    io::{BufRead, BufReader, Write},
    process::{Child, Command as Process, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::Duration,
};
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Context {
    #[serde(default)]
    pub path: Option<poleposition_baseline::Observation>,
    pub health: Health,
    pub armed_ns: Option<u64>,
    pub sent_ns: u64,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Reply {
    #[serde(default)]
    pub policy: Option<crate::policy::Diagnostic>,
    #[serde(default)]
    pub controller: Option<poleposition_baseline::Output>,
    pub command: Command,
    pub scenario_complete: bool,
}
pub struct Latest<T> {
    value: Mutex<Option<T>>,
    pub closed: AtomicBool,
    pub fault: AtomicBool,
}
impl<T> Default for Latest<T> {
    fn default() -> Self {
        Self {
            value: Mutex::new(None),
            closed: AtomicBool::new(false),
            fault: AtomicBool::new(false),
        }
    }
}
impl<T> Latest<T> {
    pub fn put(&self, value: T) {
        if let Ok(mut slot) = self.value.lock() {
            *slot = Some(value);
        } else {
            self.fault.store(true, Ordering::Release);
        }
    }
    pub fn take(&self) -> Option<T> {
        self.value.try_lock().ok().and_then(|mut v| v.take())
    }
}
fn read_latest<T: serde::de::DeserializeOwned + Send + 'static>(
    stream: impl std::io::Read + Send + 'static,
    slot: Arc<Latest<T>>,
) {
    thread::spawn(move || {
        let mut reader = BufReader::new(stream);
        loop {
            let mut bytes = Vec::new();
            // Bound malformed packets even if a producer writes without a newline.
            use std::io::Read;
            match reader.by_ref().take(8193).read_until(b'\n', &mut bytes) {
                Ok(0) => break,
                Ok(n) if n <= 8192 && bytes.last() == Some(&b'\n') => {
                    match serde_json::from_slice(&bytes) {
                        Ok(v) => slot.put(v),
                        Err(_) => {
                            slot.fault.store(true, Ordering::Release);
                            break;
                        }
                    }
                }
                _ => {
                    slot.fault.store(true, Ordering::Release);
                    break;
                }
            }
        }
        slot.closed.store(true, Ordering::Release);
    });
}
pub struct Producer {
    pub child: Child,
    pub replies: Arc<Latest<Reply>>,
    contexts: Arc<Latest<Context>>,
    shutdown: Arc<AtomicBool>,
}
impl Producer {
    pub fn spawn(
        executable: &std::path::Path,
        config: &std::path::Path,
        scenario: &str,
    ) -> Result<Self> {
        use std::os::windows::process::CommandExt;
        let mut child = Process::new(executable)
            .args(["worker", "--config-json"])
            .arg(config)
            .args(["--scenario", scenario])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .creation_flags(0x08000000 | 0x00000200)
            .spawn()?;
        let replies = Arc::new(Latest::default());
        read_latest(
            child.stdout.take().ok_or("Missing child stdout")?,
            replies.clone(),
        );
        let contexts: Arc<Latest<Context>> = Arc::new(Latest::default());
        let slot = contexts.clone();
        let shutdown = Arc::new(AtomicBool::new(false));
        let stop = shutdown.clone();
        let mut input = child.stdin.take().ok_or("Missing child stdin")?;
        thread::spawn(move || {
            while !stop.load(Ordering::Acquire) {
                if let Some(c) = slot.take()
                    && (serde_json::to_writer(&mut input, &c).is_err()
                        || input.write_all(b"\n").is_err()
                        || input.flush().is_err())
                {
                    slot.fault.store(true, Ordering::Release);
                    break;
                }
                thread::sleep(Duration::from_millis(1));
            }
            slot.closed.store(true, Ordering::Release);
        });
        Ok(Self {
            child,
            replies,
            contexts,
            shutdown,
        })
    }
    pub fn publish(&self, context: Context) {
        self.contexts.put(context);
    }
    pub fn take_reply(&self) -> Option<(Reply, u64)> {
        // Capture receipt time after taking the message. The worker can produce a
        // fresh command while the guardian is checking the game process.
        self.replies.take().map(|reply| (reply, now_ns()))
    }
    pub fn disconnect(&self) {
        self.shutdown.store(true, Ordering::Release);
    }
    pub fn lost(&mut self) -> bool {
        self.replies.closed.load(Ordering::Acquire)
            || self.replies.fault.load(Ordering::Acquire)
            || self.contexts.fault.load(Ordering::Acquire)
            || self.child.try_wait().map_or(true, |v| v.is_some())
    }
}
impl Drop for Producer {
    fn drop(&mut self) {
        self.shutdown.store(true, Ordering::Release);
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
pub struct Scenario {
    kind: String,
    phase: u32,
    phase_start: Option<u64>,
    stop_since: Option<u64>,
}
impl Scenario {
    pub fn new(kind: &str) -> Result<Self> {
        if !["stationary", "crawl", "brake-hold"].contains(&kind) {
            return Err("Unknown scenario".into());
        }
        Ok(Self {
            kind: kind.into(),
            phase: 0,
            phase_start: None,
            stop_since: None,
        })
    }
    pub fn update(&mut self, now: u64, c: &Context) -> (Axes, bool) {
        let Some(armed) = c.armed_ns else {
            return (Axes::STOP, false);
        };
        if self.kind == "brake-hold" {
            // Diagnostic hold: the guardian's existing time limit and all stop
            // gates remain authoritative. Never add throttle or steering.
            return (Axes::STOP, false);
        }
        if self.kind == "stationary" {
            let t = now.saturating_sub(armed) as f64 / 1e9;
            let steering = if (1.0..3.0).contains(&t) {
                -0.1
            } else if (5.0..7.0).contains(&t) {
                0.1
            } else {
                0.
            };
            return (
                Axes {
                    steering,
                    ..Axes::STOP
                },
                t >= 9.,
            );
        }
        if c.health.gear_raw < 2 {
            // The user selects first gear while this scenario holds the brake.
            // Neutral/reverse must not silently count as a driving trial.
            self.phase_start = None;
            self.stop_since = None;
            return (Axes::STOP, false);
        }
        let start = *self.phase_start.get_or_insert(now);
        let t = now.saturating_sub(start) as f64 / 1e9;
        let stop_phase = self.phase.is_multiple_of(2);
        if stop_phase {
            if c.health.live && c.health.speed_kmh < 1. {
                self.stop_since.get_or_insert(c.health.observation_ns);
            } else {
                self.stop_since = None;
            }
            if t >= 1.
                && self
                    .stop_since
                    .is_some_and(|s| c.health.observation_ns.saturating_sub(s) >= 500_000_000)
            {
                if self.phase >= 6 {
                    return (Axes::STOP, true);
                }
                self.phase += 1;
                self.phase_start = Some(now);
                self.stop_since = None;
            }
            (Axes::STOP, false)
        } else {
            if t >= 2. {
                self.phase += 1;
                self.phase_start = Some(now);
                return (Axes::STOP, false);
            }
            (
                Axes {
                    steering: match self.phase {
                        3 => -0.1,
                        5 => 0.1,
                        _ => 0.,
                    },
                    throttle: if self.phase == 1 { 0.15 } else { 0.10 },
                    brake: 0.,
                },
                false,
            )
        }
    }
}
pub fn worker(config: Config, scenario: &str) -> Result<()> {
    let mut policy = if let Some(policy_config) = &config.policy {
        let demo = config.baseline.as_ref().ok_or("Policy requires baseline")?;
        let prepared =
            poleposition_state::model::Prepared::load(std::path::Path::new(&demo.prepared))?;
        Some(crate::policy::Runtime::new(
            policy_config.clone(),
            policy_config.verify(&prepared.path)?,
            prepared.model,
        )?)
    } else {
        None
    };
    let mut controller = if scenario == "demo" {
        let demo = config.baseline.as_ref().ok_or("Missing baseline config")?;
        let prepared =
            poleposition_state::model::Prepared::load(std::path::Path::new(&demo.prepared))?;
        Some(poleposition_baseline::Controller::new(
            prepared.model,
            demo.config.clone(),
        )?)
    } else {
        None
    };
    let mut generator = if controller.is_none() {
        Some(Scenario::new(scenario)?)
    } else {
        None
    };
    let contexts = Arc::new(Latest::<Context>::default());
    read_latest(std::io::stdin(), contexts.clone());
    let mut current = None;
    let mut sequence = 0u64;
    let mut output = std::io::stdout().lock();
    // Policy history follows fresh guardian contexts. Sleeping another full
    // 10 ms after computation decimates 100 Hz observations through phase drift.
    // Still consume only the latest context: never replay a command backlog.
    let follow_observations = config.policy.is_some();
    while !contexts.closed.load(Ordering::Acquire) && !contexts.fault.load(Ordering::Acquire) {
        if follow_observations {
            current = contexts.take();
        } else if let Some(c) = contexts.take() {
            current = Some(c);
        }
        let now = now_ns();
        if let Some(c) = &current
            && c.sent_ns <= now
            && now - c.sent_ns < config.age_ns()
        {
            let mut diagnostic = controller
                .as_mut()
                .map(|v| v.update(c.path.as_ref(), now, c.armed_ns));
            let mut policy_diagnostic = None;
            if let Some(policy) = &mut policy
                && let Some(path) = &c.path
                && let Some(pid) = &diagnostic
            {
                let (out, detail) = policy.update(path, c.health.rpm as f64, pid, now, c.armed_ns);
                diagnostic = Some(out);
                policy_diagnostic = Some(detail);
            }
            let (axes, complete) = if let Some(out) = &diagnostic {
                (
                    Axes {
                        steering: out.steering,
                        throttle: out.throttle,
                        brake: out.brake,
                    },
                    out.complete,
                )
            } else {
                generator.as_mut().unwrap().update(now, c)
            };
            sequence += 1;
            let cmd = Command {
                sequence,
                created_ns: now,
                expires_ns: now + config.age_ns(),
                observation_sequence: c.health.observation_sequence,
                observation_ns: c.health.observation_ns,
                tracking_epoch: c.health.tracking_epoch,
                steering: axes.steering,
                throttle: axes.throttle,
                brake: axes.brake,
            };
            serde_json::to_writer(
                &mut output,
                &Reply {
                    policy: policy_diagnostic,
                    controller: diagnostic,
                    command: cmd,
                    scenario_complete: complete,
                },
            )?;
            output.write_all(b"\n")?;
            output.flush()?;
        }
        thread::sleep(if follow_observations {
            Duration::from_millis(1)
        } else {
            Duration::from_secs_f64(1. / config.poll_hz)
        });
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn brake_hold_never_generates_steering_or_throttle() {
        let mut scenario = Scenario::new("brake-hold").unwrap();
        let mut context = Context {
            path: None,
            health: Health::default(),
            armed_ns: None,
            sent_ns: 0,
        };
        assert_eq!(scenario.update(0, &context), (Axes::STOP, false));
        context.armed_ns = Some(0);
        for time in [500_000_000, 2_000_000_000, 9_000_000_000, 30_000_000_000] {
            assert_eq!(scenario.update(time, &context), (Axes::STOP, false));
        }
    }
    #[test]
    fn latest_wins() {
        let slot = Latest::default();
        slot.put(1);
        slot.put(2);
        assert_eq!(slot.take(), Some(2));
        assert_eq!(slot.take(), None);
    }
    #[test]
    fn malformed_pipe_fails() {
        let slot = Arc::new(Latest::<Reply>::default());
        read_latest(std::io::Cursor::new(b"invalid\n"), slot.clone());
        for _ in 0..100 {
            if slot.closed.load(Ordering::Acquire) {
                break;
            }
            thread::sleep(Duration::from_millis(2));
        }
        assert!(slot.fault.load(Ordering::Acquire));
    }
    #[test]
    fn crawl_waits_for_stop() {
        let mut s = Scenario::new("crawl").unwrap();
        let mut c = Context {
            path: None,
            health: Health {
                speed_kmh: 5.,
                gear_raw: 2,
                live: true,
                ..Default::default()
            },
            armed_ns: Some(0),
            sent_ns: 0,
        };
        for t in [0, 1_000_000_000, 2_000_000_000] {
            c.health.observation_ns = t;
            assert_eq!(s.update(t, &c), (Axes::STOP, false));
        }
        assert_eq!(s.phase, 0);
    }
    #[test]
    fn crawl_holds_brake_until_forward_gear() {
        let mut s = Scenario::new("crawl").unwrap();
        let mut c = Context {
            path: None,
            health: Health {
                live: true,
                gear_raw: 1,
                ..Default::default()
            },
            armed_ns: Some(0),
            sent_ns: 0,
        };
        for gear in [0, 1] {
            c.health.gear_raw = gear;
            for t in [0, 1_000_000_000, 20_000_000_000] {
                c.health.observation_ns = t;
                assert_eq!(s.update(t, &c), (Axes::STOP, false));
                assert_eq!(s.phase, 0);
            }
        }
        c.health.gear_raw = 2;
        for t in [21_000_000_000, 22_000_000_000] {
            c.health.observation_ns = t;
            assert_eq!(s.update(t, &c), (Axes::STOP, false));
        }
        assert_eq!(s.phase, 1);
        c.health.observation_ns = 22_010_000_000;
        assert!(s.update(22_010_000_000, &c).0.throttle > 0.);
        c.health.gear_raw = 1;
        assert_eq!(s.update(22_020_000_000, &c), (Axes::STOP, false));
    }
}
