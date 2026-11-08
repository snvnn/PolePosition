    use super::*;
    #[test]
    fn off_track_stop_uses_fresh_raw_speed_but_never_stale_or_wrong_identity() {
        for bad in ["none", "identity", "paused", "stale_physics", "stale_graphics", "moving", "nan"] {
            let config: crate::Config = toml::from_str(include_str!("../../config.toml")).unwrap();
            let mut guard = Guard::new_gamepad(config);
            guard.armed_ns = Some(1);
            guard.stop(1, "invalid_lap");
            guard.acknowledge(1, Axes::STOP);
            for i in 1..=70 {
                let now = 1_000_000_000 + i * 10_000_000;
                let mut health = Health {
                    observation_sequence:i, observation_ns:now, physics_ns:now, graphics_ns:now,
                    tracking_epoch:1, speed_kmh:0.004, ready:false, live:true, identity_ok:true,
                    ..Default::default()
                };
                match bad {
                    "identity" => health.identity_ok=false,
                    "paused" => {health.live=false;health.game_halted=true;},
                    "stale_physics" => health.physics_ns=0,
                    "stale_graphics" => health.graphics_ns=0,
                    "moving" => health.speed_kmh=2.,
                    "nan" => health.speed_kmh=f64::NAN,
                    _ => {}
                }
                continuous_stop_health(&mut health);
                let action=guard.tick(now,&health,Signals::default());
                if action==Action::Release { assert!(i>=51); break; }
            }
            assert_eq!(guard.stop_confirmed,bad=="none","{bad}");
            assert_eq!(guard.mode==Mode::Finished,bad=="none","{bad}");
        }
    }
    #[test]
    fn reset_during_braking_cannot_be_counted_as_normal_stop() {
        let mut c=DriveContinuity::default();
        c.observe(1,false); // Preparation can reacquire before F7.
        c.arm(2);c.observe(2,true);
        assert!(!c.confirms_stop(false));
        assert!(c.confirms_stop(true));
        c.observe(3,false); // Car reset while braking.
        c.observe(3,true);c.arm(3);
        assert!(!c.confirms_stop(true)); // New stationary observations cannot undo it.
        let mut stale=DriveContinuity::default();stale.arm(1);stale.observe(1,false);
        stale.observe(1,true);assert!(!stale.confirms_stop(true));
    }
    #[test]
    fn processing_clock_is_sampled_after_source_read() {
        use std::cell::Cell;
        struct Source<'a>(&'a Cell<u64>);
        impl TelemetrySource for Source<'_> {
            fn poll(&mut self) -> Poll { self.0.set(102); Poll::NoData }
        }
        let clock = Cell::new(100);
        let mut source = Source(&clock);
        let (_, processed_at) = poll_with_time(&mut source, || clock.get());
        assert_eq!(processed_at, 102);
    }

    #[test]
    #[ignore = "Read-only replay requires GAMEPAD_REPLAY_LOG from a real diagnostic"]
    fn replay_recorded_waiting_observations() -> Result<()> {
        let log = std::env::var("GAMEPAD_REPLAY_LOG")?;
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let c: Config = toml::from_str(&std::fs::read_to_string(root.join("GamepadIO/trial.toml"))?)?;
        let p = Prepared::load(&root.join(&c.prepared))?;
        let make = || Processor::new(p.model.clone(), p.config.clone(), p.identity.clone(), p.sign, p.offset);
        let mut old = make(); let mut fixed = make();
        let network: Network = serde_json::from_value(json(&root.join(&c.model).join("model.json"))?)?;
        network.validate()?;
        let speed: SpeedProfile = serde_json::from_value(json(&root.join(&c.model).join("speed-profile.json"))?)?;
        let cap = [10.,20.,40.,80.,120.,160.,200.,240.].into_iter().find(|v|speed.speed_kmh.iter().all(|s|s<=v)).unwrap();
        let gate: poleposition_baseline::Config = toml::from_str(&std::fs::read_to_string(root.join(&c.controller_config))?)?;
        let mut features = FeaturesV2::gamepad();
        let mut sent: Option<(u64, Axes)> = None;
        let (mut observations,mut old_reversals,mut ready,mut predictions)=(0,0,0,0);
        let mut reasons = std::collections::BTreeMap::<String,usize>::new();
        let mut first_prediction = None;
        for line in std::fs::read_to_string(log)?.lines() {
            let r: Value = serde_json::from_str(line)?;
            if r["kind"]=="sent" {
                let a=&r["axes"];
                sent=Some((r["send_end_ns"].as_u64().unwrap(),Axes{steering:a["steering"].as_f64().unwrap(),throttle:a["throttle"].as_f64().unwrap(),brake:a["brake"].as_f64().unwrap()}));
                continue;
            }
            if r["kind"]!="observation" {continue;}
            observations+=1;
            let t=r["observation_ns"].as_u64().unwrap();
            let mut v=r["sample"].clone();
            for (k,x) in [("sequence",r["sequence"].clone()),("segment_id",r["state"]["segment_id"].clone()),("elapsed_ns",value!(t)),("physics_changed_ns",value!(t)),("graphics_changed_ns",value!(t))] {v[k]=x;}
            let mut o: poleposition_state::processor::Observation = serde_json::from_value(v)?;
            o.identity=Some(p.identity.clone());
            let bad=old.update(o.clone(),r["state"]["now_ns"].as_u64().unwrap());
            if bad.reason=="time_reversal" {old_reversals+=1;}
            // Synthetic processing delay for replay only; no original log is modified.
            let now=t+1_000;
            let state=fixed.update(o,now);
            if state.status=="ready" {ready+=1;}
            let gear=r["sample"]["gear_raw"].as_i64().unwrap() as i32;
            if let Some(o)=poleposition_baseline::Observation::from_state(&state,t,gear) {
                if let Err(reason)=o.check_with_speed_limit(&gate,now,true,false) {
                    *reasons.entry(reason.to_string()).or_default()+=1;continue;
                }
                if let Some((sent_at,axes))=sent {
                    if sent_at>=t || t-sent_at>=100_000_000 {continue;}
                    if let Some(x)=features.update(&Input{frame:Frame{sequence:r["sequence"].as_u64().unwrap(),observation_ns:t,tracking_epoch:state.tracking_epoch,ready:true,speed_kmh:r["sample"]["speed_kmh"].as_f64().unwrap(),target_speed_kmh:speed.at(o.s_m,cap),rpm:r["sample"]["rpm"].as_f64().unwrap(),lateral_error_m:o.lateral_error_m,heading_error_rad:o.heading_error_rad,s_m:o.s_m},speed_cap_kmh:cap,previous_axes:[axes.steering,axes.throttle,axes.brake],previous_applied_ns:sent_at,gear_raw:gear},&p.model,&speed)? {
                        let a=network.predict(&x)?;predictions+=1;
                        if first_prediction.is_none(){first_prediction=Some(a);}
                    }
                }
            }
        }
        let result=value!({"observations":observations,"original_time_reversals":old_reversals,"corrected_ready":ready,"predictions":predictions,"gate_rejections":reasons,"first_prediction":first_prediction,"replay_only":true,"live_driving_verified":false});
        println!("{result}");
        std::fs::write(root.join("target/gamepad-clock-replay.json"),serde_json::to_vec_pretty(&result)?)?;
        assert_eq!(observations,old_reversals);
        assert!(ready>100 && predictions>100);
        Ok(())
    }
    #[test] fn engine_torque_and_gear_cannot_be_masked_by_brake_command() {
        assert!(stationary_response_ready(0,1,0.));
        for (rpm,gear,speed) in [(1,1,0.),(900,1,0.),(0,2,0.),(0,0,0.),(0,1,1.01),(0,1,f64::NAN)] {
            assert!(!stationary_response_ready(rpm,gear,speed));
        }
    }
