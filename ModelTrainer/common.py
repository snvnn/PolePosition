"""Immutable run inventories; mutable manifests only inside their new output folder."""
import hashlib
import json
import math
import os
from pathlib import Path
import time
import tomllib
import uuid

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ['speed_kmh', 'target_speed_kmh', 'speed_error_kmh', 'rpm', 'lateral_error_m',
          'heading_error_sin', 'heading_error_cos', 'reference_grade_pct',
          'curvature_0_per_m', 'curvature_10_per_m', 'curvature_20_per_m',
          'curvature_40_per_m', 'curvature_80_per_m', 'speed_100ms_kmh', 'error_100ms_kmh',
          'speed_200ms_kmh', 'error_200ms_kmh', 'speed_500ms_kmh', 'error_500ms_kmh']
UNITS = ['km/h','km/h','km/h','rpm','m','1','1','percent']+['1/m']*5+['km/h']*6


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, value, replace=False):
    path = Path(path)
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if not replace:
        with path.open('x', encoding='utf-8', newline='\n') as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
    else:
        tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        with tmp.open('x', encoding='utf-8', newline='\n') as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)


def new_folder(parent, prefix):
    p = Path(parent) / f'{prefix}-{time.time_ns()}-{uuid.uuid4().hex[:8]}'
    p.mkdir(parents=True, exist_ok=False)
    return p.resolve()


def hashes(paths):
    return {str(Path(p).resolve()): digest(p) for p in paths}


def verify_hashes(mapping, base=None):
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError('Empty hash inventory')
    for name, expected in mapping.items():
        p = (Path(base) / name).resolve() if base else Path(name)
        if base and not p.is_relative_to(Path(base).resolve()):
            raise ValueError('Output path escape')
        if digest(p) != expected:
            raise ValueError(f'Changed input/output: {p}')


def config(path):
    c = tomllib.loads(Path(path).read_text(encoding='utf-8'))
    expected = {'schema_version', 'seed', 'batch_size', 'max_epochs', 'patience', 'learning_rate',
                'weight_decay', 'beta', 'gradient_clip', 'sample_interval_ms', 'output_dir',
                'dataset_dir', 'export_dir', 'threads'}
    if set(c) != expected or c['schema_version'] != 1:
        raise ValueError('Unsupported training configuration')
    for key in expected - {'output_dir', 'dataset_dir', 'export_dir'}:
        if not isinstance(c[key], (float, int)) or not math.isfinite(c[key]) or c[key] <= 0:
            raise ValueError(f'Invalid configuration: {key}')
    for key in ['seed', 'batch_size', 'max_epochs', 'patience', 'sample_interval_ms', 'threads']:
        if type(c[key]) is not int: raise ValueError(f'Integer required: {key}')
    if c['max_epochs'] > 200 or c['threads'] != 1 or c['sample_interval_ms'] != 50:
        raise ValueError('Configuration exceeds v1 training envelope')
    return c


def finish(folder, manifest, complete=True, error=None):
    manifest.update(complete=complete)
    if error: manifest['error'] = str(error)
    manifest['output_sha256'] = {p.name: digest(p) for p in sorted(Path(folder).iterdir())
                                 if p.is_file() and p.name != 'manifest.json' and not p.name.endswith('.tmp')}
    save(Path(folder) / 'manifest.json', manifest, replace=True)


def load_artifact(folder, kind, complete=True):
    folder = Path(folder).resolve()
    m = read(folder / 'manifest.json')
    if m.get('kind') != kind or m.get('schema_version') != 1 or (complete and m.get('complete') is not True):
        raise ValueError('Incomplete/unsupported artifact')
    verify_hashes(m['output_sha256'], folder)
    verify_hashes(m['input_sha256'])
    return folder, m


def rows(path):
    with Path(path).open(encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def classify(throttle, brake):
    if throttle > .005 and brake > .005: raise ValueError('Teacher requests overlapping pedals')
    return 'brake' if brake > .005 else 'throttle' if throttle > .005 else 'coast'
