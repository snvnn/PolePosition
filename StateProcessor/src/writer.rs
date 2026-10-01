//! Bounded asynchronous diagnostics writer. Replay uses backpressure; live counts drops.
use crate::{model::Result, processor::State};
use serde::Serialize;
use std::{
    fs::{File, OpenOptions},
    io::{BufWriter, Write},
    path::{Path, PathBuf},
    sync::{Arc, Mutex, mpsc},
    thread,
    time::{Duration, Instant},
};

#[derive(Debug, Clone, Serialize)]
pub struct Event {
    pub now_ns: u64,
    pub kind: String,
    pub detail: String,
}
pub enum Message {
    State(Box<State>),
    Event(Event),
}
pub fn write_json(path: &Path, value: &impl Serialize) -> Result<()> {
    let mut f = OpenOptions::new().write(true).create_new(true).open(path)?;
    serde_json::to_writer_pretty(&mut f, value)?;
    f.write_all(b"\n")?;
    f.sync_all()?;
    Ok(())
}

pub fn finish_manifest(path: &Path, value: &impl Serialize) -> Result<()> {
    let next = path.with_extension("next.json");
    write_json(&next, value)?;
    std::fs::rename(next, path)?;
    Ok(())
}
pub fn create_output(parent: &Path, prefix: &str, id: Option<&str>) -> Result<PathBuf> {
    std::fs::create_dir_all(parent)?;
    let name = id.map(str::to_owned).unwrap_or_else(|| {
        format!(
            "{prefix}-{}-{}",
            poleposition_telemetry::unix_ns(),
            std::process::id()
        )
    });
    if name.is_empty()
        || !name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    {
        return Err("Unsafe output name".into());
    }
    let path = parent.join(name);
    std::fs::create_dir(&path)?;
    Ok(path.canonicalize()?)
}
pub struct Sink {
    tx: mpsc::SyncSender<Message>,
    worker: thread::JoinHandle<Result<(u64, u64)>>,
    pub dropped_states: u64,
    pub dropped_events: u64,
    pub error: Arc<Mutex<Option<String>>>,
    pub latest: Arc<Mutex<String>>,
}
fn flush(
    states: &mut csv::Writer<BufWriter<File>>,
    events: &mut csv::Writer<BufWriter<File>>,
) -> Result<()> {
    states.flush()?;
    events.flush()?;
    Ok(())
}
impl Sink {
    pub fn start(folder: &Path, capacity: usize, display_hz: Option<f64>) -> Result<Self> {
        let states = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(folder.join("states.csv"))?;
        let events = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(folder.join("events.csv"))?;
        let (tx, rx) = mpsc::sync_channel(capacity);
        let error = Arc::new(Mutex::new(None));
        let err = error.clone();
        let latest = Arc::new(Mutex::new(String::from("Waiting for ACC")));
        let view = latest.clone();
        let worker = thread::spawn(move || {
            let run = || -> Result<(u64, u64)> {
                let mut states = csv::Writer::from_writer(BufWriter::new(states));
                let mut events = csv::Writer::from_writer(BufWriter::new(events));
                // Always create headers, including disconnected/no-sample runs.
                let fields = super::runner::state_fields();
                states.write_record(fields)?;
                events.write_record(["now_ns", "kind", "detail"])?;
                let mut states = csv::WriterBuilder::new()
                    .has_headers(false)
                    .from_writer(states.into_inner()?);
                let mut events = csv::WriterBuilder::new()
                    .has_headers(false)
                    .from_writer(events.into_inner()?);
                let mut n = 0;
                let mut e = 0;
                let mut last_flush = Instant::now();
                let mut last_display = Instant::now();
                loop {
                    match rx.recv_timeout(Duration::from_millis(100)) {
                        Ok(Message::State(row)) => {
                            states.serialize(*row)?;
                            n += 1;
                        }
                        Ok(Message::Event(event)) => {
                            events.serialize(event)?;
                            e += 1;
                        }
                        Err(mpsc::RecvTimeoutError::Disconnected) => break,
                        Err(mpsc::RecvTimeoutError::Timeout) => {}
                    }
                    if last_flush.elapsed() >= Duration::from_secs(1) {
                        flush(&mut states, &mut events)?;
                        last_flush = Instant::now();
                    }
                    if let Some(hz) = display_hz
                        && last_display.elapsed().as_secs_f64() >= 1.0 / hz
                    {
                        eprintln!("{}", view.lock().map_err(|_| "Display mutex poisoned")?);
                        last_display = Instant::now();
                    }
                }
                flush(&mut states, &mut events)?;
                states.get_ref().get_ref().sync_all()?;
                events.get_ref().get_ref().sync_all()?;
                Ok((n, e))
            };
            let result = run();
            if let Err(e) = &result {
                *err.lock().unwrap() = Some(e.to_string());
            }
            result
        });
        Ok(Self {
            tx,
            worker,
            dropped_states: 0,
            dropped_events: 0,
            error,
            latest,
        })
    }
    pub fn send(&mut self, message: Message, live: bool) -> Result<()> {
        if let Some(e) = self
            .error
            .lock()
            .map_err(|_| "Writer error mutex poisoned")?
            .as_ref()
        {
            return Err(format!("Storage failed: {e}").into());
        }
        if !live {
            self.tx.send(message).map_err(|_| "Writer disconnected")?;
            return Ok(());
        }
        match self.tx.try_send(message) {
            Ok(()) => Ok(()),
            Err(mpsc::TrySendError::Full(m)) => {
                match m {
                    Message::State(_) => self.dropped_states += 1,
                    Message::Event(_) => self.dropped_events += 1,
                };
                Ok(())
            }
            Err(mpsc::TrySendError::Disconnected(_)) => Err("Writer disconnected".into()),
        }
    }
    pub fn finish(self) -> Result<(u64, u64)> {
        drop(self.tx);
        self.worker.join().map_err(|_| "Writer panicked")?
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn folder() -> PathBuf {
        create_output(&std::env::temp_dir(), "pole-state-test", None).unwrap()
    }
    #[test]
    fn drain_events_and_exclusive_outputs() {
        let dir = folder();
        let mut sink = Sink::start(&dir, 2, None).unwrap();
        for n in 0..200 {
            sink.send(
                Message::Event(Event {
                    now_ns: n,
                    kind: "test".into(),
                    detail: "drain".into(),
                }),
                false,
            )
            .unwrap();
        }
        assert_eq!(sink.finish().unwrap(), (0, 200));
        assert_eq!(
            csv::Reader::from_path(dir.join("events.csv"))
                .unwrap()
                .records()
                .count(),
            200
        );
        assert!(Sink::start(&dir, 2, None).is_err());
        assert!(
            create_output(
                dir.parent().unwrap(),
                "unused",
                dir.file_name().unwrap().to_str()
            )
            .is_err()
        );
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    fn full_queue_counts_without_blocking() {
        let (tx, rx) = mpsc::sync_channel(1);
        let (release, wait) = mpsc::channel();
        let worker = thread::spawn(move || {
            wait.recv().unwrap();
            drop(rx);
            Ok((0, 0))
        });
        let mut sink = Sink {
            tx,
            worker,
            dropped_states: 0,
            dropped_events: 0,
            error: Arc::new(Mutex::new(None)),
            latest: Arc::new(Mutex::new(String::new())),
        };
        for n in 0..3 {
            sink.send(
                Message::Event(Event {
                    now_ns: n,
                    kind: "test".into(),
                    detail: String::new(),
                }),
                true,
            )
            .unwrap();
        }
        assert_eq!(sink.dropped_events, 2);
        release.send(()).unwrap();
        sink.finish().unwrap();
    }
    #[test]
    fn worker_storage_failure_is_propagated() {
        let (tx, rx) = mpsc::sync_channel(1);
        drop(rx);
        let worker = thread::spawn(|| Err("injected disk write failure".into()));
        let mut sink = Sink {
            tx,
            worker,
            dropped_states: 0,
            dropped_events: 0,
            error: Arc::new(Mutex::new(Some("injected disk write failure".into()))),
            latest: Arc::new(Mutex::new(String::new())),
        };
        assert!(
            sink.send(
                Message::Event(Event {
                    now_ns: 0,
                    kind: "test".into(),
                    detail: String::new()
                }),
                true
            )
            .is_err()
        );
        assert!(sink.finish().is_err());
        let dir = folder();
        assert!(write_json(&dir, &serde_json::json!({})).is_err());
        std::fs::remove_dir(dir).unwrap();
    }
}
