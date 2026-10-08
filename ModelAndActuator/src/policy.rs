//! Policy overlay: PID starts/stops; only tracking pedals can transfer to the model.
use crate::Result;
use poleposition_policy::{Features, Frame, Network};
use poleposition_state::model::{Model, hash, json};
use serde::{Deserialize, Serialize};
use std::{path::Path, time::Instant};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub model: String,
    pub mode: String,
    pub model_seconds: f64,
    #[serde(default)]
    pub previous_run: Option<String>,
}
impl Config {
    pub fn validate(&self) -> Result<()> {
        if self.model.is_empty()
            || !["shadow", "drive"].contains(&self.mode.as_str())
            || ![5., 10., 15.].contains(&self.model_seconds)
        {
            return Err("Invalid policy mode/duration".into());
        }
        Ok(())
    }
    pub fn verify(&self, prepared: &Path) -> Result<Network> {
        self.validate()?;
        let folder = Path::new(&self.model);
        let manifest = json(&folder.join("manifest.json"))?;
        if manifest["prepared_sha256"] != hash(&prepared.join("manifest.json"))? {
            return Err("Policy/prepared mismatch".into());
        }
        let network = poleposition_policy::load(folder, true)?;
        if self.mode == "drive" {
            let prior = Path::new(
                self.previous_run
                    .as_ref()
                    .ok_or("Drive requires --previous-run: shadow, then 5 s, then 10 s")?,
            );
            let m = json(&prior.join("manifest.json"))?;
            let s = json(&prior.join("summary.json"))?;
            let base = prior.canonicalize()?;
            for (name, value) in m["output_sha256"]
                .as_object()
                .ok_or("Prior run inventory missing")?
            {
                let p = prior.join(name).canonicalize()?;
                if !p.starts_with(&base) || *value != hash(&p)? {
                    return Err("Prior run changed".into());
                }
            }
            for (name, value) in m["input_sha256"]
                .as_object()
                .ok_or("Prior inputs missing")?
            {
                if *value != hash(Path::new(name))? {
                    return Err("Prior input changed".into());
                }
            }
            let p = &m["config"]["policy"];
            if m["complete"] != true
                || m["mock"] != false
                || s["normal_shutdown"] != true
                || s["recording_valid"] != true
                || s["stop_confirmed"] != true
                || s["released"] != true
                || s["dropped"] != 0
                || p["model"]
                    .as_str()
                    .is_none_or(|v| Path::new(v).canonicalize().ok() != folder.canonicalize().ok())
                || s["policy_prediction_samples"]
                    .as_u64()
                    .is_none_or(|n| n < 100)
                || s["policy_inference"]["p99_us"]
                    .as_f64()
                    .is_none_or(|v| !(0.0..=1000.0).contains(&v))
            {
                return Err("Prior policy run did not pass".into());
            }
            if self.model_seconds == 5. {
                if p["mode"] != "shadow"
                    || !["time_limit", "scenario_complete"]
                        .contains(&s["termination_reason"].as_str().unwrap_or(""))
                {
                    return Err("First drive requires completed shadow run".into());
                }
            } else if p["mode"] != "drive"
                || p["model_seconds"] != self.model_seconds - 5.
                || s["controller_exit_reason"] != "policy_duration_complete"
                || s["policy_model_elapsed_ns"]
                    .as_u64()
                    .is_none_or(|n| n < ((self.model_seconds - 5.) * 1e9) as u64)
            {
                return Err("Previous model duration not completed".into());
            }
        }
        Ok(network)
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Diagnostic {
    pub now_ns: u64,
    pub observation_sequence: u64,
    pub observation_ns: u64,
    pub tracking_epoch: u64,
    pub control_source: String,
    pub policy_reason: String,
    pub features_json: String,
    pub prediction_u: Option<f32>,
    pub model_throttle: Option<f64>,
    pub model_brake: Option<f64>,
    pub pid_throttle: f64,
    pub pid_brake: f64,
    pub feature_ns: u64,
    pub inference_ns: Option<u64>,
    pub model_elapsed_ns: u64,
    #[serde(default)]
    pub prediction_steering: Option<f32>,
    #[serde(default)]
    pub feature_schema_version: Option<u32>,
}
pub struct Runtime {
    config: Config,
    network: Network,
    model: Model,
    features: Features,
    last: Option<(u64, u64, u64)>,
    cached: Option<(poleposition_baseline::Output, Diagnostic)>,
    stable_since: Option<u64>,
    model_since: Option<u64>,
    divergence_since: Option<u64>,
    latched: Option<String>,
}
impl Runtime {
    pub fn new(config: Config, network: Network, model: Model) -> Result<Self> {
        config.validate()?;
        network.validate()?;
        model.validate()?;
        Ok(Self {
            config,
            network,
            model,
            features: Features::default(),
            last: None,
            cached: None,
            stable_since: None,
            model_since: None,
            divergence_since: None,
            latched: None,
        })
    }
    pub fn update(
        &mut self,
        o: &poleposition_baseline::Observation,
        rpm: f64,
        pid: &poleposition_baseline::Output,
        now: u64,
        armed: Option<u64>,
    ) -> (poleposition_baseline::Output, Diagnostic) {
        let key = (o.sequence, o.observation_ns, o.tracking_epoch);
        if self.last == Some(key)
            && !pid.abort
            && !pid.complete
            && let Some(v) = &self.cached
        {
            return v.clone();
        }
        let mut out = pid.clone();
        let mut d = Diagnostic {
            prediction_steering: None,
            feature_schema_version: Some(1),
            now_ns: now,
            observation_sequence: o.sequence,
            observation_ns: o.observation_ns,
            tracking_epoch: o.tracking_epoch,
            control_source: "pid_start".into(),
            policy_reason: pid.reason.clone(),
            features_json: "[]".into(),
            prediction_u: None,
            model_throttle: None,
            model_brake: None,
            pid_throttle: pid.throttle,
            pid_brake: pid.brake,
            feature_ns: 0,
            inference_ns: None,
            model_elapsed_ns: self
                .model_since
                .map_or(0, |s| o.observation_ns.saturating_sub(s)),
        };
        let frame = Frame {
            sequence: o.sequence,
            observation_ns: o.observation_ns,
            tracking_epoch: o.tracking_epoch,
            ready: o.ready,
            speed_kmh: o.speed_kmh,
            target_speed_kmh: pid.target_speed_kmh,
            rpm,
            lateral_error_m: o.lateral_error_m,
            heading_error_rad: o.heading_error_rad,
            s_m: o.s_m,
        };
        let begin = Instant::now();
        let features = self.features.update(&frame, &self.model);
        d.feature_ns = begin.elapsed().as_nanos() as u64;
        let mut failure = None;
        match features {
            Ok(Some(x)) => {
                d.features_json = serde_json::to_string(&x).unwrap_or_default();
                let begin = Instant::now();
                let prediction = self.network.predict(&x);
                d.inference_ns = Some(begin.elapsed().as_nanos() as u64);
                match prediction
                    .and_then(|u| poleposition_policy::pedals(u).map(|(t, b)| (u, t, b)))
                {
                    Ok((u, t, b)) => {
                        d.model_throttle = Some(t);
                        d.model_brake = Some(b);
                        d.prediction_u = Some(u);
                    }
                    Err(_) => failure = Some("policy_inference_failed"),
                }
            }
            Ok(None) => {
                if self.model_since.is_some() {
                    failure = Some("policy_history_lost");
                }
            }
            Err(_) => failure = Some("policy_invalid_features"),
        }
        if pid.abort || pid.complete || armed.is_none() || pid.reason != "tracking" {
            self.stable_since = None;
            d.control_source = if pid.abort || pid.complete {
                "stop"
            } else {
                "pid_start"
            }
            .into();
        } else if self.config.mode == "shadow" {
            d.control_source = "pid_shadow".into();
            d.policy_reason = "shadow_no_model_output".into();
        } else if let Some(start) = self.model_since {
            d.control_source = "model".into();
            d.policy_reason = "model_tracking".into();
            if o.observation_ns.saturating_sub(start) >= (self.config.model_seconds * 1e9) as u64 {
                out.complete = true;
                out.reason = "policy_duration_complete".into();
                d.control_source = "stop".into();
                d.policy_reason = out.reason.clone();
            }
        } else {
            let ready = pid.target_speed_kmh == 10.
                && (8.0..=12.0).contains(&o.speed_kmh)
                && d.prediction_u.is_some()
                && o.ready;
            if ready {
                let since = *self.stable_since.get_or_insert(o.observation_ns);
                if o.observation_ns - since >= 1_000_000_000 {
                    self.model_since = Some(o.observation_ns);
                    d.control_source = "model".into();
                    d.policy_reason = "model_handoff".into();
                }
            } else {
                self.stable_since = None;
            }
            if self.model_since.is_none()
                && armed.is_some_and(|s| now.saturating_sub(s) >= 10_000_000_000)
            {
                failure = Some("policy_handoff_timeout");
            }
        }
        if d.control_source == "model" && !out.complete {
            if let Some((t, b)) = d.model_throttle.zip(d.model_brake) {
                out.throttle = t;
                out.brake = b;
                if (t - pid.throttle).abs() > 0.05 || (b - pid.brake).abs() > 0.10 {
                    let start = *self.divergence_since.get_or_insert(o.observation_ns);
                    if o.observation_ns.saturating_sub(start) >= 100_000_000 {
                        failure = Some("policy_pid_divergence");
                    }
                } else {
                    self.divergence_since = None;
                }
            } else {
                failure.get_or_insert("policy_missing_prediction");
            }
        }
        if let Some(reason) = failure {
            self.latched = Some(reason.into());
        }
        if let Some(reason) = &self.latched {
            out.abort = true;
            out.complete = false;
            out.reason = reason.clone();
            d.policy_reason = reason.clone();
            d.control_source = "stop".into();
        }
        if out.abort || out.complete {
            out.steering = 0.;
            out.throttle = 0.;
            out.brake = 1.;
        }
        self.last = Some(key);
        self.cached = Some((out.clone(), d.clone()));
        (out, d)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use poleposition_baseline::{Observation, Output};
    use poleposition_policy::{FIELDS, Layer};
    use poleposition_state::model::Point;
    fn runtime(mode: &str, u: f32) -> Runtime {
        let model = Model {
            length: 400.,
            points: [(0., 0.), (100., 0.), (100., 100.), (0., 100.)]
                .iter()
                .enumerate()
                .map(|(i, (x, z))| Point {
                    index: i,
                    s_m: i as f64 * 100.,
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
        let mut layers: Vec<Layer> = [(19, 64), (64, 64), (64, 1)]
            .iter()
            .map(|(a, b)| Layer {
                weight: vec![vec![0.; *a]; *b],
                bias: vec![0.; *b],
            })
            .collect();
        layers[2].bias[0] = u.atanh();
        let network = Network {
            schema_version: 1,
            feature_names: FIELDS.iter().map(|v| v.to_string()).collect(),
            mean: vec![0.; 19],
            std: vec![1.; 19],
            layers,
            throttle_scale: 0.2,
            brake_scale: 0.6,
        };
        Runtime::new(
            Config {
                model: "unused".into(),
                mode: mode.into(),
                model_seconds: 5.,
                previous_run: None,
            },
            network,
            model,
        )
        .unwrap()
    }
    fn observation(i: u64) -> Observation {
        Observation {
            sequence: i + 1,
            observation_ns: i * 10_000_000,
            tracking_epoch: 1,
            ready: true,
            in_pit: false,
            gear_raw: 2,
            speed_kmh: 10.,
            x: 0.,
            z: 0.,
            heading_rad: 0.,
            heading_error_rad: 0.,
            projection_distance_m: 0.,
            lateral_error_m: 0.,
            s_m: i as f64,
            accumulated_m: i as f64,
        }
    }
    fn pid() -> Output {
        Output {
            distance_travelled_m: Some(1.),
            lap_fraction: None,
            steering: 0.03,
            throttle: 0.02,
            brake: 0.,
            target_speed_kmh: 10.,
            target_x: None,
            target_z: None,
            requested_curvature: None,
            steering_unclamped: None,
            reason: "tracking".into(),
            abort: false,
            complete: false,
        }
    }
    #[test]
    fn shadow_never_sends_model_and_waiting_never_hands_off() {
        for mode in ["shadow", "drive"] {
            let mut r = runtime(mode, 0.9);
            for i in 0..400 {
                let o = observation(i);
                let p = pid();
                let (out, d) = r.update(
                    &o,
                    1000.,
                    &p,
                    o.observation_ns,
                    if mode == "shadow" { Some(0) } else { None },
                );
                assert_eq!(out, p);
                assert_ne!(d.control_source, "model");
            }
        }
    }
    #[test]
    fn handoff_is_stable_one_second_and_finishes_duration() {
        let mut r = runtime("drive", 0.1);
        for i in 0..=650 {
            let o = observation(i);
            let (out, d) = r.update(&o, 1000., &pid(), o.observation_ns, Some(0));
            if i < 150 {
                assert_ne!(d.control_source, "model");
            } else if i < 650 {
                assert_eq!(d.control_source, "model");
                assert!(!out.abort && !out.complete);
                assert_eq!(out.steering, 0.03);
            } else {
                assert!(out.complete);
                assert_eq!(out.reason, "policy_duration_complete");
                assert_eq!((out.steering, out.throttle, out.brake), (0., 0., 1.));
            }
        }
    }
    #[test]
    fn mismatch_100ms_latches_stop_and_does_not_resume() {
        let mut r = runtime("drive", 0.9);
        for i in 0..200 {
            let o = observation(i);
            let (out, _) = r.update(&o, 1000., &pid(), o.observation_ns, Some(0));
            if i >= 160 {
                assert!(out.abort);
                assert_eq!(out.reason, "policy_pid_divergence");
                assert_eq!(out.throttle, 0.);
            }
        }
    }
    #[test]
    fn history_loss_after_takeover_aborts() {
        let mut r = runtime("drive", 0.1);
        for i in 0..180 {
            let o = observation(i);
            r.update(&o, 1000., &pid(), o.observation_ns, Some(0));
        }
        let o = observation(200);
        let (out, _) = r.update(&o, 1000., &pid(), o.observation_ns, Some(0));
        assert!(out.abort);
        assert_eq!(out.reason, "policy_history_lost");
    }
    #[test]
    fn handoff_timeout_and_nonfinite_inference_fail_closed() {
        let mut r = runtime("drive", 0.1);
        for i in 0..=1000 {
            let mut o = observation(i);
            o.speed_kmh = 5.;
            let (out, _) = r.update(&o, 1000., &pid(), o.observation_ns, Some(0));
            if i == 1000 {
                assert_eq!(out.reason, "policy_handoff_timeout");
                assert!(out.abort);
            }
        }
        let mut r = runtime("shadow", 0.1);
        let o = observation(0);
        let (out, _) = r.update(&o, f64::NAN, &pid(), 0, Some(0));
        assert!(out.abort);
    }
}
