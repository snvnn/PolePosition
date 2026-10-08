use crate::session::{Event, Row};
use serde::Serialize;
use std::{
    fs::{File, OpenOptions},
    io::{self, BufWriter, Write},
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc::{self, SyncSender, TrySendError},
    },
    thread::{self, JoinHandle},
    time::Duration,
};

pub const SAMPLE_FIELDS: &[&str] = &[
    "physics_packet_id",
    "graphics_packet_id",
    "speed_kmh",
    "rpm",
    "gear_raw",
    "throttle",
    "brake",
    "steering_raw",
    "position_x",
    "position_y",
    "position_z",
    "heading_rad",
    "completed_laps",
    "lap_time_ms",
    "progress",
    "sector",
    "lap_valid",
    "session_type",
    "session_index",
    "in_pit",
    "fuel_litres",
    "clock_seconds",
    "rain_intensity",
];
pub fn create_new(path: &Path) -> io::Result<File> {
    OpenOptions::new().write(true).create_new(true).open(path)
}
pub fn write_json(path: &Path, value: &impl Serialize) -> io::Result<()> {
    let mut f = create_new(path)?;
    serde_json::to_writer_pretty(&mut f, value)?;
    f.write_all(b"\n")?;
    f.sync_all()
}
pub enum Message {
    Row(Row),
    Event(Event),
    Input(poleposition_gamepad::InputRow),
}
pub struct Sink {
    tx: SyncSender<Message>,
    handle: JoinHandle<io::Result<(u64, u64)>>,
    pub failed: Arc<AtomicBool>,
    pub dropped_rows: u64,
    pub dropped_events: u64,
}
impl Sink {
    pub fn start(path: &Path, capacity: usize) -> io::Result<Self> {
        Self::start_with_inputs(path, capacity, false)
    }
    pub fn start_with_inputs(path: &Path, capacity: usize, gamepad: bool) -> io::Result<Self> {
        let tf = create_new(&path.join("telemetry.csv"))?;
        let ef = create_new(&path.join("events.csv"))?;
        let inf = if gamepad { Some(create_new(&path.join("inputs.csv"))?) } else { None };
        let (tx, rx) = mpsc::sync_channel(capacity);
        let failed = Arc::new(AtomicBool::new(false));
        let flag = failed.clone();
        let handle = thread::spawn(move || {
            let result = (|| {
                let mut data = csv::Writer::from_writer(BufWriter::new(tf));
                let mut inputs = inf.map(|f| csv::Writer::from_writer(BufWriter::new(f)));
                let mut events = csv::WriterBuilder::new()
                    .has_headers(false)
                    .from_writer(BufWriter::new(ef));
                let mut header = vec![
                    "sequence",
                    "segment_id",
                    "received_unix_ns",
                    "elapsed_ns",
                    "interval_ns",
                    "physics_changed_ns",
                    "graphics_changed_ns",
                    "queue_drops",
                ];
                header.extend_from_slice(SAMPLE_FIELDS);
                data.write_record(&header)?;
                events.write_record(["elapsed_ns", "segment_id", "kind", "detail"])?;
                let (mut rows, mut event_count) = (0, 0);
                let mut last_flush = std::time::Instant::now();
                loop {
                    match rx.recv_timeout(Duration::from_millis(250)) {
                        Ok(Message::Row(r)) => {
                            write_row(&mut data, &r)?;
                            rows += 1;
                        }
                        Ok(Message::Event(e)) => {
                            events.serialize(e)?;
                            event_count += 1;
                        }
                        Ok(Message::Input(r)) => {
                            inputs.as_mut().ok_or_else(||io::Error::other("Input stream not enabled"))?.serialize(r)?;
                        }
                        Err(mpsc::RecvTimeoutError::Timeout) => (),
                        Err(mpsc::RecvTimeoutError::Disconnected) => break,
                    }
                    if last_flush.elapsed() >= Duration::from_secs(1) {
                        data.flush()?;
                        events.flush()?;
                        if let Some(w)=&mut inputs { w.flush()?; }
                        last_flush = std::time::Instant::now();
                    }
                }
                data.flush()?;
                events.flush()?;
                data.get_ref().get_ref().sync_all()?;
                events.get_ref().get_ref().sync_all()?;
                if let Some(w)=&mut inputs { w.flush()?; w.get_ref().get_ref().sync_all()?; }
                Ok((rows, event_count))
            })();
            if result.is_err() {
                flag.store(true, Ordering::Release);
            }
            result
        });
        Ok(Self {
            tx,
            handle,
            failed,
            dropped_rows: 0,
            dropped_events: 0,
        })
    }
    pub fn send(&mut self, msg: Message) -> io::Result<()> {
        match self.tx.try_send(msg) {
            Ok(()) => Ok(()),
            Err(TrySendError::Full(msg)) => {
                match msg {
                    Message::Row(_) => self.dropped_rows += 1,
                    Message::Event(_) => self.dropped_events += 1,
                    Message::Input(_) => { self.dropped_rows += 1; return Err(io::Error::other("Gamepad input queue overflow")); }
                };
                Ok(())
            }
            Err(TrySendError::Disconnected(_)) => Err(io::Error::other("Recording writer stopped")),
        }
    }
    pub fn finish(self) -> io::Result<(u64, u64)> {
        drop(self.tx);
        self.handle
            .join()
            .map_err(|_| io::Error::other("Writer thread panicked"))?
    }
}
fn write_row<W: Write>(writer: &mut csv::Writer<W>, r: &Row) -> io::Result<()> {
    let f = &r.frame;
    let mut fields = vec![
        r.sequence.to_string(),
        r.segment_id.to_string(),
        f.received_unix_ns.to_string(),
        f.elapsed_ns.to_string(),
        r.interval_ns.to_string(),
        f.physics_changed_ns.to_string(),
        f.graphics_changed_ns.to_string(),
        r.queue_drops.to_string(),
    ];
    let json = serde_json::to_value(&f.sample)?;
    for name in SAMPLE_FIELDS {
        fields.push(json[*name].to_string());
    }
    writer.write_record(fields)?;
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn full_queue_is_nonblocking() {
        let (tx, _rx) = mpsc::sync_channel(1);
        let event = || {
            Message::Event(Event {
                elapsed_ns: 0,
                segment_id: 0,
                kind: "test".into(),
                detail: "".into(),
            })
        };
        tx.try_send(event()).unwrap_or_else(|_| panic!());
        assert!(matches!(tx.try_send(event()), Err(TrySendError::Full(_))));
    }

    #[test]
    fn sink_counts_overflow_and_reports_worker_failure() {
        let (tx, rx) = mpsc::sync_channel(1);
        let (release_tx, release_rx) = mpsc::channel();
        let handle = thread::spawn(move || {
            release_rx.recv().unwrap();
            drop(rx);
            Err(io::Error::other("simulated writer failure"))
        });
        let mut sink = Sink {
            tx,
            handle,
            failed: Arc::new(AtomicBool::new(false)),
            dropped_rows: 0,
            dropped_events: 0,
        };
        let event = || {
            Message::Event(Event {
                elapsed_ns: 0,
                segment_id: 0,
                kind: "test".into(),
                detail: String::new(),
            })
        };
        sink.send(event()).unwrap();
        sink.send(event()).unwrap();
        assert_eq!(sink.dropped_events, 1);
        assert!(sink.send(Message::Input(poleposition_gamepad::InputRow::new(1,1,1,2,0,Some((1,Default::default()))))).is_err());
        assert_eq!(sink.dropped_rows,1);
        release_tx.send(()).unwrap();
        assert!(sink.finish().is_err());
    }
    #[test]
    fn drain_and_no_overwrite() {
        let root =
            std::env::temp_dir().join(format!("pp-writer-{}", poleposition_telemetry::unix_ns()));
        std::fs::create_dir(&root).unwrap();
        let mut sink = Sink::start(&root, 8).unwrap();
        sink.send(Message::Event(Event {
            elapsed_ns: 1,
            segment_id: 0,
            kind: "test".into(),
            detail: "comma, quote\"\n한글".into(),
        }))
        .unwrap();
        assert_eq!(sink.finish().unwrap(), (0, 1));
        assert!(Sink::start(&root, 8).is_err());
        let count = csv::Reader::from_path(root.join("events.csv"))
            .unwrap()
            .records()
            .count();
        assert_eq!(count, 1);
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn io_failure_propagates() {
        struct Fail;
        impl Write for Fail {
            fn write(&mut self, _: &[u8]) -> io::Result<usize> {
                Err(io::Error::other("disk failed"))
            }
            fn flush(&mut self) -> io::Result<()> {
                Err(io::Error::other("disk failed"))
            }
        }
        let mut w = csv::Writer::from_writer(Fail);
        w.write_record(["test"]).unwrap();
        assert!(w.flush().is_err());
    }
}
