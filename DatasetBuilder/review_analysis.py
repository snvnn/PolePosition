"""Deterministic candidate detection. Flags are evidence, never crash labels."""
import math
from statistics import median

SIGNALS = ('position_x', 'position_z', 'speed_kmh', 'steering_raw')
RELATIVE = ('trajectory_difference', 'speed_difference', 'steering_difference')


def bin_index(progress, bins):
    return min(bins - 1, max(0, int(progress * bins)))


def representatives(rows, bins):
    groups = {}
    for row in rows:
        bucket = bin_index(float(row['progress']), bins)
        groups.setdefault(bucket, []).append(row)
    return {b: {k: median(float(r[k]) for r in group) for k in SIGNALS}
            for b, group in sorted(groups.items())}


def center_mad(values):
    center = median(values)
    return center, median(abs(v-center) for v in values)


def baseline(profiles, reference_ids, config):
    result = {}
    for b in range(config['progress_bins']):
        values = [profiles[lid][b] for lid in reference_ids if b in profiles[lid]]
        if len(values) < config['min_reference_laps']:
            continue
        centers = {k: median(v[k] for v in values) for k in SIGNALS}
        distances = [math.hypot(v['position_x']-centers['position_x'],
                                v['position_z']-centers['position_z']) for v in values]
        distance_center, distance_mad = center_mad(distances)
        _, speed_mad = center_mad([v['speed_kmh'] for v in values])
        _, steering_mad = center_mad([v['steering_raw'] for v in values])
        result[b] = dict(count=len(values), **centers, distance_median_m=distance_center,
                         distance_mad_m=distance_mad, speed_mad_kmh=speed_mad,
                         steering_mad=steering_mad,
                         trajectory_threshold_m=max(config['trajectory_floor_m'], distance_center+config['mad_multiplier']*distance_mad),
                         speed_threshold_kmh=max(config['speed_floor_kmh'], config['mad_multiplier']*speed_mad),
                         steering_threshold=max(config['steering_floor'], config['mad_multiplier']*steering_mad))
    return result


def reference_models(rows_by_lap, train_ids, config):
    # Validation/test values never enter these profiles, even when already inspected.
    profiles = {lid: representatives(rows_by_lap[lid], config['progress_bins']) for lid in train_ids}
    return dict(train_ids=list(train_ids), global_baseline=baseline(profiles, train_ids, config),
                leave_one_out={lid: baseline(profiles, [x for x in train_ids if x != lid], config)
                               for lid in train_ids})


def raw_evidence(row, index, reason, observed, threshold, **extra):
    return dict(index=index, reason=reason, sequence=int(row['sequence']),
                elapsed_ns=int(row['elapsed_ns']), progress=float(row['progress']),
                observed=observed, threshold=threshold, **extra)


def detect(rows, reference, config):
    """One lap only. Every duration and context boundary uses monotonic time."""
    for a, b in zip(rows, rows[1:]):
        if (int(a['segment_id']) != int(b['segment_id']) or
                int(b['elapsed_ns']) <= int(a['elapsed_ns'])):
            raise ValueError('Detection requires one contiguous lap')
    times = [int(r['elapsed_ns']) for r in rows]
    hits = {k: [] for k in (*RELATIVE, 'low_speed', 'reverse_progress', 'position_discontinuity', 'insufficient_reference')}
    j = 0
    window_ns = round(config['window_seconds']*1e9)
    tolerance_ns = round(config['window_tolerance_seconds']*1e9)
    last_wrap_index = -1
    for i, row in enumerate(rows):
        b = bin_index(float(row['progress']), config['progress_bins'])
        ref = reference.get(b)
        if ref is None:
            hits['insufficient_reference'].append(raw_evidence(row, i, 'insufficient_reference',
                                                               None, config['min_reference_laps'], progress_bin=b))
        else:
            for reason, observed, threshold in (
                ('trajectory_difference', math.hypot(float(row['position_x'])-ref['position_x'], float(row['position_z'])-ref['position_z']), ref['trajectory_threshold_m']),
                ('speed_difference', abs(float(row['speed_kmh'])-ref['speed_kmh']), ref['speed_threshold_kmh']),
                ('steering_difference', abs(float(row['steering_raw'])-ref['steering_raw']), ref['steering_threshold'])):
                if observed > threshold:
                    hits[reason].append(raw_evidence(row, i, reason, observed, threshold, progress_bin=b,
                                                     reference_count=ref['count'], reference=ref))
        speed = float(row['speed_kmh'])
        if speed < config['low_speed_kmh']:
            hits['low_speed'].append(raw_evidence(row, i, 'low_speed', speed, config['low_speed_kmh']))
        if i and float(rows[i-1]['progress']) > .8 and float(row['progress']) < .2:
            last_wrap_index = i
        while j+1 < i and times[j+1] <= times[i]-window_ns:
            j += 1
        delta_ns = times[i]-times[j]
        if not window_ns <= delta_ns <= window_ns+tolerance_ns or last_wrap_index > j:
            continue
        progress_delta = float(row['progress'])-float(rows[j]['progress'])
        context = dict(window_start_sequence=int(rows[j]['sequence']), window_start_elapsed_ns=times[j])
        if progress_delta <= -config['reverse_progress']:
            hits['reverse_progress'].append(raw_evidence(row, i, 'reverse_progress', progress_delta,
                                                         -config['reverse_progress'], **context))
        distance = math.hypot(float(row['position_x'])-float(rows[j]['position_x']),
                              float(row['position_z'])-float(rows[j]['position_z']))
        limit = (max(float(r['speed_kmh']) for r in rows[j:i+1])/3.6 * delta_ns/1e9 *
                 config['displacement_speed_multiplier'] + config['displacement_margin_m'])
        if distance > limit:
            hits['position_discontinuity'].append(raw_evidence(row, i, 'position_discontinuity', distance, limit, **context))
    episodes = []
    for reason, samples in hits.items():
        minimum = (config['relative_duration_seconds'] if reason in RELATIVE else
                   config['low_speed_seconds'] if reason == 'low_speed' else 0)
        run = []
        for hit in samples + [None]:
            if run and (hit is None or hit['index'] != run[-1]['index']+1):
                # Last true sample must itself reach the duration threshold.
                if run[-1]['elapsed_ns']-run[0]['elapsed_ns'] >= round(minimum*1e9):
                    episodes.append(dict(reason=reason, start_index=run[0]['index'], end_index=run[-1]['index'],
                                         evidence=run))
                run = []
            if hit is not None:
                run.append(hit)
    return merge_episodes(episodes, rows, config['merge_gap_seconds'])


def merge_episodes(episodes, rows, merge_gap_seconds):
    groups = []
    for episode in sorted(episodes, key=lambda e: (e['start_index'], e['end_index'], e['reason'])):
        start, end = episode['start_index'], episode['end_index']
        if groups and int(rows[start]['elapsed_ns'])-groups[-1]['end_elapsed_ns'] <= round(merge_gap_seconds*1e9):
            group = groups[-1]
            group['end_index'] = max(group['end_index'], end)
        else:
            group = dict(start_index=start, end_index=end, reasons=[], evidence=[])
            groups.append(group)
        group['start_sequence'] = int(rows[group['start_index']]['sequence'])
        group['end_sequence'] = int(rows[group['end_index']]['sequence'])
        group['start_elapsed_ns'] = int(rows[group['start_index']]['elapsed_ns'])
        group['end_elapsed_ns'] = int(rows[group['end_index']]['elapsed_ns'])
        group['start_progress'] = float(rows[group['start_index']]['progress'])
        group['end_progress'] = float(rows[group['end_index']]['progress'])
        group['reasons'] = sorted(set(group['reasons'] + [episode['reason']]))
        group['evidence'].extend(episode['evidence'])
    for group in groups:
        group['evidence'].sort(key=lambda e: (e['sequence'], e['reason']))
    return groups
