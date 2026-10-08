//! Shared causal v2 features for offline preparation and future native collection.
use crate::{Features, Frame};
use poleposition_state::model::{Model, Result};
use serde::{Deserialize, Serialize};

pub fn names() -> Vec<String> {
    crate::FIELDS
        .iter()
        .map(|s| s.to_string())
        .chain(
            [
                "previous_steering",
                "previous_throttle",
                "previous_brake",
                "gear_raw",
                "speed_rate_kmh_s",
                "lateral_rate_m_s",
                "heading_rate_rad_s",
                "curvature_160_per_m",
                "curvature_320_per_m",
                "reference_speed_0_kmh",
                "reference_speed_10_kmh",
                "reference_speed_20_kmh",
                "reference_speed_40_kmh",
                "reference_speed_80_kmh",
                "reference_speed_160_kmh",
                "reference_speed_320_kmh",
            ]
            .iter()
            .map(|s| s.to_string()),
        )
        .collect()
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Input {
    pub frame: Frame,
    pub speed_cap_kmh: f64,
    pub previous_axes: [f64; 3],
    pub previous_applied_ns: u64,
    pub gear_raw: i32,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SpeedProfile {
    pub schema_version: u32,
    pub length_m: f64,
    pub s_m: Vec<f64>,
    pub speed_kmh: Vec<f64>,
}
impl SpeedProfile {
    pub fn validate(&self, model: &Model) -> Result<()> {
        if self.schema_version != 2
            || !self.length_m.is_finite()
            || (self.length_m - model.length).abs() > 1e-6
            || self.s_m.len() < 3
            || self.s_m.len() != self.speed_kmh.len()
            || self.s_m[0] != 0.
            || self.s_m.windows(2).any(|w| w[0] >= w[1])
            || self
                .s_m
                .iter()
                .any(|s| !s.is_finite() || *s < 0. || *s >= self.length_m)
            || self.speed_kmh.iter().any(|v| !v.is_finite() || *v < 0.)
        {
            return Err("Invalid train speed profile".into());
        }
        Ok(())
    }
    pub fn at(&self, s: f64, cap: f64) -> f64 {
        let s = s.rem_euclid(self.length_m);
        let i = self.s_m.partition_point(|x| *x <= s).saturating_sub(1);
        let j = (i + 1) % self.s_m.len();
        let end = if j == 0 { self.length_m } else { self.s_m[j] };
        let v = self.speed_kmh[i]
            + (self.speed_kmh[j] - self.speed_kmh[i]) * (s - self.s_m[i]) / (end - self.s_m[i]);
        v.min(cap)
    }
}

#[derive(Default)]
pub struct FeaturesV2 {
    independent_pedals: bool,
    base: Features,
    previous: Option<Input>,
    cached: Option<Vec<f32>>,
}
impl FeaturesV2 {
    pub fn gamepad() -> Self { Self { independent_pedals:true, ..Self::default() } }
    pub fn reset(&mut self) {
        self.base.reset();
        self.previous = None;
        self.cached = None;
    }
    pub fn update(
        &mut self,
        input: &Input,
        model: &Model,
        speed: &SpeedProfile,
    ) -> Result<Option<Vec<f32>>> {
        let result = self.compute(input, model, speed);
        if result.is_err() {
            self.reset();
        }
        result
    }
    fn compute(
        &mut self,
        input: &Input,
        model: &Model,
        speed: &SpeedProfile,
    ) -> Result<Option<Vec<f32>>> {
        let f = &input.frame;
        let a = input.previous_axes;
        if a.iter().any(|v| !v.is_finite())
            || !(-1. ..=1.).contains(&a[0])
            || !(0. ..=1.).contains(&a[1])
            || !(0. ..=1.).contains(&a[2])
            || (!self.independent_pedals && a[1] * a[2] != 0.)
            || input.previous_applied_ns >= f.observation_ns
            || f.observation_ns - input.previous_applied_ns >= 100_000_000
            || !(2..=9).contains(&input.gear_raw)
            || ![10., 20., 40., 80., 120., 160., 200., 240.].contains(&input.speed_cap_kmh)
            || !(0. ..=input.speed_cap_kmh).contains(&f.target_speed_kmh)
        {
            return Err("Invalid causal v2 input/action/stage".into());
        }
        if let Some(p) = &self.previous
            && p.frame.tracking_epoch == f.tracking_epoch
            && p.frame.sequence == f.sequence
        {
            if p != input {
                return Err("Changed duplicate v2 input".into());
            }
            return Ok(self.cached.clone());
        }
        let base = self.base.update(f, model)?;
        let old = self.previous.replace(input.clone());
        self.cached = None;
        let (Some(base), Some(old)) = (base, old) else {
            return Ok(None);
        };
        if !f.ready || old.frame.tracking_epoch != f.tracking_epoch {
            return Ok(None);
        }
        let dt = (f.observation_ns - old.frame.observation_ns) as f64 / 1e9;
        let mut v = base.to_vec();
        v.extend(a.map(|x| x as f32));
        v.extend([
            input.gear_raw as f32,
            ((f.speed_kmh - old.frame.speed_kmh) / dt) as f32,
            ((f.lateral_error_m - old.frame.lateral_error_m) / dt) as f32,
            (poleposition_state::wrap(f.heading_error_rad - old.frame.heading_error_rad) / dt)
                as f32,
            model.curvature(f.s_m + 160.) as f32,
            model.curvature(f.s_m + 320.) as f32,
        ]);
        for d in [0., 10., 20., 40., 80., 160., 320.] {
            v.push(speed.at(f.s_m + d, input.speed_cap_kmh) as f32);
        }
        if v.iter().any(|x| !x.is_finite()) {
            return Err("Nonfinite v2 feature".into());
        }
        self.cached = Some(v.clone());
        Ok(Some(v))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use poleposition_state::model::Point;

    fn fixture() -> (Model, SpeedProfile) {
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
        let speed = SpeedProfile {
            schema_version: 2,
            length_m: 400.,
            s_m: vec![0., 100., 200., 300.],
            speed_kmh: vec![80., 100., 120., 160.],
        };
        (model, speed)
    }
    fn input(i: u64) -> Input {
        Input {
            frame: Frame {
                sequence: i + 1,
                observation_ns: 1_000_000_000 + i * 10_000_000,
                tracking_epoch: 1,
                ready: true,
                speed_kmh: 10. + i as f64 / 100.,
                target_speed_kmh: 10.,
                rpm: 2000.,
                lateral_error_m: 0.,
                heading_error_rad: 0.,
                s_m: 399.,
            },
            speed_cap_kmh: 10.,
            previous_axes: [0.2, 0.1, 0.],
            previous_applied_ns: 995_000_000 + i * 10_000_000,
            gear_raw: 2,
        }
    }
    #[test]
    fn causal_history_rates_wrap_and_epoch_reset() {
        let (model, speed) = fixture();
        speed.validate(&model).unwrap();
        assert_eq!(speed.at(400., 240.), 80.);
        assert_eq!(speed.at(0., 10.), 10.);
        let mut features = FeaturesV2::default();
        for i in 0..50 {
            assert!(
                features
                    .update(&input(i), &model, &speed)
                    .unwrap()
                    .is_none()
            );
        }
        let values = features
            .update(&input(50), &model, &speed)
            .unwrap()
            .unwrap();
        assert_eq!(values.len(), names().len());
        assert_eq!(&values[19..23], &[0.2, 0.1, 0., 2.]);
        assert!((values[23] - 1.).abs() < 1e-5);
        assert!(values[28..].iter().all(|v| *v == 10.));
        assert_eq!(
            features
                .update(&input(50), &model, &speed)
                .unwrap()
                .unwrap(),
            values
        );
        let mut changed = input(50);
        changed.previous_axes[0] = 0.3;
        assert!(features.update(&changed, &model, &speed).is_err());
        assert!(
            features
                .update(&input(51), &model, &speed)
                .unwrap()
                .is_none()
        );
        let mut next = input(52);
        next.frame.tracking_epoch = 2;
        assert!(features.update(&next, &model, &speed).unwrap().is_none());
    }
    #[test]
    fn gamepad_history_preserves_both_triggers() {
        let (model,speed)=fixture();let mut f=FeaturesV2::gamepad();
        for i in 0..=50 {
            let mut x=input(i);x.previous_axes=[-0.6,0.7,0.8];
            let result=f.update(&x,&model,&speed).unwrap();
            if i==50 {assert_eq!(&result.unwrap()[19..22],&[-0.6,0.7,0.8]);}
        }
        let mut x=input(0);x.previous_axes=[0.,0.7,0.8];
        assert!(FeaturesV2::default().update(&x,&model,&speed).is_err());
    }
    #[test]
    fn future_axes_invalid_profile_and_departure_ramp() {
        let (model, mut speed) = fixture();
        let mut features = FeaturesV2::default();
        let mut value = input(0);
        value.frame.target_speed_kmh = 0.;
        assert!(features.update(&value, &model, &speed).is_ok());
        value = input(1);
        value.previous_applied_ns = value.frame.observation_ns;
        assert!(features.update(&value, &model, &speed).is_err());
        speed.s_m[1] = 0.;
        assert!(speed.validate(&model).is_err());
    }
}
