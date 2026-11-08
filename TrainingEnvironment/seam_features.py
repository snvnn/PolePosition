"""Rebuild causal gamepad features from immutable 100 Hz observations/sent reports."""
import bisect,json,subprocess,tempfile
from pathlib import Path
import numpy as np
from ModelTrainer.common import read
from ModelTrainer.dataset import policy_binary

def rebuild(run,prepared,speed_file):
    rows=[json.loads(x) for x in (Path(run)/'samples.jsonl').read_text().splitlines()]
    sent=[r for r in rows if r['kind']=='sent'];times=[r['send_end_ns'] for r in sent]
    speed=read(speed_file);cap=next(v for v in [10,20,40,80,120,160,200,240] if max(speed['speed_kmh'])<=v)
    with tempfile.TemporaryDirectory(prefix='poleposition-seam-') as tmp:
        path=Path(tmp);frames=path/'frames.jsonl';output=path/'features.jsonl'
        with frames.open('w',encoding='utf-8') as f:
            for r in rows:
                if r['kind']!='observation':continue
                t=r['observation_ns'];s=r['state'];a=r['sample'];j=bisect.bisect_left(times,t)-1
                if s['status']!='ready' or j<0 or t-times[j]>=100_000_000 or not 1<=a['gear_raw']<=9:continue
                axes=sent[j]['axes'];target=float(np.interp(s['s_m'],speed['s_m'],speed['speed_kmh'],period=speed['length_m']))
                entry=dict(frame=dict(sequence=r['sequence'],observation_ns=t,tracking_epoch=s['tracking_epoch'],ready=True,speed_kmh=a['speed_kmh'],target_speed_kmh=target,rpm=a['rpm'],lateral_error_m=s['lateral_error_m'],heading_error_rad=s['heading_error_rad'],s_m=s['s_m']),speed_cap_kmh=cap,previous_axes=[axes[k] for k in ['steering','throttle','brake']],previous_applied_ns=times[j],gear_raw=a['gear_raw'])
                f.write(json.dumps(entry)+'\n')
        subprocess.run([str(policy_binary()),'features-gamepad-seam',str(prepared),str(frames),str(output),str(speed_file)],check=True)
        return {r['observation_ns']:r['features'] for r in (json.loads(x) for x in output.read_text().splitlines()) if r['features'] is not None}
