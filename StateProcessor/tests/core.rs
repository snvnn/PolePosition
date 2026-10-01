use poleposition_state::{
    Config, Identity,
    model::{Model, Point},
    processor::{Observation, Processor},
    wrap,
};
use std::f64::consts::{PI, TAU};

fn identity() -> Identity {
    Identity {
        car: "test".into(),
        track: "circle".into(),
        sm_version: "1".into(),
        game_version: "1".into(),
    }
}
fn config() -> Config {
    toml::from_str(include_str!("../config.toml")).unwrap()
}
fn circle(reverse: bool) -> Model {
    let mut points = Vec::new();
    let mut s = 0.0;
    let sign = if reverse { -1.0 } else { 1.0 };
    for i in 0..360 {
        let angle = sign * i as f64 * TAU / 360.0;
        points.push(Point {
            index: i,
            s_m: s,
            position_x: 100.0 * angle.cos(),
            position_y: 3.0,
            position_z: 100.0 * angle.sin(),
            tangent_x: -sign * angle.sin(),
            tangent_z: sign * angle.cos(),
            heading_path_rad: (sign * angle.cos()).atan2(-sign * angle.sin()),
            curvature_per_m: sign / 100.0,
        });
        s += 200.0 * (PI / 360.0).sin();
    }
    Model { points, length: s }
}
fn rectangle(height: f64) -> Model {
    let mut points = Vec::new();
    let mut s = 0.0;
    let coords: [(f64, f64); 4] = [(0.0, 0.0), (100.0, 0.0), (100.0, height), (0.0, height)];
    for (i, (x, z)) in coords.iter().enumerate() {
        let (nx, nz) = coords[(i + 1) % 4];
        let d = (nx - x).hypot(nz - z);
        let tx = (nx - x) / d;
        let tz = (nz - z) / d;
        points.push(Point {
            index: i,
            s_m: s,
            position_x: *x,
            position_y: 0.0,
            position_z: *z,
            tangent_x: tx,
            tangent_z: tz,
            heading_path_rad: tz.atan2(tx),
            curvature_per_m: 0.0,
        });
        s += d;
    }
    Model { points, length: s }
}
fn processor() -> Processor {
    Processor::new(circle(false), config(), identity(), 1.0, PI / 2.0)
}
fn obs(n: u64, index: usize) -> Observation {
    let angle = index as f64 * TAU / 360.0;
    let t = 1_000_000_000 + n * 10_000_000;
    Observation {
        sequence: n,
        segment_id: 1,
        elapsed_ns: t,
        physics_changed_ns: t,
        graphics_changed_ns: t,
        physics_packet_id: n as i32,
        graphics_packet_id: n as i32,
        position_x: 100.0 * angle.cos(),
        position_y: 3.0,
        position_z: 100.0 * angle.sin(),
        speed_kmh: 180.0,
        heading_rad: angle,
        lap_valid: true,
        in_pit: false,
        completed_laps: 1,
        session_type: 3,
        session_index: 0,
        game_status: "live".into(),
        identity: Some(identity()),
    }
}
fn step(p: &mut Processor, o: Observation) -> poleposition_state::processor::State {
    let now = o.elapsed_ns;
    p.update(o, now)
}

#[test]
fn acquisition_requires_three_new_observations() {
    let mut p = processor();
    assert_eq!(step(&mut p, obs(1, 1)).status, "acquiring");
    assert_eq!(step(&mut p, obs(2, 2)).status, "acquiring");
    assert_eq!(step(&mut p, obs(3, 3)).status, "ready");
}
#[test]
fn projection_heading_and_signed_distance() {
    let mut p = processor();
    let mut o = obs(1, 0);
    o.position_x = 102.0;
    let s = step(&mut p, o);
    assert!((s.projection_distance_m.unwrap() - 2.0).abs() < 1e-8);
    assert!((s.lateral_error_m.unwrap() + 2.0).abs() < 1e-8);
    assert!(s.heading_error_rad.unwrap().abs() < 1e-8);
    assert_eq!(s.projected_y, Some(3.0));
}
#[test]
fn circle_and_reverse_curvature() {
    for reverse in [false, true] {
        let m = circle(reverse);
        m.validate().unwrap();
        assert!((m.length - TAU * 100.0).abs() < 0.01);
        assert!((m.curvature(m.length + 10.0) - if reverse { -0.01 } else { 0.01 }).abs() < 1e-10);
    }
}
#[test]
fn curvature_wrap_interpolation() {
    let mut m = circle(false);
    m.points[0].curvature_per_m = 2.0;
    m.points[359].curvature_per_m = 0.0;
    let s = (m.length + m.points[359].s_m) / 2.0;
    assert!((m.curvature(s) - 1.0).abs() < 1e-8);
    assert!((m.curvature(s + m.length) - 1.0).abs() < 1e-8);
}
#[test]
fn angles_wrap() {
    assert!((wrap(PI + 0.1) + PI - 0.1).abs() < 1e-12);
    assert!((wrap(-PI - 0.1) - PI + 0.1).abs() < 1e-12);
}
#[test]
fn finish_line_does_not_break_epoch() {
    let mut p = processor();
    let a = step(&mut p, obs(1, 358));
    step(&mut p, obs(2, 359));
    let mut o = obs(3, 0);
    o.completed_laps = 2;
    let b = step(&mut p, o);
    assert_eq!(a.tracking_epoch, b.tracking_epoch);
    assert_eq!(b.status, "ready");
    assert!(b.accumulated_m.unwrap() > 3.0);
    assert!(b.s_m.unwrap() < 1e-6);
}
#[test]
fn reverse_motion_is_negative() {
    let mut p = processor();
    step(&mut p, obs(1, 1));
    step(&mut p, obs(2, 0));
    let s = step(&mut p, obs(3, 359));
    assert!(s.accumulated_m.unwrap() < -3.0);
    assert_eq!(s.status, "ready");
}
#[test]
fn stopped_packets_can_acquire() {
    let mut p = processor();
    for i in 1..=3 {
        let mut o = obs(i, 0);
        o.speed_kmh = 0.0;
        let s = step(&mut p, o);
        if i == 3 {
            assert_eq!(s.status, "ready");
            assert_eq!(s.accumulated_m, Some(0.0));
        }
    }
}
#[test]
fn duplicate_packet_does_not_acquire_or_refresh() {
    let mut p = processor();
    let o = obs(1, 0);
    step(&mut p, o.clone());
    let mut d = o.clone();
    d.elapsed_ns += 50_000_000;
    let s = step(&mut p, d);
    assert_eq!(s.status, "waiting");
    assert_eq!(s.s_m, None);
    let expired = p.tick(o.elapsed_ns + 100_000_000).unwrap();
    assert_eq!(expired.reason, "stale");
    assert_eq!(expired.s_m, None);
}
#[test]
fn one_page_expiration_is_independent() {
    let mut p = processor();
    let mut o = obs(20, 1);
    o.physics_changed_ns -= 100_000_000;
    assert_eq!(step(&mut p, o).reason, "stale");
}
#[test]
fn stale_observation_not_reused() {
    let mut p = processor();
    let o = obs(1, 1);
    let s = p.update(o.clone(), o.elapsed_ns + 100_000_000);
    assert_eq!(s.status, "invalid");
    assert_eq!(s.projected_x, None);
}
#[test]
fn time_reversal_invalidates() {
    let mut p = processor();
    step(&mut p, obs(2, 2));
    let s = step(&mut p, obs(1, 1));
    assert_eq!(s.reason, "time_reversal");
    assert_eq!(s.s_m, None);
}
#[test]
fn future_page_time_invalidates() {
    let mut p = processor();
    let mut o = obs(1, 1);
    o.graphics_changed_ns += 1;
    assert_eq!(step(&mut p, o).reason, "time_reversal");
}
#[test]
fn position_jump_and_reacquisition() {
    let mut p = processor();
    step(&mut p, obs(1, 0));
    let s = step(&mut p, obs(2, 100));
    assert_eq!(s.reason, "position_jump");
    let a = step(&mut p, obs(3, 100));
    step(&mut p, obs(4, 100));
    let b = step(&mut p, obs(5, 100));
    assert_eq!(a.tracking_epoch, 2);
    assert_eq!(b.status, "ready");
    assert_eq!(b.accumulated_m, Some(0.0));
}
#[test]
fn nonfinite_rejected() {
    for value in [f64::NAN, f64::INFINITY] {
        let mut p = processor();
        let mut o = obs(1, 0);
        o.heading_rad = value;
        assert_eq!(step(&mut p, o).reason, "invalid_numeric");
    }
}
#[test]
fn identity_mismatch_even_with_duplicate_packet() {
    let mut p = processor();
    let o = obs(1, 1);
    step(&mut p, o.clone());
    let mut bad = o;
    bad.identity.as_mut().unwrap().track = "elsewhere".into();
    assert_eq!(step(&mut p, bad).reason, "identity_mismatch");
}
#[test]
fn invalid_game_lap_still_projects() {
    let mut p = processor();
    for n in 1..=3 {
        let mut o = obs(n, n as usize);
        o.lap_valid = false;
        o.in_pit = true;
        let s = step(&mut p, o);
        if n == 3 {
            assert_eq!(s.status, "ready");
            assert_eq!(s.lap_valid, Some(false));
        }
    }
}
#[test]
fn reset_breaks_accumulation() {
    let mut p = processor();
    step(&mut p, obs(1, 1));
    step(&mut p, obs(2, 2));
    p.reset("paused");
    let s = step(&mut p, obs(3, 3));
    assert_eq!(s.status, "acquiring");
    assert_eq!(s.reason, "paused");
    assert_eq!(s.accumulated_m, Some(0.0));
    assert_eq!(s.tracking_epoch, 2);
}
#[test]
fn session_and_counter_reset_reacquire() {
    for which in 0..3 {
        let mut p = processor();
        for n in 1..=3 {
            step(&mut p, obs(n, 1));
        }
        let mut o = obs(4, 1);
        match which {
            0 => o.segment_id = 2,
            1 => o.completed_laps = 0,
            _ => o.session_index = 1,
        };
        let s = step(&mut p, o);
        assert_eq!(s.status, "acquiring");
        assert_eq!(s.tracking_epoch, 2);
    }
}
#[test]
fn parallel_routes_ambiguous_at_start() {
    let mut p = Processor::new(rectangle(1.0), config(), identity(), 1.0, 0.0);
    let mut o = obs(1, 0);
    o.position_x = 50.0;
    o.position_z = 0.5;
    assert_eq!(step(&mut p, o).reason, "ambiguous_projection");
}
#[test]
fn global_branch_improvement_never_jumps_ready() {
    let mut p = Processor::new(rectangle(4.0), config(), identity(), 1.0, 0.0);
    for n in 1..=3 {
        let mut o = obs(n, 0);
        o.position_x = 50.0;
        o.position_z = 0.0;
        step(&mut p, o);
    }
    let mut o = obs(4, 0);
    o.position_x = 50.0;
    o.position_z = 3.0;
    let s = step(&mut p, o);
    assert_eq!(s.reason, "global_branch_conflict");
    assert_eq!(s.s_m, None);
}
#[test]
fn outside_range_and_invalid_model() {
    let mut p = processor();
    let mut o = obs(1, 0);
    o.position_x = 150.0;
    assert_eq!(step(&mut p, o).reason, "outside_projection_range");
    let mut m = circle(false);
    m.points[2].s_m = 0.0;
    assert!(m.validate().is_err());
}
#[test]
fn long_gap_requires_reacquisition() {
    let mut p = processor();
    step(&mut p, obs(1, 1));
    let s = step(&mut p, obs(20, 1));
    assert_eq!(s.reason, "observation_gap");
    assert_eq!(step(&mut p, obs(21, 1)).status, "acquiring");
}
#[test]
fn forward_curvature_offsets_are_preserved() {
    let mut p = processor();
    let s = step(&mut p, obs(1, 359));
    let c: Vec<f64> = serde_json::from_str(&s.forward_curvature_json).unwrap();
    assert_eq!(c.len(), 5);
    assert!(c.iter().all(|x| (*x - 0.01).abs() < 1e-12));
}

#[test]
fn non_live_observations_invalidate_and_reacquire() {
    for status in ["paused", "replay", "off", "disconnected"] {
        let mut p = processor();
        for n in 1..=3 {
            step(&mut p, obs(n, 1));
        }
        let mut o = obs(4, 1);
        o.game_status = status.into();
        let state = step(&mut p, o);
        assert_eq!(state.status, "unavailable");
        assert_eq!(state.s_m, None);
        assert_eq!(step(&mut p, obs(5, 1)).status, "acquiring");
    }
}
#[test]
fn straight_segment_projection_and_lateral_sign() {
    let mut model = rectangle(100.0);
    model.points[1].tangent_x = 1.0;
    model.points[1].tangent_z = 0.0;
    model.points[1].heading_path_rad = 0.0;
    let mut p = Processor::new(model, config(), identity(), 1.0, 0.0);
    let mut o = obs(1, 0);
    o.position_x = 25.0;
    o.position_z = 2.0;
    o.heading_rad = 0.0;
    let state = step(&mut p, o);
    assert!((state.fraction.unwrap() - 0.25).abs() < 1e-12);
    assert!((state.s_m.unwrap() - 25.0).abs() < 1e-12);
    assert!((state.lateral_error_m.unwrap() - 2.0).abs() < 1e-12);
    assert!(state.heading_error_rad.unwrap().abs() < 1e-12);
}
