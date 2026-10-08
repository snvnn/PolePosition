"""GUI-to-worker integration with temporary synthetic data, never a live game."""
import json
import os
import sys
from pathlib import Path
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from ModelTrainer.common import finish, read
from .__main__ import Controller, ROOT
from .worker import terminal_state


class OutcomeTests(unittest.TestCase):
    def test_user_stop_and_failure_are_distinct(self):
        for reason in ('user_stop', 'time_limit'):
            self.assertEqual(terminal_state(False, reason), 'stopped')
        for reason in ('connection_closed', 'heartbeat_timeout', 'protocol_error', None):
            self.assertEqual(terminal_state(False, reason), 'failed')
        self.assertEqual(terminal_state(True, None), 'completed')


class WorkflowTests(unittest.TestCase):
    def test_window_close_waits_for_actual_child_shutdown(self):
        root = tk.Tk()
        root.withdraw()
        app = Controller(root, monitoring=False)
        destroyed = []
        root.bind('<Destroy>', lambda event: destroyed.append(True) if event.widget is root else None, add='+')
        script = """from TrainingController.protocol import Control,message
import sys,threading,time
c=Control(sys.stdin)
threading.Thread(target=c.listen,daemon=True).start()
print(message(event='state',state='training'),flush=True)
while not c.stopped(): time.sleep(.01)
time.sleep(.4)
print(message(event='finished',state='stopped',reason=c.reason),flush=True)
"""
        try:
            app.coordinator.start([sys.executable, '-u', '-c', script])
            app.lock(True)
            deadline = time.monotonic() + 10
            while app.status.get() != '학습 중' and time.monotonic() < deadline:
                root.update(); time.sleep(.01)
            self.assertEqual(app.status.get(), '학습 중')
            app.close()
            self.assertFalse(destroyed, 'Window closed before the child acknowledged stop')
            self.assertFalse(app.coordinator.closed)
            deadline = time.monotonic() + 10
            while not destroyed and time.monotonic() < deadline:
                root.update(); time.sleep(.01)
            self.assertTrue(destroyed)
            self.assertTrue(app.coordinator.closed)
            self.assertEqual(app.job.terminal['state'], 'stopped')
            self.assertEqual(app.job.terminal['reason'], 'user_stop')
        finally:
            app.coordinator.close()
            deadline = time.monotonic() + 5
            while not app.coordinator.closed and time.monotonic() < deadline:
                app.coordinator.poll(); time.sleep(.02)
            if not destroyed:
                root.update_idletasks(); root.destroy()

    def test_gui_stop_resume_and_offline_evaluation(self):
        from ModelTrainer.test_training import TrainingTests
        fixture = TrainingTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        # Enough mini-batches for Tk to observe an epoch and request a real stop.
        rows = [dict(row, run_id='controller-test', sequence=index+1,
                     observation_ns=index*50_000_000, speed_kmh=10.)
                for index, row in enumerate(fixture.data)]
        for split in ('train', 'validation', 'test'):
            data = rows * 100 if split == 'train' else rows
            (fixture.dataset / (split + '.jsonl')).write_text(
                ''.join(json.dumps(row) + '\n' for row in data), encoding='utf-8')
        finish(fixture.dataset, read(fixture.dataset/'manifest.json'))
        root = tk.Tk()
        root.withdraw()
        app = Controller(root, monitoring=False)
        callback_errors = []
        root.report_callback_exception = lambda *error: callback_errors.append(error)
        # Put all generated outputs in the temporary fixture, including evaluation.
        app.coordinator.root = fixture.root

        def pump_until(predicate, timeout=45):
            deadline = time.monotonic() + timeout
            while not predicate() and time.monotonic() < deadline:
                root.update()
                self.assertFalse(callback_errors, callback_errors)
                time.sleep(.01)
            self.assertTrue(predicate(), (app.status.get(), app.detail.get()))

        try:
            with patch.dict(os.environ, {'PYTHONPATH': str(ROOT)}):
                app.dataset.set(str(fixture.dataset))
                app.config.set(str(fixture.cp))
                app.seconds.set('60')
                app.epochs.set('3')
                app.start(False)
                self.assertTrue(app.locked)
                pump_until(lambda: app.detail.get().startswith('epoch ') or not app.coordinator.busy)
                self.assertTrue(app.coordinator.busy, 'Training finished before a stop could be tested')
                app.stop()
                pump_until(lambda: not app.coordinator.busy)
                self.assertEqual(app.status.get(), '중단', app.detail.get())
                self.assertFalse(app.locked)
                stopped = next((fixture.root/'runs').iterdir()).resolve()
                summary = read(stopped/'summary.json')
                self.assertGreaterEqual(summary['epochs'], 1)
                self.assertLess(summary['epochs'], 4)
                self.assertFalse(read(stopped/'manifest.json')['complete'])
                checkpoint_bytes = (stopped/'last.pt').read_bytes()

                app.listbox.selection_clear(0, 'end')
                app.listbox.selection_set(app.results.index(stopped))
                app.use_checkpoint()
                self.assertFalse(app.coordinator.busy)
                app.start(True)
                pump_until(lambda: not app.coordinator.busy)
                self.assertEqual(app.status.get(), '완료', app.detail.get())
                runs = [path.resolve() for path in (fixture.root/'runs').iterdir()]
                self.assertEqual(len(runs), 2)
                resumed = next(path for path in runs if path != stopped)
                self.assertTrue(read(resumed/'manifest.json')['complete'])
                self.assertEqual(read(resumed/'summary.json')['epochs'], 4)
                self.assertEqual((stopped/'last.pt').read_bytes(), checkpoint_bytes)

                app.task.set('오프라인 평가')
                app.select_task()
                app.model.set(str(resumed))
                app.split.set('test')
                app.start(False)
                pump_until(lambda: not app.coordinator.busy)
                self.assertEqual(app.status.get(), '완료', app.detail.get())
                evaluation = next((fixture.root/'evaluation_runs').iterdir()).resolve()
                self.assertTrue(read(evaluation/'manifest.json')['complete'])
                self.assertTrue((evaluation/'report.html').is_file())
                self.assertIn(evaluation, app.results)
                self.assertFalse(app.locked)
        finally:
            app.coordinator.close()
            deadline = time.monotonic() + 15
            while not app.coordinator.closed and time.monotonic() < deadline:
                app.coordinator.poll()
                time.sleep(.02)
            root.update_idletasks()
            root.destroy()
            self.assertTrue(app.coordinator.closed, 'Worker did not stop during test cleanup')


if __name__ == '__main__':
    unittest.main()
