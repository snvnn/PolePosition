"""Offline SAC mean pretraining; critics and replay are never trained on teacher labels."""
import copy
from pathlib import Path
import tomllib

import gymnasium as gym
import numpy as np
from stable_baselines3 import SAC
import torch

from ModelTrainer.common import finish, hashes, load_artifact, new_folder, read, rows, save, verify_hashes
from .checkpoint import validate_policy
from .policy import export_candidate


class OfflineSpaces(gym.Env):
    def __init__(self, width):
        self.observation_space = gym.spaces.Box(-np.inf, np.inf, (width,), dtype=np.float32)
        self.action_space = gym.spaces.Box(-1., 1., (2,), dtype=np.float32)

    def reset(self, **kwargs):
        raise RuntimeError('Offline pretraining cannot collect driving experience')

    def step(self, action):
        raise RuntimeError('Offline pretraining cannot send vehicle inputs')


def config(path):
    c = tomllib.loads(Path(path).read_text(encoding='utf-8'))
    if set(c) != {'schema_version','max_epochs','patience','batch_size','learning_rate','seed',
                  'threads','output_dir','gamma','tau','ent_coef','target_entropy'}:
        raise ValueError('Invalid v2 pretraining configuration')
    if (c['schema_version'] != 2 or c['batch_size'] != 256 or c['learning_rate'] != .0003
            or c['seed'] != 42 or c['threads'] != 1 or c['ent_coef'] != 'auto'
            or c['target_entropy'] != 'auto' or not 0 < c['gamma'] <= 1 or not 0 < c['tau'] <= 1):
        raise ValueError('Unsupported SAC pretraining settings')
    for key in ('max_epochs','patience'):
        if type(c[key]) is not int or not 1 <= c[key] <= 200:
            raise ValueError('Epoch/patience must be in 1..200')
    return c


def load_dataset(folder):
    folder, manifest = load_artifact(folder, 'teacher_features_v2')
    if manifest.get('feature_schema_version') != 2 or manifest.get('policy_training_ready') is not True:
        raise ValueError('Require prepared v2 features, not action-only or v1 data')
    normalizer = read(folder/'normalizer.json')
    names = manifest['feature_names']
    if not names or len(set(names)) != len(names) or names != normalizer['feature_names']:
        raise ValueError('Normalizer schema mismatch')
    mean, std = (np.asarray(normalizer[k], dtype=np.float32) for k in ('mean','std'))
    if (mean.shape != (len(names),) or std.shape != mean.shape
            or not np.isfinite(mean).all() or not np.isfinite(std).all() or np.any(std <= 0)):
        raise ValueError('Invalid v2 normalizer')
    data, run_sets = {}, {}
    for split in ('train','validation','test'):
        records = rows(folder/f'{split}.jsonl')
        x = np.asarray([r['features'] for r in records], dtype=np.float32)
        y = np.asarray([r['action'] for r in records], dtype=np.float32)
        if (x.shape != (len(records),len(names)) or y.shape != (len(records),2) or not len(records)
                or not np.isfinite(x).all() or not np.isfinite(y).all() or np.any(np.abs(y) > 1)):
            raise ValueError('Invalid v2 examples')
        run_sets[split] = {r['run_id'] for r in records}
        expected = {Path(p).name for p in manifest['source_splits'][split]}
        if run_sets[split] != expected:
            raise ValueError('Example source split mismatch')
        normalized = (x-mean)/std
        if not np.isfinite(normalized).all():
            raise ValueError('Nonfinite normalized v2 data')
        data[split] = (torch.from_numpy(normalized), torch.from_numpy(y), x)
    if any(run_sets[a] & run_sets[b] for a,b in [('train','validation'),('train','test'),('validation','test')]):
        raise ValueError('Run crosses split boundaries')
    return folder, manifest, normalizer, data


def train(dataset, config_path, resume=None, *, epoch_limit=None, should_stop=lambda: False,
          on_progress=lambda _: None):
    c = config(config_path)
    checkpoint = None
    if resume:
        resume = Path(resume).resolve()
        _, previous = load_artifact(resume.parent, 'sac_pretraining', complete=False)
        if resume.name != 'last.pt' or 'last.pt' not in previous['output_sha256']:
            raise ValueError('Select a verified last.pt pretraining checkpoint')
        if c != previous['config']:
            raise ValueError('Resume configuration differs from original run')
        dataset = previous['dataset']
        checkpoint = torch.load(resume, map_location='cpu', weights_only=True)
    folder, dm, normalizer, data = load_dataset(dataset)
    inputs = dict(dm['input_sha256']) | hashes([folder/'manifest.json', config_path, Path(__file__)])
    inputs.update({str(folder/k):v for k,v in dm['output_sha256'].items()})
    if resume:
        inputs.update(hashes([resume, resume.parent/'manifest.json']))
    torch.set_num_threads(c['threads'])
    model = SAC('MlpPolicy', OfflineSpaces(len(dm['feature_names'])),
                policy_kwargs=dict(net_arch=[128,128], n_critics=2), buffer_size=200000,
                batch_size=256, learning_rate=.0003, seed=42, device='cpu',
                gamma=c['gamma'], tau=c['tau'], ent_coef=c['ent_coef'], target_entropy=c['target_entropy'])
    validate_policy(model)
    # A separate optimizer touches only the deterministic mean branch.
    parameters = list(model.actor.latent_pi.parameters()) + list(model.actor.mu.parameters())
    optimizer = torch.optim.Adam(parameters, lr=c['learning_rate'])
    generator = torch.Generator().manual_seed(c['seed'])
    start, best_epoch, stale = 0, 0, 0
    best_loss = float('inf')
    best = copy.deepcopy(model.policy.state_dict())
    history = []
    if checkpoint:
        model.policy.load_state_dict(checkpoint['policy'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        generator.set_state(checkpoint['shuffle_rng'])
        start, best_epoch, stale = checkpoint['epoch'], checkpoint['best_epoch'], checkpoint['stale']
        best_loss, best, history = checkpoint['best_loss'], checkpoint['best'], checkpoint['history']
    critic = copy.deepcopy(model.critic.state_dict())
    target = copy.deepcopy(model.critic_target.state_dict())
    out = new_folder(c['output_dir'], 'sac-pretraining')
    manifest = dict(schema_version=1, kind='sac_pretraining', complete=False, input_sha256=inputs,
                    dataset=str(folder), config=c, feature_schema_version=2, eligible_for_live=False)
    save(out/'manifest.json', manifest)
    on_progress(dict(phase='조향·가감속 사전학습', result_dir=str(out)))
    x, y, _ = data['train']

    def metrics(split):
        xx, yy, _ = data[split]
        total = torch.zeros(2)
        with torch.inference_mode():
            for offset in range(0,len(xx),256):
                error = model.actor(xx[offset:offset+256], deterministic=True)-yy[offset:offset+256]
                total += error.square().sum(dim=0)
        return (total/len(xx)).tolist()

    def save_last(epoch):
        # Only complete epochs are resumable. Written atomically before inventory commit.
        temp = out/'last.pt.tmp'
        torch.save(dict(policy=model.policy.state_dict(), optimizer=optimizer.state_dict(),
                        shuffle_rng=generator.get_state(), epoch=epoch, best_epoch=best_epoch,
                        stale=stale, best_loss=best_loss, best=best, history=history), temp)
        temp.replace(out/'last.pt')

    try:
        max_epochs = c['max_epochs'] if epoch_limit is None else min(c['max_epochs'], epoch_limit)
        if type(max_epochs) is not int or max_epochs < 1:
            raise ValueError('Invalid epoch cap')
        save_last(start)
        stopped = False
        for epoch in range(start+1, max_epochs+1):
            if stale >= c['patience']:
                break
            for indices in torch.randperm(len(x), generator=generator).split(c['batch_size']):
                if should_stop():
                    stopped = True
                    break
                prediction = model.actor(x[indices], deterministic=True)
                loss = torch.nn.functional.mse_loss(prediction, y[indices])
                if not torch.isfinite(loss):
                    raise ValueError('Nonfinite pretraining loss')
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
            if stopped:
                break
            validation = metrics('validation')
            score = sum(validation)/2
            if score < best_loss:
                best_loss, best_epoch, stale = score, epoch, 0
                best = copy.deepcopy(model.policy.state_dict())
            else:
                stale += 1
            history.append(dict(epoch=epoch, validation_mse=validation))
            save_last(epoch)
            on_progress(dict(epoch=epoch, updates=epoch*((len(x)+255)//256), loss=score, result_dir=str(out)))
        if any(not torch.equal(v, model.critic.state_dict()[k]) for k,v in critic.items()) or any(
                not torch.equal(v, model.critic_target.state_dict()[k]) for k,v in target.items()):
            raise RuntimeError('Pretraining modified a critic')
        if model.replay_buffer.size() != 0:
            raise RuntimeError('Teacher labels entered replay')
        if stopped or should_stop():
            save(out/'summary.json', dict(termination='stopped', best_epoch=best_epoch, history=history))
            finish(out, manifest, False)
            return out
        model.policy.load_state_dict(best)
        model.save(out/'model.zip')
        save(out/'normalizer.json', normalizer)
        summary = dict(termination='patience' if stale >= c['patience'] else 'epoch_limit',
                       best_epoch=best_epoch, history=history,
                       validation_mse=metrics('validation'), test_mse=metrics('test'),
                       critic_unchanged=True, replay_samples=0)
        check_inputs = [folder/'manifest.json', out/'model.zip', out/'normalizer.json', config_path]
        candidate = export_candidate(model, dm['feature_names'], normalizer['mean'], normalizer['std'],
                                     data['validation'][2][:128], check_inputs, Path(c['output_dir']))
        summary['candidate'] = str(candidate)
        save(out/'summary.json', summary)
        verify_hashes(inputs)
        finish(out, manifest)
    except BaseException as error:
        finish(out, manifest, False, error)
        raise
    return out
