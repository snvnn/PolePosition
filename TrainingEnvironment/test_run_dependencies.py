import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from .gamepad_runtime import Native
from .gamepad_session_config import drive_config
from .run_dependencies import RunDependencies


class DependencyTests(unittest.TestCase):
    def test_late_reset_waits_without_input_until_phase_deadline(self):
        from ExecutionRuntime.timing import TimeBudget
        now=[9.]
        reason=[]
        native=Native.__new__(Native)
        native.clock=SimpleNamespace(monotonic=lambda:now[0],sleep=lambda s:now.__setitem__(0,now[0]+s))
        native.budget=TimeBudget(10,clock=native.clock,started=0)
        native.control=SimpleNamespace(stopped=lambda:bool(reason),reason=None)
        def cancel(value):
            if not reason:reason.append(value);native.control.reason=value
        native.control.cancel=cancel
        native.exit_reason=None;native.emit=Mock()
        with self.assertRaisesRegex(InterruptedError,'session_time_limit'):
            native.reserve_reset_window(2)
        self.assertGreaterEqual(now[0],10)
        self.assertLess(now[0],10.03)
        native.emit.assert_called_once_with('progress',phase='waiting_for_phase_deadline')

    def test_reset_window_with_time_left_and_f8_are_not_delayed(self):
        from ExecutionRuntime.timing import TimeBudget
        native=Native.__new__(Native)
        native.clock=SimpleNamespace(monotonic=lambda:1.,sleep=Mock())
        native.budget=TimeBudget(10,clock=native.clock,started=0)
        native.control=SimpleNamespace(stopped=lambda:False,reason=None)
        native.exit_reason=None;native.emit=Mock()
        native.reserve_reset_window(2)
        native.clock.sleep.assert_not_called();native.emit.assert_not_called()
        native.control=SimpleNamespace(stopped=lambda:True,reason='f8')
        with self.assertRaisesRegex(InterruptedError,'f8'):native.reserve_reset_window(20)
        native.clock.sleep.assert_not_called()

    def test_native_ownership_uses_injected_process_and_game_ports(self):
        game = Mock()
        emitted = []
        reason = []
        control = SimpleNamespace(stopped=lambda: bool(reason),
                                  cancel=lambda value: reason.append(value))
        release = Mock(return_value=False)
        class Process:
            process = None
            def start(self, argv, root):
                self.argv = argv
                self.process = object()
            def poll(self):
                self.process = None
                return [dict(event='finished', reason='fixture_exit')]
            def stop(self):
                raise AssertionError('No stop expected before the fake exit')
        process = Process()
        clock = SimpleNamespace(monotonic=lambda: 0., sleep=lambda seconds: None)
        with tempfile.TemporaryDirectory() as directory:
            native = Native(Path(directory), control, lambda *a, **k: emitted.append(k), 60,
                            clock=clock, process_factory=lambda: process,
                            game_factory=lambda: game, release_after_exit=release)
            native.thread.join(2)
            self.assertFalse(native.thread.is_alive())
            self.assertEqual(reason, ['fixture_exit'])
            self.assertIn('--supervise', process.argv)
            release.assert_called_once()
            # Closing follows a process already known to have exited. It never
            # creates a device or tries to turn an exit into standstill evidence.
            control.stopped = lambda: False
            native.close()
            game.close.assert_called_once()

    def test_native_start_failure_closes_injected_game_handle(self):
        game = Mock()
        process = Mock()
        process.start.side_effect = OSError('fixture launch failure')
        with self.assertRaisesRegex(OSError, 'fixture launch failure'):
            Native(Path('unused'), Mock(), Mock(), 60,
                   game_factory=lambda: game, process_factory=lambda: process)
        game.close.assert_called_once()

    def test_reset_port_receives_existing_validation_callback(self):
        reset = Mock()
        check, progress = Mock(), Mock()
        dependencies = RunDependencies(reset_factory=reset)
        self.assertIs(dependencies.reset('fixture', check, progress), reset.return_value)
        reset.assert_called_once_with('fixture', check, progress)

    def test_comparison_config_retains_exact_legacy_serialization(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'drive.toml'
            template = dict(seconds=0, model='old', evaluation_laps=0, intervention=dict(teacher_slot=0))
            drive_config(path, template, 'new', False, True, evaluation_laps=1)
            expected = ('seconds = 0\nmodel = "new"\nevaluation_laps = 1\n'
                        'continuous_learning = true\nautomatic_initial_start = true\n'
                        '[intervention]\nteacher_slot = 0\n')
            self.assertEqual(path.read_text(encoding='utf-8'), expected)
