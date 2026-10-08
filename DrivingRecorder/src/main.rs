use poleposition_management::{Managed, platform};
use poleposition_recorder::{
    Config, Timing,
    session::{Event, Tracker, create_session},
    writer::{Message, Sink, write_json},
};
use poleposition_telemetry::{Poll, Reader, TelemetrySource, unix_ns};
use std::{
    io::{self, BufRead},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};

fn run(managed: Option<&Managed>) -> poleposition_management::Result<(Option<PathBuf>, String)> {
    let mut config_path = "DrivingRecorder/recorder.toml".to_string();
    let mut duration = None;
    let mut gamepad_profile = None;
    let mut gamepad_slot = None;
    let mut gamepad_diagnostic = false;
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--managed" => (),
            "--gamepad-diagnostic" => gamepad_diagnostic=true,
            "--gamepad-profile" => gamepad_profile = Some(PathBuf::from(args.next().ok_or("Missing gamepad profile")?)),
            "--gamepad-slot" => gamepad_slot = Some(args.next().ok_or("Missing slot")?.parse::<u32>()?),
            "--config" => config_path = args.next().ok_or("--config needs a path")?,
            "--duration" => {
                let v: f64 = args.next().ok_or("--duration needs seconds")?.parse()?;
                if !v.is_finite() || v <= 0.0 {
                    return Err("Invalid duration".into());
                }
                duration = Some(Duration::try_from_secs_f64(v)?);
            }
            "--help" => {
                println!(
                    "poleposition-recorder [--config FILE] [--duration SECONDS]\nType a note + Enter to mark an event; type /stop or press Ctrl+C to finish."
                );
                return Ok((None, "help".into()));
            }
            _ => return Err(format!("Unknown option {a}").into()),
        }
    }
    if !cfg!(windows) {
        return Err("ACC recording requires Windows".into());
    }
    let config: Config = toml::from_str(&std::fs::read_to_string(&config_path)?)?;
    config.validate()?;
    let profile = gamepad_profile.as_ref().map(|p| poleposition_gamepad::profile::Profile::load(p)).transpose()?;
    if profile.is_some() && managed.is_none() {return Err("Gamepad recording requires managed F7 device selection".into());}
    if let Some(p)=&profile { if !gamepad_diagnostic {p.verified_device()?;} if config.poll_hz!=100. {return Err("Gamepad capture requires 100Hz".into());} }
    if gamepad_diagnostic && profile.is_none(){return Err("Diagnostic requires gamepad profile".into());}
    if profile.is_some()!=gamepad_slot.is_some() || gamepad_slot.is_some_and(|s|s>3) {return Err("Gamepad needs profile and slot 0..3".into());}
    #[cfg(windows)]
    if let Some(slot)=gamepad_slot {
        if poleposition_gamepad::native::slots()!=vec![slot] {return Err("Select the sole physical controller; remove virtual targets".into());}
    }
    #[cfg(windows)]
    let gamepad_capabilities=gamepad_slot.and_then(poleposition_gamepad::native::capabilities);
    #[cfg(windows)]
    if profile.is_some() && gamepad_capabilities.is_none(){return Err("Cannot identify physical gamepad capabilities".into());}
    if let Some(manager) = managed {
        manager
            .control
            .set_time_limit(duration.map_or(7200., |d| d.as_secs_f64()))?;
    }
    let hotkeys = if managed.is_some() {
        Some(platform::Hotkeys::register()?)
    } else {
        None
    };
    if let (Some(manager), Some(keys)) = (managed, &hotkeys) {
        let wait_start = Instant::now();
        let mut source = Reader::new(
            wait_start,
            Duration::from_secs_f64(config.stale_seconds),
            Duration::from_secs_f64(config.retry_seconds),
        );
        let mut last_sample = None;
        let mut identity = None;
        let mut connection = "waiting";
        let mut stick_selected = gamepad_slot.is_none();
        let mut displayed = Instant::now() - Duration::from_secs(1);
        loop {
            #[cfg(windows)]
            if let Some(slot)=gamepad_slot {
                let (_,r)=poleposition_gamepad::native::read(slot).ok_or("Selected gamepad disconnected")?;
                if r.lx.unsigned_abs()>8192 {stick_selected=true;}
            }
            if let Some(reason) = manager.control.stop_reason() {
                return Ok((None, reason.into()));
            }
            if keys.stop.load(Ordering::Acquire) {
                return Ok((None, "f8".into()));
            }
            match source.poll() {
                Poll::Sample(frame) => {
                    last_sample = Some(Instant::now());
                    identity = Some(frame.identity);
                    connection = "live";
                }
                Poll::State(state) => {
                    connection = state;
                    if state != "live" {
                        last_sample = None;
                    }
                }
                Poll::Error(_) => {
                    connection = "read_error";
                    last_sample = None;
                }
                Poll::NoData => (),
            }
            let fresh =
                last_sample.is_some_and(|t| t.elapsed().as_secs_f64() < config.stale_seconds);
            if keys.start.swap(false, Ordering::AcqRel) && fresh && stick_selected && platform::acc_foreground() {
                if let Some(p)=&profile {p.verify()?;}
                break;
            }
            if displayed.elapsed() >= Duration::from_millis(200) {
                manager.emit(serde_json::json!({"event":"state","state":"waiting_f7","connection":connection,"identity":identity,"fresh":fresh,"gamepad_slot":gamepad_slot,"stick_selected":stick_selected}));
                displayed = Instant::now();
            }
            thread::sleep(Duration::from_millis(10));
        }
        source.close();
    }
    let start = Instant::now();
    let started_unix = unix_ns();
    let (id, path) = create_session(Path::new(&config.output_dir))?;
    let metadata = serde_json::json!({"schema_version":1,"session_id":id,"started_unix_ns":started_unix,"software_version":env!("CARGO_PKG_VERSION"),"config":config,"config_path":config_path,"user_declared":{"weather":config.weather,"traffic":config.traffic,"fuel":config.fuel,"tyres":config.tyres,"assists":config.assists},"identity_source":"ACC static page; first identity in summary, changes in events","time_basis":"host UTC Unix nanoseconds and monotonic elapsed nanoseconds; not simulation timestamps","units":{"speed_kmh":"km/h","position":"metres, ACC raw XYZ","heading_rad":"radians, ACC raw yaw","steering_raw":"ACC raw input; sign uncalibrated","throttle":"0..1","brake":"0..1","progress":"0..1","gear_raw":"0 reverse, 1 neutral, 2 first"},"completion":"see summary.json; absent summary means incomplete"});
    write_json(&path.join("metadata.json"), &metadata)?;
    let mut metadata = metadata;
    if let Some(p)=&profile {
        metadata["input_semantics"]=serde_json::json!("gamepad_v1");
        metadata["diagnostic_only"]=serde_json::json!(gamepad_diagnostic);
        metadata["input_profile"]=serde_json::to_value(p)?;
        metadata["gamepad_slot"]=serde_json::json!(gamepad_slot);
        #[cfg(windows)]
        {metadata["physical_gamepad_capabilities"]=serde_json::json!(gamepad_capabilities);}
        metadata["input_clock"]=serde_json::json!("same Instant origin as telemetry elapsed_ns; host read times, not game application time");
        std::fs::copy(&p.controls,path.join("controls.snapshot.json"))?;
        std::fs::copy(&p.assists,path.join("assists.snapshot.json"))?;
        std::fs::write(path.join("metadata.json"),serde_json::to_vec_pretty(&metadata)?)?;
    }
    eprintln!("Recording session: {}", path.display());
    eprintln!("Waiting for live ACC. Notes + Enter mark events; /stop or Ctrl+C finishes.");
    let stopped = Arc::new(AtomicBool::new(false));
    let flag = stopped.clone();
    ctrlc::set_handler(move || flag.store(true, Ordering::Release))?;
    let (notes_tx, notes_rx) = mpsc::sync_channel::<String>(128);
    if managed.is_none() {
        thread::spawn(move || {
            for line in io::stdin().lock().lines() {
                match line {
                    Ok(note) => {
                        if notes_tx.send(note).is_err() {
                            break;
                        }
                    }
                    Err(_) => break,
                }
            }
        });
    }
    let mut sink = Sink::start_with_inputs(&path, config.queue_capacity,profile.is_some())?;
    let mut input_sequence=0u64;
    let mut previous_packet:Option<u32>=None;
    let mut profile_check=Instant::now();
    let mut reader = Reader::new(
        start,
        Duration::from_secs_f64(config.stale_seconds),
        Duration::from_secs_f64(config.retry_seconds),
    );
    let mut tracker = Tracker::new(config.teleport_margin_m);
    let mut read_times = Timing::default();
    let mut poll_times = Timing::default();
    let mut sequence = 0u64;
    let mut polls = 0u64;
    let mut skipped = 0u64;
    let mut error = None;
    let mut reason = "time_limit";
    let mut first_identity = None;
    let mut initial_conditions = None;
    let mut last_identity = None;
    let period = Duration::from_secs_f64(1.0 / config.poll_hz);
    let mut next = start;
    let mut previous_poll = None;
    let mut last_status = start;
    let mut last_progress = start - Duration::from_secs(1);
    let mut connection = "waiting";
    let mut speed_kmh = 0.;
    let work = (|| -> io::Result<()> {
        loop {
            if let Some(manager) = managed {
                if let Some(stop) = manager.control.stop_reason() {
                    reason = stop;
                    break;
                }
                if hotkeys
                    .as_ref()
                    .is_some_and(|keys| keys.stop.load(Ordering::Acquire))
                {
                    reason = "f8";
                    break;
                }
                if !platform::acc_foreground() {
                    reason = "foreground_lost";
                    break;
                }
                if last_progress.elapsed() >= Duration::from_millis(200) {
                    manager.emit(serde_json::json!({"event":"progress","phase":"recording","connection":connection,"identity":last_identity,"speed_kmh":speed_kmh,"samples":sequence,"elapsed_seconds":start.elapsed().as_secs_f64(),"result_dir":path}));
                    last_progress = Instant::now();
                }
            }
            if stopped.load(Ordering::Acquire) {
                reason = "user_stop";
                break;
            }
            if duration.is_some_and(|d| start.elapsed() >= d) {
                break;
            }
            if sink.failed.load(Ordering::Acquire) {
                return Err(io::Error::other("File writer failed"));
            }
            if let Some(p)=&profile {
                if sink.dropped_rows>0 || sink.dropped_events>0 {return Err(io::Error::other("Gamepad recording queue overflow"));}
                if profile_check.elapsed()>=Duration::from_secs(1) {
                    p.verify().map_err(|e|io::Error::other(e.to_string()))?;
                    profile_check=Instant::now();
                }
            }
            while let Ok(note) = notes_rx.try_recv() {
                if note.trim() == "/stop" {
                    stopped.store(true, Ordering::Release);
                    break;
                }
                sink.send(Message::Event(Event {
                    elapsed_ns: start.elapsed().as_nanos() as u64,
                    segment_id: tracker.segment,
                    kind: "user_marker".into(),
                    detail: note,
                }))?;
            }
            let now = Instant::now();
            if let Some(p) = previous_poll {
                poll_times.add(now - p);
            }
            previous_poll = Some(now);
            let polled = reader.poll();
            read_times.add(now.elapsed());
            polls += 1;
            match polled {
                Poll::Sample(frame) => {
                    connection = "live";
                    speed_kmh = frame.sample.speed_kmh;
                    if last_identity.as_ref() != Some(&frame.identity) {
                        sink.send(Message::Event(Event {
                            elapsed_ns: frame.elapsed_ns,
                            segment_id: tracker.segment,
                            kind: "identity".into(),
                            detail: serde_json::to_string(&frame.identity)?,
                        }))?;
                        last_identity = Some(frame.identity.clone());
                    }
                    if first_identity.is_none() {
                        first_identity = Some(frame.identity.clone());
                        initial_conditions = Some(
                            serde_json::json!({"source":"ACC shared memory","session_type":frame.sample.session_type,"session_index":frame.sample.session_index,"clock_seconds":frame.sample.clock_seconds,"rain_intensity":frame.sample.rain_intensity,"fuel_litres":frame.sample.fuel_litres}),
                        );
                    }
                    sequence += 1;
                    let (row, events) = tracker.accept(frame, sequence, sink.dropped_rows);
                    for e in events {
                        sink.send(Message::Event(e))?;
                    }
                    sink.send(Message::Row(row))?;
                }
                Poll::State(state) => {
                    connection = state;
                    if managed.is_some() && state != "live" {
                        reason = "telemetry_unavailable";
                        break;
                    }
                    eprintln!("ACC: {state}");
                    if state != "live" {
                        tracker.interrupt(state);
                    }
                    sink.send(Message::Event(Event {
                        elapsed_ns: start.elapsed().as_nanos() as u64,
                        segment_id: tracker.segment,
                        kind: "connection_state".into(),
                        detail: state.into(),
                    }))?;
                }
                Poll::Error(e) => {
                    if managed.is_some() {
                        return Err(io::Error::other(e));
                    }
                    eprintln!("ACC read error: {e}");
                    tracker.interrupt("read_error");
                    sink.send(Message::Event(Event {
                        elapsed_ns: start.elapsed().as_nanos() as u64,
                        segment_id: tracker.segment,
                        kind: "read_error".into(),
                        detail: e,
                    }))?;
                }
                Poll::NoData => (),
            }
            #[cfg(windows)]
            if let Some(slot)=gamepad_slot {
                let a=start.elapsed().as_nanos() as u64;
                let value=poleposition_gamepad::native::read(slot);
                let b=start.elapsed().as_nanos() as u64;
                input_sequence+=1;
                sink.send(Message::Input(poleposition_gamepad::InputRow::new(input_sequence,tracker.segment,a,b,slot,value)))?;
                if value.is_none() || poleposition_gamepad::native::slots()!=vec![slot] {return Err(io::Error::other("Gamepad disconnected or device set changed"));}
                if poleposition_gamepad::native::capabilities(slot)!=gamepad_capabilities {return Err(io::Error::other("Gamepad capabilities changed"));}
                if let Some((packet,_))=value {
                    if previous_packet.is_some_and(|old:u32|packet.wrapping_sub(old)>i32::MAX as u32) {return Err(io::Error::other("Gamepad packet reset/device replaced"));}
                    previous_packet=Some(packet);
                }
            }
            if last_status.elapsed() >= Duration::from_secs(5) {
                eprintln!(
                    "samples={sequence}, dropped={}, segments={}",
                    sink.dropped_rows, tracker.segment
                );
                last_status = Instant::now();
            }
            next += period;
            let now = Instant::now();
            if next <= now {
                let n =
                    (now.duration_since(next).as_secs_f64() / period.as_secs_f64()).floor() + 1.0;
                skipped += n as u64;
                next += period.mul_f64(n);
            }
            let wait = next.saturating_duration_since(Instant::now());
            thread::sleep(duration.map_or(wait, |d| wait.min(d.saturating_sub(start.elapsed()))));
        }
        Ok(())
    })();
    if let Err(e) = work {
        error = Some(e.to_string());
        reason = "error";
    }
    reader.close();
    if let Some(p)=&profile {if let Err(e)=p.verify(){error=Some(e.to_string());reason="profile_changed";}}
    let dropped_rows = sink.dropped_rows;
    let dropped_events = sink.dropped_events;
    let counts = match sink.finish() {
        Ok(c) => Some(c),
        Err(e) => {
            error = Some(e.to_string());
            reason = "writer_error";
            None
        }
    };
    let summary = serde_json::json!({"schema_version":1,"session_id":id,"normal_shutdown":error.is_none(),"reason":reason,"error":error,"received_samples":sequence,"written_samples":counts.map(|c|c.0),"written_events":counts.map(|c|c.1),"dropped_samples":dropped_rows,"dropped_events":dropped_events,"first_identity":first_identity,"segments":tracker.segment,"polls":polls,"missed_periods":skipped,"elapsed_ns":start.elapsed().as_nanos(),"read_timing":read_times.report(),"poll_timing":poll_times.report()});
    metadata["automatic_identity"] = serde_json::to_value(&first_identity)?;
    let mut summary=summary;
    if profile.is_some(){
        summary["gamepad_training_eligible"]=serde_json::json!(!gamepad_diagnostic && error.is_none() && dropped_rows==0 && dropped_events==0);
        summary["input_samples"]=serde_json::json!(input_sequence);
    }
    metadata["automatic_initial_conditions"] = serde_json::to_value(&initial_conditions)?;
    write_json(&path.join("metadata.final.tmp"), &metadata)?;
    std::fs::rename(path.join("metadata.final.tmp"), path.join("metadata.json"))?;
    write_json(&path.join("summary.json"), &summary)?;
    if managed.is_none() {
        println!("Saved: {}", path.display());
    }
    if let Some(e) = error {
        return Err(e.into());
    }
    Ok((Some(path), reason.into()))
}
fn main() {
    let managed = std::env::args()
        .any(|arg| arg == "--managed")
        .then(Managed::start);
    match run(managed.as_ref()) {
        Ok((path, reason)) => {
            if let Some(manager) = managed {
                let state = if reason == "time_limit" && path.is_none() {
                    "stopped"
                } else {
                    poleposition_management::outcome(&reason)
                };
                manager.finish(state, &reason, path.as_deref());
            }
        }
        Err(e) => {
            eprintln!("Recorder error: {e}");
            if let Some(manager) = managed {
                manager.finish("failed", &e.to_string(), None);
            }
            std::process::exit(1);
        }
    }
}
