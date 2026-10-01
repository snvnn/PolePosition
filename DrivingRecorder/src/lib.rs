pub mod session;
pub mod writer;
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, time::Duration};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub poll_hz: f64,
    pub stale_seconds: f64,
    pub retry_seconds: f64,
    pub queue_capacity: usize,
    pub output_dir: String,
    pub teleport_margin_m: f64,
    pub weather: String,
    pub traffic: String,
    pub fuel: String,
    pub tyres: String,
    pub assists: String,
}
impl Config {
    pub fn validate(&self) -> Result<(), String> {
        for (name, v, max) in [
            ("poll_hz", self.poll_hz, 1000.0),
            ("stale_seconds", self.stale_seconds, 3600.0),
            ("retry_seconds", self.retry_seconds, 3600.0),
            ("teleport_margin_m", self.teleport_margin_m, 10000.0),
        ] {
            if !v.is_finite() || v <= 0.0 || v > max {
                return Err(format!("Invalid {name}"));
            }
        }
        if self.queue_capacity == 0 || self.queue_capacity > 1_000_000 || self.output_dir.is_empty()
        {
            return Err("Invalid queue capacity or output directory".into());
        }
        Ok(())
    }
}

#[derive(Default)]
pub struct Timing {
    counts: BTreeMap<u64, u64>,
    count: u64,
    sum: f64,
    max: f64,
}
impl Timing {
    pub fn add(&mut self, d: Duration) {
        let us = d.as_secs_f64() * 1e6;
        let bucket = (us / 10.0).ceil().min(100_000.0) as u64;
        *self.counts.entry(bucket).or_default() += 1;
        self.count += 1;
        self.sum += us;
        self.max = self.max.max(us);
    }
    pub fn report(&self) -> serde_json::Value {
        let percentile = |q: f64| {
            let target = (self.count as f64 * q).ceil() as u64;
            let mut n = 0;
            for (b, c) in &self.counts {
                n += c;
                if n >= target {
                    return *b as f64 * 10.0;
                }
            }
            0.0
        };
        serde_json::json!({"count":self.count,"mean_us":self.sum/self.count.max(1) as f64,"p95_us":percentile(0.95),"p99_us":percentile(0.99),"max_us":self.max,"percentile_resolution_us":10,"percentile_cap_us":1_000_000})
    }
}
