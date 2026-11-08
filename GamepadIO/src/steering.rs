//! First-order model-only steering smoothing with explicit authority handoff.
#[derive(Default)]
pub struct Smoother { state: Option<(u64,f64)> }
impl Smoother {
    pub fn sample(&mut self, now:u64, requested:f64, tau_ms:f64, model_active:bool)->f64 {
        let output=match self.state {
            Some((t,old)) if model_active && tau_ms>0. && now>t && now-t<100_000_000 => {
                let alpha=1.-(-((now-t) as f64)/1e6/tau_ms).exp();
                old+alpha*(requested-old)
            },
            _=>requested,
        };
        self.state=Some((now,output));output
    }
}
#[cfg(test)] mod tests {
    use super::*;
    #[test] fn subdivision_preserves_step_response() {
        let mut a=Smoother::default();let mut b=Smoother::default();
        a.sample(0,0.,30.,false);b.sample(0,0.,30.,false);
        let one=a.sample(20_000_000,1.,30.,true);
        b.sample(10_000_000,1.,30.,true);let two=b.sample(20_000_000,1.,30.,true);
        assert!((one-two).abs()<1e-12);
    }
    #[test] fn actual_time_constant_full_range_and_manual_bypass() {
        let mut s=Smoother::default();s.sample(0,0.,30.,false);
        let x=s.sample(20_000_000,1.,30.,true);
        assert!((x-(1.-(-2f64/3.).exp())).abs()<1e-12);
        assert_eq!(s.sample(30_000_000,-1.,30.,false),-1.);
        assert_eq!(s.sample(40_000_000,1.,0.,true),1.);
        assert_eq!(s.sample(200_000_000,-1.,30.,true),-1.);
        for i in 21..80 {s.sample(i*10_000_000,1.,30.,true);}
        assert!(s.sample(800_000_000,1.,30.,true)>0.999);
    }
}
