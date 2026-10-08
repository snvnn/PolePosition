use poleposition_telemetry::{Poll, Reader, TelemetrySource};
use std::time::{Duration, Instant};
fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args == ["--managed"] {
        managed_monitor();
        return;
    }
    if !args.is_empty() {
        eprintln!("Usage: poleposition-telemetry [--managed]");
        std::process::exit(1);
    }
    let start = Instant::now();
    let mut r = Reader::new(start, Duration::from_secs(2), Duration::from_secs(1));
    let mut last = start;
    while start.elapsed() < Duration::from_secs(10) {
        match r.poll() {
            Poll::Sample(f) if last.elapsed() > Duration::from_millis(100) => {
                println!("{}", serde_json::to_string(&f).unwrap());
                last = Instant::now();
            }
            Poll::State(s) => eprintln!("{s}"),
            Poll::Error(e) => eprintln!("{e}"),
            _ => (),
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}

/// Read-only GUI status: no hotkeys, device acquisition, or driving commands.
fn managed_monitor() {
    let manager = poleposition_management::Managed::start();
    let start = Instant::now();
    let mut reader = Reader::new(start, Duration::from_millis(100), Duration::from_secs(1));
    let mut last_display = start - Duration::from_secs(1);
    let mut connection = "waiting";
    let mut last_frame: Option<poleposition_telemetry::Frame> = None;
    let mut error = None;
    loop {
        if let Some(reason) = manager.control.stop_reason() {
            reader.close();
            manager.finish(poleposition_management::outcome(reason), reason, None);
            return;
        }
        match reader.poll() {
            Poll::Sample(frame) => {
                last_frame = Some(frame);
                connection = "live";
                error = None;
            }
            Poll::State(state) => {
                connection = state;
                if state != "live" {
                    last_frame = None;
                }
            }
            Poll::Error(message) => {
                connection = "read_error";
                last_frame = None;
                error = Some(message);
            }
            Poll::NoData => (),
        }
        if last_display.elapsed() >= Duration::from_millis(200) {
            let elapsed_ns = start.elapsed().as_nanos() as u64;
            let age_ns = last_frame.as_ref().map(|f| {
                elapsed_ns.saturating_sub(f.physics_changed_ns.min(f.graphics_changed_ns))
            });
            manager.emit(
                serde_json::json!({"event":"progress","phase":"monitoring","connection":connection,
                "identity":last_frame.as_ref().map(|f|&f.identity),
                "fresh":connection=="live" && age_ns.is_some_and(|age|age<100_000_000),
                "age_ms":age_ns.map(|age|age as f64/1e6),
                "speed_kmh":last_frame.as_ref().map(|f|f.sample.speed_kmh),
                "reason":error,"read_only":true}),
            );
            last_display = Instant::now();
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}
