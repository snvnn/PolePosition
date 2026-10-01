use poleposition_recorder::{
    Config, Timing,
    session::{Event, Tracker, create_session},
    writer::{Message, Sink, write_json},
};
use poleposition_telemetry::{Poll, Reader, TelemetrySource, unix_ns};
use std::{
    io::{self, BufRead},
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut config_path = "DrivingRecorder/recorder.toml".to_string();
    let mut duration = None;
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
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
                return Ok(());
            }
            _ => return Err(format!("Unknown option {a}").into()),
        }
    }
    if !cfg!(windows) {
        return Err("ACC recording requires Windows".into());
    }
    let config: Config = toml::from_str(&std::fs::read_to_string(&config_path)?)?;
    config.validate()?;
    let start = Instant::now();
    let started_unix = unix_ns();
    let (id, path) = create_session(Path::new(&config.output_dir))?;
    let metadata = serde_json::json!({"schema_version":1,"session_id":id,"started_unix_ns":started_unix,"software_version":env!("CARGO_PKG_VERSION"),"config":config,"config_path":config_path,"user_declared":{"weather":config.weather,"traffic":config.traffic,"fuel":config.fuel,"tyres":config.tyres,"assists":config.assists},"identity_source":"ACC static page; first identity in summary, changes in events","time_basis":"host UTC Unix nanoseconds and monotonic elapsed nanoseconds; not simulation timestamps","units":{"speed_kmh":"km/h","position":"metres, ACC raw XYZ","heading_rad":"radians, ACC raw yaw","steering_raw":"ACC raw input; sign uncalibrated","throttle":"0..1","brake":"0..1","progress":"0..1","gear_raw":"0 reverse, 1 neutral, 2 first"},"completion":"see summary.json; absent summary means incomplete"});
    write_json(&path.join("metadata.json"), &metadata)?;
    let mut metadata = metadata;
    println!("Recording session: {}", path.display());
    println!("Waiting for live ACC. Notes + Enter mark events; /stop or Ctrl+C finishes.");
    let stopped = Arc::new(AtomicBool::new(false));
    let flag = stopped.clone();
    ctrlc::set_handler(move || flag.store(true, Ordering::Release))?;
    let (notes_tx, notes_rx) = mpsc::sync_channel::<String>(128);
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
    let mut sink = Sink::start(&path, config.queue_capacity)?;
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
    let work = (|| -> io::Result<()> {
        loop {
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
    metadata["automatic_initial_conditions"] = serde_json::to_value(&initial_conditions)?;
    write_json(&path.join("metadata.final.tmp"), &metadata)?;
    std::fs::rename(path.join("metadata.final.tmp"), path.join("metadata.json"))?;
    write_json(&path.join("summary.json"), &summary)?;
    println!("Saved: {}", path.display());
    if let Some(e) = error {
        return Err(e.into());
    }
    Ok(())
}
fn main() {
    if let Err(e) = run() {
        eprintln!("Recorder error: {e}");
        std::process::exit(1);
    }
}
