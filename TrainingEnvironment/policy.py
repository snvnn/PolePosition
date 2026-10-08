"""Export a separate v2 SAC mean candidate and compare real Python/Rust outputs."""
import json
from pathlib import Path
import subprocess

import numpy as np
import torch

from ModelTrainer.common import digest, finish, hashes, new_folder, save, verify_hashes
from ModelTrainer.dataset import policy_binary
from .checkpoint import validate_policy


def export_candidate(model, feature_names, mean, std, observations, input_paths,
                     parent='reinforcement_runs'):
    validate_policy(model)
    width = model.observation_space.shape[0]
    if (len(feature_names) != width or len(set(feature_names)) != width
            or any(not isinstance(name,str) or not name.strip() for name in feature_names)):
        raise ValueError('An explicit unique v2 feature schema is required')
    mean, std = np.asarray(mean,dtype=np.float32), np.asarray(std,dtype=np.float32)
    raw = np.asarray(observations,dtype=np.float32)
    if (mean.shape != (width,) or std.shape != (width,) or raw.ndim != 2 or raw.shape[1] != width
            or raw.shape[0] == 0 or not np.isfinite(mean).all() or not np.isfinite(std).all()
            or np.any(std <= 0) or not np.isfinite(raw).all()):
        raise ValueError('Invalid explicit normalizer/parity observations')
    inputs = hashes(input_paths)
    verify_hashes(inputs)
    binary = policy_binary()
    inputs.update(hashes([binary]))
    graph = list(model.actor.latent_pi)
    expected_graph = (torch.nn.Linear,torch.nn.ReLU,torch.nn.Linear,torch.nn.ReLU)
    if len(graph) != 4 or any(not isinstance(layer,kind) for layer,kind in zip(graph,expected_graph)):
        raise ValueError('Unsupported SAC mean activation graph')
    linear = [graph[0],graph[2]]
    linear.append(model.actor.mu)
    layers = [dict(weight=layer.weight.detach().cpu().tolist(),bias=layer.bias.detach().cpu().tolist())
              for layer in linear]
    out = new_folder(parent,'sac-mean-candidate')
    manifest = dict(schema_version=1,kind='sac_mean_candidate',complete=False,eligible_for_live=False,
                    feature_schema_version=2,input_sha256=inputs,
                    limitations='Numeric parity candidate only; no driving/performance authorization')
    save(out/'manifest.json',manifest)
    try:
        save(out/'model.json',dict(schema_version=2,feature_names=list(feature_names),
                                  action_names=['steering','signed_acceleration'],mean=mean.tolist(),
                                  std=std.tolist(),layers=layers))
        manifest['model_sha256'] = digest(out/'model.json')
        save(out/'manifest.json',manifest,replace=True)
        normalized = (raw-mean)/std
        if not np.isfinite(normalized).all():
            raise ValueError('Nonfinite normalized observations')
        model.policy.set_training_mode(False)
        with torch.inference_mode():
            expected = model.actor(torch.from_numpy(normalized),deterministic=True).cpu().numpy()
        with (out/'parity-input.jsonl').open('x',encoding='utf-8',newline='\n') as stream:
            for row in raw: stream.write(json.dumps(row.tolist(),allow_nan=False)+'\n')
        subprocess.run([str(binary),'predict-v2-candidate',str(out),str(out/'parity-input.jsonl'),
                        str(out/'parity-output.jsonl')],check=True,capture_output=True,text=True)
        actual = np.asarray([json.loads(line) for line in (out/'parity-output.jsonl').read_text().splitlines()])
        if actual.shape != expected.shape or not np.isfinite(actual).all():
            raise ValueError('Rust v2 parity inventory mismatch')
        delta = float(np.max(np.abs(actual-expected)))
        save(out/'parity.json',dict(samples=len(raw),actions=2,max_absolute_error=delta,
                                    tolerance=1e-5,passed=delta<=1e-5))
        manifest['parity_max_abs'] = delta
        if delta > 1e-5:
            raise ValueError('SAC Python/Rust mean output parity failed')
        verify_hashes(inputs)
        finish(out,manifest)
    except BaseException as error:
        finish(out,manifest,False,error)
        raise
    return out
