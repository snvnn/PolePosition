//! Deliberately loads the pinned x64 SDK by absolute path, never a PATH lookup.
use crate::*;
use std::{
    ffi::{CString, c_void},
    os::windows::ffi::OsStrExt,
    path::PathBuf,
    sync::atomic::{AtomicBool, Ordering},
};
// The pinned SDK leaves a process-local window class after unload. Reloading
// can display blocking RegisterClassEx dialogs. Reuse one instance per run.
static SDK_LOADED: AtomicBool = AtomicBool::new(false);
#[link(name = "kernel32")]
unsafe extern "system" {
    fn LoadLibraryExW(path: *const u16, file: *mut c_void, flags: u32) -> *mut c_void;
    fn GetProcAddress(module: *mut c_void, name: *const u8) -> *mut c_void;
    fn FreeLibrary(module: *mut c_void) -> i32;
}
type Unary = unsafe extern "C" fn(u32) -> i32;
type RangeFn = unsafe extern "C" fn(u32, u32, *mut i32) -> i32;
pub struct Native {
    library: *mut c_void,
    path: PathBuf,
    enabled: unsafe extern "C" fn() -> i32,
    version: unsafe extern "C" fn() -> i16,
    driver_match: unsafe extern "C" fn(*mut u16, *mut u16) -> i32,
    status: Unary,
    owner: Unary,
    acquire: Unary,
    relinquish: unsafe extern "C" fn(u32),
    exists: unsafe extern "C" fn(u32, u32) -> i32,
    min: RangeFn,
    max: RangeFn,
    update: unsafe extern "C" fn(u32, *mut PositionV3) -> i32,
    position: unsafe extern "C" fn(u32, *mut PositionV3) -> u32,
    mapping: Option<Mapping>,
}
impl Native {
    pub fn load(path: &Path) -> Result<Self> {
        if std::mem::size_of::<usize>() != 8 {
            return Err("x64 required".into());
        }
        let path = path.canonicalize()?;
        if hash(&path)? != SDK_SHA256 {
            return Err("Unrecognized SDK DLL hash; expected official 2.2.2.0 x64".into());
        }
        if SDK_LOADED
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
        {
            return Err("vJoy SDK may only be loaded once per process; reuse the existing Native instance or start a new process".into());
        }
        let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
        let library = unsafe { LoadLibraryExW(wide.as_ptr(), std::ptr::null_mut(), 0x100 | 0x800) };
        if library.is_null() {
            SDK_LOADED.store(false, Ordering::SeqCst);
            return Err(std::io::Error::last_os_error().into());
        }
        macro_rules! symbol {
            ($name:literal,$typ:ty) => {{
                let name = CString::new($name).unwrap();
                let ptr = unsafe { GetProcAddress(library, name.as_ptr().cast()) };
                if ptr.is_null() {
                    unsafe { FreeLibrary(library) };
                    return Err(concat!("Missing vJoy export ", $name).into());
                }
                unsafe { std::mem::transmute::<*mut c_void, $typ>(ptr) }
            }};
        }
        Ok(Self {
            library,
            path,
            enabled: symbol!("vJoyEnabled", unsafe extern "C" fn() -> i32),
            version: symbol!("GetvJoyVersion", unsafe extern "C" fn() -> i16),
            driver_match: symbol!(
                "DriverMatch",
                unsafe extern "C" fn(*mut u16, *mut u16) -> i32
            ),
            status: symbol!("GetVJDStatus", Unary),
            owner: symbol!("GetOwnerPid", Unary),
            acquire: symbol!("AcquireVJD", Unary),
            relinquish: symbol!("RelinquishVJD", unsafe extern "C" fn(u32)),
            exists: symbol!("GetVJDAxisExist", unsafe extern "C" fn(u32, u32) -> i32),
            min: symbol!("GetVJDAxisMin", RangeFn),
            max: symbol!("GetVJDAxisMax", RangeFn),
            update: symbol!(
                "UpdateVJD",
                unsafe extern "C" fn(u32, *mut PositionV3) -> i32
            ),
            position: symbol!(
                "GetPosition",
                unsafe extern "C" fn(u32, *mut PositionV3) -> u32
            ),
            mapping: None,
        })
    }
    pub fn probe(&self) -> Result<Probe> {
        let enabled = unsafe { (self.enabled)() != 0 };
        let (mut dll, mut driver) = (0, 0);
        let matched = unsafe { (self.driver_match)(&mut dll, &mut driver) != 0 };
        let api = unsafe { (self.version)() as u16 };
        if !enabled || !matched || dll != VERSION || driver != VERSION || api != VERSION {
            return Err(format!("vJoy unavailable/version mismatch: enabled={enabled} dll={dll:#x} driver={driver:#x} api={api:#x}").into());
        }
        let range = |usage| -> Result<AxisRange> {
            let (mut min, mut max) = (0, 0);
            if unsafe { (self.exists)(1, usage) } == 0
                || unsafe { (self.min)(1, usage, &mut min) } == 0
                || unsafe { (self.max)(1, usage, &mut max) } == 0
            {
                return Err(format!("Missing/query failed axis {usage:#x}").into());
            }
            let a = AxisRange {
                min,
                max,
                center: min + (max - min + 1) / 2,
                inverted: false,
            };
            a.validate()?;
            Ok(a)
        };
        let mut pos = PositionV3 {
            device: 1,
            ..Default::default()
        };
        let read_status = unsafe { (self.position)(1, &mut pos) };
        Ok(Probe {
            dll_path: self.path.to_string_lossy().into(),
            dll_sha256: hash(&self.path)?,
            dll_api_version: dll,
            driver_api_version: driver,
            enabled,
            status: unsafe { (self.status)(1) },
            owner_pid: unsafe { (self.owner)(1) },
            mapping: Mapping {
                device: 1,
                steering: range(0x30)?,
                throttle: range(0x31)?,
                brake: range(0x32)?,
            },
            initial_axes: (read_status == 0).then_some([pos.x, pos.y, pos.z]),
            position_read_status: read_status,
        })
    }
    pub fn acquire(&mut self, mapping: Mapping) -> Result<()> {
        mapping.validate()?;
        let p = self.probe()?;
        validate_probe(&p, true)?;
        for (actual, given) in [
            (p.mapping.steering, mapping.steering),
            (p.mapping.throttle, mapping.throttle),
            (p.mapping.brake, mapping.brake),
        ] {
            if actual.min != given.min || actual.max != given.max {
                return Err("Calibrated axis range changed".into());
            }
        }
        if unsafe { (self.acquire)(mapping.device) } == 0 {
            return Err("AcquireVJD failed".into());
        }
        self.mapping = Some(mapping);
        Ok(())
    }
}
impl Device for Native {
    fn send(&mut self, axes: Axes) -> Result<[i32; 3]> {
        let m = self.mapping.as_ref().ok_or("Device not acquired")?;
        if unsafe { (self.status)(m.device) } != 0 {
            return Err("vJoy ownership lost".into());
        }
        let mut report = m.report(axes)?;
        if unsafe { (self.update)(m.device, &mut report) } == 0 {
            return Err("UpdateVJD failed; input state unconfirmed".into());
        }
        Ok([report.x, report.y, report.z])
    }
    fn release(&mut self) -> Result<()> {
        self.send(Axes::NEUTRAL)?;
        if let Some(m) = self.mapping.take() {
            unsafe { (self.relinquish)(m.device) };
        }
        Ok(())
    }
}
impl Drop for Native {
    fn drop(&mut self) {
        // Never silently neutralize an emergency brake on an error/panic path.
        // Process/driver failure can still relinquish/retain/reset axes: not a brake guarantee.
        unsafe { FreeLibrary(self.library) };
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[ignore = "requires the installed vJoy 2.2.2.0 driver and pinned SDK; read-only"]
    fn duplicate_load_is_rejected_before_and_after_drop() -> Result<()> {
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join(DEFAULT_DLL);
        let sdk = Native::load(&path)?;
        sdk.probe()?;
        assert!(Native::load(&path).is_err());
        drop(sdk);
        assert!(Native::load(&path).is_err());
        Ok(())
    }
}
