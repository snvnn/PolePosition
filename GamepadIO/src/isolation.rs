//! Isolation checks run off the control thread; stale/failed checks latch a stop.
use crate::Result;
use std::{path::Path,process::{Command,Stdio},sync::{Arc,Mutex,atomic::{AtomicBool,Ordering}},time::{Instant,Duration}};
use std::os::windows::process::CommandExt;
pub fn check(path:&Path,slot:Option<u32>,preflight:bool)->Result<()> {
    let mut c=Command::new(".venv-controller/Scripts/python.exe");
    c.args(["-m","GamepadIO.isolation","--config"]).arg(path);
    if let Some(s)=slot {c.args(["--virtual-slot",&s.to_string()]);}
    if preflight {c.arg("--preflight");}
    let mut child=c.stdout(Stdio::null()).stderr(Stdio::piped()).creation_flags(0x08000000).spawn()?;
    let start=Instant::now();let timeout=if preflight {10.} else {3.};
    loop {
        if let Some(status)=child.try_wait()? {
            if status.success(){return Ok(());}
            let output=child.wait_with_output()?;
            return Err(format!("Input isolation failed: {}",String::from_utf8_lossy(&output.stderr)).into());
        }
        if start.elapsed().as_secs_f64()>timeout {child.kill()?;child.wait()?;return Err("Input isolation verification timed out".into());}
        std::thread::sleep(Duration::from_millis(20));
    }
}
pub struct Watch { state:Arc<Mutex<(Instant,bool)>>, stop:Arc<AtomicBool>, worker:Option<std::thread::JoinHandle<()>> }
impl Watch {
    pub fn start(path:&Path,slot:u32)->Result<Self> {
        check(path,Some(slot),false)?;
        let path=path.to_path_buf();let expected=crate::hash(&path)?;
        let state=Arc::new(Mutex::new((Instant::now(),true)));let stop=Arc::new(AtomicBool::new(false));
        let s=state.clone();let end=stop.clone();
        let worker=std::thread::spawn(move || {
            while !end.load(Ordering::Acquire) {
                for _ in 0..10 {if end.load(Ordering::Acquire){return;} std::thread::sleep(Duration::from_millis(100));}
                let ok=crate::hash(&path).is_ok_and(|h|h==expected) && check(&path,Some(slot),false).is_ok();
                *s.lock().unwrap()=(Instant::now(),ok);
                if !ok {break;}
            }
        });
        Ok(Self{state,stop,worker:Some(worker)})
    }
    pub fn healthy(&self)->bool {let s=self.state.lock().unwrap();s.1 && s.0.elapsed()<Duration::from_secs(4)}
}
impl Drop for Watch {fn drop(&mut self){self.stop.store(true,Ordering::Release);if let Some(w)=self.worker.take(){let _=w.join();}}}
