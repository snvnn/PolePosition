//! Native v2 inference alongside PID. This module never supplies vehicle commands.
use crate::{Axes, Result};
use poleposition_policy::{
    Frame,
    features_v2::{FeaturesV2, Input, SpeedProfile},
};
use poleposition_state::model::{Model, Prepared, hash, json, verify_map};
use serde::{Deserialize, Serialize};
use std::{
    collections::VecDeque,
    path::{Path, PathBuf},
    time::Instant,
};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub training: String,
}

pub struct Assets {
    network: poleposition_policy::v2::Network,
    speed: SpeedProfile,
    pub inputs: Vec<PathBuf>,
}

fn artifact(folder: &Path, kind: &str) -> Result<serde_json::Value> {
    let m = json(&folder.join("manifest.json"))?;
    if m["schema_version"] != 1 || m["kind"] != kind || m["complete"] != true {
        return Err("Incomplete/wrong v2 artifact".into());
    }
    let required: &[&str] = match kind {
        "sac_pretraining" => &["summary.json", "normalizer.json", "model.zip"],
        "teacher_features_v2" => &["speed-profile.json", "normalizer.json"],
        "sac_mean_candidate" => &[
            "model.json",
            "parity.json",
            "parity-input.jsonl",
            "parity-output.jsonl",
        ],
        _ => return Err("Unsupported v2 artifact".into()),
    };
    if required
        .iter()
        .any(|name| m["output_sha256"][*name].as_str().is_none())
    {
        return Err("Missing required v2 artifact inventory".into());
    }
    let base = folder.canonicalize()?;
    for (name, expected) in m["output_sha256"].as_object().ok_or("Missing v2 outputs")? {
        let path = folder.join(name).canonicalize()?;
        if !path.starts_with(&base) || *expected != hash(&path)? {
            return Err("Changed/escaping v2 output".into());
        }
    }
    // Historical exporter executables are rebuilt during development. Model/data
    // inventories stay immutable; replay numeric evidence with this runtime below.
    let inputs = m["input_sha256"].as_object().ok_or("Missing v2 inputs")?;
    let stable: serde_json::Map<String, serde_json::Value> = inputs
        .iter()
        .filter(|(p, _)| Path::new(p).extension().is_none_or(|e| e != "exe"))
        .map(|(p, h)| (p.clone(), h.clone()))
        .collect();
    verify_map(&serde_json::Value::Object(stable), None)?;
    Ok(m)
}

impl Config {
    pub fn verify(&self, prepared: &Prepared) -> Result<Assets> {
        let training = Path::new(&self.training);
        let m = artifact(training, "sac_pretraining")?;
        let dataset = Path::new(m["dataset"].as_str().ok_or("Missing v2 dataset")?);
        let dm = artifact(dataset, "teacher_features_v2")?;
        if m["feature_schema_version"] != 2
            || dm["feature_schema_version"] != 2
            || dm["policy_training_ready"] != true
        {
            return Err("Require prepared v2 pretraining".into());
        }
        let prepared_path = prepared.path.join("manifest.json");
        if dm["input_sha256"][prepared_path.to_string_lossy().as_ref()] != hash(&prepared_path)? {
            // Canonical Windows extended paths and ordinary paths are equivalent.
            let matches = dm["input_sha256"].as_object().is_some_and(|inputs| {
                inputs.iter().any(|(p, h)| {
                    Path::new(p).canonicalize().ok() == prepared_path.canonicalize().ok()
                        && h.as_str() == hash(&prepared_path).ok().as_deref()
                })
            });
            if !matches {
                return Err("V2 prepared path differs from training".into());
            }
        }
        let summary = json(&training.join("summary.json"))?;
        let candidate = Path::new(
            summary["candidate"]
                .as_str()
                .ok_or("Missing v2 candidate")?,
        );
        let cm = artifact(candidate, "sac_mean_candidate")?;
        let parity = json(&candidate.join("parity.json"))?;
        if cm["eligible_for_live"] != false
            || parity["passed"] != true
            || parity["samples"].as_u64().is_none_or(|n| n < 100)
            || parity["max_absolute_error"]
                .as_f64()
                .is_none_or(|v| !(0. ..=1e-5).contains(&v))
            || cm["model_sha256"] != hash(&candidate.join("model.json"))?
        {
            return Err("V2 numeric parity evidence missing".into());
        }
        for name in ["model.zip", "normalizer.json"] {
            let p = training.join(name).canonicalize()?;
            if !cm["input_sha256"].as_object().is_some_and(|inputs| {
                inputs.iter().any(|(path, h)| {
                    Path::new(path).canonicalize().ok().as_ref() == Some(&p)
                        && h.as_str() == hash(&p).ok().as_deref()
                })
            }) {
                return Err("V2 candidate belongs to another training result".into());
            }
        }
        let value = json(&candidate.join("model.json"))?;
        let normalizer = json(&training.join("normalizer.json"))?;
        if value["feature_names"]
            != serde_json::to_value(poleposition_policy::features_v2::names())?
            || value["mean"] != normalizer["mean"]
            || value["std"] != normalizer["std"]
        {
            // Export rounds normalizer values to float32; compare after identical conversion below.
            let same = ["mean", "std"].iter().all(|k| {
                let a: Option<Vec<f32>> = value[*k]
                    .as_array()
                    .and_then(|a| a.iter().map(|x| x.as_f64().map(|v| v as f32)).collect());
                let b: Option<Vec<f32>> = normalizer[*k]
                    .as_array()
                    .and_then(|a| a.iter().map(|x| x.as_f64().map(|v| v as f32)).collect());
                a.is_some() && a == b
            });
            if !same
                || value["feature_names"]
                    != serde_json::to_value(poleposition_policy::features_v2::names())?
            {
                return Err("V2 feature/normalizer mismatch".into());
            }
        }
        let network: poleposition_policy::v2::Network = serde_json::from_value(value)?;
        network.validate()?;
        let input = std::fs::read_to_string(candidate.join("parity-input.jsonl"))?;
        let output = std::fs::read_to_string(candidate.join("parity-output.jsonl"))?;
        if input.lines().count() != output.lines().count() || input.lines().count() < 100 {
            return Err("Invalid archived parity rows".into());
        }
        for (x, y) in input.lines().zip(output.lines()) {
            let x: Vec<f32> = serde_json::from_str(x)?;
            let y: [f32; 2] = serde_json::from_str(y)?;
            let actual = network.predict(&x)?;
            if actual
                .iter()
                .zip(y)
                .any(|(a, b)| !b.is_finite() || (*a - b).abs() > 1e-5)
            {
                return Err("Current native runtime differs from archived v2 parity".into());
            }
        }
        let speed: SpeedProfile =
            serde_json::from_value(json(&dataset.join("speed-profile.json"))?)?;
        speed.validate(&prepared.model)?;
        let mut inputs = Vec::new();
        for (folder, meta) in [(training, &m), (dataset, &dm), (candidate, &cm)] {
            inputs.push(folder.join("manifest.json").canonicalize()?);
            for name in meta["output_sha256"]
                .as_object()
                .ok_or("Missing outputs")?
                .keys()
            {
                if [
                    "model.json",
                    "normalizer.json",
                    "speed-profile.json",
                    "summary.json",
                    "parity.json",
                ]
                .contains(&name.as_str())
                {
                    inputs.push(folder.join(name).canonicalize()?);
                }
            }
        }
        Ok(Assets {
            network,
            speed,
            inputs,
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Applied {
    pub at_ns: u64,
    pub tracking_epoch: u64,
    pub axes: Axes,
}

#[derive(Default)]
pub struct AppliedHistory(VecDeque<Applied>);
impl AppliedHistory {
    pub fn push(&mut self, sample: Applied) {
        self.0.push_back(sample);
        while self.0.len() > 128 {
            self.0.pop_front();
        }
    }
    pub fn before(&self, observation_ns: u64, epoch: u64) -> Option<Applied> {
        self.0
            .iter()
            .rev()
            .find(|a| a.at_ns < observation_ns)
            .filter(|a| a.tracking_epoch == epoch && observation_ns - a.at_ns < 100_000_000)
            .cloned()
    }
}

pub struct Runtime {
    assets: Assets,
    model: Model,
    features: FeaturesV2,
    last_decision: Option<(u64, u64)>,
}
impl Runtime {
    pub fn new(assets: Assets, model: Model) -> Self {
        Self {
            assets,
            model,
            features: FeaturesV2::default(),
            last_decision: None,
        }
    }
    pub fn update(
        &mut self,
        c: &crate::ipc::Context,
        pid: &poleposition_baseline::Output,
        now: u64,
    ) -> Result<Option<crate::policy::Diagnostic>> {
        let (Some(o), Some(applied)) = (&c.path, &c.previous_applied) else {
            self.features.reset();
            self.last_decision = None;
            return Ok(None);
        };
        if !o.ready || c.health.gear_raw < 2 || c.armed_ns.is_none() || pid.abort || pid.complete {
            self.features.reset();
            self.last_decision = None;
            return Ok(None);
        }
        if applied.tracking_epoch != o.tracking_epoch {
            return Err("V2 applied input epoch mismatch".into());
        }
        let begin = Instant::now();
        let values = self.features.update(
            &Input {
                frame: Frame {
                    sequence: o.sequence,
                    observation_ns: o.observation_ns,
                    tracking_epoch: o.tracking_epoch,
                    ready: o.ready,
                    speed_kmh: o.speed_kmh,
                    target_speed_kmh: pid.target_speed_kmh,
                    rpm: c.health.rpm as f64,
                    lateral_error_m: o.lateral_error_m,
                    heading_error_rad: o.heading_error_rad,
                    s_m: o.s_m,
                },
                speed_cap_kmh: 10.,
                previous_axes: [
                    applied.axes.steering,
                    applied.axes.throttle,
                    applied.axes.brake,
                ],
                previous_applied_ns: applied.at_ns,
                gear_raw: c.health.gear_raw,
            },
            &self.model,
            &self.assets.speed,
        )?;
        let feature_ns = begin.elapsed().as_nanos() as u64;
        let Some(values) = values else {
            return Ok(None);
        };
        if self.last_decision.is_some_and(|(epoch, t)| {
            epoch == o.tracking_epoch && o.observation_ns.saturating_sub(t) < 20_000_000
        }) {
            return Ok(None);
        }
        let begin = Instant::now();
        let action = self.assets.network.predict(&values)?;
        let axes = poleposition_policy::v2::axes(action)?;
        let inference_ns = begin.elapsed().as_nanos() as u64;
        self.last_decision = Some((o.tracking_epoch, o.observation_ns));
        Ok(Some(crate::policy::Diagnostic {
            now_ns: now,
            observation_sequence: o.sequence,
            observation_ns: o.observation_ns,
            tracking_epoch: o.tracking_epoch,
            control_source: "pid_v2_shadow".into(),
            policy_reason: "v2_shadow_no_model_output".into(),
            features_json: serde_json::to_string(&values)?,
            prediction_u: Some(action[1]),
            model_throttle: Some(axes[1]),
            model_brake: Some(axes[2]),
            pid_throttle: pid.throttle,
            pid_brake: pid.brake,
            feature_ns,
            inference_ns: Some(inference_ns),
            model_elapsed_ns: 0,
            prediction_steering: Some(action[0]),
            feature_schema_version: Some(2),
        }))
    }
}
