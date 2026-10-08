use poleposition_actuator::{Config, Result};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};
fn main() {
    let managed = std::env::args()
        .any(|arg| arg == "--managed")
        .then(poleposition_management::Managed::start);
    match run(managed.as_ref()) {
        Ok(path) => {
            if let Some(manager) = managed {
                let summary = path
                    .as_ref()
                    .and_then(|p| std::fs::read(p.join("summary.json")).ok())
                    .and_then(|bytes| serde_json::from_slice::<serde_json::Value>(&bytes).ok());
                let reason = summary
                    .as_ref()
                    .and_then(|s| s["termination_reason"].as_str())
                    .unwrap_or("missing_summary");
                let state = if summary
                    .as_ref()
                    .is_none_or(|s| s["normal_shutdown"] != true)
                {
                    "failed"
                } else {
                    poleposition_management::outcome(reason)
                };
                manager.finish(state, reason, path.as_deref());
            }
        }
        Err(e) => {
            eprintln!("Actuator: {e}");
            let state = e
                .downcast_ref::<poleposition_management::PreparationStopped>()
                .map_or("failed", |stopped| {
                    poleposition_management::outcome(stopped.0)
                });
            if let Some(manager) = managed {
                manager.finish(state, &e.to_string(), None);
            }
            std::process::exit(i32::from(state == "failed"));
        }
    }
}
fn run(managed: Option<&poleposition_management::Managed>) -> Result<Option<PathBuf>> {
    let mut args = std::env::args().skip(1);
    let command = args
        .next()
        .ok_or("Usage: poleposition-actuator bind|test [options]")?;
    let mut opts = BTreeMap::new();
    while let Some(key) = args.next() {
        if key == "--managed" {
            continue;
        }
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
    if managed.is_some()
        && !["test", "demo", "policy", "v2-shadow", "rl-supervisor"].contains(&command.as_str())
    {
        return Err("Managed mode supports only test, demo, policy".into());
    }
    let mut unlimited_management = false;
    if let Some(manager) = managed {
        let seconds: f64 = opts
            .remove("--managed-seconds")
            .unwrap_or("7200".into())
            .parse()?;
        if seconds == 0. && command == "demo" {
            unlimited_management = true;
        } else {
            manager.control.set_time_limit(seconds)?;
        }
    }
    if command == "rl-supervisor" {
        let session = PathBuf::from(opts.remove("--session").ok_or("Missing session")?);
        if !opts.is_empty() {
            return Err("Unknown supervisor option".into());
        }
        return Ok(Some(poleposition_actuator::rl::supervise(
            &session,
            managed.ok_or("Supervisor must be managed")?,
        )?));
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
        poleposition_actuator::ipc::worker(config, &scenario)?;
        return Ok(None);
    }
    let config_path = opts
        .remove("--config")
        .unwrap_or("ModelAndActuator/config.toml".into());
    let mut config = poleposition_management::prepare(managed, "config", || {
        Config::load(Path::new(&config_path))
    })?;
    if unlimited_management {
        if !config.reinforcement.as_ref().is_some_and(|r| r.unlimited_duration) {
            return Err("Unlimited management requires explicit human trial duration setting".into());
        }
        managed.unwrap().control.clear_time_limit();
    }
    match command.as_str() {
        "self-test" => {
            let fault = opts.remove("--fault").ok_or("Missing --fault")?;
            if !opts.is_empty() {
                return Err("Unknown self-test option".into());
            }
            let path = poleposition_actuator::simulation::run(config, &fault)?;
            println!("{}", path.display());
            Ok(Some(path))
        }
        "test" | "demo" | "policy" | "v2-shadow" => {
            let scenario = if ["demo", "policy", "v2-shadow"].contains(&command.as_str()) {
                "demo".into()
            } else {
                opts.remove("--scenario").ok_or("Missing --scenario")?
            };
            let prepared = opts.remove("--prepared").ok_or("Missing --prepared")?;
            if ["demo", "policy", "v2-shadow"].contains(&command.as_str()) {
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
                if command == "v2-shadow" {
                    config.v2_shadow = Some(poleposition_actuator::v2_shadow::Config {
                        training: std::fs::canonicalize(
                            opts.remove("--model").ok_or("Missing --model")?,
                        )?
                        .to_string_lossy()
                        .into_owned(),
                    });
                }
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
            let path = poleposition_actuator::runner::test_managed(
                config,
                Path::new(&prepared),
                &scenario,
                Path::new(&profile),
                managed,
            )?;
            if managed.is_none() {
                println!("{}", path.display());
            }
            Ok(Some(path))
        }
        "bind" => {
            let axis = opts.remove("--axis").ok_or("Missing --axis")?;
            let confirmed = opts.remove("--settings-confirmed").is_some();
            let main_menu = opts.remove("--main-menu-confirmed").is_some();
            if !opts.is_empty() {
                return Err("Unknown bind option".into());
            }
            poleposition_actuator::runner::bind(config, &axis, confirmed, main_menu)?;
            Ok(None)
        }
        _ => Err("Usage: poleposition-actuator bind|test [options]".into()),
    }
}
