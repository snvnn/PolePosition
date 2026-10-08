//! Frozen-policy native episodes. No learner or mutable model in the drive loop.
use crate::{
    Result,
    core::Command,
    ipc::{Context, Reply},
    policy::Diagnostic,
};
use poleposition_policy::{
    Frame,
    features_v2::{FeaturesV2, Input, SpeedProfile},
    v2::SacNetwork,
};
use poleposition_state::model::{Prepared, hash, json, verify_map};
use serde::{Deserialize, Serialize};
use std::{
    collections::VecDeque,
    path::{Path, PathBuf},
    time::Instant,
};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub bundle: String,
    pub seconds: f64,
    pub deterministic: bool,
    #[serde(default)]
    pub evaluation: bool,
    #[serde(default)]
    pub unrestricted_motion: bool,
    #[serde(default)]
    pub unlimited_duration: bool,
    pub rng_state: u64,
}
impl Config {
    pub fn validate(&self) -> Result<()> {
        if self.bundle.is_empty()
            || !self.seconds.is_finite()
            || self.seconds <= 0.
            || self.seconds > 1800.
            || self.rng_state == 0
            || (self.unlimited_duration && !self.unrestricted_motion)
            || (self.evaluation && !self.deterministic)
            || (self.unrestricted_motion && (!self.deterministic || self.evaluation || self.seconds != 5.))
            || (self.deterministic && !self.evaluation && ![5., 10., 30.].contains(&self.seconds))
        {
            return Err("Invalid reinforcement episode configuration".into());
        }
        Ok(())
    }
    pub fn verify(&self, prepared: &Prepared) -> Result<(SacNetwork, SpeedProfile, Vec<PathBuf>)> {
        self.validate()?;
        let p = Path::new(&self.bundle);
        let m = json(&p.join("manifest.json"))?;
        let human = m["kind"] == "human_test_bundle";
        if self.unrestricted_motion && !human {
            return Err("Unrestricted motion is only available in the explicit human trial".into());
        }
        if human && (!self.deterministic || self.evaluation || self.seconds != 5.
            || m["steering_raw_per_axis"].as_f64().is_none_or(|v| !(1. ..=3.).contains(&v))
            || m["scope"] != "five_second_limited_control") {
            return Err("Human candidate is restricted to a five second deterministic test".into());
        }
        if (!human && m["kind"] != "sac_live_bundle")
            || m["complete"] != true
            || m["schema_version"] != 1
            || m["prepared_sha256"] != hash(&prepared.path.join("manifest.json"))?
        {
            return Err("Invalid live bundle/prepared identity".into());
        }
        verify_map(&m["output_sha256"], Some(p))?;
        verify_map(&m["input_sha256"], None)?;
        for name in [
            "model.json",
            "speed-profile.json",
            "normalizer.json",
            "parity.json",
        ] {
            if m["output_sha256"][name].as_str().is_none() {
                return Err("Missing live bundle inventory".into());
            }
        }
        let parity = json(&p.join("parity.json"))?;
        if parity["passed"] != true
            || parity["samples"].as_u64().is_none_or(|n| n < 100)
            || parity["max_absolute_error"]
                .as_f64()
                .is_none_or(|v| !(0. ..=1e-5).contains(&v))
        {
            return Err("Missing stochastic parity evidence".into());
        }
        let value = json(&p.join("model.json"))?;
        let normalizer = json(&p.join("normalizer.json"))?;
        if value["mean_network"]["feature_names"]
            != serde_json::to_value(poleposition_policy::features_v2::names())?
            || value["mean_network"]["mean"] != normalizer["mean"]
            || value["mean_network"]["std"] != normalizer["std"]
        {
            return Err("Live feature schema mismatch".into());
        }
        let n: SacNetwork = serde_json::from_value(value)?;
        n.validate()?;
        let s: SpeedProfile = serde_json::from_value(json(&p.join("speed-profile.json"))?)?;
        s.validate(&prepared.model)?;
        let mut inputs = vec![p.join("manifest.json").canonicalize()?];
        for name in m["output_sha256"]
            .as_object()
            .ok_or("Missing inventory")?
            .keys()
        {
            inputs.push(p.join(name).canonicalize()?);
        }
        Ok((n, s, inputs))
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct LapSample {
    pub time_ns: u64,
    pub completed_laps: i32,
    pub lap_time_ms: i32,
    pub progress: f64,
    pub lap_valid: bool,
}
#[derive(Default)]
pub struct FinishLine {
    previous: Option<LapSample>,
    timer_reset: Option<u64>,
    last: Option<u64>,
    valid: Option<bool>,
}
impl FinishLine {
    /// Same counter/timer and debounce rules as DrivingRecorder.laps.
    pub fn observe(&mut self, row: &LapSample) -> Result<Option<bool>> {
        let mut boundary = None;
        if let Some(p) = &self.previous {
            let delta = row.completed_laps - p.completed_laps;
            let near = p.progress > 0.8 || row.progress < 0.2;
            if delta < 0 || delta > 1 || (delta == 1 && !near) || row.time_ns < p.time_ns {
                return Err("Lap continuity lost".into());
            }
            let mut crossed = delta == 1 && near;
            if row.lap_time_ms + 2000 < p.lap_time_ms && delta == 0 && near {
                self.timer_reset = Some(row.time_ns);
            }
            if let Some(t) = self.timer_reset {
                if p.progress > 0.8 && row.progress < 0.2 && row.time_ns - t < 1_000_000_000 {
                    crossed = true;
                    self.timer_reset = None;
                } else if delta != 0 || row.time_ns - t >= 1_000_000_000 {
                    self.timer_reset = None;
                }
            }
            if crossed && self.last.is_none_or(|t| row.time_ns - t >= 5_000_000_000) {
                boundary = Some(self.valid.unwrap_or(true));
                self.valid = Some(true);
                self.last = Some(row.time_ns);
            }
        }
        self.valid = Some(self.valid.unwrap_or(true) && row.lap_valid);
        self.previous = Some(row.clone());
        Ok(boundary)
    }
}

/// Explicit reproducible xorshift64* state; two fresh uniforms per Box-Muller pair.
pub fn noise(state: &mut u64) -> [f32; 2] {
    fn uniform(s: &mut u64) -> f64 {
        *s ^= *s >> 12;
        *s ^= *s << 25;
        *s ^= *s >> 27;
        (((s.wrapping_mul(2685821657736338717) >> 11) as f64) + 0.5) / 9007199254740992.
    }
    let r = (-2. * uniform(state).ln()).sqrt();
    let t = std::f64::consts::TAU * uniform(state);
    [(r * t.cos()) as f32, (r * t.sin()) as f32]
}

pub struct Runtime {
    human_scale: Option<f64>,
    human_throttle_limit: f64,
    observed_axes: VecDeque<(u64, u64, [f64; 3])>,
    config: Config,
    network: SacNetwork,
    speed: SpeedProfile,
    model: poleposition_state::model::Model,
    baseline: poleposition_baseline::Controller,
    envelope: poleposition_baseline::Config,
    features: FeaturesV2,
    sequence: u64,
    last_observation: Option<u64>,
    next_decision: Option<u64>,
    start: Option<u64>,
    ready_since: Option<u64>,
    epoch: Option<u64>,
    finish: FinishLine,
    crossings: u32,
    progress: VecDeque<(u64, f64)>,
    pub rng_state: u64,
    pub reason: Option<String>,
}
impl Runtime {
    pub fn new(c: &crate::Config, p: &Prepared) -> Result<Self> {
        let config = c.reinforcement.clone().ok_or("Missing RL config")?;
        let (network, speed, _) = config.verify(p)?;
        let manifest = json(&Path::new(&config.bundle).join("manifest.json"))?;
        let human_throttle_limit = manifest["human_throttle_limit"].as_f64().unwrap_or(0.15);
        if !(0. ..=1.).contains(&human_throttle_limit) {
            return Err("Invalid demonstrated throttle range".into());
        }
        let envelope = c
            .baseline
            .as_ref()
            .ok_or("Missing controller")?
            .config
            .clone();
        Ok(Self {
            human_throttle_limit,
            human_scale: json(&Path::new(&config.bundle).join("manifest.json"))?["steering_raw_per_axis"].as_f64(),
            observed_axes: VecDeque::new(),
            rng_state: config.rng_state,
            config,
            network,
            speed,
            model: p.model.clone(),
            baseline: poleposition_baseline::Controller::new(p.model.clone(), envelope.clone())?,
            envelope,
            features: FeaturesV2::default(),
            sequence: 0,
            last_observation: None,
            next_decision: None,
            start: None,
            ready_since: None,
            epoch: None,
            finish: FinishLine::default(),
            crossings: 0,
            progress: VecDeque::new(),
            reason: None,
        })
    }
    pub fn active(&self) -> bool {
        self.start.is_some()
    }
    pub fn observe_axes(&mut self, ns: u64, epoch: u64, axes: [f64; 3]) {
        self.observed_axes.push_back((ns, epoch, axes));
        while self.observed_axes.len() > 4 { self.observed_axes.pop_front(); }
    }
    pub fn update(
        &mut self,
        c: &Context,
        lap: &LapSample,
        now: u64,
        age: u64,
    ) -> Result<Option<Reply>> {
        if self.reason.is_some() {
            return Ok(None);
        }
        let Some(o) = &c.path else {
            return Ok(None);
        };
        if self.last_observation == Some(o.sequence) {
            return Ok(None);
        }
        self.last_observation = Some(o.sequence);
        let mut out = if self.active() {
            poleposition_baseline::Output {
                distance_travelled_m: None,
                lap_fraction: None,
                steering: 0.,
                throttle: 0.,
                brake: 0.,
                target_speed_kmh: 10.,
                target_x: None,
                target_z: None,
                requested_curvature: None,
                steering_unclamped: None,
                reason: "model_control".into(),
                abort: false,
                complete: false,
            }
        } else {
            self.baseline.update(Some(o), now, c.armed_ns)
        };
        let mut detail = None;
        if c.armed_ns.is_some() && o.ready && c.health.gear_raw >= 2 {
            if let Some(a) = &c.previous_applied {
                let human = self.human_scale.is_some();
                let (previous_ns, previous_axes) = if human {
                    let Some((t, _, axes)) = self.observed_axes.iter().rev()
                        .find(|(t, e, _)| *t < o.observation_ns && *e == o.tracking_epoch) else { return Ok(None); };
                    (*t, *axes)
                } else { (a.at_ns, [a.axes.steering, a.axes.throttle, a.axes.brake]) };
                let cap = if human { 160. } else { 10. };
                let target_speed = if human { self.speed.at(o.s_m, cap) } else { 10. };
                let feature_begin = Instant::now();
                let values = self.features.update(
                    &Input {
                        frame: Frame {
                            sequence: o.sequence,
                            observation_ns: o.observation_ns,
                            tracking_epoch: o.tracking_epoch,
                            ready: o.ready,
                            speed_kmh: o.speed_kmh,
                            target_speed_kmh: target_speed,
                            rpm: c.health.rpm as f64,
                            lateral_error_m: o.lateral_error_m,
                            heading_error_rad: o.heading_error_rad,
                            s_m: o.s_m,
                        },
                        speed_cap_kmh: cap,
                        previous_axes,
                        previous_applied_ns: previous_ns,
                        gear_raw: c.health.gear_raw,
                    },
                    &self.model,
                    &self.speed,
                )?;
                let feature_ns = feature_begin.elapsed().as_nanos() as u64;
                if !self.active() {
                    if (8. ..=12.).contains(&o.speed_kmh)
                        && out.target_speed_kmh == 10.
                        && !out.abort
                        && !out.complete
                    {
                        let since = *self.ready_since.get_or_insert(o.observation_ns);
                        if o.observation_ns - since >= 1_000_000_000 && values.is_some() {
                            self.start = Some(o.observation_ns);
                            self.epoch = Some(o.tracking_epoch);
                        }
                    } else {
                        self.ready_since = None;
                    }
                }
                if self.active() {
                    if self.epoch != Some(o.tracking_epoch) {
                        return Err("Policy epoch changed".into());
                    }
                    let values = values.ok_or("Active policy history unavailable")?;
                    let mut end = None;
                    if let Err(reason) = o.check_with_speed_limit(&self.envelope, now, false, !self.config.unrestricted_motion) {
                        if ["path_distance_limit", "heading_limit"].contains(&reason) {
                            end = Some(reason);
                        } else {
                            return Err(reason.into());
                        }
                    }
                    if let Some(valid) = self.finish.observe(lap)? {
                        self.crossings += 1;
                        if self.crossings >= 2 {
                            end = Some(if valid { "lap_complete" } else { "invalid_lap" });
                        }
                    }
                    if !lap.lap_valid {
                        end = Some("invalid_lap");
                    }
                    self.progress.push_back((o.observation_ns, o.accumulated_m));
                    while self.progress.len() > 1
                        && self.progress[1].0 + 10_000_000_000 <= o.observation_ns
                    {
                        self.progress.pop_front();
                    }
                    if let Some((t, s)) = self.progress.front() {
                        if o.observation_ns - t >= 10_000_000_000 && o.accumulated_m - s < 0.5 {
                            end = Some("stalled");
                        }
                    }
                    let elapsed = o.observation_ns - self.start.unwrap();
                    let limit_elapsed = if self.config.deterministic && !self.config.evaluation {
                        elapsed
                    } else {
                        o.observation_ns.saturating_sub(c.armed_ns.unwrap())
                    };
                    if !self.config.unlimited_duration && limit_elapsed >= (self.config.seconds * 1e9) as u64 {
                        end = Some(if self.config.deterministic && !self.config.evaluation {
                            "verification_complete"
                        } else {
                            "episode_timeout"
                        });
                    }
                    if end.is_none() && self.next_decision.is_some_and(|t| o.observation_ns < t) {
                        return Ok(None);
                    }
                    let target = self.next_decision.unwrap_or(o.observation_ns);
                    self.next_decision = Some(
                        target
                            + (o.observation_ns.saturating_sub(target) / 20_000_000 + 1)
                                * 20_000_000,
                    );
                    let begin = Instant::now();
                    let action = if end.is_some() {
                        None
                    } else {
                        Some(self.network.predict(
                            &values,
                            if self.config.deterministic {
                                [0.; 2]
                            } else {
                                noise(&mut self.rng_state)
                            },
                        )?)
                    };
                    let inference_ns = begin.elapsed().as_nanos() as u64;
                    if inference_ns >= 10_000_000 {
                        return Err("Policy inference exceeded guardian period".into());
                    }
                    if let Some(action) = action {
                        let axes = poleposition_policy::v2::axes(action)?;
                        let steering_limit = if human && !self.config.unrestricted_motion { self.envelope.max_steering.min(0.1) } else { self.envelope.max_steering };
                        out.steering = (axes[0] / self.human_scale.unwrap_or(1.)).clamp(-steering_limit, steering_limit);
                        if human { out.target_speed_kmh = target_speed; }
                        out.throttle = axes[1].min(if human { self.human_throttle_limit } else { self.envelope.max_throttle });
                        out.brake = axes[2].min(self.envelope.max_brake);
                        out.reason = "model_control".into();
                        out.abort = false;
                        out.complete = false;
                    } else {
                        out.steering = 0.;
                        out.throttle = 0.;
                        out.brake = 1.;
                        out.complete = true;
                        out.abort = false;
                        out.reason = end.unwrap().into();
                        self.reason = Some(out.reason.clone());
                    }
                    detail = Some(Diagnostic {
                        now_ns: now,
                        observation_sequence: o.sequence,
                        observation_ns: o.observation_ns,
                        tracking_epoch: o.tracking_epoch,
                        control_source: if action.is_some() {
                            "model"
                        } else {
                            "model_terminal"
                        }
                        .into(),
                        policy_reason: out.reason.clone(),
                        features_json: serde_json::to_string(&values)?,
                        prediction_u: action.map(|a| a[1]),
                        prediction_steering: action.map(|a| a[0]),
                        model_throttle: action.map(|a| f64::from(a[1].max(0.))),
                        model_brake: action.map(|a| f64::from((-a[1]).max(0.))),
                        pid_throttle: 0.,
                        pid_brake: 0.,
                        feature_ns,
                        inference_ns: action.map(|_| inference_ns),
                        model_elapsed_ns: elapsed,
                        feature_schema_version: Some(2),
                    });
                }
            }
        }
        self.sequence += 1;
        Ok(Some(Reply {
            command: Command {
                sequence: self.sequence,
                created_ns: now,
                expires_ns: now + age,
                observation_sequence: o.sequence,
                observation_ns: o.observation_ns,
                tracking_epoch: o.tracking_epoch,
                steering: out.steering,
                throttle: out.throttle,
                brake: out.brake,
            },
            scenario_complete: out.complete,
            controller: Some(out),
            policy: detail,
        }))
    }
}

/// One hotkey owner for the whole batch, including learning and manual reset.
/// Requests are immutable local files; they prepare a run but never synthesize F7.
pub fn supervise(session: &Path, manager: &poleposition_management::Managed) -> Result<PathBuf> {
    use poleposition_state::writer::finish_manifest as write_json;
    use std::sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    };
    let keys = crate::platform::Hotkeys::register()?;
    let interrupt = Arc::new(AtomicU64::new(0));
    let signal = interrupt.clone();
    ctrlc::set_handler(move || {
        let _ = signal.compare_exchange(
            0,
            crate::platform::now_ns(),
            Ordering::AcqRel,
            Ordering::Relaxed,
        );
    })?;
    let mut number = 1;
    manager.emit(serde_json::json!({"event":"progress","phase":"supervisor_ready"}));
    let reason = loop {
        if keys.stop_observed_ns.load(Ordering::Acquire) > 0
            || interrupt.load(Ordering::Acquire) > 0
        {
            break "f8";
        }
        if let Some(reason) = manager.control.stop_reason() {
            break reason;
        }
        if session.join("done.json").is_file() {
            break "batch_complete";
        }
        let request = session.join(format!("request-{number}.json"));
        if request.is_file() {
            let value = json(&request)?;
            let config: crate::Config = serde_json::from_value(value["config"].clone())?;
            config.validate()?;
            if config.reinforcement.is_none() {
                return Err("Supervisor accepts only RL episodes".into());
            }
            let prepared = Path::new(value["prepared"].as_str().ok_or("Missing prepared path")?);
            let profile = Path::new(
                value["profile"]
                    .as_str()
                    .ok_or("Missing calibration path")?,
            );
            let outcome = crate::runner::test_managed_shared(
                config,
                prepared,
                "demo",
                profile,
                Some(manager),
                Some((&keys, &interrupt)),
            );
            let cancelled = if keys.stop_observed_ns.load(Ordering::Acquire) > 0
                || interrupt.load(Ordering::Acquire) > 0 {
                Some("f8")
            } else { manager.control.stop_reason() };
            match outcome {
                Ok(path) => write_json(
                    &session.join(format!("response-{number}.json")),
                    &serde_json::json!({"run":path,"cancelled":cancelled}),
                )?,
                Err(e) => {
                    write_json(
                        &session.join(format!("response-{number}.json")),
                        &serde_json::json!({"error":e.to_string()}),
                    )?;
                    return Err(e);
                }
            }
            if let Some(reason) = cancelled { break reason; }
            number += 1;
        }
        // Starts pressed during learning/idle are never carried into a future run.
        keys.start.store(false, Ordering::Release);
        std::thread::sleep(std::time::Duration::from_millis(10));
    };
    write_json(
        &session.join("summary.json"),
        &serde_json::json!({"normal_shutdown":true,"termination_reason":reason}),
    )?;
    Ok(session.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;
    use poleposition_state::model::{Model, Point};
    fn runtime(deterministic: bool) -> Runtime {
        let model = Model {
            length: 4000.,
            points: [(0., 0.), (1000., 0.), (1000., 1000.), (0., 1000.)]
                .iter()
                .enumerate()
                .map(|(i, (x, z))| Point {
                    index: i,
                    s_m: i as f64 * 1000.,
                    position_x: *x,
                    position_y: 0.,
                    position_z: *z,
                    tangent_x: 1.,
                    tangent_z: 0.,
                    heading_path_rad: 0.,
                    curvature_per_m: 0.,
                })
                .collect(),
        };
        let layer = |input, output, bias: f32| serde_json::json!({"weight":vec![vec![0.;input];output],"bias":vec![bias;output]});
        let network:SacNetwork=serde_json::from_value(serde_json::json!({"schema_version":1,
            "mean_network":{"schema_version":2,"feature_names":poleposition_policy::features_v2::names(),
                "action_names":["steering","signed_acceleration"],"mean":vec![0.;35],"std":vec![1.;35],
                "layers":[layer(35,128,0.),layer(128,128,0.),layer(128,2,0.05)]},
            "log_std":layer(128,2,-4.),"log_std_bounds":[-5.,-2.]})).unwrap();
        let envelope = poleposition_baseline::Config::load(
            &Path::new(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .join("BaselineController/config.toml"),
        )
        .unwrap();
        Runtime {
            human_throttle_limit: 0.15,
            human_scale: None,
            observed_axes: VecDeque::new(),
            config: Config {
                bundle: "test".into(),
                seconds: if deterministic { 5. } else { 1800. },
                deterministic,
                evaluation: false,
                unrestricted_motion: false,
                unlimited_duration: false,
                rng_state: 42,
            },
            network,
            speed: SpeedProfile {
                schema_version: 2,
                length_m: 4000.,
                s_m: vec![0., 1000., 2000., 3000.],
                speed_kmh: vec![10.; 4],
            },
            baseline: poleposition_baseline::Controller::new(model.clone(), envelope.clone())
                .unwrap(),
            model,
            envelope,
            features: FeaturesV2::default(),
            sequence: 0,
            last_observation: None,
            next_decision: None,
            start: None,
            ready_since: None,
            epoch: None,
            finish: FinishLine::default(),
            crossings: 0,
            progress: VecDeque::new(),
            rng_state: 42,
            reason: None,
        }
    }
    fn context(i: u64, interval: u64) -> Context {
        let t = 1_000_000_000 + i * interval;
        let mut health = crate::core::Health::default();
        health.gear_raw = 2;
        health.rpm = 2000;
        Context {
            previous_applied: Some(crate::v2_shadow::Applied {
                at_ns: t - 1_000_000,
                tracking_epoch: 1,
                axes: crate::Axes::NEUTRAL,
            }),
            path: Some(poleposition_baseline::Observation {
                sequence: i + 1,
                observation_ns: t,
                tracking_epoch: 1,
                ready: true,
                in_pit: false,
                gear_raw: 2,
                speed_kmh: 10.,
                x: i as f64 * 0.027,
                z: 0.,
                heading_rad: 0.,
                heading_error_rad: 0.,
                projection_distance_m: 0.,
                lateral_error_m: 0.,
                s_m: i as f64 * 0.027,
                accumulated_m: i as f64 * 0.027,
            }),
            health,
            armed_ns: Some(1_000_000_000),
            sent_ns: t,
        }
    }
    #[test]
    fn human_trial_uses_prior_observed_axes_and_profile_with_scaled_output() {
        for unlimited in [false, true] {
        let mut r = runtime(true);
        r.config.unlimited_duration = unlimited;
        r.config.unrestricted_motion = unlimited;
        r.human_scale = Some(1.7);
        r.human_throttle_limit = 0.6;
        let mut network = serde_json::to_value(&r.network).unwrap();
        network["mean_network"]["layers"][2]["bias"][1] = serde_json::json!(0.5);
        r.network = serde_json::from_value(network).unwrap();
        r.speed.speed_kmh = vec![80.; 4];
        let mut checked = 0;
        for i in 0..1500 {
            let c = context(i, 10_000_000);
            let t = c.sent_ns;
            r.observe_axes(t, 1, [0.12, 0.08, 0.]);
            let lap = LapSample { time_ns: t, lap_valid: true, ..Default::default() };
            if let Some(reply) = r.update(&c, &lap, t, 100_000_000).unwrap() {
                if let Some(p) = reply.policy {
                    if p.control_source == "model" {
                        assert!((reply.command.steering - 0.05f64.tanh()/1.7).abs() < 1e-6);
                        assert!((reply.command.throttle - 0.5f64.tanh()).abs() < 1e-6);
                        assert!(reply.command.steering.abs() <= 0.1);
                        let values: Vec<f32> = serde_json::from_str(&p.features_json).unwrap();
                        assert_eq!(values[1], 80.);
                        assert_eq!(values[19], 0.12);
                        assert_eq!(values[20], 0.08);
                        checked += 1;
                    }
                }
            }
        }
        if unlimited {
            assert!(checked > 250);
            assert_eq!(r.reason, None);
        } else {
            assert_eq!(checked, 250);
            assert_eq!(r.reason.as_deref(), Some("verification_complete"));
        }
        }
    }
    #[test]
    fn direct_control_is_fixed_and_terminal_is_a_fresh_observation() {
        let mut r = runtime(true);
        let mut decisions = 0;
        let mut prior = 0;
        for i in 0..1500 {
            let c = context(i, 10_000_000);
            let t = c.sent_ns;
            let lap = LapSample {
                time_ns: t,
                lap_valid: true,
                ..Default::default()
            };
            if let Some(reply) = r.update(&c, &lap, t, 100_000_000).unwrap() {
                if let Some(p) = reply.policy {
                    if p.control_source == "model" {
                        assert!((reply.command.steering - 0.05f64.tanh()).abs() < 1e-6);
                        assert!((reply.command.throttle - 0.05f64.tanh()).abs() < 1e-6);
                        if decisions > 0 {
                            assert_eq!(t - prior, 20_000_000);
                        }
                        prior = t;
                        decisions += 1;
                    } else {
                        assert_eq!(p.policy_reason, "verification_complete");
                        assert!(t > prior);
                        assert!(p.prediction_u.is_none());
                    }
                }
            }
        }
        assert_eq!(decisions, 250);
        assert_eq!(r.rng_state, 42);
        assert_eq!(r.reason.as_deref(), Some("verification_complete"));
    }
    #[test]
    fn scheduler_has_no_phase_drift_and_failure_keeps_terminal_features() {
        let mut r = runtime(false);
        let mut decisions = 0;
        let mut previous = 0;
        for i in 0..1100 {
            let mut c = context(i, 10_300_000);
            let t = c.sent_ns;
            if i == 1050 {
                c.path.as_mut().unwrap().projection_distance_m = 3.1;
            }
            let lap = LapSample {
                time_ns: t,
                lap_valid: true,
                ..Default::default()
            };
            if let Some(reply) = r.update(&c, &lap, t, 100_000_000).unwrap() {
                if let Some(p) = reply.policy {
                    if p.control_source == "model" {
                        if decisions > 0 {
                            assert!(t - previous <= 20_600_000);
                        }
                        previous = t;
                        decisions += 1;
                    } else {
                        assert_eq!(p.policy_reason, "path_distance_limit");
                        assert_eq!(
                            serde_json::from_str::<Vec<f32>>(&p.features_json)
                                .unwrap()
                                .len(),
                            35
                        );
                    }
                }
            }
        }
        assert!(decisions > 100);
        assert_eq!(r.reason.as_deref(), Some("path_distance_limit"));
        assert_ne!(r.rng_state, 42);
    }
    #[test]
    fn finish_rules_counter_hotlap_and_invalid_lap() {
        let mut f = FinishLine::default();
        let row = |time_ns, completed_laps, lap_time_ms, progress, lap_valid| LapSample {
            time_ns,
            completed_laps,
            lap_time_ms,
            progress,
            lap_valid,
        };
        assert_eq!(f.observe(&row(0, 0, 10000, 0.9, true)).unwrap(), None);
        assert_eq!(
            f.observe(&row(10_000_000, 1, 0, 0.01, true)).unwrap(),
            Some(true)
        );
        assert_eq!(
            f.observe(&row(6_000_000_000, 1, 10000, 0.9, false))
                .unwrap(),
            None
        );
        assert_eq!(
            f.observe(&row(6_010_000_000, 1, 0, 0.01, true)).unwrap(),
            Some(false)
        );
        assert!(f.observe(&row(6_020_000_000, 4, 10, 0.02, true)).is_err());
    }
    #[test]
    fn rng_state_resumes_exactly() {
        let mut a = 42;
        noise(&mut a);
        let mut b = a;
        assert_eq!(noise(&mut a), noise(&mut b));
    }
}
