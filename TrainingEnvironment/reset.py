"""Screen AND fresh telemetry gates; no fixed-delay clicking or default tolerances."""
from dataclasses import dataclass
import math
import time
from typing import Callable, Protocol


class ResetFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class StartState:
    sequence: int
    observed_at: float
    car: str
    track: str
    session: str
    position: tuple[float, float, float]
    heading: float
    speed_kmh: float
    fresh: bool


@dataclass(frozen=True)
class ResetStep:
    # Names reference registered screen/action artifacts, not arbitrary shell code.
    before_screen: str
    action: str
    after_screen: str


@dataclass(frozen=True)
class ResetSpec:
    steps: tuple[ResetStep, ...]
    driving_screen: str
    position: tuple[float, float, float]
    heading: float
    position_tolerance_m: float
    heading_tolerance_rad: float
    stopped_kmh: float
    timeout_seconds: float

    def __post_init__(self):
        # These required values must come from a registered profile. No guessed defaults.
        numbers = (*self.position, self.heading, self.position_tolerance_m,
                   self.heading_tolerance_rad, self.stopped_kmh, self.timeout_seconds)
        if len(self.position) != 3 or not all(math.isfinite(v) for v in numbers):
            raise ValueError('Nonfinite/invalid reset geometry')
        if (self.position_tolerance_m < 0 or not 0 <= self.heading_tolerance_rad <= math.pi
                or self.stopped_kmh < 0 or self.timeout_seconds <= 0):
            raise ValueError('Invalid explicit reset tolerances')
        if not self.steps or not self.driving_screen:
            raise ValueError('Registered reset steps and driving screen are required')
        for step in self.steps:
            if not all((step.before_screen, step.action, step.after_screen)):
                raise ValueError('Every input needs before/after screen evidence')
        if self.steps[-1].after_screen != self.driving_screen:
            raise ValueError('Last registered transition must reach driving screen')


class ResetPort(Protocol):
    def screen_matches(self, registered_name: str) -> bool: ...
    def foreground(self) -> bool: ...
    def perform(self, registered_action: str) -> None: ...
    def observe(self) -> StartState | None: ...
    def clear_histories(self) -> None: ...
    def wait_for_change(self, remaining_seconds: float) -> None: ...


class ResetEngine:
    def __init__(self, port: ResetPort, spec: ResetSpec, cancelled: Callable[[], bool],
                 clock: Callable[[], float] = time.monotonic):
        self.port, self.spec, self.cancelled, self.clock = port, spec, cancelled, clock
        self.failed = False

    def check(self, deadline):
        if self.failed:
            raise ResetFailure('Reset failure is latched; a new prepared job is required')
        if self.cancelled():
            raise ResetFailure('Cancelled; automatic restart is forbidden')
        if self.clock() >= deadline:
            raise ResetFailure('Reset timeout')
        if not self.port.foreground():
            raise ResetFailure('ACC foreground lost')

    def wait_screen(self, name, deadline):
        while True:
            self.check(deadline)
            if self.port.screen_matches(name):
                return
            self.port.wait_for_change(max(0., deadline - self.clock()))

    def reset(self, batch_deadline: float) -> StartState:
        deadline = min(batch_deadline, self.clock() + self.spec.timeout_seconds)
        try:
            self.check(deadline)
            before = self.port.observe()
            if before is None or not before.fresh:
                raise ResetFailure('Fresh pre-reset observation required')
            for step in self.spec.steps:
                self.wait_screen(step.before_screen, deadline)
                self.check(deadline)
                self.port.perform(step.action)
                self.wait_screen(step.after_screen, deadline)
            after_input = self.clock()
            while True:
                self.check(deadline)
                if not self.port.screen_matches(self.spec.driving_screen):
                    raise ResetFailure('Driving screen no longer matches')
                observed = self.port.observe()
                if (observed is not None and observed.fresh and observed.sequence > before.sequence
                        and after_input < observed.observed_at <= self.clock()):
                    self.validate_start(observed)
                    self.check(deadline)
                    self.port.clear_histories()
                    return observed
                self.port.wait_for_change(max(0., deadline - self.clock()))
        except BaseException:
            self.failed = True
            raise

    def validate_start(self, observed):
        if (observed.car, observed.track, observed.session) != ('bmw_m4_gt3', 'brands_hatch', 'hotlap'):
            raise ResetFailure('Unsupported car/track/session after reset')
        values = (*observed.position, observed.heading, observed.speed_kmh, observed.observed_at)
        if len(observed.position) != 3 or not all(math.isfinite(v) for v in values):
            raise ResetFailure('Invalid start telemetry')
        distance = math.dist(observed.position, self.spec.position)
        heading_error = abs(math.atan2(math.sin(observed.heading-self.spec.heading),
                                       math.cos(observed.heading-self.spec.heading)))
        if (distance > self.spec.position_tolerance_m or heading_error > self.spec.heading_tolerance_rad
                or not 0 <= observed.speed_kmh <= self.spec.stopped_kmh):
            raise ResetFailure('Unexpected start position, heading or stationary state')

    def verify_three(self, batch_deadline: float) -> tuple[StartState, ...]:
        # No driving or learning callbacks exist on this verification path.
        return tuple(self.reset(batch_deadline) for _ in range(3))
