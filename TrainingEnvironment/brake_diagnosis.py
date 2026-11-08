"""Audit supervision and isolate response-feature dependence on a recorded failure."""
import json
from pathlib import Path
import numpy as np
from .gamepad import read_inputs,align
from .compare_mixed_models import predict
from ModelTrainer.common import read
import csv

def analyze():
    model=Path('reinforcement_runs/gamepad-imitation-1791726972411867800-d63bca8c')
    train=read(model/'training-examples.json');net=read(model/'model.json')
    by_record={}
    for e in train:
        if 'recording' in e:by_record.setdefault(e['recording'],[]).append(e)
    max_error=0;checked=0;max_lag=0
    for recording,examples in by_record.items():
        with (Path(recording)/'telemetry.csv').open(newline='') as f:rows=list(csv.DictReader(f))
        labels={int(r['sequence']):(a['stick_x'],a['throttle'],a['brake'],a['read_end_ns']-int(r['elapsed_ns'])) for r,_,a in align(rows,read_inputs(Path(recording)/'inputs.csv'))}
        for e in examples:
            target=labels[e['sequence']];max_error=max(max_error,max(abs(a-b) for a,b in zip(e['action'],target[:3])));max_lag=max(max_lag,target[3]);checked+=1
    old=read('reinforcement_runs/gamepad-imitation-1791712462218386600-1be3fa19/training-examples.json')
    hot=[e for e in train if e.get('source_kind')=='hotlap_teacher']
    assert len(hot)==len(old) and all(a['action']==b['action'] and a['features']==b['features'] for a,b in zip(hot,old))
    x=np.asarray([e['features'] for e in train],dtype=np.float32);y=np.asarray([e['action'] for e in train]);br=y[:,2]>.5;pred=predict(model,x)
    run=Path('actuator_runs/gamepad-1791727152479321900-22156');rows=[json.loads(l) for l in (run/'samples.jsonl').read_text().splitlines()];obs={r['observation_ns']:r for r in rows if r['kind']=='observation'};ps=[r for r in rows if r['kind']=='policy' and r['mode']=='Running']
    live=min(ps,key=lambda r:abs(obs[r['observation_ns']]['state']['s_m']-3340));lx=np.asarray(live['features'],dtype=np.float32);std=np.asarray(net['std'])
    indexes=[0,4,5,6,8,9,10,11,12,26,27,28,29,30,31,32,33,34]
    dist=np.mean(((x-lx)/std)[:,indexes]**2,axis=1);i=np.where(br)[0][np.argmin(dist[br])];teacher=x[i]
    cf=lx.copy();cf[23]=teacher[23]
    return {'supervision_audit':{'lap_labels_checked':checked,'hotlap_labels_checked':len(hot),'max_label_error':max_error,'max_lap_label_delay_ms':max_lag/1e6},'strong_brake_labels':int(sum(br)),'training_strong_brake_recall_at_half':float(np.mean(pred[br,2]>.5)),'nearby_braking_example':{'teacher_action':y[i].tolist(),'teacher_prediction':pred[i].tolist(),'live_prediction':predict(model,lx[None])[0].tolist(),'speed_rate_only_replacement':predict(model,cf[None])[0].tolist(),'teacher_speed_rate':float(teacher[23]),'live_speed_rate':float(lx[23]),'feature_differences':[{'name':net['feature_names'][j],'live':float(lx[j]),'teacher':float(teacher[j]),'standardized_difference':float((lx[j]-teacher[j])/std[j])} for j in np.argsort(abs((lx-teacher)/std))[::-1]]},'interpretation':'Causal response measurements provide an action shortcut; this is not future-label timestamp leakage. Single-feature replacement is diagnostic, not a physically consistent simulated rollout.'}
if __name__=='__main__':
    result=analyze();Path('input_profiles/gamepad/brake-diagnosis.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8');print(json.dumps({k:v for k,v in result.items() if k!='nearby_braking_example'},indent=2))
