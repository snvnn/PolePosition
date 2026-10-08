import unittest
from dataclasses import replace

from .lifecycle import BatchRunner, EpisodeResult, Limits
from .reset import ResetEngine, ResetFailure, ResetSpec, ResetStep, StartState


class Clock:
    def __init__(self): self.now = 100.
    def __call__(self): return self.now
    def advance(self, seconds=1.): self.now += seconds


class ResetFixture:
    def __init__(self, clock):
        self.clock = clock
        self.screen = 'menu'
        self.actions = []
        self.clears = 0
        self.sequence = 1
        self.block = False
        self.position = (0., 0., 0.)
        self.front = True

    def screen_matches(self, name): return self.screen == name
    def foreground(self): return self.front
    def perform(self, action):
        self.actions.append(action)
        if not self.block: self.screen = 'driving'
    def observe(self):
        self.clock.advance(.01)
        self.sequence += 1
        return StartState(self.sequence, self.clock(), 'bmw_m4_gt3', 'brands_hatch', 'hotlap',
                          self.position, 0., 0., True)
    def clear_histories(self): self.clears += 1
    def wait_for_change(self, remaining_seconds): self.clock.advance(min(.1, remaining_seconds))


def spec():
    # Test-only tolerances, not production defaults or accepted calibration.
    return ResetSpec((ResetStep('menu', 'restart', 'driving'),), 'driving',
                     (0., 0., 0.), 0., .1, .1, .1, 2.)


class ResetTests(unittest.TestCase):
    def test_screen_and_fresh_telemetry_then_history_clear(self):
        clock = Clock(); port = ResetFixture(clock)
        observed = ResetEngine(port, spec(), lambda: False, clock).reset(clock()+10)
        self.assertGreater(observed.sequence, 1)
        self.assertEqual(port.clears, 1)
        self.assertEqual(port.actions, ['restart'])

    def test_screen_failure_timeout_and_latched_no_next_input(self):
        clock = Clock(); port = ResetFixture(clock); port.block = True
        engine = ResetEngine(port, spec(), lambda: False, clock)
        with self.assertRaisesRegex(ResetFailure, 'timeout'): engine.reset(clock()+10)
        self.assertEqual(port.clears, 0)
        port.block = False
        with self.assertRaisesRegex(ResetFailure, 'latched'): engine.reset(clock()+10)
        self.assertEqual(port.actions, ['restart'])

    def test_wrong_start_never_clears_histories_or_starts_next_reset(self):
        clock = Clock(); port = ResetFixture(clock); port.position = (100., 0., 0.)
        with self.assertRaisesRegex(ResetFailure, 'Unexpected start'):
            ResetEngine(port, spec(), lambda: False, clock).verify_three(clock()+10)
        self.assertEqual(port.actions, ['restart'])
        self.assertEqual(port.clears, 0)

    def test_three_verifications_do_not_drive_or_learn(self):
        clock = Clock(); port = ResetFixture(clock); port.screen = 'driving'
        profile = replace(spec(), steps=(ResetStep('driving', 'restart', 'driving'),))
        result = ResetEngine(port, profile, lambda: False, clock).verify_three(clock()+10)
        self.assertEqual(len(result), 3)
        self.assertEqual(port.clears, 3)
        self.assertEqual(port.actions, ['restart']*3)

    def test_cancellation_and_foreground_loss_send_no_input(self):
        for cancelled, foreground in [(True, True), (False, False)]:
            clock = Clock(); port = ResetFixture(clock); port.front = foreground
            with self.assertRaises(ResetFailure):
                ResetEngine(port, spec(), lambda: cancelled, clock).reset(clock()+10)
            self.assertFalse(port.actions)

    def test_stale_post_reset_observation_times_out(self):
        clock = Clock(); port = ResetFixture(clock)
        old = port.observe()
        port.observe = lambda: old
        with self.assertRaisesRegex(ResetFailure, 'timeout'):
            ResetEngine(port, spec(), lambda: False, clock).reset(clock()+10)
        self.assertEqual(port.clears, 0)


class BatchFixture:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.policy = 'policy-v2-hash'
        self.moving = False
        self.count = 8
        self.fail_save = False
        self.cancelled = False
        self.collection_error = None
        self.stop_confirmed = True
        self.mutate_policy = False
        self.elapsed_per_collection = .1

    def policy_identity(self): return self.policy
    def reset(self, deadline): self.calls.append('reset')
    def prepare_start(self, deadline): self.calls.append('prepare')
    def collect(self, deadline, speed_cap, deterministic, check):
        self.moving = True
        self.calls.append(('collect', speed_cap, deterministic))
        self.clock.advance(self.elapsed_per_collection)
        if self.mutate_policy: self.policy = 'changed'
        check()
        return EpisodeResult('valid_lap', True, False, self.collection_error)
    def stop_and_confirm(self):
        self.calls.append('stop')
        self.moving = not self.stop_confirmed
        return self.stop_confirmed
    def validate_experience(self, result):
        self.calls.append('validate')
        return self.count
    def commit_experience(self): self.calls.append('commit')
    def update_once(self):
        if self.moving: raise AssertionError('Updated policy while moving')
        self.calls.append('update')
        self.policy += '-update'
    def save_checkpoint(self, episode, speed_cap):
        self.calls.append('save')
        if self.fail_save: raise OSError('disk full')
    def save_evaluation(self, result, policy, speed_cap): self.calls.append('evaluation')


class BatchTests(unittest.TestCase):
    def make(self, **kwargs):
        self.clock = Clock(); self.port = BatchFixture(self.clock)
        return BatchRunner(self.port, 10, kwargs.pop('limits', Limits()),
                           lambda: self.port.cancelled, clock=self.clock, **kwargs)

    def test_ten_episode_limit_with_fixed_stage_and_bounded_updates(self):
        runner = self.make()
        self.port.count = 20_000
        result = runner.run()
        self.assertEqual(result.completed_episodes, 10)
        self.assertEqual(result.updates, 20_000)
        self.assertEqual(self.port.calls.count('reset'), 10)
        self.assertEqual(set(c for c in self.port.calls if isinstance(c, tuple)), {('collect', 10, False)})
        with self.assertRaises(RuntimeError): runner.run()

    def test_update_only_after_stop_and_validation(self):
        self.make(limits=Limits(episodes=1)).run()
        self.assertEqual(self.port.calls, ['reset', 'prepare', ('collect', 10, False),
                                         'stop', 'validate', 'commit', 'update', 'update', 'save'])

    def test_insufficient_replay_is_saved_without_updates(self):
        runner = self.make(limits=Limits(episodes=1))
        self.port.replay_ready = lambda: False
        result = runner.run()
        self.assertEqual(result.updates, 0)
        self.assertIn('commit', self.port.calls)
        self.assertIn('save', self.port.calls)
        self.assertNotIn('update', self.port.calls)

    def test_evaluation_never_commits_experience_or_updates(self):
        runner = self.make(evaluation=True)
        result = runner.run()
        self.assertEqual(result.updates, 0)
        self.assertNotIn('commit', self.port.calls)
        self.assertNotIn('save', self.port.calls)
        self.assertEqual(self.port.calls.count('evaluation'), 10)
        self.assertIn(('collect', 10, True), self.port.calls)

    def test_save_failure_blocks_next_episode(self):
        runner = self.make(); self.port.fail_save = True
        with self.assertRaisesRegex(OSError, 'disk full'): runner.run()
        self.assertEqual(self.port.calls.count('reset'), 1)
        self.assertFalse(self.port.moving)

    def test_policy_mutation_and_system_error_prevent_learning(self):
        for fault in ('mutate_policy', 'collection_error'):
            runner = self.make()
            setattr(self.port, fault, True if fault == 'mutate_policy' else 'telemetry_lost')
            with self.assertRaises(RuntimeError): runner.run()
            self.assertNotIn('commit', self.port.calls)
            self.assertNotIn('update', self.port.calls)
            self.assertEqual(self.port.calls.count('reset'), 1)

    def test_cancellation_during_driving_stops_without_next_reset(self):
        runner = self.make()
        original = self.port.collect
        def collect(*args):
            self.port.cancelled = True
            return original(*args)
        self.port.collect = collect
        self.assertEqual(runner.run().reason, 'cancelled')
        self.assertFalse(self.port.moving)
        self.assertNotIn('update', self.port.calls)
        self.assertEqual(self.port.calls.count('reset'), 1)

    def test_batch_limit_includes_reset_and_learning_time(self):
        runner = self.make(limits=Limits(batch_seconds=2))
        def reset(deadline): self.port.calls.append('reset'); self.clock.advance(2)
        self.port.reset = reset
        self.assertEqual(runner.run().reason, 'batch_time_limit')
        self.assertNotIn('prepare', self.port.calls)
        runner = self.make(limits=Limits(batch_seconds=2))
        original = self.port.update_once
        def update(): original(); self.clock.advance(2)
        self.port.update_once = update
        self.assertEqual(runner.run().reason, 'batch_time_limit')
        self.assertEqual(self.port.calls.count('update'), 1)
        self.assertEqual(self.port.calls.count('reset'), 1)

    def test_no_learning_without_stop_confirmation(self):
        runner = self.make(); self.port.stop_confirmed = False
        with self.assertRaisesRegex(RuntimeError, 'stop unconfirmed'): runner.run()
        self.assertNotIn('update', self.port.calls)
        self.assertNotIn('commit', self.port.calls)

    def test_keyboard_interrupt_during_collection_still_stops(self):
        runner = self.make()
        def interrupted(*args):
            self.port.moving = True
            raise KeyboardInterrupt()
        self.port.collect = interrupted
        with self.assertRaises(KeyboardInterrupt): runner.run()
        self.assertFalse(self.port.moving)
        self.assertNotIn('update', self.port.calls)


if __name__ == '__main__': unittest.main()
