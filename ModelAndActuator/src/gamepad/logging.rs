use super::*;

pub(super) struct Log {
    tx: Option<mpsc::SyncSender<Value>>,
    pub(super) failed: Arc<AtomicBool>,
    worker: Option<std::thread::JoinHandle<Result<()>>>,
}
impl Log {
    pub(super) fn new(path: &Path) -> Result<Self> {
        let file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(path)?;
        let (tx, rx) = mpsc::sync_channel::<Value>(4096);
        let failed = Arc::new(AtomicBool::new(false));
        let f = failed.clone();
        let worker = std::thread::spawn(move || {
            let result = (|| -> Result<()> {
                let mut w = std::io::BufWriter::new(file);
                let mut flush = Instant::now();
                for row in rx {
                    serde_json::to_writer(&mut w, &row)?;
                    writeln!(w)?;
                    if flush.elapsed() > Duration::from_secs(1) {
                        w.flush()?;
                        flush = Instant::now();
                    }
                }
                w.flush()?;
                w.get_ref().sync_all()?;
                Ok(())
            })();
            if result.is_err() {
                f.store(true, Ordering::Release);
            }
            result
        });
        Ok(Self {
            tx: Some(tx),
            failed,
            worker: Some(worker),
        })
    }
    pub(super) fn push(&self, row: Value) {
        if self.tx.as_ref().unwrap().try_send(row).is_err() {
            self.failed.store(true, Ordering::Release);
        }
    }
    pub(super) fn finish(mut self) -> Result<()> {
        self.tx.take();
        self.worker
            .take()
            .unwrap()
            .join()
            .map_err(|_| "Gamepad log thread panicked")??;
        Ok(())
    }
}
