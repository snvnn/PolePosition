import io
import sys
import time
import unittest
import tempfile
from pathlib import Path

from .process import ManagedProcess
from .protocol import Control, message, validate_event

ROOT = Path(__file__).resolve().parents[1]


class ProtocolTests(unittest.TestCase):
    def test_eof_stops(self):
        control = Control(io.StringIO(''))
        control.listen()
        self.assertEqual(control.reason, 'connection_closed')

    def test_stop_preserves_first_reason(self):
        control = Control(io.StringIO(message(command='stop')))
        control.listen()
        self.assertEqual(control.reason, 'user_stop')

    def test_malformed_command_stops(self):
        for line in ('broken', '[]', '{"version":2}', message(command='drive'),
                     message(command='heartbeat', steering=1), '{"version":true,"command":"heartbeat"}',
                     ' ' * 4097):
            control = Control(io.StringIO(line))
            control.listen()
            self.assertEqual(control.reason, 'protocol_error')

    def test_invalid_display_fields_rejected_before_tk(self):
        for fields in (dict(epoch=1), dict(epoch=True, updates=1, loss=.1), dict(age_ms='old'),
                       dict(identity=['car']), dict(identity={'car':None}), dict(loss=float('nan')),
                       dict(phase=[]), dict(controller_reason=[]), dict(result_dir=17), dict(result_dir='bad\x00path'),
                       dict(fresh='false'), dict(preparation={'check':[], 'state':'verified'})):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate_event(dict(version=1, event='progress', **fields))
        validate_event(dict(version=1, event='progress', phase='recording', identity=None,
                            speed_kmh=0., samples=0, result_dir='recordings/example'))

    def test_frozen_gui_expires(self):
        control = Control(io.StringIO(''))
        control.last_heartbeat -= 4
        self.assertTrue(control.stopped())
        self.assertEqual(control.reason, 'heartbeat_timeout')


class ProcessTests(unittest.TestCase):
    def test_stop_during_reader_start_is_not_erased(self):
        import threading
        from unittest.mock import patch
        job = ManagedProcess()
        self.addCleanup(self.cleanup, job)
        original = threading.Thread.start
        requested = False
        def start(thread):
            nonlocal requested
            if not requested:
                requested = True
                job.stop()
            original(thread)
        script = """from TrainingController.protocol import Control,message
import sys,threading,time
c=Control(sys.stdin)
threading.Thread(target=c.listen,daemon=True).start()
while not c.stopped(): time.sleep(.01)
print(message(event='finished',state='stopped',reason=c.reason),flush=True)
"""
        with patch.object(threading.Thread, 'start', start):
            job.start([sys.executable, '-u', '-c', script], ROOT)
        final = self.collect(job)
        self.assertEqual(final['state'], 'stopped')
        self.assertEqual(final['reason'], 'user_stop')

    def test_closed_child_pipe_cannot_strand_completed_job(self):
        from unittest.mock import Mock
        job = ManagedProcess()
        process = Mock()
        process.poll.return_value = 0
        process.returncode = 0
        process.stdin.close.side_effect = OSError(22, 'Invalid argument')
        job.process = process
        job.reader = Mock(); job.reader.is_alive.return_value = False
        job.error_reader = Mock(); job.error_reader.is_alive.return_value = False
        job.command_writer = Mock()
        job.terminal = dict(event='finished', state='stopped', reason='user_stop')
        self.assertEqual(job.poll()[-1], job.terminal)
        self.assertIsNone(job.process)
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()

    def run_worker(self, script):
        job = ManagedProcess()
        job.start([sys.executable, '-u', '-c', script], ROOT)
        self.addCleanup(self.cleanup, job)
        return job

    @staticmethod
    def cleanup(job):
        if job.process:
            job.process.stdin.close()
            try:
                job.process.wait(timeout=5)
            except Exception:
                job.process.kill()
                job.process.wait()

    def collect(self, job):
        events = []
        deadline = time.monotonic() + 10
        while job.process and time.monotonic() < deadline:
            events.extend(job.poll())
            time.sleep(.02)
        self.assertIsNone(job.process, 'Worker failed to finish')
        return events[-1]

    def test_child_exit_without_result_is_failure(self):
        job = self.run_worker('pass')
        self.assertEqual(self.collect(job)['state'], 'failed')

    def test_duplicate_job_and_cooperative_stop(self):
        job = self.run_worker("from TrainingController.protocol import Control,message; import sys; c=Control(sys.stdin); c.listen(); print(message(event='finished',state='stopped',reason=c.reason),flush=True)")
        with self.assertRaises(RuntimeError):
            job.start([sys.executable, '-c', 'pass'], ROOT)
        job.stop()
        time.sleep(.05)
        job.process.stdin.close()
        self.assertEqual(self.collect(job)['state'], 'stopped')

    def test_nonzero_exit_overrides_success(self):
        job = self.run_worker("from TrainingController.protocol import message; print(message(event='finished',state='completed')); raise SystemExit(3)")
        self.assertEqual(self.collect(job)['state'], 'failed')

    def test_progress_flood_preserves_terminal(self):
        job = self.run_worker("from TrainingController.protocol import message\nfor i in range(2000): print(message(event='progress',epoch=i,updates=i,loss=.1))\nprint(message(event='finished',state='completed'))")
        self.assertEqual(self.collect(job)['state'], 'completed')

    def test_invalid_progress_is_a_failure_not_a_gui_callback_error(self):
        job = self.run_worker("from TrainingController.protocol import message; print(message(event='progress',epoch=1))")
        self.assertEqual(self.collect(job)['state'], 'failed')

    def test_malformed_worker_stops_with_failure(self):
        job = self.run_worker("print('invalid protocol')")
        self.assertEqual(self.collect(job)['state'], 'failed')

    def test_invalid_final_state_is_failure(self):
        job = self.run_worker("from TrainingController.protocol import message; print(message(event='finished',state='invented'))")
        self.assertEqual(self.collect(job)['state'], 'failed')

    def test_actual_training_worker(self):
        from ModelTrainer.test_training import TrainingTests
        fixture = TrainingTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        job = ManagedProcess()
        self.addCleanup(self.cleanup, job)
        job.start([sys.executable, '-u', '-m', 'TrainingController.worker',
                   '--dataset', str(fixture.dataset), '--config', str(fixture.cp)], ROOT)
        event = self.collect(job)
        self.assertEqual(event['state'], 'completed', event)
        self.assertTrue((Path(event['result_dir']) / 'last.pt').is_file())

    def test_actual_worker_epoch_cap_preserves_config(self):
        from ModelTrainer.test_training import TrainingTests
        from ModelTrainer.common import read
        fixture = TrainingTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        original_config = fixture.cp.read_bytes()
        job = ManagedProcess()
        self.addCleanup(self.cleanup, job)
        job.start([sys.executable, '-u', '-m', 'TrainingController.worker',
                   '--dataset', str(fixture.dataset), '--config', str(fixture.cp), '--epochs', '2'], ROOT)
        event = self.collect(job)
        self.assertEqual(event['state'], 'completed', event)
        folder = Path(event['result_dir'])
        self.assertEqual(read(folder/'summary.json')['epochs'], 2)
        self.assertEqual(read(folder/'manifest.json')['config']['max_epochs'], 4)
        self.assertEqual(fixture.cp.read_bytes(), original_config)

    def test_actual_worker_disconnect_preserves_incomplete_result(self):
        from ModelTrainer.test_training import TrainingTests
        fixture = TrainingTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        job = ManagedProcess()
        self.addCleanup(self.cleanup, job)
        job.start([sys.executable, '-u', '-m', 'TrainingController.worker',
                   '--dataset', str(fixture.dataset), '--config', str(fixture.cp)], ROOT)
        job.process.stdin.close()
        event = self.collect(job)
        self.assertEqual(event['state'], 'failed', event)
        self.assertEqual(event['reason'], 'connection_closed')


class GuiTests(unittest.TestCase):
    def test_preparation_history_survives_progress_and_failure(self):
        import tkinter as tk
        from .__main__ import Controller
        root = tk.Tk()
        root.withdraw()
        try:
            app = Controller(root, monitoring=False)
            app.show_state(dict(phase='pid_start', controller_reason='waiting_forward_gear', foreground=True))
            self.assertIn('전진 기어 대기', app.status.get())
            self.assertIn('ACC 전면: True', app.detail.get())
            app.show_state(dict(phase='stopping', controller_reason='waiting_forward_gear', reason='f8'))
            self.assertEqual(app.status.get(), '정지 처리')
            for check, state in [('path', 'verified'), ('calibration', 'checking')]:
                app.show_state(dict(phase='preparing', preparation=dict(check=check, state=state)))
            app.show_state(dict(phase='waiting_f7', tracking_ready=False))
            self.assertIn('경로 파일 통과', app.preparation.get())
            self.assertIn('보정 파일·설정 확인 중', app.preparation.get())
            app.finish_preparation('failed')
            self.assertIn('결과 미확인', app.preparation.get())
            self.assertIn('경로 파일 통과', app.preparation.get())
            app.select_task()
            self.assertEqual(app.preparation.get(), '')
            app.show_state(dict(phase='preparing', preparation=dict(check='calibration', state='failed')))
            app.finish_preparation('failed')
            self.assertIn('보정 파일·설정 실패', app.preparation.get())
        finally:
            root.update_idletasks()
            root.destroy()

    def test_window_initialization_and_setting_lock(self):
        import tkinter as tk
        from .__main__ import Controller
        root = tk.Tk()
        root.withdraw()
        try:
            app = Controller(root, monitoring=False)
            root.update()
            app.lock(True)
            self.assertTrue(all(str(widget['state']) == 'disabled' for widget in app.inputs))
            app.lock(False)
            for name, widgets in app.field_widgets.items():
                enabled = name in {'task', 'dataset', 'checkpoint', 'config', 'seconds', 'epochs', 'start', 'resume'}
                self.assertTrue(all((str(widget['state']) != 'disabled') == enabled for widget in widgets))
        finally:
            root.update_idletasks()
            root.destroy()

    def test_task_controls_and_custom_config_survive_switching(self):
        import tkinter as tk
        from .__main__ import Controller
        root = tk.Tk()
        root.withdraw()
        try:
            app = Controller(root, monitoring=False)
            app.config.set('custom-training.toml')
            app.task.set('평가·주행'); app.select_task()
            self.assertEqual(str(app.field_widgets['resume'][0]['state']), 'disabled')
            self.assertEqual(str(app.field_widgets['previous_run'][0]['state']), 'disabled')
            app.mode.set('drive')
            self.assertEqual(str(app.field_widgets['previous_run'][0]['state']), 'normal')
            app.lock(True)
            app.mode.set('shadow')
            self.assertTrue(all(str(widget['state']) == 'disabled' for widget in app.inputs))
            app.lock(False)
            app.task.set('강화학습'); app.select_task()
            self.assertEqual(str(app.field_widgets['start'][0]['state']), 'normal')
            self.assertEqual(app.mode.get(), 'verify5')
            self.assertIn('F7', app.detail.get())
            app.task.set('모방학습'); app.select_task()
            self.assertEqual(app.config.get(), 'custom-training.toml')
        finally:
            root.update_idletasks()
            root.destroy()

    def test_checkpoint_selection_only_selects_and_result_actions_lock(self):
        import tkinter as tk
        from unittest.mock import patch
        from .__main__ import Controller
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'training_runs'/'run'
            output.mkdir(parents=True)
            (output/'last.pt').write_text('selection only, never deserialize in GUI')
            root = tk.Tk()
            root.withdraw()
            try:
                with patch('TrainingController.__main__.ROOT', Path(directory)):
                    app = Controller(root, monitoring=False)
                    app.listbox.selection_set(0)
                    app.update_result_actions()
                    self.assertEqual(str(app.report_button['state']), 'disabled')
                    self.assertEqual(str(app.checkpoint_button['state']), 'normal')
                    app.use_checkpoint()
                    self.assertEqual(Path(app.checkpoint.get()), (output/'last.pt').resolve())
                    self.assertFalse(app.coordinator.busy)
                    app.lock(True)
                    self.assertEqual(str(app.checkpoint_button['state']), 'disabled')
                    with patch('TrainingController.__main__.os.startfile') as opener:
                        app.open_result(False)
                        opener.assert_not_called()
            finally:
                root.update_idletasks()
                root.destroy()


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ['.venv-training/Scripts/python.exe', 'target/release/poleposition-recorder.exe',
                     'target/release/poleposition-actuator.exe', 'settings.toml', 'profile.json']:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        self.values = dict(seconds='120', config='settings.toml', dataset='.', model='.',
                           prepared='.', profile='profile.json', controller_config='settings.toml',
                           previous_run='.', mode='shadow', model_seconds='5', split='test')

    def test_recording_uses_managed_preparation(self):
        from .jobs import command
        args = command(self.root, '기록 수집', self.values)
        self.assertIn('--managed', args)
        self.assertNotIn('--start', args)

    def test_epoch_cap_only_applies_to_new_training(self):
        from .jobs import command
        args = command(self.root, '모방학습', dict(self.values, epochs='2'))
        self.assertEqual(args[args.index('--epochs')+1], '2')
        for invalid in ('0', '201', '-1', '1.5', 'nan'):
            with self.assertRaises(ValueError):
                command(self.root, '모방학습', dict(self.values, epochs=invalid))
        args = command(self.root, '모방학습', dict(self.values, epochs='2', checkpoint='profile.json'), resume=True)
        self.assertNotIn('--epochs', args)
        self.assertNotIn('--epochs', command(self.root, '기록 수집', dict(self.values, epochs='2')))

    def test_live_preserves_existing_mode_and_stage_validation(self):
        from .jobs import command
        args = command(self.root, '평가·주행', self.values)
        self.assertIn('shadow', args)
        self.assertNotIn('--previous-run', args)
        args = command(self.root, '평가·주행', dict(self.values, mode='drive'))
        self.assertIn('--previous-run', args)
        with self.assertRaises(ValueError):
            command(self.root, '평가·주행', dict(self.values, model_seconds='240'))

    def test_unimplemented_and_invalid_limits_are_rejected(self):
        from .jobs import command
        for seconds in ('nan', 'inf', '0', '7201'):
            with self.assertRaises(ValueError):
                command(self.root, '모방학습', dict(self.values, seconds=seconds))
        with self.assertRaises(ValueError):
            command(self.root, '강화학습', self.values)
        with self.assertRaises(ValueError):
            command(self.root, '기록 수집', self.values, resume=True)


if __name__ == '__main__':
    unittest.main()
