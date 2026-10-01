#[cfg(windows)]
mod native {
    use std::{ffi::c_void, io, ptr};
    type Handle = *mut c_void;
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn OpenFileMappingW(access: u32, inherit: i32, name: *const u16) -> Handle;
        fn MapViewOfFile(h: Handle, access: u32, high: u32, low: u32, size: usize) -> *mut c_void;
        fn UnmapViewOfFile(p: *const c_void) -> i32;
        fn CloseHandle(h: Handle) -> i32;
    }
    pub struct Mapping {
        handle: Handle,
        view: *mut c_void,
        size: usize,
    }
    impl Mapping {
        pub fn open(name: &str, size: usize) -> io::Result<Self> {
            let wide: Vec<u16> = name.encode_utf16().chain(Some(0)).collect();
            // SAFETY: valid terminated UTF-16, correct Win32 signature, open-only read access.
            let handle = unsafe { OpenFileMappingW(4, 0, wide.as_ptr()) };
            if handle.is_null() {
                return Err(io::Error::last_os_error());
            }
            let mut m = Self {
                handle,
                view: ptr::null_mut(),
                size,
            };
            // SAFETY: Windows validates length, handle remains owned by m.
            m.view = unsafe { MapViewOfFile(handle, 4, 0, 0, size) };
            if m.view.is_null() {
                return Err(io::Error::last_os_error());
            }
            Ok(m)
        }
        pub fn copy(&self, out: &mut [u8]) {
            assert_eq!(out.len(), self.size);
            for (i, b) in out.iter_mut().enumerate() {
                // SAFETY: mapped address is valid for size bytes; byte loads require no alignment.
                // Volatile observes external writes, but does not guarantee page atomicity.
                *b = unsafe { ptr::read_volatile(self.view.cast::<u8>().add(i)) };
            }
        }
    }
    impl Drop for Mapping {
        fn drop(&mut self) {
            // SAFETY: exclusively owned handles, released once, including failed partial opens.
            unsafe {
                if !self.view.is_null() {
                    UnmapViewOfFile(self.view);
                }
                CloseHandle(self.handle);
            }
        }
    }
    #[cfg(test)]
    mod tests {
        use super::*;
        #[link(name = "kernel32")]
        unsafe extern "system" {
            fn CreateFileMappingW(
                file: Handle,
                attrs: *const c_void,
                protect: u32,
                high: u32,
                low: u32,
                name: *const u16,
            ) -> Handle;
        }
        #[test]
        fn open_only_and_release() {
            let name = format!("Local\\PolePositionTest{}", std::process::id());
            assert!(Mapping::open(&name, 4).is_err());
            let wide: Vec<u16> = name.encode_utf16().chain(Some(0)).collect();
            // SAFETY: unique test-only mapping, never touches ACC mappings.
            let h = unsafe {
                CreateFileMappingW(-1_isize as Handle, ptr::null(), 4, 0, 4, wide.as_ptr())
            };
            assert!(!h.is_null());
            {
                let m = Mapping::open(&name, 4).unwrap();
                let mut b = [1; 4];
                m.copy(&mut b);
                assert_eq!(b, [0; 4]);
            }
            unsafe {
                CloseHandle(h);
            }
            assert!(Mapping::open(&name, 4).is_err());
        }
    }
}
#[cfg(windows)]
pub use native::Mapping;
#[cfg(not(windows))]
pub struct Mapping;
#[cfg(not(windows))]
impl Mapping {
    pub fn open(_: &str, _: usize) -> std::io::Result<Self> {
        Err(std::io::Error::other("Windows required"))
    }
    pub fn copy(&self, _: &mut [u8]) {
        unreachable!()
    }
}
