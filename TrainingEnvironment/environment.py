"""Gymnasium boundary for a Rust-owned policy/action interval.

The native port must provide v2 features, explicit configured reward and a fresh
post-action observation. This module never writes vJoy or invents missing data.
"""
from dataclasses import dataclass
import math
from typing import Protocol

import gymnasium as gym
import numpy as np


class TransitionUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Observation:
    features: tuple[float, ...]
    sequence: int
    observed_ns: int
    tracking_epoch: int


@dataclass(frozen=True)
class Interval:
    next_observation: Observation | None
    source_sequence: int
    applied_ns: int
    reward: float
    terminated: bool = False
    truncated: bool = False
    reason: str = ''
    system_error: str | None = None


class NativePort(Protocol):
    def reset(self) -> Observation: ...
    def exchange(self, observation_sequence: int, action: tuple[float, float],
                 decision_hz: int) -> Interval: ...
    def stop(self) -> None: ...


class LiveEnvironment(gym.Env):
    metadata = {'render_modes': []}

    def __init__(self, port: NativePort, feature_names: tuple[str, ...], schema_version: int):
        if schema_version != 2 or not feature_names or len(set(feature_names)) != len(feature_names):
            raise ValueError('Explicit unique v2 feature schema required; v1 models cannot be reused')
        self.port, self.feature_names = port, tuple(feature_names)
        self.observation_space = gym.spaces.Box(-np.inf, np.inf, (len(feature_names),), dtype=np.float32)
        self.action_space = gym.spaces.Box(-1., 1., (2,), dtype=np.float32)
        self.previous = None
        self.ended = True
        self.failed = False
        self.closed = False

    def vector(self, observation):
        if (not isinstance(observation, Observation) or type(observation.sequence) is not int
                or observation.sequence < 1 or type(observation.observed_ns) is not int
                or observation.observed_ns < 0 or type(observation.tracking_epoch) is not int
                or observation.tracking_epoch < 0):
            raise TransitionUnavailable('Invalid observation identity/time')
        values = np.asarray(observation.features, dtype=np.float32)
        if values.shape != self.observation_space.shape or not np.isfinite(values).all():
            raise TransitionUnavailable('Missing or nonfinite v2 features')
        return values

    def reset(self, *, seed=None, options=None):
        if self.failed or self.closed:
            raise RuntimeError('Prepare a new job after an environment fault or close')
        super().reset(seed=seed)
        if not self.ended:
            raise RuntimeError('Finish and stop the current episode before resetting')
        try:
            self.previous = self.port.reset()
            values = self.vector(self.previous)
            self.ended = False
            return values, {'feature_schema_version': 2, 'sequence': self.previous.sequence}
        except BaseException:
            self.ended = True
            self.failed = True
            self.port.stop()
            raise

    def step(self, action):
        if self.ended or self.previous is None:
            raise RuntimeError('Call reset before step; terminal episodes cannot restart themselves')
        try:
            action = np.asarray(action, dtype=np.float32)
            if action.shape != (2,) or not np.isfinite(action).all() or np.any(np.abs(action) > 1):
                raise ValueError('Expected normalized steering and signed acceleration')
            interval = self.port.exchange(self.previous.sequence, tuple(float(v) for v in action), 50)
            if interval.system_error:
                raise TransitionUnavailable(interval.system_error)
            following = interval.next_observation
            if following is None:
                raise TransitionUnavailable('No next observation; no replay transition may be added')
            values = self.vector(following)
            if (interval.source_sequence != self.previous.sequence
                    or following.sequence <= self.previous.sequence
                    or type(interval.applied_ns) is not int
                    or not self.previous.observed_ns <= interval.applied_ns < following.observed_ns
                    or following.tracking_epoch != self.previous.tracking_epoch):
                raise TransitionUnavailable('Observation/action interval is stale, mismatched or discontinuous')
            if not math.isfinite(interval.reward):
                raise TransitionUnavailable('Nonfinite configured reward')
            if type(interval.terminated) is not bool or type(interval.truncated) is not bool:
                raise TransitionUnavailable('Invalid termination flags')
            if interval.terminated and interval.truncated:
                raise TransitionUnavailable('Ambiguous task termination and time limit')
            self.previous = following
            self.ended = interval.terminated or interval.truncated
            if self.ended:
                self.port.stop()
            return values, float(interval.reward), interval.terminated, interval.truncated, {
                'sequence': following.sequence, 'source_sequence': interval.source_sequence,
                'applied_ns': interval.applied_ns, 'reason': interval.reason,
            }
        except BaseException:
            self.ended = True
            self.failed = True
            self.port.stop()
            raise

    def close(self):
        self.ended = True
        self.closed = True
        self.port.stop()
