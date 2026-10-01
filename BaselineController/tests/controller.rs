use poleposition_baseline::{Config, Controller, Observation, target};
use poleposition_state::{
    model::{Model, Point},
    wrap,
};
fn config() -> Config {
    toml::from_str(include_str!("../config.toml")).unwrap()
}
fn circle(reverse: bool) -> Model {
    let n = 720;
    let r = 100.;
    let sign = if reverse { -1. } else { 1. };
    let mut s = 0.;
    let mut points = Vec::new();
    let ds = 2. * r * (std::f64::consts::PI / n as f64).sin();
    for i in 0..n {
        let a = sign * i as f64 * std::f64::consts::TAU / n as f64;
        let (tx, tz) = (-a.sin() * sign, a.cos() * sign);
        points.push(Point {
            index: i,
            s_m: s,
            position_x: r * a.cos(),
            position_y: 0.,
            position_z: r * a.sin(),
            tangent_x: tx,
            tangent_z: tz,
            heading_path_rad: tz.atan2(tx),
            curvature_per_m: sign / r,
        });
        s += ds;
    }
    Model { points, length: s }
}
fn observation(model: &Model, step: u64) -> Observation {
    let p = &model.points[0];
    Observation {
        sequence: step + 1,
        observation_ns: 1_000_000_000 + step * 10_000_000,
        tracking_epoch: 1,
        ready: true,
        in_pit: false,
        gear_raw: 2,
        speed_kmh: 0.,
        x: p.position_x,
        z: p.position_z,
        heading_rad: p.heading_path_rad,
        heading_error_rad: 0.,
        projection_distance_m: 0.,
        lateral_error_m: 0.,
        s_m: 0.,
        accumulated_m: 0.,
    }
}
fn running(reverse: bool) -> (Controller, Observation) {
    let model = circle(reverse);
    let mut c = Controller::new(model.clone(), config()).unwrap();
    let mut o = observation(&model, 0);
    for i in 0..=200 {
        o = observation(&model, i);
        assert!(
            !c.update(Some(&o), o.observation_ns, Some(1_000_000_000))
                .abort
        );
    }
    (c, o)
}
#[test]
fn circles_sign_and_wrap() {
    for reverse in [false, true] {
        let (mut c, o) = running(reverse);
        let out = c.update(Some(&o), o.observation_ns, Some(1_000_000_000));
        assert!((out.steering - if reverse { -0.073 } else { 0.073 }).abs() < 0.001);
        let a = target(&c.model, 3.);
        let b = target(&c.model, c.model.length + 3.);
        assert!((a.0 - b.0).abs() < 1e-10);
        assert!((a.1 - b.1).abs() < 1e-10);
    }
}
#[test]
fn unarmed_and_neutral_never_accelerate() {
    let m = circle(false);
    let mut c = Controller::new(m.clone(), config()).unwrap();
    for i in 0..200 {
        let mut o = observation(&m, i);
        o.gear_raw = 1;
        let a = c.update(Some(&o), o.observation_ns, None);
        assert_eq!(a.throttle, 0.);
        let b = c.update(Some(&o), o.observation_ns, Some(1_000_000_000));
        assert_eq!(b.throttle, 0.);
        assert_eq!(b.brake, 1.);
    }
}
#[test]
fn duplicate_does_not_integrate() {
    let (mut c, o) = running(false);
    let a = c.update(Some(&o), o.observation_ns, Some(1_000_000_000));
    for dt in [1, 10_000_000, 90_000_000] {
        assert_eq!(
            c.update(Some(&o), o.observation_ns + dt, Some(1_000_000_000)),
            a
        );
    }
}
#[test]
fn stale_and_latched_stop() {
    let (mut c, o) = running(false);
    let a = c.update(
        Some(&o),
        o.observation_ns + 100_000_000,
        Some(1_000_000_000),
    );
    assert!(a.abort);
    assert_eq!(a.throttle, 0.);
    let good = observation(&c.model, 220);
    assert!(
        c.update(Some(&good), good.observation_ns, Some(1_000_000_000))
            .abort
    );
}
#[test]
fn bad_state_cases() {
    for case in 0..8 {
        let (mut c, _) = running(false);
        let mut o = observation(&c.model, 201);
        match case {
            0 => o.x = f64::NAN,
            1 => o.in_pit = true,
            2 => o.tracking_epoch = 2,
            3 => o.projection_distance_m = 3.1,
            4 => o.heading_error_rad = 31f64.to_radians(),
            5 => o.speed_kmh = 21.,
            6 => o.gear_raw = 1,
            _ => o.ready = false,
        };
        let a = c.update(Some(&o), o.observation_ns, Some(1_000_000_000));
        assert!(a.abort, "case {case}");
        assert_eq!((a.throttle, a.brake), (0., 1.));
    }
}
#[test]
fn time_reversal_and_changed_duplicate() {
    for changed in [false, true] {
        let (mut c, mut o) = running(false);
        if changed {
            o.x += 0.1
        } else {
            o.sequence -= 1;
            o.observation_ns -= 10_000_000;
        }
        assert!(
            c.update(Some(&o), o.observation_ns, Some(1_000_000_000))
                .abort
        );
    }
}
#[test]
fn overspeed_pid_brakes_without_throttle() {
    let (mut c, _) = running(false);
    let mut o = observation(&c.model, 201);
    o.speed_kmh = 15.;
    let out = c.update(Some(&o), o.observation_ns, Some(1_000_000_000));
    assert!(!out.abort);
    assert_eq!(out.throttle, 0.);
    assert!(out.brake > 0.);
}
#[test]
fn steering_envelope_is_not_hidden() {
    let (mut c, _) = running(false);
    let mut o = observation(&c.model, 201);
    o.heading_rad += 0.2;
    let out = c.update(Some(&o), o.observation_ns, Some(1_000_000_000));
    assert_eq!(out.reason, "steering_envelope");
    assert!(out.abort);
    assert_eq!(out.throttle, 0.);
}
#[test]
fn limits_and_completions() {
    let (mut c, _) = running(false);
    let mut o = observation(&c.model, 201);
    o.accumulated_m = 100.;
    assert!(
        c.update(Some(&o), o.observation_ns, Some(1_000_000_000))
            .complete
    );
    let (mut c, o) = running(false);
    assert!(
        c.update(Some(&o), 31_000_000_000, Some(1_000_000_000))
            .complete
    );
    let mut bad = config();
    bad.target_speed_kmh = f64::NAN;
    assert!(bad.validate().is_err());
    bad = config();
    bad.max_steering = 0.2;
    assert!(bad.validate().is_err());
}
#[test]
fn toy_circle_closed_loop_converges() {
    for reverse in [false, true] {
        let m = circle(reverse);
        let mut c = Controller::new(m.clone(), config()).unwrap();
        let mut o = observation(&m, 0);
        o.x += 0.2;
        let (mut x, mut z, mut heading, mut v) = (o.x, o.z, o.heading_rad, 0.);
        for step in 0..2950 {
            o = observation(&m, step);
            let angle = z.atan2(x);
            let sign = if reverse { -1. } else { 1. };
            let path_heading = wrap(angle + sign * std::f64::consts::FRAC_PI_2);
            o.x = x;
            o.z = z;
            o.heading_rad = heading;
            o.heading_error_rad = wrap(heading - path_heading);
            o.speed_kmh = v;
            o.projection_distance_m = (x.hypot(z) - 100.).abs();
            o.lateral_error_m = x.hypot(z) - 100.;
            o.s_m =
                (sign * angle).rem_euclid(std::f64::consts::TAU) / std::f64::consts::TAU * m.length;
            let out = c.update(Some(&o), o.observation_ns, Some(1_000_000_000));
            assert!(!out.abort, "{}", out.reason);
            assert!(out.throttle == 0. || out.brake == 0.);
            v = (v + (out.throttle * 3. - out.brake * 8. - v / 3.6 * 0.02) * 0.01 * 3.6).max(0.);
            let ds = v / 3.6 * 0.01;
            heading = wrap(heading + ds * out.steering / 7.3);
            x += heading.cos() * ds;
            z += heading.sin() * ds;
        }
        assert!((x.hypot(z) - 100.).abs() < 0.10);
        assert!((v - 10.).abs() < 1.);
    }
}

#[test]
fn lap_mode_requires_explicit_bounds_and_timeout_is_failure() {
    let mut cfg = config();
    cfg.duration_s = 3600.;
    assert!(cfg.validate().is_err());
    cfg.single_lap = true;
    cfg.max_steering = 0.35;
    cfg.max_distance_travelled_m = 5000.;
    cfg.validate().unwrap();
    let model = circle(false);
    let o = observation(&model, 0);
    let mut c = Controller::new(model, cfg).unwrap();
    let out = c.update(Some(&o), 3_601_000_000_000, Some(1_000_000_000));
    assert!(out.abort && !out.complete);
    assert_eq!(out.reason, "lap_timeout");
}

#[test]
fn lap_finishes_after_whole_circumference_not_first_seam() {
    let model = circle(false);
    let mut cfg = config();
    cfg.single_lap = true;
    cfg.duration_s = 3600.;
    cfg.max_distance_travelled_m = 5000.;
    let mut c = Controller::new(model.clone(), cfg).unwrap();
    for i in 0..=120 {
        let o = observation(&model, i);
        c.update(Some(&o), o.observation_ns, Some(1_000_000_000));
    }
    let mut o = observation(&model, 121);
    o.accumulated_m = 2.;
    o.s_m = 0.;
    assert!(
        !c.update(Some(&o), o.observation_ns, Some(1_000_000_000))
            .complete
    );
    o = observation(&model, 122);
    o.accumulated_m = model.length + 0.01;
    let out = c.update(Some(&o), o.observation_ns, Some(1_000_000_000));
    assert!(out.complete && !out.abort);
    assert_eq!(out.reason, "lap_complete");
    assert!(out.lap_fraction.unwrap() >= 1.);
}

#[test]
fn elevation_grade_are_cyclic_and_signed() {
    let mut m = circle(false);
    for p in &mut m.points {
        p.position_y = p.position_z * 0.1;
    }
    m.validate().unwrap();
    assert!(poleposition_baseline::grade_pct(&m, 0.) > 9.9);
    assert!(poleposition_baseline::grade_pct(&m, m.length / 2.) < -9.9);
    assert!(
        (poleposition_baseline::elevation(&m, 2.)
            - poleposition_baseline::elevation(&m, m.length + 2.))
        .abs()
            < 1e-9
    );
}
