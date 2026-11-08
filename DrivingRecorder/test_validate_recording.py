import csv
import json
from pathlib import Path
import tempfile
import unittest
if __package__:
    from .recording_fixtures import RecordingFixture
    from .validate_recording import REQUIRED, validate
else:
    from recording_fixtures import RecordingFixture
    from validate_recording import REQUIRED, validate


class ValidationTests(RecordingFixture, unittest.TestCase):



    def test_full_lap_requires_two_crossings(self):
        self.save()
        report = validate(self.root)
        self.assertTrue(report["first_goal_recording_pass"], report)
        self.assertEqual(report["complete_laps"], 1)
        self.assertEqual(report["valid_complete_laps"], 1)

    def test_missing_summary_is_incomplete(self):
        self.save()
        (self.root / "summary.json").unlink()
        self.assertFalse(validate(self.root)["format_pass"])

    def test_time_reversal_and_nan_rejected(self):
        self.rows[2]["elapsed_ns"] = 1
        self.rows[2]["speed_kmh"] = "nan"
        self.save()
        self.assertFalse(validate(self.root)["format_pass"])

    def test_event_boundary_mismatch(self):
        self.events.pop()
        self.summary["written_events"] = 2
        self.save()
        self.assertFalse(validate(self.root)["format_pass"])

    def test_trailing_drop_not_hidden(self):
        self.summary["received_samples"] = 5
        self.summary["dropped_samples"] = 1
        self.save()
        r = validate(self.root)
        self.assertTrue(r["format_pass"])
        self.assertFalse(r["first_goal_recording_pass"])

    def test_duplicate_packets_rejected(self):
        self.rows[2]["physics_packet_id"] = 2
        self.rows[2]["graphics_packet_id"] = 2
        self.save()
        self.assertFalse(validate(self.root)["format_pass"])

    def test_invalid_hotlap_counter_can_stay_constant(self):
        for i, row in enumerate(self.rows):
            row["completed_laps"] = 0
            row["lap_time_ms"] = 10000 if i % 2 == 0 else 0
            row["lap_valid"] = "false"
        self.save()
        report = validate(self.root)
        self.assertTrue(report["format_pass"], report)
        self.assertEqual(report["complete_laps"], 1)
        self.assertEqual(report["valid_complete_laps"], 0)


if __name__ == "__main__":
    unittest.main()
