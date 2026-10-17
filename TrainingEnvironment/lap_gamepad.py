"""Whole-lap split for raw gamepad demonstrations; no history crosses lap splits."""
import json
import tempfile
from pathlib import Path
from DrivingRecorder.validate_recording import validate
from ModelTrainer.common import read,digest,hashes
from ModelTrainer.dataset import policy_binary

def settings_json(path):
    b=Path(path).read_bytes()
    return json.loads(b.decode('utf-16') if b.startswith(b'\xff\xfe') or b[1:2]==b'\0' else b.decode('utf-8-sig'))

def equivalent_controls(recording,profile):
    reference=profile.get('controls_reference')
    if not reference or digest(reference)!=profile['input_sha256'][profile['controls']]:return False
    def normalized(x):
        x=dict(x)
        x['commandDevices']=sorted(x['commandDevices'],key=lambda d:json.dumps(d,sort_keys=True))
        return x
    return normalized(settings_json(Path(recording)/'controls.snapshot.json'))==normalized(settings_json(reference))

def select(c):
    entries=c['laps']
    keys=[(str(Path(e['recording']).resolve()),e['lap_index']) for e in entries]
    if len(keys)!=len(set(keys)):raise ValueError('Duplicate/overlapping selected lap')
    if [e['split'] for e in entries].count('validation')!=1 or entries[-1]['split']!='validation':raise ValueError('Final selected lap must be sole validation lap')
    if any(e['split'] not in ('train','validation') for e in entries):raise ValueError('Invalid split')
    reports={};selected=[]
    for e in entries:
        path=Path(e['recording']).resolve()
        if path not in reports:reports[path]=validate(path)
        proof=reports[path]
        if not proof['format_pass']:raise ValueError(proof['errors'])
        index=e['lap_index']
        if type(index)!=int or not 1<=index<=len(proof['laps']):raise ValueError('Invalid lap index')
        lap=proof['laps'][index-1]
        if not lap['all_samples_lap_valid']:raise ValueError('Invalid lap cannot train')
        selected.append((path,lap,e['split'],index))
    return selected

def prepare(c,config):
    if c.get('prepared_examples_model'):return cached_prepare(c,config)
    from .gamepad import prepare_recording
    chosen=select(c)
    paths=[Path(config),Path(__file__),Path(__file__).with_name('gamepad.py'),Path(__file__).with_name('human_imitation.py'),Path('DrivingRecorder/validate_recording.py'),Path('DrivingRecorder/laps.py'),policy_binary(),Path(c['prepared'])/'manifest.json',Path(c['prepared'])/'heading_profile.json',Path(c['speed_profile'])]
    for d,_,_,_ in chosen:
        paths += [d/n for n in ('metadata.json','summary.json','events.csv','telemetry.csv','inputs.csv','controls.snapshot.json','assists.snapshot.json')]
        pr=read(d/'metadata.json')['input_profile']
        if pr.get('controls_reference'):paths.append(Path(pr['controls_reference']))
    inventory=hashes(list(dict.fromkeys(paths)));training=[];validation=[];profiles=[];counts=[];names=None
    with tempfile.TemporaryDirectory(prefix='poleposition-lap-training-') as tmp:
        for number,(d,lap,split,index) in enumerate(chosen):
            print(f'Preparing lap {number+1}/{len(chosen)}: {d.name} lap {index} ({split})',flush=True)
            dest=Path(tmp)/str(number);dest.mkdir()
            ex,nn,profile=prepare_recording(d,c['prepared'],c['speed_profile'],dest,lap=lap,feature_transform=c['feature_transform'])
            if names is not None and nn!=names:raise ValueError('Feature names differ')
            names=nn;profiles.append(profile)
            for e in ex:e.update(recording=str(d),lap_index=index)
            (training if split=='train' else validation).extend(ex)
            counts.append(dict(recording=str(d),lap_index=index,split=split,examples=len(ex),**lap))
    signatures={(p['input_sha256'][p['controls']],p['input_sha256'][p['assists']],p['steam_input_disabled']) for p in profiles}
    if len(signatures)!=1:raise ValueError('Teacher settings mismatch')
    m=dict(schema_version=1,kind='gamepad_imitation',semantics='gamepad_v1',complete=False,teacher_source='raw_gamepad_laps',initialization='random_seed_42',normalization='eight training laps only',recordings=list(dict.fromkeys(str(d) for d,_,_,_ in chosen)),teacher_laps=counts,prepared=str(Path(c['prepared']).resolve()),input_sha256=inventory,feature_transform=c['feature_transform'],history_reset_per_lap=True,validation_scope='held-out lap in same continuous session; not independent session')
    if c.get('hotlap_train_runs'):
        from .fresh_gamepad import prepare as prepare_hotlap
        hot_config=dict(c,train_runs=c['hotlap_train_runs'],validation_run=c['hotlap_validation_run'])
        hot_train,hot_validation,hot_names,hot_profiles,hot_inventory,hot_manifest=prepare_hotlap(hot_config,config)
        if hot_names!=names:raise ValueError('Hotlap/lap feature schemas differ')
        hot_signatures={(p['input_sha256'][p['controls']],p['input_sha256'][p['assists']],p['steam_input_disabled']) for p in hot_profiles}
        if hot_signatures!=signatures:raise ValueError('Hotlap/lap settings differ')
        for e in hot_train:e['source_kind']='hotlap_teacher'
        training.extend(hot_train)
        inventory.update(hot_inventory)
        m.update(hotlap_teacher_runs=hot_manifest['teacher_runs'],normalization='eight laps plus five hotlap teacher runs; training only',hotlap_validation_examples=len(hot_validation),hotlap_validation_usage='not trained; separate diagnostic only',sampling='uniform over combined training examples',input_sha256=inventory)
    return training,validation,names,profiles,inventory,m


def cached_prepare(c,config):
    """Reuse unchanged raw feature examples; normalization/weights are recomputed."""
    from ModelTrainer.common import verify_hashes
    source=Path(c['prepared_examples_model']).resolve();m=read(source/'manifest.json')
    verify_hashes(m['output_sha256'],source)
    if not m['complete'] or m['feature_transform']!=c['feature_transform'] or Path(m['prepared']).resolve()!=Path(c['prepared']).resolve():raise ValueError('Cached feature specification mismatch')
    # Source code is revised for this experiment; immutable source data must match.
    data_inventory={p:h for p,h in m['input_sha256'].items() if Path(p).suffix!='.py'}
    verify_hashes(data_inventory)
    selected=select(c)
    expected=[(str(p),index,split,lap['start_elapsed_ns'],lap['end_elapsed_ns']) for p,lap,split,index in selected]
    actual=[(r['recording'],r['lap_index'],r['split'],r['start_elapsed_ns'],r['end_elapsed_ns']) for r in m['teacher_laps']]
    if expected!=actual:raise ValueError('Cached lap split changed')
    hot=m.get('hotlap_teacher_runs',[])
    if [str(Path(p).resolve()) for p in c.get('hotlap_train_runs',[])]+[str(Path(c['hotlap_validation_run']).resolve())]!=[r['run'] for r in hot]:raise ValueError('Cached hotlap split changed')
    inventory=dict(data_inventory)
    inventory.update(hashes([source/'manifest.json',source/'training-examples.json',source/'validation-examples.json',Path(config),Path(__file__),Path(__file__).with_name('gamepad.py')]))
    profile=read(selected[0][0]/'metadata.json')['input_profile'];names=read(source/'model.json')['feature_names']
    manifest=dict(m)
    for k in ['output_sha256','parity_passed','excluded_input_features','error']:manifest.pop(k,None)
    manifest.update(complete=False,input_sha256=inventory,prepared_examples_source=str(source),initialization='random_seed_42',normalization='combined training examples only; recomputed')
    return read(source/'training-examples.json'),read(source/'validation-examples.json'),names,[profile],inventory,manifest
