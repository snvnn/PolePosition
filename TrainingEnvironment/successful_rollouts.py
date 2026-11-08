"""Explicit, provenance-preserving reuse of validated autonomous success intervals."""
import bisect
import json
from pathlib import Path
import numpy as np
from ModelTrainer.common import read, hashes, digest, verify_hashes


def example_from_pair(o, p, sent, *, archive, lap, source_model):
    if (p['mode'] != 'Running' or p['authority'] != 'model' or any(p['label_mask'])
            or sent['mode'] != 'Running' or sent['authority'] != 'model'):
        raise ValueError('Human intervention or non-policy control in success interval')
    if not o['sample']['lap_valid'] or o['state']['status'] != 'ready':
        raise ValueError('Invalid or untracked success observation')
    features = np.asarray(p['features'], dtype=np.float64)
    action = np.asarray(p['selected_action'], dtype=np.float64)
    if (features.shape != (35,) or action.shape != (3,) or not np.isfinite(features).all()
            or not np.isfinite(action).all() or abs(action[0]) > 1 or np.any(action[1:] < 0)
            or np.any(action[1:] > 1)):
        raise ValueError('Invalid success features/action')
    if not np.array_equal(action, p['action']) or not np.array_equal(action, p['command_action']):
        raise ValueError('Transformed or overridden policy labels')
    t = o['observation_ns']
    if p['observation_ns'] != t or not t <= p['now_ns'] <= sent['send_start_ns'] <= sent['send_end_ns'] <= t + 20_000_000:
        raise ValueError('Success observation/action timing mismatch')
    expected = action * [32768 if action[0] < 0 else 32767, 255, 255]
    if max(abs(expected - [sent['report'][k] for k in ('lx', 'rt', 'lt')])) > 1:
        raise ValueError('Success command/report mismatch')
    if sent['xinput'][1] != sent['report']:
        raise ValueError('Success XInput feedback mismatch')
    return dict(features=features.tolist(), action=action.tolist(), sequence=o['sequence'],
                observation_ns=t, policy_ns=p['now_ns'], send_end_ns=sent['send_end_ns'],
                recording=str(archive / 'source-run'), source_kind='autonomous_success',
                source_model_sha256=source_model, lap=lap, s_m=o['state']['s_m'],
                tracking_epoch=o['state']['tracking_epoch'], mask=[1, 1, 1])


def set_mixture_weights(training, additions, fraction):
    """Preserve relative importance inside each source and set its total mass."""
    human_mass=sum(r.get('weight',1.) for r in training)
    success_mass=sum(r.get('weight',1.) for r in additions)
    if not 0<fraction<1 or human_mass<=0 or success_mass<=0:raise ValueError('Invalid mixture')
    scale=fraction*human_mass/((1-fraction)*success_mass)
    for row in additions:row['weight']=row.get('weight',1.)*scale
    return scale


def validate_segment(chunk, low, high, full_laps=False):
    if len(chunk)<256:raise ValueError('Missing start of success segment')
    start_s=chunk[0]['s_m']
    if (min(start_s,high-start_s)>5 if full_laps else start_s>low+5):raise ValueError('Missing start of success segment')
    wraps=0
    for a,b in zip(chunk,chunk[1:]):
        if not 0<b['observation_ns']-a['observation_ns']<=50_000_000:
            raise ValueError('Discontinuous success observations')
        if b['s_m']<a['s_m']:
            # Reference projection can wrap a few frames before ACC's progress
            # crossing. Allow one bounded end-of-lap wrap, never a reset mid-lap.
            if not (full_laps and wraps==0 and a['s_m']>=high-10 and b['s_m']<=10
                    and min(chunk[-1]['observation_ns']-b['observation_ns'],b['observation_ns']-chunk[0]['observation_ns'])<=200_000_000):
                raise ValueError('Unexplained reverse progress/reset')
            wraps+=1
    end=chunk[-1]['s_m']+wraps*high
    if full_laps:
        if abs((end-start_s)-high)>5:raise ValueError('Incomplete success path coverage')
    elif end<high-5:raise ValueError('Incomplete success path coverage')
    return wraps


def append_successes(c, training, validation, names, manifest, inventory):
    from GamepadIO.run_report import inspect_log
    spec = c['successful_rollouts']
    source_path=spec.get('source_model',c.get('cached_teacher_model'))
    augmented=bool(c.get('cached_augmented_dataset'))
    if (not c.get('weighted_sampling') or not source_path
            or not c.get('shared_normalization') or c.get('steering_feedback')
            or (c.get('noise') and not augmented) or c.get('response_dropout')):
        raise ValueError('Success reuse requires explicit compatible weighted training')
    archive = Path(spec['archive']).resolve()
    evidence = read(archive / 'successful-laps.json')
    verify_hashes(evidence['evidence_sha256'])
    source = Path(source_path).resolve()
    source_meta=read(source/'manifest.json');verify_hashes(source_meta['output_sha256'],source)
    source_network = read(source / 'model.json')
    if digest(source / 'model.json') != evidence['model_sha256'] or names != source_network['feature_names']:
        raise ValueError('Success policy/schema does not match the preserved source model')
    normalizer = read(c['shared_normalization'])
    if not augmented and any(normalizer[k] != source_network[k] for k in ('mean', 'std')):
        raise ValueError('Success training must retain the source normalization')
    run = archive / 'source-run'
    rm = read(run / 'manifest.json'); summary = read(run / 'summary.json')
    if (rm['model_sha256'] != evidence['model_sha256'] or rm['steering_smoothing_ms'] != 0
            or rm['semantics'] != 'gamepad_v1' or summary['logging_failure']
            or rm['controls_teacher_sha256'] != manifest['controls_sha256']):
        raise ValueError('Success recording model/input/logging mismatch')
    if run in {Path(p).resolve() for p in {r['recording'] for r in training + validation}}:
        raise ValueError('Success source overlaps existing data')
    rows = [json.loads(line) for line in (run / 'samples.jsonl').read_text().splitlines()]
    transport, pairs = inspect_log(rows)
    if not transport['passed']:raise ValueError(f'Success transport failed: {transport}')
    full_laps=spec.get('full_laps',False)
    if full_laps:
        prepared=read(Path(c['prepared'])/'manifest.json')
        low,high=0.,read(Path(prepared['track_model_path'])/'manifest.json')['total_length_m']
    else:low, high = spec['start_s_m'], spec['end_s_m']
    fraction = spec['sampling_fraction']
    if not 0 <= low < high or not 0 < fraction < 1:raise ValueError('Invalid success selection')
    laps = evidence['successful_full_laps']
    if [lap['lap'] for lap in laps] != spec['laps']:raise ValueError('Unexpected success laps')
    observations = [r for r in rows if r['kind'] == 'observation']
    observation_times = [r['observation_ns'] for r in observations]
    rebuilt=None
    if spec.get('reproject'):
        if c['feature_transform']!='original':raise ValueError('Repaired geometry uses unmodified shared feature engine')
        from .continuous_rollout import rebuild
        rebuilt,positions=rebuild(rows,c['prepared'],c['speed_profile'],end_ns=laps[-1]['end_observation_ns']+20_000_000)
        inventory.update(hashes([Path(__file__).with_name('continuous_rollout.py'),Path(__file__).with_name('continuous_reference.py')]))
    additions = []; counts = []
    for lap in laps:
        start, end = lap['start_observation_ns'], lap['end_observation_ns']
        obs = [r for r in observations if start <= r['observation_ns'] < end]
        if (not lap['all_valid'] or len(obs) < 2 or any(not r['sample']['lap_valid'] or r['state']['status'] != 'ready' for r in obs)
                or len({r['state']['tracking_epoch'] for r in obs}) != 1
                or any(b['sequence'] != a['sequence'] + 1 or not 0 < b['observation_ns'] - a['observation_ns'] <= 20_000_000 for a, b in zip(obs, obs[1:]))):
            raise ValueError('Success lap continuity/validity failed')
        chunk = []
        for o, p, sent in pairs:
            if not start <= o['observation_ns'] < end:continue
            # Inspect the whole lap's authority, even outside the selected corner.
            example = example_from_pair(o, p, sent, archive=archive, lap=lap['lap'], source_model=evidence['model_sha256'])
            if rebuilt is not None:
                t=example['observation_ns']
                if t not in rebuilt:raise ValueError('Rebuilt success history missing')
                example.update(features=rebuilt[t],s_m=positions[t][0],features_rebuilt_from_world_pose=True)
            next_i = bisect.bisect_right(observation_times, sent['send_end_ns'])
            if next_i == len(observations) or observations[next_i]['state']['tracking_epoch'] != o['state']['tracking_epoch']:
                raise ValueError('No continuous observation after success action')
            if full_laps or low <= example['s_m'] <= high:chunk.append(example)
        wraps=validate_segment(chunk,low,high,full_laps)
        if full_laps and len(chunk)!=lap['policy_samples']:raise ValueError('Incomplete full-lap policy samples')
        additions.extend(chunk)
        counts.append(dict(lap=lap['lap'], examples=len(chunk), start_s_m=chunk[0]['s_m'], end_s_m=chunk[-1]['s_m'],
                           reference_wraps=wraps,
                           duration_s=(chunk[-1]['observation_ns']-chunk[0]['observation_ns'])/1e9))
    if len({r['sequence'] for r in additions}) != len(additions):raise ValueError('Duplicate success samples')
    if augmented:
        from .augmentation import sample_weight
        for row in additions:row['weight']=sample_weight(row['features'],row['action'],manifest['augmentation_settings'])
    weight=set_mixture_weights(training,additions,fraction)
    training.extend(additions)
    inventory.update(evidence['evidence_sha256'])
    inventory.update(hashes([archive / 'successful-laps.json', source / 'model.json', Path(__file__), Path('GamepadIO/run_report.py')]))
    manifest.update(training_sources=['human_teacher', 'autonomous_success'], success_selection=dict(spec, counts=counts,
        examples=len(additions), weight_scale=weight, label_semantics='selected model actions; not human correction; no reward learning'),
        normalization=manifest['normalization'] if augmented else 'frozen original model', success_transport=transport, input_sha256=inventory)
    return additions
