import importlib.util
import unittest

AVAILABLE = importlib.util.find_spec('gymnasium') is not None


@unittest.skipUnless(AVAILABLE, 'Use the separate .venv-controller environment')
class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        from .environment import Interval, LiveEnvironment, Observation
        self.Interval, self.Observation = Interval, Observation
        class Port:
            def __init__(self):
                self.stops = 0
                self.interval = Interval(Observation((1., 2.), 2, 30_000_000, 1), 1, 10_000_000, 1.)
            def reset(self): return Observation((0., 0.), 1, 0, 1)
            def exchange(self, sequence, action, hz):
                self.exchange_args = (sequence, action, hz)
                return self.interval
            def stop(self): self.stops += 1
        self.port = Port()
        # Synthetic test schema only. Production schema remains an unresolved task.
        self.env = LiveEnvironment(self.port, ('test_a', 'test_b'), 2)

    def test_matching_interval_preserves_action_and_observation(self):
        self.env.reset(seed=42)
        _, reward, terminated, truncated, info = self.env.step((.5, -.5))
        self.assertEqual(self.port.exchange_args, (1, (.5, -.5), 50))
        self.assertEqual((reward, terminated, truncated), (1., False, False))
        self.assertEqual(info['applied_ns'], 10_000_000)

    def test_missing_next_observation_is_not_a_fake_terminal_transition(self):
        from .environment import TransitionUnavailable
        self.env.reset()
        self.port.interval = self.Interval(None, 1, 10_000_000, 0.)
        with self.assertRaises(TransitionUnavailable): self.env.step((0., 0.))
        self.assertEqual(self.port.stops, 1)
        with self.assertRaises(RuntimeError): self.env.step((0., 0.))
        with self.assertRaises(RuntimeError): self.env.reset()

    def test_stale_epoch_or_time_is_rejected(self):
        from .environment import TransitionUnavailable
        for sequence, stamp, epoch in [(1, 30_000_000, 1), (2, 5_000_000, 1), (2, 30_000_000, 2)]:
            self.setUp()
            self.env.reset()
            self.port.interval = self.Interval(self.Observation((0., 0.), sequence, stamp, epoch),
                                               1, 10_000_000, 0.)
            with self.assertRaises(TransitionUnavailable): self.env.step((0., 0.))

    def test_time_limit_and_task_termination_remain_distinct(self):
        for terminated, truncated in [(True, False), (False, True)]:
            self.env.reset()
            self.port.interval = self.Interval(self.Observation((0., 0.), 2, 30_000_000, 1),
                                               1, 10_000_000, 0., terminated, truncated)
            result = self.env.step((0., 0.))
            self.assertEqual(result[2:4], (terminated, truncated))
            with self.assertRaises(RuntimeError): self.env.step((0., 0.))

    def test_invalid_action_stops_before_exchange(self):
        for action in [(2., 0.), (float('nan'), 0.), (0.,)]:
            self.setUp()
            self.env.reset()
            with self.assertRaises(ValueError): self.env.step(action)
            self.assertFalse(hasattr(self.port, 'exchange_args'))


if __name__ == '__main__': unittest.main()
