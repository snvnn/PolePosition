use super::*;

pub fn run(
    config_path: &Path,
    managed: Option<&poleposition_management::Managed>,
) -> Result<(PathBuf, String)> {
    let keys = platform::Hotkeys::register()?;
    let interrupt = Arc::new(AtomicBool::new(false));
    let flag = interrupt.clone();
    ctrlc::set_handler(move || flag.store(true, Ordering::Release))?;
    run_shared(config_path, managed, &keys, &interrupt, None)
}

pub(super) fn run_shared(config_path: &Path, managed: Option<&poleposition_management::Managed>,
    keys: &platform::Hotkeys, interrupt: &AtomicBool,
    automatic:Option<&crate::automatic_start::AutomaticStart>) -> Result<(PathBuf,String)> {
    let c: Config = toml::from_str(&std::fs::read_to_string(config_path)?)?;
    if !c.steering_smoothing_ms.is_finite() || !(0. ..=100.).contains(&c.steering_smoothing_ms) {return Err("Invalid steering smoothing time constant".into());}
    if c.autonomous_isolation.is_some() && (!c.continuous_learning || c.intervention.is_some()) {
        return Err("Autonomous isolation requires continuous learning without teacher input".into());
    }
    if c.continuous_learning && c.intervention.is_none() && c.autonomous_isolation.is_none() {
        return Err("Continuous autonomous driving requires isolation verification".into());
    }
    if ![5, 10, 30].contains(&c.seconds) && !([0, 15].contains(&c.seconds) && c.intervention.is_some())
        && !(c.seconds==0 && c.continuous_learning && c.autonomous_isolation.is_some()) {
        return Err("Diagnostic stage must be 5, 10 or 30 seconds (15 or continuous 0 allowed with intervention)".into());
    }
    let profile = Profile::load(&c.profile)?;
    let shift_mask = if c.intervention.is_some() { profile.shift_button_mask()? } else { 0 };
    let automatic_shift=if automatic.is_some(){profile.automatic_gear_up_button()?}else{0};
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
        || m["controls_sha256"] != profile.controls_hash()?
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
    let steering_feedback=poleposition_policy::gamepad::validate_observation_variant(&network,m["observation_variant"].as_str())?;
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
    if let Some(path)=&c.autonomous_isolation {poleposition_gamepad::isolation::check(path,None,true)?;}
    let mut cfg = crate::Config::load(&c.guardian_config)?;
    cfg.reinforcement = None;
    cfg.test_seconds = if c.policy_comparison {180.} else {c.seconds as f64};
    cfg.stopped_seconds = 0.5;
    cfg.stopped_kmh = 1.;
    let mut guard = Guard::new_gamepad(cfg);
    if c.policy_comparison && (!c.continuous_learning || c.evaluation_laps!=1 || c.intervention.is_some() || c.autonomous_isolation.is_none()) {
        return Err("Policy comparison requires isolated continuous single-lap evaluation".into());
    }
    if c.continuous_learning && (c.seconds!=0 || c.steering_smoothing_ms!=0. || !(if c.policy_comparison {c.evaluation_laps==1} else {[0,3].contains(&c.evaluation_laps)})) {
        return Err("Continuous IQL requires unlimited episode, unchanged LPF-off and evaluation laps 0 or 3".into());
    }
    let output = poleposition_state::writer::create_output(&c.output_dir, "gamepad", None)?;
    let log = Log::new(&output.join("samples.jsonl"))?;
    std::fs::write(
        output.join("manifest.json"),
        serde_json::to_vec_pretty(
            &value!({"semantics":"gamepad_v1","model":c.model,"model_sha256":model_hash,"profile":c.profile,"profile_sha256":profile_hash,"controls_actual_sha256":hash(&profile.controls)?,"controls_teacher_sha256":profile.controls_hash()?,"prepared":c.prepared,"seconds":c.seconds,"intervention":c.intervention,"intervention_scheme":"rb_toggle_v1","disable_path_departure_stop":c.disable_path_departure_stop,"shift_button_mask":shift_mask,"steering_smoothing_ms":c.steering_smoothing_ms,"complete":false}),
        )?,
    )?;
    let mut device = VirtualPad::open_with_teacher(&profile.dll, &profile.dll_sha256,c.intervention.as_ref().map(|i|i.teacher_slot))?;
    let isolation_path=c.intervention.as_ref().map(|i|&i.isolation_config).or(c.autonomous_isolation.as_ref());
    let isolation_watch=isolation_path.map(|p|poleposition_gamepad::isolation::Watch::start(p,device.slot())).transpose()?;
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
    let mut features = match m["feature_transform"].as_str() {
        None | Some("original") => FeaturesV2::gamepad(),
        Some("seam_bridge_15m_v1") => FeaturesV2::gamepad_seam_bridge(),
        _ => return Err("Unsupported gamepad feature transform".into()),
    };
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
    let mut steering_filter = poleposition_gamepad::steering::Smoother::default();
    let mut takeover = poleposition_gamepad::intervention::Toggle::default();
    let mut teacher_sequence = 0;
    let mut teacher_packet:Option<u32> = None;
    let mut teacher_selected = c.intervention.is_none() || automatic.is_some();
    let mut automatic_position_ok=false;
    let mut input_proof=crate::automatic_start::InputProof::default();
    let mut automatic_shift_started:Option<u64>=None;
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
    let mut episode = crate::gamepad_episode::Episode::default();
    let mut episode_end: Option<Value> = None;
    let mut episode_start: Option<Value> = None;
    let mut neutralized = false;
    let mut cancelled = false;
    let mut current_lap_valid = false;
    let mut latest_episode_observation:Option<crate::gamepad_episode::Observation> = None;
    let mut episode_armed = false;
    let mut relocation_evidence:Option<Value> = None;
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
        let toggle_enabled = guard.mode == Mode::Running || (guard.mode == Mode::Stopping && guard.reason == "time_limit");
        takeover.update(teacher_report.buttons, toggle_enabled);
        let step = (|| -> Result<()> {
            if let Some(t)=&teacher {
                log.push(value!({"kind":"human_input","input":t,"toggle_enabled":toggle_enabled,"full_toggle":takeover.active}));
                if !t.connected {return Err("Teacher pad disconnected; no slot switching".into());}
                if teacher_packet.is_some_and(|p|t.packet.wrapping_sub(p)>i32::MAX as u32){return Err("Teacher packet reset/device replacement".into());}
                teacher_packet=Some(t.packet);
                if poleposition_gamepad::native::capabilities(t.slot).as_ref()!=teacher_caps.as_ref(){return Err("Teacher pad identity changed".into());}
                if poleposition_gamepad::intervention::confirms_teacher_selection(teacher_report, shift_mask) {
                    teacher_selected=true;
                }
            }
            if isolation_watch.as_ref().is_some_and(|w|!w.healthy()){return Err("Input isolation lost/stale".into());}
            match update {
                Poll::Sample(raw) => {
                    latest_features = None;
                    halted_at = None;
                    sequence += 1;
                    let sample = raw.sample.clone();
                    current_lap_valid = sample.lap_valid;
                    automatic_position_ok=automatic.is_some_and(|a|a.matches(
                        [sample.position_x as f64,sample.position_y as f64,sample.position_z as f64],
                        sample.heading_rad as f64,sample.session_type,sample.gear_raw,sample.lap_valid));
                    let observed = raw.elapsed_ns;
                    if guard.mode==Mode::Waiting && automatic.is_some() {
                        input_proof.observe(observed,sample.steering_raw as f64,sample.brake as f64);
                    }
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
                    if c.continuous_learning && guard.mode==Mode::Running && state.reason=="suspected_teleport"
                        && identity_ok && sample.session_type==3 && sample.gear_raw==0
                        && (0. ..=1.).contains(&(sample.speed_kmh as f64))
                        && observed.saturating_sub(physics)<100_000_000 && observed.saturating_sub(graphics)<100_000_000 {
                        if let (Some(p),Some(a))=(latest_episode_observation,automatic) {
                            let distance=(sample.position_x as f64-p.x).hypot(sample.position_z as f64-p.z);
                            let spawn=(sample.position_x as f64-a.position[0]).hypot(sample.position_z as f64-a.position[2]);
                            if p.ready && p.epoch!=state.tracking_epoch && p.sequence+1==sequence
                                && observed>p.ns && observed-p.ns<=50_000_000 && distance>50. && spawn<=1. {
                                relocation_evidence=Some(value!({"verified":true,"observation_ns":observed,
                                    "previous_observation_ns":p.ns,"displacement_m":distance,"spawn_distance_m":spawn,
                                    "previous_epoch":p.epoch,"tracking_epoch":state.tracking_epoch}));
                            }
                        }
                    }
                    if let Some(o) = obs {
                        latest_episode_observation=Some(crate::gamepad_episode::Observation {
                            ns:observed,sequence,epoch:state.tracking_epoch,ready:state.status=="ready",
                            valid:sample.lap_valid,s:o.s_m,x:sample.position_x as f64,z:sample.position_z as f64,
                        });
                        let check = o.check_with_limits(
                            &gate,
                            now,
                            guard.mode == Mode::Waiting,
                            false,
                            !c.disable_path_departure_stop,
                        );
                        if let Err(reason) = check {
                            health.ready = false;
                            if guard.mode == Mode::Running {
                                guard.stop(now, reason);
                            }
                        }
                        max_lateral = max_lateral.max(o.lateral_error_m.abs());
                        if guard.mode == Mode::Running {
                            if c.continuous_learning {
                                episode.observe(crate::gamepad_episode::Observation {
                                    ns:observed,sequence,epoch:state.tracking_epoch,ready:state.status=="ready",
                                    valid:sample.lap_valid,s:o.s_m,x:sample.position_x as f64,z:sample.position_z as f64,
                                }, prepared.model.length, events.iter().any(|e|e.kind=="lap_boundary"),c.evaluation_laps);
                                if let Some(e)=&episode.end { guard.stop(now,&e.reason); }
                            }
                            let (last, initial) = first_s.get_or_insert((o.s_m, o.s_m));
                            progress += poleposition_state::cyclic_delta(
                                o.s_m,
                                *last,
                                prepared.model.length,
                            );
                            *last = o.s_m;
                            let _ = initial;
                            if !c.continuous_learning && !sample.lap_valid {
                                guard.stop(now, "invalid_lap");
                            }
                            if !c.continuous_learning && now.saturating_sub(guard.armed_ns.unwrap()) >= 10_000_000_000
                                && progress < 0.5
                            {
                                guard.stop(now, "progress_stall");
                            }
                        }
                        if let Some((time, axes)) =
                            history.iter().rev().find(|(t, _)| *t < observed)
                        {
                            if observed - *time < 100_000_000 && (1..=9).contains(&sample.gear_raw) {
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
                    if steering_feedback {
                        if let Some(values)=latest_features.as_mut() {
                            poleposition_policy::gamepad::apply_steering_feedback(values,sample.steering_raw as f64)?;
                        }
                    }
                    observation_valid = health.ready && latest_features.is_some();
                    log.push(value!({"kind":"observation","observation_ns":observed,"sequence":sequence,"sample":sample,"state":state,"features":latest_features,
                        "stop_telemetry":{"live":true,"identity_ok":identity_ok,"physics_ns":physics,"graphics_ns":graphics}}));
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
                        let raw_choice=takeover.choose(Axes{steering:a[0] as f64,throttle:a[1] as f64,brake:a[2] as f64},teacher_report);
                        let model_active=guard.mode==Mode::Running && raw_choice.authority==poleposition_gamepad::intervention::Authority::Model;
                        let seed=if guard.mode==Mode::Waiting {0.} else {raw_choice.axes.steering};
                        let filtered=steering_filter.sample(computed,seed,c.steering_smoothing_ms,model_active);
                        let command_action=[if model_active {filtered} else {a[0] as f64},a[1] as f64,a[2] as f64];
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
                                    steering: command_action[0],
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
                        let selected=takeover.choose(Axes{steering:command_action[0],throttle:command_action[1],brake:command_action[2]},teacher_report);
                        if guard.mode==Mode::Running && selected.label_mask.iter().any(|m|*m!=0){corrected+=1;}
                        log.push(value!({"kind":"policy","full_toggle":takeover.active,"now_ns":computed,"observation_ns":observed,"features":latest_features,"action":a,"command_action":command_action,"selected_action":[selected.axes.steering,selected.axes.throttle,selected.axes.brake],"authority":selected.authority,"label_mask":selected.label_mask,"teacher":teacher,"mode":format!("{:?}",guard.mode)}));
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
        cancelled |= cancel;
        if c.continuous_learning {
            continuous_stop_health(&mut health);
        }
        if guard.mode==Mode::Waiting && automatic.is_some_and(|a|!a.valid(poleposition_telemetry::unix_ns())) {
            guard.stop(now,"automatic_start_expired");
        }
        let automatic_ready=automatic_position_ok && automatic.is_some()
            && teacher_report.lx.unsigned_abs()<8000 && teacher_report.ly.unsigned_abs()<8000
            && teacher_report.rx.unsigned_abs()<8000 && teacher_report.ry.unsigned_abs()<8000
            && teacher_report.lt==0 && teacher_report.rt==0 && teacher_report.buttons==0;
        let probe_allowed=guard.mode==Mode::Waiting && automatic_ready && !cancel && fault.is_none()
            && health.allowed(now,&guard.config) && health.speed_kmh<=1.;
        if guard.mode==Mode::Waiting && automatic.is_some() && !probe_allowed {
            input_proof=crate::automatic_start::InputProof::default();
        }
        if probe_allowed && input_proof.failed(now) {
            fault=Some("Automatic virtual-pad input proof failed (steering/brake response missing)".into());
            guard.stop(now,"automatic_input_unresponsive");
        }
        let probe_steer=if probe_allowed && guard.mode==Mode::Waiting && !input_proof.ready(now) {
            Some(input_proof.command(now))
        } else {None};
        let auto_shift_button=if guard.mode==Mode::Waiting && automatic_ready
            && input_proof.ready(now)
            && health.allowed(now,&guard.config) && health.speed_kmh<=1. && health.gear_raw==1 {
            let at=*automatic_shift_started.get_or_insert(now);
            if now.saturating_sub(at)<150_000_000 {automatic_shift}else{0}
        } else {0};
        let manual_start=keys.start.swap(false,Ordering::AcqRel);
        let action = guard.tick(
            start.elapsed().as_nanos() as u64,
            &health,
            Signals {
                start: (if automatic.is_some(){automatic_ready && input_proof.ready(now) && automatic_shift_started.is_none_or(|at|now.saturating_sub(at)>300_000_000)}else{manual_start}) && observation_valid && (!c.continuous_learning || current_lap_valid) && (2..=9).contains(&health.gear_raw) && teacher_selected && teacher_report.buttons==0,
                stop: cancel,
                log_failed: log.failed.load(Ordering::Acquire),
                ..Default::default()
            },
        );
        if guard.mode == Mode::Running { continuity.arm(health.tracking_epoch); }
        if c.continuous_learning && guard.mode==Mode::Running && !episode_armed {
            if let Some(o)=latest_episode_observation {
                episode.observe(o,prepared.model.length,false,c.evaluation_laps);
                episode_start=Some(value!({"observation_ns":o.ns,"tracking_epoch":o.epoch,
                    "position_x":o.x,"position_z":o.z,"s_m":o.s,"speed_kmh":health.speed_kmh,"gear_raw":health.gear_raw}));
                log.push(value!({"kind":"episode_start","start":episode_start,"policy_sha256":model_hash,"automatic_start":automatic.is_some()}));
            }
            episode_armed=true;
        }
        if c.continuous_learning && guard.mode != Mode::Running && guard.armed_ns.is_some() && episode_end.is_none() {
            episode_end=Some(value!({"reason":guard.reason,"observation_ns":health.observation_ns,
                "tracking_epoch":health.tracking_epoch,"policy_sha256":model_hash,
                "last_applied":history.back().map(|(t,a)|value!({"send_end_ns":t,"axes":a})),
                "event":episode.end}));
            log.push(value!({"kind":"episode_end","end":episode_end}));
        }
        let sent = match action {
            Action::Release => {
                if c.continuous_learning {
                    match device.send(Axes::NEUTRAL) {
                        Ok(report) => {neutralized=true;log.push(value!({"kind":"neutralized","report":report,"now_ns":start.elapsed().as_nanos() as u64}));},
                        Err(e) => {fault=Some(e.to_string());}
                    }
                }
                match device.release() {
                    Ok(()) => {
                        released = true;
                        if c.continuous_learning {
                            let wait=Instant::now();
                            while read(device.slot()).is_some() && wait.elapsed()<Duration::from_secs(2) {std::thread::sleep(Duration::from_millis(10));}
                            released=read(device.slot()).is_none();
                        }
                    },
                    Err(e) => fault = Some(e.to_string()),
                }
                break;
            }
            Action::Send(a) => Some(a),
            Action::None if guard.mode == Mode::Waiting => Some(Axes::NEUTRAL),
            _ => None,
        };
        if let Some(mut a) = sent {
            if guard.mode==Mode::Waiting {
                a=poleposition_gamepad::intervention::waiting_axes(teacher_report,
                    c.intervention.is_some() && health.foreground && !cancel && fault.is_none()
                        && teacher.as_ref().is_some_and(|r|r.connected), health.speed_kmh);
                authority=poleposition_gamepad::intervention::Authority::HumanSteer;
                if let Some(steering)=probe_steer {a=Axes{steering,throttle:0.,brake:0.5};}
            } else if guard.mode==Mode::Running {
                let selected=takeover.choose(a,teacher_report);
                a=selected.axes;authority=selected.authority;
                if selected.label_mask.iter().any(|m|*m!=0) || teacher_report.buttons & shift_mask !=0 {episode.interrupted=true;}
            } else if guard.mode==Mode::Stopping && guard.reason=="time_limit" && c.intervention.is_some() && !cancel {
                // Human may steer a normal timed stop; braking remains mandatory.
                let selected=takeover.choose(a,teacher_report);
                a.steering=selected.axes.steering;authority=selected.authority;
            } else {authority=poleposition_gamepad::intervention::Authority::Model;}
            let t = start.elapsed().as_nanos() as u64;
            let shift_buttons = auto_shift_button | poleposition_gamepad::intervention::shift_buttons(
                teacher_report, shift_mask,
                matches!(guard.mode, Mode::Waiting | Mode::Running) && !cancel && fault.is_none()
                    && health.foreground && teacher.as_ref().is_some_and(|r| r.connected),
            );
            match device.send_with_buttons(a, shift_buttons) {
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
                m.emit(value!({"event":"progress","phase":match guard.mode{Mode::Waiting=>"waiting_f7",Mode::Running=>"model_control",_=>if c.continuous_learning && continuity.broken {"relocated_stopping"} else {"stopping"}},"valid_laps":episode.valid_laps,"progress_m":progress,"end_reason":episode_end.as_ref().map(|e|e["reason"].clone()),"policy_before":model_hash,"gamepad_slot":device.slot(),"teacher_slot":c.intervention.as_ref().map(|i|i.teacher_slot),"stick_selected":teacher_selected,"authority":authority,"correction_samples":corrected,"isolation_ok":isolation_watch.as_ref().map(|w|w.healthy()),"model_type":"gamepad_v1","profile_match":fault.is_none(),"speed_kmh":health.speed_kmh,"samples":predicted,"reason":guard.reason,"result_dir":output}));
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
    if predicted == 0 && fault.is_none() && automatic.is_none() {
        // Readers/log writers are closed above. Only remove this run's direct
        // child directory, never a configured parent or a resolved outside path.
        let folder = output.canonicalize()?;
        let parent = c.output_dir.canonicalize()?;
        if folder.parent() != Some(parent.as_path())
            || !folder.file_name().is_some_and(|n| n.to_string_lossy().starts_with("gamepad-")) {
            return Err("Refusing to remove an unexpected empty-session path".into());
        }
        std::fs::remove_dir_all(&folder)?;
        if let Some(e) = fault { return Err(e.into()); }
        return Ok((output, reason));
    }
    std::fs::write(
        output.join("summary.json"),
        serde_json::to_vec_pretty(
            &value!({"semantics":"gamepad_v1","passed":false,"drive_completed":passed,"comparison_report_created":false,"report_status":"pending","intervention":c.intervention.is_some(),"correction_samples":corrected,"correction_eligible":false,"seconds":c.seconds,"model_sha256":model_hash,"profile_sha256":profile_hash,"stop_confirmed":stop_confirmed,"standstill_confirmed":guard.stop_confirmed,"tracking_continuity":!continuity.broken,"released":released,"reason":reason,"error":fault,"logging_failure":log_failed,"policy_samples":predicted,"max_lateral_m":max_lateral,"progress_m":progress,"performance":"first-corner pass requires comparison report; human-assisted runs are not autonomous passes"}),
        )?,
    )?;
    let mut manifest = json(&output.join("manifest.json"))?;
    if c.continuous_learning {
        let mut summary=json(&output.join("summary.json"))?;
        summary["policy_comparison"]=value!(c.policy_comparison);
        summary["relocation_evidence"]=value!(relocation_evidence);
        summary["continuous_semantics"]=value!("gamepad_continuous_iql_v1");
        summary["episode_end"]=value!(episode_end);
        summary["episode_start"]=value!(episode_start);
        summary["episode"]=value!(episode);
        summary["cancelled"]=value!(cancelled);
        summary["shutdown"]=value!({"standstill_confirmed":guard.stop_confirmed,
            "observation_ns":health.observation_ns,"tracking_epoch":health.tracking_epoch,
            "kind":if !continuity.broken {"continuous_stop"} else if continuity.epoch==Some(health.tracking_epoch) {"untracked_standstill"} else {"relocated_standstill"},
            "continuous_stop_confirmed":stop_confirmed,"neutralized":neutralized,"released":released});
        std::fs::write(output.join("summary.json"),serde_json::to_vec_pretty(&summary)?)?;
        manifest["continuous_semantics"]=value!("gamepad_continuous_iql_v1");
        manifest["policy_comparison"]=value!(c.policy_comparison);
    }
    manifest["complete"] = value!(fault.is_none() && !log_failed);
    manifest["output_sha256"] = value!({"samples.jsonl":hash(&output.join("samples.jsonl"))?,"summary.json":hash(&output.join("summary.json"))?});
    std::fs::write(
        output.join("manifest.tmp"),
        serde_json::to_vec_pretty(&manifest)?,
    )?;
    std::fs::rename(output.join("manifest.tmp"), output.join("manifest.json"))?;
    if let Some(e) = fault {
        return Err(format!("{e}; result={}", output.display()).into());
    }
    Ok((output, reason))
}
