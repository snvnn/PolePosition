"""Read-only dataset review: scan evidence, then finalize explicit decisions."""
import argparse
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time
import tomllib
import uuid

from .builder import EXTRA, REQUIRED, check_config, hashes, json_write, quality, split_laps
from DrivingRecorder.validate_recording import validate
from .review_analysis import reference_models, detect
from .review_report import render_review

VERSION = '0.1.0'
DEFAULTS = dict(output_dir='reviews', progress_bins=1000, min_reference_laps=3,
                mad_multiplier=6.0, trajectory_floor_m=5.0, speed_floor_kmh=20.0,
                steering_floor=.35, relative_duration_seconds=.2, merge_gap_seconds=.3,
                low_speed_kmh=20.0, low_speed_seconds=2.0, window_seconds=.1,
                window_tolerance_seconds=.03, reverse_progress=.002,
                displacement_speed_multiplier=2.0, displacement_margin_m=5.0, context_seconds=3.0)


def config_check(values):
    if set(values)-DEFAULTS.keys():
        raise ValueError('Unknown review config key')
    config = DEFAULTS | values
    for key, value in config.items():
        if key == 'output_dir':
            if not isinstance(value, str) or not value.strip():
                raise ValueError('Invalid output_dir')
        elif key in ('progress_bins', 'min_reference_laps'):
            if type(value) is not int or not (3 <= value <= 10000):
                raise ValueError(f'Invalid {key}')
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'Invalid {key}')
    if config['reverse_progress'] >= .5 or config['window_tolerance_seconds'] >= config['window_seconds']:
        raise ValueError('Invalid progress/window setting')
    return config


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def read_csv(path, fields=None):
    with path.open(newline='', encoding='utf-8') as stream:
        reader = csv.DictReader(stream)
        if fields is not None and reader.fieldnames != fields:
            raise ValueError(f'Invalid header: {path}')
        rows = list(reader)
        if any(None in r or None in r.values() for r in rows):
            raise ValueError(f'Malformed CSV: {path}')
        return rows


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def snapshot(paths):
    return {str(p.resolve()): digest(p) for p in sorted(paths)}


def verify_snapshot(expected):
    for name, checksum in expected.items():
        if digest(Path(name)) != checksum:
            raise ValueError(f'Input changed: {name}')


def load_dataset(folder):
    """Validate against source rows, not merely a self-consistent derived CSV."""
    folder = Path(folder).resolve()
    meta = read_json(folder/'manifest.json')
    if meta.get('schema_version') != 1 or meta.get('complete') is not True or meta.get('split_complete') is not True:
        raise ValueError('Review needs a completed, split schema v1 dataset')
    source = Path(meta['source_path']).resolve()
    paths = [folder/p for p in ('manifest.json', 'laps.csv', 'splits.json', 'excluded_intervals.csv')]
    paths += sorted((folder/'laps').glob('*.csv'))
    paths += [source/p for p in ('metadata.json','events.csv','telemetry.csv','summary.json')]
    before = snapshot(paths)
    if hashes(source) != meta['source_sha256']:
        raise ValueError('Source SHA-256 disagrees with dataset')
    validation = validate(source)
    source_summary = read_json(source/'summary.json')
    if not validation['format_pass'] or source_summary['dropped_samples'] or source_summary['dropped_events']:
        raise ValueError('Source validation failed')
    original = read_csv(source/'telemetry.csv', REQUIRED)
    raw_by_seg = {}
    for row in original:
        raw_by_seg.setdefault(int(row['segment_id']), []).append(row)
    table = read_csv(folder/'laps.csv')
    if len(table) != len(validation['laps']):
        raise ValueError('Lap count disagrees with source')
    rows_by_lap, checked, covered, complete_rows = {}, [], set(), set()
    excluded_laps = {}
    builder_config = check_config(meta['config'])
    for ordinal, (lap, boundary) in enumerate(zip(table, validation['laps']), 1):
        seg, start, end = (boundary[k] for k in ('segment_id','start_elapsed_ns','end_elapsed_ns'))
        rows = [r for r in raw_by_seg[seg] if start <= int(r['elapsed_ns']) < end]
        complete_rows.update(int(r['sequence']) for r in rows)
        lid = f"{meta['session_id']}-s{seg}-q{rows[0]['sequence']}"
        if not re.fullmatch(r'[A-Za-z0-9_-]+', lid):
            raise ValueError('Unsafe lap ID')
        q = quality(rows, start, end)
        reasons = []
        if any(r['lap_valid'] != 'true' for r in rows): reasons.append('invalid_lap')
        if any(r['in_pit'] == 'true' for r in rows): reasons.append('in_pit')
        if q['sample_intervals']['max_ms'] > builder_config['max_gap_ms']: reasons.append('sample_gap')
        expected = dict(lap_id=lid, ordinal=str(ordinal), segment_id=str(seg), start_elapsed_ns=str(start),
                        end_elapsed_ns=str(end), samples=str(len(rows)), accepted=str(not reasons), reasons=';'.join(reasons))
        if any(lap.get(k) != v for k,v in expected.items()):
            raise ValueError(f'Lap metadata disagrees with source: {lid}')
        expected_numbers = dict(duration_seconds=(end-start)/1e9, max_gap_ms=q['sample_intervals']['max_ms'])
        expected_numbers.update({f'sector_{i+1}_seconds': v for i,v in enumerate(q['sector_seconds'] or ['', '', ''])})
        for k,v in expected_numbers.items():
            if (v == '' and lap.get(k) != '') or (v != '' and float(lap[k]) != v):
                raise ValueError(f'Lap timing disagrees: {lid}/{k}')
        checked.append(dict(lap, accepted=not reasons))
        if reasons:
            excluded_laps[(int(rows[0]['sequence']), int(rows[-1]['sequence']))] = ';'.join(reasons)
            continue
        extracted = read_csv(folder/'laps'/f'{lid}.csv', REQUIRED+EXTRA)
        if len(extracted) != len(rows):
            raise ValueError('Lap samples missing/extra')
        for actual, raw in zip(extracted, rows):
            expected_row = raw | dict(session_id=meta['session_id'], lap_id=lid, lap_elapsed_ns=str(int(raw['elapsed_ns'])-start))
            if actual != expected_row:
                raise ValueError(f'Export differs from source: {lid}, sequence {raw["sequence"]}')
            seq = int(actual['sequence'])
            if seq in covered: raise ValueError('Duplicate exported sample')
            covered.add(seq)
        rows_by_lap[lid] = extracted
    if len(rows_by_lap) != meta['accepted_laps'] or {p.stem for p in (folder/'laps').glob('*.csv')} != set(rows_by_lap):
        raise ValueError('Exported lap inventory disagrees')
    splits = read_json(folder/'splits.json')
    if splits != split_laps(checked, builder_config):
        raise ValueError('Split membership disagrees with dataset rules')
    for lap in checked:
        expected_split = next((name for name, ids in splits.items() if lap['lap_id'] in ids), 'excluded')
        if lap['split'] != expected_split: raise ValueError('Lap split disagrees')
    exclusions = read_csv(folder/'excluded_intervals.csv')
    raw_by_seq = {int(r['sequence']): r for r in original}
    for interval in exclusions:
        start, end = int(interval['first_sequence']), int(interval['last_sequence'])
        if end < start or end-start+1 != int(interval['samples']): raise ValueError('Invalid excluded interval')
        if interval['reason'] != 'incomplete_lap' and excluded_laps.get((start,end)) != interval['reason']:
            raise ValueError('Excluded lap reason disagrees')
        for seq in range(start, end+1):
            if seq in covered or seq not in raw_by_seq or int(raw_by_seq[seq]['segment_id']) != int(interval['segment_id']):
                raise ValueError('Excluded range overlaps or is not in source')
            if interval['reason'] == 'incomplete_lap' and seq in complete_rows:
                raise ValueError('Complete lap mislabeled incomplete')
            covered.add(seq)
    if covered != set(raw_by_seq): raise ValueError('Not all source rows are accounted for')
    verify_snapshot(before)
    return dict(path=folder, meta=meta, source=source, input_sha256=before, laps=checked,
                rows=rows_by_lap, splits=splits, exclusions=exclusions,
                events=read_csv(source/'events.csv'))


def create_output(parent, prefix, protected, output_id=None):
    parent = Path(parent).resolve()
    if any(parent == p or p in parent.parents for p in protected):
        raise ValueError('Output cannot be inside input directories')
    name = output_id or f'{prefix}-{time.time_ns()}-{uuid.uuid4().hex[:8]}'
    if not re.fullmatch(r'[A-Za-z0-9_-]+', name): raise ValueError('Unsafe output ID')
    parent.mkdir(parents=True, exist_ok=True)
    output = parent/name
    output.mkdir()
    return output


def finish_manifest(output, manifest):
    json_write(output/'manifest.next.json', manifest)
    (output/'manifest.next.json').replace(output/'manifest.json')


def scan(dataset, config, *, output_id=None):
    config = config_check(config)
    dataset = Path(dataset).resolve()
    # Read only enough to protect the recording directory before creating output.
    source = Path(read_json(dataset/'manifest.json')['source_path']).resolve()
    output = create_output(config['output_dir'], 'scan', [dataset, source], output_id)
    manifest = dict(schema_version=1, review_version=VERSION, kind='scan', complete=False,
                    dataset_path=str(dataset), config=config, created_unix_ns=time.time_ns())
    json_write(output/'manifest.json', manifest)
    try:
        data = load_dataset(dataset)
        manifest['input_sha256'] = data['input_sha256']
        models = reference_models(data['rows'], data['splits']['train'], config)
        json_write(output/'baselines.json', models)
        results = []
        for lap in data['laps']:
            if not lap['accepted']: continue
            lid = lap['lap_id']
            reference = models['leave_one_out'].get(lid, models['global_baseline'])
            candidates = detect(data['rows'][lid], reference, config)
            for i,candidate in enumerate(candidates,1): candidate['candidate_id'] = f'{lid}-c{i:03}'
            results.append(dict(lap_id=lid, ordinal=int(lap['ordinal']), split=lap['split'],
                                samples=len(data['rows'][lid]), duration_seconds=float(lap['duration_seconds']),
                                candidates=candidates))
        findings = dict(schema_version=1, laps=results,
                        excluded_laps=[lap for lap in data['laps'] if not lap['accepted']],
                        excluded_intervals=data['exclusions'], source_events=data['events'])
        json_write(output/'findings.json', findings)
        decisions = dict(schema_version=1, laps=[dict(lap_id=lap['lap_id'], status='unreviewed',
                         full_lap_reviewed=False, reason='', reviewer='', reviewed_at='',
                         candidates=[dict(candidate_id=c['candidate_id'], resolution='unreviewed', reason='')
                                     for c in lap['candidates']]) for lap in results])
        json_write(output/'decisions.template.json', decisions)
        with (output/'report.html').open('x',encoding='utf-8') as stream:
            stream.write(render_review(findings, data['rows'], config))
        verify_snapshot(data['input_sha256'])
        manifest.update(complete=True, reviewed=False,
                        candidate_count=sum(len(l['candidates']) for l in results), lap_count=len(results),
                        artifact_sha256={name:digest(output/name) for name in ('baselines.json','findings.json','report.html')})
    except Exception as exc:
        manifest.update(complete=False, error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        finish_manifest(output, manifest)
    return output


def validate_decisions(decisions, findings):
    if decisions.get('schema_version') != 1 or not isinstance(decisions.get('laps'), list):
        raise ValueError('Unsupported decisions schema')
    expected = {lap['lap_id']:lap for lap in findings['laps']}
    seen = set()
    for decision in decisions['laps']:
        lid = decision['lap_id']
        if lid not in expected or lid in seen: raise ValueError('Unknown or duplicate lap decision')
        seen.add(lid)
        if decision['status'] not in ('eligible','hold','exclude') or decision.get('full_lap_reviewed') is not True:
            raise ValueError('Every lap needs explicit review and a final disposition')
        if any(not isinstance(decision.get(k),str) or not decision[k].strip() for k in ('reason','reviewer','reviewed_at')):
            raise ValueError('Missing decision evidence/reviewer/time')
        if datetime.fromisoformat(decision['reviewed_at']).utcoffset() is None:
            raise ValueError('Review time must include a time zone')
        candidates = {c['candidate_id']:c for c in expected[lid]['candidates']}
        reviewed = set()
        for verdict in decision['candidates']:
            cid = verdict['candidate_id']
            if cid not in candidates or cid in reviewed: raise ValueError('Unknown/duplicate candidate verdict')
            reviewed.add(cid)
            if verdict['resolution'] not in ('resolved','unresolved') or not isinstance(verdict.get('reason'),str) or not verdict['reason'].strip():
                raise ValueError('Candidate needs evidence and resolution')
            if decision['status'] == 'eligible' and (verdict['resolution'] != 'resolved' or 'insufficient_reference' in candidates[cid]['reasons']):
                raise ValueError('Unresolved/unsupported comparison cannot enter eligible set')
        if reviewed != set(candidates): raise ValueError('Unreviewed candidates remain')
    if seen != set(expected): raise ValueError('Missing lap decisions')


def decision_usage(findings, decisions):
    """Reconstruct downstream membership from explicit decisions, without writes."""
    validate_decisions(decisions, findings)
    eligible = {name:[] for name in ('train','validation','test')}
    holds, excludes = [], []
    index = {d['lap_id']:d for d in decisions['laps']}
    for lap in findings['laps']:
        status = index[lap['lap_id']]['status']
        if status == 'eligible': eligible[lap['split']].append(lap['lap_id'])
        elif status == 'hold': holds.append(lap['lap_id'])
        else: excludes.append(lap['lap_id'])
    return dict(eligible=eligible, hold=holds, exclude=excludes,
                trackmodel_candidates=eligible['train'], trackmodel_ready=bool(eligible['train']))


def finalize(review, decisions_path, *, output_id=None):
    review, decisions_path = Path(review).resolve(), Path(decisions_path).resolve()
    meta = read_json(review/'manifest.json')
    if meta.get('kind') != 'scan' or meta.get('schema_version') != 1 or meta.get('review_version') != VERSION or meta.get('complete') is not True:
        raise ValueError('Finalize requires a complete supported scan')
    dataset = Path(meta['dataset_path'])
    source = Path(read_json(dataset/'manifest.json')['source_path']).resolve()
    output = create_output(review.parent, 'final', [review,dataset,source], output_id)
    result = dict(schema_version=1, review_version=VERSION, kind='final', complete=False,
                  review_path=str(review), created_unix_ns=time.time_ns())
    json_write(output/'manifest.json', result)
    try:
        scan_inputs = {str(review/name):h for name,h in meta['artifact_sha256'].items()}
        scan_inputs[str(review/'manifest.json')] = digest(review/'manifest.json')
        scan_inputs[str(decisions_path)] = digest(decisions_path)
        verify_snapshot(meta['input_sha256'])
        verify_snapshot(scan_inputs)
        findings = read_json(review/'findings.json')
        decisions = read_json(decisions_path)
        lists = decision_usage(findings, decisions)
        json_write(output/'decisions.json', decisions)
        json_write(output/'usage.json', lists)
        verify_snapshot(meta['input_sha256'])
        verify_snapshot(scan_inputs)
        result.update(complete=True, input_sha256=meta['input_sha256'], review_sha256=scan_inputs,
                      counts={k:len(v) for k,v in lists['eligible'].items()}, hold_count=len(lists['hold']),
                      exclude_count=len(lists['exclude']), trackmodel_ready=lists['trackmodel_ready'])
    except Exception as exc:
        result.update(complete=False, error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        finish_manifest(output, result)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('scan')
    p.add_argument('--dataset', type=Path, required=True)
    p.add_argument('--config', type=Path, default=Path(__file__).with_name('review.toml'))
    p = commands.add_parser('finalize')
    p.add_argument('--review', type=Path, required=True)
    p.add_argument('--decisions', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'scan':
            with args.config.open('rb') as stream: config = tomllib.load(stream)
            output = scan(args.dataset, config)
        else: output = finalize(args.review, args.decisions)
    except (OSError, ValueError, KeyError, TypeError, csv.Error) as exc:
        print(f'Review failed: {exc}', file=sys.stderr)
        return 1
    print(f'Review: {output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
