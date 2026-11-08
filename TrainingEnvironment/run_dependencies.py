"""Explicit effect boundaries shared by the separate gamepad workflows.

Defaults are resolved by each workflow at call time, preserving compatibility
with existing callers and allowing offline tests to supply entirely fake ports.
"""
from dataclasses import dataclass
from typing import Callable
from ExecutionRuntime.timing import Clock, SystemClock


@dataclass(frozen=True)
class RunDependencies:
    clock: Clock | None = None
    native_factory: Callable | None = None
    reset_factory: Callable | None = None
    train: Callable | None = None
    compare: Callable | None = None
    f8_down: Callable | None = None

    def time_source(self):
        return self.clock if self.clock is not None else SystemClock()

    def reset(self, profile, check, progress):
        if self.reset_factory is not None:
            return self.reset_factory(profile, check, progress)
        from .acc_reset import AutoReset
        return AutoReset(profile, check, progress)
