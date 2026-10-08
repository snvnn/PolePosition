import math
import statistics
from collections import Counter
from pathlib import Path
import torch

from ModelTrainer.common import finish, hashes, load_artifact, new_folder, rows, save, verify_hashes


def pedal(u):return max(u,0)*.20,max(-u,0)*.60


def metrics(data,predictions,baseline_u):
    if not data or len(data)!=len(predictions) or any(not math.isfinite(p) or abs(p)>1.000001 for p in predictions):
        raise ValueError('Invalid predictions/inventory')
    errors={'throttle':[],'brake':[]};groups={k:[] for k in ['throttle','brake','coast']};base={k:[] for k in groups}
    baseline=pedal(baseline_u);pred=[pedal(p) for p in predictions]
    for r,p in zip(data,pred):
        e=[abs(p[0]-r['throttle']),abs(p[1]-r['brake'])]
        errors['throttle'].append(e[0]);errors['brake'].append(e[1]);groups[r['pedal_class']].append(sum(e)/2)
        base[r['pedal_class']].append((abs(baseline[0]-r['throttle'])+abs(baseline[1]-r['brake']))/2)
    missing=[k for k,v in groups.items() if not v]
    macro=statistics.mean(statistics.mean(v) for v in groups.values() if v)
    baseline_macro=statistics.mean(statistics.mean(v) for v in base.values() if v)
    m=dict(samples=len(data),classes=dict(Counter(r['pedal_class'] for r in data)),missing_classes=missing,
        class_mae={k:statistics.mean(v) if v else None for k,v in groups.items()},macro_mae=macro,baseline_macro_mae=baseline_macro)
    for k,e in errors.items():m[k+'_mae']=statistics.mean(e);m[k+'_p95']=sorted(e)[math.ceil(.95*len(e))-1]
    m['passed']=not missing and m['throttle_mae']<=.01 and m['brake_mae']<=.02 and m['throttle_p95']<=.03 and m['brake_p95']<=.05 and macro<baseline_macro
    m['class_pedal_errors']={}
    for group in groups:
        ids=[i for i,r in enumerate(data) if r['pedal_class']==group]
        m['class_pedal_errors'][group]=dict(samples=len(ids),**{k+'_mae':statistics.mean(e[i] for i in ids) if ids else None for k,e in errors.items()})
    m['request_transmission_observation']={}
    for pedal_name in ['throttle','brake']:
        deltas=[abs(r[pedal_name]-r[pedal_name+'_transmitted']) for r in data if r.get(pedal_name+'_transmitted') is not None]
        observed=[abs(r[pedal_name]-r[pedal_name+'_observed']) for r in data]
        m['request_transmission_observation'][pedal_name]=dict(transmitted_pairs=len(deltas),requested_vs_transmitted_mae=statistics.mean(deltas) if deltas else None,
             request_vs_same_observation_mae=statistics.mean(observed),caveat='Same-source observed pedal precedes the request. Not a causal response latency.')
    delivery=[r['received_ns']-r['created_ns'] for r in data if 'received_ns' in r]
    m['delivery_ns']=dict(samples=len(delivery),mean=statistics.mean(delivery) if delivery else None,
                         p95=sorted(delivery)[math.ceil(.95*len(delivery))-1] if delivery else None)
    m['terrain']={}
    for name,check in [('uphill',lambda r:r['grade_pct']>2),('level',lambda r:abs(r['grade_pct'])<=2),('downhill',lambda r:r['grade_pct']< -2)]:
        ids=[i for i,r in enumerate(data) if check(r)]
        m['terrain'][name]=dict(samples=len(ids),throttle_mae=statistics.mean(errors['throttle'][i] for i in ids) if ids else None,
                                brake_mae=statistics.mean(errors['brake'][i] for i in ids) if ids else None)
    for name,ids in [('ramp',[i for i,r in enumerate(data) if r['target_speed_kmh']<10]),('steady_target',[i for i,r in enumerate(data) if r['target_speed_kmh']==10])]:
        m[name]=dict(samples=len(ids),throttle_mae=statistics.mean(errors['throttle'][i] for i in ids) if ids else None,
                     brake_mae=statistics.mean(errors['brake'][i] for i in ids) if ids else None)
    return m


def replay(run,dataset,split,should_stop=None,on_progress=None):
    from ModelTrainer.training import load_trained,tensors
    folder,m,norm,model=load_trained(run);dataset,dm=load_artifact(dataset,'teacher_dataset')
    if str(dataset)!=m['dataset']:raise ValueError('Training/evaluation dataset mismatch')
    if split not in ['validation','test']:raise ValueError('Evaluation split must be validation/test')
    data=rows(dataset/(split+'.jsonl'));x,_,_=tensors(data,norm)
    out=new_folder('evaluation_runs',split)
    meta=dict(schema_version=1,kind='policy_evaluation',complete=False,split=split,
              input_sha256=hashes([folder/'manifest.json',folder/'best.pt',dataset/'manifest.json',dataset/(split+'.jsonl')]))
    save(out/'manifest.json',meta)
    try:
        p=[]
        with torch.inference_mode():
            for offset in range(0,len(x),1024):
                if should_stop and should_stop():
                    finish(out,meta,False,'Evaluation stopped');return out
                p.extend(model(x[offset:offset+1024]).tolist())
                if on_progress:on_progress(dict(phase='evaluating',samples=len(p),total_samples=len(x),result_dir=str(out)))
        result=metrics(data,p,norm['baseline_u']);save(out/'metrics.json',result)
        import csv
        with (out/'predictions.csv').open('x',encoding='utf-8',newline='') as f:
            w=csv.writer(f);w.writerow(['run_id','sequence','observation_ns','target_throttle','target_brake','predicted_throttle','predicted_brake','observed_throttle','observed_brake','grade_pct','speed_kmh'])
            for r,u in zip(data,p):
                if should_stop and should_stop():
                    # Close the CSV before its hash is inventoried.
                    break
                w.writerow([r['run_id'],r['sequence'],r['observation_ns'],r['throttle'],r['brake'],*pedal(u),r['throttle_observed'],r['brake_observed'],r['grade_pct'],r['speed_kmh']])
        if should_stop and should_stop():
            finish(out,meta,False,'Evaluation stopped');return out
        from .report import evaluation_report
        evaluation_report(out,{split:result},'Recorded-observation predictions; not simulated vehicle response')
        verify_hashes(meta['input_sha256']);finish(out,meta)
    except BaseException as e:finish(out,meta,False,e);raise
    return out
