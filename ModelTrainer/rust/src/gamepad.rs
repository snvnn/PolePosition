//! Independent 3-action gamepad model; wheel/SAC formats are deliberately rejected.
use crate::Layer;
use poleposition_state::model::Result;
use serde::{Deserialize, Serialize};
pub fn names() -> Vec<String> {
    let mut n = crate::features_v2::names();
    n[19] = "previous_stick_x".into();
    n[20] = "previous_rt".into();
    n[21] = "previous_lt".into();
    n
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Network {
    pub semantics: String,
    pub feature_names: Vec<String>,
    pub action_names: Vec<String>,
    pub mean: Vec<f32>,
    pub std: Vec<f32>,
    pub layers: Vec<Layer>,
}
impl Network {
    pub fn validate(&self) -> Result<()> {
        if self.semantics != "gamepad_v1"
            || self.feature_names != names()
            || self.action_names != ["stick_x", "rt", "lt"]
            || self.mean.len() != 35
            || self.std.len() != 35
            || self.layers.len() != 3
            || self.mean.iter().any(|v| !v.is_finite())
            || self.std.iter().any(|v| !v.is_finite() || *v <= 0.)
        {
            return Err("Invalid gamepad_v1 semantics/normalizer".into());
        }
        for (l, (a, b)) in self.layers.iter().zip([(35, 128), (128, 128), (128, 3)]) {
            if l.bias.len() != b
                || l.weight.len() != b
                || l.bias.iter().any(|v| !v.is_finite())
                || l.weight
                    .iter()
                    .any(|r| r.len() != a || r.iter().any(|v| !v.is_finite()))
            {
                return Err("Invalid gamepad network dimensions or values".into());
            }
        }
        Ok(())
    }
    pub fn predict(&self, features: &[f32]) -> Result<[f32; 3]> {
        self.validate()?;
        if features.len() != 35 || features.iter().any(|v| !v.is_finite()) {
            return Err("Invalid gamepad features".into());
        }
        let mut x: Vec<f32> = features
            .iter()
            .zip(&self.mean)
            .zip(&self.std)
            .map(|((a, b), c)| (a - b) / c)
            .collect();
        for (i, l) in self.layers.iter().enumerate() {
            x = l
                .weight
                .iter()
                .zip(&l.bias)
                .map(|(r, b)| r.iter().zip(&x).fold(*b, |s, (a, x)| s + a * x))
                .collect();
            if x.iter().any(|v| !v.is_finite()) {
                return Err("Nonfinite gamepad inference".into());
            }
            if i < 2 {
                for v in &mut x {
                    *v = v.max(0.);
                }
            }
        }
        Ok([
            x[0].tanh(),
            1. / (1. + (-x[1]).exp()),
            1. / (1. + (-x[2]).exp()),
        ])
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn model_contract_and_overlap() {
        let mut n = Network {
            semantics: "gamepad_v1".into(),
            feature_names: names(),
            action_names: vec!["stick_x".into(), "rt".into(), "lt".into()],
            mean: vec![0.; 35],
            std: vec![1.; 35],
            layers: [(35, 128), (128, 128), (128, 3)]
                .iter()
                .map(|(a, b)| Layer {
                    weight: vec![vec![0.; *a]; *b],
                    bias: vec![0.; *b],
                })
                .collect(),
        };
        assert_eq!(n.predict(&[0.; 35]).unwrap(), [0., 0.5, 0.5]);
        n.semantics = "wheel".into();
        assert!(n.validate().is_err());
    }
}
