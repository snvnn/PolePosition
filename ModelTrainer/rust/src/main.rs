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
    if args.len() != 4 {
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
