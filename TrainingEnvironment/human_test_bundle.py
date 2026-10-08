"""Export a calibrated human imitation model for the explicit human trial mode."""
import csv
import json
from pathlib import Path
import shutil
import subprocess
import numpy as np
import torch
from stable_baselines3 import SAC
from ModelTrainer.common import load_artifact, new_folder, save, hashes, finish, digest
from ModelTrainer.dataset import policy_binary


def export(source, calibration_run, parent='reinforcement_runs'):
    source, source_manifest = load_artifact(source, 'human_raw_imitation')
    calibration_run = Path(calibration_run).resolve()
    load_artifact(calibration_run, 'human_steering_calibration')
    evidence=json.loads((calibration_run/'mapping.json').read_text())
    if evidence['passed'] is not True or evidence['coverage'] is not True:
        raise ValueError('Calibration must cover the demonstrated steering range')
    gain=evidence['gain']
    if not 1 <= gain <= 3: raise ValueError('Invalid calibrated gain')
    examples=[json.loads(s) for s in (source/'train.jsonl').read_text().splitlines()]
    throttle_limit=max(max(0.,r['action'][1]) for r in examples)
    out=new_folder(parent,'human-test-bundle')
    binary=policy_binary()
    m=dict(schema_version=1,kind='human_test_bundle',complete=False,scope='five_second_limited_control',
           steering_raw_per_axis=gain,prepared_sha256=digest(Path(source_manifest['prepared'])/'manifest.json'),
           human_throttle_limit=throttle_limit,
           input_sha256=hashes([source/'manifest.json',source/'model.zip',source/'normalizer.json',
               source/'speed-profile.json',source/'train.jsonl',calibration_run/'manifest.json',
               calibration_run/'mapping.json',Path(__file__),binary]),
           source_model=str(source),eligible_for_unrestricted_drive=False)
    save(out/'manifest.json',m)
    try:
        model=SAC.load(source/'model.zip',device='cpu');torch.set_num_threads(1)
        norm=json.loads((source/'normalizer.json').read_text())
        def layer(l):return dict(weight=l.weight.detach().numpy().tolist(),bias=l.bias.detach().numpy().tolist())
        graph=list(model.actor.latent_pi)
        network=dict(schema_version=1,mean_network=dict(schema_version=2,feature_names=norm['feature_names'],
            action_names=['steering','signed_acceleration'],mean=norm['mean'],std=norm['std'],
            layers=[layer(graph[0]),layer(graph[2]),layer(model.actor.mu)]),
            log_std=dict(weight=np.zeros((2,128)).tolist(),bias=[-4.,-4.]),log_std_bounds=[-5.,-2.])
        save(out/'model.json',network)
        for name in ('normalizer.json','speed-profile.json'):shutil.copyfile(source/name,out/name)
        examples=[json.loads(s)['features'] for s in (source/'train.jsonl').read_text().splitlines()]
        probes=np.asarray(examples,np.float32)[np.linspace(0,len(examples)-1,128,dtype=int)]
        eps=np.random.default_rng(42).standard_normal((128,2)).astype(np.float32);eps[:64]=0
        with torch.inference_mode():
            z=(probes-np.asarray(norm['mean'],np.float32))/np.asarray(norm['std'],np.float32)
            mu=model.actor.mu(model.actor.latent_pi(torch.from_numpy(z)))
            expected=torch.tanh(mu+np.exp(-4)*torch.from_numpy(eps)).numpy()
        with (out/'parity-input.jsonl').open('x') as f:
            for row,e in zip(probes,eps):f.write(json.dumps(dict(features=row.tolist(),epsilon=e.tolist()))+'\n')
        subprocess.run([str(binary),'predict-sac',str(out/'model.json'),str(out/'parity-input.jsonl'),str(out/'parity-output.jsonl')],check=True)
        actual=np.asarray([json.loads(s) for s in (out/'parity-output.jsonl').read_text().splitlines()])
        delta=float(np.abs(actual-expected).max())
        if not np.isfinite(actual).all() or delta>1e-5:raise ValueError('Native parity failed')
        save(out/'parity.json',dict(passed=True,samples=128,max_absolute_error=delta))
        save(out/'steering-evidence.json',evidence)
        finish(out,m)
    except BaseException as error:
        finish(out,m,False,error);raise
    print(out)
    return out

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('calibration_run')
    a=p.parse_args();export(a.source,a.calibration_run)
