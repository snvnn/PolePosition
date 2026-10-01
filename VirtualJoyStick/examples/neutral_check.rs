//! Hardware diagnostic: ACC must be closed. Sends only steering center / pedals released.
use poleposition_vjoy::{Axes, DEFAULT_DLL, Device, Result, native::Native, windows_read};
use std::{path::Path, process::Command, thread::sleep, time::Duration};

fn main() {
    if let Err(error) = run() {
        eprintln!("neutral check: {error}");
        std::process::exit(1);
    }
}
fn run() -> Result<()> {
    let status = Command::new("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
        .args(["-NoProfile", "-Command", "if (@(Get-Process -Name 'AC2*' -ErrorAction SilentlyContinue).Count) { exit 1 } else { exit 0 }"])
        .status()?;
    if !status.success() {
        return Err("ACC must be closed".into());
    }
    let mut device = Native::load(Path::new(DEFAULT_DLL))?;
    eprintln!("SDK loaded once; checking free device");
    let before = device.probe()?;
    let mut evidence = serde_json::json!({"kind":"hardware_neutral_check", "mock":false,
        "complete":false, "before":before, "windows_before":windows_read::positions()});
    evidence["duplicate_load_rejected"] =
        serde_json::json!(Native::load(Path::new(DEFAULT_DLL)).is_err());
    evidence["scope"] = serde_json::json!(
        "neutral send and release with an already-open observer; cold-start values and ACC response are not established"
    );
    device.acquire(before.mapping)?;
    eprintln!("Device acquired; sending neutral only");
    let test = (|| -> Result<()> {
        evidence["acquired"] = serde_json::to_value(device.probe()?)?;
        evidence["neutral_report"] = serde_json::to_value(device.send(Axes::NEUTRAL)?)?;
        sleep(Duration::from_millis(300));
        evidence["owned_after_send"] = serde_json::to_value(device.probe()?)?;
        evidence["windows_owned"] = serde_json::to_value(windows_read::positions())?;
        Ok(())
    })();
    let release = device.release();
    eprintln!("Device released; inspecting with the same SDK instance");
    sleep(Duration::from_millis(500));
    // The SDK keeps a process-local window class. Unloading/reloading the DLL
    // within one process can make RegisterClassEx fail and display a modal box.
    let released = device.probe()?;
    let windows = windows_read::positions();
    evidence["released"] = serde_json::to_value(&released)?;
    evidence["windows_released"] = serde_json::to_value(&windows)?;
    let value = &evidence["windows_released"];
    let neutral = value.as_array().is_some_and(|items| {
        items.len() == 1
            && items[0]["xyz"].as_array().is_some_and(|axes| {
                axes.len() == 3
                    && (axes[0].as_i64().unwrap_or(-100) - 32768).abs() <= 4
                    && axes[1].as_i64().unwrap_or(-100).abs() <= 4
                    && axes[2].as_i64().unwrap_or(-100).abs() <= 4
            })
    });
    evidence["complete"] =
        serde_json::json!(test.is_ok() && release.is_ok() && released.status == 1 && neutral);
    evidence["send_error"] = serde_json::json!(test.as_ref().err().map(ToString::to_string));
    evidence["release_error"] = serde_json::json!(release.as_ref().err().map(ToString::to_string));
    println!("{}", serde_json::to_string_pretty(&evidence)?);
    test?;
    release?;
    if !neutral || released.status != 1 {
        return Err("Released Windows axes / ownership were not neutral and Free".into());
    }
    Ok(())
}
