//! Windows monotonic cross-process clock, global hotkeys, foreground/process checks.
use crate::Result;
use std::{
    ffi::c_void,
    mem::size_of,
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    thread,
};
#[repr(C)]
#[derive(Default)]
struct Msg {
    hwnd: *mut c_void,
    message: u32,
    wparam: usize,
    lparam: isize,
    time: u32,
    x: i32,
    y: i32,
    private: u32,
}
#[repr(C)]
struct ProcessEntry {
    size: u32,
    usage: u32,
    pid: u32,
    heap: usize,
    module: u32,
    threads: u32,
    parent: u32,
    priority: i32,
    flags: u32,
    exe: [u16; 260],
}
#[link(name = "kernel32")]
unsafe extern "system" {
    fn QueryPerformanceCounter(v: *mut i64) -> i32;
    fn QueryPerformanceFrequency(v: *mut i64) -> i32;
    fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut c_void;
    fn QueryFullProcessImageNameW(
        handle: *mut c_void,
        flags: u32,
        path: *mut u16,
        size: *mut u32,
    ) -> i32;
    fn CloseHandle(handle: *mut c_void) -> i32;
    fn CreateToolhelp32Snapshot(flags: u32, pid: u32) -> *mut c_void;
    fn Process32FirstW(handle: *mut c_void, entry: *mut ProcessEntry) -> i32;
    fn Process32NextW(handle: *mut c_void, entry: *mut ProcessEntry) -> i32;
}
#[link(name = "user32")]
unsafe extern "system" {
    fn GetForegroundWindow() -> *mut c_void;
    fn GetWindowThreadProcessId(hwnd: *mut c_void, pid: *mut u32) -> u32;
    fn RegisterHotKey(hwnd: *mut c_void, id: i32, modifiers: u32, key: u32) -> i32;
    fn UnregisterHotKey(hwnd: *mut c_void, id: i32) -> i32;
    fn PeekMessageW(msg: *mut Msg, hwnd: *mut c_void, min: u32, max: u32, remove: u32) -> i32;
}
pub fn now_ns() -> u64 {
    let (mut count, mut frequency) = (0i64, 0i64);
    unsafe {
        QueryPerformanceCounter(&mut count);
        QueryPerformanceFrequency(&mut frequency);
    }
    assert!(
        frequency > 0 && count >= 0,
        "Windows monotonic clock failure"
    );
    (count as u128 * 1_000_000_000 / frequency as u128) as u64
}
fn acc_name(name: &str) -> bool {
    name.eq_ignore_ascii_case("AC2-Win64-Shipping.exe")
}
pub fn acc_foreground() -> bool {
    unsafe {
        let hwnd = GetForegroundWindow();
        let mut pid = 0;
        GetWindowThreadProcessId(hwnd, &mut pid);
        let handle = OpenProcess(0x1000, 0, pid);
        if handle.is_null() {
            return false;
        }
        let mut buf = [0u16; 32768];
        let mut size = buf.len() as u32;
        let ok = QueryFullProcessImageNameW(handle, 0, buf.as_mut_ptr(), &mut size) != 0;
        CloseHandle(handle);
        ok && Path::new(&String::from_utf16_lossy(&buf[..size as usize]))
            .file_name()
            .is_some_and(|n| acc_name(&n.to_string_lossy()))
    }
}
/// None on enumeration failure: never interpret a failed query as game exit.
pub fn acc_running() -> Option<bool> {
    unsafe {
        let handle = CreateToolhelp32Snapshot(2, 0);
        if handle as isize == -1 {
            return None;
        }
        let mut entry: ProcessEntry = std::mem::zeroed();
        entry.size = size_of::<ProcessEntry>() as u32;
        if Process32FirstW(handle, &mut entry) == 0 {
            CloseHandle(handle);
            return None;
        }
        let mut found = false;
        loop {
            let end = entry
                .exe
                .iter()
                .position(|v| *v == 0)
                .unwrap_or(entry.exe.len());
            if acc_name(&String::from_utf16_lossy(&entry.exe[..end])) {
                found = true;
                break;
            }
            if Process32NextW(handle, &mut entry) == 0 {
                break;
            }
        }
        CloseHandle(handle);
        Some(found)
    }
}
pub struct Hotkeys {
    pub start: Arc<AtomicBool>,
    pub stop: Arc<AtomicBool>,
    pub stop_observed_ns: Arc<AtomicU64>,
    shutdown: Arc<AtomicBool>,
    worker: Option<thread::JoinHandle<()>>,
}
impl Hotkeys {
    pub fn register() -> Result<Self> {
        let start = Arc::new(AtomicBool::new(false));
        let stop = Arc::new(AtomicBool::new(false));
        let stop_observed_ns = Arc::new(AtomicU64::new(0));
        let stop_clock = stop_observed_ns.clone();
        let shutdown = Arc::new(AtomicBool::new(false));
        let (a, b, end) = (start.clone(), stop.clone(), shutdown.clone());
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        let worker = thread::spawn(move || unsafe {
            let hwnd = std::ptr::null_mut();
            if RegisterHotKey(hwnd, 7, 0x4000, 0x76) == 0 {
                let _ = tx.send(false);
                return;
            }
            if RegisterHotKey(hwnd, 8, 0x4000, 0x77) == 0 {
                UnregisterHotKey(hwnd, 7);
                let _ = tx.send(false);
                return;
            }
            let _ = tx.send(true);
            while !end.load(Ordering::Acquire) {
                let mut msg = Msg::default();
                while PeekMessageW(&mut msg, hwnd, 0, 0, 1) != 0 {
                    if msg.message == 0x312 {
                        if msg.wparam == 7 {
                            a.store(true, Ordering::Release);
                        } else if msg.wparam == 8 {
                            let _ = stop_clock.compare_exchange(
                                0,
                                now_ns(),
                                Ordering::AcqRel,
                                Ordering::Relaxed,
                            );
                            b.store(true, Ordering::Release);
                        }
                    }
                }
                thread::sleep(std::time::Duration::from_millis(2));
            }
            UnregisterHotKey(hwnd, 7);
            UnregisterHotKey(hwnd, 8);
        });
        if !rx.recv()? {
            worker.join().map_err(|_| "Hotkey thread failed")?;
            return Err("F7/F8 global hotkey registration failed; output disabled".into());
        }
        Ok(Self {
            start,
            stop,
            stop_observed_ns,
            shutdown,
            worker: Some(worker),
        })
    }
}
impl Drop for Hotkeys {
    fn drop(&mut self) {
        self.shutdown.store(true, Ordering::Release);
        if let Some(w) = self.worker.take() {
            let _ = w.join();
        }
    }
}
