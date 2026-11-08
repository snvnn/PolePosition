import unittest
from types import SimpleNamespace

from .timing import TimeBudget, check_cancelled


class TimingTests(unittest.TestCase):
    def test_wall_clock_rollback_cannot_extend_fixed_monotonic_budget(self):
        now = [10., 1000.]
        clock = SimpleNamespace(monotonic=lambda: now[0], wall=lambda: now[1])
        budget = TimeBudget(3600, clock=clock, deadline=1060)
        now[:] = [69., 900.]
        self.assertFalse(budget.expired())
        now[0] = 70.
        self.assertTrue(budget.expired())

    def test_absolute_deadline_can_expire_before_monotonic_budget(self):
        now = [10., 1000.]
        clock = SimpleNamespace(monotonic=lambda: now[0], wall=lambda: now[1])
        budget = TimeBudget(3600, clock=clock, deadline=1060)
        now[:] = [11., 1060.]
        self.assertTrue(budget.expired())
        restarted = TimeBudget(3600, clock=clock, deadline=1060)
        self.assertTrue(restarted.expired())

    def test_budget_does_not_overwrite_latched_user_cancellation(self):
        reasons = ['f8']
        control = SimpleNamespace(stopped=lambda: True, reason='f8',
                                  cancel=lambda reason: reasons.append(reason) if not reasons else None)
        clock = SimpleNamespace(monotonic=lambda: 100.)
        TimeBudget(1, clock=clock, started=0).cancel_if_expired(control, 'session_time_limit')
        self.assertEqual(reasons, ['f8'])
        with self.assertRaisesRegex(InterruptedError, 'f8'):
            check_cancelled(control)
