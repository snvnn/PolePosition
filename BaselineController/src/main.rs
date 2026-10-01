use poleposition_baseline::{Config, Controller, Observation, target};
use poleposition_state::{
    model::{Prepared, Result, hash},
    processor::{Observation as Raw, Processor},
    wrap,
    writer::{create_output, finish_manifest, write_json},
};
use serde_json::json;
use std::{collections::BTreeMap, path::Path};
fn main() {
    if let Err(e) = run() {
        eprintln!("Baseline: {e}");
        std::process::exit(1);
    }
}
fn run() -> Result<()> {
    let mut args = std::env::args().skip(1);
    if args.next().as_deref() != Some("verify") {
        return Err(
            "Usage: poleposition-baseline verify --prepared <folder> [--config <TOML>]".into(),
        );
    }
    let mut opts = BTreeMap::new();
    while let Some(k) = args.next() {
        let v = args.next().ok_or("Missing value")?;
        if opts.insert(k, v).is_some() {
            return Err("Duplicate option".into());
        }
    }
    let path = opts.remove("--prepared").ok_or("Missing prepared")?;
    let config_path = opts
        .remove("--config")
        .unwrap_or("BaselineController/config.toml".into());
    if !opts.is_empty() {
        return Err("Unknown option".into());
    }
    let prepared = Prepared::load(Path::new(&path))?;
    let config = Config::load(Path::new(&config_path))?;
    let output = create_output(Path::new("baseline_runs"), "offline", None)?;
    let mut manifest = json!({"schema_version":1,"kind":"baseline_offline","complete":false,"tool_version":"0.1.0","config":config,"prepared":prepared.path,
        "input_sha256":{prepared.path.join("manifest.json").to_string_lossy().into_owned():hash(&prepared.path.join("manifest.json"))?,std::fs::canonicalize(&config_path)?.to_string_lossy().into_owned():hash(Path::new(&config_path))?},"output_sha256":{},
        "limitations":"Recorded observations are gate diagnostics. Toy simulation checks planar tracking, not ACC response, road grade or actual vehicle dynamics. No vJoy loaded."});
    write_json(&output.join("manifest.json"), &manifest)?;
    let result = (|| -> Result<()> {
        let mut w = csv::Writer::from_writer(
            std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(output.join("reference_audit.csv"))?,
        );
        w.write_record([
            "index",
            "s_m",
            "target_x",
            "target_z",
            "requested_curvature",
            "steering_requested",
            "within_envelope",
        ])?;
        let mut outside = 0;
        let mut max_steering = 0f64;
        for p in &prepared.model.points {
            let (tx, tz) = target(&prepared.model, p.s_m + config.lookahead_m);
            let dx = tx - p.position_x;
            let dz = tz - p.position_z;
            let k = 2. * (p.tangent_x * dz - p.tangent_z * dx) / (dx * dx + dz * dz);
            let steer = k * config.steering_per_curvature_m;
            max_steering = max_steering.max(steer.abs());
            if steer.abs() > config.max_steering {
                outside += 1;
            }
            w.serialize((
                p.index,
                p.s_m,
                tx,
                tz,
                k,
                steer,
                steer.abs() <= config.max_steering,
            ))?;
        }
        w.flush()?;
        // Replay real observations through StateProcessor + the controller independently
        // per sample. No speed is replaced, and no trajectory outcome is inferred.
        let source = Path::new(prepared.replay["source_path"].as_str().ok_or("No source")?);
        let mut core = Processor::new(
            prepared.model.clone(),
            prepared.config.clone(),
            prepared.identity.clone(),
            prepared.sign,
            prepared.offset,
        );
        let mut reasons = BTreeMap::<String, u64>::new();
        let mut rows = 0u64;
        let mut replay = csv::Writer::from_writer(
            std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(output.join("replay_gate_audit.csv"))?,
        );
        replay.write_record(["sequence", "state_status", "gate_result"])?;
        // Each source record is audited at its actual speed; this is not a controller tuning pass.
        for row in csv::Reader::from_path(source.join("telemetry.csv"))?.deserialize::<Raw>() {
            let mut raw = row?;
            raw.identity = Some(prepared.identity.clone());
            let now = raw.elapsed_ns;
            let state = core.update(raw, now);
            let o = Observation::from_state(&state, now, 2);
            if let Some(o) = &o {
                *reasons
                    .entry(
                        o.check(&config, now, true)
                            .err()
                            .unwrap_or("geometry_and_speed_eligible")
                            .into(),
                    )
                    .or_default() += 1;
            } else {
                *reasons.entry("no_ready_path_state".into()).or_default() += 1;
            }
            // Gate audit only: no hypothetical control outputs are fabricated for recorded human driving.
            let reason = o
                .as_ref()
                .and_then(|o| o.check(&config, now, true).err())
                .unwrap_or(if o.is_none() {
                    "no_ready_path_state"
                } else {
                    "geometry_and_speed_eligible"
                });
            replay.serialize((state.sequence, state.status, reason))?;
            rows += 1;
        }
        replay.flush()?;
        // Closed-loop toy plant on 3 straight/gentle starts, driven by the real controller.
        // Same approximate gain used by the plant: numerical wiring test, not ACC evidence.
        let mut simulations = Vec::new();
        let start_indices = if config.single_lap {
            vec![3179usize]
        } else {
            vec![0usize, 1000, 2500]
        };
        for start_index in start_indices {
            let p = &prepared.model.points[start_index];
            let mut controller = Controller::new(prepared.model.clone(), config.clone())?;
            let mut processor = Processor::new(
                prepared.model.clone(),
                prepared.config.clone(),
                prepared.identity.clone(),
                prepared.sign,
                prepared.offset,
            );
            let (mut x, mut z, mut heading, mut speed) =
                (p.position_x, p.position_z, p.heading_path_rad, 0f64);
            let mut max_error = 0f64;
            let mut driven = 0.;
            let mut last_reason = String::new();
            let mut abort = false;
            let step_ns = if config.single_lap {
                50_000_000u64
            } else {
                10_000_000
            };
            let dt = step_ns as f64 / 1e9;
            let steps = if config.single_lap {
                (config.duration_s / dt) as u64 + 50
            } else {
                3100
            };
            for step in 0..steps {
                let now = 1_000_000_000 + step * step_ns;
                let raw = Raw {
                    sequence: step + 1,
                    segment_id: 0,
                    elapsed_ns: now,
                    physics_changed_ns: now,
                    graphics_changed_ns: now,
                    physics_packet_id: step as i32,
                    graphics_packet_id: step as i32,
                    position_x: x,
                    position_y: p.position_y,
                    position_z: z,
                    speed_kmh: speed,
                    heading_rad: wrap((heading - prepared.offset) / prepared.sign),
                    lap_valid: true,
                    in_pit: false,
                    completed_laps: 0,
                    session_type: 0,
                    session_index: 0,
                    game_status: "live".into(),
                    identity: Some(prepared.identity.clone()),
                };
                let state = processor.update(raw, now);
                max_error = max_error.max(state.projection_distance_m.unwrap_or(0.));
                let o = Observation::from_state(&state, now, 2);
                let out = controller.update(
                    o.as_ref(),
                    now,
                    (now >= 1_600_000_000).then_some(1_600_000_000),
                );
                speed = (speed
                    + (0.8 * out.throttle - 3. * out.brake - speed / 3.6 * 0.015) * 0.01 * 3.6)
                    .max(0.);
                if config.single_lap {
                    // Ideal external speed hold isolates path geometry; no hill model inferred.
                    speed = if out.reason == "tracking" {
                        config.target_speed_kmh
                    } else {
                        0.
                    };
                }
                let ds = speed / 3.6 * dt;
                heading = wrap(heading + ds * out.steering / config.steering_per_curvature_m);
                x += heading.cos() * ds;
                z += heading.sin() * ds;
                driven += ds;
                last_reason = out.reason;
                if out.abort || out.complete {
                    abort = out.abort;
                    break;
                }
            }
            simulations.push(json!({"start_index":start_index,"distance_m":driven,"maximum_path_error_m":max_error,"termination":last_reason,"aborted":abort}));
        }
        prepared.verify()?;
        write_json(
            &output.join("validation.json"),
            &json!({"reference_points":prepared.model.points.len(),"reference_outside_steering_envelope":outside,"reference_max_requested_steering":max_steering,"recorded_observations":rows,"recorded_gate_counts":reasons,"toy_simulations":simulations,"input_unchanged":true,"live_validation":false}),
        )?;
        for name in [
            "reference_audit.csv",
            "replay_gate_audit.csv",
            "validation.json",
        ] {
            manifest["output_sha256"][name] = json!(hash(&output.join(name))?);
        }
        Ok(())
    })();
    manifest["complete"] = json!(result.is_ok());
    if let Err(e) = &result {
        manifest["error"] = json!(e.to_string());
    }
    finish_manifest(&output.join("manifest.json"), &manifest)?;
    println!("{}", output.display());
    result
}
