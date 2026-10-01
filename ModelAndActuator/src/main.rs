use poleposition_actuator::{Config, Result};
use std::{collections::BTreeMap, path::Path};
fn main() {
    if let Err(e) = run() {
        eprintln!("Actuator: {e}");
        std::process::exit(1);
    }
}
fn run() -> Result<()> {
    let mut args = std::env::args().skip(1);
    let command = args
        .next()
        .ok_or("Usage: poleposition-actuator bind|test [options]")?;
    let mut opts = BTreeMap::new();
    while let Some(key) = args.next() {
        if !key.starts_with("--") || opts.contains_key(&key) {
            return Err("Invalid/duplicate option".into());
        }
        let value = if key == "--settings-confirmed" || key == "--main-menu-confirmed" {
            "true".into()
        } else {
            args.next().ok_or("Missing option value")?
        };
        opts.insert(key, value);
    }
    if command == "worker" {
        let config: Config = serde_json::from_slice(&std::fs::read(
            opts.remove("--config-json").ok_or("Missing config JSON")?,
        )?)?;
        config.validate()?;
        let scenario = opts.remove("--scenario").ok_or("Missing scenario")?;
        if !opts.is_empty() {
            return Err("Unknown worker option".into());
        }
        return poleposition_actuator::ipc::worker(config, &scenario);
    }
    let config_path = opts
        .remove("--config")
        .unwrap_or("ModelAndActuator/config.toml".into());
    let mut config = Config::load(Path::new(&config_path))?;
    match command.as_str() {
        "self-test" => {
            let fault = opts.remove("--fault").ok_or("Missing --fault")?;
            if !opts.is_empty() {
                return Err("Unknown self-test option".into());
            }
            let path = poleposition_actuator::simulation::run(config, &fault)?;
            println!("{}", path.display());
            Ok(())
        }
        "test" | "demo" | "policy" => {
            let scenario = if command == "demo" || command == "policy" {
                "demo".into()
            } else {
                opts.remove("--scenario").ok_or("Missing --scenario")?
            };
            let prepared = opts.remove("--prepared").ok_or("Missing --prepared")?;
            if command == "demo" || command == "policy" {
                let path = opts
                    .remove("--controller-config")
                    .unwrap_or("BaselineController/config.toml".into());
                let controller_config = poleposition_baseline::Config::load(Path::new(&path))?;
                if controller_config.single_lap {
                    config.max_steering = controller_config.max_steering;
                    config.test_seconds = controller_config.duration_s;
                    config.arm_wait_seconds = 600.;
                }
                config.baseline = Some(poleposition_actuator::Demo {
                    prepared: std::fs::canonicalize(&prepared)?
                        .to_string_lossy()
                        .into_owned(),
                    config: controller_config,
                });
                if command == "policy" {
                    config.policy = Some(poleposition_actuator::policy::Config {
                        model: std::fs::canonicalize(
                            opts.remove("--model").ok_or("Missing --model")?,
                        )?
                        .to_string_lossy()
                        .into_owned(),
                        mode: opts.remove("--mode").ok_or("Missing --mode")?,
                        model_seconds: opts
                            .remove("--model-seconds")
                            .unwrap_or("5".into())
                            .parse()?,
                        previous_run: opts
                            .remove("--previous-run")
                            .map(std::fs::canonicalize)
                            .transpose()?
                            .map(|p| p.to_string_lossy().into_owned()),
                    });
                }
                config.validate()?;
            }
            let profile = opts
                .remove("--profile")
                .unwrap_or_else(|| config.profile.clone());
            if !opts.is_empty() {
                return Err("Unknown test option".into());
            }
            let path = poleposition_actuator::runner::test(
                config,
                Path::new(&prepared),
                &scenario,
                Path::new(&profile),
            )?;
            println!("{}", path.display());
            Ok(())
        }
        "bind" => {
            let axis = opts.remove("--axis").ok_or("Missing --axis")?;
            let confirmed = opts.remove("--settings-confirmed").is_some();
            let main_menu = opts.remove("--main-menu-confirmed").is_some();
            if !opts.is_empty() {
                return Err("Unknown bind option".into());
            }
            poleposition_actuator::runner::bind(config, &axis, confirmed, main_menu)
        }
        _ => Err("Usage: poleposition-actuator bind|test [options]".into()),
    }
}
