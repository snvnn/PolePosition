"""Continuous IQL continuation of the deployed B checkpoint, with atomic candidates."""
import copy
import json
import random
import shutil
import subprocess
from pathlib import Path
import numpy as np
import torch
from ModelTrainer.common import read, save, digest, verify_hashes, finish, new_folder, hashes
from ModelTrainer.dataset import policy_binary
from .gamepad import ACTION_NAMES
from .outcome_learning import actor, scalar_network, predict, expectile_loss, advantage_weights, bellman_target, save_checkpoint
from .gamepad_online_data import SEMANTICS, derive_seed
from .gamepad_online_io import read,save,rows_digest,same_bindings
from . import gamepad_checkpoint_reader


def finite(value):
    if isinstance(value, torch.Tensor) and not torch.isfinite(value).all():
        raise ValueError('Nonfinite checkpoint tensor')
    if isinstance(value, np.ndarray) and not np.isfinite(value).all():
        raise ValueError('Nonfinite checkpoint array')
    if isinstance(value, dict):
        for v in value.values():finite(v)
    if isinstance(value, (tuple,list)):
        for v in value:finite(v)
    if isinstance(value, float) and not np.isfinite(value):raise ValueError('Nonfinite checkpoint scalar')


def smoothness_config(value=None):
    value=dict(value or dict(method='none',coefficient=0.))
    if set(value)!={'method','coefficient'} or value['method'] not in ('none','temporal_steering_v1'):
        raise ValueError('Unknown smoothness configuration')
    coefficient=value['coefficient']
    if isinstance(coefficient,bool) or not isinstance(coefficient,(int,float)) or not np.isfinite(coefficient) or coefficient<0:
        raise ValueError('Invalid smoothness coefficient')
    if value['method']=='none' and coefficient!=0:raise ValueError('Disabled smoothness must have zero coefficient')
    return dict(method=value['method'],coefficient=float(coefficient))


def smooth_pair(row):
    """Only verified causal transitions; never bridge a reset/terminal boundary."""
    try:
        t,send,nt=(row[k] for k in ('observation_ns','send_end_ns','next_observation_ns'))
        dt=row['dt']
        return (row.get('continuous_semantics')==SEMANTICS and not row['terminated']
                and not row.get('truncated',False) and bool(row.get('source_log_sha256'))
                and row.get('tracking_epoch') is not None
                and row.get('next_tracking_epoch',row['tracking_epoch'])==row['tracking_epoch']
                and np.isfinite(dt) and 0<dt<=.05 and t<=send<nt
                and abs((nt-t)/1e9-dt)<1e-8
                and len(row['features'])==len(row['next_features'])==35
                and np.isfinite([row['features'],row['next_features']]).all())
    except (KeyError,TypeError,ValueError):return False

def resume_smoothness(saved,requested=None,branch=False):
    previous=smoothness_config(saved)
    selected=previous if requested is None else smoothness_config(requested)
    if previous!=selected and not branch:
        raise ValueError('Changing smoothness requires an explicit experiment branch')
    return selected


def temporal_steering_loss(current,next_action,dt,valid):
    mask=valid.reshape(-1).bool() & torch.isfinite(dt.reshape(-1)) & (dt.reshape(-1)>0) & (dt.reshape(-1)<=.05)
    if not mask.any():return current[:,:1].sum()*0
    return (((next_action[mask,0]-current[mask,0])*.02/dt.reshape(-1)[mask]).square()).mean()


class Learner:
    def __init__(self, source, dataset, binding, checkpoint=None, smoothness=None, branch=False):
        torch.set_num_threads(1)
        self.source=Path(source).resolve();self.dataset=Path(dataset).resolve();self.binding=binding
        self.pm=read(self.source/'manifest.json');verify_hashes(self.pm['output_sha256'],self.source)
        dm=read(self.dataset/'manifest.json');verify_hashes(dm['output_sha256'],self.dataset)
        self.data_hash=digest(self.dataset/'manifest.json')
        self.c=read(self.dataset/'config.json');self.ic=self.c['iql']
        outcomes=read(self.dataset/'transitions.json')
        self.heldout_logs={r['source_log_sha256'] for r in outcomes if r['split']=='validation'}
        heldout=[dict(r,continuous_semantics=SEMANTICS) for r in outcomes if r['split']=='validation']
        self.seed=derive_seed(outcomes)
        del outcomes
        self.seed_hash=rows_digest(self.seed)
        self.names=read(self.source/'model.json')['feature_names']
        self.excluded=[self.names.index(n) for n in self.pm['excluded_input_features']]
        folder=Path(checkpoint) if checkpoint else self.source
        if checkpoint:
            cm=read(folder/'manifest.json');verify_hashes(cm['output_sha256'],folder)
            if cm.get('continuous_semantics')!=SEMANTICS or not cm.get('complete') or not cm.get('parity_passed'):
                raise ValueError('Cannot resume episodic/incomplete checkpoint as continuous IQL')
        state=torch.load(folder/'checkpoint.pt',map_location='cpu',weights_only=False,
                         pickle_module=gamepad_checkpoint_reader)
        finite(state)
        self.smoothness=resume_smoothness(state.get('smoothness'),smoothness,branch)
        self.smoothness_branch=state.get('smoothness_branch')
        if branch:
            self.smoothness_branch=dict(parent=str(folder),parent_manifest_sha256=digest(folder/'manifest.json'),
                parent_model_sha256=digest(folder/'model.json'),method_version='steering_regularization_v1')
        self.offline_actor_updates=state.get('offline_actor_updates',0)
        if checkpoint and (not same_bindings(state.get('binding'),binding) or state.get('seed_sha256')!=self.seed_hash
                           or state.get('dataset_sha256')!=self.data_hash or state.get('continuous_semantics')!=SEMANTICS):
            raise ValueError('Checkpoint data, profile or continuous semantics differs')
        self.mean=np.asarray(state['mean'],dtype=np.float32);self.std=np.asarray(state['std'],dtype=np.float32)
        self.actor=actor();self.actor.load_state_dict(state['model'])
        self.ao=torch.optim.Adam(self.actor.parameters(),lr=.0003);self.ao.load_state_dict(state['optimizer'])
        self.q1,self.q2,self.tq1,self.tq2,self.v=[scalar_network(n) for n in (38,38,38,38,35)]
        critic=state['critic']
        if critic is None:raise ValueError('B critic/optimizers required')
        for key,net in self.networks().items():net.load_state_dict(critic[key])
        self.qo=torch.optim.Adam(list(self.q1.parameters())+list(self.q2.parameters()),lr=.0003)
        self.vo=torch.optim.Adam(self.v.parameters(),lr=.0003)
        self.qo.load_state_dict(critic['q_optimizer']);self.vo.load_state_dict(critic['value_optimizer'])
        self.bc_rng=torch.Generator();self.bc_rng.set_state(state['bc_rng'])
        self.rng=torch.Generator();self.rng.set_state(state['replay_rng'])
        torch.set_rng_state(state['torch_rng']);np.random.set_state(state['numpy_rng']);random.setstate(state['python_rng'])
        self.online=state.get('online',[]);self.pending=state.get('pending',0)
        if any(r.get('split')!='train' or r['source_log_sha256'] in self.heldout_logs for r in self.online):
            raise ValueError('Validation data in online checkpoint')
        self.episodes=state.get('episodes',0);self.q_updates=state.get('q_updates',0);self.actor_updates=state.get('actor_updates',0)
        self.warm_updates=state.get('warm_updates',0);self.committed=state.get('committed',[])
        self.seed_tensors=self.tensors(self.seed)
        self.heldout_tensors=self.tensors(heldout)
        human=read(self.dataset/'teacher-training.json')+read(self.dataset/'correction-training.json')
        success=read(self.dataset/'success-training.json')
        labels=human+success
        self.bc_x=self.features(labels);self.bc_y=torch.tensor([r['action'] for r in labels],dtype=torch.float32)
        self.mask=torch.tensor([r.get('mask',[1,1,1]) for r in labels],dtype=torch.float32)
        hw=np.array([r.get('weight',1) for r in human]);sw=np.array([r.get('weight',1) for r in success])
        f=self.c['success_actor_fraction'];self.weights=torch.tensor(np.r_[(1-f)*hw/hw.sum(),f*sw/sw.sum()],dtype=torch.double)
        validation=read(self.dataset/'teacher-validation.json')
        def identity(r):return (r.get('recording',r.get('run')),r.get('sequence'),r.get('observation_ns'))
        held_labels={identity(r) for r in validation+read(self.dataset/'correction-validation.json')}
        if any(identity(r) in held_labels for r in labels):raise ValueError('Validation label in actor anchor')
        self.probes=np.array([r['features'] for r in validation],dtype=np.float32)
        self.probe_actions=np.array([r['action'] for r in validation],dtype=np.float32)

    def networks(self):return dict(q1=self.q1,q2=self.q2,target_q1=self.tq1,target_q2=self.tq2,value=self.v)

    def features(self, rows, key='features'):
        x=torch.tensor((np.array([r[key] for r in rows],dtype=np.float32)-self.mean)/self.std)
        x[:,self.excluded]=0
        finite(x);return x

    def tensors(self, rows):
        return (self.features(rows),self.features(rows,'next_features'),
                torch.tensor([r['action'] for r in rows],dtype=torch.float32),
                *[torch.tensor([r[k] for r in rows],dtype=torch.float32).reshape(-1,1) for k in ('reward','dt','terminated')],
                torch.tensor([bool(smooth_pair(r)) for r in rows],dtype=torch.bool).reshape(-1,1))

    def batch(self, online=None):
        ids=torch.randint(len(self.seed),(256 if online is None else 128,),generator=self.rng)
        batch=[v[ids] for v in self.seed_tensors]
        if online is not None:
            other=torch.randint(len(self.online),(128,),generator=self.rng)
            batch=[torch.cat((a,b[other])) for a,b in zip(batch,online)]
        return batch

    def actor_step(self,batch):
        x,n,a,r,dt,done,*extra=batch;qa=torch.cat((x,a),1);ic=self.ic
        ids=torch.multinomial(self.weights,256,replacement=True,generator=self.bc_rng)
        bc=((predict(self.actor,self.bc_x[ids])-self.bc_y[ids]).square()*self.mask[ids]).sum()/self.mask[ids].sum()
        with torch.no_grad():w=advantage_weights(torch.minimum(self.q1(qa),self.q2(qa)),self.v(x),ic['advantage_temperature'],ic['maximum_actor_weight'])
        prediction=predict(self.actor,x)
        rl=(w.squeeze(1)*(prediction-a).square().mean(1)).mean()
        loss=bc+ic['actor_rl_coefficient']*rl
        smooth=prediction[:,:1].sum()*0
        if self.smoothness['coefficient']>0:
            if not extra:raise ValueError('Missing verified smoothness pair mask')
            smooth=temporal_steering_loss(prediction,predict(self.actor,n),dt,extra[0])
            loss=loss+self.smoothness['coefficient']*smooth
        finite(loss);self.ao.zero_grad();loss.backward();self.ao.step();self.actor_updates+=1
        return dict(bc_loss=bc.item(),actor_rl_loss=rl.item(),steering_smoothness_loss=smooth.item())

    def step(self, batch, update_actor):
        x,n,a,r,dt,done=batch[:6];qa=torch.cat((x,a),1);ic=self.ic
        with torch.no_grad():qmin=torch.minimum(self.tq1(qa),self.tq2(qa))
        vl=expectile_loss(qmin-self.v(x),ic['expectile'])
        finite(vl);self.vo.zero_grad();vl.backward();self.vo.step()
        with torch.no_grad():target=bellman_target(r*ic['reward_scale'],dt,done,self.v(n),ic['gamma_per_20ms'])
        ql=(self.q1(qa)-target).square().mean()+(self.q2(qa)-target).square().mean()
        finite(ql);self.qo.zero_grad();ql.backward();self.qo.step()
        with torch.no_grad():
            for source,dest in ((self.q1,self.tq1),(self.q2,self.tq2)):
                for p,t in zip(source.parameters(),dest.parameters()):t.lerp_(p,ic['target_tau'])
        losses=dict(q_loss=ql.item(),value_loss=vl.item())
        if update_actor:
            losses.update(self.actor_step(batch))
        return losses

    def warm(self, check, progress):
        original=copy.deepcopy(self.actor.state_dict())
        for step in range(self.warm_updates,20000):
            check();loss=self.step(self.batch(),False);self.warm_updates=step+1
            if (step+1)%100==0:progress(phase='critic_warmup',q_updates=step+1,actor_updates=0,**loss)
        if any(not torch.equal(v,original[k]) for k,v in self.actor.state_dict().items()):
            raise ValueError('Warmup changed B actor')

    def commit(self, experience, artifact):
        if any(r.get('evaluation_only') for r in experience):raise ValueError('Evaluation data cannot train')
        if any(r.get('split')!='train' or r['source_log_sha256'] in self.heldout_logs for r in experience):
            raise ValueError('Validation data cannot enter online replay')
        sha=digest(Path(artifact)/'experience.json')
        if sha in self.committed:raise ValueError('Experience already committed')
        self.online.extend(experience);self.pending+=len(experience)
        lost=max(0,len(self.online)-200000)
        if lost:self.online=self.online[lost:]
        self.pending=min(self.pending,len(self.online));self.episodes+=1;self.committed.append(sha)

    def update(self, check, progress):
        if len(self.online)<256:return []
        count=min(self.pending//4,500);data=self.tensors(self.online);history=[]
        for _ in range(count):
            check();self.q_updates+=1
            loss=self.step(self.batch(data),self.q_updates%4==0);self.pending-=4
            history.append(dict(q_update=self.q_updates,actor_updates=self.actor_updates,**loss))
            if self.q_updates%20==0:progress(phase='learning',q_updates=self.q_updates,actor_updates=self.actor_updates,**loss)
        return history

    def state(self):
        critic={k:v.state_dict() for k,v in self.networks().items()}
        critic.update(q_optimizer=self.qo.state_dict(),value_optimizer=self.vo.state_dict(),
            sampling_rng=self.rng.get_state(),updates=20000+self.warm_updates+self.q_updates)
        return dict(continuous_semantics=SEMANTICS,binding=self.binding,dataset_sha256=self.data_hash,seed_sha256=self.seed_hash,
            model=self.actor.state_dict(),optimizer=self.ao.state_dict(),critic=critic,mean=self.mean,std=self.std,
            online=self.online,pending=self.pending,episodes=self.episodes,q_updates=self.q_updates,actor_updates=self.actor_updates,
            warm_updates=self.warm_updates,committed=self.committed,bc_rng=self.bc_rng.get_state(),replay_rng=self.rng.get_state(),
            torch_rng=torch.get_rng_state(),numpy_rng=np.random.get_state(),python_rng=random.getstate(),config=self.c,
            source=str(self.source),dataset=str(self.dataset),smoothness=self.smoothness,
            smoothness_branch=self.smoothness_branch,offline_actor_updates=self.offline_actor_updates)

    def publish(self, parent, check, history):
        check();out=new_folder(parent,'gamepad-online-checkpoint');state=self.state();finite(state)
        seed_file=Path(parent)/'continuous-seed-replay.json'
        if not seed_file.exists():save(seed_file,self.seed)
        save_checkpoint(out/'checkpoint.pt',state)
        restored=torch.load(out/'checkpoint.pt',weights_only=False,map_location='cpu',
                            pickle_module=gamepad_checkpoint_reader);finite(restored)
        # Compare every tensor/state field, including optimizer and RNG, after disk reload.
        def equal(a,b):
            if isinstance(a,torch.Tensor):return isinstance(b,torch.Tensor) and torch.equal(a,b)
            if isinstance(a,np.ndarray):return np.array_equal(a,b)
            if isinstance(a,dict):return a.keys()==b.keys() and all(equal(v,b[k]) for k,v in a.items())
            if isinstance(a,(list,tuple)):return len(a)==len(b) and all(equal(x,y) for x,y in zip(a,b))
            return a==b
        if not equal(state,restored):raise ValueError('Checkpoint restore differs')
        model=dict(semantics='gamepad_v1',feature_names=self.names,action_names=ACTION_NAMES,
            mean=self.mean.tolist(),std=self.std.tolist(),layers=[dict(weight=self.actor[i].weight.detach().tolist(),bias=self.actor[i].bias.detach().tolist()) for i in (0,2,4)])
        # Preserve initial deployed bytes/hash as well as numerical behavior through critic warmup.
        if self.actor_updates==0:shutil.copyfile(self.source/'model.json',out/'model.json')
        else:save(out/'model.json',model)
        shutil.copyfile(self.source/'speed-profile.json',out/'speed-profile.json')
        save(out/'config.json',dict(self.c,source_model=str(self.source),dataset_production_source=self.c['source_model'],
            continuous_semantics=SEMANTICS,smoothness=self.smoothness,online_schedule=dict(replay_capacity=200000,seed_fraction=.5,
                minimum_online_samples=256,qv_per_new_samples=4,maximum_qv_updates=500,actor_every_qv=4)))
        save(out/'updates.json',history)
        probes=self.probes
        if self.online:probes=np.concatenate((probes,np.array([r['features'] for r in self.online[-2048:]],dtype=np.float32)))
        with (out/'parity-input.jsonl').open('x') as f:
            for row in probes:f.write(json.dumps(row.tolist())+'\n')
        check()
        subprocess.run([str(policy_binary()),'predict-gamepad',str(out/'model.json'),str(out/'parity-input.jsonl'),str(out/'parity-output.jsonl')],check=True,timeout=120)
        clone=actor();clone.load_state_dict(restored['model'])
        with torch.inference_mode():expected=predict(clone,torch.tensor((probes-self.mean)/self.std)).numpy()
        with (out/'parity-output.jsonl').open() as f:actual=np.array([json.loads(line) for line in f])
        error=float(np.max(abs(actual-expected)))
        if not np.isfinite(actual).all() or error>1e-5:raise ValueError('Python/Rust parity failed')
        hx,hn,ha,hr,hdt,hdone=self.heldout_tensors[:6]
        with torch.inference_mode():
            target=bellman_target(hr*self.ic['reward_scale'],hdt,hdone,self.v(hn),self.ic['gamma_per_20ms'])
            held_td=(self.q1(torch.cat((hx,ha),1))-target).square().mean().item()
        check();save(out/'verification.json',dict(max_absolute_error=error,restore_exact=True,heldout_td_mse=held_td,
            teacher_validation_mse=((expected[:len(self.probes)]-self.probe_actions)**2).mean(0).tolist(),live_stability_passed=False))
        manifest=dict(self.pm,kind='gamepad_imitation',continuous_semantics=SEMANTICS,
            training_method='continuous_IQL_with_teacher_anchor',complete=False,parity_passed=True,
            parent_model=str(self.source),smoothness=self.smoothness,smoothness_branch=self.smoothness_branch,
            seed_sha256=self.seed_hash,outcome_dataset=str(self.dataset),
            lineage=dict(previous_semantics='episodic_valid_lap_terminal',valid_lap_terminal=False,warmup_qv=20000),
            input_sha256=hashes([self.source/'manifest.json',self.dataset/'manifest.json',seed_file]),live_performance_verified=False)
        manifest['producer_sha256']=hashes([Path(__file__),Path(__file__).with_name('gamepad_online_data.py'),
            Path(__file__).with_name('gamepad_online.py'),Path(__file__).with_name('gamepad_online_io.py'),
            Path(__file__).with_name('game_process.py'),Path(__file__).with_name('champion_cycle.py'),
            Path(__file__).with_name('lap_gamepad.py'),Path(__file__).with_name('acc_reset.py'),policy_binary(),
            Path(gamepad_checkpoint_reader.__file__),Path('target/release/poleposition-gamepad.exe')])
        manifest.pop('output_sha256',None);finish(out,manifest)
        verify_hashes(read(out/'manifest.json')['output_sha256'],out);check()
        return out
