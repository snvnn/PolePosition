"""Whole-session human demonstrations with an independent held-out session."""
from pathlib import Path
import tempfile
import shutil
from ModelTrainer.common import read, hashes, digest
from ModelTrainer.dataset import policy_binary


def prepare(c, config):
    from .gamepad import prepare_recording
    if c.get('cached_augmented_dataset'):
        from .cached_augmentation import prepare as cached_augmented
        return cached_augmented(c,config)
    if c.get('cached_teacher_model'):
        return prepare_cached(c, config)
    if c.get('augmentation_dataset'):
        from ModelTrainer.common import verify_hashes
        folder=Path(c['augmentation_dataset']); meta=read(folder/'manifest.json')
        assert meta['complete']
        verify_hashes(meta['output_sha256'],folder);verify_hashes(meta['input_sha256'])
        data=read(folder/'training-pool.json')
        if c['variant']=='A': data=[r for r in data if 0 in r['phases_ms']]
        validation=read(folder/'validation.json')
        inventory=dict(meta['input_sha256']);inventory.update(hashes([folder/'manifest.json',folder/'training-pool.json',folder/'validation.json',Path(config),Path(c['shared_normalization'])]))
        m=dict(meta['model_manifest'],input_sha256=inventory,augmentation_variant=c['variant'],augmentation_settings=meta['augmentation'],noise=c.get('noise'),normalization='shared A curated original samples',validation_scope='held-out session reused for model selection; not final test')
        return data,validation,meta['feature_names'],meta['profiles'],inventory,m
    split = read(c['session_split'])
    training_paths = [Path(p).resolve() for p in split['training_sessions']]
    validation_path = Path(split['validation_session']).resolve()
    paths = training_paths + [validation_path]
    if len(set(paths)) != len(paths):
        raise ValueError('Training/validation session overlap')
    if len({digest(p/'telemetry.csv') for p in paths}) != len(paths):
        raise ValueError('Duplicate session contents')
    sources = [Path(config), Path(c['session_split']), Path(__file__),
               Path(__file__).with_name('gamepad.py'), Path(__file__).with_name('human_imitation.py'),
               Path('DrivingRecorder/validate_recording.py'), Path('DrivingRecorder/laps.py'),
               Path(c['prepared'])/'manifest.json', Path(c['prepared'])/'heading_profile.json',
               Path(c['speed_profile']), policy_binary()]
    for p in paths:
        sources.extend(p/n for n in ('metadata.json','summary.json','events.csv','telemetry.csv','inputs.csv','controls.snapshot.json','assists.snapshot.json'))
    sources.append(Path(__file__).with_name('augmentation.py'))
    inventory = hashes(sources)
    training, validation, profiles, counts = [], [], [], []
    names = None
    with tempfile.TemporaryDirectory(prefix='session-gamepad-') as tmp:
        for i,p in enumerate(paths):
            print(f'Preparing session {i+1}/{len(paths)}: {p.name}', flush=True)
            dest = Path(tmp)/str(i); dest.mkdir()
            is_train = p != validation_path
            examples, nn, profile = prepare_recording(p,c['prepared'],c['speed_profile'],dest,
                feature_transform=c['feature_transform'],include_invalid=is_train,augmentation=c.get('augmentation') if is_train else None)
            if c.get('audit_dir'):
                audit=Path(c['audit_dir']);audit.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(dest/'alignment-review.json',audit/(p.name+'.json'))
            if names is not None and names != nn:
                raise ValueError('Feature schema mismatch')
            names = nn; profiles.append(profile)
            for e in examples:
                e['recording'] = str(p)
            (training if is_train else validation).extend(examples)
            counts.append(dict(recording=str(p),split='train' if is_train else 'validation',examples=len(examples)))
    signatures={(p['input_sha256'][p['controls']],p['input_sha256'][p['assists']],p['steam_input_disabled']) for p in profiles}
    if len(signatures)!=1:
        raise ValueError('Teacher settings differ')
    manifest=dict(schema_version=1,kind='gamepad_imitation',semantics='gamepad_v1',complete=False,
        teacher_source='raw_gamepad_sessions',initialization='random_seed_42',normalization='training sessions only',
        recordings=list(map(str,paths)),teacher_sessions=counts,prepared=str(Path(c['prepared']).resolve()),
        input_sha256=inventory,feature_transform=c['feature_transform'],
        validation_scope='independent held-out session',
        invalid_training_policy='included; human mistakes and successful recoveries not automatically distinguished')
    return training,validation,names,profiles,inventory,manifest


def prepare_cached(c, config):
    """Reuse verified teacher features, never policy rollouts or prior weights."""
    from ModelTrainer.common import verify_hashes
    source=Path(c['cached_teacher_model']).resolve(); meta=read(source/'manifest.json')
    if (meta.get('complete') is not True or meta.get('teacher_source')!='raw_gamepad_sessions'
            or meta.get('success_selection') or meta.get('observation_variant')
            or meta.get('semantics')!='gamepad_v1' or meta.get('augmentation_variant')
            or meta.get('feature_transform')!=c['feature_transform']
            or Path(meta['prepared']).resolve()!=Path(c['prepared']).resolve()):
        raise ValueError('Incompatible teacher feature cache')
    verify_hashes(meta['output_sha256'],source)
    split=read(c['session_split'])
    expected=[str(Path(p).resolve()) for p in split['training_sessions']+[split['validation_session']]]
    if meta['recordings']!=expected or len(set(expected))!=len(expected):
        raise ValueError('Cached teacher split mismatch')
    if digest(c['speed_profile'])!=meta['output_sha256']['speed-profile.json']:
        raise ValueError('Cached speed profile mismatch')
    if digest(Path(c['prepared'])/'manifest.json')!=meta['prepared_sha256']:
        raise ValueError('Cached path mismatch')
    # Historical source code may have changed; verify raw evidence and cache outputs,
    # and fingerprint current training code separately rather than blessing old hashes.
    evidence={p:h for p,h in meta['input_sha256'].items() if any(Path(p).is_relative_to(Path(r)) for r in expected)}
    required={str(Path(r)/n) for r in expected for n in ('telemetry.csv','inputs.csv','metadata.json','summary.json','controls.snapshot.json','assists.snapshot.json')}
    if not required.issubset(evidence):raise ValueError('Missing cached raw evidence')
    verify_hashes(evidence)
    if len({digest(Path(r)/'telemetry.csv') for r in expected})!=len(expected):
        raise ValueError('Duplicate teacher contents')
    training=read(source/'training-examples.json');validation=read(source/'validation-examples.json')
    for rows,allowed in [(training,set(expected[:-1])),(validation,{expected[-1]})]:
        if not rows or any(r.get('recording') not in allowed for r in rows):
            raise ValueError('Cached examples cross the teacher split')
        if len({(r['recording'],r['sequence']) for r in rows})!=len(rows):
            raise ValueError('Duplicate cached teacher examples')
    profiles=[read(Path(r)/'metadata.json')['input_profile'] for r in expected]
    inventory=dict(evidence)
    inventory.update(hashes([source/'manifest.json',source/'training-examples.json',source/'validation-examples.json',
        Path(config),Path(c['session_split']),Path(c['prepared'])/'manifest.json',Path(c['speed_profile']),
        Path(__file__),Path(__file__).with_name('gamepad.py'),policy_binary()]))
    if c.get('shared_normalization'):inventory.update(hashes([Path(c['shared_normalization'])]))
    manifest={k:v for k,v in meta.items() if k not in ('output_sha256','input_sha256','excluded_input_features','error','parity_passed')}
    manifest.update(complete=False,input_sha256=inventory,cached_teacher_model=str(source),initialization='random_seed_42',
        validation_scope='held-out session reused for model selection; not final test')
    return training,validation,read(source/'model.json')['feature_names'],profiles,inventory,manifest
