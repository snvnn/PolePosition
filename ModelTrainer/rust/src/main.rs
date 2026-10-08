use poleposition_policy::{Features, Frame, load};
use poleposition_state::model::{Prepared, Result};
use std::{
    fs::{File, OpenOptions},
    io::{BufRead, BufReader, Write},
    path::Path,
};
fn main() {
    if let Err(e) = run() {
        eprintln!("Policy: {e}");
        std::process::exit(1);
    }
}
fn run() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.as_slice()==["gamepad-schema"] {
        println!("{}",serde_json::json!({"semantics":"gamepad_v1","feature_names":poleposition_policy::gamepad::names(),"action_names":["stick_x","rt","lt"]}));return Ok(());
    }
    if args.len() != 4 && !(args.len() == 5 && matches!(args[0].as_str(),"features-v2"|"features-gamepad")) {
        return Err("Usage: poleposition-policy features PREPARED INPUT.jsonl OUTPUT.jsonl | predict MODEL INPUT.jsonl OUTPUT.jsonl".into());
    }
    let input = BufReader::new(File::open(&args[2])?);
    let mut output = std::io::BufWriter::new(
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&args[3])?,
    );
    match args[0].as_str() {
        "predict-gamepad" => {
            let n:poleposition_policy::gamepad::Network=serde_json::from_slice(&std::fs::read(&args[1])?)?;
            n.validate()?;
            for line in input.lines(){
                let v:Vec<f32>=serde_json::from_str(&line?)?;
                serde_json::to_writer(&mut output,&n.predict(&v)?)?;writeln!(output)?;
            }
        }
        "predict-sac" => {
            let network: poleposition_policy::v2::SacNetwork =
                serde_json::from_slice(&std::fs::read(&args[1])?)?;
            network.validate()?;
            for line in input.lines() {
                let row: serde_json::Value = serde_json::from_str(&line?)?;
                let features: Vec<f32> = serde_json::from_value(row["features"].clone())?;
                let epsilon: [f32; 2] = serde_json::from_value(row["epsilon"].clone())?;
                serde_json::to_writer(&mut output, &network.predict(&features, epsilon)?)?;
                writeln!(output)?;
            }
        }
        "features-v2" | "features-gamepad" => {
            use poleposition_policy::features_v2::{FeaturesV2, Input, SpeedProfile, names};
            let p = Prepared::load(Path::new(&args[1]))?;
            let speed: SpeedProfile =
                serde_json::from_value(poleposition_state::model::json(Path::new(&args[4]))?)?;
            speed.validate(&p.model)?;
            let gamepad=args[0]=="features-gamepad";
            let mut features = if gamepad {FeaturesV2::gamepad()} else {FeaturesV2::default()};
            for line in input.lines() {
                let input: Input = serde_json::from_str(&line?)?;
                let values = features.update(&input, &p.model, &speed)?;
                serde_json::to_writer(
                    &mut output,
                    &serde_json::json!({
                        "sequence":input.frame.sequence,"tracking_epoch":input.frame.tracking_epoch,
                        "observation_ns":input.frame.observation_ns,"feature_names":if gamepad {poleposition_policy::gamepad::names()} else {names()},"features":values
                    }),
                )?;
                writeln!(output)?;
            }
            p.verify()?;
        }
        "predict-v2-candidate" => {
            let folder = Path::new(&args[1]);
            let m = poleposition_state::model::json(&folder.join("manifest.json"))?;
            if m["kind"] != "sac_mean_candidate"
                || m["complete"] != false
                || m["eligible_for_live"] != false
                || m["model_sha256"] != poleposition_state::model::hash(&folder.join("model.json"))?
            {
                return Err("Invalid v2 parity candidate".into());
            }
            let network: poleposition_policy::v2::Network = serde_json::from_value(
                poleposition_state::model::json(&folder.join("model.json"))?,
            )?;
            network.validate()?;
            for line in input.lines() {
                let values: Vec<f32> = serde_json::from_str(&line?)?;
                serde_json::to_writer(&mut output, &network.predict(&values)?)?;
                writeln!(output)?;
            }
        }
        "features" => {
            let p = Prepared::load(Path::new(&args[1]))?;
            let mut f = Features::default();
            for line in input.lines() {
                let frame: Frame = serde_json::from_str(&line?)?;
                let v = f.update(&frame, &p.model)?;
                serde_json::to_writer(
                    &mut output,
                    &serde_json::json!({"sequence":frame.sequence,"tracking_epoch":frame.tracking_epoch,"observation_ns":frame.observation_ns,"features":v}),
                )?;
                writeln!(output)?;
            }
            p.verify()?;
        }
        "predict" | "predict-candidate" => {
            let n = if args[0] == "predict-candidate" {
                let folder = Path::new(&args[1]);
                let m = poleposition_state::model::json(&folder.join("manifest.json"))?;
                if m["kind"] != "policy_export"
                    || m["complete"] != false
                    || m["eligible_for_live"] != false
                    || m["model_sha256"]
                        != poleposition_state::model::hash(&folder.join("model.json"))?
                {
                    return Err("Invalid incomplete parity candidate".into());
                }
                let n: poleposition_policy::Network = serde_json::from_value(
                    poleposition_state::model::json(&folder.join("model.json"))?,
                )?;
                n.validate()?;
                n
            } else {
                load(Path::new(&args[1]), false)?
            };
            for line in input.lines() {
                let x: [f32; 19] = serde_json::from_str(&line?)?;
                writeln!(output, "{}", n.predict(&x)?)?;
            }
        }
        _ => return Err("Unknown policy command".into()),
    }
    output.flush()?;
    output.get_ref().sync_all()?;
    Ok(())
}
