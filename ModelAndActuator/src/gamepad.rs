//! Separate gamepad execution. It reuses the safety guardian, never wheel PID output.
use crate::{
    Axes, Result,
    core::{Action, Command, Guard, Health, Mode, Signals},
    platform,
};
use poleposition_gamepad::{
    native::{VirtualPad, read},
    profile::Profile,
};
use poleposition_policy::{
    Frame,
    features_v2::{FeaturesV2, Input, SpeedProfile},
    gamepad::Network,
};
use poleposition_recorder::session::Tracker;
use poleposition_state::{
    model::{Prepared, hash, json},
    processor::Processor,
};
use poleposition_telemetry::{Poll, Reader, TelemetrySource};
use serde::Deserialize;
use serde_json::{Value, json as value};
use std::{
    io::Write,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};

// Processing time must be read AFTER the source has timestamped its observation.
// Do not clamp future timestamps: real clock/order faults must still be rejected.
fn poll_with_time(source: &mut impl TelemetrySource, clock: impl FnOnce() -> u64) -> (Poll, u64) {
    let update = source.poll();
    (update, clock())
}

#[derive(Default)]
struct DriveContinuity { epoch: Option<u64>, broken: bool }
impl DriveContinuity {
    fn arm(&mut self, epoch: u64) { self.epoch.get_or_insert(epoch); }
    fn observe(&mut self, epoch: u64, ready: bool) {
        if self.epoch.is_some_and(|initial| initial != epoch || !ready) { self.broken = true; }
    }
    fn confirms_stop(&self, standstill: bool) -> bool {
        standstill && self.epoch.is_some() && !self.broken
    }
}

/// Electrical/report compatibility only. Refuses to send if the game is running.
pub fn bench(dll: &Path, output: &Path) -> Result<()> {
    if platform::acc_running() != Some(false) {
        return Err("Close ACC before device bench test".into());
    }
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(output)?;
    let dll_hash = hash(dll)?;
    let mut pad = VirtualPad::open(dll, &dll_hash)?;
    let slot = pad.slot();
    let mut rows = Vec::new();
    let result = (|| -> Result<()> {
        for (s, t, b) in [
            (0., 0., 0.),
            (-1., 0., 0.),
            (1., 0., 0.),
            (0., 1., 0.),
            (0., 0., 1.),
            (0.25, 0.5, 0.5),
            (0., 0., 0.),
        ] {
            if platform::acc_running() != Some(false) {
                return Err("ACC started during bench".into());
            }
            let sent = pad.send(Axes {
                steering: s,
                throttle: t,
                brake: b,
            })?;
            let start = Instant::now();
            let mut observed = read(slot);
            while observed.is_none_or(|(_, r)| r != sent)
                && start.elapsed() < Duration::from_millis(500)
            {
                std::thread::sleep(Duration::from_millis(10));
                observed = read(slot);
            }
            let matches = observed.is_some_and(|(_, r)| r == sent);
            rows.push(value!({"sent":sent,"xinput":observed,"match":matches}));
            if !matches {
                return Err("Sent report differs from XInput readback".into());
            }
        }
        Ok(())
    })();
    let release = pad.release();
    drop(pad);
    let wait = Instant::now();
    while read(slot).is_some() && wait.elapsed() < Duration::from_secs(2) {
        std::thread::sleep(Duration::from_millis(10));
    }
    let passed = result.is_ok() && release.is_ok() && read(slot).is_none();
    serde_json::to_writer_pretty(
        &mut file,
        &value!({"semantics":"gamepad_v1","kind":"device_bench","passed":passed,"dll_sha256":dll_hash,"slot":slot,"rows":rows,"released":read(slot).is_none(),"in_game_verified":false,"error":result.err().map(|e|e.to_string()),"release_error":release.err().map(|e|e.to_string())}),
    )?;
    file.sync_all()?;
    if !passed {
        return Err("Device bench failed; see evidence JSON".into());
    }
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    profile: PathBuf,
    prepared: PathBuf,
    model: PathBuf,
    guardian_config: PathBuf,
    controller_config: PathBuf,
    output_dir: PathBuf,
    seconds: u32,
    previous_run: Option<PathBuf>,
    intervention: Option<InterventionConfig>,
}
#[derive(Deserialize,serde::Serialize)]
#[serde(deny_unknown_fields)]
struct InterventionConfig { teacher_slot:u32, isolation_config:PathBuf }
struct Log {
    tx: Option<mpsc::SyncSender<Value>>,
    failed: Arc<AtomicBool>,
    worker: Option<std::thread::JoinHandle<Result<()>>>,
}
impl Log {
    fn new(path: &Path) -> Result<Self> {
        let file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(path)?;
        let (tx, rx) = mpsc::sync_channel::<Value>(4096);
        let failed = Arc::new(AtomicBool::new(false));
        let f = failed.clone();
        let worker = std::thread::spawn(move || {
            let result = (|| -> Result<()> {
                let mut w = std::io::BufWriter::new(file);
                let mut flush = Instant::now();
                for row in rx {
                    serde_json::to_writer(&mut w, &row)?;
                    writeln!(w)?;
                    if flush.elapsed() > Duration::from_secs(1) {
                        w.flush()?;
                        flush = Instant::now();
                    }
                }
                w.flush()?;
                w.get_ref().sync_all()?;
                Ok(())
            })();
            if result.is_err() {
                f.store(true, Ordering::Release);
            }
            result
        });
        Ok(Self {
            tx: Some(tx),
            failed,
            worker: Some(worker),
        })
    }
    fn push(&self, row: Value) {
        if self.tx.as_ref().unwrap().try_send(row).is_err() {
            self.failed.store(true, Ordering::Release);
        }
    }
    fn finish(mut self) -> Result<()> {
        self.tx.take();
        self.worker
            .take()
            .unwrap()
            .join()
            .map_err(|_| "Gamepad log thread panicked")??;
        Ok(())
    }
}
fn inventory(folder: &Path, m: &Value) -> Result<()> {
    for (name, expected) in m["output_sha256"]
        .as_object()
        .ok_or("Missing model inventory")?
    {
        if name.contains(['/', '\\']) || expected.as_str() != Some(&hash(&folder.join(name))?) {
            return Err("Model inventory mismatch".into());
        }
    }
    Ok(())
}

fn stationary_response_ready(rpm:i32,gear:i32,speed:f64)->bool {
    rpm==0 && gear==1 && speed.is_finite() && (0.0..=1.0).contains(&speed)
}

#[cfg(test)] mod response_tests {
    use super::*;
    #[test]
    fn reset_during_braking_cannot_be_counted_as_normal_stop() {
        let mut c=DriveContinuity::default();
        c.observe(1,false); // Preparation can reacquire before F7.
        c.arm(2);c.observe(2,true);
        assert!(!c.confirms_stop(false));
        assert!(c.confirms_stop(true));
        c.observe(3,false); // Car reset while braking.
        c.observe(3,true);c.arm(3);
        assert!(!c.confirms_stop(true)); // New stationary observations cannot undo it.
        let mut stale=DriveContinuity::default();stale.arm(1);stale.observe(1,false);
        stale.observe(1,true);assert!(!stale.confirms_stop(true));
    }
    #[test]
    fn processing_clock_is_sampled_after_source_read() {
        use std::cell::Cell;
        struct Source<'a>(&'a Cell<u64>);
        impl TelemetrySource for Source<'_> {
            fn poll(&mut self) -> Poll { self.0.set(102); Poll::NoData }
        }
        let clock = Cell::new(100);
        let mut source = Source(&clock);
        let (_, processed_at) = poll_with_time(&mut source, || clock.get());
        assert_eq!(processed_at, 102);
    }

    #[test]
    #[ignore = "Read-only replay requires GAMEPAD_REPLAY_LOG from a real diagnostic"]
    fn replay_recorded_waiting_observations() -> Result<()> {
        let log = std::env::var("GAMEPAD_REPLAY_LOG")?;
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let c: Config = toml::from_str(&std::fs::read_to_string(root.join("GamepadIO/trial.toml"))?)?;
        let p = Prepared::load(&root.join(&c.prepared))?;
        let make = || Processor::new(p.model.clone(), p.config.clone(), p.identity.clone(), p.sign, p.offset);
        let mut old = make(); let mut fixed = make();
        let network: Network = serde_json::from_value(json(&root.join(&c.model).join("model.json"))?)?;
        network.validate()?;
        let speed: SpeedProfile = serde_json::from_value(json(&root.join(&c.model).join("speed-profile.json"))?)?;
        let cap = [10.,20.,40.,80.,120.,160.,200.,240.].into_iter().find(|v|speed.speed_kmh.iter().all(|s|s<=v)).unwrap();
        let gate: poleposition_baseline::Config = toml::from_str(&std::fs::read_to_string(root.join(&c.controller_config))?)?;
        let mut features = FeaturesV2::gamepad();
        let mut sent: Option<(u64, Axes)> = None;
        let (mut observations,mut old_reversals,mut ready,mut predictions)=(0,0,0,0);
        let mut reasons = std::collections::BTreeMap::<String,usize>::new();
        let mut first_prediction = None;
        for line in std::fs::read_to_string(log)?.lines() {
            let r: Value = serde_json::from_str(line)?;
            if r["kind"]=="sent" {
                let a=&r["axes"];
                sent=Some((r["send_end_ns"].as_u64().unwrap(),Axes{steering:a["steering"].as_f64().unwrap(),throttle:a["throttle"].as_f64().unwrap(),brake:a["brake"].as_f64().unwrap()}));
                continue;
            }
            if r["kind"]!="observation" {continue;}
            observations+=1;
            let t=r["observation_ns"].as_u64().unwrap();
            let mut v=r["sample"].clone();
            for (k,x) in [("sequence",r["sequence"].clone()),("segment_id",r["state"]["segment_id"].clone()),("elapsed_ns",value!(t)),("physics_changed_ns",value!(t)),("graphics_changed_ns",value!(t))] {v[k]=x;}
            let mut o: poleposition_state::processor::Observation = serde_json::from_value(v)?;
            o.identity=Some(p.identity.clone());
            let bad=old.update(o.clone(),r["state"]["now_ns"].as_u64().unwrap());
            if bad.reason=="time_reversal" {old_reversals+=1;}
            // Synthetic processing delay for replay only; no original log is modified.
            let now=t+1_000;
            let state=fixed.update(o,now);
            if state.status=="ready" {ready+=1;}
            let gear=r["sample"]["gear_raw"].as_i64().unwrap() as i32;
            if let Some(o)=poleposition_baseline::Observation::from_state(&state,t,gear) {
                if let Err(reason)=o.check_with_speed_limit(&gate,now,true,false) {
                    *reasons.entry(reason.to_string()).or_default()+=1;continue;
                }
                if let Some((sent_at,axes))=sent {
                    if sent_at>=t || t-sent_at>=100_000_000 {continue;}
                    if let Some(x)=features.update(&Input{frame:Frame{sequence:r["sequence"].as_u64().unwrap(),observation_ns:t,tracking_epoch:state.tracking_epoch,ready:true,speed_kmh:r["sample"]["speed_kmh"].as_f64().unwrap(),target_speed_kmh:speed.at(o.s_m,cap),rpm:r["sample"]["rpm"].as_f64().unwrap(),lateral_error_m:o.lateral_error_m,heading_error_rad:o.heading_error_rad,s_m:o.s_m},speed_cap_kmh:cap,previous_axes:[axes.steering,axes.throttle,axes.brake],previous_applied_ns:sent_at,gear_raw:gear},&p.model,&speed)? {
                        let a=network.predict(&x)?;predictions+=1;
                        if first_prediction.is_none(){first_prediction=Some(a);}
                    }
                }
            }
        }
        let result=value!({"observations":observations,"original_time_reversals":old_reversals,"corrected_ready":ready,"predictions":predictions,"gate_rejections":reasons,"first_prediction":first_prediction,"replay_only":true,"live_driving_verified":false});
        println!("{result}");
        std::fs::write(root.join("target/gamepad-clock-replay.json"),serde_json::to_vec_pretty(&result)?)?;
        assert_eq!(observations,old_reversals);
        assert!(ready>100 && predictions>100);
        Ok(())
    }
    #[test] fn engine_torque_and_gear_cannot_be_masked_by_brake_command() {
        assert!(stationary_response_ready(0,1,0.));
        for (rpm,gear,speed) in [(1,1,0.),(900,1,0.),(0,2,0.),(0,0,0.),(0,1,1.01),(0,1,f64::NAN)] {
            assert!(!stationary_response_ready(rpm,gear,speed));
        }
    }
}

/// Same stationary step sequence for a human pad and the virtual target. Physical
/// mode only reads XInput: it never creates a target or takes control of the car.
pub fn response(
    config_path: &Path,
    managed: Option<&poleposition_management::Managed>,
) -> Result<(PathBuf, String)> {
    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    struct ResponseConfig {
        profile: PathBuf,
        guardian_config: PathBuf,
        output_dir: PathBuf,
        physical_slot: Option<u32>,
        #[serde(default)]
        brake_held: bool,
    }
    let c: ResponseConfig = toml::from_str(&std::fs::read_to_string(config_path)?)?;
    let p = Profile::load(&c.profile)?;
    let physical = c.physical_slot.is_some();
    if let Some(s) = c.physical_slot {
        if poleposition_gamepad::native::slots() != vec![s] {
            return Err("Select sole physical pad slot".into());
        }
    }
    let stages = if c.brake_held {
        [
            ("1/10 중앙·페달 해제", [0., 0., 0.]),
            ("2/10 LT 끝 + 스틱 왼쪽 끝", [-1., 0., 1.]),
            ("3/10 LT 끝 + 스틱 중앙", [0., 0., 1.]),
            ("4/10 LT 끝 + 스틱 오른쪽 끝", [1., 0., 1.]),
            ("5/10 LT 끝 + 스틱 중앙", [0., 0., 1.]),
            ("6/10 LT 끝 유지 + RT 끝", [0., 1., 1.]),
            ("7/10 LT 끝 + RT 해제", [0., 0., 1.]),
            ("8/10 LT 끝 + RT 해제", [0., 0., 1.]),
            ("9/10 양쪽 트리거 끝", [0., 1., 1.]),
            ("10/10 중앙·페달 해제", [0., 0., 0.]),
        ]
    } else {
        [
            ("중앙 / 페달 해제", [0., 0., 0.]),
            ("왼쪽 끝", [-1., 0., 0.]),
            ("중앙 / 페달 해제", [0., 0., 0.]),
            ("오른쪽 끝", [1., 0., 0.]),
            ("중앙 / 페달 해제", [0., 0., 0.]),
            ("가속 끝", [0., 1., 0.]),
            ("중앙 / 페달 해제", [0., 0., 0.]),
            ("브레이크 끝", [0., 0., 1.]),
            ("양쪽 트리거 끝", [0., 1., 1.]),
            ("중앙 / 페달 해제", [0., 0., 0.]),
        ]
    };
    let keys = platform::Hotkeys::register()?;
    let interrupt = Arc::new(AtomicBool::new(false));
    let flag = interrupt.clone();
    ctrlc::set_handler(move || flag.store(true, Ordering::Release))?;
    let output =
        poleposition_state::writer::create_output(&c.output_dir, "gamepad-response", None)?;
    let log = Log::new(&output.join("samples.jsonl"))?;
    let mut device = if physical {
        None
    } else {
        Some(VirtualPad::open(&p.dll, &p.dll_sha256)?)
    };
    let slot = c
        .physical_slot
        .unwrap_or_else(|| device.as_ref().unwrap().slot());
    let mut cfg = crate::Config::load(&c.guardian_config)?;
    cfg.reinforcement = None;
    cfg.test_seconds = if physical { 180. } else { 30. };
    cfg.stopped_kmh = 1.;
    cfg.stopped_seconds = 0.5;
    let mut guard = Guard::new_gamepad(cfg);
    let start = Instant::now();
    let mut reader = Reader::new(start, Duration::from_millis(100), Duration::from_secs(1));
    let mut health = Health::default();
    let mut sequence = 0;
    let mut commands = 0;
    let mut deadline = 0;
    let mut physical_stage = 0usize;
    let mut held_since = None;
    let mut physical_complete = false;
    let mut selected = !physical;
    let mut next = start;
    let mut shown = start;
    let mut checked = start;
    let mut fault = None;
    let mut released = false;
    let mut halted_at = None;
    let mut identity = None;
    loop {
        let now = start.elapsed().as_nanos() as u64;
        let stage = if physical {
            physical_stage
        } else {
            guard
                .armed_ns
                .map_or(0, |t| ((now - t) / 3_000_000_000).min(9) as usize)
        };
        let a = start.elapsed().as_nanos() as u64;
        let raw = read(slot);
        let b = start.elapsed().as_nanos() as u64;
        if raw.is_some_and(|(_, r)| r.lx.unsigned_abs() > 8192) {
            selected = true;
        }
        if raw.is_none() || poleposition_gamepad::native::slots() != vec![slot] {
            fault = Some("device_disconnected_or_changed".into());
            guard.stop(now, "device_error");
        }
        match reader.poll() {
            Poll::Sample(f) => {
                sequence += 1;
                halted_at = None;
                let id = value!(f.identity);
                let expected = identity.get_or_insert(id.clone());
                health = Health {
                    observation_sequence: sequence,
                    observation_ns: f.elapsed_ns,
                    physics_ns: f.physics_changed_ns,
                    graphics_ns: f.graphics_changed_ns,
                    speed_kmh: f.sample.speed_kmh as f64,
                    gear_raw: f.sample.gear_raw,
                    rpm: f.sample.rpm,
                    ready: stationary_response_ready(f.sample.rpm,f.sample.gear_raw,f.sample.speed_kmh as f64),
                    live: true,
                    identity_ok: *expected == id,
                    foreground: platform::acc_foreground(),
                    ..Default::default()
                };
                if guard.mode==Mode::Running && f.sample.rpm!=0 {guard.stop(start.elapsed().as_nanos() as u64,"engine_started_during_input_check");}
                log.push(value!({"kind":"response","mode":format!("{:?}",guard.mode),"stage":stage,"target":stages[stage].1,"observation_ns":f.elapsed_ns,"sample":f.sample,
                    "input":poleposition_gamepad::InputRow::new(sequence,1,a,b,slot,raw)}));
                if now >= deadline
                    && health.ready
                    && matches!(guard.mode, Mode::Waiting | Mode::Running)
                {
                    deadline += (now.saturating_sub(deadline) / 20_000_000 + 1) * 20_000_000;
                    commands += 1;
                    let v = if guard.mode == Mode::Waiting {
                        [0.; 3]
                    } else {
                        stages[stage].1
                    };
                    let computed = start.elapsed().as_nanos() as u64;
                    if let Err(e) = guard.accept(
                        Command {
                            sequence: commands,
                            created_ns: computed,
                            expires_ns: computed + 80_000_000,
                            observation_sequence: sequence,
                            observation_ns: f.elapsed_ns,
                            tracking_epoch: 0,
                            steering: v[0],
                            throttle: v[1],
                            brake: v[2],
                        },
                        computed,
                        &health,
                    ) {
                        fault = Some(e.into());
                        guard.stop(computed, "command_error");
                    }
                }
            }
            Poll::State(s) => {
                health.live = false;
                health.ready = false;
                halted_at = matches!(s, "off" | "paused").then_some(now);
            }
            Poll::Error(e) => {
                fault = Some(e);
                health.ready = false;
                guard.stop(now, "telemetry_error");
            }
            Poll::NoData => {}
        }
        if checked.elapsed() >= Duration::from_secs(1) {
            if let Err(e) = p.verify() {
                fault = Some(e.to_string());
                guard.stop(now, "settings_changed");
            }
            checked = Instant::now();
        }
        health.foreground = platform::acc_foreground();
        health.game_halted = halted_at.is_some_and(|t| now.saturating_sub(t) < 100_000_000);
        let cancel = keys.stop.load(Ordering::Acquire)
            || interrupt.load(Ordering::Acquire)
            || managed.and_then(|m| m.control.stop_reason()).is_some();
        let act = guard.tick(
            start.elapsed().as_nanos() as u64,
            &health,
            Signals {
                start: keys.start.swap(false, Ordering::AcqRel) && selected,
                stop: cancel,
                log_failed: log.failed.load(Ordering::Acquire),
                ..Default::default()
            },
        );
        if physical {
            if guard.mode == Mode::Running {
                let target = stages[stage].1;
                let matched = raw.is_some_and(|(_, r)| {
                    let a = r.axes();
                    [
                        a.steering - target[0],
                        a.throttle - target[1],
                        a.brake - target[2],
                    ]
                    .iter()
                    .all(|v| v.abs() <= 0.06)
                });
                if matched {
                    let since = *held_since.get_or_insert(now);
                    if now - since >= 3_000_000_000 {
                        held_since = None;
                        if physical_stage == 9 {
                            physical_complete = true;
                            guard.stop(now, "response_complete");
                        } else {
                            physical_stage += 1;
                        }
                    }
                } else {
                    held_since = None;
                }
            }
            if matches!(guard.mode, Mode::Stopping | Mode::Finished) {
                // Read-only capture cannot brake or release a physical device.
                released = true;
                break;
            }
        } else {
            match act {
                Action::Release => {
                    match device.as_mut().unwrap().release() {
                        Ok(()) => released = true,
                        Err(e) => fault = Some(e.to_string()),
                    }
                    break;
                }
                Action::Send(axes) => {
                    let t = start.elapsed().as_nanos() as u64;
                    match device.as_mut().unwrap().send(axes) {
                        Ok(r) => {
                            guard.acknowledge(start.elapsed().as_nanos() as u64, axes);
                            log.push(value!({"kind":"sent","send_start_ns":t,"send_end_ns":start.elapsed().as_nanos() as u64,"report":r,"stage":stage,"mode":format!("{:?}",guard.mode)}));
                        }
                        Err(e) => {
                            fault = Some(e.to_string());
                            break;
                        }
                    }
                }
                _ => {}
            }
        }
        if shown.elapsed() > Duration::from_millis(100) {
            if let Some(m) = managed {
                m.emit(value!({"event":"progress","phase":if guard.mode==Mode::Waiting{"waiting_f7"}else{"device_response"},"gamepad_slot":slot,"stick_selected":selected,"profile_match":fault.is_none(),"speed_kmh":health.speed_kmh,"rpm":health.rpm,"reason":if guard.mode==Mode::Waiting{"엔진 OFF(RPM 0)·정차·중립 N, 실제 패드는 스틱을 움직인 뒤 F7"}else{stages[stage].0},"result_dir":output}));
            }
            shown = Instant::now();
        }
        next += Duration::from_millis(10);
        while next <= Instant::now() {
            next += Duration::from_millis(10);
        }
        std::thread::sleep(next.saturating_duration_since(Instant::now()));
    }
    reader.close();
    drop(device);
    let lost = log.failed.load(Ordering::Acquire);
    if let Err(e) = log.finish() {
        fault = Some(e.to_string());
    }
    if let Err(e) = p.verify() {
        fault = Some(e.to_string());
    }
    let passed = fault.is_none()
        && !lost
        && released
        && if physical {
            physical_complete
        } else {
            guard.reason == "time_limit" && guard.stop_confirmed
        };
    let summary = value!({"semantics":"gamepad_v1","kind":"device_response","engine_off_required":true,"physical":physical,"device_owned":!physical,"brake_held":c.brake_held,"capture_passed":passed,"reason":guard.reason,"error":fault,"released":if physical{Value::Null}else{value!(released)},"stop_confirmed":guard.stop_confirmed,"identity":identity,
        "controls_sha256":hash(&p.controls)?,"assists_sha256":hash(&p.assists)?,"dll_sha256":p.dll_sha256,"driver_sha256":p.driver_sha256,"samples_sha256":hash(&output.join("samples.jsonl"))?});
    std::fs::write(
        output.join("summary.json"),
        serde_json::to_vec_pretty(&summary)?,
    )?;
    if let Some(e) = fault {
        return Err(format!("{e}; result={}", output.display()).into());
    }
    Ok((output, guard.reason))
}
pub fn run(
    config_path: &Path,
    managed: Option<&poleposition_management::Managed>,
) -> Result<(PathBuf, String)> {
    let c: Config = toml::from_str(&std::fs::read_to_string(config_path)?)?;
    if ![5, 10, 30].contains(&c.seconds) {
        return Err("Diagnostic stage must be 5, 10 or 30 seconds".into());
    }
    let profile = Profile::load(&c.profile)?;
    profile.verified_device()?;
    let profile_hash = hash(&c.profile)?;
    let prepared = Prepared::load(&c.prepared)?;
    let m = json(&c.model.join("manifest.json"))?;
    inventory(&c.model, &m)?;
    if m["kind"] != "gamepad_imitation"
        || m["semantics"] != "gamepad_v1"
        || m["complete"] != true
        || m["parity_passed"] != true
        || m["prepared_sha256"] != hash(&prepared.path.join("manifest.json"))?
        || m["controls_sha256"] != hash(&profile.controls)?
        || m["assists_sha256"] != hash(&profile.assists)?
    {
        return Err("Gamepad model/prepared/profile mismatch".into());
    }
    let model_hash = hash(&c.model.join("model.json"))?;
    if c.seconds > 5 && c.intervention.is_none() {
        let previous = c
            .previous_run
            .as_ref()
            .ok_or("Previous passed diagnostic run required")?;
        let previous_manifest = json(&previous.join("manifest.json"))?;
        if previous_manifest["complete"] != true || previous_manifest["intervention"].is_object() {
            return Err("Incomplete previous gamepad trial".into());
        }
        inventory(previous, &previous_manifest)?;
        let s = json(&previous.join("summary.json"))?;
        if s["passed"] != true
            || s["seconds"] != if c.seconds == 10 { 5 } else { 10 }
            || s["model_sha256"] != model_hash
            || s["profile_sha256"] != profile_hash
        {
            return Err("Previous diagnostic gate failed".into());
        }
    }
    let network: Network = serde_json::from_value(json(&c.model.join("model.json"))?)?;
    network.validate()?;
    let speed: SpeedProfile = serde_json::from_value(json(&c.model.join("speed-profile.json"))?)?;
    speed.validate(&prepared.model)?;
    let cap = [10., 20., 40., 80., 120., 160., 200., 240.]
        .into_iter()
        .find(|v| speed.speed_kmh.iter().all(|s| s <= v))
        .ok_or("Speed profile out of range")?;
    let gate: poleposition_baseline::Config =
        toml::from_str(&std::fs::read_to_string(&c.controller_config)?)?;
    gate.validate()?;
    let teacher_caps=if let Some(i)=&c.intervention {
        if i.teacher_slot>3 {return Err("Teacher slot must be 0..3".into());}
        poleposition_gamepad::isolation::check(&i.isolation_config,None,true)?;
        Some(poleposition_gamepad::native::capabilities(i.teacher_slot).ok_or("Selected physical teacher pad missing")?)
    } else {None};
    let mut cfg = crate::Config::load(&c.guardian_config)?;
    cfg.reinforcement = None;
    cfg.test_seconds = c.seconds as f64;
    cfg.stopped_seconds = 0.5;
    cfg.stopped_kmh = 1.;
    let mut guard = Guard::new_gamepad(cfg);
    let keys = platform::Hotkeys::register()?;
    let interrupt = Arc::new(AtomicBool::new(false));
    let flag = interrupt.clone();
    ctrlc::set_handler(move || flag.store(true, Ordering::Release))?;
    let output = poleposition_state::writer::create_output(&c.output_dir, "gamepad", None)?;
    let log = Log::new(&output.join("samples.jsonl"))?;
    std::fs::write(
        output.join("manifest.json"),
        serde_json::to_vec_pretty(
            &value!({"semantics":"gamepad_v1","model":c.model,"model_sha256":model_hash,"profile":c.profile,"profile_sha256":profile_hash,"prepared":c.prepared,"seconds":c.seconds,"intervention":c.intervention,"complete":false}),
        )?,
    )?;
    let mut device = VirtualPad::open_with_teacher(&profile.dll, &profile.dll_sha256,c.intervention.as_ref().map(|i|i.teacher_slot))?;
    let isolation_watch=c.intervention.as_ref().map(|i|poleposition_gamepad::isolation::Watch::start(&i.isolation_config,device.slot())).transpose()?;
    let start = Instant::now();
    let mut reader = Reader::new(start, Duration::from_millis(100), Duration::from_secs(1));
    let mut processor = Processor::new(
        prepared.model.clone(),
        prepared.config.clone(),
        prepared.identity.clone(),
        prepared.sign,
        prepared.offset,
    );
    let mut tracker = Tracker::new(30.);
    let mut features = FeaturesV2::gamepad();
    let mut health = Health::default();
    let mut sequence = 0;
    let mut command_sequence = 0;
    let mut history = std::collections::VecDeque::<(u64, Axes)>::new();
    let mut deadline = 0;
    let mut next = start;
    let mut shown = start;
    let mut checked = start;
    let mut predicted = 0;
    let mut corrected = 0;
    let mut teacher_sequence = 0;
    let mut teacher_packet:Option<u32> = None;
    let mut teacher_selected = c.intervention.is_none();
    let mut authority = poleposition_gamepad::intervention::Authority::Model;
    let mut continuity = DriveContinuity::default();
    let mut fault = None;
    let mut released = false;
    let mut first_s = None;
    let mut progress = 0.;
    let mut max_lateral = 0f64;
    let mut observation_valid = false;
    let mut latest_features = None;
    let mut halted_at = None;
    loop {
        let (update, now) = poll_with_time(&mut reader, || start.elapsed().as_nanos() as u64);
        let teacher=if let Some(i)=&c.intervention {
            teacher_sequence+=1;
            let begin=start.elapsed().as_nanos() as u64;
            let input=poleposition_gamepad::native::read(i.teacher_slot);
            let end=start.elapsed().as_nanos() as u64;
            Some(poleposition_gamepad::InputRow::new(teacher_sequence,tracker.segment,begin,end,i.teacher_slot,input))
        } else {None};
        let teacher_report=teacher.as_ref().map(|r|poleposition_gamepad::Report{buttons:r.buttons,lx:r.lx,ly:r.ly,rx:r.rx,ry:r.ry,lt:r.lt,rt:r.rt}).unwrap_or_default();
        let step = (|| -> Result<()> {
            if let Some(t)=&teacher {
                log.push(value!({"kind":"human_input","input":t}));
                if !t.connected {return Err("Teacher pad disconnected; no slot switching".into());}
                if teacher_packet.is_some_and(|p|t.packet.wrapping_sub(p)>i32::MAX as u32){return Err("Teacher packet reset/device replacement".into());}
                teacher_packet=Some(t.packet);
                if poleposition_gamepad::native::capabilities(t.slot).as_ref()!=teacher_caps.as_ref(){return Err("Teacher pad identity changed".into());}
                if teacher_report.lx.unsigned_abs()>8192 {teacher_selected=true;}
                if isolation_watch.as_ref().is_none_or(|w|!w.healthy()){return Err("Input isolation lost/stale".into());}
            }
            match update {
                Poll::Sample(raw) => {
                    latest_features = None;
                    halted_at = None;
                    sequence += 1;
                    let sample = raw.sample.clone();
                    let observed = raw.elapsed_ns;
                    let physics = raw.physics_changed_ns;
                    let graphics = raw.graphics_changed_ns;
                    let identity_ok = raw.identity.car == prepared.identity.car
                        && raw.identity.track == prepared.identity.track
                        && raw.identity.sm_version == prepared.identity.sm_version
                        && raw.identity.game_version == prepared.identity.game_version;
                    let (row, events) = tracker.accept(raw, sequence, 0);
                    for e in &events {
                        if e.kind == "segment_start" {
                            processor.reset(&e.detail);
                            features.reset();
                            latest_features = None;
                        }
                    }
                    let state = processor.update(poleposition_state::runner::from_frame(row), now);
                    continuity.observe(state.tracking_epoch, state.status == "ready");
                    let obs = poleposition_baseline::Observation::from_state(
                        &state,
                        observed,
                        sample.gear_raw,
                    );
                    health = Health {
                        rpm: sample.rpm,
                        observation_sequence: sequence,
                        observation_ns: observed,
                        physics_ns: physics,
                        graphics_ns: graphics,
                        tracking_epoch: state.tracking_epoch,
                        speed_kmh: sample.speed_kmh as f64,
                        gear_raw: sample.gear_raw,
                        ready: state.status == "ready",
                        live: true,
                        identity_ok,
                        foreground: platform::acc_foreground(),
                        game_halted: false,
                    };
                    if let Some(o) = obs {
                        let check = o.check_with_speed_limit(
                            &gate,
                            now,
                            guard.mode == Mode::Waiting,
                            false,
                        );
                        if let Err(reason) = check {
                            health.ready = false;
                            if guard.mode == Mode::Running {
                                guard.stop(now, reason);
                            }
                        }
                        max_lateral = max_lateral.max(o.lateral_error_m.abs());
                        if guard.mode == Mode::Running {
                            let (last, initial) = first_s.get_or_insert((o.s_m, o.s_m));
                            progress += poleposition_state::cyclic_delta(
                                o.s_m,
                                *last,
                                prepared.model.length,
                            );
                            *last = o.s_m;
                            let _ = initial;
                            if !sample.lap_valid {
                                guard.stop(now, "invalid_lap");
                            }
                            if now.saturating_sub(guard.armed_ns.unwrap()) >= 10_000_000_000
                                && progress < 0.5
                            {
                                guard.stop(now, "progress_stall");
                            }
                        }
                        if let Some((time, axes)) =
                            history.iter().rev().find(|(t, _)| *t < observed)
                        {
                            if observed - *time < 100_000_000 && sample.gear_raw >= 2 {
                                latest_features = features.update(
                                    &Input {
                                        frame: Frame {
                                            sequence,
                                            observation_ns: observed,
                                            tracking_epoch: state.tracking_epoch,
                                            ready: health.ready,
                                            speed_kmh: sample.speed_kmh as f64,
                                            target_speed_kmh: speed.at(o.s_m, cap),
                                            rpm: sample.rpm as f64,
                                            lateral_error_m: o.lateral_error_m,
                                            heading_error_rad: o.heading_error_rad,
                                            s_m: o.s_m,
                                        },
                                        speed_cap_kmh: cap,
                                        previous_axes: [axes.steering, axes.throttle, axes.brake],
                                        previous_applied_ns: *time,
                                        gear_raw: sample.gear_raw,
                                    },
                                    &prepared.model,
                                    &speed,
                                )?;
                            }
                        }
                    }
                    observation_valid = health.ready && latest_features.is_some();
                    log.push(value!({"kind":"observation","observation_ns":observed,"sequence":sequence,"sample":sample,"state":state}));
                    if now >= deadline
                        && observation_valid
                        && matches!(guard.mode, Mode::Waiting | Mode::Running)
                        && fault.is_none()
                    {
                        deadline += (now.saturating_sub(deadline) / 20_000_000 + 1) * 20_000_000;
                        let begin = Instant::now();
                        let a = network.predict(latest_features.as_ref().unwrap())?;
                        let computed = start.elapsed().as_nanos() as u64;
                        if begin.elapsed() >= Duration::from_millis(10) {
                            return Err("policy_deadline_missed".into());
                        }
                        command_sequence += 1;
                        guard
                            .accept(
                                Command {
                                    sequence: command_sequence,
                                    created_ns: computed,
                                    expires_ns: computed + 80_000_000,
                                    observation_sequence: sequence,
                                    observation_ns: observed,
                                    tracking_epoch: health.tracking_epoch,
                                    steering: a[0] as f64,
                                    throttle: a[1] as f64,
                                    brake: a[2] as f64,
                                },
                                computed,
                                &health,
                            )
                            .map_err(|e| e.to_string())?;
                        if guard.mode == Mode::Running {
                            predicted += 1;
                        }
                        let selected=poleposition_gamepad::intervention::choose(Axes{steering:a[0] as f64,throttle:a[1] as f64,brake:a[2] as f64},teacher_report);
                        if guard.mode==Mode::Running && selected.label_mask.iter().any(|m|*m!=0){corrected+=1;}
                        log.push(value!({"kind":"policy","now_ns":computed,"observation_ns":observed,"features":latest_features,"action":a,"selected_action":[selected.axes.steering,selected.axes.throttle,selected.axes.brake],"authority":selected.authority,"label_mask":selected.label_mask,"teacher":teacher,"mode":format!("{:?}",guard.mode)}));
                    }
                }
                Poll::State(s) => {
                    continuity.observe(health.tracking_epoch, false);
                    health.live = false;
                    health.ready = false;
                    halted_at = matches!(s, "paused" | "off").then_some(now);
                    latest_features = None;
                    observation_valid = false;
                    features.reset();
                    processor.reset(s);
                    tracker.interrupt(s);
                }
                Poll::Error(e) => {
                    continuity.observe(health.tracking_epoch, false);
                    health.live = false;
                    health.ready = false;
                    return Err(e.into());
                }
                Poll::NoData => {}
            }
            if checked.elapsed() >= Duration::from_secs(1) {
                profile.verify()?;
                if hash(&c.profile)? != profile_hash
                    || hash(&c.model.join("model.json"))? != model_hash
                {
                    return Err("Profile/model changed during trial".into());
                }
                checked = Instant::now();
            }
            Ok(())
        })();
        if let Err(e) = step {
            if fault.is_none() {
                fault = Some(e.to_string());
            }
            guard.stop(now, "gamepad_runtime_error");
        }
        health.foreground = platform::acc_foreground();
        health.game_halted = halted_at.is_some_and(|t| now.saturating_sub(t) < 100_000_000);
        let cancel = managed.and_then(|m| m.control.stop_reason()).is_some()
            || interrupt.load(Ordering::Acquire)
            || keys.stop.load(Ordering::Acquire)
            || teacher_report.buttons & poleposition_gamepad::intervention::STOP_BUTTON != 0;
        let action = guard.tick(
            start.elapsed().as_nanos() as u64,
            &health,
            Signals {
                start: keys.start.swap(false, Ordering::AcqRel) && observation_valid && teacher_selected && teacher_report.buttons==0,
                stop: cancel,
                log_failed: log.failed.load(Ordering::Acquire),
                ..Default::default()
            },
        );
        if guard.mode == Mode::Running { continuity.arm(health.tracking_epoch); }
        let sent = match action {
            Action::Release => {
                match device.release() {
                    Ok(()) => released = true,
                    Err(e) => fault = Some(e.to_string()),
                }
                break;
            }
            Action::Send(a) => Some(a),
            Action::None if guard.mode == Mode::Waiting => Some(Axes::NEUTRAL),
            _ => None,
        };
        if let Some(mut a) = sent {
            if guard.mode==Mode::Running {
                let selected=poleposition_gamepad::intervention::choose(a,teacher_report);
                a=selected.axes;authority=selected.authority;
            } else if guard.mode==Mode::Stopping && guard.reason=="time_limit" && c.intervention.is_some() && !cancel {
                // Human may steer a normal timed stop; braking remains mandatory.
                let selected=poleposition_gamepad::intervention::choose(a,teacher_report);
                a.steering=selected.axes.steering;authority=selected.authority;
            } else {authority=poleposition_gamepad::intervention::Authority::Model;}
            let t = start.elapsed().as_nanos() as u64;
            match device.send(a) {
                Ok(report) => {
                    let applied = start.elapsed().as_nanos() as u64;
                    guard.acknowledge(applied, a);
                    history.push_back((applied, report.axes()));
                    while history.len() > 128 {
                        history.pop_front();
                    }
                    log.push(value!({"kind":"sent","send_start_ns":t,"send_end_ns":start.elapsed().as_nanos() as u64,"report":report,"axes":report.axes(),"authority":authority,"teacher_sequence":teacher.as_ref().map(|r|r.sequence),"slot":device.slot(),"xinput":read(device.slot()),"mode":format!("{:?}",guard.mode)}));
                }
                Err(e) => {
                    fault = Some(e.to_string());
                    guard.stop(t, "gamepad_device_error");
                    break;
                }
            }
        }
        if shown.elapsed() >= Duration::from_millis(200) {
            if let Some(m) = managed {
                m.emit(value!({"event":"progress","phase":match guard.mode{Mode::Waiting=>"waiting_f7",Mode::Running=>"model_control",_=>"stopping"},"gamepad_slot":device.slot(),"teacher_slot":c.intervention.as_ref().map(|i|i.teacher_slot),"stick_selected":teacher_selected,"authority":authority,"correction_samples":corrected,"isolation_ok":isolation_watch.as_ref().map(|w|w.healthy()),"model_type":"gamepad_v1","profile_match":fault.is_none(),"speed_kmh":health.speed_kmh,"samples":predicted,"reason":guard.reason,"result_dir":output}));
            }
            shown = Instant::now();
        }
        next += Duration::from_millis(10);
        while next <= Instant::now() {
            next += Duration::from_millis(10);
        }
        std::thread::sleep(next.saturating_duration_since(Instant::now()));
    }
    reader.close();
    let log_failed = log.failed.load(Ordering::Acquire);
    if let Err(e) = log.finish() {
        fault = Some(e.to_string());
    }
    if let Err(e) = profile.verify() {
        fault = Some(e.to_string());
    }
    let stop_confirmed = continuity.confirms_stop(guard.stop_confirmed);
    let passed = fault.is_none()
        && !log_failed
        && released
        && stop_confirmed
        && guard.reason == "time_limit"
        && predicted > 0;
    let reason = if continuity.broken {
        "tracking_changed_before_stop".to_string()
    } else { guard.reason.clone() };
    std::fs::write(
        output.join("summary.json"),
        serde_json::to_vec_pretty(
            &value!({"semantics":"gamepad_v1","passed":passed,"intervention":c.intervention.is_some(),"correction_samples":corrected,"correction_eligible":passed && corrected>0,"seconds":c.seconds,"model_sha256":model_hash,"profile_sha256":profile_hash,"stop_confirmed":stop_confirmed,"standstill_confirmed":guard.stop_confirmed,"tracking_continuity":!continuity.broken,"released":released,"reason":reason,"error":fault,"logging_failure":log_failed,"policy_samples":predicted,"max_lateral_m":max_lateral,"progress_m":progress,"performance":"first-corner pass requires comparison report; human-assisted runs are not autonomous passes"}),
        )?,
    )?;
    let mut manifest = json(&output.join("manifest.json"))?;
    manifest["complete"] = value!(fault.is_none() && !log_failed);
    manifest["output_sha256"] = value!({"samples.jsonl":hash(&output.join("samples.jsonl"))?,"summary.json":hash(&output.join("summary.json"))?});
    std::fs::write(
        output.join("manifest.tmp"),
        serde_json::to_vec_pretty(&manifest)?,
    )?;
    std::fs::rename(output.join("manifest.tmp"), output.join("manifest.json"))?;
    if fault.is_none() {
        use std::os::windows::process::CommandExt;
        let result = std::process::Command::new(".venv-controller/Scripts/python.exe")
            .args(["-m", "GamepadIO.run_report"])
            .arg(&output)
            .creation_flags(0x08000000)
            .output();
        let report_ok = result.as_ref().is_ok_and(|r| r.status.success());
        let mut summary = json(&output.join("summary.json"))?;
        summary["passed"] = value!(passed && report_ok);
        summary["correction_eligible"] = value!(passed && report_ok && corrected>0 && c.intervention.is_some());
        summary["comparison_report_created"] = value!(report_ok);
        if !report_ok {
            summary["report_error"] = value!(match result {
                Ok(r) => String::from_utf8_lossy(&r.stderr).to_string(),
                Err(e) => e.to_string(),
            });
        }
        std::fs::write(
            output.join("summary.json"),
            serde_json::to_vec_pretty(&summary)?,
        )?;
        manifest["output_sha256"]["summary.json"] = value!(hash(&output.join("summary.json"))?);
        if output.join("gamepad-comparison.json").exists() {
            manifest["output_sha256"]["gamepad-comparison.json"] =
                value!(hash(&output.join("gamepad-comparison.json"))?);
        }
        if output.join("gamepad-comparison.png").exists() {
            manifest["output_sha256"]["gamepad-comparison.png"] =
                value!(hash(&output.join("gamepad-comparison.png"))?);
        }
        std::fs::write(
            output.join("manifest.tmp"),
            serde_json::to_vec_pretty(&manifest)?,
        )?;
        std::fs::rename(output.join("manifest.tmp"), output.join("manifest.json"))?;
    }
    if let Some(e) = fault {
        return Err(format!("{e}; result={}", output.display()).into());
    }
    Ok((output, reason))
}
