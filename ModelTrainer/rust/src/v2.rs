//! Separate SAC deterministic-mean inference. Never changes the v1 model format.
use crate::Layer;
use poleposition_state::model::Result;
use serde::{Deserialize, Serialize};
use std::collections::HashSet;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Network {
    schema_version: u32,
    feature_names: Vec<String>,
    action_names: Vec<String>,
    mean: Vec<f32>,
    std: Vec<f32>,
    layers: Vec<Layer>,
}
impl Network {
    pub fn validate(&self) -> Result<()> {
        let width = self.feature_names.len();
        if self.schema_version != 2
            || width == 0
            || self.feature_names.iter().any(|s| s.trim().is_empty())
            || self.feature_names.iter().collect::<HashSet<_>>().len() != width
            || self.action_names != ["steering", "signed_acceleration"]
            || self.mean.len() != width
            || self.std.len() != width
            || self.layers.len() != 3
            || self.mean.iter().any(|v| !v.is_finite())
            || self.std.iter().any(|v| !v.is_finite() || *v <= 0.)
        {
            return Err("Invalid v2 policy/normalizer schema".into());
        }
        for (layer, (input, output)) in self.layers.iter().zip([(width, 128), (128, 128), (128, 2)])
        {
            if layer.bias.len() != output
                || layer.weight.len() != output
                || layer.bias.iter().any(|v| !v.is_finite())
                || layer
                    .weight
                    .iter()
                    .any(|r| r.len() != input || r.iter().any(|v| !v.is_finite()))
            {
                return Err("Invalid v2 SAC mean network".into());
            }
        }
        Ok(())
    }
    pub fn predict(&self, features: &[f32]) -> Result<[f32; 2]> {
        let x = self.latent(features)?;
        let mu = dense(&self.layers[2], &x)?;
        Ok([mu[0].tanh(), mu[1].tanh()])
    }
    fn latent(&self, features: &[f32]) -> Result<Vec<f32>> {
        self.validate()?;
        if features.len() != self.mean.len() || features.iter().any(|v| !v.is_finite()) {
            return Err("Invalid v2 observation".into());
        }
        let mut x: Vec<f32> = features
            .iter()
            .zip(&self.mean)
            .zip(&self.std)
            .map(|((v, m), s)| (v - m) / s)
            .collect();
        if x.iter().any(|v| !v.is_finite()) {
            return Err("Nonfinite v2 normalization".into());
        }
        for layer in self.layers.iter().take(2) {
            let next: Vec<f32> = layer
                .weight
                .iter()
                .zip(&layer.bias)
                .map(|(w, b)| w.iter().zip(&x).fold(*b, |sum, (w, x)| sum + w * x))
                .collect();
            if next.iter().any(|v| !v.is_finite()) {
                return Err("Nonfinite v2 policy layer".into());
            }
            x = next.into_iter().map(|v| v.max(0.)).collect();
        }
        Ok(x)
    }
}

fn dense(layer: &Layer, x: &[f32]) -> Result<Vec<f32>> {
    if layer.weight.len() != 2
        || layer.bias.len() != 2
        || layer
            .weight
            .iter()
            .any(|r| r.len() != x.len() || r.iter().any(|v| !v.is_finite()))
        || layer.bias.iter().any(|v| !v.is_finite())
    {
        return Err("Invalid SAC output branch".into());
    }
    let y: Vec<f32> = layer
        .weight
        .iter()
        .zip(&layer.bias)
        .map(|(w, b)| w.iter().zip(x).fold(*b, |s, (w, x)| s + w * x))
        .collect();
    if y.iter().any(|v| !v.is_finite()) {
        return Err("Nonfinite SAC output".into());
    }
    Ok(y)
}

/// Separate envelope: existing deterministic v2 files remain unchanged.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SacNetwork {
    pub schema_version: u32,
    pub mean_network: Network,
    pub log_std: Layer,
    pub log_std_bounds: [f32; 2],
}
impl SacNetwork {
    pub fn validate(&self) -> Result<()> {
        self.mean_network.validate()?;
        if self.schema_version != 1 || self.log_std_bounds != [-5., -2.] {
            return Err("Invalid SAC exploration envelope".into());
        }
        dense(&self.log_std, &[0.; 128])?;
        Ok(())
    }
    pub fn predict(&self, features: &[f32], epsilon: [f32; 2]) -> Result<[f32; 2]> {
        self.validate()?;
        if epsilon.iter().any(|v| !v.is_finite()) {
            return Err("Invalid SAC noise".into());
        }
        let latent = self.mean_network.latent(features)?;
        let mu = dense(&self.mean_network.layers[2], &latent)?;
        let log_std = dense(&self.log_std, &latent)?;
        Ok(std::array::from_fn(|i| {
            (mu[i] + log_std[i].clamp(-5., -2.).exp() * epsilon[i]).tanh()
        }))
    }
}

pub fn axes(action: [f32; 2]) -> Result<[f64; 3]> {
    if action
        .iter()
        .any(|v| !v.is_finite() || !(-1. ..=1.).contains(v))
    {
        return Err("Invalid v2 action".into());
    }
    Ok([
        f64::from(action[0]),
        f64::from(action[1].max(0.)),
        f64::from((-action[1]).max(0.)),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;
    fn network() -> Network {
        Network {
            schema_version: 2,
            feature_names: vec!["test_x".into(), "test_y".into()],
            action_names: vec!["steering".into(), "signed_acceleration".into()],
            mean: vec![0.; 2],
            std: vec![1.; 2],
            layers: vec![
                Layer {
                    weight: vec![vec![0.; 2]; 128],
                    bias: vec![0.; 128],
                },
                Layer {
                    weight: vec![vec![0.; 128]; 128],
                    bias: vec![0.; 128],
                },
                Layer {
                    weight: vec![vec![0.; 128]; 2],
                    bias: vec![0.; 2],
                },
            ],
        }
    }
    #[test]
    fn format_and_dimensions_are_distinct_from_v1() {
        let mut n = network();
        assert_eq!(n.predict(&[0., 0.]).unwrap(), [0., 0.]);
        n.schema_version = 1;
        assert!(n.validate().is_err());
        let mut n = network();
        n.layers[2].bias.pop();
        assert!(n.validate().is_err());
        let mut n = network();
        n.feature_names[1] = "test_x".into();
        assert!(n.validate().is_err());
    }
    #[test]
    fn finite_normalization_and_no_overlapping_pedals() {
        let mut n = network();
        n.std[0] = 0.;
        assert!(n.predict(&[1., 0.]).is_err());
        assert!(network().predict(&[f32::NAN, 0.]).is_err());
        assert_eq!(axes([0.5, -0.25]).unwrap(), [0.5, 0., 0.25]);
        assert_eq!(axes([-0.5, 0.25]).unwrap(), [-0.5, 0.25, 0.]);
        assert!(axes([2., 0.]).is_err());
    }
}
