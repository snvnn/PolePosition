"""Validated, explicit geometric quality limits."""
import math

DEFAULTS = dict(output_dir='track_models', spacing_m=1.0, smoothing_window=11,
                tangent_span=5, max_closure_m=5.0, max_smoothing_displacement_m=.5,
                max_length_change_ratio=.005, max_spacing_error_ratio=.01,
                max_tangent_step_degrees=10.0)


def check_config(values):
    if not isinstance(values, dict) or set(values)-DEFAULTS.keys():
        raise ValueError('Unknown TrackModel config key')
    config = DEFAULTS | values
    for key, value in config.items():
        if key == 'output_dir':
            if not isinstance(value, str) or not value.strip():
                raise ValueError('Invalid output_dir')
        elif key in ('smoothing_window', 'tangent_span'):
            if type(value) is not int or not 1 <= value <= 201:
                raise ValueError(f'Invalid {key}')
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'Invalid {key}')
    if config['smoothing_window'] < 5 or config['smoothing_window'] % 2 == 0:
        raise ValueError('smoothing_window must be odd and at least 5')
    if max(config['max_length_change_ratio'], config['max_spacing_error_ratio']) >= 1:
        raise ValueError('Ratio limits must be below 1')
    if config['max_tangent_step_degrees'] >= 180:
        raise ValueError('Tangent step limit must be below 180 degrees')
    return config
