use poleposition_actuator::{
    Config,
    core::{Action, Command, Guard, Health, Mode, Signals},
    ipc::{Context, Producer},
    platform::now_ns,
};
use poleposition_vjoy::Axes;
use std::{
    path::PathBuf,
    thread,
    time::{Duration, Instant},
};
fn config() -> Config {
    toml::from_str(include_str!("../config.toml")).unwrap()
}
fn temp() -> PathBuf {
    let p = std::env::temp_dir().join(format!(
        "poleposition-ipc-{}-{}",
        std::process::id(),
        now_ns()
    ));
    std::fs::create_dir(&p).unwrap();
    p
}
fn health(now: u64, seq: u64) -> Health {
    Health {
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
    }
}
fn armed() -> Guard {
    let mut guard = Guard::new(config());
    let now = now_ns();
    for i in 0..=60 {
        let t = now - 700_000_000 + i * 10_000_000;
        let h = health(t, i + 1);
        guard
            .accept(
                Command {
                    sequence: i + 1,
                    created_ns: t,
                    expires_ns: t + 100_000_000,
                    observation_sequence: i + 1,
                    observation_ns: t,
                    tracking_epoch: 1,
                    steering: 0.,
                    throttle: 0.2,
                    brake: 0.,
                },
                t,
                &h,
            )
            .unwrap();
        if let Action::Send(a) = guard.tick(
            t,
            &h,
            Signals {
                start: i == 60,
                ..Default::default()
            },
        ) {
            guard.acknowledge(t, a);
        }
    }
    assert_eq!(guard.mode, Mode::Running);
    guard
}
#[test]
fn reply_generated_during_guardian_checks_uses_receipt_time() {
    let p = temp();
    std::fs::write(
        p.join("config.json"),
        serde_json::to_vec(&config()).unwrap(),
    )
    .unwrap();
    let producer = Producer::spawn(
        std::path::Path::new(env!("CARGO_BIN_EXE_poleposition-actuator")),
        &p.join("config.json"),
        "stationary",
    )
    .unwrap();
    let deadline = Instant::now();
    let mut reproduced = false;
    let mut sequence = 1;
    while deadline.elapsed() < Duration::from_secs(2) {
        let before_checks = now_ns();
        let h = health(before_checks, sequence);
        producer.publish(Context {
            path: None,
            health: h.clone(),
            armed_ns: None,
            sent_ns: before_checks,
        });
        // Model work between the early clock read and consumption of a reply.
        thread::sleep(Duration::from_millis(25));
        if let Some((reply, received)) = producer.take_reply()
            && reply.command.created_ns > before_checks
            && reply.command.observation_sequence == sequence
            && received < reply.command.expires_ns
            && received.saturating_sub(h.observation_ns) < config().age_ns()
        {
            let mut guard = Guard::new(config());
            assert_eq!(
                guard.accept(reply.command.clone(), before_checks, &h),
                Err("command_expired")
            );
            assert!(received >= reply.command.created_ns);
            guard.accept(reply.command, received, &h).unwrap();
            reproduced = true;
            break;
        }
        sequence += 1;
    }
    assert!(
        reproduced,
        "No command generated during simulated guardian work"
    );
    drop(producer);
    std::fs::remove_dir_all(p).unwrap();
}
#[test]
fn killed_child_and_broken_pipe_stop_independent_guard() {
    for disconnect in [false, true] {
        let p = temp();
        std::fs::write(
            p.join("config.json"),
            serde_json::to_vec(&config()).unwrap(),
        )
        .unwrap();
        let mut producer = Producer::spawn(
            std::path::Path::new(env!("CARGO_BIN_EXE_poleposition-actuator")),
            &p.join("config.json"),
            "crawl",
        )
        .unwrap();
        let mut guard = armed();
        if disconnect {
            producer.disconnect();
        } else {
            producer.child.kill().unwrap();
        }
        let deadline = Instant::now();
        while !producer.lost() && deadline.elapsed() < Duration::from_secs(2) {
            thread::sleep(Duration::from_millis(2));
        }
        assert!(producer.lost());
        let now = now_ns();
        assert_eq!(
            guard.tick(
                now,
                &health(now, 1000),
                Signals {
                    producer_lost: true,
                    ..Default::default()
                }
            ),
            Action::Send(Axes::STOP)
        );
        drop(producer);
        std::fs::remove_dir_all(p).unwrap();
    }
}
#[link(name = "ntdll")]
unsafe extern "system" {
    fn NtSuspendProcess(process: *mut std::ffi::c_void) -> i32;
}
#[test]
fn suspended_producer_expires_while_guard_continues() {
    use std::os::windows::io::AsRawHandle;
    let p = temp();
    std::fs::write(
        p.join("config.json"),
        serde_json::to_vec(&config()).unwrap(),
    )
    .unwrap();
    let mut producer = Producer::spawn(
        std::path::Path::new(env!("CARGO_BIN_EXE_poleposition-actuator")),
        &p.join("config.json"),
        "crawl",
    )
    .unwrap();
    let mut guard = armed();
    let start = Instant::now();
    let mut seq = 1000;
    let mut got = false;
    while start.elapsed() < Duration::from_secs(2) {
        let now = now_ns();
        let h = health(now, seq);
        seq += 1;
        producer.publish(Context {
            path: None,
            health: h.clone(),
            armed_ns: Some(now),
            sent_ns: now,
        });
        if let Some(reply) = producer.replies.take()
            && guard.accept(reply.command, now, &h).is_ok()
        {
            got = true;
            break;
        }
        thread::sleep(Duration::from_millis(2));
    }
    assert!(got);
    assert!(unsafe { NtSuspendProcess(producer.child.as_raw_handle()) } >= 0);
    let expiration = guard.command().unwrap().expires_ns;
    thread::sleep(Duration::from_millis(115));
    let now = now_ns();
    assert!(!producer.lost());
    assert_eq!(
        guard.tick(now, &health(now, seq), Signals::default()),
        Action::Send(Axes::STOP)
    );
    assert!(now.saturating_sub(expiration) < 150_000_000);
    assert_eq!(guard.reason, "command_expired");
    drop(producer);
    std::fs::remove_dir_all(p).unwrap();
}
#[test]
fn mock_cli_outputs_complete_verified_bundle() {
    let p = temp();
    let mut cfg = config();
    cfg.output_dir = p.join("runs").to_string_lossy().into();
    std::fs::write(p.join("config.toml"), toml::to_string(&cfg).unwrap()).unwrap();
    let result = std::process::Command::new(env!("CARGO_BIN_EXE_poleposition-actuator"))
        .current_dir(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap(),
        )
        .args(["self-test", "--fault", "manual-stop", "--config"])
        .arg(p.join("config.toml"))
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let out = PathBuf::from(String::from_utf8(result.stdout).unwrap().trim());
    let manifest: serde_json::Value =
        serde_json::from_slice(&std::fs::read(out.join("manifest.json")).unwrap()).unwrap();
    assert_eq!(manifest["complete"], true);
    assert_eq!(manifest["mock"], true);
    for (name, expected) in manifest["output_sha256"].as_object().unwrap() {
        assert_eq!(
            poleposition_vjoy::hash(&out.join(name)).unwrap(),
            expected.as_str().unwrap()
        );
    }
    let summary: serde_json::Value =
        serde_json::from_slice(&std::fs::read(out.join("summary.json")).unwrap()).unwrap();
    assert_eq!(summary["stop_target_pass"], true);
    assert_eq!(summary["released"], true);
    assert_eq!(summary["dropped"], 0);
    std::fs::remove_dir_all(p).unwrap();
}
