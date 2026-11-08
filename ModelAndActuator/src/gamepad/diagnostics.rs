use super::*;

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

/// Check physical/virtual isolation with ACC closed; never a driving qualification.
pub fn intervention_bench(config: &Path, output: &Path) -> Result<()> {
    if platform::acc_running() != Some(false) {
        return Err("Close ACC before intervention isolation bench".into());
    }
    let c: Config = toml::from_str(&std::fs::read_to_string(config)?)?;
    let i = c.intervention.as_ref().ok_or("Intervention configuration required")?;
    poleposition_gamepad::isolation::check(&i.isolation_config, None, true)?;
    let teacher = read(i.teacher_slot).ok_or("Allowlisted controller cannot read teacher pad")?;
    let p: Value = json(&c.profile)?;
    let dll = Path::new(p["dll"].as_str().ok_or("Missing ViGEm DLL")?);
    let expected = p["dll_sha256"].as_str().ok_or("Missing ViGEm DLL hash")?;
    let mut file = std::fs::OpenOptions::new().write(true).create_new(true).open(output)?;
    let mut pad = VirtualPad::open_with_teacher(dll, expected, Some(i.teacher_slot))?;
    let slot = pad.slot();
    let check = (|| -> Result<()> {
        if platform::acc_running() != Some(false) {return Err("ACC started during bench".into());}
        poleposition_gamepad::isolation::check(&i.isolation_config, Some(slot), false)?;
        if read(i.teacher_slot).is_none() {return Err("Teacher disappeared after virtual creation".into());}
        Ok(())
    })();
    let release = pad.release();
    drop(pad);
    let after = poleposition_gamepad::isolation::check(&i.isolation_config, None, false);
    let passed = check.is_ok() && release.is_ok() && after.is_ok() && read(i.teacher_slot).is_some();
    serde_json::to_writer_pretty(&mut file, &value!({
        "kind":"intervention_isolation_bench", "semantics":"gamepad_v1",
        "passed":passed, "teacher_slot":i.teacher_slot, "teacher_before":teacher,
        "virtual_slot":slot, "released":release.is_ok(), "in_game_verified":false,
        "error":check.err().map(|e|e.to_string()),
        "release_error":release.err().map(|e|e.to_string()),
        "after_release_error":after.err().map(|e|e.to_string())
    }))?;
    file.sync_all()?;
    if !passed {return Err("Intervention isolation bench failed; see evidence JSON".into());}
    Ok(())
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
