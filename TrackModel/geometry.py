"""Ordered planar geometry; no nearest-point sorting, speed profile or control."""
from bisect import bisect_right
import math

EPSILON_M = 1e-6
FIELDS = ['index', 's_m', 'position_x', 'position_y', 'position_z',
          'tangent_x', 'tangent_z', 'heading_path_rad', 'curvature_per_m']


def distance(a, b):
    return math.hypot(a[0]-b[0], a[2]-b[2])


def clean_points(points):
    cleaned = []
    for point in points:
        p = tuple(float(v) for v in point)
        if len(p) != 3 or not all(math.isfinite(v) for v in p):
            raise ValueError('Coordinates must be finite XYZ triples')
        if not cleaned or distance(cleaned[-1], p) > EPSILON_M:
            cleaned.append(p)
    # A closed input may explicitly repeat its first point. Keep only one copy.
    if len(cleaned) > 1 and distance(cleaned[-1], cleaned[0]) <= EPSILON_M:
        cleaned.pop()
    if len(cleaned) < 3:
        raise ValueError('Not enough distinct ordered coordinates')
    return cleaned


def arc_lengths(points):
    cumulative = [0.0]
    for i, point in enumerate(points):
        step = distance(point, points[(i+1) % len(points)])
        if not math.isfinite(step) or step <= EPSILON_M:
            raise ValueError('Duplicate or invalid path segment')
        cumulative.append(cumulative[-1]+step)
    if not math.isfinite(cumulative[-1]):
        raise ValueError('Nonfinite path length')
    return cumulative


def resample(points, spacing_m):
    cumulative = arc_lengths(points)
    requested = cumulative[-1]/spacing_m
    if not math.isfinite(requested) or requested > 1_000_000:
        raise ValueError('Resampling exceeds 1000000 points')
    count = math.ceil(requested)
    if not 3 <= count <= 1_000_000:
        raise ValueError('Resampling requires 3..1000000 points')
    step = cumulative[-1]/count
    result = []
    for i in range(count):
        s = i*step
        j = min(bisect_right(cumulative, s)-1, len(points)-1)
        fraction = (s-cumulative[j])/(cumulative[j+1]-cumulative[j])
        a, b = points[j], points[(j+1) % len(points)]
        result.append(tuple(x+(y-x)*fraction for x, y in zip(a, b)))
    return result, step


def smooth_periodic(points, window):
    if len(points) < window:
        raise ValueError('Too few points for smoothing window')
    offsets = range(-(window//2), window//2+1)
    s2, s4 = sum(k*k for k in offsets), sum(k**4 for k in offsets)
    # Intercept of a quadratic least-squares fit at evenly spaced offsets.
    weights = [(s4-s2*k*k)/(window*s4-s2*s2) for k in offsets]
    result = []
    for i, center in enumerate(points):
        # Local differences preserve precision when world coordinates are large.
        result.append(tuple(center[axis]+sum(w*(points[(i+k) % len(points)][axis]-center[axis])
                            for k, w in zip(offsets, weights)) for axis in range(3)))
    return result


def path_rows(points, span):
    if len(points) <= 2*span:
        raise ValueError('Too few points for tangent/curvature span')
    cumulative = arc_lengths(points)
    rows = []
    for i, point in enumerate(points):
        a, c = points[(i-span) % len(points)], points[(i+span) % len(points)]
        ux, uz = point[0]-a[0], point[2]-a[2]
        vx, vz = c[0]-point[0], c[2]-point[2]
        left, right, chord = math.hypot(ux, uz), math.hypot(vx, vz), math.hypot(ux+vx, uz+vz)
        if min(left, right, chord) <= EPSILON_M:
            raise ValueError('Degenerate tangent or curvature triple')
        tx, tz = (ux+vx)/chord, (uz+vz)/chord
        curvature = 2*(ux*vz-uz*vx)/(left*right*chord)
        values = (i, cumulative[i], *point, tx, tz, math.atan2(tz, tx), curvature)
        if not all(math.isfinite(v) for v in values):
            raise ValueError('Nonfinite path geometry')
        rows.append(dict(zip(FIELDS, values)))
    return rows, cumulative[-1]


def quality(rows, raw_length, target_step, displacements, closure_m, config):
    points = [tuple(r[k] for k in ('position_x','position_y','position_z')) for r in rows]
    cumulative = arc_lengths(points)
    steps = [b-a for a,b in zip(cumulative, cumulative[1:])]
    angles = [r['heading_path_rad'] for r in rows]
    differences = [abs(math.degrees(math.atan2(math.sin(b-a), math.cos(b-a))))
                   for a,b in zip(angles, angles[1:]+angles[:1])]
    metrics = dict(raw_closed_length_m=raw_length, total_length_m=cumulative[-1],
                   length_change_ratio=abs(cumulative[-1]-raw_length)/raw_length,
                   closure_m=closure_m, max_smoothing_displacement_m=max(displacements),
                   target_spacing_m=target_step, min_spacing_m=min(steps), max_spacing_m=max(steps),
                   max_spacing_error_ratio=max(abs(s-target_step)/target_step for s in steps),
                   max_tangent_step_degrees=max(differences), seam_tangent_step_degrees=differences[-1],
                   max_abs_curvature_per_m=max(abs(r['curvature_per_m']) for r in rows), points=len(rows))
    checks = dict(closure_m=config['max_closure_m'],
                  max_smoothing_displacement_m=config['max_smoothing_displacement_m'],
                  length_change_ratio=config['max_length_change_ratio'],
                  max_spacing_error_ratio=config['max_spacing_error_ratio'],
                  max_tangent_step_degrees=config['max_tangent_step_degrees'])
    failures = [f'{key}: {metrics[key]:.12g} exceeds {limit:.12g}'
                for key,limit in checks.items() if metrics[key] > limit]
    return dict(passed=not failures, metrics=metrics, limits=checks, errors=failures)


def generate(points, config):
    raw = [tuple(float(v) for v in point) for point in points]
    cleaned = clean_points(raw)
    closure = distance(raw[-1], raw[0])
    if closure > config['max_closure_m']:
        raise ValueError(f'Closure {closure:.6f}m exceeds {config["max_closure_m"]}m')
    raw_length = arc_lengths(cleaned)[-1]
    first_grid, first_step = resample(cleaned, config['spacing_m'])
    smooth = smooth_periodic(first_grid, config['smoothing_window'])
    displacement = [distance(a,b) for a,b in zip(first_grid, smooth)]
    final, target_step = resample(smooth, config['spacing_m'])
    rows, length = path_rows(final, config['tangent_span'])
    validation = quality(rows, raw_length, target_step, displacement, closure, config)
    validation['metrics'].update(raw_points=len(raw), deduplicated_points=len(cleaned), first_grid_points=len(first_grid))
    return dict(rows=rows, raw=cleaned, length_m=length, validation=validation,
                displacement=[dict(s_m=i*first_step, displacement_m=d) for i,d in enumerate(displacement)])
