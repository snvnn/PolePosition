"""Lap evaluation over validated native observations and transmitted-control spans.

This module is the calculation layer, not a raw-log verifier or live authorization.
The native v2 adapter must establish freshness, continuity and transmission evidence.
"""
from dataclasses import dataclass
import math
import re

from DrivingRecorder.laps import FinishLineTracker
from .lifecycle import SPEED_CAPS


@dataclass(frozen=True)
class ControlSpan:
    start_ns: int
    end_ns: int
    policy_sha256: str
    steering_source: str
    pedals_source: str
    deterministic: bool
    learning_updates: int
    replay_insertions: int


@dataclass(frozen=True)
class EvaluatedLap:
    source_sha256: str
    segment_id: int
    start_ns: int
    end_ns: int
    policy_sha256: str
    speed_cap: int
    reasons: tuple[str, ...]

    @property
    def valid(self):
        return not self.reasons

    @property
    def duration_seconds(self):
        return (self.end_ns - self.start_ns) / 1e9


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('Expected SHA-256 identity')


class LapEvaluation:
    """One immutable model/stage, one source, increasing validated observations.

    Each span must cover the whole interval since the previous observation. Missing
    or mixed control cannot be inferred from the command at the ending sample.
    Separate segments never form a lap together.
    """

    def __init__(self, source_sha256, policy_sha256, speed_cap):
        _hash(source_sha256)
        _hash(policy_sha256)
        if type(speed_cap) is not int or speed_cap not in SPEED_CAPS:
            raise ValueError('Unknown speed stage')
        self.source, self.policy, self.speed_cap = source_sha256, policy_sha256, speed_cap
        self.tracker = FinishLineTracker()
        self.previous = None
        self.start = None
        self.reasons = set()
        self.laps = []

    def observe(self, row, span: ControlSpan | None, *, continuous: bool):
        """row uses recorder lap fields; continuous comes from existing guard checks."""
        for name in ('elapsed_ns', 'segment_id', 'completed_laps', 'lap_time_ms'):
            if type(row[name]) is not int or row[name] < 0:
                raise ValueError('Invalid lap time/count')
        if (type(row['lap_valid']) is not bool or type(continuous) is not bool
                or not math.isfinite(row['progress']) or not 0 <= row['progress'] <= 1):
            raise ValueError('Invalid lap observation')
        prior = self.previous
        if prior is not None and (row['elapsed_ns'] <= prior['elapsed_ns']
                                  or row['segment_id'] < prior['segment_id']):
            raise ValueError('Observation time/segment reversed')
        interval_reasons = set()
        if not continuous:
            interval_reasons.add('continuity_not_verified')
        if prior is None or prior['segment_id'] != row['segment_id']:
            self.start = None
            self.reasons.clear()
        else:
            if span is None:
                interval_reasons.add('control_evidence_missing')
            else:
                if (type(span.start_ns) is not int or type(span.end_ns) is not int
                        or not 0 <= span.start_ns <= prior['elapsed_ns']
                        or span.end_ns < row['elapsed_ns']):
                    interval_reasons.add('control_interval_uncovered')
                if span.policy_sha256 != self.policy:
                    interval_reasons.add('policy_changed')
                if span.steering_source != 'model' or span.pedals_source != 'model':
                    interval_reasons.add('model_did_not_control_all_axes')
                if span.deterministic is not True:
                    interval_reasons.add('exploration_enabled')
                if type(span.learning_updates) is not int or span.learning_updates != 0:
                    interval_reasons.add('learning_during_evaluation')
                if type(span.replay_insertions) is not int or span.replay_insertions != 0:
                    interval_reasons.add('evaluation_entered_replay')
        boundary, discontinuity = self.tracker.observe(row)
        if discontinuity:
            interval_reasons.add('lap_discontinuity')
        self.reasons.update(interval_reasons)
        completed = None
        if boundary is not None:
            if self.start is not None:
                if not boundary[3]:
                    self.reasons.add('game_lap_invalid')
                completed = EvaluatedLap(self.source, row['segment_id'], self.start,
                                         row['elapsed_ns'], self.policy, self.speed_cap,
                                         tuple(sorted(self.reasons)))
                self.laps.append(completed)
            self.start = row['elapsed_ns']
            # The interval before the first crossing proves takeover before it.
            self.reasons = set(interval_reasons)
        self.previous = dict(row)
        return completed


def next_stage_candidate(laps, policy_sha256, speed_cap):
    """Calculate the three-lap rule; does not authorize a native/GUI speed unlock.

    Repeated imports and overlapping spans cannot inflate the valid lap count.
    Proof verification and live integration remain the caller's responsibility.
    """
    _hash(policy_sha256)
    if type(speed_cap) is not int or speed_cap not in SPEED_CAPS:
        raise ValueError('Unknown speed stage')
    by_source = {}
    for lap in laps:
        if not lap.valid or lap.policy_sha256 != policy_sha256 or lap.speed_cap != speed_cap:
            continue
        _hash(lap.source_sha256)
        if lap.end_ns <= lap.start_ns:
            raise ValueError('Invalid lap interval')
        by_source.setdefault(lap.source_sha256, set()).add((lap.start_ns, lap.end_ns))
    count = 0
    for intervals in by_source.values():
        end = -1
        for start, stop in sorted(intervals):
            if start < end:
                raise ValueError('Overlapping evaluation laps')
            count += 1
            end = stop
    index = SPEED_CAPS.index(speed_cap)
    return SPEED_CAPS[index + 1] if count >= 3 and index + 1 < len(SPEED_CAPS) else None
