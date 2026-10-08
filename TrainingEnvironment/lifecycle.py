"""Bounded collect-stop-validate-update-save orchestration, with latched cancellation."""
from dataclasses import dataclass
import math
import time
from typing import Callable, Protocol

SPEED_CAPS = (10, 20, 40, 80, 120, 160, 200, 240)


class BatchStopped(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    episodes: int = 10
    batch_seconds: float = 7200
    episode_seconds: float = 1800

    def __post_init__(self):
        if type(self.episodes) is not int or not 1 <= self.episodes <= 10:
            raise ValueError('Episode limit must be in 1..10')
        for value, cap in ((self.batch_seconds, 7200), (self.episode_seconds, 1800)):
            if not math.isfinite(value) or not 0 < value <= cap:
                raise ValueError('Duration exceeds planned limits')


@dataclass(frozen=True)
class EpisodeResult:
    # Missing next observations must be excluded by experience validation.
    reason: str
    terminated: bool
    truncated: bool
    system_error: str | None = None

    def __post_init__(self):
        if self.terminated and self.truncated:
            raise ValueError('Episode cannot be both task termination and time truncation')
        if not self.system_error and not (self.terminated or self.truncated):
            raise ValueError('Episode must have an explicit terminal or time-limit outcome')


class BatchPort(Protocol):
    def policy_identity(self) -> str: ...
    def reset(self, deadline: float) -> None: ...
    def prepare_start(self, deadline: float) -> None: ...
    def collect(self, deadline: float, speed_cap: int, deterministic: bool,
                check: Callable[[], None]) -> EpisodeResult: ...
    def stop_and_confirm(self) -> bool: ...
    def validate_experience(self, result: EpisodeResult) -> int: ...
    def commit_experience(self) -> None: ...
    def update_once(self) -> None: ...
    def save_checkpoint(self, episode: int, speed_cap: int) -> None: ...
    def save_evaluation(self, result: EpisodeResult, policy: str, speed_cap: int) -> None: ...


@dataclass(frozen=True)
class BatchResult:
    completed_episodes: int
    updates: int
    reason: str


class BatchRunner:
    def __init__(self, port: BatchPort, speed_cap: int, limits: Limits,
                 cancelled: Callable[[], bool], evaluation=False,
                 clock: Callable[[], float] = time.monotonic,
                 progress: Callable[[str], None] = lambda _: None):
        if speed_cap not in SPEED_CAPS:
            raise ValueError('Unknown speed stage')
        self.port, self.speed_cap, self.limits = port, speed_cap, limits
        self.cancelled, self.evaluation, self.clock, self.progress = cancelled, evaluation, clock, progress
        self.stop_reason = None
        self.used = False

    def check(self):
        if self.cancelled() and self.stop_reason is None:
            self.stop_reason = 'cancelled'
        if self.clock() >= self.deadline and self.stop_reason is None:
            self.stop_reason = 'batch_time_limit'
        if self.stop_reason:
            raise BatchStopped(self.stop_reason)

    def phase(self, name):
        self.check()
        self.progress(name)
        self.check()

    def run(self) -> BatchResult:
        if self.used:
            raise RuntimeError('A batch cannot restart itself after completion or failure')
        self.used = True
        self.deadline = self.clock() + self.limits.batch_seconds
        episodes = updates = 0
        try:
            initial_policy = self.port.policy_identity()
            for episode in range(1, self.limits.episodes + 1):
                self.phase('resetting')
                self.port.reset(self.deadline)
                self.phase('preparing')
                self.port.prepare_start(self.deadline)
                self.check()
                policy = self.port.policy_identity()
                if self.evaluation and policy != initial_policy:
                    raise RuntimeError('Evaluation policy changed')
                self.phase('driving')
                result = self.port.collect(min(self.deadline, self.clock()+self.limits.episode_seconds),
                                           self.speed_cap, self.evaluation, self.check)
                # Always stop first, even if collection ended with cancellation/system failure.
                self.progress('stopping')
                if not self.port.stop_and_confirm():
                    raise RuntimeError('Stationary/release confirmation missing')
                self.check()
                if self.port.policy_identity() != policy:
                    raise RuntimeError('Policy changed while driving')
                if result.system_error:
                    raise RuntimeError(result.system_error)
                self.phase('validating')
                count = self.port.validate_experience(result)
                if type(count) is not int or count < 0:
                    raise RuntimeError('Invalid fresh experience count')
                self.check()
                if self.evaluation:
                    self.port.save_evaluation(result, policy, self.speed_cap)
                else:
                    self.port.commit_experience()
                    # Real SAC needs a full replay minibatch; lightweight legacy
                    # ports retain the existing count-based behavior.
                    ready = getattr(self.port, 'replay_ready', lambda: True)()
                    for _ in range(min(count//4, 2000) if ready else 0):
                        self.phase('learning')
                        self.port.update_once()
                        updates += 1
                    self.phase('saving')
                    self.port.save_checkpoint(episode, self.speed_cap)
                self.check()
                episodes += 1
            return BatchResult(episodes, updates, 'episode_limit')
        except BatchStopped as error:
            # No next reset or update, even if the user later clears the stop input.
            if not self.port.stop_and_confirm():
                raise RuntimeError('Stop could not be confirmed') from error
            return BatchResult(episodes, updates, str(error))
        except BaseException:
            self.stop_reason = 'system_error'
            # Do not hide the original error or falsely report successful braking.
            if not self.port.stop_and_confirm():
                raise RuntimeError('System failure and stop unconfirmed')
            raise
