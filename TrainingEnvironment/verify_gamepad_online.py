"""Offline continuation/restore test. Synthetic replay is never a live candidate."""
import argparse
import copy
import json
from pathlib import Path
import torch
from ModelTrainer.common import digest, new_folder, finish
from .gamepad_online import configuration,resolve_resume
from .gamepad_online_io import read,save
from .gamepad_online_learning import Learner


def verify(config,resume):
    c,_,binding=configuration(config);cp,session=resolve_resume(resume)
    initial_hash=digest(cp/'model.json');pointer_hash=digest(session/'latest.json') if session else None
    out=new_folder('tmp','gamepad-online-offline-verification')
    print('OUTPUT',out,flush=True)
    learner=Learner(c['source_model'],c['dataset'],binding,cp)
    if learner.warm_updates!=20000 or learner.actor_updates!=0:raise ValueError('Expected initial warmed B checkpoint')
    if initial_hash!=c['source_policy_sha256']:raise ValueError('Warmup changed deployed actor bytes')
    # Test-only duplication of historical train rows, not a claim of fresh experience.
    fixture=copy.deepcopy(learner.seed[:256]);save(out/'experience.json',fixture)
    learner.commit(fixture,out)
    history=learner.update(lambda:None,lambda **kw:None)
    if len(history)!=64 or learner.actor_updates!=16 or learner.pending!=0:raise ValueError('Unexpected update schedule')
    if not torch.equal(learner.actor[0].weight[:,learner.excluded],torch.zeros_like(learner.actor[0].weight[:,learner.excluded])):
        raise ValueError('Excluded input columns reactivated')
    candidate=learner.publish(out,lambda:None,history)
    print('CANDIDATE_VERIFIED',candidate,flush=True)
    restored=Learner(c['source_model'],c['dataset'],binding,candidate)
    a=learner.batch(learner.tensors(learner.online));b=restored.batch(restored.tensors(restored.online))
    if any(not torch.equal(x,y) for x,y in zip(a,b)):raise ValueError('Replay RNG restore differs')
    learner.step(a,True);restored.step(b,True)
    for name,left in dict(actor=learner.actor,**learner.networks()).items():
        right=dict(actor=restored.actor,**restored.networks())[name]
        if any(not torch.equal(v,right.state_dict()[k]) for k,v in left.state_dict().items()):
            raise ValueError('Next update after restore differs: '+name)
    before=digest(candidate/'checkpoint.pt');restored.pending=256
    def cancel():raise InterruptedError('test interruption')
    try:restored.update(cancel,lambda **kw:None)
    except InterruptedError:pass
    else:raise ValueError('Cancellation ignored')
    if digest(candidate/'checkpoint.pt')!=before:raise ValueError('Cancellation mutated completed checkpoint')
    if session and digest(session/'latest.json')!=pointer_hash:raise ValueError('Offline test promoted a policy')
    result=dict(passed=True,test_only=True,live_episode_verified=False,live_stability_passed=False,
        initial_policy_sha256=initial_hash,initial_actor_identical_to_B=True,qv_warmup_updates=20000,
        synthetic_samples=256,qv_updates=len(history),actor_updates=16,replay_rng_and_next_optimizer_step_exact=True,
        cancellation_preserved_checkpoint=True,selected_policy_unchanged=True,
        parity=read(candidate/'verification.json'))
    save(out/'report.json',result);finish(out,dict(kind='offline_gamepad_online_test',test_only=True))
    print(json.dumps(result),flush=True)
    return out


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',default='TrainingEnvironment/gamepad-online.toml');p.add_argument('--resume',required=True)
    a=p.parse_args();verify(a.config,a.resume)
