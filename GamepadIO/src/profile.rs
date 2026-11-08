use crate::*;
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};
fn setting(path: &Path) -> Result<serde_json::Value> {
    let bytes = std::fs::read(path)?;
    let text = if bytes.starts_with(&[255, 254]) || bytes.get(1) == Some(&0) {
        if bytes.len() % 2 != 0 {
            return Err("Invalid UTF16 settings".into());
        }
        let start = if bytes.starts_with(&[255, 254]) { 2 } else { 0 };
        String::from_utf16(
            &bytes[start..]
                .chunks_exact(2)
                .map(|x| u16::from_le_bytes([x[0], x[1]]))
                .collect::<Vec<_>>(),
        )?
    } else {
        String::from_utf8(bytes)?
            .trim_start_matches('\u{feff}')
            .to_string()
    };
    Ok(serde_json::from_str(&text)?)
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Profile {
    pub semantics: String,
    pub controls: PathBuf,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub controls_reference: Option<PathBuf>,
    pub assists: PathBuf,
    pub input_sha256: BTreeMap<PathBuf, String>,
    pub steam_input_disabled: bool,
    pub steam_input_evidence: String,
    pub dll: PathBuf,
    pub dll_sha256: String,
    pub driver_path: PathBuf,
    pub driver_sha256: String,
    pub device_validation: Option<PathBuf>,
}
impl Profile {
    pub fn load(path: &Path) -> Result<Self> {
        let p: Self = serde_json::from_slice(&std::fs::read(path)?)?;
        p.verify()?;
        Ok(p)
    }
    pub fn verify(&self) -> Result<()> {
        if self.semantics != SEMANTICS
            || !self.steam_input_disabled
            || self.steam_input_evidence.trim().is_empty()
            || !self.input_sha256.contains_key(&self.controls)
            || !self.input_sha256.contains_key(&self.assists)
        {
            return Err("Gamepad profile/Steam Input evidence incomplete".into());
        }
        for (p, h) in &self.input_sha256 {
            if &hash(p)? != h {
                if p == &self.controls { self.controls_hash()?; }
                else { return Err(format!("Settings changed: {}", p.display()).into()); }
            }
        }
        let c = setting(&self.controls)?;
        let a = setting(&self.assists)?;
        if c["carControllerMode"] != 1
            || c["gamepadSettings"]["useSteerWithRight"] != false
            || (a["autoGear"] != 1 && a["autoGear"] != 0)
            || a["autoClutch"] != 1
        {
            return Err("ACC requires left-stick gamepad mode and automatic clutch".into());
        }
        if hash(&self.dll)? != self.dll_sha256 || hash(&self.driver_path)? != self.driver_sha256 {
            return Err("Gamepad driver/client changed".into());
        }
        Ok(())
    }
    /// Return the pinned teacher hash only after proving current settings equivalent.
    pub fn controls_hash(&self) -> Result<String> {
        let expected = self.input_sha256.get(&self.controls).ok_or("Missing controls hash")?;
        verified_controls_hash(&self.controls, expected, self.controls_reference.as_deref())
    }
    /// Only forward the two pinned gamepad gear bindings, never control/menu buttons.
    pub fn shift_button_mask(&self) -> Result<u16> {
        shift_mask(&setting(&self.controls)?)
    }
    pub fn automatic_gear_up_button(&self) -> Result<u16> {
        let s=setting(&self.controls)?;
        shift_mask(&s)?;
        let list=s["gamepadSettings"]["raceCommandButtonList"].as_array().ok_or("Missing gamepad bindings")?;
        let entry=list.iter().find(|r|r["actionType"]=="GearUp").ok_or("Missing GearUp")?;
        match entry["key"]["key"].as_str() {
            Some("Gamepad_FaceButton_Bottom")=>Ok(0x1000),
            Some("Gamepad_FaceButton_Right")=>Ok(0x2000),
            Some("Gamepad_FaceButton_Left")=>Ok(0x4000),
            Some("Gamepad_FaceButton_Top")=>Ok(0x8000),
            _=>Err("Unsupported automatic GearUp binding".into()),
        }
    }
    pub fn verified_device(&self) -> Result<()> {
        if setting(&self.assists)?["autoGear"] != 1 {
            return Err("Teacher/model profile requires automatic gears; manual neutral diagnostics are not training clearance".into());
        }
        let file = self
            .device_validation
            .as_ref()
            .ok_or("Gamepad device validation required")?;
        let v: serde_json::Value = serde_json::from_slice(&std::fs::read(file)?)?;
        if v["semantics"] != SEMANTICS
            || v["passed"] != true
            || v["controls_sha256"] != self.controls_hash()?
            || v["assists_sha256"] != hash(&self.assists)?
            || v["dll_sha256"] != self.dll_sha256
            || v["driver_sha256"] != self.driver_sha256
        {
            return Err("Missing/mismatched in-game physical and virtual pad validation".into());
        }
        let files = v["evidence_sha256"]
            .as_object()
            .ok_or("Device evidence inventory missing")?;
        if files.len() < 4 || v["visual_response_confirmed"] != true {
            return Err("Both device recordings and visual response confirmation required".into());
        }
        for (p, h) in files {
            let actual = if Path::new(p) == self.controls {
                self.controls_hash()?
            } else { hash(Path::new(p))? };
            if h.as_str() != Some(&actual) {
                return Err(format!("Changed device evidence: {p}").into());
            }
        }
        Ok(())
    }
}


fn shift_mask(settings: &serde_json::Value) -> Result<u16> {
    let list=settings["gamepadSettings"]["raceCommandButtonList"].as_array().ok_or("Missing gamepad bindings")?;
    let mut mask=0u16;
    for action in ["GearUp", "GearDown"] {
        let entries:Vec<_>=list.iter().filter(|r|r["actionType"]==action).collect();
        if entries.len()!=1 {return Err("Exactly one binding per gear action required".into());}
        let key=&entries[0]["key"];
        if ["bShift","bCtrl","bAlt","bCmd"].iter().any(|k|key[*k].as_bool()!=Some(false)) {return Err("Modified gear bindings unsupported".into());}
        let bit=match key["key"].as_str() {
            Some("Gamepad_FaceButton_Bottom")=>0x1000,
            Some("Gamepad_FaceButton_Right")=>0x2000,
            Some("Gamepad_FaceButton_Left")=>0x4000,
            Some("Gamepad_FaceButton_Top")=>0x8000,
            _=>return Err("Gear passthrough requires face buttons; LB/RB/Back reserved".into()),
        };
        if mask&bit!=0 {return Err("Gear buttons overlap".into());}
        mask|=bit;
    }
    Ok(mask)
}

fn normalized_controls(mut value: serde_json::Value) -> Result<serde_json::Value> {
    let devices = value.get_mut("commandDevices").and_then(|v| v.as_array_mut())
        .ok_or("Missing commandDevices array")?;
    if devices.iter().filter(|d| unbound_virtual_device(d)).count()>1 {
        return Err("Multiple virtual controller inventory entries".into());
    }
    // Only the exact default, unbound ViGEm inventory entry is volatile.
    // Preserve all learned settings and every existing bound device.
    devices.retain(|d|!unbound_virtual_device(d));
    devices.sort_by_cached_key(|d| d.to_string());
    Ok(value)
}

fn unbound_virtual_device(device:&serde_json::Value)->bool {
    let Some(mut d)=device.as_object().cloned() else {return false;};
    if !d.remove("instanceGuid").is_some_and(|v|v.as_str().is_some_and(|s|!s.is_empty())) {return false;}
    serde_json::Value::Object(d)==serde_json::json!({
        "version":0,"productName":"Controller (XBOX 360 For Windows)",
        "productId":"42861662-0-0-00807368867368","steeringWheelModel":"None",
        "isGamePad":0,"isPedals":0,"iconTemplate":"None","uICommandButtonList":[],
        "raceCommandButtonList":[],"raceCommandAxisList":[],"forceFeedbackGain":1,
        "steerScale":1,"steerLock":900,"dynamicDamping":1,"steerLinearity":1,
        "roadEffects":0,"minDamper":0,"damperGain":1,"antaniGain":1,
        "minimumForceFeedback":0,"brakeGamma":1,"combinedPedals":0})
}

fn verified_controls_hash(current: &Path, expected: &str, reference: Option<&Path>) -> Result<String> {
    if hash(current)? == expected { return Ok(expected.to_owned()); }
    let reference = reference.ok_or("Controls changed; verified reference required")?;
    if hash(reference)? != expected { return Err("Controls reference hash mismatch".into()); }
    if normalized_controls(setting(current)?)? != normalized_controls(setting(reference)?)? {
        return Err("ACC input settings changed beyond unbound device inventory".into());
    }
    Ok(expected.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn inventory_entry_does_not_change_gamepad_settings() -> Result<()> {
        let virtual_pad:serde_json::Value=serde_json::from_str(include_str!("../tests/unbound-virtual-device.json"))?;
        let base=serde_json::json!({"carControllerMode":1,"gamepadSettings":{"gamma":3,"gear":"A"},
            "commandDevices":[{"id":"bound","axes":[1,2]}]});
        let mut changed=base.clone();changed["commandDevices"].as_array_mut().unwrap().push(virtual_pad.clone());
        assert_eq!(normalized_controls(base.clone())?,normalized_controls(changed.clone())?);
        for (key,value) in [("raceCommandAxisList",serde_json::json!([{"axisCode":"Steer"}])),
                            ("raceCommandButtonList",serde_json::json!([{"actionType":"GearUp"}])),
                            ("steerScale",serde_json::json!(2)),("unexpected",serde_json::json!(true))] {
            let mut altered=changed.clone();altered["commandDevices"][1][key]=value;
            assert_ne!(normalized_controls(base.clone())?,normalized_controls(altered)?);
        }
        changed["gamepadSettings"]["gamma"]=serde_json::json!(2);
        assert_ne!(normalized_controls(base.clone())?,normalized_controls(changed.clone())?);
        changed["commandDevices"].as_array_mut().unwrap().push(virtual_pad);
        assert!(normalized_controls(changed).is_err());
        Ok(())
    }
    #[test]
    fn shift_bindings_are_explicit_unique_and_not_reserved() {
        let mut c=serde_json::json!({"gamepadSettings":{"raceCommandButtonList":[
            {"actionType":"GearUp","key":{"key":"Gamepad_FaceButton_Bottom","bShift":false,"bCtrl":false,"bAlt":false,"bCmd":false}},
            {"actionType":"GearDown","key":{"key":"Gamepad_FaceButton_Left","bShift":false,"bCtrl":false,"bAlt":false,"bCmd":false}}
        ]}});
        assert_eq!(shift_mask(&c).unwrap(),0x5000);
        c["gamepadSettings"]["raceCommandButtonList"][1]["key"]["key"]=serde_json::json!("Gamepad_FaceButton_Bottom");
        assert!(shift_mask(&c).is_err());
        c["gamepadSettings"]["raceCommandButtonList"][1]["key"]["key"]=serde_json::json!("Gamepad_RightShoulder");
        assert!(shift_mask(&c).is_err());
    }
    #[test]
    fn accepts_only_device_order_and_requires_untampered_reference() -> Result<()> {
        let folder = std::env::temp_dir().join(format!("poleposition-controls-{}-{}",std::process::id(),std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_nanos()));
        std::fs::create_dir(&folder)?;
        let reference=folder.join("reference.json");let current=folder.join("current.json");
        let original=serde_json::json!({"carControllerMode":1,"gamepadSettings":{"gamma":2},"commandDevices":[{"id":"pad","axes":[1,2]},{"id":"vjoy","axes":[3,4]}]});
        std::fs::write(&reference,serde_json::to_vec(&original)?)?;
        let expected=hash(&reference)?;
        let mut reordered=original.clone();reordered["commandDevices"].as_array_mut().unwrap().reverse();
        std::fs::write(&current,serde_json::to_vec_pretty(&reordered)?)?;
        assert_eq!(verified_controls_hash(&current,&expected,Some(&reference))?,expected);
        assert!(verified_controls_hash(&current,&expected,None).is_err());
        for changed in [
            {let mut v=reordered.clone();v["gamepadSettings"]["gamma"]=serde_json::json!(3);v},
            {let mut v=reordered.clone();v["commandDevices"][0]["axes"]=serde_json::json!([4,3]);v},
            {let mut v=reordered.clone();v["commandDevices"].as_array_mut().unwrap().pop();v},
        ] {
            std::fs::write(&current,serde_json::to_vec(&changed)?)?;
            assert!(verified_controls_hash(&current,&expected,Some(&reference)).is_err());
        }
        std::fs::write(&reference,serde_json::to_vec(&reordered)?)?;
        std::fs::write(&current,serde_json::to_vec(&reordered)?)?;
        assert!(verified_controls_hash(&current,&expected,Some(&reference)).is_err());
        std::fs::remove_file(current)?;std::fs::remove_file(reference)?;std::fs::remove_dir(folder)?;
        Ok(())
    }
}
