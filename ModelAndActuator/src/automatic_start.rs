//! Explicit first start or verified restart, with fresh telemetry input proof.
use serde::Deserialize;
use serde_json::Value;

#[derive(Deserialize)]
pub struct AutomaticStart {
    pub schema: String,
    pub complete: bool,
    pub created_unix_ns: u128,
    pub position: [f64;3],
    pub heading: f64,
    pub car: String,
    pub track: String,
    pub position_tolerance_m: f64,
    pub heading_tolerance_rad: f64,
}
impl AutomaticStart {
    pub fn valid(&self, now: u128) -> bool {
        self.schema=="acc_auto_start_v1" && self.complete
            && now>=self.created_unix_ns && now-self.created_unix_ns<90_000_000_000
            && self.position.iter().all(|v|v.is_finite()) && self.heading.is_finite()
            && self.car=="bmw_m4_gt3" && self.track=="brands_hatch"
            && self.position_tolerance_m.is_finite() && (0. ..=1.).contains(&self.position_tolerance_m)
            && self.heading_tolerance_rad.is_finite() && (0. ..=0.1).contains(&self.heading_tolerance_rad)
    }
    pub fn matches(&self, position:[f64;3], heading:f64, session:i32, gear:i32, valid:bool) -> bool {
        let distance=position.iter().zip(self.position).map(|(a,b)|(a-b).powi(2)).sum::<f64>().sqrt();
        let angle=(heading-self.heading).sin().atan2((heading-self.heading).cos()).abs();
        session==3 && (1..=2).contains(&gear) && valid && distance<=self.position_tolerance_m && angle<=self.heading_tolerance_rad
    }
}
fn confirmed_training_event(s: &Value) -> bool {
    let end = &s["episode_end"];
    let event = &end["event"];
    event.is_object() && event == &s["episode"]["end"]
        && end["observation_ns"].as_u64().is_some() && end["tracking_epoch"].as_u64().is_some()
        && ["reason", "observation_ns", "tracking_epoch"].iter().all(|key| event[*key] == end[*key])
        && end["policy_sha256"].is_string() && end["policy_sha256"] == s["model_sha256"]
}
pub fn eligible_previous(s:&Value)->bool {
    s["policy_comparison"]==false && s["continuous_semantics"]=="gamepad_continuous_iql_v1"
        && confirmed_training_event(s)
        && matches!(s["episode_end"]["reason"].as_str(),Some("invalid_lap"|"progress_stall"))
        && s["cancelled"]==false && s["episode"]["interrupted"]==false
        && s["error"].is_null() && s["logging_failure"]==false
        && ["standstill_confirmed","neutralized","released"].iter().all(|k|s["shutdown"][k]==true)
}
pub fn eligible_comparison(s:&Value)->bool {
    s["policy_comparison"]==true && s["continuous_semantics"]=="gamepad_continuous_iql_v1"
        && matches!(s["episode_end"]["reason"].as_str(),Some("invalid_lap"|"progress_stall"|"evaluation_complete"|"time_limit"))
        && s["cancelled"]==false && s["episode"]["interrupted"]==false
        && s["error"].is_null() && s["logging_failure"]==false
        && ["standstill_confirmed","neutralized","released"].iter().all(|k|s["shutdown"][k]==true)
}
// Recovery skips an unclassifiable trajectory; it never authorizes learning it.
pub fn eligible_relocation(s:&Value, comparison:bool)->bool {
    s["policy_comparison"].as_bool().unwrap_or(false)==comparison
        && s["continuous_semantics"]=="gamepad_continuous_iql_v1"
        && s["episode_end"]["reason"]=="tracking_not_ready"
        && s["episode_end"]["event"].is_null()
        && s["relocation_evidence"]["verified"]==true
        && s["relocation_evidence"]["observation_ns"]==s["episode_end"]["observation_ns"]
        && s["shutdown"]["kind"]=="relocated_standstill"
        && s["cancelled"]==false && s["episode"]["interrupted"]==false
        && s["error"].is_null() && s["logging_failure"]==false
        && ["standstill_confirmed","neutralized","released"].iter().all(|k|s["shutdown"][k]==true)
}
pub fn authorized_comparison(number:u32,initial:bool,previous:Option<&Value>)->bool {
    match previous {None=>number==1 && initial,Some(s)=>number>1 && !initial && eligible_comparison(s)}
}
pub fn eligible_timing_gap(s:&Value)->bool {
    s["policy_comparison"]==false && s["continuous_semantics"]=="gamepad_continuous_iql_v1"
        && s["episode_end"]["reason"]=="observation_timing_gap"
        && confirmed_training_event(s)
        && s["cancelled"]==false && s["episode"]["interrupted"]==false
        && s["error"].is_null() && s["logging_failure"]==false
        && s["shutdown"]["kind"]=="continuous_stop"
        && s["shutdown"]["tracking_epoch"]==s["episode_end"]["tracking_epoch"]
        && s["shutdown"]["continuous_stop_confirmed"]==true
        && ["standstill_confirmed","neutralized","released"].iter().all(|k|s["shutdown"][k]==true)
}
pub fn authorized(number:u32, initial:bool, previous:Option<&Value>)->bool {
    match previous {
        None=>number==1 && initial,
        Some(s)=>number>1 && !initial && eligible_previous(s),
    }
}

#[derive(Default)]
pub struct InputProof { started:Option<u64>, seen:[bool;3], brake:bool }
impl InputProof {
    pub fn command(&mut self,now:u64)->f64 {
        let start=*self.started.get_or_insert(now);
        match now.saturating_sub(start)/1_500_000_000 {0=>-0.5,1=>0.5,_=>0.0}
    }
    pub fn observe(&mut self,now:u64,steer:f64,brake:f64) {
        let Some(start)=self.started else{return;};
        let elapsed=now.saturating_sub(start);let stage=elapsed/1_500_000_000;
        if stage>2 || elapsed%1_500_000_000<750_000_000{return;}
        self.seen[stage as usize]|=match stage {0=>steer < -0.05,1=>steer > 0.05,_=>steer.abs()<0.03};
        self.brake|=brake>0.25;
    }
    pub fn ready(&self,now:u64)->bool {
        self.started.is_some_and(|s|now.saturating_sub(s)>=4_500_000_000) && self.seen.iter().all(|v|*v) && self.brake
    }
    pub fn failed(&self,now:u64)->bool {
        self.started.is_some_and(|s|now.saturating_sub(s)>6_000_000_000) && !self.ready(now)
    }
}
#[cfg(test)] mod tests {
    use super::*;
    fn attach_event(s: &mut Value) {
        let event = serde_json::json!({"reason":s["episode_end"]["reason"], "observation_ns":20, "tracking_epoch":1});
        s["episode_end"] = event.clone();
        s["episode_end"]["event"] = event.clone();
        s["episode"]["end"] = event;
        s["episode_end"]["policy_sha256"] = "model".into();
        s["model_sha256"] = "model".into();
    }
    #[test] fn timing_recovery_does_not_authorize_learning_or_comparison() {
        let mut base=serde_json::json!({"policy_comparison":false,"continuous_semantics":"gamepad_continuous_iql_v1",
            "episode_end":{"reason":"observation_timing_gap","event":{"reason":"observation_timing_gap"},"tracking_epoch":1},
            "cancelled":false,"episode":{"interrupted":false},"error":null,"logging_failure":false,
            "shutdown":{"kind":"continuous_stop","tracking_epoch":1,"continuous_stop_confirmed":true,"standstill_confirmed":true,"neutralized":true,"released":true}});
        attach_event(&mut base);
        assert!(eligible_timing_gap(&base));assert!(!eligible_previous(&base));assert!(!eligible_comparison(&base));
        for field in ["cancelled","error","logging_failure","policy_comparison"] {
            let mut s=base.clone();s[field]=true.into();assert!(!eligible_timing_gap(&s));
        }
        for field in ["released","neutralized","standstill_confirmed","continuous_stop_confirmed"] {
            let mut s=base.clone();s["shutdown"][field]=false.into();assert!(!eligible_timing_gap(&s));
        }
        let mut s=base.clone();s["shutdown"]["tracking_epoch"]=2.into();assert!(!eligible_timing_gap(&s));
        s=base.clone();s["episode_end"]["reason"]="observation_discontinuity".into();assert!(!eligible_timing_gap(&s));
    }
    #[test] fn relocation_recovery_is_separate_bounded_authority_not_training_eligibility() {
        let mut s=serde_json::json!({"policy_comparison":true,"continuous_semantics":"gamepad_continuous_iql_v1",
            "episode_end":{"reason":"tracking_not_ready","event":null,"observation_ns":20},
            "relocation_evidence":{"verified":true,"observation_ns":20},
            "cancelled":false,"episode":{"interrupted":false},"error":null,"logging_failure":false,
            "shutdown":{"kind":"relocated_standstill","standstill_confirmed":true,"neutralized":true,"released":true}});
        assert!(eligible_relocation(&s,true));assert!(!eligible_relocation(&s,false));
        assert!(!eligible_previous(&s));assert!(!eligible_comparison(&s));
        s["cancelled"]=true.into();assert!(!eligible_relocation(&s,true));s["cancelled"]=false.into();
        s["episode_end"]["reason"]="telemetry_expired".into();assert!(!eligible_relocation(&s,true));
        s["episode_end"]["reason"]="tracking_not_ready".into();
        s["relocation_evidence"]["observation_ns"]=21.into();assert!(!eligible_relocation(&s,true));
        s["relocation_evidence"]["observation_ns"]=20.into();
        s["shutdown"]["released"]=false.into();assert!(!eligible_relocation(&s,true));
    }
    #[test] fn comparison_restart_cannot_leak_into_training() {
        for reason in ["invalid_lap","progress_stall","evaluation_complete","time_limit"] {
            let mut s=serde_json::json!({"policy_comparison":true,"continuous_semantics":"gamepad_continuous_iql_v1",
                "episode_end":{"reason":reason},"cancelled":false,"episode":{"interrupted":false},"error":null,
                "logging_failure":false,"shutdown":{"standstill_confirmed":true,"neutralized":true,"released":true}});
            assert!(authorized_comparison(2,false,Some(&s)));assert!(!authorized(2,false,Some(&s)));
            s["cancelled"]=serde_json::json!(true);assert!(!eligible_comparison(&s));
            s["cancelled"]=serde_json::json!(false);s["shutdown"]["released"]=serde_json::json!(false);assert!(!eligible_comparison(&s));
        }
        assert!(authorized_comparison(1,true,None));assert!(!authorized_comparison(2,true,None));
        assert!(!eligible_comparison(&serde_json::json!({"policy_comparison":true,"episode_end":{"reason":"telemetry_expired"}})));
    }
    #[test] fn first_start_requires_explicit_opt_in_and_cannot_skip_previous_stop() {
        assert!(authorized(1,true,None));assert!(!authorized(1,false,None));
        assert!(!authorized(2,true,None));assert!(!authorized(1,true,Some(&serde_json::json!({}))));
        assert!(!authorized(2,false,Some(&serde_json::json!({}))));
    }
    #[test] fn input_proof_requires_each_settled_direction_brake_and_recent_command() {
        let mut p=InputProof::default();p.observe(900_000_000,-1.,1.);assert!(!p.ready(9_000_000_000));
        assert_eq!(p.command(0),-0.5);p.observe(800_000_000,-0.5,0.5);
        assert_eq!(p.command(1_500_000_000),0.5);p.observe(2_300_000_000,0.5,0.5);
        assert_eq!(p.command(3_000_000_000),0.);p.observe(3_800_000_000,0.,0.5);
        assert!(!p.ready(4_400_000_000));assert!(p.ready(4_500_000_000));assert!(!p.failed(7_000_000_000));
        let mut absent=InputProof::default();absent.command(0);absent.observe(800_000_000,0.,0.);
        assert!(absent.failed(6_000_000_001));
    }
    #[test] fn receipt_rejects_expiry_future_nonfinite_and_wrong_spawn() {
        let mut r=AutomaticStart{schema:"acc_auto_start_v1".into(),complete:true,created_unix_ns:1,
            position:[1.,2.,3.],heading:0.,car:"bmw_m4_gt3".into(),track:"brands_hatch".into(),
            position_tolerance_m:1.,heading_tolerance_rad:0.1};
        assert!(r.valid(1));assert!(!r.valid(0));assert!(!r.valid(90_000_000_001));
        assert!(r.matches([1.,2.,3.],0.,3,2,true));
        for (p,h,s,g,v) in [([3.,2.,3.],0.,3,2,true),([1.,2.,3.],1.,3,2,true),
            ([1.,2.,3.],0.,0,2,true),([1.,2.,3.],0.,3,0,true),([1.,2.,3.],0.,3,2,false)] {
            assert!(!r.matches(p,h,s,g,v));
        }
        r.heading=f64::NAN;assert!(!r.valid(1));
    }
    #[test] fn cancelled_or_unreleased_episode_cannot_authorize_restart() {
        let mut s=serde_json::json!({"policy_comparison":false,"continuous_semantics":"gamepad_continuous_iql_v1",
            "episode_end":{"reason":"invalid_lap"},"cancelled":false,"episode":{"interrupted":false},
            "error":null,"logging_failure":false,"shutdown":{"standstill_confirmed":true,"neutralized":true,"released":true}});
        assert!(!eligible_previous(&s));
        attach_event(&mut s);
        assert!(eligible_previous(&s));
        for field in ["observation_ns","tracking_epoch","policy_sha256"] {
            let mut changed=s.clone();changed["episode_end"][field]=99.into();
            assert!(!eligible_previous(&changed));
        }
        s["cancelled"]=true.into();assert!(!eligible_previous(&s));s["cancelled"]=false.into();
        s["shutdown"]["released"]=false.into();assert!(!eligible_previous(&s));
        s["shutdown"]["released"]=true.into();s["episode_end"]["reason"]="user_stop".into();assert!(!eligible_previous(&s));
    }
}
