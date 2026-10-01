use poleposition_state::{
    model::{Prepared, Result},
    runner,
};
use std::path::Path;

fn run() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() || !["replay", "live"].contains(&args[0].as_str()) {
        return Err(
            "Usage: poleposition-state replay|live --prepared DIR [--duration-seconds 180]".into(),
        );
    }
    let mut prepared = None;
    let mut duration: f64 = 180.0;
    let mut i = 1;
    while i < args.len() {
        let value = args.get(i + 1).ok_or("Missing argument value")?;
        match args[i].as_str() {
            "--prepared" => prepared = Some(value),
            "--duration-seconds" => duration = value.parse()?,
            _ => return Err("Unknown argument".into()),
        };
        i += 2;
    }
    if !duration.is_finite() || duration <= 0.0 {
        return Err("Invalid duration".into());
    }
    let prepared = Prepared::load(Path::new(prepared.ok_or("--prepared is required")?))?;
    let result = runner::run(prepared, &args[0], duration)?;
    println!("{}", result.display());
    Ok(())
}
fn main() {
    if let Err(e) = run() {
        eprintln!("StateProcessor failed: {e}");
        std::process::exit(1);
    }
}
