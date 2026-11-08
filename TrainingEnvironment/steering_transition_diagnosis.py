"""Matched-position diagnostics; substitutions are sensitivity tests, not rollouts."""
import json
from pathlib import Path
import numpy as np
from ModelTrainer.common import read, verify_hashes, digest
from .augmentation_experiment import predict

BASE = Path('reinforcement_runs/gamepad-imitation-1792231181059399600-43934c30')
DATA = Path('reinforcement_runs/augmentation-comparison-1792233044576944100-923b5038/dataset')
RUNS = ['gamepad-1793419197708887500-32076', 'gamepad-1793419408994112000-11348', 'gamepad-1793419625640624800-19680']


def main():
    meta=read(DATA/'manifest.json');verify_hashes(meta['output_sha256'],DATA)
    pool=read(DATA/'training-pool.json')
    teacher=[r for r in pool if 550<=r['s_m']<=950 and 0 in r['phases_ms']]
    del pool
    tx=np.array([r['features'] for r in teacher],dtype=np.float32)
    ty=np.array([r['action'] for r in teacher]);ts=np.array([r['s_m'] for r in teacher])
    tp=predict(BASE,tx);net=read(BASE/'model.json');std=np.array(net['std'])
    bins=[]
    for a,b in [(550,650),(650,680),(680,710),(710,750),(750,800),(800,850),(850,950)]:
        mask=(ts>=a)&(ts<b)
        bins.append(dict(range=[a,b],samples=int(mask.sum()),teacher_stick=float(ty[mask,0].mean()),prediction=float(tp[mask,0].mean()),steering_mse=float(np.mean((tp[mask,0]-ty[mask,0])**2))))
    runs=[]
    groups={'lateral':[4], 'heading':[5,6], 'rates':[24,25], 'lateral_and_heading':[4,5,6], 'all_lateral_state':[4,5,6,24,25], 'speed':[0,2]}
    for name in RUNS:
        p=Path('actuator_runs')/name;m=read(p/'manifest.json');verify_hashes(m['output_sha256'],p)
        rows=[json.loads(s) for s in (p/'samples.jsonl').read_text().splitlines()]
        obs={r['observation_ns']:r for r in rows if r['kind']=='observation'}
        policies=[r for r in rows if r['kind']=='policy' and r['mode']=='Running']
        points=[]
        for s in [650,670,690,710,730,750,770,790]:
            q=min(policies,key=lambda r:abs(obs[r['observation_ns']]['state']['s_m']-s))
            x=np.array(q['features'],dtype=np.float32);position=obs[q['observation_ns']]['state']['s_m']
            ids=np.flatnonzero(abs(ts-position)<2)
            median=np.median(tx[ids],axis=0)
            # Select an actual teacher observation rather than a synthetic average state.
            nearest=ids[np.argmin(np.mean(((tx[ids]-x)/std)[:,[0,4,5,6]]**2,axis=1))]
            donor=tx[nearest]; cf={}
            for key,cols in groups.items():
                z=x.copy();z[cols]=donor[cols];cf[key]=predict(BASE,z[None])[0].tolist()
            points.append(dict(s=position,live=q['action'],teacher_median=np.median(ty[ids],axis=0).tolist(),teacher_prediction_mean=tp[ids].mean(0).tolist(),matched_teacher_action=ty[nearest].tolist(),matched_teacher_prediction=tp[nearest].tolist(),substitutions=cf,
                differences=[dict(name=net['feature_names'][i],live=float(x[i]),teacher=float(donor[i]),teacher_p10=float(np.quantile(tx[ids,i],.1)),teacher_p90=float(np.quantile(tx[ids,i],.9))) for i in [0,4,5,24,25]]))
        runs.append(dict(run=name,points=points))
    result=dict(base=str(BASE),model_sha256=digest(BASE/'model.json'),teacher_pool=str(DATA),teacher_bins=bins,runs=runs,interpretation='Teacher data only. Counterfactual feature replacements are sensitivity diagnostics, not physically consistent rollouts or proof of causality.')
    dest=Path('input_profiles/gamepad/steering-transition-diagnosis-20261031.json');dest.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(bins))
    for run in runs:
        print(run['run'])
        for r in run['points']:
            print(round(r['s']), 'live',round(r['live'][0],3),'teacher',round(r['teacher_median'][0],3),'on_teacher',round(r['teacher_prediction_mean'][0],3),'replace', {k:round(v[0],3) for k,v in r['substitutions'].items()})

if __name__=='__main__':main()
