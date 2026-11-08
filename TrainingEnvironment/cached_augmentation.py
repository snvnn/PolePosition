"""Reuse immutable curated examples while separately fingerprinting current code."""
from pathlib import Path
from ModelTrainer.common import read, verify_hashes, hashes, digest
from ModelTrainer.dataset import policy_binary


def prepare(c, config):
    folder=Path(c['cached_augmented_dataset']).resolve();meta=read(folder/'manifest.json')
    if not meta.get('complete') or not meta.get('boundary_revision'):
        raise ValueError('Need the completed boundary-corrected augmentation pool')
    verify_hashes(meta['output_sha256'],folder)
    # Historical producer code is provenance, not the current trainer. Raw evidence
    # and immutable artifacts must still match their historical inventories.
    evidence={k:v for k,v in meta['input_sha256'].items() if Path(k).suffix not in ('.py','.exe')}
    verify_hashes(evidence)
    split=read(c['session_split']);expected=[str(Path(p).resolve()) for p in split['training_sessions']+[split['validation_session']]]
    original=meta['model_manifest']
    if len(set(expected))!=len(expected) or original['recordings']!=expected:
        raise ValueError('Augmented teacher split mismatch')
    if len({digest(Path(p)/'telemetry.csv') for p in expected})!=len(expected):
        raise ValueError('Duplicate teacher contents')
    if (original['feature_transform']!=c['feature_transform'] or Path(original['prepared']).resolve()!=Path(c['prepared']).resolve()
            or digest(c['shared_normalization'])!=meta['output_sha256']['normalization.json']):
        raise ValueError('Cached feature/normalization mismatch')
    training=read(folder/'training-pool.json');validation=read(folder/'validation.json')
    if len(training)!=meta['b_samples'] or len(validation)!=meta['validation_samples']:
        raise ValueError('Cached sample count mismatch')
    for rows,allowed in ((training,set(expected[:-1])),(validation,{expected[-1]})):
        if any(r['recording'] not in allowed for r in rows) or len({(r['recording'],r['sequence']) for r in rows})!=len(rows):
            raise ValueError('Duplicate or cross-split samples')
    from .augmentation import sample_weight
    for r in training:
        if (not r['previous_input_end_ns']<r['observation_ns']<=r['action_read_start_ns']<=r['action_read_end_ns']<=r['observation_ns']+20_000_000
                or r['previous_input_end_ns']<r['context_start_ns'] or r['exclusion_reason'] is not None
                or not set(r['phases_ms']).issubset({0,10}) or not r['phases_ms']
                or r['weight']!=sample_weight(r['features'],r['action'],meta['augmentation'])):
            raise ValueError('Cached augmentation alignment/weight failed')
    profiles=meta['profiles']
    signatures={(p['input_sha256'][p['controls']],p['input_sha256'][p['assists']],p['steam_input_disabled']) for p in profiles}
    if len(signatures)!=1:raise ValueError('Teacher settings differ')
    controls,assists,_=signatures.pop()
    inventory=dict(evidence)
    inventory.update(hashes([folder/'manifest.json',folder/'training-pool.json',folder/'validation.json',folder/'normalization.json',
        Path(config),Path(c['session_split']),Path(__file__),Path(__file__).with_name('session_gamepad.py'),
        Path(__file__).with_name('gamepad.py'),Path(__file__).with_name('augmentation.py'),policy_binary()]))
    m=dict(original,input_sha256=inventory,complete=False,controls_sha256=controls,assists_sha256=assists,
        cached_augmented_dataset=str(folder),augmentation_variant='C_with_autonomous_full_laps',
        augmentation_settings=meta['augmentation'],noise=c.get('noise'),normalization='frozen curated A human samples only',
        validation_scope='held-out session reused for model selection; not final test')
    return training,validation,meta['feature_names'],profiles,inventory,m
