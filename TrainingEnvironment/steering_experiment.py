"""Immutable matched actor-only branches; no ACC input and no evaluation replay."""
import copy
import json
from pathlib import Path
import torch
from ModelTrainer.common import digest,hashes,verify_hashes,finish,new_folder
from .gamepad_online_io import read,save
from .gamepad_online import configuration
from .gamepad_online_learning import Learner,temporal_steering_loss
from .outcome_learning import predict

BASE=Path('reinforcement_runs')
PINS={
 'initial':(BASE/'gamepad-outcome-b-release-1793428298259417400-d7511d7d','3ffbd0ce329b9262f157ebc9b0ceaa30ace4cc1564d38489bd21bced39452d10'),
 'best':(BASE/'gamepad-online-session-1793878126400294000-79c7e829/gamepad-online-checkpoint-1793878600447167400-2b6d1b5a','ce05cd9443969a1ea4dd0dff2fe0d8aa754fbe3285af43919b444e385d869a7b'),
 'latest':(BASE/'gamepad-online-session-1793878126400294000-79c7e829/gamepad-online-checkpoint-1793880155794000000-1a4d8c7a','d553bcfb8a45ff04ee243e3c6a1e600692b65ea402dfbf6e69596beeab3fb86b')}

def identical(a,b):
    if isinstance(a,torch.Tensor):return isinstance(b,torch.Tensor) and torch.equal(a,b)
    if isinstance(a,dict):return a.keys()==b.keys() and all(identical(v,b[k]) for k,v in a.items())
    if isinstance(a,(list,tuple)):return len(a)==len(b) and all(identical(x,y) for x,y in zip(a,b))
    return a==b

def diagnostic(learner):
    x,n,_,_,dt,_,valid=learner.heldout_tensors
    if not valid.any():raise ValueError('No held-out continuous smoothness pairs')
    with torch.inference_mode():
        smooth=temporal_steering_loss(predict(learner.actor,x),predict(learner.actor,n),dt,valid).item()
        p=torch.tensor(learner.probes)
        p=(p-torch.tensor(learner.mean))/torch.tensor(learner.std);p[:,learner.excluded]=0
        mse=(predict(learner.actor,p)-torch.tensor(learner.probe_actions)).square().mean(0).tolist()
    return dict(temporal_loss=smooth,teacher_mse=mse,pair_count=int(valid.sum()))

def choose_candidate(candidates,parent):
    control=next(r for r in candidates if r['coefficient']==0)
    eligible=[]
    for r in candidates:
        d=r['diagnostic']
        passed=(r['coefficient']>0 and d['pair_count']>0
            and d['temporal_loss']<=.8*min(parent['temporal_loss'],control['diagnostic']['temporal_loss'])
            and all(v<=1.05*max(p,1e-12) for v,p in zip(d['teacher_mse'],parent['teacher_mse'])))
        r['offline_eligible']=passed
        if passed:eligible.append(r)
    return min(eligible,key=lambda r:(r['diagnostic']['temporal_loss'],r['coefficient'])) if eligible else None

def run(config='TrainingEnvironment/gamepad-online-auto.toml',check=lambda:None,emit=print):
    c,template,binding=configuration(config)
    pins={}
    for label,(folder,sha) in PINS.items():
        check();verify_hashes(read(folder/'manifest.json')['output_sha256'],folder)
        if digest(folder/'model.json')!=sha:raise ValueError('Pinned model changed: '+label)
        pins[label]=dict(path=str(folder.resolve()),model_sha256=sha,manifest_sha256=digest(folder/'manifest.json'))
    out=new_folder(c['output_dir'],'steering-smoothness-experiment')
    save(out/'pins.json',pins);save(out/'input-config.json',c)
    candidates=[];parent=None
    for coefficient in (0.,.1,1.,10.):
        check();emit(dict(phase='loading_branch',coefficient=coefficient,result_dir=str(out)))
        learner=Learner(c['source_model'],c['dataset'],binding,PINS['best'][0],
            smoothness=dict(method='temporal_steering_v1',coefficient=coefficient),branch=True)
        if parent is None:parent=diagnostic(learner);save(out/'parent-diagnostic.json',parent)
        frozen=copy.deepcopy(learner.state()['critic'])
        start_actor=learner.actor_updates;pending=learner.pending
        branch=out/('coefficient-'+str(coefficient));branch.mkdir()
        save(branch/'initial-rng.json',dict(bc=learner.bc_rng.get_state().tolist(),replay=learner.rng.get_state().tolist(),
             torch=torch.get_rng_state().tolist(),parent_manifest_sha256=pins['best']['manifest_sha256']))
        data=learner.tensors(learner.online);history=[]
        for i in range(1000):
            check();loss=learner.actor_step(learner.batch(data));learner.offline_actor_updates+=1
            history.append(dict(actor_update=i+1,**loss))
            if (i+1)%100==0:emit(dict(phase='actor_only',coefficient=coefficient,step=i+1,**loss))
        if not identical(frozen,learner.state()['critic']):
            # Replay sampling RNG legitimately advances; critic weights/optimizers do not.
            current=learner.state()['critic'];current.pop('sampling_rng');frozen.pop('sampling_rng')
            if not identical(frozen,current):raise ValueError('Actor-only experiment changed critic')
        if learner.pending!=pending or learner.actor_updates-start_actor!=1000:raise ValueError('Incorrect actor-only accounting')
        d=diagnostic(learner);model=learner.publish(branch,check,history)
        row=dict(coefficient=coefficient,path=str(model.resolve()),model_sha256=digest(model/'model.json'),
                 diagnostic=d,critic_frozen=True,actor_updates=1000)
        candidates.append(row);save(out/'candidates.json',candidates,replace=True)
        del learner,data,frozen
    selected=choose_candidate(candidates,parent)
    save(out/'candidates.json',candidates,replace=True)
    result=dict(pins=pins,candidates=candidates,selected=selected,evaluation_only=False,
                live_verified=False,selection_rule='temporal <= 0.8 * min(parent, matched-zero), each teacher axis MSE <= parent * 1.05')
    save(out/'experiment.json',result)
    if selected:
        models={**pins,'control':next(r for r in candidates if r['coefficient']==0),'candidate':selected}
        cfg=dict(schema='gamepad_policy_comparison_v1',training_config=str(Path(config).resolve()),models=models,
                 seed=42,screening_repeats=4,confirmation_repeats=5,session_seconds=7200,trial_seconds=180,
                 preset=str(Path('TrainingController/gamepad-online-auto-preset.json').resolve()))
        save(out/'comparison-config.json',cfg)
    finish(out,dict(kind='steering_smoothness_experiment',input_sha256=hashes([Path(__file__),Path(__file__).with_name('gamepad_online_learning.py')]),selected=selected))
    emit(dict(phase='finished',result_dir=str(out),selected=selected))
    return out

if __name__=='__main__':
    run(emit=lambda data:print(json.dumps(data),flush=True))
