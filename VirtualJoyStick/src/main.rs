use poleposition_vjoy::{DEFAULT_DLL, Result, native::Native};
fn main() {
    if let Err(e) = run() {
        eprintln!("vJoy probe: {e}");
        std::process::exit(1);
    }
}
fn run() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) != Some("probe") {
        return Err("Usage: poleposition-vjoy probe [--dll PATH]".into());
    }
    let path = if args.len() == 3 && args[1] == "--dll" {
        &args[2]
    } else if args.len() == 1 {
        DEFAULT_DLL
    } else {
        return Err("Unknown probe arguments".into());
    };
    let native = Native::load(std::path::Path::new(path))?;
    let mut report = serde_json::to_value(native.probe()?)?;
    // A new WinMM client can initially return cached/default axes. Preserve
    // a short observation series rather than presenting its first read as final.
    let clock = std::time::Instant::now();
    let mut observations = Vec::new();
    for index in 0..21 {
        if index > 0 {
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        let positions = poleposition_vjoy::windows_read::positions();
        observations.push(serde_json::json!({
            "elapsed_ns":clock.elapsed().as_nanos(), "positions":positions
        }));
    }
    report["windows_positions"] = observations.last().unwrap()["positions"].clone();
    report["windows_position_observations"] = serde_json::to_value(observations)?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}
