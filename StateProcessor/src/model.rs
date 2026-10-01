use crate::{Config, Identity};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    fs::File,
    io::Read,
    path::{Path, PathBuf},
};

pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;
pub const FIELDS: [&str; 9] = [
    "index",
    "s_m",
    "position_x",
    "position_y",
    "position_z",
    "tangent_x",
    "tangent_z",
    "heading_path_rad",
    "curvature_per_m",
];
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Point {
    pub index: usize,
    pub s_m: f64,
    pub position_x: f64,
    pub position_y: f64,
    pub position_z: f64,
    pub tangent_x: f64,
    pub tangent_z: f64,
    pub heading_path_rad: f64,
    pub curvature_per_m: f64,
}
#[derive(Debug, Clone)]
pub struct Model {
    pub points: Vec<Point>,
    pub length: f64,
}
impl Model {
    pub fn validate(&self) -> Result<()> {
        if self.points.len() < 3 || !self.length.is_finite() || self.length <= 0.0 {
            return Err("Invalid model size/length".into());
        }
        let mut cumulative = 0.0;
        for (i, p) in self.points.iter().enumerate() {
            let q = &self.points[(i + 1) % self.points.len()];
            let length = (q.position_x - p.position_x).hypot(q.position_z - p.position_z);
            if p.index != i
                || (i > 0 && p.s_m <= self.points[i - 1].s_m)
                || [
                    p.s_m,
                    p.position_x,
                    p.position_y,
                    p.position_z,
                    p.tangent_x,
                    p.tangent_z,
                    p.heading_path_rad,
                    p.curvature_per_m,
                ]
                .iter()
                .any(|v| !v.is_finite())
                || length <= 1e-9
                || (p.s_m - cumulative).abs() > 1e-7
                || (p.tangent_x.hypot(p.tangent_z) - 1.0).abs() > 1e-8
                || crate::wrap(p.heading_path_rad - p.tangent_z.atan2(p.tangent_x)).abs() > 1e-8
            {
                return Err(format!("Invalid reference point {i}").into());
            }
            cumulative += length;
        }
        if (cumulative - self.length).abs() > 1e-7 {
            return Err("Closed path length mismatch".into());
        }
        Ok(())
    }
    pub fn curvature(&self, s: f64) -> f64 {
        let s = s.rem_euclid(self.length);
        let i = self
            .points
            .partition_point(|p| p.s_m <= s)
            .saturating_sub(1);
        let a = &self.points[i];
        let b = &self.points[(i + 1) % self.points.len()];
        let end = if i + 1 == self.points.len() {
            self.length
        } else {
            b.s_m
        };
        a.curvature_per_m + (b.curvature_per_m - a.curvature_per_m) * (s - a.s_m) / (end - a.s_m)
    }
}
pub fn hash(path: &Path) -> Result<String> {
    let mut f = File::open(path)?;
    let mut digest = Sha256::new();
    let mut buf = [0u8; 65536];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        digest.update(&buf[..n]);
    }
    Ok(format!("{:x}", digest.finalize()))
}
pub fn json(path: &Path) -> Result<Value> {
    Ok(serde_json::from_reader(File::open(path)?)?)
}
pub fn verify_map(map: &Value, base: Option<&Path>) -> Result<()> {
    let entries = map.as_object().ok_or("Missing hash inventory")?;
    if entries.is_empty() {
        return Err("Empty hash inventory".into());
    }
    for (name, checksum) in entries {
        let path = base.map_or_else(|| PathBuf::from(name), |p| p.join(name));
        if hash(&path)? != checksum.as_str().ok_or("Invalid hash")? {
            return Err(format!("Input changed: {}", path.display()).into());
        }
    }
    Ok(())
}
pub struct Prepared {
    pub path: PathBuf,
    pub manifest: Value,
    pub config: Config,
    pub model: Model,
    pub identity: Identity,
    pub sign: f64,
    pub offset: f64,
    pub replay: Value,
}
impl Prepared {
    pub fn load(folder: &Path) -> Result<Self> {
        let path = folder.canonicalize()?;
        let manifest = json(&path.join("manifest.json"))?;
        if manifest["kind"] != "state_prepared"
            || manifest["schema_version"] != 1
            || manifest["tool_version"] != "0.1.0"
            || manifest["complete"] != true
        {
            return Err("Incomplete/unsupported preparation".into());
        }
        let expected = ["config.json", "heading_profile.json", "replay.json"];
        let keys = manifest["output_sha256"]
            .as_object()
            .ok_or("Missing prepared hashes")?;
        if keys.len() != expected.len() || expected.iter().any(|k| !keys.contains_key(*k)) {
            return Err("Prepared output inventory".into());
        }
        verify_map(&manifest["input_sha256"], None)?;
        verify_map(&manifest["output_sha256"], Some(&path))?;
        let config: Config = serde_json::from_value(json(&path.join("config.json"))?)?;
        config.validate()?;
        let profile = json(&path.join("heading_profile.json"))?;
        let replay = json(&path.join("replay.json"))?;
        let identity: Identity = serde_json::from_value(profile["identity"].clone())?;
        let sign = profile["sign"].as_f64().ok_or("Missing heading sign")?;
        let offset = profile["offset_rad"]
            .as_f64()
            .ok_or("Missing heading offset")?;
        if profile["candidates"][0]["sign"] != profile["sign"]
            || profile["candidates"][0]["offset_rad"] != profile["offset_rad"]
        {
            return Err("Heading transform disagrees with selected candidate".into());
        }
        if profile["complete"] != true
            || profile["schema_version"] != 1
            || ![-1.0, 1.0].contains(&sign)
            || ![
                0.0,
                std::f64::consts::FRAC_PI_2,
                std::f64::consts::PI,
                -std::f64::consts::FRAC_PI_2,
            ]
            .contains(&offset)
            || profile["identity"] != replay["identity"]
            || profile["eligible_train"] != replay["eligible"]["train"]
            || profile["windows"].as_u64().unwrap_or(0) < config.heading_min_windows as u64
            || profile["candidates"][0]["median_abs_deg"]
                .as_f64()
                .ok_or("Missing calibration median")?
                > config.heading_max_median_deg
            || profile["candidates"][0]["p95_abs_deg"]
                .as_f64()
                .ok_or("Missing calibration p95")?
                > config.heading_max_p95_deg
            || profile["runner_gap_deg"]
                .as_f64()
                .ok_or("Missing runner gap")?
                < config.heading_min_runner_gap_deg
        {
            return Err("Invalid heading profile".into());
        }
        let track = Path::new(
            manifest["track_model_path"]
                .as_str()
                .ok_or("Missing track path")?,
        );
        let meta = json(&track.join("manifest.json"))?;
        let inventory = manifest["input_sha256"]
            .as_object()
            .ok_or("Missing input inventory")?;
        let source = Path::new(
            replay["source_path"]
                .as_str()
                .ok_or("Missing replay source")?,
        );
        for (folder, names) in [
            (
                track,
                vec![
                    "manifest.json",
                    "reference.csv",
                    "validation.json",
                    "report.html",
                ],
            ),
            (
                source,
                vec![
                    "metadata.json",
                    "telemetry.csv",
                    "events.csv",
                    "summary.json",
                ],
            ),
        ] {
            for name in names {
                let required = folder.join(name);
                if !inventory.contains_key(required.to_str().ok_or("Invalid input path")?) {
                    return Err(format!(
                        "Required input absent from hash inventory: {}",
                        required.display()
                    )
                    .into());
                }
            }
        }
        if meta["review_path"] != manifest["review_path"] {
            return Err("Review path mismatch".into());
        }
        if meta["complete"] != true
            || meta["schema_version"] != 1
            || meta["kind"] != "track_model"
            || meta["tool_version"] != "0.1.0"
            || meta["identity"] != profile["identity"]
        {
            return Err("Unsupported or mismatched TrackModel".into());
        }
        verify_map(&meta["output_sha256"], Some(track))?;
        let conventions = serde_json::json!({"position":"ACC raw world XYZ; X-Z planar distance; Y retained",
            "origin":"s=0 corresponds to the smoothed first observed point of the selected lap", "direction":"original sample order",
            "closed":true,"duplicate_endpoint":false,"heading":"atan2(tangent_z, tangent_x), [-pi, pi]",
            "curvature":"positive for rotation from +X toward +Z; not calibrated to ACC heading or steering"});
        let units = serde_json::json!({"index":"integer","s_m":"m","position_x":"m","position_y":"m","position_z":"m",
            "tangent_x":"unitless","tangent_z":"unitless","heading_path_rad":"rad","curvature_per_m":"1/m"});
        if meta["coordinates"] != conventions
            || meta["units"] != units
            || meta["fields"] != serde_json::json!(FIELDS)
        {
            return Err("Unsupported coordinates/units/fields".into());
        }
        let mut reader = csv::Reader::from_path(track.join("reference.csv"))?;
        if reader.headers()?.iter().ne(FIELDS) {
            return Err("Reference CSV header mismatch".into());
        }
        let model = Model {
            points: reader
                .deserialize()
                .collect::<std::result::Result<Vec<Point>, _>>()?,
            length: meta["total_length_m"].as_f64().ok_or("Missing length")?,
        };
        model.validate()?;
        if meta["points"].as_u64() != Some(model.points.len() as u64) {
            return Err("Point count mismatch".into());
        }
        let result = Self {
            path,
            manifest,
            config,
            model,
            identity,
            sign,
            offset,
            replay,
        };
        result.verify()?;
        Ok(result)
    }
    pub fn verify(&self) -> Result<()> {
        if json(&self.path.join("manifest.json"))? != self.manifest {
            return Err("Prepared manifest changed".into());
        }
        verify_map(&self.manifest["input_sha256"], None)?;
        verify_map(&self.manifest["output_sha256"], Some(&self.path))
    }
}
