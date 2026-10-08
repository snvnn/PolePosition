import time
import tkinter as tk
import unittest

from ModelTrainer.common import read
from .__main__ import Controller
from .jobs import command


class V2WorkflowTests(unittest.TestCase):
    def test_gui_pretrain_stop_and_resume_with_korean_progress(self):
        from TrainingEnvironment.test_pretrain import PretrainTests
        fixture = PretrainTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = tk.Tk()
        root.withdraw()
        app = Controller(root, monitoring=False)
        errors = []
        root.report_callback_exception = lambda *args: errors.append(args)

        def wait():
            deadline = time.monotonic()+45
            while app.locked and time.monotonic() < deadline:
                root.update()
                time.sleep(.01)
            self.assertFalse(app.locked, app.detail.get())
            self.assertFalse(errors)

        try:
            app.task.set('강화학습 사전학습')
            app.select_task()
            app.dataset.set(str(fixture.dataset))
            app.config.set(str(fixture.cp))
            app.start(False)
            self.assertTrue(app.locked)
            deadline = time.monotonic()+10
            while app.job.process is None and time.monotonic() < deadline:
                root.update()
                time.sleep(.01)
            self.assertIsNotNone(app.job.process)
            app.stop()
            wait()
            self.assertEqual(app.job.terminal['state'], 'stopped', app.job.terminal)
            from pathlib import Path
            previous = Path(app.job.terminal['result_dir'])
            self.assertFalse(read(previous/'manifest.json')['complete'])
            app.checkpoint.set(str(previous/'last.pt'))
            app.start(True)
            wait()
            self.assertEqual(app.job.terminal['state'], 'completed', app.job.terminal)
            self.assertNotEqual(app.job.terminal['result_dir'], str(previous))
        finally:
            app.coordinator.close()
            deadline = time.monotonic()+10
            while not app.coordinator.closed and time.monotonic() < deadline:
                app.coordinator.poll()
                time.sleep(.02)
            root.destroy()


if __name__ == '__main__': unittest.main()
