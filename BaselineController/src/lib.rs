//! Low-speed pure pursuit + speed PID. No device access; X-Z curvature is signed.
use poleposition_state::{
    model::{Model, Result},
    processor::State,
    wrap,
};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    #[serde(default)]
    pub single_lap: bool,
    pub target_speed_kmh: f64,
    pub target_ramp_kmh_per_s: f64,
    pub lookahead_m: f64,
    pub steering_per_curvature_m: f64,
    pub max_steering: f64,
    pub max_throttle: f64,
    pub max_brake: f64,
    pub speed_kp: f64,
    pub speed_ki: f64,
    pub speed_kd: f64,
    pub derivative_filter_s: f64,
    pub integral_limit: f64,
    pub speed_deadband_kmh: f64,
    pub max_age_ms: f64,
    pub max_speed_kmh: f64,
    pub start_distance_m: f64,
    pub max_distance_m: f64,
    pub start_heading_deg: f64,
    pub max_heading_deg: f64,
    pub initial_hold_s: f64,
    pub duration_s: f64,
    pub max_distance_travelled_m: f64,
}
impl Config {
    pub fn load(path: &std::path::Path) -> Result<Self> {
        let c: Self = toml::from_str(&std::fs::read_to_string(path)?)?;
        c.validate()?;
        Ok(c)
    }
    pub fn validate(&self) -> Result<()> {
        let values = serde_json::to_value(self)?;
        if values
            .as_object()
            .unwrap()
            .iter()
            .filter(|(key, _)| key.as_str() != "single_lap")
            .any(|(_, v)| v.as_f64().is_none_or(|x| !x.is_finite() || x <= 0.))
            || self.target_speed_kmh > 10.
            || self.max_speed_kmh > 20.
            || self.target_speed_kmh >= self.max_speed_kmh
            || self.max_age_ms > 100.
            || self.max_steering > 1.
            || self.max_throttle > 0.2
            || self.max_brake > 1.
            || self.duration_s > if self.single_lap { 3600. } else { 30. }
            || self.max_distance_travelled_m > if self.single_lap { 10000. } else { 100. }
            || self.start_distance_m > self.max_distance_m
            || self.max_distance_m > 3.
            || self.start_heading_deg > self.max_heading_deg
            || self.max_heading_deg > 30.
            || !(3.0..=20.0).contains(&self.lookahead_m)
            || !(1.0..=20.0).contains(&self.steering_per_curvature_m)
        {
            return Err("Invalid baseline demonstration envelope".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Observation {
    pub sequence: u64,
    pub observation_ns: u64,
    pub tracking_epoch: u64,
    pub ready: bool,
    pub in_pit: bool,
    pub gear_raw: i32,
    pub speed_kmh: f64,
    pub x: f64,
    pub z: f64,
    pub heading_rad: f64,
    pub heading_error_rad: f64,
    pub projection_distance_m: f64,
    pub lateral_error_m: f64,
    pub s_m: f64,
    pub accumulated_m: f64,
}
impl Observation {
    pub fn from_state(state: &State, observation_ns: u64, gear_raw: i32) -> Option<Self> {
        Some(Self {
            sequence: state.sequence?,
            observation_ns,
            tracking_epoch: state.tracking_epoch,
            ready: state.status == "ready",
            in_pit: state.in_pit?,
            gear_raw,
            speed_kmh: state.speed_kmh?,
            x: state.position_x?,
            z: state.position_z?,
            heading_rad: state.vehicle_heading_rad?,
            heading_error_rad: state.heading_error_rad?,
            projection_distance_m: state.projection_distance_m?,
            lateral_error_m: state.lateral_error_m?,
            s_m: state.s_m?,
            accumulated_m: state.accumulated_m?,
        })
    }
    pub fn check(
        &self,
        c: &Config,
        now: u64,
        starting: bool,
    ) -> std::result::Result<(), &'static str> {
        self.check_with_speed_limit(c, now, starting, true)
    }
    pub fn check_with_speed_limit(
        &self, c: &Config, now: u64, starting: bool, speed_limit: bool,
    ) -> std::result::Result<(), &'static str> {
        if !self.ready {
            return Err("tracking_not_ready");
        }
        if self.sequence == 0
            || self.observation_ns > now
            || now - self.observation_ns >= (c.max_age_ms * 1e6) as u64
        {
            return Err("observation_expired");
        }
        if [
            self.speed_kmh,
            self.x,
            self.z,
            self.heading_rad,
            self.heading_error_rad,
            self.projection_distance_m,
            self.lateral_error_m,
            self.s_m,
            self.accumulated_m,
        ]
        .iter()
        .any(|v| !v.is_finite())
            || self.speed_kmh < 0.
            || self.projection_distance_m < 0.
        {
            return Err("invalid_observation");
        }
        if self.in_pit {
            return Err("pit_area");
        }
        if speed_limit && self.speed_kmh > c.max_speed_kmh {
            return Err("speed_limit");
        }
        if self.projection_distance_m
            > if starting {
                c.start_distance_m
            } else {
                c.max_distance_m
            }
        {
            return Err("path_distance_limit");
        }
        if wrap(self.heading_error_rad).abs().to_degrees()
            > if starting {
                c.start_heading_deg
            } else {
                c.max_heading_deg
            }
        {
            return Err("heading_limit");
        }
        Ok(())
    }
}

/// Interpolation along the existing ordered closed polyline, including its seam.
pub fn target(model: &Model, s: f64) -> (f64, f64) {
    let s = s.rem_euclid(model.length);
    let i = model
        .points
        .partition_point(|p| p.s_m <= s)
        .saturating_sub(1);
    let a = &model.points[i];
    let b = &model.points[(i + 1) % model.points.len()];
    let end = if i + 1 == model.points.len() {
        model.length
    } else {
        b.s_m
    };
    let u = (s - a.s_m) / (end - a.s_m);
    (
        a.position_x + u * (b.position_x - a.position_x),
        a.position_z + u * (b.position_z - a.position_z),
    )
}
/// World Y, and signed height change over a centered 10 m planar path window.
/// This is a reference-line slope estimate, not independently measured road grade.
pub fn elevation(model: &Model, s: f64) -> f64 {
    let s = s.rem_euclid(model.length);
    let i = model
        .points
        .partition_point(|p| p.s_m <= s)
        .saturating_sub(1);
    let a = &model.points[i];
    let b = &model.points[(i + 1) % model.points.len()];
    let end = if i + 1 == model.points.len() {
        model.length
    } else {
        b.s_m
    };
    a.position_y + (b.position_y - a.position_y) * (s - a.s_m) / (end - a.s_m)
}
pub fn grade_pct(model: &Model, s: f64) -> f64 {
    (elevation(model, s + 5.) - elevation(model, s - 5.)) * 10.
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Output {
    pub distance_travelled_m: Option<f64>,
    pub lap_fraction: Option<f64>,
    pub steering: f64,
    pub throttle: f64,
    pub brake: f64,
    pub target_speed_kmh: f64,
    pub target_x: Option<f64>,
    pub target_z: Option<f64>,
    pub requested_curvature: Option<f64>,
    pub steering_unclamped: Option<f64>,
    pub reason: String,
    pub abort: bool,
    pub complete: bool,
}
impl Output {
    fn hold(reason: &str, abort: bool, complete: bool) -> Self {
        Self {
            distance_travelled_m: None,
            lap_fraction: None,
            steering: 0.,
            throttle: 0.,
            brake: 1.,
            target_speed_kmh: 0.,
            target_x: None,
            target_z: None,
            requested_curvature: None,
            steering_unclamped: None,
            reason: reason.into(),
            abort,
            complete,
        }
    }
}
pub struct Controller {
    pub model: Model,
    pub config: Config,
    last: Option<Observation>,
    cached: Output,
    latched: Option<Output>,
    drive_start: Option<u64>,
    start_distance: Option<f64>,
    epoch: Option<u64>,
    integral: f64,
    derivative: f64,
    previous_speed: Option<f64>,
}
impl Controller {
    pub fn new(model: Model, config: Config) -> Result<Self> {
        model.validate()?;
        config.validate()?;
        if config.single_lap && config.max_distance_travelled_m < model.length {
            return Err("Single-lap distance cap is shorter than this model".into());
        }
        Ok(Self {
            model,
            config,
            last: None,
            cached: Output::hold("waiting_start", false, false),
            latched: None,
            drive_start: None,
            start_distance: None,
            epoch: None,
            integral: 0.,
            derivative: 0.,
            previous_speed: None,
        })
    }
    fn stop(&mut self, reason: &str, complete: bool) -> Output {
        let mut out = Output::hold(reason, !complete, complete);
        if let Some((o, start)) = self.last.as_ref().zip(self.start_distance) {
            out.distance_travelled_m = Some(o.accumulated_m - start);
            out.lap_fraction = Some((o.accumulated_m - start) / self.model.length);
        }
        self.latched = Some(out.clone());
        out
    }
    pub fn update(&mut self, o: Option<&Observation>, now: u64, armed_ns: Option<u64>) -> Output {
        if let Some(out) = &self.latched {
            return out.clone();
        }
        let Some(armed) = armed_ns else {
            return Output::hold("waiting_start", false, false);
        };
        if now < armed {
            return self.stop("clock_reversal", false);
        }
        if now - armed >= (self.config.duration_s * 1e9) as u64 {
            return if self.config.single_lap {
                self.stop("lap_timeout", false)
            } else {
                self.stop("duration_complete", true)
            };
        }
        let Some(o) = o else {
            return self.stop("missing_path_state", false);
        };
        if let Err(reason) = o.check(&self.config, now, self.drive_start.is_none()) {
            return self.stop(reason, false);
        }
        if self.epoch.is_some_and(|e| e != o.tracking_epoch) {
            return self.stop("tracking_epoch_changed", false);
        }
        self.epoch = Some(o.tracking_epoch);
        if let Some(last) = &self.last {
            if o.sequence < last.sequence || o.observation_ns < last.observation_ns {
                return self.stop("observation_order", false);
            }
            if o.sequence == last.sequence {
                if o != last {
                    return self.stop("duplicate_changed", false);
                }
                return self.cached.clone();
            }
            if o.observation_ns <= last.observation_ns
                || o.observation_ns - last.observation_ns >= (self.config.max_age_ms * 1e6) as u64
            {
                return self.stop("observation_gap", false);
            }
        }
        let dt = self.last.as_ref().map_or(0., |last| {
            (o.observation_ns - last.observation_ns) as f64 / 1e9
        });
        self.last = Some(o.clone());
        if o.gear_raw < 2 {
            if self.drive_start.is_some() {
                return self.stop("forward_gear_lost", false);
            }
            self.cached = Output::hold("waiting_forward_gear", false, false);
            return self.cached.clone();
        }
        if now - armed < (self.config.initial_hold_s * 1e9) as u64 {
            self.cached = Output::hold("initial_brake_hold", false, false);
            return self.cached.clone();
        }
        let drive_start = *self.drive_start.get_or_insert(o.observation_ns);
        let start_distance = *self.start_distance.get_or_insert(o.accumulated_m);
        let travelled = o.accumulated_m - start_distance;
        if travelled < -0.5 {
            return self.stop("reverse_progress", false);
        }
        if self.config.single_lap && travelled >= self.model.length {
            return self.stop("lap_complete", true);
        }
        if travelled >= self.config.max_distance_travelled_m {
            return self.stop("distance_complete", true);
        }
        let (tx, tz) = target(&self.model, o.s_m + self.config.lookahead_m);
        let dx = tx - o.x;
        let dz = tz - o.z;
        let forward = o.heading_rad.cos() * dx + o.heading_rad.sin() * dz;
        let cross = o.heading_rad.cos() * dz - o.heading_rad.sin() * dx;
        let d2 = dx * dx + dz * dz;
        if forward <= 0. || d2 < 1. {
            return self.stop("target_not_ahead", false);
        }
        let curvature = 2. * cross / d2;
        let steering = curvature * self.config.steering_per_curvature_m;
        // Never conceal a curve/correction outside this demonstrated input range.
        if steering.abs() > self.config.max_steering {
            let mut out = self.stop("steering_envelope", false);
            out.target_x = Some(tx);
            out.target_z = Some(tz);
            out.requested_curvature = Some(curvature);
            out.steering_unclamped = Some(steering);
            self.latched = Some(out.clone());
            return out;
        }
        let target_speed = self
            .config
            .target_speed_kmh
            .min((o.observation_ns - drive_start) as f64 / 1e9 * self.config.target_ramp_kmh_per_s);
        let error = target_speed - o.speed_kmh;
        if let Some(old) = self.previous_speed
            && dt > 0.
        {
            let raw = -(o.speed_kmh - old) / dt;
            self.derivative +=
                dt / (self.config.derivative_filter_s + dt) * (raw - self.derivative);
        }
        self.previous_speed = Some(o.speed_kmh);
        if error.abs() > self.config.speed_deadband_kmh {
            let candidate = (self.integral + error * dt)
                .clamp(-self.config.integral_limit, self.config.integral_limit);
            let u = self.config.speed_kp * error
                + self.config.speed_ki * candidate
                + self.config.speed_kd * self.derivative;
            if (-self.config.max_brake..=self.config.max_throttle).contains(&u)
                || (u > self.config.max_throttle && error < 0.)
                || (u < -self.config.max_brake && error > 0.)
            {
                self.integral = candidate;
            }
        }
        // Drop positive integral when slowing: residual acceleration cannot oppose braking.
        if error < -self.config.speed_deadband_kmh {
            self.integral = self.integral.min(0.);
        }
        let u = self.config.speed_kp * error
            + self.config.speed_ki * self.integral
            + self.config.speed_kd * self.derivative;
        let u = if error < -self.config.speed_deadband_kmh {
            u.min(self.config.speed_kp * error)
        } else {
            u
        };
        let u = u.clamp(-self.config.max_brake, self.config.max_throttle);
        self.cached = Output {
            distance_travelled_m: Some(travelled),
            lap_fraction: Some(travelled / self.model.length),
            steering,
            throttle: u.max(0.),
            brake: (-u).max(0.),
            target_speed_kmh: target_speed,
            target_x: Some(tx),
            target_z: Some(tz),
            requested_curvature: Some(curvature),
            steering_unclamped: Some(steering),
            reason: "tracking".into(),
            abort: false,
            complete: false,
        };
        self.cached.clone()
    }
}
