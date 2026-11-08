//! Continuous lap semantics. No device access and no inferred reset attribution.
use std::collections::VecDeque;
use serde::Serialize;

#[derive(Clone, Copy)]
pub struct Observation {
    pub ns: u64, pub sequence: u64, pub epoch: u64, pub ready: bool,
    pub valid: bool, pub s: f64, pub x: f64, pub z: f64,
}
#[derive(Default, Serialize)]
pub struct Episode {
    #[serde(skip)] previous: Option<Observation>,
    #[serde(skip)] window: VecDeque<(u64, f64)>,
    #[serde(skip)] crossing_progress: Option<f64>,
    #[serde(skip)] crossing_ns: Option<u64>,
    pub progress: f64,
    pub valid_laps: u32,
    pub lap_events: Vec<u64>,
    pub lap_times_s: Vec<f64>,
    pub end: Option<End>,
    pub interrupted: bool,
}
#[derive(Serialize)]
pub struct End {pub reason: String, pub observation_ns: u64, pub tracking_epoch: u64}
impl Episode {
    pub fn observe(&mut self, o: Observation, length: f64, boundary: bool, evaluation_laps: u32) {
        if self.end.is_some() {return;}
        if let Some(p) = self.previous {
            let distance=(o.x-p.x).hypot(o.z-p.z);
            if !o.ready || !p.ready || o.epoch!=p.epoch || o.sequence!=p.sequence+1
                || o.ns<=p.ns || o.ns-p.ns>50_000_000 || distance>5. {
                self.finish("observation_discontinuity",o); return;
            }
            // Still stop at the original 20 ms learning boundary. A short timing
            // gap has separate recovery authority; it is never a training pair.
            if o.ns-p.ns>20_000_000 {
                self.finish("observation_timing_gap",o); return;
            }
            let ds=(o.s-p.s+length/2.).rem_euclid(length)-length/2.;
            if ds.abs()>distance+3. {self.finish("progress_discontinuity",o);return;}
            self.progress+=ds;
            if p.valid && !o.valid {self.finish("invalid_lap",o);return;}
            if !o.valid {self.finish("already_invalid",o);return;}
            // Tracker confirms forward line crossings; distance rejects duplicate boundaries.
            if boundary && ds>=0. {
                match self.crossing_progress {
                    None => {self.crossing_progress=Some(self.progress);self.crossing_ns=Some(o.ns);},
                    Some(at) if self.progress-at>=length*0.9 => {
                        self.valid_laps+=1; self.lap_events.push(o.ns);
                        if let Some(at)=self.crossing_ns {self.lap_times_s.push((o.ns-at) as f64/1e9);}
                        self.crossing_ns=Some(o.ns);
                        self.crossing_progress=Some(self.progress);
                        if evaluation_laps>0 && self.valid_laps>=evaluation_laps {
                            self.finish("evaluation_complete",o);return;
                        }
                    }
                    _ => {}
                }
            }
        }
        self.window.push_back((o.ns,self.progress));
        while self.window.len()>1 && self.window[1].0<=o.ns.saturating_sub(10_000_000_000) {self.window.pop_front();}
        if let Some(&(at,s))=self.window.front() {
            if o.ns-at>=10_000_000_000 && self.progress-s<0.5 {self.finish("progress_stall",o);}
        }
        self.previous=Some(o);
    }
    fn finish(&mut self, reason:&str,o:Observation) {
        self.end=Some(End{reason:reason.into(),observation_ns:o.ns,tracking_epoch:o.epoch});
    }
}

#[cfg(test)] mod tests {
    use super::*;
    fn o(n:u64,s:f64)->Observation {Observation{ns:n*10_000_000,sequence:n,epoch:1,ready:true,valid:true,s,x:s,z:0.}}
    #[test] fn invalid_event_survives_reset(){let mut e=Episode::default();e.observe(o(1,0.),100.,false,0);let mut b=o(2,0.);b.valid=false;e.observe(b,100.,false,0);b.epoch=2;e.observe(b,100.,false,0);assert_eq!(e.end.unwrap().reason,"invalid_lap");}
    #[test] fn gap_before_invalid_is_not_failure_training(){let mut e=Episode::default();e.observe(o(1,0.),100.,false,0);let mut b=o(4,0.);b.valid=false;e.observe(b,100.,false,0);assert_eq!(e.end.unwrap().reason,"observation_discontinuity");}
    #[test] fn timing_gap_is_separate_and_never_accepted_as_training() {
        for (dt,expected) in [(20_000_000,None),(20_000_001,Some("observation_timing_gap")),
            (50_000_000,Some("observation_timing_gap")),(50_000_001,Some("observation_discontinuity"))] {
            let mut e=Episode::default();let a=o(1,0.);let mut b=o(2,0.1);b.ns=a.ns+dt;
            e.observe(a,100.,false,0);e.observe(b,100.,false,0);
            assert_eq!(e.end.as_ref().map(|v|v.reason.as_str()),expected);
        }
        for field in ["epoch","sequence","position","ready"] {
            let mut e=Episode::default();let a=o(1,0.);let mut b=o(2,0.1);b.ns=a.ns+30_000_000;
            match field {"epoch"=>b.epoch=2,"sequence"=>b.sequence=4,"position"=>b.x=100.,_=>b.ready=false}
            e.observe(a,100.,false,0);e.observe(b,100.,false,0);
            assert_eq!(e.end.unwrap().reason,"observation_discontinuity");
        }
    }
    #[test] fn rolling_stall_after_progress(){let mut e=Episode::default();for n in 1..=2101 {e.observe(o(n,(n.min(1000) as f64)*0.001),100.,false,0);}assert_eq!(e.end.unwrap().reason,"progress_stall");}
    #[test] fn first_and_duplicate_crossings_are_not_laps(){let mut e=Episode::default();for n in 1..=5 {e.observe(o(n,0.),100.,true,0);}assert_eq!(e.valid_laps,0);}
    #[test] fn comparison_counts_full_lap_and_measures_between_crossings(){
        let mut e=Episode::default();
        for n in 1..=2000 {let mut b=o(n,(n as f64*0.1)%100.);b.x=n as f64*0.1;e.observe(b,100.,n%1000==0,1);}
        assert_eq!(e.valid_laps,1);assert_eq!(e.lap_times_s,vec![10.]);
        assert_eq!(e.end.unwrap().reason,"evaluation_complete");
    }
    #[test] fn training_continues_and_evaluation_stops_after_three_full_laps(){
        for target in [0,3] {
            let mut e=Episode::default();
            for n in 1..=4000 {let mut b=o(n,(n as f64*0.1)%100.);b.x=n as f64*0.1;e.observe(b,100.,n%1000==0,target);}
            assert_eq!(e.valid_laps,3);
            assert_eq!(e.end.as_ref().map(|v|v.reason.as_str()),if target==0{None}else{Some("evaluation_complete")});
        }
    }
}
