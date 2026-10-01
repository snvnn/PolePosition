//! Read-only WinMM observation of vJoy HID axes without acquiring the feeder handle.
use serde::Serialize;
#[repr(C)]
struct Caps {
    mid: u16,
    pid: u16,
    name: [u16; 32],
    xmin: u32,
    xmax: u32,
    ymin: u32,
    ymax: u32,
    zmin: u32,
    zmax: u32,
    buttons: u32,
    pmin: u32,
    pmax: u32,
    rmin: u32,
    rmax: u32,
    umin: u32,
    umax: u32,
    vmin: u32,
    vmax: u32,
    flags: u32,
    max_axes: u32,
    axes: u32,
    max_buttons: u32,
    reg: [u16; 32],
    oem: [u16; 260],
}
#[repr(C)]
struct Pos {
    size: u32,
    flags: u32,
    x: u32,
    y: u32,
    z: u32,
    r: u32,
    u: u32,
    v: u32,
    buttons: u32,
    number: u32,
    pov: u32,
    reserved1: u32,
    reserved2: u32,
}
#[link(name = "winmm")]
unsafe extern "system" {
    fn joyGetNumDevs() -> u32;
    fn joyGetDevCapsW(id: usize, caps: *mut Caps, size: u32) -> u32;
    fn joyGetPosEx(id: u32, position: *mut Pos) -> u32;
}
#[derive(Debug, Serialize)]
pub struct WindowsPosition {
    pub joystick_index: u32,
    pub name: String,
    pub vendor: u16,
    pub product: u16,
    pub xyz: [u32; 3],
    pub min: [u32; 3],
    pub max: [u32; 3],
}
pub fn positions() -> Vec<WindowsPosition> {
    let mut output = Vec::new();
    unsafe {
        for id in 0..joyGetNumDevs() {
            let mut caps: Caps = std::mem::zeroed();
            if joyGetDevCapsW(id as usize, &mut caps, std::mem::size_of::<Caps>() as u32) != 0
                || caps.mid != 0x1234
                || caps.pid != 0xbead
            {
                continue;
            }
            let mut pos: Pos = std::mem::zeroed();
            pos.size = std::mem::size_of::<Pos>() as u32;
            pos.flags = 0xff;
            if joyGetPosEx(id, &mut pos) != 0 {
                continue;
            }
            let end = caps
                .name
                .iter()
                .position(|&v| v == 0)
                .unwrap_or(caps.name.len());
            output.push(WindowsPosition {
                joystick_index: id,
                name: String::from_utf16_lossy(&caps.name[..end]),
                vendor: caps.mid,
                product: caps.pid,
                xyz: [pos.x, pos.y, pos.z],
                min: [caps.xmin, caps.ymin, caps.zmin],
                max: [caps.xmax, caps.ymax, caps.zmax],
            });
        }
    }
    output
}
