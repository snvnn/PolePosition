"""Build v2 examples with shared Rust features and strictly causal sent axes."""
import csv
import json
from pathlib import Path
import subprocess

import numpy as np

from ModelTrainer.common import finish, hashes, load_artifact, new_folder, read, rows, save, verify_hashes
from ModelTrainer.dataset import policy_binary
from TrackModel.inputs import load_review
from TrackModel.model import load_reference
from .teacher import axes


class PreparationStopped(RuntimeError):
    pass


def check_stop(should_stop):
    if should_stop():
        raise PreparationStopped('Preparation stopped')


def train_speed_profile(prepared, should_stop):
    pm = read(prepared/'manifest.json')
    track = load_reference(pm['track_model_path'])
    reviewed = load_review(pm['review_path'])
    eligible = reviewed['usage']['eligible']['train']
    if not eligible:
        raise ValueError('No reviewed human train laps')
    dataset = reviewed['locations'][2]
    reference = track['rows']
    xy = np.array([[r['position_x'], r['position_z']] for r in reference])
    s = np.array([r['s_m'] for r in reference])
    length = track['manifest']['total_length_m']
    profiles = []
    for lap in eligible:
        check_stop(should_stop)
        with (dataset/'laps'/f'{lap}.csv').open(encoding='utf-8', newline='') as stream:
            data = list(csv.DictReader(stream))
        positions = np.array([[float(r['position_x']), float(r['position_z'])] for r in data])
        speeds = np.array([float(r['speed_kmh']) for r in data])
        if not np.isfinite(positions).all() or not np.isfinite(speeds).all() or np.any(speeds < 0):
            raise ValueError('Invalid human train reference')
        bins = [[] for _ in reference]
        for start in range(0, len(data), 256):
            check_stop(should_stop)
            nearest = np.square(positions[start:start+256, None, :]-xy[None, :, :]).sum(axis=2).argmin(axis=1)
            for index, speed in zip(nearest, speeds[start:start+256]):
                bins[index].append(speed)
        known = np.array([i for i, values in enumerate(bins) if values])
        if len(known) < 3:
            raise ValueError('Insufficient human speed coverage')
        values = np.array([np.median(bins[i]) for i in known])
        profiles.append(np.interp(s, s[known], values, period=length))
    return dict(schema_version=2, length_m=length, s_m=s.tolist(),
                speed_kmh=np.median(profiles, axis=0).tolist()), eligible


def causal_frames(examples, commands, should_stop=lambda: False, speed_cap=10):
    sent = [r for r in commands if r['send_success'] == 'true']
    if any(int(b['now_ns']) < int(a['now_ns']) for a, b in zip(sent, sent[1:])):
        raise ValueError('Transmission time reversal')
    cursor = 0
    previous = None
    for example in examples:
        check_stop(should_stop)
        t = example['observation_ns']
        while cursor < len(sent) and int(sent[cursor]['now_ns']) < t:
            previous = sent[cursor]
            cursor += 1
        if (previous is None or previous['mode'] != 'Running'
                or int(previous['tracking_epoch']) != example['tracking_epoch']
                or t-int(previous['now_ns']) >= 100_000_000):
            continue
        o = example['observation']
        frame = dict(sequence=example['sequence'], observation_ns=t, tracking_epoch=example['tracking_epoch'],
                     ready=True, speed_kmh=float(o['speed_kmh']), target_speed_kmh=example['target_speed_kmh'],
                     rpm=float(o['rpm']), lateral_error_m=float(o['lateral_error_m']),
                     heading_error_rad=float(o['heading_error_rad']), s_m=float(o['path_s_m']))
        yield example, dict(frame=frame, speed_cap_kmh=speed_cap, previous_axes=axes(previous, 'applied'),
                            previous_applied_ns=int(previous['now_ns']), gear_raw=int(o['gear_raw']))


def prepare_features(action_folder, parent='reinforcement_runs', should_stop=lambda: False,
                     on_progress=lambda _: None):
    source, sm = load_artifact(action_folder, 'teacher_actions_v2')
    binary = policy_binary()
    inputs = dict(sm['input_sha256']) | hashes([source/'manifest.json', binary, Path(__file__)])
    inputs.update({str(source/name): value for name, value in sm['output_sha256'].items()})
    out = new_folder(parent, 'teacher-features-v2')
    manifest = dict(schema_version=1, kind='teacher_features_v2', complete=False, input_sha256=inputs,
                    feature_schema_version=2, policy_training_ready=False, eligible_for_live=False,
                    source_splits=sm['source_splits'], sample_interval_ns=20_000_000)
    save(out/'manifest.json', manifest)
    on_progress(dict(phase='v2 자료 준비', result_dir=str(out)))
    try:
        profile, laps = train_speed_profile(Path(sm['prepared']), should_stop)
        save(out/'speed-profile.json', profile)
        manifest['human_train_laps'] = laps
        all_data = {}
        names = None
        for split in ('train', 'validation', 'test'):
            examples = rows(source/f'{split}.jsonl')
            all_data[split] = []
            for index, run in enumerate(sm['source_splits'][split]):
                check_stop(should_stop)
                run = Path(run)
                selected = [r for r in examples if r['run_id'] == run.name]
                with (run/'commands.csv').open(encoding='utf-8', newline='') as stream:
                    commands = list(csv.DictReader(stream))
                cap = read(run/'config.json')['baseline']['config']['target_speed_kmh']
                paired = list(causal_frames(selected, commands, should_stop, cap))
                frame_file, feature_file = out/f'{split}-{index}-frames.jsonl', out/f'{split}-{index}-features.jsonl'
                with frame_file.open('x', encoding='utf-8') as stream:
                    for _, frame in paired:
                        stream.write(json.dumps(frame, allow_nan=False)+'\n')
                # Feature computation has no device access. Poll to keep GUI stop responsive.
                with (out/f'{split}-{index}-rust.log').open('x', encoding='utf-8') as log:
                    process = subprocess.Popen([str(binary), 'features-v2', sm['prepared'], str(frame_file),
                                                str(feature_file), str(out/'speed-profile.json')],
                                               stdout=log, stderr=log)
                    try:
                        while True:
                            try:
                                code = process.wait(timeout=.1)
                                break
                            except subprocess.TimeoutExpired:
                                check_stop(should_stop)
                        if code:
                            raise ValueError('Rust v2 feature preparation failed; see result log')
                    finally:
                        if process.poll() is None:
                            process.terminate()
                            process.wait()
                results = rows(feature_file)
                if len(results) != len(paired):
                    raise ValueError('V2 feature inventory mismatch')
                last = {}
                for (example, _), result in zip(paired, results):
                    if any(result[k] != example[k] for k in ('sequence', 'tracking_epoch', 'observation_ns')):
                        raise ValueError('V2 feature identity mismatch')
                    if names is None:
                        names = result['feature_names']
                    if names != result['feature_names']:
                        raise ValueError('Changed v2 feature schema')
                    epoch, t = example['tracking_epoch'], example['observation_ns']
                    if result['features'] is None or t-last.get(epoch, -20_000_000) < 20_000_000:
                        continue
                    last[epoch] = t
                    all_data[split].append({k: example[k] for k in
                        ('run_id','sequence','tracking_epoch','observation_ns','action')} | {'features':result['features']})
                on_progress(dict(phase=f'{split} 특징 준비', samples=len(all_data[split]), result_dir=str(out)))
        if any(not data for data in all_data.values()):
            raise ValueError('All original splits require usable v2 examples')
        train = np.asarray([r['features'] for r in all_data['train']], dtype=np.float64)
        std = train.std(axis=0)
        std[std == 0] = 1.
        save(out/'normalizer.json', dict(feature_names=names, mean=train.mean(axis=0).tolist(), std=std.tolist()))
        for split, data in all_data.items():
            with (out/f'{split}.jsonl').open('x', encoding='utf-8') as stream:
                for row in data:
                    stream.write(json.dumps(row, allow_nan=False)+'\n')
        manifest.update(feature_names=names, counts={s:len(r) for s,r in all_data.items()}, policy_training_ready=True)
        verify_hashes(inputs)
        check_stop(should_stop)
        finish(out, manifest)
    except BaseException as error:
        finish(out, manifest, False, error)
        raise
    return out
