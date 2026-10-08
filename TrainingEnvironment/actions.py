"""v2 normalized action algebra; actuator limits remain the Rust guard's job."""
import math


def from_axes(steering, throttle, brake):
    values = (steering, throttle, brake)
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        raise ValueError('Nonfinite axes')
    if not -1 <= steering <= 1 or not 0 <= throttle <= 1 or not 0 <= brake <= 1:
        raise ValueError('Axes outside normalized range')
    if throttle > 0 and brake > 0:
        raise ValueError('Overlapping pedals cannot be represented by signed acceleration')
    return (float(steering), float(throttle-brake))


def to_axes(action):
    if len(action) != 2 or not all(math.isfinite(v) and -1 <= v <= 1 for v in action):
        raise ValueError('Expected two normalized action values')
    steering, acceleration = action
    return (float(steering), max(float(acceleration), 0.), max(-float(acceleration), 0.))
