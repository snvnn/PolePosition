"""Actual native validation failures before device access or hotkey registration."""
from pathlib import Path
import tempfile
import time
import unittest

from .process import ManagedProcess

ROOT = Path(__file__).resolve().parents[1]


class PreparationTests(unittest.TestCase):
    def test_invalid_config_reports_stage_and_never_reaches_device(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'invalid.toml'
            config.write_text('not valid toml !', encoding='utf-8')
            job = ManagedProcess()
            job.start([str(ROOT / 'target/release/poleposition-actuator.exe'),
                       'test', '--managed', '--config', str(config)], ROOT)
            events = []
            try:
                deadline = time.monotonic() + 10
                while job.process is not None and time.monotonic() < deadline:
                    events.extend(job.poll())
                    time.sleep(.02)
                self.assertIsNone(job.process)
                checks = [event['preparation'] for event in events if 'preparation' in event]
                self.assertEqual(checks, [dict(check='config', state='checking'),
                                          dict(check='config', state='failed')])
                self.assertEqual(events[-1]['state'], 'failed')
                self.assertTrue(events[-1]['reason'])
                self.assertNotIn('waiting_f7', [event.get('phase') for event in events])
            finally:
                if job.process:
                    job.stop()
                    job.process.wait(timeout=10)
                    job.poll()


if __name__ == '__main__':
    unittest.main()
