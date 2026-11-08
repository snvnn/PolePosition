"""Injectable time sources and fixed budgets; restarting a phase cannot extend them."""
import time
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def wall(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self):
        return time.monotonic()

    def wall(self):
        return time.time()

    def sleep(self, seconds):
        time.sleep(seconds)


class TimeBudget:
    def __init__(self, seconds, *, clock=None, deadline=None, started=None):
        self.clock = clock if clock is not None else SystemClock()
        self.started = self.clock.monotonic() if started is None else started
        self.deadline = deadline
        self.seconds = min(seconds, deadline - self.clock.wall()) if deadline is not None else seconds

    def elapsed(self):
        return self.clock.monotonic() - self.started

    def expired(self):
        return self.elapsed() >= self.seconds or (
            self.deadline is not None and self.clock.wall() >= self.deadline)

    def cancel_if_expired(self, control, reason):
        if self.expired():
            control.cancel(reason)


def check_cancelled(control):
    if control.stopped():
        raise InterruptedError(control.reason or 'user_stop')
