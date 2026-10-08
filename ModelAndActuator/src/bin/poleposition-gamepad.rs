fn main() {
    #[cfg(windows)]
    {
        let args: Vec<String> = std::env::args().skip(1).collect();
        if args.len() == 3 && args[0] == "--bench" {
            if let Err(e) = poleposition_actuator::gamepad::bench(
                std::path::Path::new(&args[1]),
                std::path::Path::new(&args[2]),
            ) {
                eprintln!("{e}");
                std::process::exit(1);
            }
            return;
        }
        if args.as_slice() == ["--probe"] {
            let slots:Vec<_>=poleposition_gamepad::native::slots().iter().map(|s|serde_json::json!({"slot":s,"state":poleposition_gamepad::native::read(*s),"capabilities":poleposition_gamepad::native::capabilities(*s)})).collect();
            println!(
                "{}",
                serde_json::json!({"semantics":"gamepad_v1","slots":slots})
            );
            return;
        }
        let managed = args
            .iter()
            .any(|s| s == "--managed")
            .then(poleposition_management::Managed::start);
        let config = args
            .windows(2)
            .find(|a| a[0] == "--config")
            .map(|a| a[1].clone());
        let result = config
            .ok_or_else(|| "--config required".into())
            .and_then(|p| {
                if args.iter().any(|s| s == "--response") {
                    poleposition_actuator::gamepad::response(
                        std::path::Path::new(&p),
                        managed.as_ref(),
                    )
                } else {
                    poleposition_actuator::gamepad::run(std::path::Path::new(&p), managed.as_ref())
                }
            });
        match result {
            Ok((path, reason)) => {
                if let Some(m) = managed {
                    m.finish(
                        if reason == "response_complete" {
                            "completed"
                        } else {
                            poleposition_management::outcome(&reason)
                        },
                        &reason,
                        Some(&path),
                    );
                } else {
                    println!("{}", path.display());
                }
            }
            Err(e) => {
                eprintln!("Gamepad: {e}");
                if let Some(m) = managed {
                    m.finish("failed", &e.to_string(), None);
                }
                std::process::exit(1);
            }
        }
    }
    #[cfg(not(windows))]
    {
        eprintln!("Windows required");
        std::process::exit(1);
    }
}
