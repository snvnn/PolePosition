"""Verify success-augmented candidate against unchanged human validation and Rust."""
import argparse
import json
from pathlib import Path
import subprocess
import numpy as np
from ModelTrainer.common import read, save, hashes, digest, verify_hashes, new_folder, finish
from ModelTrainer.dataset import policy_binary
from .augmentation_experiment import predict
from .evaluate_steering_transition import evaluate


def main(candidate):
    candidate=Path(candidate).resolve();m=read(candidate/'manifest.json')
    if not m['complete'] or not m.get('success_selection'):raise ValueError('Incomplete success candidate')
    augmented=bool(m.get('cached_augmented_dataset'))
    base=Path(m.get('cached_teacher_model') or m['success_selection']['source_model']);bm=read(base/'manifest.json')
    verify_hashes(bm['output_sha256'],base);verify_hashes(m['output_sha256'],candidate)
    if digest(candidate/'validation-examples.json')!=digest(base/'validation-examples.json'):
        raise ValueError('Validation changed')
    training=read(candidate/'training-examples.json')
    original=read(Path(m['cached_augmented_dataset'])/'training-pool.json') if augmented else read(base/'training-examples.json')
    # A new field may mark the sampling weight, but no original label or feature may change.
    if len(training)!=len(original)+m['success_selection']['examples']:raise ValueError('Unexpected training rows')
    for a,b in zip(training,original):
        if (a if augmented else {k:v for k,v in a.items() if k!='weight'})!=b:raise ValueError('Original teacher data changed')
    del training,original
    models={'existing':base,'candidate':candidate}
    if augmented:
        models['previous_corner_candidate']=Path('reinforcement_runs/gamepad-imitation-1793422894431417900-f5e67723')
        models['previous_augmented_C']=Path(read(Path(m['cached_augmented_dataset']).parent/'C-model.json')['path'])
    result=evaluate(models)
    success=read(candidate/'successful-examples.json')
    sx=np.array([r['features'] for r in success],dtype=np.float32)
    sy=np.array([r['action'] for r in success])
    old=predict(base,sx);new=predict(candidate,sx)
    source_error=float(abs(old-sy).max())
    if source_error>1e-5:raise ValueError('Success labels do not reproduce original policy')
    result['success_training_fit']=dict(count=len(success),old_max_error=source_error,
        candidate_mse=((new-sy)**2).mean(0).tolist(),scope='Training fit to model-generated labels, not an independent driving result')
    from .steering_transition_diagnosis import RUNS
    probes=[sx]
    for run in RUNS:
        with (Path('actuator_runs')/run/'samples.jsonl').open() as f:
            rows=[r['features'] for line in f if (r:=json.loads(line))['kind']=='policy' and r['mode']=='Running']
        probes.append(np.array(rows,dtype=np.float32))
    probes=np.concatenate(probes)
    out=new_folder('reinforcement_runs','successful-corner-evaluation')
    with (out/'parity-input.jsonl').open('x') as f:
        for row in probes:f.write(json.dumps(row.tolist())+'\n')
    subprocess.run([str(policy_binary()),'predict-gamepad',str(candidate/'model.json'),str(out/'parity-input.jsonl'),str(out/'parity-output.jsonl')],check=True)
    actual=np.array([json.loads(s) for s in (out/'parity-output.jsonl').read_text().splitlines()])
    error=float(abs(actual-predict(candidate,probes)).max())
    if not np.isfinite(actual).all() or error>1e-5:raise ValueError('Expanded Rust parity failed')
    result.update(expanded_parity=dict(samples=len(probes),maximum_absolute_error=error,passed=True),
        original_model_preserved=True,original_teacher_rows_unchanged=True,validation_byte_identical=True,
        teacher_comparison_source=m.get('cached_augmented_dataset',str(base)),
        success_selection=m['success_selection'],auto_activated=False)
    save(out/'comparison.json',result)
    finish(out,dict(kind='successful_corner_evaluation',input_sha256=hashes([candidate/'manifest.json',candidate/'model.json',
        base/'manifest.json',base/'model.json',Path(__file__),Path(__file__).with_name('evaluate_steering_transition.py')]),
        candidate=str(candidate),base=str(base)))
    print('REPORT',out,flush=True);print(json.dumps(result['metrics']),flush=True)
    print('PARITY',result['expanded_parity'],flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('candidate');a=p.parse_args();main(a.candidate)
