//! Real producer process + guardian core, synthetic telemetry and mock device only.
use crate::{
    Axes, Config, Result,
    core::{Action, Guard, Health, Mode, Signals},
    ipc::{Context, Producer},
    logger::{Message, Sink, TelemetryRow},
    platform::now_ns,
    runner::{command_row, event},
};
use poleposition_state::{
    model::hash,
    writer::{create_output, finish_manifest, write_json},
};
use poleposition_vjoy::{Device, MockDevice};
use serde_json::json;
use std::{
    path::{Path, PathBuf},
    thread,
    time::{Duration, Instant},
};
pub fn run(config: Config, fault: &str) -> Result<PathBuf> {
    if ![
        "manual-stop",
        "producer-kill",
        "producer-stall",
        "pipe-disconnect",
        "telemetry-stale",
        "focus-loss",
    ]
    .contains(&fault)
    {
        return Err("Unknown simulated fault".into());
    }
    let output = create_output(
        Path::new(&config.output_dir),
        &format!("mock-{fault}"),
        None,
    )?;
    write_json(&output.join("config.json"), &config)?;
    let mut manifest = json!({"schema_version":1,"kind":"actuator_run","tool_version":"0.1.0","complete":false,"mock":true,"scenario":fault,
        "config":config,"input_sha256":{},"output_sha256":{},"created_unix_ns":poleposition_telemetry::unix_ns(),"limitations":"Synthetic health/telemetry and mock device. No vJoy DLL loaded; not ACC validation."});
    write_json(&output.join("manifest.json"), &manifest)?;
    let result = (|| -> Result<()> {
        let mut producer = Producer::spawn(
            &std::env::current_exe()?,
            &output.join("config.json"),
            "crawl",
        )?;
        let mut guard = Guard::new(config.clone());
        let mut device = MockDevice::default();
        let mut sink = Sink::start(&output, config.queue_capacity, None)?;
        let base = now_ns();
        let mut h = Health::default();
        let mut count = 0;
        let mut fault_at = None;
        let mut last_mode = Mode::Waiting;
        let mut sent = 0;
        let mut last_send = None;
        let mut accepted_ipc = None;
        event(
            &mut sink,
            base,
            "mock_started",
            "Real child producer; synthetic stopped car; mock axes only",
        );
        loop {
            let now = now_ns();
            count += 1;
            let stale = fault_at.is_some() && fault == "telemetry-stale";
            if !stale {
                h = Health {
                    rpm: 1600,
                    observation_sequence: count,
                    observation_ns: now,
                    physics_ns: now,
                    graphics_ns: now,
                    tracking_epoch: 1,
                    speed_kmh: 0.,
                    gear_raw: 2,
                    ready: true,
                    live: true,
                    identity_ok: true,
                    foreground: true,
                    game_halted: false,
                };
            }
            if fault_at.is_some() && fault == "focus-loss" {
                h.foreground = false;
            }
            if !(fault_at.is_some() && fault == "producer-stall") {
                producer.publish(Context {
                    path: None,
                    health: h.clone(),
                    armed_ns: guard.armed_ns,
                    sent_ns: now,
                });
            }
            if let Some(reply) = producer.replies.take() {
                accepted_ipc = now.checked_sub(reply.command.created_ns);
                if let Err(e) = guard.accept(reply.command, now, &h)
                    && guard.mode == Mode::Running
                {
                    guard.stop(now, e);
                }
            }
            if guard.mode == Mode::Running
                && guard.armed_ns.is_some_and(|t| now - t >= 1_800_000_000)
                && fault_at.is_none()
            {
                fault_at = Some(now);
                event(&mut sink, now, "fault_injected", fault);
                match fault {
                    "producer-kill" => {
                        producer.child.kill()?;
                    }
                    "pipe-disconnect" => producer.disconnect(),
                    _ => {}
                }
            }
            let signals = Signals {
                start: now - base >= 700_000_000 && guard.mode == Mode::Waiting,
                stop: fault_at.is_some() && fault == "manual-stop",
                producer_lost: producer.lost(),
                log_failed: sink.failed.load(std::sync::atomic::Ordering::Acquire),
                ..Default::default()
            };
            let action = guard.tick(now, &h, signals);
            if guard.mode != last_mode {
                event(
                    &mut sink,
                    now,
                    "execution_state",
                    format!("{:?}:{}", guard.mode, guard.reason),
                );
                last_mode = guard.mode;
            }
            sink.push(Message::Telemetry(TelemetryRow {
                now_ns: now,
                sequence: count,
                segment_id: 1,
                tracking_epoch: 1,
                observation_ns: h.observation_ns,
                physics_ns: h.physics_ns,
                graphics_ns: h.graphics_ns,
                physics_packet_id: count as i32,
                graphics_packet_id: count as i32,
                speed_kmh: 0.,
                throttle_observed: 0.,
                brake_observed: 0.,
                steering_raw: 0.,
                position_x: 0.,
                position_y: 0.,
                position_z: 0.,
                lap_valid: true,
                in_pit: false,
                state_status: "synthetic".into(),
                state_reason: "Not read from ACC".into(),
                gear_raw: 2,
                rpm: 0,
                path_s_m: None,
                lateral_error_m: None,
                heading_error_rad: None,
                vehicle_heading_rad: None,
                projection_distance_m: None,
                accumulated_m: None,
                reference_elevation_m: None,
                reference_grade_pct: None,
                completed_laps: 0,
                lap_time_ms: 0,
                progress: 0.,
                sector: 0,
            }));
            match action {
                Action::Send(a) => {
                    let begin = Instant::now();
                    let raw = device.send(a)?;
                    let cost = begin.elapsed();
                    let at = now_ns();
                    let mut row = command_row(at, guard.command(), Some(a), &guard);
                    row.send_success = true;
                    row.axis_x = Some(raw[0]);
                    row.axis_y = Some(raw[1]);
                    row.axis_z = Some(raw[2]);
                    row.driver_ns = Some(cost.as_nanos() as u64);
                    row.ipc_ns = accepted_ipc;
                    row.update_interval_ns = last_send.map(|t| at - t);
                    last_send = Some(at);
                    guard.acknowledge(at, a);
                    sent += 1;
                    sink.push(Message::Command(row));
                }
                Action::Release => {
                    device.release()?;
                    event(
                        &mut sink,
                        now_ns(),
                        "mock_device_released",
                        "neutral; no physical device",
                    );
                    break;
                }
                Action::None => {}
            }
            if stale
                && guard.mode == Mode::Stopping
                && fault_at.is_some_and(|t| now - t > 800_000_000)
            {
                if device.sent.last() != Some(&Axes::STOP) {
                    return Err("Stale data did not hold braking".into());
                }
                h.game_halted = true;
            }
            if now - base > 6_000_000_000 {
                return Err("Mock guardian timed out".into());
            }
            thread::sleep(Duration::from_secs_f64(1. / config.poll_hz));
        }
        let dropped = sink.dropped;
        let counts = sink.finish()?;
        let latency = guard
            .first_zero_sent_ns
            .zip(guard.stop_ns)
            .map(|(b, a)| b.saturating_sub(a));
        let fault_latency = guard
            .first_zero_sent_ns
            .zip(fault_at)
            .map(|(b, a)| b.saturating_sub(a));
        let pass = device.released && dropped == 0 && latency.is_some_and(|n| n <= 150_000_000);
        write_json(
            &output.join("summary.json"),
            &json!({"schema_version":1,"mock":true,"normal_shutdown":pass,"recording_valid":dropped==0,"written":counts,"dropped":dropped,
            "termination_reason":guard.reason,"released":device.released,"stop_confirmed":guard.stop_confirmed,"stop_trigger_ns":guard.stop_ns,"first_zero_send_ns":guard.first_zero_sent_ns,
            "stop_to_zero_send_ns":latency,"fault_to_zero_send_ns":fault_latency,"stop_target_ns":150_000_000,"stop_target_pass":latency.map(|n|n<=150_000_000),"sent_reports":sent,
            "response_caveat":"Mock driver cost and synthetic observations cannot validate physical device or ACC latency."}),
        )?;
        if !pass {
            return Err("Mock guardian verification failed".into());
        }
        Ok(())
    })();
    for name in [
        "config.json",
        "commands.csv",
        "telemetry.csv",
        "events.csv",
        "summary.json",
    ] {
        if output.join(name).exists() {
            manifest["output_sha256"][name] = json!(hash(&output.join(name))?);
        }
    }
    if let Err(e) = &result {
        manifest["error"] = json!(e.to_string());
    }
    finish_manifest(&output.join("manifest.json"), &manifest)?;
    if output.join("summary.json").exists() {
        let report = std::process::Command::new("python")
            .args(["-m", "ModelAndActuator", "report", "--run"])
            .arg(&output)
            .output()?;
        if !report.status.success() {
            return Err(format!(
                "Mock report failed: {}",
                String::from_utf8_lossy(&report.stderr)
            )
            .into());
        }
        for name in ["report.html", "validation.json"] {
            manifest["output_sha256"][name] = json!(hash(&output.join(name))?);
        }
    }
    manifest["complete"] = json!(result.is_ok());
    finish_manifest(&output.join("manifest.json"), &manifest)?;
    result?;
    Ok(output)
}
