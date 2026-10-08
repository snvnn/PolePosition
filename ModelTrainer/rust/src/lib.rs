//! Causal features and a fixed, small CPU policy. No device or game access.
pub mod features_v2;
pub mod v2;
use poleposition_state::model::{Model, Result, hash, json};
use serde::{Deserialize, Serialize};
use std::{collections::VecDeque, path::Path};

pub const FIELDS: [&str; 19] = [
    "speed_kmh",
    "target_speed_kmh",
    "speed_error_kmh",
    "rpm",
    "lateral_error_m",
    "heading_error_sin",
    "heading_error_cos",
    "reference_grade_pct",
    "curvature_0_per_m",
    "curvature_10_per_m",
    "curvature_20_per_m",
    "curvature_40_per_m",
    "curvature_80_per_m",
    "speed_100ms_kmh",
    "error_100ms_kmh",
    "speed_200ms_kmh",
    "error_200ms_kmh",
    "speed_500ms_kmh",
    "error_500ms_kmh",
];
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Frame {
    pub sequence: u64,
    pub observation_ns: u64,
    pub tracking_epoch: u64,
    pub ready: bool,
    pub speed_kmh: f64,
    pub target_speed_kmh: f64,
    pub rpm: f64,
    pub lateral_error_m: f64,
    pub heading_error_rad: f64,
    pub s_m: f64,
}
#[derive(Default)]
pub struct Features {
    history: VecDeque<Frame>,
    cached: Option<[f32; 19]>,
}
impl Features {
    pub fn reset(&mut self) {
        self.history.clear();
        self.cached = None;
    }
    pub fn update(&mut self, f: &Frame, model: &Model) -> Result<Option<[f32; 19]>> {
        if [
            f.speed_kmh,
            f.target_speed_kmh,
            f.rpm,
            f.lateral_error_m,
            f.heading_error_rad,
            f.s_m,
        ]
        .iter()
        .any(|v| !v.is_finite())
            || f.speed_kmh < 0.
            || f.sequence == 0
        {
            self.reset();
            return Err("Invalid feature observation".into());
        }
        if !f.ready {
            self.reset();
            return Ok(None);
        }
        if let Some(last) = self.history.back() {
            if f.tracking_epoch != last.tracking_epoch {
                self.reset();
            } else if f.sequence == last.sequence {
                if f != last {
                    self.reset();
                    return Err("Changed duplicate feature input".into());
                }
                return Ok(self.cached);
            } else if f.sequence < last.sequence || f.observation_ns <= last.observation_ns {
                self.reset();
                return Err("Feature time/sequence reversal".into());
            } else if f.observation_ns - last.observation_ns >= 100_000_000 {
                self.reset();
            }
        }
        self.history.push_back(f.clone());
        while self
            .history
            .front()
            .is_some_and(|x| f.observation_ns - x.observation_ns > 620_000_000)
        {
            self.history.pop_front();
        }
        let mut v = [0f32; 19];
        v[..8].copy_from_slice(&[
            f.speed_kmh as f32,
            f.target_speed_kmh as f32,
            (f.target_speed_kmh - f.speed_kmh) as f32,
            f.rpm as f32,
            f.lateral_error_m as f32,
            f.heading_error_rad.sin() as f32,
            f.heading_error_rad.cos() as f32,
            poleposition_baseline::grade_pct(model, f.s_m) as f32,
        ]);
        for (i, d) in [0., 10., 20., 40., 80.].iter().enumerate() {
            v[8 + i] = model.curvature(f.s_m + d) as f32;
        }
        self.cached = None;
        for (i, lag) in [100_000_000, 200_000_000, 500_000_000].iter().enumerate() {
            let Some(t) = f.observation_ns.checked_sub(*lag) else {
                return Ok(None);
            };
            let Some(old) = self.history.iter().rev().find(|x| x.observation_ns <= t) else {
                return Ok(None);
            };
            if t - old.observation_ns > 20_000_000 {
                return Ok(None);
            }
            v[13 + 2 * i] = old.speed_kmh as f32;
            v[14 + 2 * i] = (old.target_speed_kmh - old.speed_kmh) as f32;
        }
        if v.iter().any(|x| !x.is_finite()) {
            return Err("Nonfinite features".into());
        }
        self.cached = Some(v);
        Ok(Some(v))
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Layer {
    pub weight: Vec<Vec<f32>>,
    pub bias: Vec<f32>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Network {
    pub schema_version: u32,
    pub feature_names: Vec<String>,
    pub mean: Vec<f32>,
    pub std: Vec<f32>,
    pub layers: Vec<Layer>,
    pub throttle_scale: f32,
    pub brake_scale: f32,
}
impl Network {
    pub fn validate(&self) -> Result<()> {
        if self.schema_version != 1
            || self.feature_names != FIELDS
            || self.mean.len() != 19
            || self.std.len() != 19
            || self.throttle_scale != 0.20
            || self.brake_scale != 0.60
            || self.layers.len() != 3
            || self.mean.iter().any(|x| !x.is_finite())
            || self.std.iter().any(|x| !x.is_finite() || *x <= 0.)
        {
            return Err("Invalid policy schema/features/scales".into());
        }
        for (layer, (input, output)) in self.layers.iter().zip([(19, 64), (64, 64), (64, 1)]) {
            if layer.weight.len() != output
                || layer.bias.len() != output
                || layer.bias.iter().any(|x| !x.is_finite())
                || layer
                    .weight
                    .iter()
                    .any(|r| r.len() != input || r.iter().any(|x| !x.is_finite()))
            {
                return Err("Invalid policy layer".into());
            }
        }
        Ok(())
    }
    pub fn predict(&self, features: &[f32; 19]) -> Result<f32> {
        if features.iter().any(|x| !x.is_finite()) {
            return Err("Invalid inference input".into());
        }
        let mut x: Vec<f32> = features
            .iter()
            .zip(&self.mean)
            .zip(&self.std)
            .map(|((x, m), s)| (x - m) / s)
            .collect();
        if x.iter().any(|v| !v.is_finite()) {
            return Err("Nonfinite normalized features".into());
        }
        for (i, layer) in self.layers.iter().enumerate() {
            let next: Vec<f32> = layer
                .weight
                .iter()
                .zip(&layer.bias)
                .map(|(w, b)| w.iter().zip(&x).fold(*b, |a, (w, x)| a + w * x))
                .collect();
            if next.iter().any(|v| !v.is_finite()) {
                return Err("Nonfinite policy layer".into());
            }
            x = next
                .into_iter()
                .map(|z| if i == 2 { z.tanh() } else { z.max(0.) })
                .collect();
        }
        if !x[0].is_finite() {
            return Err("Nonfinite prediction".into());
        }
        Ok(x[0])
    }
}
pub fn pedals(u: f32) -> Result<(f64, f64)> {
    if !u.is_finite() || !(-1.0..=1.0).contains(&u) {
        return Err("Invalid policy output".into());
    }
    Ok((f64::from(u.max(0.)) * 0.20, f64::from((-u).max(0.)) * 0.60))
}
pub fn load(folder: &Path, approved: bool) -> Result<Network> {
    let m = json(&folder.join("manifest.json"))?;
    if m["kind"] != "policy_export"
        || m["schema_version"] != 1
        || m["complete"] != true
        || m["model_sha256"] != hash(&folder.join("model.json"))?
    {
        return Err("Incomplete/changed policy export".into());
    }
    if approved {
        let outputs = m["output_sha256"]
            .as_object()
            .ok_or("Missing policy inventory")?;
        let base = folder.canonicalize()?;
        for (name, expected) in outputs {
            let p = folder.join(name).canonicalize()?;
            if !p.starts_with(&base) || *expected != hash(&p)? {
                return Err("Changed policy output".into());
            }
        }
        for name in [
            "model.json",
            "evaluations.json",
            "parity-input.jsonl",
            "parity-output.jsonl",
        ] {
            if !outputs.contains_key(name) {
                return Err("Incomplete policy inventory".into());
            }
        }
        if json(&folder.join("evaluations.json"))? != m["evaluations"] {
            return Err("Policy evaluation mismatch".into());
        }
        if m["eligible_for_live"] != true
            || m["parity_max_abs"]
                .as_f64()
                .is_none_or(|v| !(0. ..=1e-5).contains(&v))
        {
            return Err("Policy evaluation/parity gate not passed".into());
        }
        for split in ["validation", "test"] {
            let e = &m["evaluations"][split];
            if e["passed"] != true
                || e["missing_classes"]
                    .as_array()
                    .is_none_or(|a| !a.is_empty())
                || ["throttle", "brake", "coast"]
                    .iter()
                    .any(|k| e["classes"][k].as_u64().is_none_or(|n| n == 0))
                || e["throttle_mae"]
                    .as_f64()
                    .is_none_or(|v| !(0.0..=0.01).contains(&v))
                || e["brake_mae"]
                    .as_f64()
                    .is_none_or(|v| !(0.0..=0.02).contains(&v))
                || e["throttle_p95"]
                    .as_f64()
                    .is_none_or(|v| !(0.0..=0.03).contains(&v))
                || e["brake_p95"]
                    .as_f64()
                    .is_none_or(|v| !(0.0..=0.05).contains(&v))
                || e["macro_mae"]
                    .as_f64()
                    .zip(e["baseline_macro_mae"].as_f64())
                    .is_none_or(|(a, b)| a < 0. || a >= b)
            {
                return Err("Policy accuracy gate not passed".into());
            }
        }
    }
    let n: Network = serde_json::from_value(json(&folder.join("model.json"))?)?;
    n.validate()?;
    Ok(n)
}

#[cfg(test)]
mod tests {
    use super::*;
    use poleposition_state::model::Point;
    fn model() -> Model {
        Model {
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
        }
    }
    fn frame(i: u64) -> Frame {
        Frame {
            sequence: i + 1,
            observation_ns: i * 10_000_000,
            tracking_epoch: 1,
            ready: true,
            speed_kmh: i as f64,
            target_speed_kmh: 10.,
            rpm: 1000.,
            lateral_error_m: 0.,
            heading_error_rad: 0.,
            s_m: 399. + i as f64,
        }
    }
    #[test]
    fn causal_history_duplicate_and_seam() {
        let mut f = Features::default();
        let m = model();
        for i in 0..50 {
            assert!(f.update(&frame(i), &m).unwrap().is_none());
        }
        let v = f.update(&frame(50), &m).unwrap().unwrap();
        assert_eq!([v[13], v[15], v[17]], [40., 30., 0.]);
        assert_eq!(f.update(&frame(50), &m).unwrap(), Some(v));
        let mut bad = frame(50);
        bad.rpm = 2.;
        assert!(f.update(&bad, &m).is_err());
    }
    #[test]
    fn gap_reset_reversal_and_invalid() {
        let mut f = Features::default();
        let m = model();
        for i in 0..70 {
            f.update(&frame(i), &m).unwrap();
        }
        assert!(f.update(&frame(80), &m).unwrap().is_none());
        assert!(f.update(&frame(79), &m).is_err());
        let mut x = frame(81);
        x.speed_kmh = f64::NAN;
        assert!(f.update(&x, &m).is_err());
        x = frame(82);
        x.tracking_epoch = 2;
        assert!(f.update(&x, &m).unwrap().is_none());
    }
    #[test]
    fn past_tolerance_no_future_fill() {
        let mut f = Features::default();
        let m = model();
        for i in 0..=80 {
            if !(28..=30).contains(&i) {
                let out = f.update(&frame(i), &m).unwrap();
                if i == 80 {
                    assert!(out.is_none());
                }
            }
        }
    }
    #[test]
    fn bounds_and_brake_priority_by_construction() {
        for u in [-1., -0.5, 0., 0.5, 1.] {
            let (t, b) = pedals(u).unwrap();
            assert!(t == 0. || b == 0.);
        }
        assert!(pedals(f32::NAN).is_err());
        assert!(pedals(1.1).is_err());
        assert_eq!(pedals(1.).unwrap(), (0.20, 0.));
        assert_eq!(pedals(-1.).unwrap(), (0., 0.60));
    }
    fn network() -> Network {
        Network {
            schema_version: 1,
            feature_names: FIELDS.iter().map(|x| x.to_string()).collect(),
            mean: vec![0.; 19],
            std: vec![1.; 19],
            layers: [(19, 64), (64, 64), (64, 1)]
                .iter()
                .map(|(a, b)| Layer {
                    weight: vec![vec![0.; *a]; *b],
                    bias: vec![0.; *b],
                })
                .collect(),
            throttle_scale: 0.20,
            brake_scale: 0.60,
        }
    }
    #[test]
    fn network_shape_order_and_finite_intermediates() {
        let mut n = network();
        n.validate().unwrap();
        assert_eq!(n.predict(&[0.; 19]).unwrap(), 0.);
        n.feature_names.swap(0, 1);
        assert!(n.validate().is_err());
        n = network();
        n.layers[1].weight[0].pop();
        assert!(n.validate().is_err());
        n = network();
        n.std[0] = f32::MIN_POSITIVE;
        let mut x = [0.; 19];
        x[0] = f32::MAX;
        assert!(n.predict(&x).is_err());
        n = network();
        n.layers[0].weight[0][0] = f32::MAX;
        x[0] = 2.;
        assert!(n.predict(&x).is_err());
        n.layers[0].weight[0][0] = f32::NAN;
        assert!(n.validate().is_err());
    }
    #[test]
    fn loader_rejects_gate_incomplete_and_hash_tamper() {
        let folder = std::env::temp_dir().join(format!(
            "poleposition-policy-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&folder).unwrap();
        let write = |name: &str, value: &serde_json::Value| {
            std::fs::write(folder.join(name), serde_json::to_vec(value).unwrap()).unwrap()
        };
        write("model.json", &serde_json::to_value(network()).unwrap());
        let e = serde_json::json!({"passed":true,"missing_classes":[],"classes":{"throttle":1,"brake":1,"coast":1},"throttle_mae":0.,"brake_mae":0.,"throttle_p95":0.,"brake_p95":0.,"macro_mae":0.,"baseline_macro_mae":0.1});
        let es = serde_json::json!({"validation":e,"test":e});
        write("evaluations.json", &es);
        for name in ["parity-input.jsonl", "parity-output.jsonl"] {
            std::fs::write(folder.join(name), b"0\n").unwrap();
        }
        let outputs: serde_json::Map<String, serde_json::Value> = [
            "model.json",
            "evaluations.json",
            "parity-input.jsonl",
            "parity-output.jsonl",
        ]
        .iter()
        .map(|name| {
            (
                name.to_string(),
                serde_json::json!(hash(&folder.join(name)).unwrap()),
            )
        })
        .collect();
        let mut m = serde_json::json!({"schema_version":1,"kind":"policy_export","complete":true,"eligible_for_live":true,"model_sha256":hash(&folder.join("model.json")).unwrap(),"parity_max_abs":0.,"evaluations":es,"output_sha256":outputs});
        write("manifest.json", &m);
        assert!(load(&folder, true).is_ok());
        m["complete"] = false.into();
        write("manifest.json", &m);
        assert!(load(&folder, false).is_err());
        m["complete"] = true.into();
        m["eligible_for_live"] = false.into();
        write("manifest.json", &m);
        assert!(load(&folder, true).is_err());
        m["eligible_for_live"] = true.into();
        write("manifest.json", &m);
        std::fs::write(folder.join("model.json"), b"changed").unwrap();
        assert!(load(&folder, true).is_err());
        std::fs::remove_dir_all(folder).unwrap();
    }
}
pub mod gamepad;
