use super::*;

/// One F7/F8 owner for driving, validation, learning and checkpoint publication.
pub fn supervise(session:&Path, manager:&poleposition_management::Managed)->Result<(PathBuf,String)> {
    let keys=platform::Hotkeys::register()?;
    let interrupt=Arc::new(AtomicBool::new(false));let flag=interrupt.clone();
    ctrlc::set_handler(move || flag.store(true,Ordering::Release))?;
    let mut number=1;
    let mut previous_run:Option<PathBuf>=None;
    manager.emit(value!({"event":"progress","phase":"supervisor_ready"}));
    let reason=loop {
        if keys.stop.load(Ordering::Acquire)||interrupt.load(Ordering::Acquire) {break "f8";}
        if let Some(r)=manager.control.stop_reason(){break r;}
        if session.join("done.json").is_file(){break "session_complete";}
        // Every preparation request requires a fresh F7. Idle keypresses are discarded.
        keys.start.store(false,Ordering::Release);
        let request=session.join(format!("request-{number}.json"));
        if request.is_file(){
            let r=json(&request)?;
            let path=Path::new(r["config"].as_str().ok_or("Missing config")?);
            for (file,sha) in r["bindings"].as_object().ok_or("Missing request hashes")? {
                if hash(Path::new(file))?!=sha.as_str().ok_or("Invalid request hash")? {return Err("Request binding changed".into());}
            }
            let config:Config=toml::from_str(&std::fs::read_to_string(path)?)?;
            if !config.continuous_learning {return Err("Supervisor requires continuous gamepad semantics".into());}
            let automatic=if let Some(receipt)=r["automatic_start"].as_str() {
                let previous=previous_run.as_ref().map(|p|json(&p.join("summary.json"))).transpose()?;
                let authorized=if config.policy_comparison {
                    crate::automatic_start::authorized_comparison(number,config.automatic_initial_start,previous.as_ref())
                } else {crate::automatic_start::authorized(number,config.automatic_initial_start,previous.as_ref())};
                let recovery=config.allow_verified_relocation_recovery && number>1 && !config.automatic_initial_start
                    && previous.as_ref().is_some_and(|s|crate::automatic_start::eligible_relocation(s,config.policy_comparison));
                let timing_recovery=config.allow_verified_timing_gap_recovery && number>1 && !config.automatic_initial_start
                    && !config.policy_comparison && previous.as_ref().is_some_and(crate::automatic_start::eligible_timing_gap);
                if !authorized && !recovery && !timing_recovery {
                    return Err("Prior episode does not authorize automatic restart".into());
                }
                if r["bindings"].get(receipt).is_none() {return Err("Unbound reset receipt".into());}
                let a:crate::automatic_start::AutomaticStart=serde_json::from_value(json(Path::new(receipt))?)?;
                if !a.valid(poleposition_telemetry::unix_ns()){return Err("Invalid or expired reset receipt".into());}
                Some(a)
            } else {None};
            let result=run_shared(path,Some(manager),&keys,&interrupt,automatic.as_ref());
            let response=match result {
                Ok((run,reason))=>{previous_run=Some(run.clone());value!({"run":run,"reason":reason})},
                Err(e)=>value!({"error":e.to_string()}),
            };
            poleposition_state::writer::finish_manifest(&session.join(format!("response-{number}.json")),&response)?;
            if response.get("error").is_some(){break "episode_error";}
            number+=1;
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    poleposition_state::writer::finish_manifest(&session.join("native-summary.json"),
        &value!({"reason":reason}))?;
    Ok((session.to_owned(),reason.into()))
}
