pub mod decode;
mod memory;
use decode::{GRAPHICS_SIZE, Identity, PHYSICS_SIZE, STATIC_SIZE, Sample};
use memory::Mapping;
use serde::Serialize;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub fn unix_ns() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
}
#[derive(Debug, Clone, Serialize)]
pub struct Frame {
    pub received_unix_ns: u128,
    pub elapsed_ns: u64,
    pub physics_changed_ns: u64,
    pub graphics_changed_ns: u64,
    pub identity: Identity,
    pub sample: Sample,
}
#[derive(Debug)]
pub enum Poll {
    Sample(Frame),
    NoData,
    State(&'static str),
    Error(String),
}
pub trait TelemetrySource {
    fn poll(&mut self) -> Poll;
}
/// Read-only absence check for explicitly confirmed main-menu calibration.
/// Present pages or access errors must never be treated as a session-free menu.
pub fn all_pages_absent() -> std::io::Result<bool> {
    for name in [
        "Local\\acpmf_physics",
        "Local\\acpmf_graphics",
        "Local\\acpmf_static",
    ] {
        match Mapping::open(name, 1) {
            Ok(_) => return Ok(false),
            Err(error) if error.raw_os_error() == Some(2) => (),
            Err(error) => return Err(error),
        }
    }
    Ok(true)
}
fn stable_snapshot<T: PartialEq>(mut copy: impl FnMut() -> T) -> Option<T> {
    for _ in 0..3 {
        let first = copy();
        let second = copy();
        if first == second {
            return Some(second);
        }
    }
    None
}
#[derive(Default)]
pub struct Freshness {
    ids: Option<[i32; 2]>,
    changed: [u64; 2],
    primed: [bool; 2],
}
impl Freshness {
    pub fn update(&mut self, ids: [i32; 2], now: u64, timeout: u64) -> Result<bool, &'static str> {
        let Some(old) = self.ids else {
            self.ids = Some(ids);
            self.changed = [now; 2];
            return Ok(false);
        };
        for i in 0..2 {
            if old[i] != ids[i] {
                self.changed[i] = now;
                self.primed[i] = true;
            } else if now.saturating_sub(self.changed[i]) >= timeout {
                return Err("stale");
            }
        }
        self.ids = Some(ids);
        Ok(self.primed.iter().all(|v| *v) && old != ids)
    }
}
struct Connection {
    p: Mapping,
    g: Mapping,
    s: Mapping,
}
pub struct Reader {
    conn: Option<Connection>,
    start: Instant,
    last_stable: u64,
    fresh: Freshness,
    state: &'static str,
    timeout: u64,
    retry: Duration,
    next_connect: Instant,
}
impl Reader {
    pub fn new(start: Instant, stale: Duration, retry: Duration) -> Self {
        Self {
            conn: None,
            start,
            last_stable: 0,
            fresh: Freshness::default(),
            state: "initial",
            timeout: stale.as_nanos() as u64,
            retry,
            next_connect: Instant::now(),
        }
    }
    fn state(&mut self, state: &'static str) -> Poll {
        if self.state == state {
            Poll::NoData
        } else {
            self.state = state;
            Poll::State(state)
        }
    }
    fn disconnect(&mut self, state: &'static str) -> Poll {
        self.conn = None;
        self.fresh = Freshness::default();
        self.next_connect = Instant::now() + self.retry;
        self.state(state)
    }
    pub fn close(&mut self) {
        self.conn = None;
    }
    /// Status snapshot time also refreshes when a paused/off transition is unchanged.
    pub fn observed_game_state(&self) -> (&'static str, u64) {
        (self.state, self.last_stable)
    }
}
impl TelemetrySource for Reader {
    fn poll(&mut self) -> Poll {
        if self.conn.is_none() {
            if Instant::now() < self.next_connect {
                return Poll::NoData;
            }
            let opened = (|| {
                Ok::<_, std::io::Error>(Connection {
                    p: Mapping::open("Local\\acpmf_physics", PHYSICS_SIZE)?,
                    g: Mapping::open("Local\\acpmf_graphics", GRAPHICS_SIZE)?,
                    s: Mapping::open("Local\\acpmf_static", STATIC_SIZE)?,
                })
            })();
            match opened {
                Ok(c) => {
                    self.conn = Some(c);
                    self.last_stable = self.start.elapsed().as_nanos() as u64;
                }
                Err(e) => {
                    let event = self.disconnect("disconnected");
                    return if e.raw_os_error() == Some(2) {
                        event
                    } else {
                        Poll::Error(e.to_string())
                    };
                }
            }
        }
        let c = self.conn.as_ref().unwrap();
        let snapshot = stable_snapshot(|| {
            let mut p = [0; PHYSICS_SIZE];
            let mut g = [0; GRAPHICS_SIZE];
            let mut s = [0; STATIC_SIZE];
            c.p.copy(&mut p);
            c.g.copy(&mut g);
            c.s.copy(&mut s);
            (p, g, s)
        });
        if let Some((p, g, s)) = snapshot {
            let now = self.start.elapsed().as_nanos() as u64;
            self.last_stable = now;
            let status = decode::int(&g, 4);
            if status != 2 {
                return self.disconnect(match status {
                    0 => "off",
                    1 => "replay",
                    3 => "paused",
                    _ => "unknown_status",
                });
            }
            let sample = match decode::decode(&p, &g) {
                Ok(v) => v,
                Err(e) => {
                    self.disconnect("invalid");
                    return Poll::Error(e.into());
                }
            };
            let id = decode::identity(&s).unwrap();
            match self.fresh.update(
                [sample.physics_packet_id, sample.graphics_packet_id],
                now,
                self.timeout,
            ) {
                Err(_) => return self.disconnect("stale"),
                Ok(false) => return Poll::NoData,
                Ok(true) => (),
            }
            if self.state != "live" {
                return self.state("live");
            }
            return Poll::Sample(Frame {
                received_unix_ns: unix_ns(),
                elapsed_ns: now,
                physics_changed_ns: self.fresh.changed[0],
                graphics_changed_ns: self.fresh.changed[1],
                identity: id,
                sample,
            });
        }
        if self.start.elapsed().as_nanos() as u64 - self.last_stable >= self.timeout {
            self.disconnect("unstable")
        } else {
            Poll::NoData
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unstable_copy_retries_are_bounded() {
        let mut n = 0;
        assert!(
            stable_snapshot(|| {
                n += 1;
                n
            })
            .is_none()
        );
        assert_eq!(n, 6);
        let mut values = [1, 2, 3, 3].into_iter();
        assert_eq!(stable_snapshot(|| values.next().unwrap()), Some(3));
    }
    #[test]
    fn freshness_both_duplicates_stale_wrap() {
        let mut f = Freshness::default();
        assert!(!f.update([0, 0], 0, 100).unwrap());
        assert!(!f.update([1, 0], 1, 100).unwrap());
        assert!(f.update([1, 1], 2, 100).unwrap());
        assert!(!f.update([1, 1], 3, 100).unwrap());
        assert!(f.update([2, 1], 102, 100).is_err());
        let mut f = Freshness::default();
        f.update([i32::MAX; 2], 0, 100).unwrap();
        assert!(f.update([i32::MIN; 2], 1, 100).unwrap());
    }
}
