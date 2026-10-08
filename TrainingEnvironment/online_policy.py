"""Frozen, bounded SAC policies and independently verified native exports."""
from pathlib import Path
import json
import shutil
import subprocess
import numpy as np
import torch
from stable_baselines3 import SAC
from stable_baselines3.sac.policies import Actor, SACPolicy
from stable_baselines3.common.logger import configure
from ModelTrainer.common import read, save, digest, hashes, verify_hashes, new_folder, finish
from ModelTrainer.dataset import policy_binary
from .pretrain import OfflineSpaces
from .checkpoint import validate_policy

class BoundedActor(Actor):
    def get_action_dist_params(self, obs):
        mu, log_std, kwargs = super().get_action_dist_params(obs)
        return mu, torch.clamp(log_std, -5., -2.), kwargs

class BoundedPolicy(SACPolicy):
    def make_actor(self, features_extractor=None):
        return BoundedActor(**self._update_features_extractor(self.actor_kwargs, features_extractor)).to(self.device)

def verified_outputs(folder, kind):
    """Historical code/binary hashes are provenance, not current executable requirements.

    Verify every immutable output before loading trusted local model serialization.
    Current prepared geometry and runtime are bound separately in the new bundle.
    """
    folder = Path(folder).resolve()
    m = read(folder/'manifest.json')
    if m.get('schema_version') != 1 or m.get('kind') != kind or m.get('complete') is not True:
        raise ValueError('Incomplete/wrong source artifact')
    verify_hashes(m['output_sha256'], folder)
    return folder, m

def bootstrap(training, prepared, parent):
    training, m = verified_outputs(training, 'sac_pretraining')
    dataset, dm = verified_outputs(m['dataset'], 'teacher_features_v2')
    prepared = Path(prepared).resolve()
    if (dm.get('feature_schema_version') != 2 or dm.get('policy_training_ready') is not True
        or not any(Path(p).resolve() == prepared/'manifest.json' and h == digest(prepared/'manifest.json')
                   for p,h in dm['input_sha256'].items())):
        raise ValueError('Pretraining and prepared geometry differ')
    norm = read(training/'normalizer.json')
    mean, std = np.asarray(norm['mean'],np.float32),np.asarray(norm['std'],np.float32)
    if mean.shape != (35,) or std.shape != (35,) or not np.isfinite(mean).all() or not np.isfinite(std).all() or np.any(std<=0):
        raise ValueError('Invalid frozen normalizer')
    env = OfflineSpaces(35)
    env.np_random=np.random.default_rng(42)
    env.action_space.seed(42); env.observation_space.seed(42)
    pretrained = SAC.load(training/'model.zip',device='cpu')
    validate_policy(pretrained)
    model = SAC(BoundedPolicy,env,policy_kwargs=dict(net_arch=[128,128],n_critics=2),buffer_size=200000,
                batch_size=256,learning_rate=.0003,gamma=.99,tau=.005,ent_coef='auto',target_entropy='auto',seed=42,device='cpu')
    model.policy.load_state_dict(pretrained.policy.state_dict())
    with torch.no_grad():
        model.actor.log_std.weight.zero_(); model.actor.log_std.bias.fill_(-4.)
    model.set_logger(configure(folder=None,format_strings=[]))
    out=new_folder(parent,'sac-bootstrap')
    meta=dict(schema_version=1,kind='sac_bootstrap',complete=False,prepared=str(prepared),
              source_training=str(training),historical_input_sha256=m['input_sha256'],
              input_sha256=hashes([training/'manifest.json',training/'model.zip',prepared/'manifest.json']))
    save(out/'manifest.json',meta)
    save(out/'normalizer.json',dict(feature_names=dm['feature_names'],mean=mean.tolist(),std=std.tolist()))
    shutil.copyfile(dataset/'speed-profile.json',out/'speed-profile.json')
    # Fixed real validation observations; never learn from these parity probes.
    probes=[]
    with (dataset/'validation.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            row=json.loads(line); probes.append(row['features'])
            if len(probes)==128: break
    if len(probes)<100: raise ValueError('Insufficient parity probes')
    save(out/'probes.json',probes)
    model.save(out/'model.zip')
    finish(out,meta)
    return out,model,env

def export_live(model, bootstrap_folder, parent):
    validate_policy(model)
    if not isinstance(model.actor,BoundedActor): raise ValueError('Online SAC requires bounded exploration')
    boot=Path(bootstrap_folder)
    norm=read(boot/'normalizer.json')
    raw=np.asarray(read(boot/'probes.json'),np.float32)
    mean,std=np.asarray(norm['mean'],np.float32),np.asarray(norm['std'],np.float32)
    graph=list(model.actor.latent_pi)
    def layer(v): return dict(weight=v.weight.detach().cpu().tolist(),bias=v.bias.detach().cpu().tolist())
    value=dict(schema_version=1,mean_network=dict(schema_version=2,feature_names=norm['feature_names'],
               action_names=['steering','signed_acceleration'],mean=mean.tolist(),std=std.tolist(),
               layers=[layer(graph[0]),layer(graph[2]),layer(model.actor.mu)]),
               log_std=layer(model.actor.log_std),log_std_bounds=[-5.,-2.])
    out=new_folder(parent,'sac-live-bundle')
    prepared=Path(read(boot/'manifest.json')['prepared'])
    binary=policy_binary()
    meta=dict(schema_version=1,kind='sac_live_bundle',complete=False,prepared_sha256=digest(prepared/'manifest.json'),
              bootstrap=str(boot.resolve()),input_sha256=hashes([boot/'manifest.json',prepared/'manifest.json']),
              runtime_sha256=digest(binary))
    save(out/'manifest.json',meta)
    try:
        save(out/'model.json',value)
        save(out/'normalizer.json',norm)
        shutil.copyfile(boot/'speed-profile.json',out/'speed-profile.json')
        eps=np.random.default_rng(42).standard_normal((len(raw),2)).astype(np.float32)
        eps[:len(eps)//2]=0
        model.policy.set_training_mode(False)
        with torch.inference_mode():
            mu,log_std,_=model.actor.get_action_dist_params(torch.from_numpy((raw-mean)/std))
            expected=torch.tanh(mu+log_std.exp()*torch.from_numpy(eps)).numpy()
        with (out/'parity-input.jsonl').open('x',encoding='utf-8') as f:
            for x,e in zip(raw,eps): f.write(json.dumps(dict(features=x.tolist(),epsilon=e.tolist()),allow_nan=False)+'\n')
        subprocess.run([str(binary),'predict-sac',str(out/'model.json'),str(out/'parity-input.jsonl'),str(out/'parity-output.jsonl')],
                       check=True,capture_output=True,text=True)
        actual=np.asarray([json.loads(s) for s in (out/'parity-output.jsonl').read_text().splitlines()])
        if actual.shape != expected.shape or not np.isfinite(actual).all(): raise ValueError('Invalid native parity output')
        delta=float(np.max(np.abs(actual-expected)))
        save(out/'parity.json',dict(samples=len(raw),max_absolute_error=delta,tolerance=1e-5,passed=delta<=1e-5))
        if delta>1e-5: raise ValueError('SAC native parity failed')
        finish(out,meta)
    except BaseException as e:
        finish(out,meta,False,e); raise
    return out
