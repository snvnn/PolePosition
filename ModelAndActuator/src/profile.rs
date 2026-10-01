use crate::Result;
use poleposition_state::{
    Identity,
    model::{json, verify_map},
};
use poleposition_vjoy::{Mapping, Probe, SDK_SHA256, hash, validate_probe};
use serde_json::Value;
use std::path::{Path, PathBuf};
pub struct Calibration {
    pub path: PathBuf,
    pub sha256: String,
    pub data: Value,
    pub mapping: Mapping,
}
impl Calibration {
    pub fn load(path: &Path, probe: &Probe, identity: &Identity) -> Result<Self> {
        let path = path.canonicalize()?;
        let data = json(&path)?;
        if data["schema_version"] != 1
            || data["kind"] != "vjoy_calibration"
            || data["complete"] != true
            || data["driver_release"] != "2.2.2.0"
            || data["dll_sha256"] != SDK_SHA256
            || data["identity"] != serde_json::to_value(identity)?
        {
            return Err(
                "Calibration missing/incomplete/incompatible with current prepared identity".into(),
            );
        }
        validate_probe(probe, true)?;
        let checks = [
            "windows_axes_verified",
            "acc_axis_assignment_verified",
            "direction_and_inversion_verified",
            "linear_settings_verified",
            "initial_neutral_verified",
            "released_neutral_verified",
            "xbox_disconnected",
            "separate_preset_saved",
        ];
        for check in checks {
            if data["checks"][check]["passed"] != true
                || data["checks"][check]["evidence"]
                    .as_str()
                    .is_none_or(|s| s.trim().is_empty())
            {
                return Err(format!("Calibration check/evidence missing: {check}").into());
            }
        }
        for field in ["reviewer", "reviewed_at", "preset_name"] {
            if data[field].as_str().is_none_or(|s| s.trim().is_empty()) {
                return Err(format!("Missing {field}").into());
            }
        }
        if data["driver_file_version"] != "2.2.2.0" {
            return Err("Driver file version not verified".into());
        }
        let hashes = data["input_sha256"]
            .as_object()
            .ok_or("Missing calibration input hashes")?;
        for field in [
            "controls_path",
            "assists_path",
            "driver_path",
            "backup_manifest_path",
        ] {
            let p = data[field]
                .as_str()
                .ok_or("Missing calibration settings/driver/backup path")?;
            if !hashes.contains_key(p) {
                return Err("Required calibration input not hashed".into());
            }
        }
        verify_map(&data["input_sha256"], None)?;
        validate_settings(
            Path::new(data["controls_path"].as_str().unwrap()),
            Path::new(data["assists_path"].as_str().unwrap()),
        )?;
        let mapping: Mapping = serde_json::from_value(data["mapping"].clone())?;
        mapping.validate()?;
        for (a, b) in [
            (mapping.steering, probe.mapping.steering),
            (mapping.throttle, probe.mapping.throttle),
            (mapping.brake, probe.mapping.brake),
        ] {
            if a.min != b.min || a.max != b.max {
                return Err("Axis range changed since calibration".into());
            }
        }
        let sha256 = hash(&path)?;
        Ok(Self {
            path,
            sha256,
            data,
            mapping,
        })
    }
    pub fn verify(&self) -> Result<()> {
        if hash(&self.path)? != self.sha256 {
            return Err("Calibration file changed".into());
        }
        verify_map(&self.data["input_sha256"], None)
    }
}

fn acc_json(path: &Path) -> Result<Value> {
    let bytes = std::fs::read(path)?;
    let text = if bytes.starts_with(&[0xff, 0xfe]) || bytes.starts_with(&[b'{', 0]) {
        let start = if bytes.starts_with(&[0xff, 0xfe]) {
            2
        } else {
            0
        };
        if (bytes.len() - start) % 2 != 0 {
            return Err("Malformed ACC UTF-16 settings".into());
        }
        String::from_utf16(
            &bytes[start..]
                .chunks_exact(2)
                .map(|c| u16::from_le_bytes([c[0], c[1]]))
                .collect::<Vec<_>>(),
        )?
    } else {
        String::from_utf8(bytes)?
            .trim_start_matches('\u{feff}')
            .to_owned()
    };
    Ok(serde_json::from_str(&text)?)
}
fn validate_settings(controls: &Path, assists: &Path) -> Result<()> {
    let c = acc_json(controls)?;
    let a = acc_json(assists)?;
    if c["carControllerMode"] != 0 || a["autoGear"] != 1 {
        return Err("ACC wheel preset and automatic gears are required".into());
    }
    let devices = c["commandDevices"]
        .as_array()
        .ok_or("Missing ACC devices")?;
    let device = devices
        .iter()
        .find(|d| {
            d["productName"]
                .as_str()
                .is_some_and(|s| s.to_ascii_lowercase().contains("vjoy"))
        })
        .ok_or("ACC preset has no vJoy device")?;
    if device["brakeGamma"].as_f64() != Some(1.) || device["steerLinearity"].as_f64() != Some(1.) {
        return Err("ACC vJoy linear steering/brake gamma 1 required".into());
    }
    let axes = device["raceCommandAxisList"]
        .as_array()
        .ok_or("Missing ACC vJoy axis assignments")?;
    for (code, index) in [("Steer", 0), ("Gas", 1), ("Brake", 2)] {
        let matches: Vec<_> = axes
            .iter()
            .filter(|axis| axis["axisCode"] == code)
            .collect();
        if matches.len() != 1 {
            return Err(format!("Missing or duplicate ACC active axis: {code}").into());
        }
        let axis = matches[0];
        let bounds = (axis["minRange"].as_f64(), axis["maxRange"].as_f64());
        if axis["axisIndex"] != index
            || axis["isCombined"] != false
            || !matches!(bounds, (Some(-1.), Some(1.)) | (Some(1.), Some(-1.)))
        {
            return Err(format!("ACC active vJoy X/Y/Z binding/range mismatch: {code}").into());
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use poleposition_vjoy::{AxisRange, VERSION};
    use serde_json::json;
    fn fixture() -> (PathBuf, Probe, Identity) {
        let path = std::env::temp_dir().join(format!(
            "poleposition-profile-{}-{}",
            std::process::id(),
            crate::platform::now_ns()
        ));
        std::fs::create_dir(&path).unwrap();
        let controls = path.join("controls.json");
        let assists = path.join("assists.json");
        let driver = path.join("vjoy.sys");
        let backup = path.join("backup.json");
        let bindings: Vec<_> = [("Steer",0),("Gas",1),("Brake",2)].into_iter().map(|(code,index)| json!({"axisCode":code,"axisIndex":index,"minRange":-1,"maxRange":1,"isCombined":false})).collect();
        std::fs::write(&controls, serde_json::to_vec(&json!({"carControllerMode":0,"commandDevices":[{"productName":"vJoy Device","brakeGamma":1,"steerLinearity":1,"raceCommandAxisList":bindings}]})).unwrap()).unwrap();
        let bytes: Vec<u8> = "{\"autoGear\":1}"
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect();
        std::fs::write(&assists, bytes).unwrap();
        std::fs::write(&driver, "synthetic driver bytes").unwrap();
        std::fs::write(&backup, "synthetic backup manifest").unwrap();
        let axis = AxisRange {
            min: 0,
            max: 32767,
            center: 16384,
            inverted: false,
        };
        let mapping = Mapping {
            device: 1,
            steering: axis,
            throttle: axis,
            brake: axis,
        };
        let identity = Identity {
            car: "bmw_m4_gt3".into(),
            track: "brands_hatch".into(),
            sm_version: "1.9".into(),
            game_version: "1.7".into(),
        };
        let probe = Probe {
            dll_path: "synthetic".into(),
            dll_sha256: SDK_SHA256.into(),
            dll_api_version: VERSION,
            driver_api_version: VERSION,
            enabled: true,
            status: 1,
            owner_pid: 0,
            mapping: mapping.clone(),
            initial_axes: None,
            position_read_status: 4319,
        };
        let mut data = json!({"schema_version":1,"kind":"vjoy_calibration","complete":true,"driver_release":"2.2.2.0","driver_file_version":"2.2.2.0",
            "dll_sha256":SDK_SHA256,"identity":identity,"mapping":mapping,"reviewer":"synthetic test","reviewed_at":"2026-09-29T12:00:00+09:00","preset_name":"test",
            "controls_path":controls,"assists_path":assists,"driver_path":driver,"backup_manifest_path":backup,"checks":{},"input_sha256":{}});
        for name in [
            "windows_axes_verified",
            "acc_axis_assignment_verified",
            "direction_and_inversion_verified",
            "linear_settings_verified",
            "initial_neutral_verified",
            "released_neutral_verified",
            "xbox_disconnected",
            "separate_preset_saved",
        ] {
            data["checks"][name] =
                json!({"passed":true,"evidence":"synthetic test fixture; not a real calibration"});
        }
        for file in [&controls, &assists, &driver, &backup] {
            data["input_sha256"][file.to_str().unwrap()] = json!(hash(file).unwrap());
        }
        std::fs::write(
            path.join("calibration.json"),
            serde_json::to_vec(&data).unwrap(),
        )
        .unwrap();
        (path, probe, identity)
    }
    #[test]
    fn calibration_file_and_settings_tamper() {
        let (p, probe, id) = fixture();
        let c = Calibration::load(&p.join("calibration.json"), &probe, &id).unwrap();
        std::fs::write(p.join("calibration.json"), "modified").unwrap();
        assert!(c.verify().is_err());
        std::fs::remove_dir_all(p).unwrap();
        let (p, probe, id) = fixture();
        std::fs::write(p.join("controls.json"), "modified").unwrap();
        assert!(Calibration::load(&p.join("calibration.json"), &probe, &id).is_err());
        std::fs::remove_dir_all(p).unwrap();
    }
    #[test]
    fn active_controls_require_actual_axis_assignments() {
        let (path, _, _) = fixture();
        let controls = path.join("controls.json");
        let assists = path.join("assists.json");
        validate_settings(&controls, &assists).unwrap();
        let original = json(&controls).unwrap();
        let mut missing = original.clone();
        missing["commandDevices"][0]["raceCommandAxisList"] = json!([]);
        std::fs::write(&controls, serde_json::to_vec(&missing).unwrap()).unwrap();
        assert!(validate_settings(&controls, &assists).is_err());
        let mut wrong = original;
        wrong["commandDevices"][0]["raceCommandAxisList"][1]["axisIndex"] = json!(2);
        std::fs::write(&controls, serde_json::to_vec(&wrong).unwrap()).unwrap();
        assert!(validate_settings(&controls, &assists).is_err());
        std::fs::remove_dir_all(path).unwrap();
    }
    #[test]
    fn unconfirmed_mapping_and_wrong_identity_rejected() {
        let (p, probe, mut id) = fixture();
        id.car = "different".into();
        assert!(Calibration::load(&p.join("calibration.json"), &probe, &id).is_err());
        id.car = "bmw_m4_gt3".into();
        let file = p.join("calibration.json");
        let mut data = json(&file).unwrap();
        data["checks"]["initial_neutral_verified"]["passed"] = json!(false);
        std::fs::write(&file, serde_json::to_vec(&data).unwrap()).unwrap();
        assert!(Calibration::load(&file, &probe, &id).is_err());
        std::fs::remove_dir_all(p).unwrap();
    }
}
