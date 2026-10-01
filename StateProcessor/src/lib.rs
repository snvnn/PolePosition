pub mod model;
pub mod processor;
pub mod runner;
pub mod writer;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub output_dir: String,
    pub poll_hz: f64,
    pub max_age_ms: f64,
    pub max_projection_m: f64,
    pub acquisition_samples: usize,
    pub branch_separation_m: f64,
    pub ambiguity_m: f64,
    pub movement_multiplier: f64,
    pub movement_margin_m: f64,
    pub curvature_offsets_m: Vec<f64>,
    pub display_hz: f64,
    pub queue_capacity: usize,
    pub stale_seconds: f64,
    pub retry_seconds: f64,
    pub teleport_margin_m: f64,
    pub heading_window_ms: f64,
    pub heading_center_spacing_ms: f64,
    pub heading_min_speed_kmh: f64,
    pub heading_max_turn_deg: f64,
    pub heading_min_windows: usize,
    pub heading_max_median_deg: f64,
    pub heading_max_p95_deg: f64,
    pub heading_min_runner_gap_deg: f64,
}
impl Config {
    pub fn validate(&self) -> Result<(), String> {
        if self.output_dir.is_empty()
            || self.acquisition_samples == 0
            || self.queue_capacity == 0
            || self.queue_capacity > 1_000_000
            || self.heading_min_windows == 0
            || self.poll_hz > 1000.0
            || self.display_hz > self.poll_hz
        {
            return Err("Invalid capacity/rate".into());
        }
        for x in [
            self.poll_hz,
            self.max_age_ms,
            self.max_projection_m,
            self.branch_separation_m,
            self.ambiguity_m,
            self.movement_multiplier,
            self.movement_margin_m,
            self.display_hz,
            self.stale_seconds,
            self.retry_seconds,
            self.teleport_margin_m,
            self.heading_window_ms,
            self.heading_center_spacing_ms,
            self.heading_min_speed_kmh,
            self.heading_max_turn_deg,
            self.heading_max_median_deg,
            self.heading_max_p95_deg,
            self.heading_min_runner_gap_deg,
        ] {
            if !x.is_finite() || x <= 0.0 {
                return Err("Invalid numeric configuration".into());
            }
        }
        if self.curvature_offsets_m.first() != Some(&0.0)
            || self
                .curvature_offsets_m
                .iter()
                .any(|v| !v.is_finite() || *v < 0.0)
            || self.curvature_offsets_m.windows(2).any(|v| v[1] <= v[0])
        {
            return Err("Invalid curvature offsets".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Identity {
    pub car: String,
    pub track: String,
    pub sm_version: String,
    pub game_version: String,
}

pub fn wrap(a: f64) -> f64 {
    a.sin().atan2(a.cos())
}
pub fn cyclic_delta(a: f64, b: f64, length: f64) -> f64 {
    (a - b + length / 2.0).rem_euclid(length) - length / 2.0
}
