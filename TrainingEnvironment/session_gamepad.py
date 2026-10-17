"""Whole-session human demonstrations with an independent held-out session."""
from pathlib import Path
import tempfile
import shutil
from ModelTrainer.common import read, hashes, digest
from ModelTrainer.dataset import policy_binary


def prepare(c, config):
    from .gamepad import prepare_recording
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
