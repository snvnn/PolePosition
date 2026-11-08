"""Reproject recorded world poses and rebuild histories with the deployed Rust code."""
import bisect,json,subprocess,tempfile
from pathlib import Path
import numpy as np
from ModelTrainer.common import read
from ModelTrainer.dataset import policy_binary
from TrackModel.model import load_reference
from .continuous_reference import fast_project


def rebuild(rows,prepared,speed_file,*,end_ns=None):
    prepared=Path(prepared);pm=read(prepared/'manifest.json');ref=load_reference(pm['track_model_path']);heading=read(prepared/'heading_profile.json')
    observations=[r for r in rows if r['kind']=='observation' and (end_ns is None or r['observation_ns']<=end_ns)]
    raw=[dict(r['sample'],elapsed_ns=r['observation_ns'],segment_id=r['state']['tracking_epoch']) for r in observations]
    geom=fast_project(raw,ref['rows'],ref['manifest']['total_length_m'],heading,True)
    sent=[r for r in rows if r['kind']=='sent'];times=[r['send_end_ns'] for r in sent]
    speed=read(speed_file);cap=next(v for v in [10,20,40,80,120,160,200,240] if max(speed['speed_kmh'])<=v)
    positions={r['observation_ns']:g.tolist() for r,g in zip(observations,geom)}
    with tempfile.TemporaryDirectory(prefix='continuous-features-') as tmp:
        path=Path(tmp);frames=path/'frames.jsonl';output=path/'features.jsonl'
        with frames.open('w') as f:
            for r,g in zip(observations,geom):
                t=r['observation_ns'];s=r['state'];a=r['sample'];j=bisect.bisect_left(times,t)-1
                if s['status']!='ready' or j<0 or t-times[j]>=100_000_000 or not 1<=a['gear_raw']<=9:continue
                axes=sent[j]['axes'];target=float(np.interp(g[0],speed['s_m'],speed['speed_kmh'],period=speed['length_m']))
                entry=dict(frame=dict(sequence=r['sequence'],observation_ns=t,tracking_epoch=s['tracking_epoch'],ready=True,
                    speed_kmh=a['speed_kmh'],target_speed_kmh=target,rpm=a['rpm'],lateral_error_m=g[1],heading_error_rad=g[2],s_m=g[0]),
                    speed_cap_kmh=cap,previous_axes=[axes[k] for k in ['steering','throttle','brake']],previous_applied_ns=times[j],gear_raw=a['gear_raw'])
                f.write(json.dumps(entry)+'\n')
        subprocess.run([str(policy_binary()),'features-gamepad',str(prepared),str(frames),str(output),str(speed_file)],check=True)
        features={r['observation_ns']:r['features'] for r in (json.loads(x) for x in output.read_text().splitlines()) if r['features'] is not None}
    return features,positions
