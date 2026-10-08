//! Replay recorded observations through native v2; no game/device access.
use poleposition_actuator::{
    Axes,
    core::Health,
    ipc::Context,
    v2_shadow::{Applied, AppliedHistory, Config, Runtime},
};
use poleposition_policy::features_v2::Input;
use poleposition_state::model::Prepared;
use std::{
    fs::File,
    io::{BufRead, BufReader},
    path::Path,
};

#[test]
fn sent_history_never_uses_future_or_another_epoch() {
    let mut history = AppliedHistory::default();
    for t in [10, 20, 30] {
        history.push(Applied {
            at_ns: t,
            tracking_epoch: 1,
            axes: Axes::STOP,
        });
    }
    assert_eq!(history.before(20, 1).unwrap().at_ns, 10);
    assert!(history.before(10, 1).is_none());
    assert!(history.before(20, 2).is_none());
    assert!(history.before(100_000_030, 1).is_none());
}

#[test]
fn recorded_features_match_native_shadow_without_changing_pid() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let prepared =
        Prepared::load(&root.join("state_runs/prepared-1790679300781499500-3e604d8c")).unwrap();
    let config = Config {
        training: root
            .join("reinforcement_runs/sac-pretraining-1791009589852996200-037e7448")
            .to_string_lossy()
            .into_owned(),
    };
    let assets = config.verify(&prepared).unwrap();
    let mut runtime = Runtime::new(assets, prepared.model);
    let data = root.join("reinforcement_runs/teacher-features-v2-1791009556804138500-82b7987a");
    let frames = BufReader::new(File::open(data.join("train-0-frames.jsonl")).unwrap()).lines();
    let expected = BufReader::new(File::open(data.join("train-0-features.jsonl")).unwrap()).lines();
    let mut decisions = 0;
    let mut last = None;
    for (line, expected) in frames.zip(expected).take(1000) {
        let input: Input = serde_json::from_str(&line.unwrap()).unwrap();
        let expected: serde_json::Value = serde_json::from_str(&expected.unwrap()).unwrap();
        let f = input.frame;
        let context = Context {
            previous_applied: Some(Applied {
                at_ns: input.previous_applied_ns,
                tracking_epoch: f.tracking_epoch,
                axes: Axes {
                    steering: input.previous_axes[0],
                    throttle: input.previous_axes[1],
                    brake: input.previous_axes[2],
                },
            }),
            path: Some(poleposition_baseline::Observation {
                sequence: f.sequence,
                observation_ns: f.observation_ns,
                tracking_epoch: f.tracking_epoch,
                ready: true,
                in_pit: false,
                gear_raw: input.gear_raw,
                speed_kmh: f.speed_kmh,
                x: 0.,
                z: 0.,
                heading_rad: 0.,
                heading_error_rad: f.heading_error_rad,
                projection_distance_m: 0.,
                lateral_error_m: f.lateral_error_m,
                s_m: f.s_m,
                accumulated_m: 0.,
            }),
            health: Health {
                gear_raw: input.gear_raw,
                rpm: f.rpm as i32,
                ..Default::default()
            },
            armed_ns: Some(0),
            sent_ns: f.observation_ns,
        };
        let pid = poleposition_baseline::Output {
            distance_travelled_m: None,
            lap_fraction: None,
            steering: 0.1,
            throttle: 0.15,
            brake: 0.,
            target_speed_kmh: f.target_speed_kmh,
            target_x: None,
            target_z: None,
            requested_curvature: None,
            steering_unclamped: None,
            reason: "tracking".into(),
            abort: false,
            complete: false,
        };
        let saved = pid.clone();
        if let Some(result) = runtime
            .update(&context, &pid, f.observation_ns + 1)
            .unwrap()
        {
            let actual: Vec<f32> = serde_json::from_str(&result.features_json).unwrap();
            let expected: Vec<f32> = serde_json::from_value(expected["features"].clone()).unwrap();
            assert_eq!(actual, expected);
            assert_eq!(result.control_source, "pid_v2_shadow");
            assert_eq!(result.model_elapsed_ns, 0);
            assert!((-1. ..=1.).contains(&result.prediction_steering.unwrap()));
            assert_eq!(
                result.model_throttle.unwrap() * result.model_brake.unwrap(),
                0.
            );
            if let Some(t) = last {
                assert!(f.observation_ns - t >= 20_000_000);
            }
            last = Some(f.observation_ns);
            decisions += 1;
        }
        assert_eq!(pid, saved);
        assert!(
            runtime
                .update(&context, &pid, f.observation_ns + 2)
                .unwrap()
                .is_none()
        );
    }
    assert!(decisions >= 100);
}

#[test]
fn v2_short_profile_cannot_be_combined_with_v1_or_long_laps() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let mut config =
        poleposition_actuator::Config::load(&root.join("ModelAndActuator/config.toml")).unwrap();
    config.v2_shadow = Some(Config {
        training: "candidate".into(),
    });
    assert!(config.validate().is_err());
    config.baseline = Some(poleposition_actuator::Demo {
        prepared: "prepared".into(),
        config: poleposition_baseline::Config::load(&root.join("BaselineController/config.toml"))
            .unwrap(),
    });
    config.validate().unwrap();
    config.baseline.as_mut().unwrap().config.single_lap = true;
    assert!(config.validate().is_err());
    config.baseline.as_mut().unwrap().config.single_lap = false;
    config.policy = Some(poleposition_actuator::policy::Config {
        model: "v1".into(),
        mode: "shadow".into(),
        model_seconds: 5.,
        previous_run: None,
    });
    assert!(config.validate().is_err());
}

#[test]
fn real_worker_keeps_commands_on_pid_while_emitting_v2_predictions() {
    use poleposition_actuator::{Config as GuardianConfig, Demo, ipc::Producer, platform::now_ns};
    use std::{
        thread,
        time::{Duration, Instant},
    };
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let prepared_path = root.join("state_runs/prepared-1790679300781499500-3e604d8c");
    let prepared = Prepared::load(&prepared_path).unwrap();
    let mut config = GuardianConfig::load(&root.join("ModelAndActuator/config.toml")).unwrap();
    config.baseline = Some(Demo {
        prepared: prepared_path.to_string_lossy().into_owned(),
        config: poleposition_baseline::Config::load(&root.join("BaselineController/config.toml"))
            .unwrap(),
    });
    config.v2_shadow = Some(Config {
        training: root
            .join("reinforcement_runs/sac-pretraining-1791009589852996200-037e7448")
            .to_string_lossy()
            .into_owned(),
    });
    config.validate().unwrap();
    let path = std::env::temp_dir().join(format!("v2-shadow-worker-{}.json", now_ns()));
    std::fs::write(&path, serde_json::to_vec(&config).unwrap()).unwrap();
    let mut producer = Producer::spawn(
        Path::new(env!("CARGO_BIN_EXE_poleposition-actuator")),
        &path,
        "demo",
    )
    .unwrap();
    let start = Instant::now();
    let mut sequence = 0;
    let mut predictions = 0;
    let point = &prepared.model.points[0];
    while start.elapsed() < Duration::from_secs(90) && predictions < 20 {
        let now = now_ns();
        sequence += 1;
        producer.publish(Context {
            previous_applied: Some(Applied {
                at_ns: now - 5_000_000,
                tracking_epoch: 1,
                axes: Axes::STOP,
            }),
            path: Some(poleposition_baseline::Observation {
                sequence,
                observation_ns: now,
                tracking_epoch: 1,
                ready: true,
                in_pit: false,
                gear_raw: 2,
                speed_kmh: 10.,
                x: point.position_x,
                z: point.position_z,
                heading_rad: point.heading_path_rad,
                heading_error_rad: 0.,
                projection_distance_m: 0.,
                lateral_error_m: 0.,
                s_m: 0.,
                accumulated_m: 0.,
            }),
            health: Health {
                observation_sequence: sequence,
                observation_ns: now,
                tracking_epoch: 1,
                gear_raw: 2,
                rpm: 2000,
                speed_kmh: 10.,
                ..Default::default()
            },
            armed_ns: Some(now - 2_000_000_000),
            sent_ns: now,
        });
        if let Some((reply, _)) = producer.take_reply() {
            let pid = reply.controller.unwrap();
            assert!(!pid.abort, "{}", pid.reason);
            assert_eq!(
                reply.command.axes(),
                Axes {
                    steering: pid.steering,
                    throttle: pid.throttle,
                    brake: pid.brake
                }
            );
            if let Some(d) = reply.policy {
                assert_eq!(d.control_source, "pid_v2_shadow");
                assert!(d.prediction_steering.is_some());
                predictions += 1;
            }
        }
        assert!(!producer.lost(), "Native v2 worker failed");
        thread::sleep(Duration::from_millis(10));
    }
    drop(producer);
    std::fs::remove_file(path).unwrap();
    assert_eq!(predictions, 20);
}
