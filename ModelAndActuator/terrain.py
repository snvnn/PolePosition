"""Descriptive terrain/speed statistics; does not change gains or establish causation."""
import math
import statistics


def analyze(telemetry, controller, summary):
    active={r['observation_sequence']:r for r in controller if r['reason']=='tracking'}
    groups={name:[] for name in ('uphill_gt_2pct','level_within_2pct','downhill_lt_minus2pct')}
    xs=[]
    for row in telemetry:
        command=active.get(row['sequence'])
        if command is None or not row.get('reference_grade_pct'):
            continue
        grade=float(row['reference_grade_pct'])
        entry={'speed':float(row['speed_kmh']),'target':float(command['target_speed_kmh']),
               'throttle':float(row['throttle_observed']),'brake':float(row['brake_observed']),
               'height':float(row['position_y']),'grade':grade}
        if not all(math.isfinite(v) for v in entry.values()):
            raise ValueError('Non-finite terrain observation')
        groups['uphill_gt_2pct' if grade>2 else 'downhill_lt_minus2pct' if grade<-2 else 'level_within_2pct'].append(entry)
        xs.append(entry)
    result={}
    for name,rows in groups.items():
        result[name]={'samples':len(rows)}
        if rows:
            result[name].update(speed_mean_kmh=statistics.mean(v['speed'] for v in rows),speed_max_kmh=max(v['speed'] for v in rows),
                speed_error_mean_kmh=statistics.mean(v['speed']-v['target'] for v in rows),
                speed_error_abs_mean_kmh=statistics.mean(abs(v['speed']-v['target']) for v in rows),
                observed_throttle_mean=statistics.mean(v['throttle'] for v in rows),observed_brake_mean=statistics.mean(v['brake'] for v in rows))
    fractions=[float(r['lap_fraction']) for r in controller if r.get('lap_fraction')]
    return {'lap_completed':summary.get('lap_completed',False),'maximum_lap_fraction':max(fractions,default=0),
            'observed_height_range_m':max(v['height'] for v in xs)-min(v['height'] for v in xs) if xs else None,
            'reference_grade_min_pct':min((v['grade'] for v in xs),default=None),'reference_grade_max_pct':max((v['grade'] for v in xs),default=None),
            'groups':result,'scope':'Sample means for matching controller observations only; includes speed ramp. Partial runs are not full-lap coverage.'}
