//! Independent read-only observer, kept open across feeder shutdown.
fn main() {
    let start = std::time::Instant::now();
    let mut rows = Vec::new();
    for _ in 0..60 {
        rows.push(serde_json::json!({
            "elapsed_ns":start.elapsed().as_nanos(),
            "positions":poleposition_vjoy::windows_read::positions()
        }));
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    println!("{}", serde_json::to_string_pretty(&rows).unwrap());
}
