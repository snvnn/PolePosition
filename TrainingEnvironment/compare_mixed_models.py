"""Offline held-out lap and start comparison; never changes deployed models."""
import json
from pathlib import Path
import numpy as np
from ModelTrainer.common import read,verify_hashes

def predict(folder,x):
    m=read(Path(folder)/'model.json')
    a=(x-np.asarray(m['mean'],dtype=np.float32))/np.asarray(m['std'],dtype=np.float32)
    for i,l in enumerate(m['layers']):
        a=a@np.asarray(l['weight'],dtype=np.float32).T+np.asarray(l['bias'],dtype=np.float32)
        if i<2:a=np.maximum(a,0)
    pred=np.column_stack([np.tanh(a[:,0]),1/(1+np.exp(-np.clip(a[:,1],-80,80))),1/(1+np.exp(-np.clip(a[:,2],-80,80)))])
    if not np.isfinite(pred).all():raise ValueError('Nonfinite prediction')
    return pred

def evaluate(candidate):
    models={'A':'reinforcement_runs/gamepad-imitation-1791712462218386600-1be3fa19','laps_only':'reinforcement_runs/gamepad-imitation-1791718728324314300-3b36589d','mixed':str(candidate)}
    for folder in models.values():
        manifest=read(Path(folder)/'manifest.json');verify_hashes(manifest['output_sha256'],folder)
    lap=read(Path(candidate)/'validation-examples.json')
    oldlap=read(Path(models['laps_only'])/'validation-examples.json')
    if lap!=oldlap:raise ValueError('Held-out lap changed')
    hotlap=read(Path(models['A'])/'validation-examples.json')
    train=read(Path(candidate)/'training-examples.json')
    keys=lambda rows:{(r['recording'],r['lap_index']) for r in rows if 'recording' in r}
    if keys(train)&keys(lap):raise ValueError('Validation leakage')
    report={'models':models,'live_performance_verified':False,'train_examples':len(train),'train_below_20kmh':sum(r['features'][0]<20 for r in train),'datasets':{}}
    for name,rows in [('heldout_lap',lap),('heldout_hotlap',hotlap),('heldout_hotlap_under_50kmh',[r for r in hotlap if r['features'][0]<50])]:
        x=np.asarray([r['features'] for r in rows],dtype=np.float32);y=np.asarray([r['action'] for r in rows],dtype=np.float32)
        report['datasets'][name]={'samples':len(rows),'mse':{n:np.mean((predict(folder,x)-y)**2,axis=0).tolist() for n,folder in models.items()}}
    run=Path('actuator_runs/gamepad-1791726688164895200-35484')
    live=[json.loads(line) for line in (run/'samples.jsonl').read_text().splitlines()]
    obs={r['observation_ns']:r for r in live if r['kind']=='observation'}
    policies=[r for r in live if r['kind']=='policy' and r['mode']=='Running']
    replay=[]
    for position in [3111,3320,3340,3360]:
        policy=min(policies,key=lambda r:abs(obs[r['observation_ns']]['state']['s_m']-position))
        state=obs[policy['observation_ns']]['state'];x=np.asarray([policy['features']],dtype=np.float32)
        replay.append({'s_m':state['s_m'],'speed_kmh':state['speed_kmh'],'actions':{n:predict(folder,x)[0].tolist() for n,folder in models.items()}})
    report['fixed_observation_replay_not_closed_loop']=replay
    return report
