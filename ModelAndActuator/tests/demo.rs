//! Real IPC worker with verified existing model; synthetic car and no vJoy DLL.
use poleposition_actuator::{
    Config, Demo,
    core::{Action, Guard, Health, Mode, Signals},
    ipc::{Context, Producer},
    platform::now_ns,
};
use poleposition_baseline::Observation;
use poleposition_state::model::Prepared;
use poleposition_vjoy::Axes;
use std::{
    path::Path,
    thread,
    time::{Duration, Instant},
};
#[test]
fn demo_worker_drives_mock_and_producer_loss_stops() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let p = root.join("state_runs/prepared-1790679300781499500-3e604d8c");
    let prepared = Prepared::load(&p).unwrap();
    let mut cfg: Config = toml::from_str(include_str!("../config.toml")).unwrap();
    cfg.baseline = Some(Demo {
        prepared: p.to_string_lossy().into_owned(),
        config: poleposition_baseline::Config::load(&root.join("BaselineController/config.toml"))
            .unwrap(),
    });
    cfg.validate().unwrap();
    let dir = std::env::temp_dir().join(format!("poleposition-demo-ipc-{}", now_ns()));
    std::fs::create_dir(&dir).unwrap();
    let path = dir.join("config.json");
    std::fs::write(&path, serde_json::to_vec(&cfg).unwrap()).unwrap();
    let mut producer = Producer::spawn(
        Path::new(env!("CARGO_BIN_EXE_poleposition-actuator")),
        &path,
        "demo",
    )
    .unwrap();
    let mut guard = Guard::new(cfg);
    let start = Instant::now();
    let mut seq = 0;
    let mut driven = false;
    let mut killed = false;
    let mut stopped = false;
    let point = &prepared.model.points[0];
    while start.elapsed() < Duration::from_secs(20) {
        let now = now_ns();
        seq += 1;
        let h = Health {
            rpm: 1600,
            observation_sequence: seq,
            observation_ns: now,
            physics_ns: now,
            graphics_ns: now,
            tracking_epoch: 1,
            speed_kmh: 0.,
            gear_raw: 2,
            ready: true,
            live: true,
            identity_ok: true,
            foreground: true,
            game_halted: false,
        };
        let o = Observation {
            sequence: seq,
            observation_ns: now,
            tracking_epoch: 1,
            ready: true,
            in_pit: false,
            gear_raw: 2,
            speed_kmh: 0.,
            x: point.position_x,
            z: point.position_z,
            heading_rad: point.heading_path_rad,
            heading_error_rad: 0.,
            projection_distance_m: 0.,
            lateral_error_m: 0.,
            s_m: point.s_m,
            accumulated_m: 0.,
        };
        producer.publish(Context {
            previous_applied: None,
            path: Some(o),
            health: h.clone(),
            armed_ns: guard.armed_ns,
            sent_ns: now,
        });
        if let Some((reply, received)) = producer.take_reply() {
            assert!(!reply.controller.as_ref().unwrap().abort);
            guard.accept(reply.command, received, &h).unwrap();
        }
        if driven && !killed {
            producer.child.kill().unwrap();
            producer.child.wait().unwrap();
            killed = true;
        }
        let action = guard.tick(
            now_ns(),
            &h,
            Signals {
                start: guard.mode == Mode::Waiting && start.elapsed().as_secs_f64() > 0.6,
                producer_lost: producer.lost(),
                ..Default::default()
            },
        );
        if let Action::Send(axes) = action {
            if killed {
                assert_eq!(axes, Axes::STOP);
                stopped = true;
                break;
            }
            driven |= axes.throttle > 0.01;
            guard.acknowledge(now_ns(), axes);
        }
        thread::sleep(Duration::from_millis(10));
    }
    drop(producer);
    std::fs::remove_file(path).unwrap();
    std::fs::remove_dir(dir).unwrap();
    assert!(
        driven && killed && stopped,
        "demo did not complete mock lifecycle"
    );
    assert_eq!(guard.reason, "producer_lost");
}
