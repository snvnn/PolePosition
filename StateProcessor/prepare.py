"""Validate immutable inputs and calibrate heading using eligible train only."""
import bisect
import math
from pathlib import Path
import statistics
import time
import tomllib

from DatasetBuilder.builder import json_write
from DatasetBuilder.review import create_output, digest, finish_manifest, snapshot, verify_snapshot
from TrackModel.inputs import input_locations, load_review
from TrackModel.model import load_reference
from . import __version__


def config_check(values):
    defaults = tomllib.loads(Path(__file__).with_name('config.toml').read_text(encoding='utf-8'))
    if set(values)-defaults.keys():
        raise ValueError('Unknown state configuration keys')
    config = defaults | values
    for key, value in config.items():
        if key == 'output_dir':
            if not isinstance(value, str) or not value.strip(): raise ValueError(key)
        elif key == 'curvature_offsets_m':
            if (not isinstance(value, list) or not value or value[0] != 0 or
                    any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in value) or
                    sorted(set(value)) != value): raise ValueError(key)
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(key)
    for key in ('acquisition_samples', 'queue_capacity', 'heading_min_windows'):
        if type(config[key]) is not int: raise ValueError(key)
    if config['poll_hz'] > 1000 or config['queue_capacity'] > 1_000_000 or config['display_hz'] > config['poll_hz']:
        raise ValueError('Unreasonable rate/capacity')
    return config


def wrap(angle):
    return math.atan2(math.sin(angle), math.cos(angle))


def percentile(values, q):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered)*q)-1)] if ordered else None


def calibrate(rows_by_lap, eligible_train, identity, config):
    if not eligible_train or len(set(eligible_train)) != len(eligible_train):
        raise ValueError('No unique eligible train calibration laps')
    candidates = [(sign, offset) for sign in (1, -1) for offset in (0, math.pi/2, math.pi, -math.pi/2)]
    errors = [[] for _ in candidates]
    counts = {}
    windows = []
    half = int(config['heading_window_ms']*1e6)
    spacing = int(config['heading_center_spacing_ms']*1e6)
    for lid in eligible_train:
        rows = rows_by_lap[lid]
        times = [int(r['elapsed_ns']) for r in rows]
        if any(b <= a for a, b in zip(times, times[1:])): raise ValueError('Calibration time order')
        last = -spacing
        counts[lid] = 0
        for i, row in enumerate(rows):
            t = times[i]
            if t-last < spacing or float(row['speed_kmh']) < config['heading_min_speed_kmh']: continue
            a = bisect.bisect_right(times, t-half)-1
            b = bisect.bisect_left(times, t+half)
            if a < 0 or b >= len(rows): continue
            # No interpolation across a gap; ~100 ms windows may overshoot by at most one normal poll.
            if t-times[a] > half+config['max_age_ms']*1e6 or times[b]-t > half+config['max_age_ms']*1e6: continue
            if any(times[k+1]-times[k] > config['max_age_ms']*1e6 for k in range(a, b)): continue
            if len({rows[k]['segment_id'] for k in (a, i, b)}) != 1: continue
            points = [(float(rows[k]['position_x']), float(rows[k]['position_z'])) for k in (a, i, b)]
            dx1, dz1 = points[1][0]-points[0][0], points[1][1]-points[0][1]
            dx2, dz2 = points[2][0]-points[1][0], points[2][1]-points[1][1]
            if min(math.hypot(dx1,dz1), math.hypot(dx2,dz2)) < .01: continue
            h1, h2 = math.atan2(dz1,dx1), math.atan2(dz2,dx2)
            if abs(math.degrees(wrap(h2-h1))) > config['heading_max_turn_deg']: continue
            course = math.atan2(dz1+dz2, dx1+dx2)
            heading = float(row['heading_rad'])
            if not all(math.isfinite(v) for v in (heading, course)): raise ValueError('Nonfinite heading window')
            for error, (sign, offset) in zip(errors, candidates):
                error.append(abs(math.degrees(wrap(sign*heading+offset-course))))
            windows.append(dict(lap_id=lid, sequence=int(row['sequence']), elapsed_ns=t,
                                incoming_delta_ns=t-times[a], outgoing_delta_ns=times[b]-t))
            counts[lid] += 1
            last = t
    if len(windows) < config['heading_min_windows']: raise ValueError(f'Insufficient heading windows: {len(windows)}')
    stats = [dict(sign=sign, offset_rad=offset, median_abs_deg=statistics.median(err),
                  p95_abs_deg=percentile(err,.95), max_abs_deg=max(err)) for (sign,offset),err in zip(candidates,errors)]
    stats.sort(key=lambda s:(s['median_abs_deg'],s['p95_abs_deg'],s['sign'],s['offset_rad']))
    best = stats[0]
    gap = stats[1]['median_abs_deg']-best['median_abs_deg']
    if (best['median_abs_deg'] > config['heading_max_median_deg'] or best['p95_abs_deg'] > config['heading_max_p95_deg'] or
            gap < config['heading_min_runner_gap_deg']): raise ValueError('Heading transform is ambiguous or inaccurate')
    return dict(schema_version=1, complete=True, identity=identity, eligible_train=eligible_train,
                sign=best['sign'], offset_rad=best['offset_rad'], windows=len(windows), windows_by_lap=counts,
                candidates=stats, runner_gap_deg=gap, selected_windows=windows,
                limitation='Motion direction validates coordinate correspondence, not absolute body pose accuracy')


def prepare(track_model, review, values=None, *, output_id=None):
    config = config_check(values or {})
    track_model = Path(track_model).resolve()
    locations = input_locations(review)
    output = create_output(config['output_dir'], 'prepared', [track_model, *locations], output_id)
    meta = dict(schema_version=1, tool_version=__version__, kind='state_prepared', complete=False,
                created_unix_ns=time.time_ns(), track_model_path=str(track_model), review_path=str(locations[0]))
    json_write(output/'manifest.json',meta)
    try:
        model = load_reference(track_model)
        reviewed = load_review(locations[0])
        if Path(model['manifest']['review_path']).resolve() != locations[0]: raise ValueError('Model/review mismatch')
        verify_snapshot(model['manifest']['input_sha256'])
        if model['manifest']['input_sha256'] != reviewed['input_sha256']: raise ValueError('Model input inventory mismatch')
        data, usage = reviewed['data'], reviewed['usage']
        identity = data['meta']['source_metadata']['automatic_identity']
        if model['manifest']['identity'] != identity: raise ValueError('Model identity mismatch')
        inputs = reviewed['input_sha256'] | snapshot([track_model/n for n in ('manifest.json','reference.csv','validation.json','report.html')])
        profile = calibrate(data['rows'],usage['eligible']['train'],identity,config)
        eligible = {lid:split for split,lids in usage['eligible'].items() for lid in lids}
        laps = [dict(lap_id=r['lap_id'], ordinal=int(r['ordinal']), split=eligible[r['lap_id']],
                     start_ns=int(r['start_elapsed_ns']),end_ns=int(r['end_elapsed_ns']),segment_id=int(r['segment_id']))
                for r in data['laps'] if r['lap_id'] in eligible]
        replay = dict(source_path=str(data['source']), identity=identity, laps=laps,
                      eligible=usage['eligible'], hold=usage['hold'], exclude=usage['exclude'],
                      diagnostic_policy='All other source ranges are diagnostic only; original decisions/splits unchanged')
        for name,value in (('config.json',config),('heading_profile.json',profile),('replay.json',replay)):
            json_write(output/name,value)
        verify_snapshot(inputs)
        meta.update(input_sha256=inputs, output_sha256={n:digest(output/n) for n in ('config.json','heading_profile.json','replay.json')},complete=True)
    except Exception as exc:
        meta['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        finish_manifest(output,meta)
    return output
