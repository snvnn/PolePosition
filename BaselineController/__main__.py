import argparse
import bisect
import math
import statistics
import time
from pathlib import Path
from ModelAndActuator.report import verify
from ModelAndActuator.setup import digest, save_json


def steering_audit(run):
    run = Path(run).resolve()
    manifest, summary, commands, telemetry, _ = verify(run)
    if manifest.get("complete") is not True or not summary.get("recording_valid") or manifest.get("scenario") != "crawl":
        raise ValueError("Requires a completed crawl recording")
    sent = [r for r in commands if r["send_success"] == "true" and r.get("applied_steering")]
    times = [int(r["now_ns"]) for r in sent]
    samples = []
    for row in telemetry:
        t = int(row["physics_ns"])
        i = bisect.bisect_right(times,t)-1
        if i < 0 or t-times[i] > 100_000_000:
            continue
        samples.append((t,float(row["position_x"]),float(row["position_z"]),float(row["speed_kmh"]),float(sent[i]["applied_steering"])))
    ts = [r[0] for r in samples]
    # Keep only unique physics-change observations; page duplicates are not extra evidence.
    samples = [r for i,r in enumerate(samples) if i == 0 or r[0] != samples[i-1][0]]
    ts = [r[0] for r in samples]
    buckets = {-1: [], 1: []}
    for i,b in enumerate(samples):
        j = bisect.bisect_right(ts,b[0]-250_000_000)-1
        k = bisect.bisect_left(ts,b[0]+250_000_000)
        if j < 0 or k >= len(samples):
            continue
        a,c = samples[j],samples[k]
        if max(b[0]-a[0],c[0]-b[0]) > 300_000_000 or min(v[3] for v in samples[j:k+1]) < 3:
            continue
        axis=b[4]
        if not .09 <= abs(axis) <= .11 or any(abs(v[4]-axis) > .005 for v in samples[j:k+1]):
            continue
        ux,uz=b[1]-a[1],b[2]-a[2];vx,vz=c[1]-b[1],c[2]-b[2]
        den=math.hypot(ux,uz)*math.hypot(vx,vz)*math.hypot(c[1]-a[1],c[2]-a[2])
        if den < .001:
            continue
        curvature=2*(ux*vz-uz*vx)/den
        if abs(curvature) < .001:
            continue
        buckets[1 if axis>0 else -1].append((curvature,axis/curvature))
    result={}
    for direction,values in buckets.items():
        if len(values)<10:
            raise ValueError(f"Insufficient steady steering observations: {direction}, {len(values)}")
        k=statistics.median(v[0] for v in values)
        gain=statistics.median(v[1] for v in values)
        if k*direction <= 0 or not 1<gain<20:
            raise ValueError("Observed steering sign/gain is inconsistent")
        result[str(direction)]={"windows":len(values),"median_signed_curvature_per_m":k,"median_axis_per_curvature_m":gain}
    folder=Path("baseline_runs")/f"steering-audit-{time.time_ns()}"
    folder.mkdir(parents=True,exist_ok=False)
    save_json(folder/"evidence.json",{"schema_version":1,"complete":True,"source":str(run),"input_sha256":{str(run/n):digest(run/n) for n in ("manifest.json","commands.csv","telemetry.csv","summary.json")},"directions":result,"method":"Signed three-point X-Z circumcircle, actual +/-250ms physics-change windows, >=3km/h, stable successful virtual axis +/-0.1. No steering_raw-to-axis conversion.","limitations":"Small-motion estimate with overlapping windows; not independent trials, wheel angle, general steering map or a closed-loop validation. Does not auto-update controller configuration."})
    return folder


def main():
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=["steering-audit"])
    p.add_argument("--run",required=True)
    args=p.parse_args()
    print(steering_audit(args.run))


if __name__ == "__main__":
    main()
