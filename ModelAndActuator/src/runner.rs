use crate::{
    Axes, Config, Result,
    core::{Action, Command, Guard, Health, Mode, Signals},
    ipc::{Context, Producer},
    logger::{CommandRow, EventRow, Message, Sink, TelemetryRow},
    platform,
    profile::Calibration,
};
use poleposition_recorder::{Timing, session::Tracker};
use poleposition_state::{
    model::{Prepared, hash},
    processor::Processor,
    writer::{create_output, finish_manifest, write_json},
};
use poleposition_telemetry::{Poll, Reader, TelemetrySource};
use poleposition_vjoy::{Device, native::Native};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

pub(crate) fn event(sink: &mut Sink, now: u64, kind: &str, detail: impl Into<String>) {
    sink.push(Message::Event(EventRow {
        now_ns: now,
        kind: kind.into(),
        detail: detail.into(),
    }));
}
pub(crate) fn command_row(
    now: u64,
    cmd: Option<&Command>,
    axes: Option<Axes>,
    guard: &Guard,
) -> CommandRow {
    CommandRow {
        now_ns: now,
        sequence: cmd.map(|c| c.sequence),
        created_ns: cmd.map(|c| c.created_ns),
        expires_ns: cmd.map(|c| c.expires_ns),
        observation_sequence: cmd.map(|c| c.observation_sequence),
        tracking_epoch: cmd.map(|c| c.tracking_epoch),
        requested_steering: cmd.map(|c| c.steering),
        requested_throttle: cmd.map(|c| c.throttle),
        requested_brake: cmd.map(|c| c.brake),
        applied_steering: axes.map(|a| a.steering),
        applied_throttle: axes.map(|a| a.throttle),
        applied_brake: axes.map(|a| a.brake),
        axis_x: None,
        axis_y: None,
        axis_z: None,
        send_success: false,
        mode: format!("{:?}", guard.mode),
        reason: guard.reason.clone(),
        validation_ns: None,
        ipc_ns: None,
        driver_ns: None,
        update_interval_ns: None,
    }
}
pub fn test(
    config: Config,
    prepared_path: &Path,
    scenario: &str,
    profile_path: &Path,
) -> Result<PathBuf> {
    if scenario != "demo" {
        crate::ipc::Scenario::new(scenario)?;
    }
    if (scenario == "demo") != config.baseline.is_some() {
        return Err("Demo/config mismatch".into());
    }
    let prepared = Prepared::load(prepared_path)?;
    if let Some(policy) = &config.policy {
        policy.verify(&prepared.path)?;
    }
    if let Some(demo) = &config.baseline
        && std::fs::canonicalize(&demo.prepared)? != std::fs::canonicalize(prepared_path)?
    {
        return Err("Demo prepared path mismatch".into());
    }
    let mut device = Native::load(Path::new(&config.dll))?;
    let probe = device.probe()?;
    let calibration = Calibration::load(profile_path, &probe, &prepared.identity)?;
    let hotkeys = platform::Hotkeys::register()?;
    let ctrl_c = Arc::new(AtomicU64::new(0));
    let signal = ctrl_c.clone();
    ctrlc::set_handler(move || {
        let _ = signal.compare_exchange(0, platform::now_ns(), Ordering::AcqRel, Ordering::Relaxed);
    })?;
    let output = create_output(Path::new(&config.output_dir), scenario, None)?;
    let config_path = output.join("config.json");
    write_json(&config_path, &config)?;
    let mut manifest = json!({"schema_version":1,"kind":"actuator_run","tool_version":"0.1.0","complete":false,"scenario":scenario,"mock":false,
        "created_unix_ns":poleposition_telemetry::unix_ns(),"config":config,"calibration":calibration.data,"probe":probe,
        "prepared_path":prepared.path,"input_sha256":{prepared.path.join("manifest.json").to_string_lossy().into_owned():hash(&prepared.path.join("manifest.json"))?,
            calibration.path.to_string_lossy().into_owned():calibration.sha256},
        "clock":"Windows QueryPerformanceCounter converted to ns, shared across local processes; not game simulation time",
        "units":{"steering":"normalized virtual axis; -1 left, +1 right","pedals":"0 released, 1 full","steering_raw":"ACC raw observed value; not virtual axis or wheel angle","speed_kmh":"km/h","time":"ns"},
        "limitations":"Driver acknowledgement is not ACC response. Guardian/driver failure cannot guarantee braking. Gamepad data uses different input settings.",
        "lap_definition":"For single_lap only: one TrackModel circumference from the initial tracked position in the same tracking epoch; not necessarily an ACC finish-line-to-finish-line timed lap.",
        "reference_length_m":prepared.model.length,
        "reference_grade":"100*(Y(s+5m)-Y(s-5m))/10m; signed estimate from recorded reference, not independently surveyed road slope"});
    write_json(&output.join("manifest.json"), &manifest)?;
    if let Some(policy) = &config.policy {
        for name in ["manifest.json", "model.json"] {
            let p = Path::new(&policy.model).join(name).canonicalize()?;
            manifest["input_sha256"][p.to_string_lossy().into_owned()] = json!(hash(&p)?);
        }
        finish_manifest(&output.join("manifest.json"), &manifest)?;
    }
    let mut result = (|| -> Result<()> {
        let mut sink = Sink::start(&output, config.queue_capacity, Some(config.display_hz))?;
        let mut producer = Producer::spawn(&std::env::current_exe()?, &config_path, scenario)?;
        // All fallible setup is complete before acquiring the physical device.
        calibration.verify()?;
        prepared.verify()?;
        device.acquire(calibration.mapping.clone())?;
        if let Err(e) = device.send(Axes::NEUTRAL) {
            return Err(format!("Initial neutral failed: {e}; device state unconfirmed").into());
        }
        let profile_bad = Arc::new(AtomicBool::new(false));
        let monitor_stop = Arc::new(AtomicBool::new(false));
        let bad = profile_bad.clone();
        let end = monitor_stop.clone();
        let profile_path = calibration.path.clone();
        let profile_hash = calibration.sha256.clone();
        let profile_inputs = calibration.data["input_sha256"].clone();
        let policy_inputs = config.policy.as_ref().map(|p| {
            ["manifest.json", "model.json"]
                .iter()
                .map(|n| {
                    let path = Path::new(&p.model).join(n);
                    let h = hash(&path);
                    (path, h)
                })
                .collect::<Vec<_>>()
        });
        let monitor = thread::spawn(move || {
            while !end.load(Ordering::Acquire) {
                if hash(&profile_path).map_or(true, |h| h != profile_hash)
                    || poleposition_state::model::verify_map(&profile_inputs, None).is_err()
                    || policy_inputs.as_ref().is_some_and(|v| {
                        v.iter().any(|(p, h)| {
                            h.as_ref().is_err() || hash(p).ok().as_ref() != h.as_ref().ok()
                        })
                    })
                {
                    bad.store(true, Ordering::Release);
                    break;
                }
                thread::sleep(Duration::from_millis(250));
            }
        });
        let base = platform::now_ns();
        let start = Instant::now();
        let mut reader = Reader::new(start, Duration::from_millis(100), Duration::from_secs(1));
        let mut tracker = Tracker::new(prepared.config.teleport_margin_m);
        let mut processor = Processor::new(
            prepared.model.clone(),
            prepared.config.clone(),
            prepared.identity.clone(),
            prepared.sign,
            prepared.offset,
        );
        let mut guard = Guard::new(config.clone());
        let mut health = Health::default();
        let mut path_observation = None;
        let mut demo_gate_reason = String::new();
        let mut last_demo_reason = String::new();
        let mut last_policy_source = String::new();
        let mut last_policy_observation = None;
        let mut policy_feature_timing = Timing::default();
        let mut policy_inference_timing = Timing::default();
        let mut policy_model_elapsed_ns = 0u64;
        let mut policy_prediction_samples = 0u64;
        let mut policy_sources = std::collections::BTreeMap::<String, u64>::new();
        let mut provenance = VecDeque::new();
        let mut sequence = 0;
        let mut connection = "initial".to_string();
        let mut halted_at = None;
        let mut last_mode = Mode::Waiting;
        let mut last_foreground = None;
        let mut last_send = None;
        let mut previous_poll = None;
        let (
            mut validation_timing,
            mut ipc_timing,
            mut driver_timing,
            mut poll_timing,
            mut observation_age,
        ) = (
            Timing::default(),
            Timing::default(),
            Timing::default(),
            Timing::default(),
            Timing::default(),
        );
        let (mut sends, mut rejected, mut missed) = (0u64, 0u64, 0u64);
        let mut device_error = None;
        let mut released = false;
        let mut scenario_complete = false;
        let mut first_stop_observed = None;
        let period = Duration::from_secs_f64(1. / config.poll_hz);
        let mut next = Instant::now();
        let mut process_check = Instant::now() - Duration::from_secs(1);
        let mut game_absent = false;
        event(
            &mut sink,
            base,
            "guardian_started",
            format!(
                "guardian_pid={} producer_pid={}; F7=start F8=stop",
                std::process::id(),
                producer.child.id()
            ),
        );
        loop {
            let now = platform::now_ns();
            if let Some(previous) = previous_poll {
                poll_timing.add(Duration::from_nanos(now - previous));
            }
            previous_poll = Some(now);
            let mut source_changed = false;
            match reader.poll() {
                Poll::Sample(f) => {
                    connection = "live".into();
                    halted_at = None;
                    game_absent = false;
                    sequence += 1;
                    let (row, events) = tracker.accept(f, sequence, 0);
                    for e in events {
                        if e.kind == "segment_start" {
                            processor.reset(&e.detail);
                            source_changed = true;
                        }
                        event(&mut sink, base + e.elapsed_ns, &e.kind, e.detail);
                    }
                    let raw = row.frame.clone();
                    let o = poleposition_state::runner::from_frame(row);
                    let identity_ok = o.identity.as_ref() == Some(&prepared.identity);
                    let state = processor.update(o, start.elapsed().as_nanos() as u64);
                    path_observation = poleposition_baseline::Observation::from_state(
                        &state,
                        base + raw.elapsed_ns,
                        raw.sample.gear_raw,
                    );
                    health = Health {
                        rpm: raw.sample.rpm,
                        observation_sequence: sequence,
                        observation_ns: base + raw.elapsed_ns,
                        physics_ns: base + raw.physics_changed_ns,
                        graphics_ns: base + raw.graphics_changed_ns,
                        tracking_epoch: state.tracking_epoch,
                        speed_kmh: raw.sample.speed_kmh as f64,
                        gear_raw: raw.sample.gear_raw,
                        ready: state.status == "ready",
                        live: true,
                        identity_ok,
                        foreground: false,
                        game_halted: false,
                    };
                    provenance.push_back((sequence, health.observation_ns, health.tracking_epoch));
                    while provenance.len() > 128 {
                        provenance.pop_front();
                    }
                    let observed = platform::now_ns();
                    observation_age.add(Duration::from_nanos(
                        observed.saturating_sub(health.observation_ns),
                    ));
                    if guard.stop_ns.is_some()
                        && first_stop_observed.is_none()
                        && raw.sample.throttle <= 0.01
                        && raw.physics_changed_ns + base >= guard.stop_ns.unwrap()
                    {
                        first_stop_observed = Some(observed);
                    }
                    sink.push(Message::Telemetry(TelemetryRow {
                        now_ns: observed,
                        sequence,
                        segment_id: state.segment_id.unwrap_or(0),
                        tracking_epoch: state.tracking_epoch,
                        observation_ns: health.observation_ns,
                        physics_ns: health.physics_ns,
                        graphics_ns: health.graphics_ns,
                        physics_packet_id: raw.sample.physics_packet_id,
                        graphics_packet_id: raw.sample.graphics_packet_id,
                        speed_kmh: health.speed_kmh,
                        throttle_observed: raw.sample.throttle as f64,
                        brake_observed: raw.sample.brake as f64,
                        steering_raw: raw.sample.steering_raw as f64,
                        position_x: raw.sample.position_x as f64,
                        position_y: raw.sample.position_y as f64,
                        position_z: raw.sample.position_z as f64,
                        lap_valid: raw.sample.lap_valid,
                        in_pit: raw.sample.in_pit,
                        state_status: state.status,
                        state_reason: state.reason,
                        gear_raw: raw.sample.gear_raw,
                        rpm: raw.sample.rpm,
                        path_s_m: state.s_m,
                        lateral_error_m: state.lateral_error_m,
                        heading_error_rad: state.heading_error_rad,
                        vehicle_heading_rad: state.vehicle_heading_rad,
                        projection_distance_m: state.projection_distance_m,
                        accumulated_m: state.accumulated_m,
                        reference_elevation_m: state
                            .s_m
                            .map(|s| poleposition_baseline::elevation(&prepared.model, s)),
                        reference_grade_pct: state
                            .s_m
                            .map(|s| poleposition_baseline::grade_pct(&prepared.model, s)),
                        completed_laps: raw.sample.completed_laps,
                        lap_time_ms: raw.sample.lap_time_ms,
                        progress: raw.sample.progress as f64,
                        sector: raw.sample.sector,
                    }));
                }
                Poll::State(status) => {
                    connection = status.into();
                    event(&mut sink, now, "connection_state", status);
                    if status != "live" {
                        health.live = false;
                        health.ready = false;
                        tracker.interrupt(status);
                        processor.reset(status);
                        source_changed = true;
                    }
                    halted_at = matches!(status, "paused" | "off").then_some(now);
                }
                Poll::Error(e) => {
                    connection = "read_error".into();
                    health.live = false;
                    health.ready = false;
                    tracker.interrupt("read_error");
                    processor.reset("read_error");
                    source_changed = true;
                    halted_at = None;
                    event(&mut sink, now, "read_error", e);
                }
                Poll::NoData => {}
            }
            if let Some(state) = processor.tick(start.elapsed().as_nanos() as u64) {
                health.ready = state.status == "ready";
            }
            health.foreground = platform::acc_foreground();
            if last_foreground != Some(health.foreground) {
                event(
                    &mut sink,
                    platform::now_ns(),
                    "acc_foreground",
                    health.foreground.to_string(),
                );
                last_foreground = Some(health.foreground);
            }
            if process_check.elapsed() >= Duration::from_millis(500) {
                game_absent = platform::acc_running() == Some(false);
                process_check = Instant::now();
            }
            let now = platform::now_ns();
            if let Some(demo) = &config.baseline {
                let gate = path_observation
                    .as_ref()
                    .ok_or("missing_path_state")
                    .and_then(|o| o.check(&demo.config, now, guard.mode == Mode::Waiting));
                demo_gate_reason = gate.err().unwrap_or("").into();
                if !demo_gate_reason.is_empty() {
                    health.ready = false;
                    if guard.mode == Mode::Running {
                        guard.stop(now, &format!("demo:{demo_gate_reason}"));
                    }
                }
            }
            health.game_halted =
                game_absent || halted_at.is_some_and(|t| now.saturating_sub(t) < config.age_ns());
            producer.publish(Context {
                path: path_observation.clone(),
                health: health.clone(),
                armed_ns: guard.armed_ns,
                sent_ns: now,
            });
            if let Some((reply, now)) = producer.take_reply() {
                let begin = Instant::now();
                let c = reply.command;
                let request = c.clone();
                let valid_source = provenance.contains(&(
                    c.observation_sequence,
                    c.observation_ns,
                    c.tracking_epoch,
                ));
                let check = if valid_source {
                    guard.accept(c, now, &health)
                } else {
                    Err("unknown_observation")
                };
                let last_validated_ns = begin.elapsed().as_nanos() as u64;
                validation_timing.add(Duration::from_nanos(last_validated_ns));
                let last_ipc_ns = now.checked_sub(request.created_ns);
                if let Some(n) = last_ipc_ns {
                    ipc_timing.add(Duration::from_nanos(n));
                }
                let mut validation_row = command_row(now, Some(&request), None, &guard);
                validation_row.reason = check.err().unwrap_or("accepted").into();
                validation_row.validation_ns = Some(last_validated_ns);
                validation_row.ipc_ns = last_ipc_ns;
                sink.push(Message::Command(validation_row));
                if let Err(reason) = check {
                    rejected += 1;
                    if guard.mode == Mode::Running {
                        guard.stop(now, reason);
                    }
                } else if let Some(diagnostic) = &reply.controller {
                    if let Some(detail) = &reply.policy {
                        let key = (detail.observation_sequence, detail.tracking_epoch);
                        if last_policy_observation != Some(key) {
                            last_policy_observation = Some(key);
                            policy_feature_timing.add(Duration::from_nanos(detail.feature_ns));
                            if let Some(n) = detail.inference_ns {
                                policy_inference_timing.add(Duration::from_nanos(n));
                                policy_prediction_samples += 1;
                            }
                            *policy_sources
                                .entry(detail.control_source.clone())
                                .or_default() += 1;
                        }
                        if detail.control_source == "model" || last_policy_source == "model" {
                            policy_model_elapsed_ns =
                                policy_model_elapsed_ns.max(detail.model_elapsed_ns);
                        }
                        if detail.control_source != last_policy_source {
                            event(
                                &mut sink,
                                now,
                                "policy_control_source",
                                format!("{}:{}", detail.control_source, detail.policy_reason),
                            );
                            last_policy_source = detail.control_source.clone();
                        }
                        sink.push(Message::Policy(detail.clone()));
                    }
                    if diagnostic.reason != last_demo_reason {
                        event(&mut sink, now, "controller_state", &diagnostic.reason);
                        last_demo_reason = diagnostic.reason.clone();
                    }
                    sink.push(Message::Controller(crate::logger::ControllerRow::new(
                        now, &request, diagnostic,
                    )));
                    if diagnostic.abort && guard.mode == Mode::Running {
                        guard.stop(now, &format!("demo:{}", diagnostic.reason));
                    } else if diagnostic.complete && guard.mode == Mode::Running {
                        scenario_complete = true;
                        guard.stop(now, "scenario_complete");
                    }
                } else if reply.scenario_complete && guard.mode == Mode::Running {
                    scenario_complete = true;
                    guard.stop(now, "scenario_complete");
                }
            }
            if profile_bad.load(Ordering::Acquire) {
                guard.stop(now, "calibration_changed");
            }
            if guard.mode == Mode::Waiting
                && now.saturating_sub(base) >= (config.arm_wait_seconds * 1e9) as u64
            {
                guard.stop(now, "arm_timeout");
            }
            let now = platform::now_ns();
            let hotkey_time = hotkeys.stop_observed_ns.load(Ordering::Acquire);
            let ctrl_time = ctrl_c.load(Ordering::Acquire);
            if let Some(trigger) = [hotkey_time, ctrl_time]
                .into_iter()
                .filter(|t| *t > 0)
                .min()
            {
                guard.stop(
                    trigger,
                    if hotkey_time > 0 && trigger == hotkey_time {
                        "f8"
                    } else {
                        "ctrl_c"
                    },
                );
            }
            let signals = Signals {
                start: hotkeys.start.swap(false, Ordering::AcqRel),
                stop: hotkeys.stop.swap(false, Ordering::AcqRel)
                    || ctrl_c.load(Ordering::Acquire) > 0,
                producer_lost: producer.lost(),
                log_failed: sink.failed.load(Ordering::Acquire),
                source_changed,
                explicit_release: false,
            };
            let start_requested = signals.start;
            let before_start = guard.mode;
            let action = guard.tick(now, &health, signals);
            if start_requested {
                let detail = format!(
                    "before={before_start:?};after={:?};foreground={};fresh={};ready={};identity_ok={};speed_kmh={:.3};command_unexpired={}",
                    guard.mode,
                    health.foreground,
                    health.fresh(now, config.age_ns()),
                    health.ready,
                    health.identity_ok,
                    health.speed_kmh,
                    guard.command().is_some_and(|c| c.expires_ns > now),
                );
                event(&mut sink, now, "start_requested", &detail);
                if before_start == Mode::Waiting && guard.mode == Mode::Waiting {
                    eprintln!(
                        "F7 received, start blocked: {detail}; continuous fresh stop for {} s also required",
                        config.stopped_seconds
                    );
                }
            }
            if guard.mode != last_mode {
                event(
                    &mut sink,
                    now,
                    "execution_state",
                    format!("{:?}:{}", guard.mode, guard.reason),
                );
                last_mode = guard.mode;
            }
            match action {
                Action::None => {}
                Action::Send(axes) => {
                    let requested = guard.command().cloned();
                    let begin = Instant::now();
                    let sent = device.send(axes);
                    let cost = begin.elapsed();
                    let sent_at = platform::now_ns();
                    driver_timing.add(cost);
                    let mut row = command_row(sent_at, requested.as_ref(), Some(axes), &guard);
                    row.driver_ns = Some(cost.as_nanos() as u64);
                    row.update_interval_ns = last_send.map(|n| sent_at - n);
                    last_send = Some(sent_at);
                    match sent {
                        Ok([x, y, z]) => {
                            sends += 1;
                            row.send_success = true;
                            row.axis_x = Some(x);
                            row.axis_y = Some(y);
                            row.axis_z = Some(z);
                            guard.acknowledge(sent_at, axes);
                        }
                        Err(e) => {
                            let e = e.to_string();
                            row.reason = format!("driver_failure:{e}");
                            device_error.get_or_insert(e);
                            guard.stop(sent_at, "driver_failure");
                        }
                    }
                    sink.push(Message::Command(row));
                    if device_error.is_some() && health.game_halted {
                        break;
                    }
                }
                Action::Release => {
                    let begin = Instant::now();
                    let result = device.release();
                    let cost = begin.elapsed();
                    let mut row =
                        command_row(platform::now_ns(), None, Some(Axes::NEUTRAL), &guard);
                    row.reason = "neutral_and_release".into();
                    row.driver_ns = Some(cost.as_nanos() as u64);
                    row.send_success = result.is_ok();
                    if let Err(e) = result {
                        device_error = Some(e.to_string());
                    } else {
                        released = true;
                    }
                    sink.push(Message::Command(row));
                    event(
                        &mut sink,
                        platform::now_ns(),
                        "device_release",
                        format!(
                            "success={released} stop_confirmed={} game_halted={}",
                            guard.stop_confirmed, health.game_halted
                        ),
                    );
                    break;
                }
            }
            if let Ok(mut display) = sink.display.try_lock() {
                *display = format!(
                    "{:?} {} | ACC={} foreground={} ready={} speed={:.2} gear_raw={} | F7 start / F8 stop | log_drops={} demo_gate={}",
                    guard.mode,
                    guard.reason,
                    connection,
                    health.foreground,
                    health.ready,
                    health.speed_kmh,
                    health.gear_raw,
                    sink.dropped,
                    demo_gate_reason
                );
            }
            next += period;
            let current = Instant::now();
            if current >= next {
                let n = ((current - next).as_secs_f64() / period.as_secs_f64()).floor() + 1.;
                missed += n as u64;
                next += period.mul_f64(n);
            }
            thread::sleep(next.saturating_duration_since(Instant::now()));
        }
        monitor_stop.store(true, Ordering::Release);
        let _ = monitor.join();
        reader.close();
        drop(producer);
        let dropped = sink.dropped;
        let counts = sink.finish();
        let log_error = counts.as_ref().err().map(|e| e.to_string());
        let latency = guard
            .stop_ns
            .zip(guard.first_zero_sent_ns)
            .map(|(a, b)| b.saturating_sub(a));
        let recording_valid = counts.is_ok() && dropped == 0;
        let verified = recording_valid && device_error.is_none() && released;
        let summary = json!({"schema_version":1,"normal_shutdown":verified,"recording_valid":recording_valid,"released":released,"stop_confirmed":guard.stop_confirmed,
            "controller_exit_reason":last_demo_reason,"lap_completed":config.baseline.as_ref().is_some_and(|b|b.config.single_lap)&&last_demo_reason=="lap_complete"&&verified&&guard.stop_confirmed,
            "released_while_halted":guard.released_while_halted,"termination_reason":guard.reason,"scenario_completed":scenario_complete,
            "device_error":device_error,"log_error":log_error,"dropped":dropped,"written":counts.as_ref().ok(),"sent_reports":sends,"rejected_commands":rejected,"missed_periods":missed,
            "command_validation":validation_timing.report(),"ipc_delivery":ipc_timing.report(),"driver_call":driver_timing.report(),"poll_interval":poll_timing.report(),"observation_age":observation_age.report(),
            "policy_feature":policy_feature_timing.report(),"policy_inference":policy_inference_timing.report(),"policy_sources":policy_sources,"policy_prediction_samples":policy_prediction_samples,"policy_model_elapsed_ns":policy_model_elapsed_ns,
            "stop_trigger_ns":guard.stop_ns,"first_zero_send_ns":guard.first_zero_sent_ns,"stop_to_zero_send_ns":latency,"stop_target_ns":150_000_000,
            "stop_target_pass":latency.map(|n|n<=150_000_000),"first_observed_zero_throttle_ns":first_stop_observed,
            "stop_to_observed_zero_ns":guard.stop_ns.zip(first_stop_observed).map(|(a,b)|b.saturating_sub(a)),
            "response_caveat":"First fresh observation with throttle <=0.01; may already be zero. This is not proof of causal ACC response or total system latency."});
        write_json(&output.join("summary.json"), &summary)?;
        calibration.verify()?;
        prepared.verify()?;
        if let Some(policy) = &config.policy {
            policy.verify(&prepared.path)?;
        }
        if !verified {
            return Err("Test ended with device/logging/release failure; see summary.json".into());
        }
        Ok(())
    })();
    if let Err(e) = &result {
        manifest["error"] = json!(e.to_string());
    }
    let mut hashes = serde_json::Map::new();
    for name in [
        "config.json",
        "commands.csv",
        "telemetry.csv",
        "events.csv",
        "summary.json",
        "controller.csv",
        "policy.csv",
    ] {
        let p = output.join(name);
        if p.is_file() {
            hashes.insert(name.into(), json!(hash(&p)?));
        }
    }
    manifest["output_sha256"] = Value::Object(hashes);
    finish_manifest(&output.join("manifest.json"), &manifest)?;
    if output.join("summary.json").is_file() {
        let report = std::process::Command::new("python")
            .args(["-m", "ModelAndActuator", "report", "--run"])
            .arg(&output)
            .output();
        match report {
            Ok(r) if r.status.success() => {
                for name in ["report.html", "validation.json", "terrain.json"] {
                    if output.join(name).is_file() {
                        manifest["output_sha256"][name] = json!(hash(&output.join(name))?);
                    }
                }
            }
            other => {
                let message = match other {
                    Ok(r) => String::from_utf8_lossy(&r.stderr).into_owned(),
                    Err(e) => e.to_string(),
                };
                manifest["report_error"] = json!(message);
                if result.is_ok() {
                    result = Err("Report generation/validation failed".into());
                }
            }
        }
    }
    manifest["complete"] = json!(result.is_ok());
    finish_manifest(&output.join("manifest.json"), &manifest)?;
    eprintln!("Actuator result: {}", output.display());
    result?;
    Ok(output)
}

/// Full-range assignment is only enabled with explicit settings-screen confirmation,
/// fresh non-live telemetry and ACC in front; no calibration bypass for driving tests.
pub fn bind(config: Config, axis: &str, settings_confirmed: bool, main_menu: bool) -> Result<()> {
    if !settings_confirmed || !["steering", "throttle", "brake"].contains(&axis) {
        return Err("bind requires --settings-confirmed and --axis steering|throttle|brake; open ACC controls screen first".into());
    }
    let mut device = Native::load(Path::new(&config.dll))?;
    let probe = device.probe()?;
    poleposition_vjoy::validate_probe(&probe, true)?;
    let keys = platform::Hotkeys::register()?;
    let stop = Arc::new(AtomicBool::new(false));
    let s = stop.clone();
    ctrlc::set_handler(move || s.store(true, Ordering::Release))?;
    let start = Instant::now();
    let mut reader = Reader::new(start, Duration::from_millis(100), Duration::from_millis(20));
    let mut armed = None;
    let mut acquired = false;
    let mut main_menu_gate = crate::binding::MainMenuGate::default();
    let output = create_output(Path::new(&config.output_dir), &format!("bind-{axis}"), None)?;
    let mut manifest = json!({"schema_version":1,"kind":"actuator_run","tool_version":"0.1.0","complete":false,"scenario":format!("bind-{axis}"),
        "mock":false,"config":config,"probe":probe,"main_menu_confirmed":main_menu,"input_sha256":{},"output_sha256":{},"created_unix_ns":poleposition_telemetry::unix_ns(),
        "limitations":"Full-range axis assignment in ACC settings only. Not a driving validation; no calibrated profile yet."});
    write_json(&output.join("manifest.json"), &manifest)?;
    let mut sink = Sink::start(&output, config.queue_capacity, None)?;
    let mut metadata_guard = Guard::new(config.clone());
    let mut error = None;
    let mut reason = "arm_timeout";
    let mut sends = 0;
    let mut last_send = None;
    event(&mut sink, platform::now_ns(), "bind_waiting", axis);
    eprintln!(
        "In ACC controls only: F7 moves {axis} for {} s, F8 stops. Do not enter driving.",
        config.bind_seconds
    );
    loop {
        let poll = reader.poll();
        let (status, observed_ns) = reader.observed_game_state();
        let status_age_ns = (start.elapsed().as_nanos() as u64).saturating_sub(observed_ns);
        let acc_in_front = platform::acc_foreground();
        let paused_safe = !matches!(poll, Poll::Sample(_) | Poll::Error(_))
            && matches!(status, "paused" | "off")
            && status_age_ns < 100_000_000
            && acc_in_front;
        let pages_absent = if main_menu {
            poleposition_telemetry::all_pages_absent().ok()
        } else {
            None
        };
        // Never fall back to menu mode after a live session or a query error.
        let safe = if main_menu {
            main_menu_gate.update(
                start.elapsed().as_nanos() as u64,
                pages_absent,
                acc_in_front,
            )
        } else {
            paused_safe
        };
        if stop.load(Ordering::Acquire)
            || keys.stop.swap(false, Ordering::AcqRel)
            || start.elapsed().as_secs_f64() > config.arm_wait_seconds
        {
            reason = "user_stop_or_arm_timeout";
            break;
        }
        if sink.failed.load(Ordering::Acquire) {
            reason = "logging_failure";
            error = Some("Binding log failure".to_string());
            break;
        }
        if armed.is_none() && keys.start.swap(false, Ordering::AcqRel) {
            if !safe {
                let detail = format!(
                    "axis={axis};status={status};status_age_ns={status_age_ns};acc_foreground={acc_in_front};main_menu={main_menu};pages_absent={pages_absent:?}"
                );
                event(&mut sink, platform::now_ns(), "binding_blocked", &detail);
                eprintln!("Binding blocked: controls-screen gate not satisfied. {detail}");
            } else {
                if let Err(e) = device.acquire(probe.mapping.clone()) {
                    error = Some(e.to_string());
                    reason = "acquire_failed";
                    break;
                }
                acquired = true;
                if let Err(e) = device.send(Axes::NEUTRAL) {
                    error = Some(e.to_string());
                    reason = "initial_neutral_failed";
                    break;
                }
                metadata_guard.mode = Mode::Running;
                event(&mut sink, platform::now_ns(), "binding_started", axis);
                armed = Some(Instant::now());
            }
        }
        if let Some(t) = armed {
            if !safe || t.elapsed().as_secs_f64() >= config.bind_seconds {
                reason = if safe {
                    "binding_duration_complete"
                } else {
                    "settings_guard_lost"
                };
                break;
            }
            let wave = (t.elapsed().as_secs_f64() * std::f64::consts::PI).sin();
            let mut axes = Axes::NEUTRAL;
            match axis {
                "steering" => axes.steering = wave,
                "throttle" => axes.throttle = (wave + 1.) / 2.,
                _ => axes.brake = (wave + 1.) / 2.,
            }
            let begin = Instant::now();
            let result = device.send(axes);
            let cost = begin.elapsed();
            let now = platform::now_ns();
            let mut row = command_row(now, None, Some(axes), &metadata_guard);
            row.reason = "full_range_binding".into();
            row.driver_ns = Some(cost.as_nanos() as u64);
            row.update_interval_ns = last_send.map(|t| now - t);
            last_send = Some(now);
            match result {
                Ok([x, y, z]) => {
                    sends += 1;
                    row.send_success = true;
                    row.axis_x = Some(x);
                    row.axis_y = Some(y);
                    row.axis_z = Some(z);
                }
                Err(e) => {
                    error = Some(e.to_string());
                    reason = "driver_failure";
                }
            }
            sink.push(Message::Command(row));
            if error.is_some() {
                break;
            }
        }
        thread::sleep(Duration::from_secs_f64(1. / config.poll_hz));
    }
    let mut released = !acquired;
    if acquired {
        match device.release() {
            Ok(()) => released = true,
            Err(e) => {
                error = Some(e.to_string());
                reason = "release_failure";
            }
        }
    }
    event(
        &mut sink,
        platform::now_ns(),
        "binding_finished",
        format!("{reason};released={released}"),
    );
    let dropped = sink.dropped;
    let counts = sink.finish();
    if let Err(e) = &counts {
        error = Some(e.to_string());
    }
    let valid = error.is_none() && dropped == 0;
    write_json(
        &output.join("summary.json"),
        &json!({"schema_version":1,"normal_shutdown":valid&&released,"recording_valid":valid,
        "written":counts.as_ref().ok(),"dropped":dropped,"released":released,"termination_reason":reason,"error":error,"sent_reports":sends,
        "scenario_completed":reason=="binding_duration_complete","mode":"settings_only","driver_position_after_release":device.probe().ok(),
        "response_caveat":"Assignment sweep only; verify axis direction in ACC settings. No driving reaction was measured."}),
    )?;
    for name in [
        "commands.csv",
        "telemetry.csv",
        "events.csv",
        "summary.json",
    ] {
        manifest["output_sha256"][name] = json!(hash(&output.join(name))?);
    }
    finish_manifest(&output.join("manifest.json"), &manifest)?;
    let report = std::process::Command::new("python")
        .args(["-m", "ModelAndActuator", "report", "--run"])
        .arg(&output)
        .output();
    let report_ok = match report {
        Ok(r) if r.status.success() => true,
        Ok(r) => {
            manifest["report_error"] = json!(String::from_utf8_lossy(&r.stderr));
            false
        }
        Err(e) => {
            manifest["report_error"] = json!(e.to_string());
            false
        }
    };
    if report_ok {
        for name in ["report.html", "validation.json"] {
            manifest["output_sha256"][name] = json!(hash(&output.join(name))?);
        }
    }
    manifest["complete"] = json!(valid && released && report_ok);
    if let Some(e) = error {
        manifest["error"] = json!(e);
    }
    finish_manifest(&output.join("manifest.json"), &manifest)?;
    eprintln!("Binding result: {}", output.display());
    if valid && released && report_ok {
        Ok(())
    } else {
        Err("Binding/recording failed; inspect manifest and summary".into())
    }
}
