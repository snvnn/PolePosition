"""Compare models on the same observations using each model's own reference features."""
import json,subprocess
from pathlib import Path
import numpy as np
from ModelTrainer.common import read,save,new_folder,hashes,finish,verify_hashes
from ModelTrainer.dataset import policy_binary
from .augmentation_experiment import predict

BASE=Path('reinforcement_runs/gamepad-imitation-1792231181059399600-43934c30')
PREVIOUS=Path('reinforcement_runs/gamepad-imitation-1793423783910919400-7e63824b')


def evaluate(candidate,experiment):
    candidate=Path(candidate);experiment=Path(experiment)
    models={'original':BASE,'previous_augmented_two_laps':PREVIOUS,'continuous_reference':candidate}
    for model in models.values():
        m=read(model/'manifest.json')
        if not m['complete']:raise ValueError('Incomplete comparison model')
        verify_hashes(m['output_sha256'],model)
    old=read(BASE/'validation-examples.json');new=read(candidate/'validation-examples.json')
    if len(old)!=len(new):raise ValueError('Validation sample set changed')
    for a,b in zip(old,new):
        if any(a[k]!=b[k] for k in ('recording','sequence','observation_ns','action')):raise ValueError('Validation labels/alignment changed')
    xold=np.array([r['features'] for r in old],dtype=np.float32);xnew=np.array([r['features'] for r in new],dtype=np.float32);y=np.array([r['action'] for r in new])
    s=np.array([r['s_m'] for r in new]);length=read(candidate/'speed-profile.json')['length_m'];q=(s+length/2)%length-length/2
    groups={'whole_validation':np.ones(len(y),bool),'finish_line_100m':abs(q)<=100,'seam_15m':abs(q)<=15,
            'problem_corner_550_950':(s>=550)&(s<=950),'braking':y[:,2]>=.2}
    predictions={label:predict(model,xnew if label=='continuous_reference' else xold) for label,model in models.items()}
    metrics={label:{g:((pred[mask]-y[mask])**2).mean(0).tolist() for g,mask in groups.items()} for label,pred in predictions.items()}
    dt=np.diff([r['observation_ns']/1e9 for r in new]);valid=(dt>0)&(dt<.05)
    rate_metrics={label:float(np.mean(abs((np.diff(pred[:,0])-np.diff(y[:,0]))[valid]/dt[valid]))) for label,pred in predictions.items()}
    successes=read(candidate/'successful-examples.json');sx=np.array([r['features'] for r in successes],dtype=np.float32)
    rebuilt=read(experiment/'latest-rebuilt-features.json');source=Path(rebuilt['source'])
    with (source/'samples.jsonl').open() as f:policies=[r for line in f if (r:=json.loads(line))['kind']=='policy' and r['mode']=='Running']
    latest=np.array([rebuilt['features'][str(p['observation_ns'])] for p in policies],dtype=np.float32)
    probes=np.concatenate([xnew,sx,latest]);out=new_folder('reinforcement_runs','continuous-reference-evaluation')
    with (out/'parity-input.jsonl').open('x') as f:
        for row in probes:f.write(json.dumps(row.tolist())+'\n')
    subprocess.run([str(policy_binary()),'predict-gamepad',str(candidate/'model.json'),str(out/'parity-input.jsonl'),str(out/'parity-output.jsonl')],check=True)
    actual=np.array([json.loads(s) for s in (out/'parity-output.jsonl').read_text().splitlines()]);expected=predict(candidate,probes)
    error=float(abs(actual-expected).max())
    if not np.isfinite(actual).all() or error>1e-5:raise ValueError('Expanded prediction parity failed')
    report=dict(models={k:str(v) for k,v in models.items()},metrics=metrics,groups={k:int(v.sum()) for k,v in groups.items()},
        steering_rate_difference_mae=rate_metrics,validation_same_observations_and_actions=True,
        validation_scope='Existing held-out session, reused for selection; each model uses its matching geometry and speed features',
        parity=dict(samples=len(probes),maximum_absolute_error=error,passed=True),native_geometry=read(experiment/'native-geometry-parity.json'),
        seam_feature_replay=read(experiment/'seam-feature-replay.json'),live_performance_verified=False,
        limitation='Offline error and feature continuity do not demonstrate stable autonomous driving; no GUI activation or F7.')
    save(out/'comparison.json',report)
    finish(out,dict(kind='continuous_reference_evaluation',input_sha256=hashes([p/n for p in models.values() for n in ('manifest.json','model.json')]+[Path(__file__),experiment/'native-geometry-parity.json',experiment/'seam-feature-replay.json'])))
    print('REPORT',out,flush=True);print(json.dumps(metrics),flush=True);print('PARITY',report['parity'],flush=True)


if __name__=='__main__':
    import sys
    evaluate(sys.argv[1],sys.argv[2])
