"""Build immutable augmented dataset, train A/B/C, and compare frozen validation."""
import csv
import json
from pathlib import Path
import tomllib
import numpy as np
from ModelTrainer.common import read,save,new_folder,finish,hashes,verify_hashes

BASE=Path('reinforcement_runs/gamepad-imitation-1792231181059399600-43934c30')
RUNS=['gamepad-1792231570855084600-9996','gamepad-1792231710120101700-30252']
AUG=dict(exclude_before_s=2.,exclude_after_s=1.,brake_threshold=.2,curvature_threshold=.003,important_weight=2.,offsets_ms=[0,10])
NOISE=dict(probability=.5,lateral_std_m=.02,lateral_clip_m=.06,heading_std_rad=.002,heading_clip_rad=.006)


def write_config(path,c):
    lines=[];tables=[]
    for k,v in c.items():
        if isinstance(v,dict): tables.append((k,v))
        else:lines.append(f'{k} = {json.dumps(v)}')
    for k,v in tables:
        lines.append(f'\n[{k}]')
        lines.extend(f'{key} = {json.dumps(val)}' for key,val in v.items())
    path.write_text('\n'.join(lines)+'\n',encoding='utf-8')


def predict(model,x):
    import torch
    torch.set_num_threads(1)
    n=read(Path(model)/'model.json');result=[]
    weights=[(torch.tensor(l['weight']),torch.tensor(l['bias'])) for l in n['layers']]
    with torch.inference_mode():
        for start in range(0,len(x),4096):
            z=(torch.tensor(x[start:start+4096])-torch.tensor(n['mean']))/torch.tensor(n['std'])
            for i,(w,b) in enumerate(weights):
                z=z@w.T+b
                if i<2:z=z.relu()
            result.append(torch.cat((z[:,:1].tanh(),z[:,1:].sigmoid()),1).numpy())
    return np.concatenate(result)


def evaluate(folder,models,dataset):
    validation=read(dataset/'validation.json');x=np.array([r['features'] for r in validation],dtype=np.float32);y=np.array([r['action'] for r in validation])
    t=np.array([r['observation_ns'] for r in validation])/1e9;dt=np.diff(t)
    continuous=(dt>0)&(dt<.1)
    corner=np.max(np.abs(x[:,8:12]),axis=1)>=.003;brake=y[:,2]>=.2
    groups={'all':np.ones(len(x),dtype=bool),'braking':brake,'corner':corner,'straight':~corner}
    results={}
    for name,model in models.items():
        pred=predict(model,x) if name!='persistence' else x[:,19:22]
        rates=np.abs(np.diff(pred[:,0])[continuous]/dt[continuous]);teacher_rates=np.diff(y[:,0])[continuous]/dt[continuous]
        results[name]=dict(model=str(model),mse={k:((pred[mask]-y[mask])**2).mean(0).tolist() for k,mask in groups.items()},
            steering_rate_abs_mean=float(rates.mean()),steering_rate_abs_p95=float(np.percentile(rates,95)),
            steering_rate_difference_mae=float(np.abs(np.diff(pred[:,0])[continuous]/dt[continuous]-teacher_rates).mean()))
    # Frozen failed observations are diagnostics, never synthetic rollouts or labels.
    replay={}
    for run in RUNS:
        rows=[json.loads(l) for l in (Path('actuator_runs')/run/'samples.jsonl').read_text().splitlines()]
        p=[r for r in rows if r['kind']=='policy' and r['mode']=='Running'];origin=p[0]['now_ns']
        p=[r for r in p if 9<=(r['now_ns']-origin)/1e9<=13]
        f=np.array([r['features'] for r in p],dtype=np.float32)
        replay[run]={'times':[(r['now_ns']-origin)/1e9 for r in p], 'predictions':{k:predict(v,f).tolist() for k,v in models.items() if k!='persistence'},'interpretation':'fixed observations, not closed-loop driving'}
    ordered=sorted(['A','B','C'],key=lambda k:(sum(results[k]['mse']['all'])/3,k))
    best=ordered[0];better=sum(results[best]['mse']['all'])<sum(results['existing']['mse']['all'])
    report=dict(metrics=results,group_counts={k:int(v.sum()) for k,v in groups.items()},rank=ordered,
        selected_candidate=best if better else None,live_performance_verified=False,validation_scope='reused selection set, not final test',fixed_observation_replay=replay)
    save(folder/'comparison.json',report)
    return report


def main():
    from .session_gamepad import prepare
    from .gamepad import train
    from GamepadIO.run_report import report
    out=new_folder('reinforcement_runs','augmentation-comparison');print('EXPERIMENT',out,flush=True)
    # First diagnose actual trajectories against the correct held-out teacher session.
    for name in RUNS:
        r=report(Path('actuator_runs')/name,replace=True,render_plot=False)
        save(out/(name+'-teacher-comparison.json'),r)
        print('Teacher comparison complete:',name,flush=True)
    dataset=out/'dataset';dataset.mkdir()
    c=tomllib.loads(Path('TrainingEnvironment/session-teacher-20261017.toml').read_text())
    build=dict(c,augmentation=AUG,audit_dir=str(dataset/'audit'))
    build_config=out/'dataset.toml';write_config(build_config,build)
    training,validation,names,profiles,inventory,manifest=prepare(build,build_config)
    # Validation must remain byte-for-byte equivalent at the example level.
    old=read(BASE/'validation-examples.json')
    assert validation==old,'Validation changed during augmentation'
    assert len({(r['recording'],r['sequence']) for r in training})==len(training)
    for r in training:
        assert r['previous_input_end_ns']<r['observation_ns']<=r['action_read_start_ns']<=r['action_read_end_ns']<=r['observation_ns']+20_000_000
        assert r['previous_input_end_ns']>=r['context_start_ns']
    a=[r for r in training if 0 in r['phases_ms']]
    raw=np.array([r['features'] for r in a],dtype=np.float32);mean=raw.mean(0);std=raw.std(0);std[std<1e-6]=1
    save(dataset/'normalization.json',dict(mean=mean.tolist(),std=std.tolist(),source='A original curated examples',count=len(a)))
    save(dataset/'training-pool.json',training);save(dataset/'validation.json',validation)
    inventory.update(hashes([Path(__file__)]))
    meta=dict(input_sha256=inventory,feature_names=names,profiles=profiles,model_manifest=manifest,augmentation=AUG,
        a_samples=len(a),b_samples=len(training),validation_samples=len(validation))
    finish(dataset,meta)
    draws=len(a);del training,validation,a,raw
    models={'existing':BASE}
    for variant in ['A','B','C']:
        config=dict(c,augmentation_dataset=str(dataset),variant=variant,shared_normalization=str(dataset/'normalization.json'),weighted_sampling=True,epoch_samples=draws)
        if variant=='C':config['noise']=NOISE
        path=out/(variant+'.toml');write_config(path,config)
        model=train(path,on_progress=lambda x:print(json.dumps(dict(variant=variant,**x)),flush=True))
        models[variant]=model
        save(out/(variant+'-model.json'),dict(path=str(model)))
        import gc;gc.collect()
    summaries=[read(models[k]/'summary.json') for k in ['A','B','C']]
    assert len({s['optimizer_updates'] for s in summaries})==1
    assert len({s['initial_weights_sha256'] for s in summaries})==1
    nets=[read(models[k]/'model.json') for k in ['A','B','C']]
    assert all(n['mean']==nets[0]['mean'] and n['std']==nets[0]['std'] for n in nets)
    for k in models:
        m=read(models[k]/'manifest.json');verify_hashes(m['output_sha256'],models[k])
    comparison=evaluate(out,dict(models,persistence=None),dataset)
    template=Path('GamepadIO/session-model-test-5s.toml').read_text()
    presets=[]
    for name,model in models.items():
        for seconds in (5,15,30):
            config_path=Path('GamepadIO')/f'augmentation-{name}-{seconds}s.toml'
            text=template.replace(str(BASE).replace('\\','/'),str(model).replace('\\','/')).replace('seconds = 5',f'seconds = {seconds}')
            config_path.write_text(text,encoding='utf-8')
            preset=Path('TrainingController')/f'augmentation-{name}-{seconds}s-preset.json'
            save(preset,{'task':'게임패드 교정 주행','config':str(config_path.resolve())},replace=preset.exists())
            presets.append(str(preset))
    save(out/'result.json',dict(models={k:str(v) for k,v in models.items()},selected_candidate=comparison['selected_candidate'],presets=presets,live_tests_pending=True))
    print('COMPLETED',out,flush=True)

if __name__=='__main__': main()
