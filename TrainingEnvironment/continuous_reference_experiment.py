"""Rebuild geometry-dependent training evidence; retain human splits and actions."""
import csv,json,gc,subprocess
from pathlib import Path
import numpy as np
from ModelTrainer.common import read,save,hashes,finish,new_folder,verify_hashes,digest
from ModelTrainer.dataset import policy_binary
from TrackModel.model import load_reference
from .continuous_reference import fast_project
from .augmentation_experiment import write_config,NOISE

OLD_DATA=Path('reinforcement_runs/augmentation-comparison-1792233044576944100-923b5038/dataset')
PREPARED=Path('state_runs/continuous-seam-prepared-1793424813725418100-6c34e4b4').resolve()


def build():
    from DrivingRecorder.validate_recording import validate
    from .gamepad import prepare_recording
    meta=read(OLD_DATA/'manifest.json');verify_hashes(meta['output_sha256'],OLD_DATA)
    evidence={k:v for k,v in meta['input_sha256'].items() if Path(k).suffix not in ('.py','.exe')};verify_hashes(evidence)
    pm=read(PREPARED/'manifest.json');ref=load_reference(pm['track_model_path']);length=ref['manifest']['total_length_m'];heading=read(PREPARED/'heading_profile.json')
    out=new_folder('reinforcement_runs','continuous-reference');print('EXPERIMENT',out,flush=True)
    source_paths=[Path(p) for p in meta['model_manifest']['recordings']]
    grid=np.linspace(0,length,round(length/5),endpoint=False);profiles=[];lap_inventory=[];projections={}
    for path in source_paths[:-1]:
        proof=validate(path)
        if not proof['format_pass']:raise ValueError(proof['errors'])
        with (path/'telemetry.csv').open(newline='') as f:rows=list(csv.DictReader(f))
        geometry=fast_project(rows,ref['rows'],length,heading,True);projections[str(path)]=geometry
        times=np.array([int(r['elapsed_ns']) for r in rows]);segments=np.array([int(r['segment_id']) for r in rows]);speeds=np.array([float(r['speed_kmh']) for r in rows])
        count=0
        for ordinal,lap in enumerate(proof['laps'],1):
            if not lap['all_samples_lap_valid']:continue
            ids=np.where((segments==lap['segment_id'])&(times>=lap['start_elapsed_ns'])&(times<lap['end_elapsed_ns']))[0]
            if not len(ids) or min(speeds[ids])<10 or min(speeds[ids[0]],speeds[ids[-1]])<100:continue
            bins=np.minimum((geometry[ids,0]/length*len(grid)).astype(int),len(grid)-1)
            known=np.unique(bins)
            if len(known)<.98*len(grid):raise ValueError('Continuous speed lap coverage too low')
            medians=np.array([np.median(speeds[ids[bins==j]]) for j in known])
            profiles.append(np.interp(grid,grid[known],medians,period=length));count+=1
            lap_inventory.append(dict(recording=str(path),lap_index=ordinal,**lap))
        print('Speed laps',path.name,count,flush=True)
        del rows,geometry;gc.collect()
    if len(profiles)<5:raise ValueError('Insufficient continuous training laps')
    speed=np.median(profiles,axis=0)
    # Symmetric periodic five-bin smoothing: the finish line is an ordinary interior point.
    speed=sum(w*np.roll(speed,k) for k,w in zip(range(-2,3),[1,2,3,2,1]))/9
    if abs(speed[0]-speed[-1])>5:raise ValueError('Speed seam remains discontinuous')
    speed_file=out/'speed-profile.json';save(speed_file,dict(schema_version=2,length_m=length,s_m=grid.tolist(),speed_kmh=speed.tolist()))
    save(out/'speed-source-laps.json',dict(laps=lap_inventory,aggregation='equal-lap median per ~5m bin, periodic [1,2,3,2,1]/9 smoothing',validation_used=False))
    del projections;gc.collect()
    # Regenerate all causal histories from original 100Hz records, then retain
    # the original sample IDs when available. No copied old feature values.
    old_train=read(OLD_DATA/'training-pool.json');old_val=read(OLD_DATA/'validation.json')
    dataset=out/'dataset';dataset.mkdir();training=[];validation=[];removed=[];profiles_meta=[];counts=[]
    for number,path in enumerate(source_paths):
        is_train=number<len(source_paths)-1
        dest=out/f'session-{number+1}';dest.mkdir()
        print('Rebuilding features',number+1,len(source_paths),path.name,flush=True)
        ex,names,profile=prepare_recording(path,PREPARED,speed_file,dest,feature_transform='original',include_invalid=is_train,
            augmentation=meta['augmentation'] if is_train else None,fast_projection=True)
        profiles_meta.append(profile)
        # Features for every valid observation, independent of phase selection.
        with (dest/'features.jsonl').open() as f:
            rebuilt={r['sequence']:r for line in f if (r:=json.loads(line))['features'] is not None}
        original=[r for r in (old_train if is_train else old_val) if r['recording']==str(path)]
        selected=[]
        from .augmentation import sample_weight
        for row in original:
            fresh=rebuilt.get(row['sequence'])
            if fresh is None:
                removed.append(dict(recording=str(path),sequence=row['sequence'],split='train' if is_train else 'validation',reason='new causal history not ready'))
                continue
            if fresh['observation_ns']!=row['observation_ns']:raise ValueError('Rebuilt timestamp mismatch')
            item=dict(row,features=fresh['features'])
            if is_train:item['weight']=sample_weight(item['features'],item['action'],meta['augmentation'])
            selected.append(item)
        (training if is_train else validation).extend(selected)
        counts.append(dict(recording=str(path),split='train' if is_train else 'validation',examples=len(selected)))
        print('Rebuilt',len(selected),'examples',flush=True)
        del ex,rebuilt,selected,original;gc.collect()
    a=[r for r in training if 0 in r['phases_ms']];raw=np.array([r['features'] for r in a],dtype=np.float32)
    mean=raw.mean(0);std=raw.std(0);std[std<1e-6]=1
    save(dataset/'normalization.json',dict(mean=mean.tolist(),std=std.tolist(),source='curated human training phase 0; repaired geometry/continuous profile',count=len(a)))
    save(dataset/'training-pool.json',training);save(dataset/'validation.json',validation);save(dataset/'removed-boundary-labels.json',removed)
    paths=[Path(__file__),Path(__file__).with_name('continuous_reference.py'),Path(__file__).with_name('gamepad.py'),OLD_DATA/'manifest.json',
           OLD_DATA/'training-pool.json',OLD_DATA/'validation.json',PREPARED/'manifest.json',speed_file,out/'speed-source-laps.json']
    inventory=dict(evidence);inventory.update(hashes(paths));inventory.update(pm['input_sha256']);inventory.update(hashes([PREPARED/n for n in ('config.json','heading_profile.json','replay.json')]))
    model_manifest=dict(meta['model_manifest'],prepared=str(PREPARED),feature_transform='original',geometry_revision=pm['geometry_revision'],
        teacher_sessions=counts,normalization='curated human training only; repaired geometry',input_sha256=inventory)
    new_meta=dict(input_sha256=inventory,feature_names=names,profiles=profiles_meta,model_manifest=model_manifest,augmentation=meta['augmentation'],
        a_samples=len(a),b_samples=len(training),validation_samples=len(validation),boundary_revision=True,reprojected=True)
    finish(dataset,new_meta)
    config=dict(session_teacher_recordings=True,session_split='input_profiles/gamepad/teacher-cleaned-split-20261017.json',exclude_engine_state=True,
        exclude_longitudinal_response=True,exclude_previous_actions=True,feature_transform='original',prepared=str(PREPARED),speed_profile=str(speed_file),
        epochs=50,output_dir='reinforcement_runs',cached_augmented_dataset=str(dataset),shared_normalization=str(dataset/'normalization.json'),
        weighted_sampling=True,epoch_samples=len(a),noise=NOISE,
        successful_rollouts=dict(archive='reinforcement_runs/autonomous-two-laps-1793422357204916300-c70c929b',
            source_model='reinforcement_runs/gamepad-imitation-1792231181059399600-43934c30',laps=[1,2],full_laps=True,sampling_fraction=.1,reproject=True))
    write_config(out/'train.toml',config)
    print('DATASET_READY',out,flush=True)
    return out


if __name__=='__main__':build()
