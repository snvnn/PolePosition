use poleposition_telemetry::{Poll, Reader, TelemetrySource};
use std::time::{Duration, Instant};
fn main() {
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
