"""Package compatible actors and compare on matching held-out observations."""
import json
import shutil
from pathlib import Path
import numpy as np
import torch
from ModelTrainer.common import read,save,hashes,finish,new_folder,verify_hashes,digest
from .augmentation_experiment import predict
from .outcome_learning import scalar_network
from .verify_outcome_dataset import verify


def evaluate(training):
    training=Path(training);tm=read(training/'manifest.json');verify_hashes(tm['output_sha256'],training)
    if not tm['complete']:raise ValueError('Training still incomplete')
    c=read(training/'config.json');models=read(training/'models.json')
    dataset=Path(read(Path(models['B'])/'manifest.json')['outcome_dataset']);check=verify(dataset)
    validation=read(dataset/'teacher-validation.json');corrections=read(dataset/'correction-validation.json')
    old=Path(c['original_model']);oldm=read(old/'manifest.json');verify_hashes(oldm['output_sha256'],old)
    oldrows=read(old/'validation-examples.json');lookup={(r['recording'],r['sequence'],r['observation_ns']):r for r in oldrows}
    aligned=[lookup[(r['recording'],r['sequence'],r['observation_ns'])] for r in validation]
    if any(a['action']!=b['action'] for a,b in zip(aligned,validation)):raise ValueError('Validation action labels changed')
    x=np.array([r['features'] for r in validation],dtype=np.float32);oldx=np.array([r['features'] for r in aligned],dtype=np.float32);y=np.array([r['action'] for r in validation])
    cx=np.array([r['features'] for r in corrections],dtype=np.float32);cy=np.array([r['action'] for r in corrections]);cm=np.array([r['mask'] for r in corrections])
    s=np.array([r['s_m'] for r in validation]);length=read(Path(c['source_model'])/'speed-profile.json')['length_m'];q=(s+length/2)%length-length/2
    groups=dict(whole=np.ones(len(s),bool),finish_100m=abs(q)<=100,seam_15m=abs(q)<=15,problem_corner=(s>=550)&(s<=950),braking=y[:,2]>=.2)
    out=new_folder(c['output_dir'],'outcome-evaluation');save(out/'dataset-verification.json',check)
    packaged={};summaries={};checkpoint_checks={}
    for label,path in models.items():
        path=Path(path);m=read(path/'manifest.json');verify_hashes(m['output_sha256'],path)
        summary=read(path/'summary.json');summaries[label]=summary
        if not m['complete'] or not m['parity_passed'] or summary['parity_max_error']>1e-5:raise ValueError('Candidate export not verified')
        if label=='B':
            checkpoint=torch.load(path/'checkpoint.pt',weights_only=False,map_location='cpu');critic=torch.load(training/'critic.pt',weights_only=False,map_location='cpu')
            for key in ('q1','q2','value','target_q1','target_q2'):
                if not all(torch.equal(v,critic[key][k]) for k,v in checkpoint['critic'][key].items()):raise ValueError('Checkpoint critic differs')
            net=scalar_network(35);net.load_state_dict(checkpoint['critic']['value'])
            copy=scalar_network(35);copy.load_state_dict(critic['value'])
            with torch.inference_mode():
                inputs=torch.tensor((x[:256]-checkpoint['mean'])/checkpoint['std'])
                if not torch.equal(net(inputs),copy(inputs)):raise ValueError('Restored critic prediction changed')
            checkpoint_checks[label]=dict(actor=summary['checkpoint_actor_restore_exact'],critics_and_targets_exact=True)
        else:checkpoint_checks[label]=dict(actor=summary['checkpoint_actor_restore_exact'])
        destination=new_folder(c['output_dir'],'gamepad-outcome-'+label.lower()+'-release')
        for p in path.iterdir():
            if p.is_file() and p.name!='manifest.json':shutil.copyfile(p,destination/p.name)
        m.update(training_sources=['human_teacher','verified_human_correction','autonomous_success']+(['verified_autonomous_failure'] if label=='B' else []),
            initialization='seed42 shared BC40 best checkpoint then matched10 actor updates',
            training_run=str(training.resolve()),unpackaged_candidate=str(path.resolve()),
            validation_scope='retained teacher session reused for selection; separate held-out correction and failure sessions for diagnostics',
            documentation=str(Path('Docs/OutcomeLearningUnresolved.md').resolve()))
        m['input_sha256'].update(hashes([path/'manifest.json',training/'manifest.json',Path(__file__),Path('Docs/OutcomeLearningUnresolved.md')]))
        finish(destination,m)
        if digest(destination/'model.json')!=digest(path/'model.json'):raise ValueError('Packaging changed actor')
        packaged[label]=destination
    all_models={'original':old,'previous_repaired':Path(c['source_model']),**packaged}
    predictions={label:predict(path,oldx if label=='original' else x) for label,path in all_models.items()}
    metrics={label:{g:((p[mask]-y[mask])**2).mean(0).tolist() for g,mask in groups.items()} for label,p in predictions.items()}
    correction_metrics={}
    for label,path in packaged.items():
        squared=(predict(path,cx)-cy)**2
        correction_metrics[label]=[(float((squared[:,i]*cm[:,i]).sum()/cm[:,i].sum()) if cm[:,i].sum() else None) for i in range(3)]
    dt=np.diff([r['observation_ns']/1e9 for r in validation]);continuous=(dt>0)&(dt<.05)
    continuous &= np.array([a['recording']==b['recording'] and a['tracking_epoch']==b['tracking_epoch'] for a,b in zip(validation,validation[1:])])
    rates={label:float(np.mean(np.abs(np.diff(p[:,0])[continuous]/dt[continuous]-np.diff(y[:,0])[continuous]/dt[continuous]))) for label,p in predictions.items()}
    if summaries['A']['total_actor_updates']!=summaries['B']['total_actor_updates']:raise ValueError('Unmatched actor update counts')
    transitions=read(dataset/'transitions.json');held=[r for r in transitions if r['split']=='validation']
    hx=np.array([r['features'] for r in held],dtype=np.float32)
    delta=predict(packaged['B'],hx)-predict(packaged['A'],hx)
    per_run={}
    for run in sorted({r['run'] for r in held}):
        ids=np.array([r['run']==run for r in held]);per_run[run]=dict(samples=int(ids.sum()),mean_B_minus_A=delta[ids].mean(0).tolist(),mean_absolute_change=np.abs(delta[ids]).mean(0).tolist())
    presets={}
    template=Path('GamepadIO/continuous-reference-continuous.toml').read_text(encoding='utf-8')
    for label,path in packaged.items():
        presets[label]={}
        for mode,seconds in [('continuous',0),('5s',5)]:
            config=Path(f'GamepadIO/outcome-{label.lower()}-{mode}.toml')
            content=template.replace(c['source_model'],str(path.relative_to(Path.cwd())).replace('\\','/')).replace('seconds = 0',f'seconds = {seconds}')
            if seconds:content=content.replace('# Continuous driving; no duration limit or steering LPF.','# Five-second diagnostic; steering LPF off.')
            config.write_text(content,encoding='utf-8')
            preset=Path(f'TrainingController/outcome-{label.lower()}-{mode}-preset.json')
            save(preset,dict(task='게임패드 교정 주행',config=str(config.resolve())))
            presets[label][mode]=str(preset.resolve())
    report=dict(models={k:str(v) for k,v in all_models.items()},dataset=str(dataset.resolve()),metrics=metrics,group_counts={k:int(v.sum()) for k,v in groups.items()},
        correction_mse=correction_metrics,steering_rate_difference_mae=rates,checkpoint_checks=checkpoint_checks,
        actor_updates=summaries['A']['total_actor_updates'],critic_updates=summaries['B']['critic_updates'],
        advantage_summary=read(training/'advantage-summary.json'),heldout_failure_prediction_changes=per_run,
        heldout_failure_prediction_scope='Prediction changes on recorded states, not a counterfactual drive or correct-action error',
        presets=presets,gui_activated=False,live_performance_verified=False,
        candidate='B',candidate_basis='requested failure-aware experimental model; no automatic promotion based on offline metrics')
    save(out/'comparison.json',report)
    finish(out,dict(kind='outcome_evaluation',input_sha256=hashes([Path(__file__),dataset/'manifest.json',training/'manifest.json']+[p/'manifest.json' for p in all_models.values()])))
    print('EVALUATION_COMPLETE',out,flush=True);print(json.dumps(metrics),flush=True)
    return out


if __name__=='__main__':
    import sys
    evaluate(sys.argv[1])
