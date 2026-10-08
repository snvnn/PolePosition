//! XInput reads never create a virtual target. Creation requires explicit caller intent.
use crate::*;
use std::{ffi::c_void, os::windows::ffi::OsStrExt, path::Path};
#[repr(C)]
#[derive(Default)]
struct State {
    packet: u32,
    report: Report,
}
pub fn read(slot: u32) -> Option<(u32, Report)> {
    static GET: std::sync::OnceLock<usize> = std::sync::OnceLock::new();
    let address = *GET.get_or_init(|| {
        let name: Vec<u16> = "xinput1_4.dll".encode_utf16().chain(Some(0)).collect();
        let lib = unsafe { LoadLibraryExW(name.as_ptr(), std::ptr::null_mut(), 0x800) };
        if lib.is_null() {
            0
        } else {
            unsafe { GetProcAddress(lib, c"XInputGetState".as_ptr().cast()) as usize }
        }
    });
    if address == 0 || slot > 3 {
        return None;
    }
    let get: unsafe extern "system" fn(u32, *mut State) -> u32 =
        unsafe { std::mem::transmute(address) };
    let mut s = State::default();
    (unsafe { get(slot, &mut s) } == 0).then_some((s.packet, s.report))
}
pub fn slots() -> Vec<u32> {
    (0..4).filter(|s| read(*s).is_some()).collect()
}
#[repr(C)]
#[derive(Default,Debug,Clone,PartialEq,Serialize)]
pub struct Capabilities {
    pub device_type:u8, pub subtype:u8, pub flags:u16,
    pub supported_report:Report, pub left_motor:u16, pub right_motor:u16,
}
pub fn capabilities(slot:u32)->Option<Capabilities> {
    static GET:std::sync::OnceLock<usize>=std::sync::OnceLock::new();
    let address=*GET.get_or_init(||{
        let name:Vec<u16>="xinput1_4.dll".encode_utf16().chain(Some(0)).collect();
        let lib=unsafe{LoadLibraryExW(name.as_ptr(),std::ptr::null_mut(),0x800)};
        if lib.is_null(){0}else{unsafe{GetProcAddress(lib,c"XInputGetCapabilities".as_ptr().cast()) as usize}}
    });
    if address==0 || slot>3{return None;}
    let get:unsafe extern "system" fn(u32,u32,*mut Capabilities)->u32=unsafe{std::mem::transmute(address)};
    let mut caps=Capabilities::default();(unsafe{get(slot,0,&mut caps)}==0).then_some(caps)
}
#[link(name = "kernel32")]
unsafe extern "system" {
    fn LoadLibraryExW(path: *const u16, file: *mut c_void, flags: u32) -> *mut c_void;
    fn GetProcAddress(module: *mut c_void, name: *const u8) -> *mut c_void;
    fn FreeLibrary(module: *mut c_void) -> i32;
}
type Ptr = *mut c_void;
type Pair = unsafe extern "C" fn(Ptr, Ptr) -> u32;
type Free = unsafe extern "C" fn(Ptr);
pub struct VirtualPad {
    library: Ptr,
    client: Ptr,
    target: Ptr,
    update: unsafe extern "C" fn(Ptr, Ptr, Report) -> u32,
    remove: Pair,
    free_target: Free,
    disconnect: Free,
    free_client: Free,
    slot: u32,
    released: bool,
    teacher_slot: Option<u32>,
}
fn ok(code: u32) -> Result<()> {
    if code == 0x20000000 {
        Ok(())
    } else {
        Err(format!("ViGEm error 0x{code:08x}").into())
    }
}
impl VirtualPad {
    pub fn open(path: &Path, expected_hash: &str) -> Result<Self> {
        Self::open_with_teacher(path,expected_hash,None)
    }
    /// Caller must first verify HidHide isolation. Ordinary mode remains exclusive.
    pub fn open_with_teacher(path: &Path, expected_hash: &str, teacher_slot:Option<u32>) -> Result<Self> {
        if teacher_slot.is_some_and(|s|s>3) || slots()!=teacher_slot.into_iter().collect::<Vec<_>>() {
            return Err("Disconnect physical/XInput controllers before virtual-pad run".into());
        }
        if hash(path)? != expected_hash {
            return Err("ViGEmClient hash mismatch".into());
        }
        let p = path.canonicalize()?;
        let wide: Vec<u16> = p.as_os_str().encode_wide().chain(Some(0)).collect();
        let library = unsafe { LoadLibraryExW(wide.as_ptr(), std::ptr::null_mut(), 0x100 | 0x800) };
        if library.is_null() {
            return Err(std::io::Error::last_os_error().into());
        }
        // Resolve all exports before allocation so an ABI mismatch has no device side effects.
        let mut owned = false;
        let setup = (|| -> Result<Self> {
            macro_rules! sym {
                ($name:literal,$ty:ty) => {{
                    let a = unsafe { GetProcAddress(library, concat!($name, "\0").as_ptr()) };
                    if a.is_null() {
                        return Err(concat!("Missing ViGEm export: ", $name).into());
                    }
                    unsafe { std::mem::transmute::<Ptr, $ty>(a) }
                }};
            }
            let alloc = sym!("vigem_alloc", unsafe extern "C" fn() -> Ptr);
            let connect = sym!("vigem_connect", unsafe extern "C" fn(Ptr) -> u32);
            let target_alloc = sym!("vigem_target_x360_alloc", unsafe extern "C" fn() -> Ptr);
            let add = sym!("vigem_target_add", Pair);
            let index = sym!(
                "vigem_target_x360_get_user_index",
                unsafe extern "C" fn(Ptr, Ptr, *mut u32) -> u32
            );
            let mut v = Self {
                library,
                client: std::ptr::null_mut(),
                target: std::ptr::null_mut(),
                update: sym!(
                    "vigem_target_x360_update",
                    unsafe extern "C" fn(Ptr, Ptr, Report) -> u32
                ),
                remove: sym!("vigem_target_remove", Pair),
                free_target: sym!("vigem_target_free", Free),
                disconnect: sym!("vigem_disconnect", Free),
                free_client: sym!("vigem_free", Free),
                slot: 0,
                released: true,
                teacher_slot,
            };
            owned = true;
            unsafe {
                v.client = alloc();
                if v.client.is_null() {
                    return Err("vigem_alloc failed".into());
                }
                ok(connect(v.client))?;
                v.target = target_alloc();
                if v.target.is_null() {
                    return Err("target allocation failed".into());
                }
                ok(add(v.client, v.target))?;
                v.released = false;
                ok(index(v.client, v.target, &mut v.slot))?;
                if teacher_slot==Some(v.slot){return Err("Teacher and virtual slot collision".into());}
            }
            let wait = std::time::Instant::now();
            while read(v.slot).is_none() && wait.elapsed() < std::time::Duration::from_secs(3) {
                std::thread::sleep(std::time::Duration::from_millis(10));
            }
            // ViGEmBus XusbPdo.cpp seeds a nonneutral USB init blob, but its report
            // cache is zero. An initial all-zero update is ignored as unchanged.
            // Brake, then neutral, with readback; never prime using throttle/steering.
            for axes in [Axes::STOP, Axes::NEUTRAL] {
                let wait = std::time::Instant::now();
                loop {
                    let expected = v.send(axes)?;
                    if read(v.slot).is_some_and(|(_, r)| r == expected) {
                        break;
                    }
                    if wait.elapsed() > std::time::Duration::from_secs(3) {
                        return Err("Virtual target initialization readback failed".into());
                    }
                    std::thread::sleep(std::time::Duration::from_millis(10));
                }
            }
            Ok(v)
        })();
        if !owned {
            unsafe {
                FreeLibrary(library);
            }
        }
        setup
    }
    pub fn slot(&self) -> u32 {
        self.slot
    }
    pub fn send(&mut self, axes: Axes) -> Result<Report> {
        if self.released {
            return Err("Virtual pad already released".into());
        }
        let present=slots();
        if !present.contains(&self.slot) || present.iter().any(|s|*s!=self.slot && Some(*s)!=self.teacher_slot) {
            return Err("Virtual controller lost or unexpected second XInput controller".into());
        }
        let report = Report::from_axes(axes)?;
        ok(unsafe { (self.update)(self.client, self.target, report) })?;
        Ok(report)
    }
    pub fn release(&mut self) -> Result<()> {
        let neutral = self.send(Axes::NEUTRAL)?;
        let wait = std::time::Instant::now();
        while read(self.slot).is_none_or(|(_, r)| r != neutral) {
            if wait.elapsed() > std::time::Duration::from_millis(100) {
                return Err("Neutralization not acknowledged".into());
            }
            self.send(Axes::NEUTRAL)?;
            std::thread::sleep(std::time::Duration::from_millis(1));
        }
        ok(unsafe { (self.remove)(self.client, self.target) })?;
        self.released = true;
        let wait = std::time::Instant::now();
        while read(self.slot).is_some() {
            if wait.elapsed() > std::time::Duration::from_secs(2) {
                return Err("Virtual detach not confirmed".into());
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        Ok(())
    }
}
impl poleposition_vjoy::Device for VirtualPad {
    fn kind(&self) -> &'static str {
        "xbox360_gamepad"
    }
    fn connected(&self) -> Option<bool> {
        let present=slots();
        Some(!self.released && present.contains(&self.slot) && present.iter().all(|s|*s==self.slot || Some(*s)==self.teacher_slot))
    }
    fn send(&mut self, axes: Axes) -> Result<[i32; 3]> {
        let r = VirtualPad::send(self, axes)?;
        Ok([r.lx as i32, r.rt as i32, r.lt as i32])
    }
    fn release(&mut self) -> Result<()> {
        VirtualPad::release(self)
    }
}
impl Drop for VirtualPad {
    fn drop(&mut self) {
        unsafe {
            // Disconnect on exceptional exit is NOT a guarantee that the car stopped.
            if !self.target.is_null() {
                if !self.released {
                    (self.remove)(self.client, self.target);
                }
                (self.free_target)(self.target);
            }
            if !self.client.is_null() {
                (self.disconnect)(self.client);
                (self.free_client)(self.client);
            }
            FreeLibrary(self.library);
        }
    }
}
