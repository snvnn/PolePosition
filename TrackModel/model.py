"""Build immutable reference artifacts and load them without the source session."""
import math
from pathlib import Path
import time

from DatasetBuilder.builder import csv_write, json_write
from DatasetBuilder.review import (create_output, digest, finish_manifest, read_csv,
                                   read_json, snapshot, verify_snapshot)
from . import __version__
from .config import check_config
from .geometry import FIELDS, generate, path_rows, quality
from .inputs import input_locations, load_review, select_lap
from .report import render

OUTPUTS = ('reference.csv', 'validation.json', 'report.html')
UNITS = dict(index='integer', s_m='m', position_x='m', position_y='m', position_z='m',
             tangent_x='unitless', tangent_z='unitless', heading_path_rad='rad', curvature_per_m='1/m')
COORDINATES = dict(position='ACC raw world XYZ; X-Z planar distance; Y retained',
                   origin='s=0 corresponds to the smoothed first observed point of the selected lap',
                   direction='original sample order', closed=True, duplicate_endpoint=False,
                   heading='atan2(tangent_z, tangent_x), [-pi, pi]',
                   curvature='positive for rotation from +X toward +Z; not calibrated to ACC heading or steering')


def _read_reference(folder, manifest, validation):
    if validation.get('passed') is not True or validation.get('input_pass') is not True:
        raise ValueError('Reference validation did not pass')
    if manifest.get('fields') != FIELDS or manifest.get('units') != UNITS or manifest.get('coordinates') != COORDINATES:
        raise ValueError('Unsupported reference fields, units or coordinate convention')
    config = check_config(manifest['config'])
    rows = []
    for i, raw in enumerate(read_csv(folder/'reference.csv', FIELDS)):
        if raw['index'] != str(i):
            raise ValueError('Reference point indices must start at zero and be contiguous')
        row = {k:float(raw[k]) for k in FIELDS[1:]}
        if not all(math.isfinite(v) for v in row.values()):
            raise ValueError('Nonfinite reference field')
        rows.append(dict(index=i, **row))
    if len(rows) != manifest['points']:
        raise ValueError('Reference point count disagrees')
    points = [tuple(r[k] for k in ('position_x','position_y','position_z')) for r in rows]
    recomputed, length = path_rows(points, config['tangent_span'])
    for actual, expected in zip(rows, recomputed):
        if any(not math.isclose(actual[k],expected[k],rel_tol=1e-9,abs_tol=1e-9) for k in FIELDS):
            raise ValueError('Reference geometry disagrees with its coordinates')
    if not math.isclose(length,manifest['total_length_m'],rel_tol=1e-12,abs_tol=1e-8):
        raise ValueError('Reference length disagrees')
    metrics = validation['metrics']
    if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in metrics.values()):
        raise ValueError('Invalid validation metrics')
    expected_quality = quality(recomputed, metrics['raw_closed_length_m'], metrics['target_spacing_m'],
                               [metrics['max_smoothing_displacement_m']], metrics['closure_m'], config)
    if not expected_quality['passed'] or validation['limits'] != expected_quality['limits']:
        raise ValueError('Reference quality limits disagree')
    if any(not math.isclose(metrics[k],v,rel_tol=1e-9,abs_tol=1e-9) for k,v in expected_quality['metrics'].items()):
        raise ValueError('Reference quality metrics disagree')
    return rows


def load_reference(folder):
    """Return {manifest, rows, validation}; the original recordings are not required."""
    folder = Path(folder).resolve()
    before = snapshot([folder/'manifest.json', *[folder/name for name in OUTPUTS]])
    manifest = read_json(folder/'manifest.json')
    if (manifest.get('kind') != 'track_model' or manifest.get('schema_version') != 1 or
            manifest.get('tool_version') != __version__ or manifest.get('complete') is not True):
        raise ValueError('Incomplete or unsupported TrackModel')
    if set(manifest['output_sha256']) != set(OUTPUTS):
        raise ValueError('Missing reference output fingerprints')
    verify_snapshot({str(folder/name):checksum for name,checksum in manifest['output_sha256'].items()})
    validation = read_json(folder/'validation.json')
    if validation.get('schema_version') != 1:
        raise ValueError('Unsupported validation schema')
    rows = _read_reference(folder, manifest, validation)
    verify_snapshot(before)
    return dict(manifest=manifest, rows=rows, validation=validation)


def build(review, values=None, *, output_id=None):
    config = check_config(values or {})
    locations = input_locations(review)
    output = create_output(config['output_dir'], 'track', locations, output_id)
    manifest = dict(schema_version=1, tool_version=__version__, kind='track_model', complete=False,
                    build_id=output.name, created_unix_ns=time.time_ns(), review_path=str(locations[0]),
                    config=config, fields=FIELDS, units=UNITS, coordinates=COORDINATES)
    json_write(output/'manifest.json', manifest)
    validation = dict(schema_version=1, input_pass=False, passed=False, errors=[])
    geometry = None
    try:
        reviewed = load_review(locations[0])
        data = reviewed['data']
        validation['input_pass'] = True
        manifest['input_sha256'] = reviewed['input_sha256']
        selected, candidates = select_lap(data['laps'], reviewed['usage']['trackmodel_candidates'])
        manifest.update(selected_lap=selected, candidates=candidates, dataset_path=str(data['path']),
                        identity=data['meta']['source_metadata'].get('automatic_identity',{}),
                        selection='minimum (end_elapsed_ns-start_elapsed_ns), then source start order; eligible train only')
        original = data['rows'][selected['lap_id']]
        points = [tuple(float(r[k]) for k in ('position_x','position_y','position_z')) for r in original]
        geometry = generate(points,config)
        validation.update(geometry['validation'])
        manifest.update(points=len(geometry['rows']), total_length_m=geometry['length_m'])
        csv_write(output/'reference.csv', FIELDS, geometry['rows'])
        json_write(output/'validation.json', validation)
        with (output/'report.html').open('x',encoding='utf-8') as stream:
            stream.write(render(manifest, validation, geometry))
        if not validation['passed']:
            raise ValueError('Selected lap failed geometry quality limits; no fallback selection')
        _read_reference(output, manifest, validation)
        verify_snapshot(reviewed['input_sha256'])
        manifest.update(output_sha256={name:digest(output/name) for name in OUTPUTS}, complete=True)
    except Exception as exc:
        message = f'{type(exc).__name__}: {exc}'
        manifest.update(complete=False, error=message)
        validation.update(passed=False, errors=validation['errors']+[message])
        # Diagnostics are best effort if the output device itself has failed.
        try:
            json_write(output/'validation.next.json', validation)
            (output/'validation.next.json').replace(output/'validation.json')
            with (output/'report.html').open('w',encoding='utf-8') as stream:
                stream.write(render(manifest, validation, geometry))
        except OSError:
            pass
        raise
    finally:
        finish_manifest(output, manifest)
    return output
