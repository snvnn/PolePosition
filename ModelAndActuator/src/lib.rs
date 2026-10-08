pub mod binding;
#[cfg(windows)] pub mod gamepad;
pub mod core;
pub mod ipc;
pub mod logger;
pub mod platform;
pub mod policy;
pub mod profile;
pub mod rl;
pub mod runner;
pub mod simulation;
pub mod v2_shadow;
pub use poleposition_vjoy::{Axes, Result};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    #[serde(default)]
    pub reinforcement: Option<rl::Config>,
    #[serde(default)]
    pub v2_shadow: Option<v2_shadow::Config>,
    #[serde(default)]
    pub policy: Option<policy::Config>,
    #[serde(default)]
    pub baseline: Option<Demo>,
    pub output_dir: String,
    pub profile: String,
    pub dll: String,
    pub device_id: u32,
    pub poll_hz: f64,
    pub max_age_ms: f64,
    pub start_key: String,
    pub stop_key: String,
    pub stopped_kmh: f64,
    pub stopped_seconds: f64,
    pub max_speed_kmh: f64,
    pub max_steering: f64,
    pub max_throttle: f64,
    pub steering_rate: f64,
    pub throttle_rate: f64,
    pub brake_rate: f64,
    pub test_seconds: f64,
    pub arm_wait_seconds: f64,
    pub bind_seconds: f64,
    pub display_hz: f64,
    pub queue_capacity: usize,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Demo {
    pub prepared: String,
    pub config: poleposition_baseline::Config,
}
impl Config {
    pub fn load(path: &std::path::Path) -> Result<Self> {
        let c: Self = toml::from_str(&std::fs::read_to_string(path)?)?;
        c.validate()?;
        Ok(c)
    }
    pub fn validate(&self) -> Result<()> {
        if let Some(rl) = &self.reinforcement {
            rl.validate()?;
            let b = self
                .baseline
                .as_ref()
                .ok_or("RL needs baseline preparation")?;
            if self.policy.is_some()
                || self.v2_shadow.is_some()
                || b.config.single_lap
                || b.config.target_speed_kmh != 10.
                || b.config.max_brake > 0.6
                || self.poll_hz != 100.
                || self.test_seconds != rl.seconds + 30.
            {
                return Err("Invalid separate reinforcement envelope".into());
            }
        }
        if let Some(v2) = &self.v2_shadow {
            let baseline = self
                .baseline
                .as_ref()
                .ok_or("V2 shadow requires baseline")?;
            if v2.training.is_empty()
                || self.policy.is_some()
                || baseline.config.single_lap
                || baseline.config.target_speed_kmh != 10.
                || baseline.config.max_brake > 0.60
                || self.poll_hz != 100.
            {
                return Err("V2 shadow requires separate short 10km/h, 100Hz profile".into());
            }
        }
        if let Some(policy) = &self.policy {
            policy.validate()?;
            let baseline = self
                .baseline
                .as_ref()
                .ok_or("Policy requires baseline geometry/start/stop")?;
            if baseline.config.single_lap
                || baseline.config.target_speed_kmh != 10.
                || baseline.config.max_brake > 0.60
            {
                return Err("Policy exceeds short 10km/h envelope".into());
            }
        }
        let single_lap = self.baseline.as_ref().is_some_and(|d| d.config.single_lap);
        if let Some(demo) = &self.baseline {
            demo.config.validate()?;
            if demo.prepared.is_empty()
                || demo.config.max_steering > self.max_steering
                || demo.config.max_throttle > self.max_throttle
                || demo.config.max_age_ms > self.max_age_ms
                || demo.config.max_speed_kmh > self.max_speed_kmh
                || demo.config.duration_s > self.test_seconds
            {
                return Err("Controller exceeds guardian envelope".into());
            }
        }
        for v in [
            self.poll_hz,
            self.max_age_ms,
            self.stopped_kmh,
            self.stopped_seconds,
            self.max_speed_kmh,
            self.max_steering,
            self.max_throttle,
            self.steering_rate,
            self.throttle_rate,
            self.brake_rate,
            self.test_seconds,
            self.arm_wait_seconds,
            self.bind_seconds,
            self.display_hz,
        ] {
            if !v.is_finite() || v <= 0. {
                return Err("Invalid positive configuration".into());
            }
        }
        if self.device_id != 1
            || self.start_key != "F7"
            || self.stop_key != "F8"
            || self.max_age_ms > 100.
            || self.poll_hz > 1000.
            || self.poll_hz < 20.
            || self.display_hz > self.poll_hz
            || self.max_speed_kmh > 20.
            || self.max_steering > 1.
            || self.max_throttle > if self.reinforcement.as_ref().is_some_and(|r| r.unrestricted_motion) { 1. } else { 0.20 }
            || self.test_seconds
                > if self.reinforcement.is_some() {
                    1830.
                } else if single_lap {
                    3600.
                } else {
                    30.
                }
            || self.bind_seconds > 15.
            || self.stopped_kmh > 1.
            || self.stopped_seconds < 0.5
            || self.queue_capacity == 0
            || self.queue_capacity > 1_000_000
            || self.output_dir.is_empty()
        {
            return Err("Configuration exceeds v1 test envelope".into());
        }
        Ok(())
    }
    pub fn age_ns(&self) -> u64 {
        (self.max_age_ms * 1e6) as u64
    }
}
