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
                return Err(format!("Settings changed: {}", p.display()).into());
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
            || v["controls_sha256"] != hash(&self.controls)?
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
            if h.as_str() != Some(&hash(Path::new(p))?) {
                return Err("Changed device evidence".into());
            }
        }
        Ok(())
    }
}
