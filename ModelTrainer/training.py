"""Bounded CPU training, reproducible epoch checkpoints, and immutable exports."""
import copy
import csv
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time

import numpy as np
import torch
from torch import nn

from .common import FIELDS, UNITS, config, digest, finish, hashes, load_artifact, new_folder, read, rows, save, verify_hashes
from .dataset import policy_binary


class Policy(nn.Module):
    def __init__(self, normalizer):
        super().__init__()
        self.register_buffer('mean',torch.tensor(normalizer['mean'],dtype=torch.float32))
        self.register_buffer('std',torch.tensor(normalizer['std'],dtype=torch.float32))
        self.net=nn.Sequential(nn.Linear(19,64),nn.ReLU(),nn.Linear(64,64),nn.ReLU(),nn.Linear(64,1),nn.Tanh())

    def forward(self, x):
        return self.net((x-self.mean)/self.std).squeeze(-1)


def environment(c):
    if torch.__version__!='2.8.0+cpu' or np.__version__!='2.3.3' or sys.version_info[:2]!=(3,11):
        raise ValueError('Use pinned Python 3.11 / torch 2.8.0+cpu / numpy 2.3.3 training environment')
    torch.set_num_threads(c['threads'])
    torch.use_deterministic_algorithms(True)
    random.seed(c['seed']);np.random.seed(c['seed']);torch.manual_seed(c['seed'])


def tensors(data, normalizer):
    if not data: raise ValueError('Empty training/evaluation split')
    x=torch.tensor([r['features'] for r in data],dtype=torch.float32)
    y=torch.tensor([r['target_u'] for r in data],dtype=torch.float32)
    w=torch.tensor([normalizer['class_weights'].get(r['pedal_class'],1.) for r in data],dtype=torch.float32)
    if x.ndim!=2 or x.shape[1]!=19 or not torch.isfinite(x).all() or not torch.isfinite(y).all() or (y.abs()>1.000001).any():
        raise ValueError('Invalid prepared feature/target values')
    return x,y,w


def checkpoint(path, data):
    path=Path(path);tmp=path.with_suffix('.tmp')
    with tmp.open('xb') as f:
        torch.save(data,f);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)


def train(dataset=None, config_path=None, resume=None, smoke=False, epoch_limit=None):
    previous=None
    if resume:
        parent,pm=load_artifact(Path(resume).parent,'policy_training',complete=False)
        if Path(resume).name not in pm['output_sha256']:raise ValueError('Uninventoried checkpoint')
        previous=torch.load(resume,map_location='cpu',weights_only=True)
        dataset=previous['dataset'];c=previous['config'];smoke=previous['smoke']
    else:
        c=config(config_path)
    environment(c)
    dataset,dm=load_artifact(dataset,'teacher_dataset')
    if not smoke and not dm['training_ready']:raise ValueError('Independent validation/test teacher runs required')
    if previous and previous['dataset_sha256']!=digest(dataset/'manifest.json'):raise ValueError('Resume dataset changed')
    normalizer=read(dataset/'normalizer.json')
    train_rows=rows(dataset/'train.jsonl');validation=rows(dataset/'validation.jsonl') if not smoke else train_rows
    x,y,w=tensors(train_rows,normalizer);vx,vy,vw=tensors(validation,normalizer)
    model=Policy(normalizer);optimizer=torch.optim.AdamW(model.parameters(),lr=c['learning_rate'],weight_decay=c['weight_decay'])
    loss_fn=nn.SmoothL1Loss(beta=c['beta'],reduction='none')
    history=[];best_loss=float('inf');best_epoch=0;bad=0;start=1;best=None;updates=0
    if previous:
        model.load_state_dict(previous['model_state_dict']);optimizer.load_state_dict(previous['optimizer_state_dict'])
        history=previous['history'];best_loss=previous['best_loss'];best_epoch=previous['best_epoch'];bad=previous['bad_epochs']
        best=previous['best_state_dict'];updates=previous['updates'];start=previous['epoch']+1
        random.setstate(previous['python_rng']);torch.set_rng_state(previous['torch_rng'])
        ns=previous['numpy_rng'];np.random.set_state((ns[0],np.array(ns[1],dtype=np.uint32),ns[2],ns[3],ns[4]))
    out=new_folder(c['output_dir'],'smoke' if smoke else 'train')
    inputs=hashes([dataset/'manifest.json',dataset/'normalizer.json',dataset/'train.jsonl',dataset/'validation.jsonl'])
    implementation=hashes(Path(__file__).parent.glob('*.py'))
    if resume:inputs.update(hashes([resume,Path(resume).parent/'manifest.json']))
    manifest=dict(schema_version=1,kind='policy_training',complete=False,tool_version='0.1.0',
        dataset=str(dataset),dataset_sha256=digest(dataset/'manifest.json'),config=c,smoke=smoke,
        input_sha256=inputs,implementation_sha256=implementation,environment=dict(python=sys.version,torch=torch.__version__,numpy=np.__version__),
        iteration_definition='One optimizer update per minibatch; epochs bounded by max_epochs and patience; no auto restart.')
    save(out/'manifest.json',manifest);save(out/'normalizer.json',normalizer)
    # A tiny provisional smoke run never qualifies for live use.
    cap=min(c['max_epochs'],3) if smoke else c['max_epochs']
    if epoch_limit is not None:cap=min(cap,epoch_limit)
    interrupted=False;started=time.perf_counter()
    try:
        if previous:checkpoint(out/'last.pt',previous)
        for epoch in range(start,cap+1 if bad<c['patience'] else start):
            model.train();permutation=torch.randperm(len(x));total=0.;weight_sum=0.
            for batch in permutation.split(c['batch_size']):
                optimizer.zero_grad(set_to_none=True)
                loss=(loss_fn(model(x[batch]),y[batch])*w[batch]).mean()
                if not torch.isfinite(loss):raise ValueError('Nonfinite training loss')
                loss.backward();nn.utils.clip_grad_norm_(model.parameters(),c['gradient_clip'],error_if_nonfinite=True)
                optimizer.step();updates+=1;total+=loss.item()*len(batch);weight_sum+=len(batch)
            model.eval()
            with torch.inference_mode():vl=float((loss_fn(model(vx),vy)*vw).mean())
            if not np.isfinite(vl):raise ValueError('Nonfinite validation loss')
            history.append(dict(epoch=epoch,updates=updates,train_loss=total/weight_sum,validation_loss=vl))
            if vl<best_loss:
                best_loss=vl;best_epoch=epoch;bad=0;best=copy.deepcopy(model.state_dict())
            else:bad+=1
            state=dict(epoch=epoch,updates=updates,model_state_dict=model.state_dict(),optimizer_state_dict=optimizer.state_dict(),
                best_state_dict=best,best_loss=best_loss,best_epoch=best_epoch,bad_epochs=bad,history=history,
                torch_rng=torch.get_rng_state(),python_rng=random.getstate(),dataset=str(dataset),
                numpy_rng=[np.random.get_state()[0],np.random.get_state()[1].tolist(),*np.random.get_state()[2:]],
                dataset_sha256=digest(dataset/'manifest.json'),config=c,smoke=smoke)
            checkpoint(out/'last.pt',state)
            print(f'epoch={epoch} updates={updates} train={total/weight_sum:.7f} validation={vl:.7f} best={best_epoch}',flush=True)
            if bad>=c['patience']:break
    except KeyboardInterrupt:
        interrupted=True
        if (out/'last.pt').exists():
            state=torch.load(out/'last.pt',map_location='cpu',weights_only=True)
            history=state['history'];best=state['best_state_dict'];best_epoch=state['best_epoch'];best_loss=state['best_loss'];updates=state['updates']
    except BaseException as e:
        finish(out,manifest,False,e);raise
    if best is None:
        finish(out,manifest,False,'No completed epoch');return out
    try:
        checkpoint(out/'best.pt',dict(model_state_dict=best,epoch=best_epoch))
        with (out/'history.csv').open('x',encoding='utf-8',newline='') as f:
            wr=csv.DictWriter(f,fieldnames=['epoch','updates','train_loss','validation_loss']);wr.writeheader();wr.writerows(history)
        save(out/'summary.json',dict(epochs=len(history),updates=updates,best_epoch=best_epoch,best_loss=best_loss,
            training_samples=len(x),iterations_per_epoch=(len(x)+c['batch_size']-1)//c['batch_size'],
            interrupted=interrupted,smoke=smoke,seconds=time.perf_counter()-started,
            termination='interrupted' if interrupted else 'early_stopping' if bad>=c['patience'] else 'epoch_limit'))
        from Evaluator.report import learning_report
        learning_report(out,history,manifest)
        verify_hashes(inputs);verify_hashes(implementation);finish(out,manifest,not interrupted)
    except BaseException as e:
        finish(out,manifest,False,e);raise
    return out


def load_trained(folder):
    folder,m=load_artifact(folder,'policy_training')
    normalizer=read(folder/'normalizer.json');model=Policy(normalizer)
    model.load_state_dict(torch.load(folder/'best.pt',map_location='cpu',weights_only=True)['model_state_dict']);model.eval()
    return folder,m,normalizer,model


def network_json(model, normalizer):
    layers=[]
    for layer in model.net:
        if isinstance(layer,nn.Linear):layers.append(dict(weight=layer.weight.detach().tolist(),bias=layer.bias.detach().tolist()))
    return dict(schema_version=1,feature_names=FIELDS,mean=model.mean.tolist(),std=model.std.tolist(),layers=layers,
                throttle_scale=.20,brake_scale=.60)


def export(run):
    folder,m,norm,model=load_trained(run)
    if m['smoke']:raise ValueError('Smoke training cannot be exported for live evaluation')
    dataset,dm=load_artifact(m['dataset'],'teacher_dataset')
    if not dm['training_ready']:raise ValueError('Independent teacher splits required')
    from Evaluator.evaluate import metrics
    out=new_folder(m['config']['export_dir'],'policy')
    manifest=dict(schema_version=1,kind='policy_export',complete=False,eligible_for_live=False,
        training_run=str(folder),training_manifest_sha256=digest(folder/'manifest.json'),
        prepared=dm['prepared'],prepared_sha256=dm['prepared_sha256'],
        feature_names=FIELDS,feature_units=UNITS,output_transform=dict(nonnegative='throttle=0.20*u; brake=0',negative='throttle=0; brake=-0.60*u'),
        conditions=dict(car='bmw_m4_gt3',track='brands_hatch',target_speed_kmh=10,max_speed_kmh=20),
        inference_implementation_sha256=hashes([policy_binary()]),
        input_sha256=hashes([folder/'manifest.json',folder/'best.pt',folder/'normalizer.json',dataset/'manifest.json']))
    save(out/'manifest.json',manifest)
    try:
        save(out/'model.json',network_json(model,norm));manifest['model_sha256']=digest(out/'model.json')
        evals={};all_x=[];all_predictions=[]
        for split in ['validation','test']:
            data=rows(dataset/(split+'.jsonl'));x,_,_=tensors(data,norm)
            with torch.inference_mode():p=model(x).tolist()
            evals[split]=metrics(data,p,norm['baseline_u']);all_x.extend(x.tolist());all_predictions.extend(p)
        save(out/'evaluations.json',evals);manifest['evaluations']=evals
        # Explicit parity-only command accepts incomplete artifacts; live loader never does.
        save(out/'manifest.json',manifest,replace=True)
        with (out/'parity-input.jsonl').open('x',encoding='utf-8') as f:
            for x in all_x:f.write(json.dumps(x)+'\n')
        subprocess.run([str(policy_binary()),'predict-candidate',str(out),str(out/'parity-input.jsonl'),str(out/'parity-output.jsonl')],check=True,capture_output=True,text=True)
        rust=[float(line) for line in (out/'parity-output.jsonl').read_text().splitlines()]
        if len(rust)!=len(all_predictions):raise ValueError('Parity row mismatch')
        delta=max(abs(a-b) for a,b in zip(rust,all_predictions))
        manifest['parity_max_abs']=delta
        manifest['eligible_for_live']=delta<=1e-5 and all(e['passed'] for e in evals.values())
        from Evaluator.report import evaluation_report
        evaluation_report(out,evals,'validation/test frozen-checkpoint evaluation')
        verify_hashes(manifest['input_sha256']);verify_hashes(manifest['inference_implementation_sha256']);finish(out,manifest)
    except BaseException as e:
        finish(out,manifest,False,e);raise
    return out
