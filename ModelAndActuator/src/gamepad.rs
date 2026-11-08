//! Separate gamepad execution. It reuses the safety guardian, never wheel PID output.
use crate::{
    Axes, Result,
    core::{Action, Command, Guard, Health, Mode, Signals},
    platform,
};
use poleposition_gamepad::{
    native::{VirtualPad, read},
    profile::Profile,
};
use poleposition_policy::{
    Frame,
    features_v2::{FeaturesV2, Input, SpeedProfile},
    gamepad::Network,
};
use poleposition_recorder::session::Tracker;
use poleposition_state::{
    model::{Prepared, hash, json},
    processor::Processor,
};
use poleposition_telemetry::{Poll, Reader, TelemetrySource};
use serde::Deserialize;
use serde_json::{Value, json as value};
use std::{
    io::Write,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};

// Processing time must be read AFTER the source has timestamped its observation.
// Do not clamp future timestamps: real clock/order faults must still be rejected.
fn poll_with_time(source: &mut impl TelemetrySource, clock: impl FnOnce() -> u64) -> (Poll, u64) {
    let update = source.poll();
    (update, clock())
}

fn continuous_stop_health(health: &mut Health) {
    // Track projection is needed to drive, not to measure a stopped car in gravel.
    // Keep raw live/freshness checks and identity; never treat paused data as a stop.
    health.game_halted = false;
    if !health.identity_ok { health.live = false; }
}

#[derive(Default)]
struct DriveContinuity { epoch: Option<u64>, broken: bool }
impl DriveContinuity {
    fn arm(&mut self, epoch: u64) { self.epoch.get_or_insert(epoch); }
    fn observe(&mut self, epoch: u64, ready: bool) {
        if self.epoch.is_some_and(|initial| initial != epoch || !ready) { self.broken = true; }
    }
    fn confirms_stop(&self, standstill: bool) -> bool {
        standstill && self.epoch.is_some() && !self.broken
    }
}

fn stationary_response_ready(rpm:i32,gear:i32,speed:f64)->bool {
    rpm==0 && gear==1 && speed.is_finite() && (0.0..=1.0).contains(&speed)
}

mod config;
mod diagnostics;
mod drive;
mod logging;
mod supervisor;

use config::{Config, inventory};
use drive::run_shared;
use logging::Log;
pub use diagnostics::{bench, intervention_bench, response};
pub use drive::run;
pub use supervisor::supervise;

#[cfg(test)]
#[path = "gamepad/tests.rs"]
mod response_tests;
