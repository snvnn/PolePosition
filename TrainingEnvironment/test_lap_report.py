import copy
import math
import unittest

from .lap_report import compare, metrics


class LapReportTests(unittest.TestCase):
    def test_metrics_use_elapsed_time_and_signed_progress(self):
        rows = [dict(observation_ns=t, lateral_error_m=e, heading_error_rad=0,
                     speed_kmh=10, accumulated_m=s)
                for t, e, s in [(0, 0, 2), (10_000_000, 2, 1), (40_000_000, 2, 0)]]
        result = metrics(rows, [], 0, 40_000_000)
        self.assertAlmostEqual(result['lateral_rmse_m'], math.sqrt(3.5))
        self.assertEqual(result['progress_m'], -2)
        self.assertEqual(result['speed_error_rmse_kmh'], 0)
        rows[-1]['observation_ns'] = 200_000_000
        with self.assertRaises(ValueError):
            metrics(rows, [], 0, 200_000_000)

    def report(self):
        return dict(prepared_sha256='a'*64, speed_target_kmh=10,
                    laps=[dict(valid=True, metrics=dict(duration_seconds=1400,
                        lateral_rmse_m=.2, heading_rmse_deg=1, speed_error_rmse_kmh=.3))],
                    conditions=[dict(rain_intensity=0, session_type=3,
                                     fuel_litres=10, clock_seconds=57600)])

    def test_failed_or_unmatched_runs_do_not_establish_improvement(self):
        initial = self.report()
        updated = copy.deepcopy(initial)
        updated['laps'][0]['metrics']['duration_seconds'] = 1390
        matched = compare(initial, updated)
        self.assertTrue(matched['observed_improvement'])
        self.assertFalse(matched['statistical_improvement_established'])
        updated['conditions'][0]['fuel_litres'] = 7
        self.assertFalse(compare(initial, updated)['observed_improvement'])
        updated['laps'] = []
        self.assertFalse(compare(initial, updated)['comparable'])
        updated['speed_target_kmh'] = 20
        with self.assertRaises(ValueError):
            compare(initial, updated)
