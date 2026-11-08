"""Compare on fixed held-out teacher observations and frozen live observations."""
import csv,json
from pathlib import Path
import numpy as np
from ModelTrainer.common import read,verify_hashes
from .augmentation_experiment import predict
from .steering_transition_diagnosis import BASE,RUNS

def evaluate(models):
    for folder in models.values():
        m=read(Path(folder)/'manifest.json');verify_hashes(m['output_sha256'],folder)
    validation=read(BASE/'validation-examples.json')
    x=np.array([r['features'] for r in validation],dtype=np.float32);y=np.array([r['action'] for r in validation])
    # Project only the generous surrounding interval; do not join its separated passes.
    from TrackModel.model import load_reference
    from .human_imitation import project
    prep=Path(read(BASE/'manifest.json')['prepared']);pm=read(prep/'manifest.json')
    ref=load_reference(pm['track_model_path']);heading=read(prep/'heading_profile.json')
    recording=Path(validation[0]['recording'])
    with (recording/'telemetry.csv').open(newline='') as f:
        raw=[r for r in csv.DictReader(f) if .12<float(r['progress'])<.27]
    chunks=[]
    for r in raw:
        if not chunks or int(r['sequence'])!=int(chunks[-1][-1]['sequence'])+1:chunks.append([])
        chunks[-1].append(r)
    geometry={}
    for chunk in chunks:
        g=project(chunk,ref['rows'],ref['manifest']['total_length_m'],heading,allow_finish_seam=True)
        geometry.update((int(r['sequence']),float(v[0])) for r,v in zip(chunk,g))
    ss=np.array([geometry.get(r['sequence'],-1) for r in validation])
    groups={'whole_validation':np.ones(len(x),bool),'corner_550_950':(ss>=550)&(ss<=950),'transition_650_750':(ss>=650)&(ss<=750),'braking_750_900':(ss>=750)&(ss<=900)}
    metrics={};predictions={};feedback_x=None
    if any(read(Path(f)/'manifest.json').get('observation_variant')=='acc_steering_feedback_v1' for f in models.values()):
        import copy
        from .steering_feedback import attach_feedback
        feedback_rows=copy.deepcopy(validation);attach_feedback(feedback_rows)
        feedback_x=np.array([r['features'] for r in feedback_rows],dtype=np.float32)
    for name,model in models.items():
        use_feedback=read(Path(model)/'manifest.json').get('observation_variant')=='acc_steering_feedback_v1'
        pred=predict(model,feedback_x if use_feedback else x);predictions[name]=pred
        metrics[name]={k:np.mean((pred[v]-y[v])**2,axis=0).tolist() for k,v in groups.items()}
    # First sustained left command, >=0.15 seconds with no sample gap above 50ms.
    def transition(s,t,stick):
        start=None;previous=None
        for pos,time,a in zip(s,t,stick):
            if not 650<=pos<=770 or a>=-.2 or (previous is not None and time-previous>.05):start=None
            if 650<=pos<=770 and a<-.2:
                if start is None:start=(pos,time)
                if time-start[1]>=.15:return float(start[0])
            previous=time
        return None
    teacher_passes=[]
    for chunk in chunks:
        ids=[i for i,r in enumerate(validation) if int(chunk[0]['sequence'])<=r['sequence']<=int(chunk[-1]['sequence'])]
        t=np.array([validation[i]['observation_ns']/1e9 for i in ids])
        teacher_passes.append(dict(teacher_s=transition(ss[ids],t,y[ids,0]),predicted_s={n:transition(ss[ids],t,p[ids,0]) for n,p in predictions.items()}))
    live=[]
    for name in RUNS:
        rows=[json.loads(s) for s in (Path('actuator_runs')/name/'samples.jsonl').read_text().splitlines()]
        obs={r['observation_ns']:r for r in rows if r['kind']=='observation'}
        ps=[r for r in rows if r['kind']=='policy' and r['mode']=='Running']
        lx=np.array([r['features'] for r in ps],dtype=np.float32)
        s=np.array([obs[r['observation_ns']]['state']['s_m'] for r in ps]);t=np.array([r['observation_ns']/1e9 for r in ps])
        inputs={}
        for n,f in models.items():
            inputs[n]=lx.copy()
            if read(Path(f)/'manifest.json').get('observation_variant')=='acc_steering_feedback_v1':
                inputs[n][:,3]=[obs[r['observation_ns']]['sample']['steering_raw'] for r in ps]
        pr={n:predict(f,inputs[n]) for n,f in models.items()}
        points=[]
        for pos in [670,690,710,730,750,790,820]:
            i=int(abs(s-pos).argmin())
            points.append(dict(s=float(s[i]),predictions={n:p[i].tolist() for n,p in pr.items()}))
        region=(s>=650)&(s<=750)
        sensitivity={}
        for n,f in models.items():
            plus=inputs[n][region].copy();minus=inputs[n][region].copy();plus[:,25]+=.05;minus[:,25]-=.05
            sensitivity[n]=float(np.mean(abs(predict(f,plus)[:,0]-predict(f,minus)[:,0])))
        live.append(dict(run=name,sustained_left_s={n:transition(s,t,p[:,0]) for n,p in pr.items()},points=points,
            steering_span_for_heading_rate_plus_minus_005=sensitivity))
    return dict(models={n:str(p) for n,p in models.items()},metrics=metrics,validation_counts={k:int(v.sum()) for k,v in groups.items()},teacher_passes=teacher_passes,
        frozen_live_observation_diagnostics=live,live_performance_verified=False,limitation='Reused validation session; fixed observation predictions are not new trajectories. Altered steering changes future states.')

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('candidate');p.add_argument('--output',required=True);a=p.parse_args()
    result=evaluate({'existing':BASE,'candidate':Path(a.candidate)})
    Path(a.output).write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result['metrics']))
    print('teacher transitions',json.dumps(result['teacher_passes']))
    print('live transitions',json.dumps([{'run':r['run'],'transition':r['sustained_left_s']} for r in result['frozen_live_observation_diagnostics']]))
