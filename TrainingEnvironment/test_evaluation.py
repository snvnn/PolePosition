import unittest
from dataclasses import replace

from DrivingRecorder.laps import FinishLineTracker
from .evaluation import ControlSpan, LapEvaluation, next_stage_candidate

MODEL = 'a' * 64
SOURCE = 'b' * 64


def row(index, *, segment=1):
    # Synthetic 20-second laps, with a sample on either side of each crossing.
    return dict(elapsed_ns=(index + 1) * 10_000_000_000, segment_id=segment,
                completed_laps=(index + 1) // 2, progress=.99 if index % 2 == 0 else .01,
                lap_time_ms=19000 if index % 2 == 0 else 100, lap_valid=True)


def span(index):
    return ControlSpan(index * 10_000_000_000, (index + 1) * 10_000_000_000,
                       MODEL, 'model', 'model', True, 0, 0)


class EvaluationTests(unittest.TestCase):
    def evaluate(self, changes=None, count=8):
        monitor = LapEvaluation(SOURCE, MODEL, 10)
        for index in range(count):
            sample, control, continuous = row(index), span(index), True
            if changes:
                sample, control, continuous = changes(index, sample, control, continuous)
            monitor.observe(sample, control, continuous=continuous)
        return monitor

    def test_three_finish_to_finish_laps_unlock_only_next_stage(self):
        monitor = self.evaluate()
        self.assertEqual(len(monitor.laps), 3)
        self.assertTrue(all(lap.valid for lap in monitor.laps))
        self.assertEqual([lap.duration_seconds for lap in monitor.laps], [20.] * 3)
        self.assertEqual(next_stage_candidate(monitor.laps, MODEL, 10), 20)
        self.assertIsNone(next_stage_candidate(monitor.laps[:2], MODEL, 10))
        self.assertIsNone(next_stage_candidate(monitor.laps, 'c' * 64, 10))
        self.assertIsNone(next_stage_candidate(monitor.laps, MODEL, 20))
        final = [replace(lap, speed_cap=240) for lap in monitor.laps]
        self.assertIsNone(next_stage_candidate(final, MODEL, 240))

    def test_takeover_at_first_crossing_is_too_late(self):
        def change(index, sample, control, continuous):
            if index == 1:
                control = replace(control, start_ns=sample['elapsed_ns'])
            return sample, control, continuous
        monitor = self.evaluate(change)
        self.assertIn('control_interval_uncovered', monitor.laps[0].reasons)
        self.assertTrue(monitor.laps[1].valid)
        self.assertIsNone(next_stage_candidate(monitor.laps, MODEL, 10))

    def test_pid_steering_and_mid_lap_control_faults_do_not_qualify(self):
        for alteration, reason in [
            ({'steering_source': 'pid'}, 'model_did_not_control_all_axes'),
            ({'pedals_source': 'pid'}, 'model_did_not_control_all_axes'),
            ({'policy_sha256': 'c' * 64}, 'policy_changed'),
            ({'deterministic': False}, 'exploration_enabled'),
            ({'learning_updates': 1}, 'learning_during_evaluation'),
            ({'replay_insertions': 1}, 'evaluation_entered_replay'),
        ]:
            with self.subTest(reason=reason):
                def change(index, sample, control, continuous):
                    return sample, replace(control, **alteration) if index == 2 else control, continuous
                monitor = self.evaluate(change)
                self.assertIn(reason, monitor.laps[0].reasons)
                self.assertIsNone(next_stage_candidate(monitor.laps, MODEL, 10))

    def test_missing_transmissions_and_continuity_failure(self):
        for missing in (True, False):
            def change(index, sample, control, continuous):
                if index == 2:
                    return sample, None if missing else control, missing
                return sample, control, continuous
            lap = self.evaluate(change).laps[0]
            self.assertIn('control_evidence_missing' if missing else 'continuity_not_verified', lap.reasons)

    def test_boundary_validity_belongs_to_following_lap(self):
        def change(index, sample, control, continuous):
            if index == 3:
                sample['lap_valid'] = False
            return sample, control, continuous
        laps = self.evaluate(change).laps
        self.assertTrue(laps[0].valid)
        self.assertIn('game_lap_invalid', laps[1].reasons)
        self.assertTrue(laps[2].valid)

    def test_reset_and_partial_lap_never_complete_a_lap(self):
        self.assertFalse(self.evaluate(count=3).laps)
        def change(index, sample, control, continuous):
            sample['segment_id'] = 1 if index < 2 else 2
            return sample, control, continuous
        monitor = self.evaluate(change, count=4)
        self.assertFalse(monitor.laps)

    def test_duplicate_and_overlapping_evidence_cannot_inflate_count(self):
        laps = self.evaluate().laps
        self.assertIsNone(next_stage_candidate([laps[0]] * 3, MODEL, 10))
        self.assertEqual(next_stage_candidate(laps * 2, MODEL, 10), 20)
        overlap = replace(laps[1], start_ns=laps[0].start_ns + 1)
        with self.assertRaisesRegex(ValueError, 'Overlapping'):
            next_stage_candidate([laps[0], overlap, laps[2]], MODEL, 10)

    def test_reversed_time_rejected_before_tracker_mutation(self):
        monitor = self.evaluate(count=2)
        with self.assertRaisesRegex(ValueError, 'reversed'):
            monitor.observe(row(0), span(0), continuous=True)
        self.assertEqual(monitor.previous, row(1))

    def test_hotlap_without_counter_increment_and_reset_discontinuity(self):
        tracker = FinishLineTracker()
        boundaries = []
        for index in range(4):
            sample = row(index)
            sample['completed_laps'] = 0
            boundary, bad = tracker.observe(sample)
            self.assertFalse(bad)
            if boundary:
                boundaries.append(boundary)
        self.assertEqual(len(boundaries), 2)
        sample = row(4)
        sample['completed_laps'] = 10
        self.assertTrue(tracker.observe(sample)[1])


if __name__ == '__main__':
    unittest.main()
