"""Synthetic recorder files shared by recorder and dataset validation tests."""
import csv
import json
from pathlib import Path
import tempfile
if __package__:
    from .validate_recording import REQUIRED
else:
    from validate_recording import REQUIRED


class RecordingFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.metadata = {"schema_version": 1, "session_id": "test", "config": {"stale_seconds": 20}}
        self.summary = {"schema_version": 1, "session_id": "test", "normal_shutdown": True,
                        "received_samples": 4, "written_samples": 4, "written_events": 3,
                        "dropped_samples": 0, "dropped_events": 0, "read_timing": {}, "poll_timing": {}}
        self.rows = []
        for i, (lap, progress) in enumerate([(0, .99), (1, .01), (1, .99), (2, .01)], 1):
            row = dict.fromkeys(REQUIRED, 0)
            row.update(sequence=i, segment_id=1, received_unix_ns=i * 10_000_000_000,
                       elapsed_ns=i * 10_000_000_000, interval_ns=0 if i == 1 else 10_000_000_000,
                       physics_changed_ns=i * 10_000_000_000, graphics_changed_ns=i * 10_000_000_000,
                       physics_packet_id=i, graphics_packet_id=i, progress=progress,
                       completed_laps=lap, lap_valid="true", in_pit="false")
            self.rows.append(row)
        self.events = [(10_000_000_000, 1, "segment_start", "recording_start"),
                       (20_000_000_000, 1, "lap_boundary", "completed_laps=1"),
                       (40_000_000_000, 1, "lap_boundary", "completed_laps=2")]

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        for filename, data in [("metadata.json", self.metadata), ("summary.json", self.summary)]:
            (self.root / filename).write_text(json.dumps(data), encoding="utf-8")
        with (self.root / "telemetry.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=REQUIRED)
            writer.writeheader()
            writer.writerows(self.rows)
        with (self.root / "events.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["elapsed_ns", "segment_id", "kind", "detail"])
            writer.writerows(self.events)
