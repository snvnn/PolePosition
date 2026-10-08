"""Explicit Windows integration tests. Never send F7 or vehicle inputs."""
import os
from pathlib import Path
import time
import unittest

from .process import ManagedProcess

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('POLEPOSITION_MANAGEMENT_TESTS') == '1',
                     'Explicit run required: temporarily registers global F7/F8')
class RustManagementTests(unittest.TestCase):
    def start(self):
        job = ManagedProcess()
        job.start([str(ROOT / 'target/release/poleposition-recorder.exe'), '--managed',
                   '--config', 'DrivingRecorder/recorder.toml', '--duration', '60'], ROOT)
        self.addCleanup(self.cleanup, job)
        return job

    def cleanup(self, job):
        if job.process:
            job.stop()
            self.until(job, lambda e: e['event'] == 'finished')

    def until(self, job, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            for event in job.poll():
                if predicate(event):
                    return event
                if event['event'] == 'finished':
                    self.fail(str(event))
            time.sleep(.05)
        self.fail('Managed recorder did not respond')

    def test_prepare_does_not_record_and_stop_is_normal(self):
        job = self.start()
        state = self.until(job, lambda e: e.get('state') == 'waiting_f7')
        self.assertNotIn('result_dir', state)
        job.stop()
        result = self.until(job, lambda e: e['event'] == 'finished')
        self.assertEqual(result['state'], 'stopped')
        self.assertEqual(result['reason'], 'user_stop')
        self.assertIsNone(result['result_dir'])

    def test_hotkey_collision_is_reported_to_gui(self):
        first = self.start()
        self.until(first, lambda e: e.get('state') == 'waiting_f7')
        second = self.start()
        result = self.until(second, lambda e: e['event'] == 'finished')
        self.assertEqual(result['state'], 'failed')
        self.assertIn('hotkey registration failed', result['reason'])

    def test_frozen_gui_stops_waiting_process(self):
        job = self.start()
        self.until(job, lambda e: e.get('state') == 'waiting_f7')
        # No poll: the background writer must not manufacture heartbeats.
        deadline = time.monotonic() + 6
        while job.process.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertIsNotNone(job.process.poll())
        result = self.until(job, lambda e: e['event'] == 'finished')
        self.assertEqual(result['reason'], 'heartbeat_timeout')
        self.assertIsNone(result['result_dir'])


if __name__ == '__main__':
    unittest.main()
