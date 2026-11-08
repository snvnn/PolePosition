"""Matched BC control and offline IQL-weighted gamepad actor, with no live learning."""
import copy
import json
import random
import shutil
import subprocess
from pathlib import Path

import numpy as np
import torch
from ModelTrainer.common import read,save,hashes,verify_hashes,finish,new_folder,digest
from ModelTrainer.dataset import policy_binary
from .gamepad import ACTION_NAMES


def actor():
    return torch.nn.Sequential(torch.nn.Linear(35,128),torch.nn.ReLU(),torch.nn.Linear(128,128),torch.nn.ReLU(),torch.nn.Linear(128,3))


def scalar_network(inputs):
    return torch.nn.Sequential(torch.nn.Linear(inputs,128),torch.nn.ReLU(),torch.nn.Linear(128,128),torch.nn.ReLU(),torch.nn.Linear(128,1))


def predict(network,x):
    z=network(x)
    return torch.cat((z[:,:1].tanh(),z[:,1:].sigmoid()),1)


def expectile_loss(delta,tau):
    return (torch.where(delta>0,tau,1-tau)*delta.square()).mean()


def advantage_weights(q,v,temperature,maximum):
    return (temperature*(q-v)).clamp(max=float(np.log(maximum))).exp().detach()


def bellman_target(reward,dt,terminated,next_value,gamma):
    return reward+torch.pow(gamma,dt/.02)*(1-terminated)*next_value


def save_checkpoint(path,data):
    temp=path.with_suffix('.tmp');torch.save(data,temp);temp.replace(path)


def train(folder):
    print('PREPARING',folder,flush=True)
    folder=Path(folder);dm=read(folder/'manifest.json');verify_hashes(dm['output_sha256'],folder)
    if not dm['complete']:raise ValueError('Incomplete outcome dataset')
    c=read(folder/'config.json');parent=Path(c['source_model']);pm=read(parent/'manifest.json')
    verify_hashes(pm['output_sha256'],parent)
    torch.set_num_threads(1);torch.manual_seed(c['seed']);np.random.seed(c['seed']);random.seed(c['seed'])
    normal=read(folder/'normalization.json');mean=np.array(normal['mean'],dtype=np.float32);std=np.array(normal['std'],dtype=np.float32)
    names=dm['feature_names'];excluded=[names.index(n) for n in pm['excluded_input_features']]
    def features(rows,key='features'):
        raw=np.array([r[key] for r in rows],dtype=np.float32);x=torch.tensor((raw-mean)/std);x[:,excluded]=0
        if not torch.isfinite(x).all():raise ValueError('Nonfinite normalized features')
        return raw,x
    human=read(folder/'teacher-training.json');success=read(folder/'success-training.json');validation=read(folder/'teacher-validation.json')
    corrections=read(folder/'correction-training.json') if (folder/'correction-training.json').exists() else []
    human=human+corrections
    print('BC_LOADED',len(human),len(success),len(validation),flush=True)
    labels=human+success;raw,x=features(labels);vraw,vx=features(validation)
    y=torch.tensor([r['action'] for r in labels],dtype=torch.float32);vy=torch.tensor([r['action'] for r in validation],dtype=torch.float32)
    masks=torch.tensor([r.get('mask',[1,1,1]) for r in labels],dtype=torch.float32)
    human_w=np.array([r.get('weight',1) for r in human]);success_w=np.array([r.get('weight',1) for r in success])
    fraction=c['success_actor_fraction']
    weights=torch.tensor(np.r_[(1-fraction)*human_w/human_w.sum(),fraction*success_w/success_w.sum()],dtype=torch.double)
    transitions=read(folder/'transitions.json');tr=[r for r in transitions if r['split']=='train'];va=[r for r in transitions if r['split']=='validation']
    print('REPLAY_LOADED',len(tr),len(va),flush=True)
    _,tx=features(tr);_,tn=features(tr,'next_features');_,hx=features(va);_,hn=features(va,'next_features')
    ta=torch.tensor([r['action'] for r in tr],dtype=torch.float32);ha=torch.tensor([r['action'] for r in va],dtype=torch.float32)
    ic=c['iql']
    def tensors(rows):
        return [torch.tensor([r[key] for r in rows],dtype=torch.float32).reshape(-1,1) for key in ('reward','dt','terminated')]
    rew,dt,done=tensors(tr);hrew,hdt,hdone=tensors(va);rew*=ic['reward_scale'];hrew*=ic['reward_scale']
    # Retain the empirical mixture; no inferred counterfactual or success balancing.
    replay_rng=torch.Generator().manual_seed(c['seed']+1)
    run=new_folder(c['output_dir'],'outcome-training');save(run/'config.json',c)
    inputs=hashes([folder/'manifest.json',parent/'manifest.json',Path(__file__),policy_binary()])
    inputs.update({str((folder/name).resolve()):sha for name,sha in dm['output_sha256'].items()})
    save(run/'manifest.json',dict(schema_version=1,kind='outcome_training',complete=False,input_sha256=inputs))
    print('TRAINING_OUTPUT',run,flush=True)
    base=actor()
    with torch.no_grad():base[0].weight[:,excluded]=0
    optimizer=torch.optim.Adam(base.parameters(),lr=c['learning_rate'])
    batch=c['batch_size'];draws=c['epoch_samples'];common_epochs=c['epochs']-10
    if common_epochs<1:raise ValueError('At least eleven epochs required')
    common_rng=torch.Generator().manual_seed(c['seed']);history=[];best=float('inf');best_state=None;best_optimizer=None
    for epoch in range(1,common_epochs+1):
        indices=torch.multinomial(weights,draws,replacement=True,generator=common_rng)
        for ids in indices.split(batch):
            loss=((predict(base,x[ids])-y[ids]).square()*masks[ids]).sum()/masks[ids].sum()
            optimizer.zero_grad();loss.backward();optimizer.step()
        with torch.inference_mode():errors=(predict(base,vx)-vy).square().mean(0).tolist()
        history.append(dict(epoch=epoch,validation_mse=errors))
        if np.mean(errors)<best:best=np.mean(errors);best_state=copy.deepcopy(base.state_dict());best_optimizer=copy.deepcopy(optimizer.state_dict());best_epoch=epoch
        print('COMMON_BC',epoch,errors,flush=True)
    base.load_state_dict(best_state)
    save_checkpoint(run/'common-checkpoint.pt',dict(model=best_state,optimizer=best_optimizer,best_epoch=best_epoch,history=history,
        torch_rng=torch.get_rng_state(),numpy_rng=np.random.get_state(),python_rng=random.getstate(),sampling_rng=common_rng.get_state(),mean=mean,std=std))
    # IQL critic only sees recorded (state, action) pairs. No unseen actions are maximized.
    torch.manual_seed(c['seed']+1)
    q1,q2,value=scalar_network(38),scalar_network(38),scalar_network(35)
    tq1,tq2=copy.deepcopy(q1),copy.deepcopy(q2)
    qo=torch.optim.Adam(list(q1.parameters())+list(q2.parameters()),lr=c['learning_rate']);vo=torch.optim.Adam(value.parameters(),lr=c['learning_rate'])
    critic_history=[]
    for step in range(1,ic['updates']+1):
        ids=torch.randint(len(tx),(batch,),generator=replay_rng);sx,sa=tx[ids],ta[ids]
        qa=torch.cat((sx,sa),1)
        with torch.no_grad():qmin=torch.minimum(tq1(qa),tq2(qa))
        vl=expectile_loss(qmin-value(sx),ic['expectile']);vo.zero_grad();vl.backward();vo.step()
        with torch.no_grad():target=bellman_target(rew[ids],dt[ids],done[ids],value(tn[ids]),ic['gamma_per_20ms'])
        ql=(q1(qa)-target).square().mean()+(q2(qa)-target).square().mean()
        if not torch.isfinite(vl+ql):raise ValueError('Nonfinite critic loss')
        qo.zero_grad();ql.backward();qo.step()
        with torch.no_grad():
            for net,target_net in ((q1,tq1),(q2,tq2)):
                for p,t in zip(net.parameters(),target_net.parameters()):t.lerp_(p,ic['target_tau'])
        if step%500==0 or step==1:
            with torch.inference_mode():
                htarget=bellman_target(hrew,hdt,hdone,value(hn),ic['gamma_per_20ms'])
                held=(q1(torch.cat((hx,ha),1))-htarget).square().mean().item()
            item=dict(updates=step,q_loss=ql.item(),value_loss=vl.item(),heldout_td_mse=held);critic_history.append(item)
            print('IQL_CRITIC',item,flush=True)
    critic_state=dict(q1=q1.state_dict(),q2=q2.state_dict(),target_q1=tq1.state_dict(),target_q2=tq2.state_dict(),value=value.state_dict(),
        q_optimizer=qo.state_dict(),value_optimizer=vo.state_dict(),sampling_rng=replay_rng.get_state(),updates=ic['updates'])
    save_checkpoint(run/'critic.pt',critic_state);save(run/'critic-history.json',critic_history)
    with torch.inference_mode():
        advantage=torch.minimum(q1(torch.cat((tx,ta),1)),q2(torch.cat((tx,ta),1)))-value(tx)
        aw=advantage_weights(advantage,torch.zeros_like(advantage),ic['advantage_temperature'],ic['maximum_actor_weight'])
    aw=aw.clone()  # Autograd may save these fixed weights during the actor update.
    save(run/'advantage-summary.json',{label:dict(count=sum(r['outcome']==label for r in tr),
        mean_weight=float(aw[[r['outcome']==label for r in tr]].mean()),
        minimum_weight=float(aw[[r['outcome']==label for r in tr]].min()),maximum_weight=float(aw[[r['outcome']==label for r in tr]].max()))
        for label in sorted({r['outcome'] for r in tr})})
    outputs={}
    for variant in ('A','B'):
        network=actor();network.load_state_dict(best_state)
        opt=torch.optim.Adam(network.parameters(),lr=c['learning_rate']);opt.load_state_dict(copy.deepcopy(best_optimizer))
        rng=torch.Generator().manual_seed(c['seed']+2);rl_rng=torch.Generator().manual_seed(c['seed']+3)
        destination=new_folder(c['output_dir'],'gamepad-outcome-'+variant.lower());outputs[variant]=str(destination)
        manifest=dict(pm,complete=False,training_method='curated_BC_control' if variant=='A' else 'offline_IQL_weighted_regression_with_teacher_anchor',
            outcome_dataset=str(folder.resolve()),parent_model=str(parent),input_sha256=dict(inputs),
            normalization='retained human phase 0 only',live_performance_verified=False,selection='minimum retained teacher validation MSE among ten matched fine-tuning epochs')
        # Historical summary fields must not describe this newly trained model.
        for key in ['output_sha256','success_selection','augmentation_variant','noise','cached_augmented_dataset']:
            manifest.pop(key,None)
        save(destination/'manifest.json',manifest);save(destination/'config.json',c)
        best_score=float('inf');variant_history=[];updates=0
        for epoch in range(1,11):
            indices=torch.multinomial(weights,draws,replacement=True,generator=rng)
            for ids in indices.split(batch):
                bc=((predict(network,x[ids])-y[ids]).square()*masks[ids]).sum()/masks[ids].sum();loss=bc
                rl=torch.tensor(0.)
                if variant=='B':
                    ri=torch.randint(len(tx),(batch,),generator=rl_rng)
                    rl=(aw[ri].squeeze(1)*(predict(network,tx[ri])-ta[ri]).square().mean(1)).mean()
                    loss=bc+ic['actor_rl_coefficient']*rl
                if not torch.isfinite(loss):raise ValueError('Nonfinite actor loss')
                opt.zero_grad();loss.backward();opt.step();updates+=1
            with torch.inference_mode():errors=(predict(network,vx)-vy).square().mean(0).tolist()
            item=dict(epoch=epoch,validation_mse=errors,bc_loss=bc.item(),outcome_loss=rl.item());variant_history.append(item)
            if np.mean(errors)<best_score:
                best_score=np.mean(errors);selected=copy.deepcopy(network.state_dict());chosen=epoch
                save_checkpoint(destination/'checkpoint.pt',dict(model=selected,optimizer=opt.state_dict(),epoch=epoch,mean=mean,std=std,
                    torch_rng=torch.get_rng_state(),numpy_rng=np.random.get_state(),python_rng=random.getstate(),bc_rng=rng.get_state(),replay_rng=rl_rng.get_state(),
                    critic=critic_state if variant=='B' else None,config=c))
            print('ACTOR',variant,item,flush=True)
        network.load_state_dict(selected)
        save(destination/'model.json',dict(semantics='gamepad_v1',feature_names=names,action_names=ACTION_NAMES,mean=mean.tolist(),std=std.tolist(),
            layers=[dict(weight=network[i].weight.detach().tolist(),bias=network[i].bias.detach().tolist()) for i in (0,2,4)]))
        shutil.copyfile(parent/'speed-profile.json',destination/'speed-profile.json')
        # Every retained human validation state + both success laps + held-out outcome states.
        probes=np.concatenate((vraw,np.array([r['features'] for r in success],dtype=np.float32),np.array([r['features'] for r in va],dtype=np.float32)))
        with (destination/'parity-input.jsonl').open('x') as f:
            for row in probes:f.write(json.dumps(row.tolist())+'\n')
        subprocess.run([str(policy_binary()),'predict-gamepad',str(destination/'model.json'),str(destination/'parity-input.jsonl'),str(destination/'parity-output.jsonl')],check=True)
        with torch.inference_mode():expected=predict(network,torch.tensor((probes-mean)/std)).numpy()
        actual=np.array([json.loads(line) for line in (destination/'parity-output.jsonl').open()]);error=float(abs(actual-expected).max())
        if not np.isfinite(actual).all() or error>1e-5:raise ValueError('Native actor parity failed')
        restored=torch.load(destination/'checkpoint.pt',weights_only=False,map_location='cpu');restored_net=actor();restored_net.load_state_dict(restored['model'])
        with torch.inference_mode():
            if not torch.equal(predict(network,vx[:256]),predict(restored_net,vx[:256])):raise ValueError('Checkpoint actor restoration differs')
        manifest.update(parity_passed=True,prepared_sha256=digest(Path(dm['prepared'])/'manifest.json'))
        save(destination/'summary.json',dict(variant=variant,training_examples=len(labels),human_examples=len(human),correction_examples=len(corrections),success_examples=len(success),validation_examples=len(validation),
            replay_train=len(tr) if variant=='B' else 0,replay_validation=len(va),common_epochs=common_epochs,common_best_epoch=best_epoch,
            actor_finetuning_updates=updates,total_actor_updates=(common_epochs+10)*int(np.ceil(draws/batch)),critic_updates=ic['updates'] if variant=='B' else 0,
            best_epoch=chosen,validation_mse=variant_history[chosen-1]['validation_mse'],history=variant_history,
            parity_samples=len(probes),parity_max_error=error,checkpoint_actor_restore_exact=True,live_performance_verified=False))
        verify_hashes(inputs);finish(destination,manifest)
        print('MODEL_COMPLETE',variant,destination,flush=True)
    save(run/'models.json',outputs);finish(run,dict(schema_version=1,kind='outcome_training',input_sha256=inputs))
    print('TRAINING_COMPLETE',run,flush=True)
    return run


if __name__=='__main__':
    import sys
    train(sys.argv[1])
