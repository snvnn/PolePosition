//! vJoy 2.2.2.0 V3 ABI. Values are virtual axes, never ACC raw steering angles.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{io::Read, path::Path};
pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;
pub const SDK_SHA256: &str = "199316aaefac95118e869c07e7f332ce492dd9a8b6dac5a3ca20b1d5bb5058a1";
pub const DEFAULT_DLL: &str = "target/vjoy-2.2.2.0/SDK/SDK/lib/x64/vJoyInterface.dll";
pub const VERSION: u16 = 0x222;

pub fn hash(path: &Path) -> Result<String> {
    let mut f = std::fs::File::open(path)?;
    let mut h = Sha256::new();
    let mut buf = [0u8; 65536];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(format!("{:x}", h.finalize()))
}

/// Mirrors SDK/inc/public.h, USE_JOYSTICK_API_VERSION=3, Windows LONG=32 bits.
#[repr(C)]
#[derive(Debug, Default, Clone, Copy)]
pub struct PositionV3 {
    pub device: u8,
    pub throttle: i32,
    pub rudder: i32,
    pub aileron: i32,
    pub x: i32,
    pub y: i32,
    pub z: i32,
    pub rx: i32,
    pub ry: i32,
    pub rz: i32,
    pub slider: i32,
    pub dial: i32,
    pub wheel: i32,
    pub accelerator: i32,
    pub brake: i32,
    pub clutch: i32,
    pub steering: i32,
    pub vx: i32,
    pub vy: i32,
    pub buttons: i32,
    pub hats: u32,
    pub hats1: u32,
    pub hats2: u32,
    pub hats3: u32,
    pub buttons1: i32,
    pub buttons2: i32,
    pub buttons3: i32,
    pub vz: i32,
    pub vbrx: i32,
    pub vbry: i32,
    pub vbrz: i32,
}
#[derive(Debug, Default, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Axes {
    pub steering: f64,
    pub throttle: f64,
    pub brake: f64,
}
impl Axes {
    pub const NEUTRAL: Self = Self {
        steering: 0.,
        throttle: 0.,
        brake: 0.,
    };
    pub const STOP: Self = Self {
        steering: 0.,
        throttle: 0.,
        brake: 1.,
    };
    pub fn validate(self) -> Result<Self> {
        if !self.steering.is_finite()
            || !(-1.0..=1.0).contains(&self.steering)
            || !self.throttle.is_finite()
            || !(0.0..=1.0).contains(&self.throttle)
            || !self.brake.is_finite()
            || !(0.0..=1.0).contains(&self.brake)
        {
            return Err("Non-finite/out-of-range axis".into());
        }
        Ok(self)
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AxisRange {
    pub min: i32,
    pub max: i32,
    pub center: i32,
    pub inverted: bool,
}
impl AxisRange {
    pub fn validate(&self) -> Result<()> {
        if self.min < 0 || self.min >= self.center || self.center >= self.max || self.max > 65535 {
            return Err("Invalid axis range/center".into());
        }
        Ok(())
    }
    pub fn map(&self, value: f64, steering: bool) -> Result<i32> {
        self.validate()?;
        let range = if steering { -1.0..=1.0 } else { 0.0..=1.0 };
        if !value.is_finite() || !range.contains(&value) {
            return Err("Invalid normalized axis".into());
        }
        let v = if self.inverted {
            if steering { -value } else { 1.0 - value }
        } else {
            value
        };
        let mapped = if steering {
            self.center as f64
                + v * (if v < 0.0 {
                    self.center - self.min
                } else {
                    self.max - self.center
                }) as f64
        } else {
            self.min as f64 + v * (self.max - self.min) as f64
        };
        Ok(mapped.round() as i32)
    }
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Mapping {
    pub device: u32,
    pub steering: AxisRange,
    pub throttle: AxisRange,
    pub brake: AxisRange,
}
impl Mapping {
    pub fn validate(&self) -> Result<()> {
        if self.device != 1 {
            return Err("This version requires vJoy device 1".into());
        }
        self.steering.validate()?;
        self.throttle.validate()?;
        self.brake.validate()
    }
    pub fn report(&self, axes: Axes) -> Result<PositionV3> {
        self.validate()?;
        axes.validate()?;
        Ok(PositionV3 {
            device: self.device as u8,
            x: self.steering.map(axes.steering, true)?,
            y: self.throttle.map(axes.throttle, false)?,
            z: self.brake.map(axes.brake, false)?,
            hats: u32::MAX,
            hats1: u32::MAX,
            hats2: u32::MAX,
            hats3: u32::MAX,
            ..Default::default()
        })
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Probe {
    pub dll_path: String,
    pub dll_sha256: String,
    pub dll_api_version: u16,
    pub driver_api_version: u16,
    pub enabled: bool,
    pub status: i32,
    pub owner_pid: i32,
    pub mapping: Mapping,
    pub initial_axes: Option<[i32; 3]>,
    pub position_read_status: u32,
}
pub fn validate_probe(p: &Probe, require_free: bool) -> Result<()> {
    if !p.enabled
        || p.dll_api_version != VERSION
        || p.driver_api_version != VERSION
        || p.dll_sha256 != SDK_SHA256
    {
        return Err("Missing vJoy or driver/SDK version mismatch (expected 2.2.2.0)".into());
    }
    if !matches!(p.status, 0 | 1) || (require_free && p.status != 1) {
        return Err("vJoy missing, busy or not free".into());
    }
    p.mapping.validate()
}
pub trait Device {
    fn kind(&self) -> &'static str {"vjoy_wheel"}
    /// None means this backend has no independent connection query.
    fn connected(&self) -> Option<bool> {None}
    fn send(&mut self, axes: Axes) -> Result<[i32; 3]>;
    fn neutralize(&mut self) -> Result<()> {self.send(Axes::NEUTRAL)?;Ok(())}
    /// Caller must have verified stop/game pause before neutralizing/releasing.
    fn release(&mut self) -> Result<()>;
}
#[derive(Default)]
pub struct MockDevice {
    pub sent: Vec<Axes>,
    pub released: bool,
    pub fail_send: bool,
}
impl Device for MockDevice {
    fn kind(&self)->&'static str {"mock"}
    fn connected(&self)->Option<bool> {Some(!self.released&&!self.fail_send)}
    fn send(&mut self, axes: Axes) -> Result<[i32; 3]> {
        if self.fail_send || self.released {
            return Err("Mock device unavailable".into());
        }
        axes.validate()?;
        self.sent.push(axes);
        Ok([
            (axes.steering * 1000.) as i32,
            (axes.throttle * 1000.) as i32,
            (axes.brake * 1000.) as i32,
        ])
    }
    fn release(&mut self) -> Result<()> {
        self.send(Axes::NEUTRAL)?;
        self.released = true;
        Ok(())
    }
}
#[cfg(windows)]
pub mod native;
#[cfg(windows)]
pub mod windows_read;

#[cfg(test)]
mod tests {
    use super::*;
    use std::mem::{align_of, offset_of, size_of};
    #[test]
    fn v3_abi() {
        assert_eq!(size_of::<PositionV3>(), 124);
        assert_eq!(align_of::<PositionV3>(), 4);
        assert_eq!(offset_of!(PositionV3, x), 16);
        assert_eq!(offset_of!(PositionV3, y), 20);
        assert_eq!(offset_of!(PositionV3, z), 24);
        assert_eq!(offset_of!(PositionV3, accelerator), 52);
        assert_eq!(offset_of!(PositionV3, buttons), 76);
        assert_eq!(offset_of!(PositionV3, hats), 80);
        assert_eq!(offset_of!(PositionV3, buttons1), 96);
        assert_eq!(offset_of!(PositionV3, vz), 108);
        assert_eq!(offset_of!(PositionV3, vbrz), 120);
    }
    #[test]
    fn axis_mapping() {
        let mut a = AxisRange {
            min: 0,
            max: 32767,
            center: 16384,
            inverted: false,
        };
        for (v, expected) in [(-1., 0), (0., 16384), (1., 32767)] {
            assert_eq!(a.map(v, true).unwrap(), expected);
        }
        assert_eq!(a.map(0., false).unwrap(), 0);
        assert_eq!(a.map(1., false).unwrap(), 32767);
        a.inverted = true;
        assert_eq!(a.map(-1., true).unwrap(), 32767);
        assert_eq!(a.map(0., false).unwrap(), 32767);
        assert!(a.map(f64::NAN, false).is_err());
        assert!(a.map(1.01, true).is_err());
    }
    #[test]
    fn mock_failure_and_release() {
        let mut d = MockDevice::default();
        d.send(Axes::STOP).unwrap();
        d.release().unwrap();
        assert_eq!(d.sent.last(), Some(&Axes::NEUTRAL));
        assert!(d.send(Axes::STOP).is_err());
    }
    #[test]
    fn device_validation() {
        let a = AxisRange {
            min: 0,
            max: 32767,
            center: 16384,
            inverted: false,
        };
        let mut p = Probe {
            dll_path: String::new(),
            dll_sha256: SDK_SHA256.into(),
            dll_api_version: VERSION,
            driver_api_version: VERSION,
            enabled: true,
            status: 1,
            owner_pid: 0,
            mapping: Mapping {
                device: 1,
                steering: a,
                throttle: a,
                brake: a,
            },
            initial_axes: None,
            position_read_status: 0,
        };
        validate_probe(&p, true).unwrap();
        p.status = 2;
        assert!(validate_probe(&p, true).is_err());
        p.status = 3;
        assert!(validate_probe(&p, true).is_err());
        p.status = 1;
        p.driver_api_version = 0x219;
        assert!(validate_probe(&p, true).is_err());
    }
}
