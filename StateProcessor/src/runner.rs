use crate::{
    Identity,
    model::{Prepared, Result, hash},
    processor::{Observation, Processor, State},
    writer::{self, Event, Message, Sink},
};
use poleposition_recorder::{Timing, session::Tracker};
use poleposition_telemetry::{Poll, Reader, TelemetrySource};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

pub fn state_fields() -> Vec<&'static str> {
    vec![
        "sequence",
        "segment_id",
        "observation_ns",
        "now_ns",
        "tracking_epoch",
        "status",
        "reason",
        "observation_age_ns",
        "physics_age_ns",
        "graphics_age_ns",
        "position_x",
        "position_y",
        "position_z",
        "speed_kmh",
        "lap_valid",
        "in_pit",
        "segment_index",
        "fraction",
        "projected_x",
        "projected_y",
        "projected_z",
        "projection_distance_m",
        "s_m",
        "accumulated_m",
        "lateral_error_m",
        "vehicle_heading_rad",
        "path_heading_rad",
        "heading_error_rad",
        "curvature_per_m",
        "forward_curvature_json",
    ]
}
#[derive(Default)]
struct Metrics {
    states: u64,
    observations: u64,
    events: u64,
    polls: u64,
    skipped: u64,
    statuses: BTreeMap<String, u64>,
    reasons: BTreeMap<String, u64>,
    core: Timing,
    poll: Timing,
    age: Timing,
    lap_status: BTreeMap<String, BTreeMap<String, u64>>,
    last_transition: Option<(String, String, u64)>,
}
impl Metrics {
    fn event(
        &mut self,
        sink: &mut Sink,
        live: bool,
        now: u64,
        kind: &str,
        detail: String,
    ) -> Result<()> {
        self.events += 1;
        sink.send(
            Message::Event(Event {
                now_ns: now,
                kind: kind.into(),
                detail,
            }),
            live,
        )
    }
    fn state(&mut self, sink: &mut Sink, live: bool, s: State, prepared: &Prepared) -> Result<()> {
        self.states += 1;
        *self.statuses.entry(s.status.clone()).or_default() += 1;
        if !s.reason.is_empty() {
            *self.reasons.entry(s.reason.clone()).or_default() += 1;
        }
        if let Some(age) = s.observation_age_ns {
            self.age.add(Duration::from_nanos(age));
        }
        let transition = (s.status.clone(), s.reason.clone(), s.tracking_epoch);
        if self.last_transition.as_ref() != Some(&transition) {
            self.event(
                sink,
                live,
                s.now_ns,
                "state_transition",
                serde_json::to_string(
                    &json!({"status":s.status,"reason":s.reason,"epoch":s.tracking_epoch}),
                )?,
            )?;
            self.last_transition = Some(transition);
        }
        if !live && let Some(t) = s.observation_ns {
            for lap in prepared.replay["laps"]
                .as_array()
                .ok_or("Missing replay lap inventory")?
            {
                if t >= lap["start_ns"].as_u64().unwrap()
                    && t < lap["end_ns"].as_u64().unwrap()
                    && s.segment_id == lap["segment_id"].as_u64()
                {
                    *self
                        .lap_status
                        .entry(lap["lap_id"].as_str().unwrap().into())
                        .or_default()
                        .entry(s.status.clone())
                        .or_default() += 1;
                }
            }
        }
        if live && let Ok(mut latest) = sink.latest.try_lock() {
            *latest = format!(
                "ACC {} {} | speed={:.1} km/h s={:.2?} lateral={:.2?} heading={:.3?} | samples={} drops={}",
                s.status,
                s.reason,
                s.speed_kmh.unwrap_or(0.0),
                s.s_m,
                s.lateral_error_m,
                s.heading_error_rad,
                self.observations,
                sink.dropped_states
            );
        }
        sink.send(Message::State(Box::new(s)), live)
    }
}
#[derive(Deserialize)]
struct SourceEvent {
    elapsed_ns: u64,
    segment_id: u64,
    kind: String,
    detail: String,
}
fn replay(p: &Prepared, core: &mut Processor, sink: &mut Sink, m: &mut Metrics) -> Result<String> {
    let source = Path::new(
        p.replay["source_path"]
            .as_str()
            .ok_or("Missing source path")?,
    );
    let events: Vec<SourceEvent> = csv::Reader::from_path(source.join("events.csv"))?
        .deserialize()
        .collect::<std::result::Result<_, _>>()?;
    let mut event_index = 0;
    let mut identity = p.identity.clone();
    let mut last_time = None;
    let mut source_rows = csv::Reader::from_path(source.join("telemetry.csv"))?;
    for row in source_rows.deserialize::<Observation>() {
        let mut o = row?;
        if last_time.is_some_and(|t| o.elapsed_ns <= t) {
            return Err("Source time order changed".into());
        }
        while event_index < events.len() && events[event_index].elapsed_ns <= o.elapsed_ns {
            let e = &events[event_index];
            if let Some(state) = core.tick(e.elapsed_ns) {
                m.state(sink, false, state, p)?;
            }
            m.event(
                sink,
                false,
                e.elapsed_ns,
                &format!("source_{}", e.kind),
                format!("segment={};{}", e.segment_id, e.detail),
            )?;
            if e.kind == "identity" {
                identity = serde_json::from_str(&e.detail)?;
            }
            if e.kind == "segment_start"
                || e.kind == "read_error"
                || (e.kind == "connection_state" && e.detail != "live")
            {
                let state = core.reset_state(&format!("{}:{}", e.kind, e.detail), e.elapsed_ns);
                m.state(sink, false, state, p)?;
            }
            event_index += 1;
        }
        // Replay host time without sleeping; tick at the next recorded observation/event.
        if let Some(t) = last_time {
            let expiry = t + (p.config.max_age_ms * 1e6) as u64;
            if o.elapsed_ns >= expiry
                && let Some(s) = core.tick(o.elapsed_ns)
            {
                m.state(sink, false, s, p)?;
            }
        }
        o.identity = Some(identity.clone());
        last_time = Some(o.elapsed_ns);
        m.observations += 1;
        let now = o.elapsed_ns;
        let start = Instant::now();
        let state = core.update(o, now);
        m.core.add(start.elapsed());
        m.state(sink, false, state, p)?;
    }
    for e in &events[event_index..] {
        if let Some(s) = core.tick(e.elapsed_ns) {
            m.state(sink, false, s, p)?;
        }
        m.event(
            sink,
            false,
            e.elapsed_ns,
            &format!("source_{}", e.kind),
            e.detail.clone(),
        )?;
        if e.kind == "connection_state" && e.detail != "live" {
            let s = core.reset_state(&e.detail, e.elapsed_ns);
            m.state(sink, false, s, p)?;
        }
    }
    Ok("source_end".into())
}
pub fn from_frame(row: poleposition_recorder::session::Row) -> Observation {
    let f = row.frame;
    let s = f.sample;
    Observation {
        sequence: row.sequence,
        segment_id: row.segment_id,
        elapsed_ns: f.elapsed_ns,
        physics_changed_ns: f.physics_changed_ns,
        graphics_changed_ns: f.graphics_changed_ns,
        physics_packet_id: s.physics_packet_id,
        graphics_packet_id: s.graphics_packet_id,
        position_x: s.position_x as f64,
        position_y: s.position_y as f64,
        position_z: s.position_z as f64,
        speed_kmh: s.speed_kmh as f64,
        heading_rad: s.heading_rad as f64,
        lap_valid: s.lap_valid,
        in_pit: s.in_pit,
        completed_laps: s.completed_laps,
        session_type: s.session_type,
        session_index: s.session_index,
        game_status: "live".into(),
        identity: Some(Identity {
            car: f.identity.car,
            track: f.identity.track,
            sm_version: f.identity.sm_version,
            game_version: f.identity.game_version,
        }),
    }
}
fn live(
    p: &Prepared,
    core: &mut Processor,
    sink: &mut Sink,
    m: &mut Metrics,
    duration: f64,
) -> Result<String> {
    let stop = Arc::new(AtomicBool::new(false));
    let signal = stop.clone();
    ctrlc::set_handler(move || signal.store(true, Ordering::Release))?;
    let start = Instant::now();
    let mut reader = Reader::new(
        start,
        Duration::from_secs_f64(p.config.stale_seconds),
        Duration::from_secs_f64(p.config.retry_seconds),
    );
    let mut tracker = Tracker::new(p.config.teleport_margin_m);
    let mut last_identity = None;
    let period = Duration::from_secs_f64(1.0 / p.config.poll_hz);
    let mut next = start;
    let mut previous_poll = None;
    while !stop.load(Ordering::Acquire) && start.elapsed().as_secs_f64() < duration {
        let poll_start = Instant::now();
        if let Some(t) = previous_poll {
            m.poll.add(poll_start - t);
        }
        previous_poll = Some(poll_start);
        m.polls += 1;
        match reader.poll() {
            Poll::Sample(f) => {
                if last_identity.as_ref() != Some(&f.identity) {
                    m.event(
                        sink,
                        true,
                        f.elapsed_ns,
                        "identity",
                        serde_json::to_string(&f.identity)?,
                    )?;
                    last_identity = Some(f.identity.clone());
                }
                m.observations += 1;
                let (row, events) = tracker.accept(f, m.observations, 0);
                for e in events {
                    if e.kind == "segment_start" {
                        let s = core.reset_state(&e.detail, e.elapsed_ns);
                        m.state(sink, true, s, p)?;
                    }
                    m.event(sink, true, e.elapsed_ns, &e.kind, e.detail)?;
                }
                let o = from_frame(row);
                let now = start.elapsed().as_nanos() as u64;
                let begin = Instant::now();
                let state = core.update(o, now);
                m.core.add(begin.elapsed());
                m.state(sink, true, state, p)?;
            }
            Poll::State(status) => {
                let now = start.elapsed().as_nanos() as u64;
                m.event(sink, true, now, "connection_state", status.into())?;
                if status != "live" {
                    tracker.interrupt(status);
                    let s = core.reset_state(status, now);
                    m.state(sink, true, s, p)?;
                }
            }
            Poll::Error(error) => {
                let now = start.elapsed().as_nanos() as u64;
                tracker.interrupt("read_error");
                m.event(sink, true, now, "read_error", error)?;
                let s = core.reset_state("read_error", now);
                m.state(sink, true, s, p)?;
            }
            Poll::NoData => {}
        }
        let now = start.elapsed().as_nanos() as u64;
        if let Some(state) = core.tick(now) {
            m.state(sink, true, state, p)?;
        }
        if let Some(error) = sink.error.lock().map_err(|_| "Writer error lock")?.clone() {
            return Err(error.into());
        }
        next += period;
        let now = Instant::now();
        if now >= next {
            let skipped =
                (now.duration_since(next).as_secs_f64() / period.as_secs_f64()).floor() + 1.0;
            m.skipped += skipped as u64;
            next += period.mul_f64(skipped);
        }
        std::thread::sleep(next.saturating_duration_since(Instant::now()));
    }
    reader.close();
    Ok(if stop.load(Ordering::Acquire) {
        "ctrl_c"
    } else {
        "time_limit"
    }
    .into())
}
pub fn run(p: Prepared, mode: &str, duration: f64) -> Result<PathBuf> {
    let is_live = mode == "live";
    let output = writer::create_output(Path::new(&p.config.output_dir), mode, None)?;
    let mut manifest = json!({"schema_version":1,"tool_version":"0.1.0","kind":"state_run","mode":mode,"complete":false,
        "prepared_path":p.path,"prepared_manifest_sha256":hash(&p.path.join("manifest.json"))?,"config":p.config,"duration_seconds":if is_live {Some(duration)} else {None},
        "identity":p.identity,"track_points":p.model.points.len(),"track_length_m":p.model.length,"created_unix_ns":poleposition_telemetry::unix_ns(),
        "fields":state_fields(),"time_basis":"monotonic host ns; replay uses original elapsed_ns; no simulation timestamp",
        "units":{"position":"m; ACC XYZ; projection in X-Z","s_m":"m wrapped [0,L)","accumulated_m":"signed metres since epoch first candidate",
            "lateral_error_m":"tx*(z-qz)-tz*(x-qx)","heading":"radians; [-pi,pi]","curvature":"1/m; offsets in config order","speed_kmh":"km/h"},
        "limitations":"Geometry quality does not authorize control or prove absence of accidents. Same-session diagnostics, not independent model evaluation."});
    writer::write_json(&output.join("manifest.json"), &manifest)?;
    eprintln!("State diagnostics output: {}", output.display());
    let result = (|| -> Result<()> {
        let mut sink = Sink::start(
            &output,
            p.config.queue_capacity,
            is_live.then_some(p.config.display_hz),
        )?;
        let mut core = Processor::new(
            p.model.clone(),
            p.config.clone(),
            p.identity.clone(),
            p.sign,
            p.offset,
        );
        let mut m = Metrics::default();
        let result = if is_live {
            live(&p, &mut core, &mut sink, &mut m, duration)
        } else {
            replay(&p, &mut core, &mut sink, &mut m)
        };
        let dropped_states = sink.dropped_states;
        let dropped_events = sink.dropped_events;
        let counts = sink.finish();
        let error = result
            .as_ref()
            .err()
            .map(|e| e.to_string())
            .or_else(|| counts.as_ref().err().map(|e| e.to_string()));
        let recording_valid = error.is_none() && dropped_states == 0 && dropped_events == 0;
        let summary = json!({"schema_version":1,"normal_shutdown":error.is_none(),"recording_valid":recording_valid,"error":error,
            "termination_reason":result.as_ref().ok(),"observations":m.observations,"calculated_rows":m.states,"generated_events":m.events,
            "written_rows":counts.as_ref().ok().map(|c|c.0),"written_events":counts.as_ref().ok().map(|c|c.1),
            "dropped_states":dropped_states,"dropped_events":dropped_events,"polls":m.polls,"missed_periods":m.skipped,
            "states":m.statuses,"reasons":m.reasons,"eligible_lap_states":m.lap_status,"core_timing":m.core.report(),
            "poll_timing":m.poll.report(),"observation_age":m.age.report(),"core_p99_target_us":1000,
            "core_p99_pass":if m.observations>0 {Some(m.core.report()["p99_us"].as_f64().unwrap_or(f64::INFINITY)<=1000.0)} else {None},
            "timing_scope":"Core update only. Poll interval and observation age separate; not end-to-end control latency."});
        writer::write_json(&output.join("summary.json"), &summary)?;
        p.verify()?;
        let report = format!(
            "<!doctype html><html><head><meta charset=\"utf-8\"><title>StateProcessor diagnostics</title></head><body><h1>StateProcessor</h1><p>Run Python report command for trajectory and signal graphs.</p><a href=\"summary.json\">Timing and state summary</a><pre>{}</pre></body></html>",
            serde_json::to_string_pretty(&summary)?
                .replace('&', "&amp;")
                .replace('<', "&lt;")
        );
        std::fs::write(output.join("report.html"), report)?;
        let mut hashes = serde_json::Map::new();
        for name in ["states.csv", "events.csv", "summary.json", "report.html"] {
            hashes.insert(name.into(), Value::String(hash(&output.join(name))?));
        }
        manifest["output_sha256"] = Value::Object(hashes);
        if let Some(error) = error {
            return Err(error.into());
        }
        if !recording_valid {
            return Err("Diagnostics queue overflow; recording verification failed".into());
        }
        manifest["complete"] = json!(true);
        Ok(())
    })();
    if let Err(e) = &result {
        manifest["error"] = json!(e.to_string());
    }
    writer::finish_manifest(&output.join("manifest.json"), &manifest)?;
    result?;
    Ok(output)
}
