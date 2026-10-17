"""Rebuild the dataset without labels overlapping excluded interval boundaries."""
from pathlib import Path
import json,shutil,tomllib,gc
import numpy as np
from ModelTrainer.common import read,save,hashes,verify_hashes,new_folder,finish
from .augmentation_experiment import BASE,NOISE,write_config,evaluate
from .gamepad import train


def main():
    source=Path('reinforcement_runs/augmentation-comparison-1792232268882390000-bae10bd2/dataset')
    meta=read(source/'manifest.json');verify_hashes(meta['output_sha256'],source)
    # Code changed to fix boundary filtering; original recording/config inputs must not change.
    verify_hashes({k:v for k,v in meta['input_sha256'].items() if not k.endswith('.py')})
    out=new_folder('reinforcement_runs','augmentation-comparison');dataset=out/'dataset';dataset.mkdir()
    print('EXPERIMENT',out,flush=True)
    shutil.copytree(source/'audit',dataset/'audit')
    for p in source.parent.glob('gamepad-*-teacher-comparison.json'):shutil.copyfile(p,out/p.name)
    p=source.parent/'corner-9-13s-matched-review.json'
    if p.exists():shutil.copyfile(p,out/p.name)
    rows=read(source/'training-pool.json');audit={p.stem:read(p) for p in (dataset/'audit').glob('*.json')}
    def overlap(r):
        return any(r['action_read_start_ns']<=b and r['action_read_end_ns']>=a for _,a,b in audit[Path(r['recording']).name]['exclusion_windows'])
    removed=[r for r in rows if overlap(r)];clean=[r for r in rows if not overlap(r)]
    save(dataset/'removed-boundary-labels.json',[dict(recording=r['recording'],sequence=r['sequence'],observation_ns=r['observation_ns'],action_read_start_ns=r['action_read_start_ns'],action_read_end_ns=r['action_read_end_ns'],reason='action_read_overlaps_exclusion') for r in removed])
    assert len(removed)==3
    save(dataset/'training-pool.json',clean);shutil.copyfile(source/'validation.json',dataset/'validation.json')
    a=[r for r in clean if 0 in r['phases_ms']];raw=np.array([r['features'] for r in a],dtype=np.float32)
    mean=raw.mean(0);std=raw.std(0);std[std<1e-6]=1
    save(dataset/'normalization.json',dict(mean=mean.tolist(),std=std.tolist(),source='A curated originals after interval-boundary fix',count=len(a)))
    inventory=hashes([Path(k) for k in meta['input_sha256']]+[Path(__file__),source/'manifest.json',source/'training-pool.json',source/'validation.json'])
    draws=len(a)
    meta.update(input_sha256=inventory,a_samples=len(a),b_samples=len(clean),boundary_revision=True)
    finish(dataset,meta)
    save(source.parent/'superseded.json',dict(reason='3 action-read intervals overlapped exclusion windows',replacement=str(out),eligible_for_selection=False))
    del a,raw,clean,rows;gc.collect()
    c=tomllib.loads(Path('TrainingEnvironment/session-teacher-20261017.toml').read_text())
    models={'existing':BASE}
    for variant in ['A','B','C']:
        cfg=dict(c,augmentation_dataset=str(dataset),variant=variant,shared_normalization=str(dataset/'normalization.json'),weighted_sampling=True,epoch_samples=draws)
        if variant=='C':cfg['noise']=NOISE
        path=out/(variant+'.toml');write_config(path,cfg)
        models[variant]=train(path,on_progress=lambda e:print(json.dumps(dict(variant=variant,**e)),flush=True))
        save(out/(variant+'-model.json'),dict(path=str(models[variant])))
        gc.collect()
    summaries=[read(models[k]/'summary.json') for k in ['A','B','C']]
    assert len({s['optimizer_updates'] for s in summaries})==1
    assert len({s['initial_weights_sha256'] for s in summaries})==1
    nets=[read(models[k]/'model.json') for k in ['A','B','C']]
    assert all(n['mean']==nets[0]['mean'] and n['std']==nets[0]['std'] for n in nets)
    for model in models.values():verify_hashes(read(model/'manifest.json')['output_sha256'],model)
    report=evaluate(out,dict(models,persistence=None),dataset)
    template=Path('GamepadIO/session-model-test-5s.toml').read_text()
    presets=[]
    for name,model in models.items():
        for seconds in (5,15,30):
            config=Path('GamepadIO')/f'augmentation-{name}-{seconds}s.toml'
            config.write_text(template.replace(str(BASE).replace('\\','/'),str(model).replace('\\','/')).replace('seconds = 5',f'seconds = {seconds}'),encoding='utf-8')
            preset=Path('TrainingController')/f'augmentation-{name}-{seconds}s-preset.json'
            save(preset,{'task':'게임패드 교정 주행','config':str(config.resolve())},replace=preset.exists());presets.append(str(preset))
    save(out/'result.json',dict(models={k:str(v) for k,v in models.items()},selected_candidate=report['selected_candidate'],presets=presets,live_tests_pending=True))
    print('COMPLETED',out,flush=True)

if __name__=='__main__':main()
