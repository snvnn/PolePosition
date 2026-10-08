"""Rejoin proven PID requests including steering, preserving original run splits."""
import csv
import json
from pathlib import Path
import tomllib

from ModelTrainer.common import digest, finish, hashes, new_folder, read, save, verify_hashes
from ModelTrainer.dataset import joined_teacher
from .actions import from_axes


def axes(row, prefix):
    return tuple(float(row[f'{prefix}_{name}']) for name in ('steering','throttle','brake'))


def join_actions(folder):
    folder = Path(folder).resolve()
    # Retain the existing real-run, successful-stop, hash and observation checks.
    manifest, selected, excluded = joined_teacher(folder)
    with (folder/'commands.csv').open(encoding='utf-8', newline='') as stream:
        commands = list(csv.DictReader(stream))
    signatures, requests, transmitted = {}, {}, {}
    for row in commands:
        key = (int(row['observation_sequence']), int(row['tracking_epoch'])) if row['observation_sequence'] else None
        if key is None or row['mode'] != 'Running':
            continue
        command_key = (int(row['sequence']), *key)
        if row['reason'] == 'accepted':
            action = from_axes(*axes(row, 'requested'))
            signatures.setdefault(key, set()).add(action)
            if command_key in requests:
                raise ValueError('Duplicate accepted command identity')
            requests[command_key] = row
        if row['send_success'] == 'true':
            transmitted.setdefault(command_key, row)
    output = []
    for observation, command in selected:
        key = (int(observation['sequence']), int(observation['tracking_epoch']))
        if len(signatures.get(key, set())) != 1:
            excluded.append(dict(sequence=key[0], tracking_epoch=key[1], reason='ambiguous_v2_action'))
            continue
        command_key = (command['command_sequence'], *key)
        requested, sent = requests.get(command_key), transmitted.get(command_key)
        if requested is None:
            raise ValueError('Missing exact teacher request')
        if sent is None:
            excluded.append(dict(sequence=key[0], tracking_epoch=key[1], reason='no_successful_transmission'))
            continue
        source_ns = int(observation['observation_ns'])
        created, received, applied, expires = (int(requested['created_ns']), int(requested['now_ns']),
                                               int(sent['now_ns']), int(requested['expires_ns']))
        if not source_ns <= created <= received <= applied < expires:
            raise ValueError('Teacher action transmission is outside its source/lease interval')
        if any(sent[field] != requested[field] for field in ('created_ns','expires_ns','requested_steering',
                                                             'requested_throttle','requested_brake')):
            raise ValueError('Transmitted command does not match accepted request')
        request_axes, applied_axes = axes(requested, 'requested'), axes(sent, 'applied')
        action = from_axes(*request_axes)
        applied_action = from_axes(*applied_axes)
        # Raw ACC steering is intentionally absent: it is never a target.
        raw = {name:value for name,value in observation.items() if name != 'steering_raw'}
        output.append(dict(run_id=folder.name, sequence=key[0], tracking_epoch=key[1],
                           observation_ns=source_ns, command_sequence=command_key[0],
                           created_ns=created, received_ns=received, transmitted_ns=applied,
                           expires_ns=expires, action=list(action), transmitted_action=list(applied_action),
                           requested_axes=list(request_axes), transmitted_axes=list(applied_axes),
                           target_speed_kmh=command['target_speed_kmh'], observation=raw))
    return manifest, output, excluded


def prepare_actions(sources_path, output_parent='reinforcement_runs', should_stop=lambda: False,
                    on_progress=lambda _: None):
    sources_path = Path(sources_path).resolve()
    sources = tomllib.loads(sources_path.read_text(encoding='utf-8'))
    if sources.get('schema_version') != 1 or not sources.get('train'):
        raise ValueError('Existing split-assigned teacher sources are required')
    groups = {split:[Path(p).resolve() for p in sources.get(split, [])]
              for split in ('train','validation','test')}
    paths = [p for group in groups.values() for p in group]
    if any(path.samefile(previous) for index,path in enumerate(paths) for previous in paths[:index]):
        raise ValueError('Teacher run cannot occur twice or cross split boundaries')
    prepared = Path(sources['prepared']).resolve()
    pm = read(prepared/'manifest.json')
    if pm.get('complete') is not True:
        raise ValueError('Prepared path is incomplete')
    verify_hashes(pm['input_sha256']); verify_hashes(pm['output_sha256'], prepared)
    inputs = hashes([sources_path, prepared/'manifest.json'])
    inputs.update(pm['input_sha256'])
    inputs.update({str(prepared/name):value for name,value in pm['output_sha256'].items()})
    out = new_folder(output_parent, 'teacher-actions-v2')
    manifest = dict(schema_version=1, kind='teacher_actions_v2', complete=False,
                    action_schema_version=2, action_names=['steering','signed_acceleration'],
                    input_sha256=inputs, sources_path=str(sources_path),
                    source_splits={split:[str(p) for p in group] for split,group in groups.items()},
                    prepared=str(prepared), prepared_sha256=digest(prepared/'manifest.json'),
                    policy_training_ready=False,
                    pending=['v2 Rust features and normalization','actor pretraining specification'])
    save(out/'manifest.json',manifest)
    on_progress(dict(phase='교사 입력 연결', result_dir=str(out)))
    save(out/'sources-snapshot.json',sources)
    exclusions, counts = [], {}
    try:
        for split, group in groups.items():
            counts[split] = 0
            with (out/f'{split}.jsonl').open('x',encoding='utf-8',newline='\n') as stream:
                for folder in group:
                    if should_stop():
                        from .preparation import PreparationStopped
                        raise PreparationStopped('Teacher preparation stopped')
                    source_manifest, rows, excluded = join_actions(folder)
                    if not Path(source_manifest['prepared_path']).samefile(prepared):
                        raise ValueError('Teacher/prepared identity mismatch')
                    inputs.update(hashes([folder/'manifest.json']))
                    inputs.update(source_manifest['input_sha256'])
                    inputs.update({str(folder/name):value for name,value in source_manifest['output_sha256'].items()})
                    for row in rows:
                        stream.write(json.dumps(row, allow_nan=False)+'\n')
                    counts[split] += len(rows)
                    exclusions.extend(dict(run_id=folder.name, split=split, **item) for item in excluded)
        if counts['train'] == 0:
            raise ValueError('No transmitted training actions')
        save(out/'exclusions.json',exclusions)
        save(out/'summary.json',dict(counts=counts,excluded=len(exclusions),
                                    target_source='exact accepted PID request; not ACC steering_raw',
                                    holdouts_preserved=True, policy_training_ready=False))
        manifest['counts'] = counts
        verify_hashes(inputs)
        finish(out,manifest)
    except BaseException as error:
        finish(out,manifest,False,error)
        raise
    return out
