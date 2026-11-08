use super::*;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Config {
    #[serde(default)]
    pub(super) allow_verified_relocation_recovery: bool,
    #[serde(default)]
    pub(super) allow_verified_timing_gap_recovery: bool,
    #[serde(default)]
    pub(super) policy_comparison: bool,
    #[serde(default)]
    pub(super) automatic_initial_start: bool,
    #[serde(default)]
    pub(super) autonomous_isolation: Option<PathBuf>,
    #[serde(default)]
    pub(super) continuous_learning: bool,
    #[serde(default)]
    pub(super) evaluation_laps: u32,
    pub(super) profile: PathBuf,
    pub(super) prepared: PathBuf,
    pub(super) model: PathBuf,
    pub(super) guardian_config: PathBuf,
    pub(super) controller_config: PathBuf,
    pub(super) output_dir: PathBuf,
    pub(super) seconds: u32,
    #[serde(default)]
    pub(super) disable_path_departure_stop: bool,
    #[serde(default)]
    pub(super) steering_smoothing_ms: f64,
    pub(super) previous_run: Option<PathBuf>,
    pub(super) intervention: Option<InterventionConfig>,
}
#[derive(Deserialize,serde::Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct InterventionConfig { pub(super) teacher_slot:u32, pub(super) isolation_config:PathBuf }
pub(super) fn inventory(folder: &Path, m: &Value) -> Result<()> {
    for (name, expected) in m["output_sha256"]
        .as_object()
        .ok_or("Missing model inventory")?
    {
        if name.contains(['/', '\\']) || expected.as_str() != Some(&hash(&folder.join(name))?) {
            return Err("Model inventory mismatch".into());
        }
    }
    Ok(())
}
