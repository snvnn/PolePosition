import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from .coordinator import Coordinator
from .jobs import JobRequest, request
from .task_catalog import BY_LABEL, SPECS, WORKER_TASKS
from .task_fixtures import command_inputs


class TaskCatalogTests(unittest.TestCase):
    def test_imports_do_not_load_training_or_device_backends(self):
        code = """import sys
import TrainingController.task_catalog
import TrainingController.worker
import ExecutionRuntime.process
assert not any(n.startswith(('torch', 'TrainingEnvironment', 'GamepadIO')) for n in sys.modules)
"""
        subprocess.run([sys.executable, '-c', code], check=True, capture_output=True)

    def test_catalog_has_unique_ids_and_worker_handlers_agree(self):
        self.assertEqual(len(SPECS), len({s.id for s in SPECS}))
        self.assertEqual(len(SPECS), len(BY_LABEL))
        for spec in SPECS:
            if spec.worker_task:
                worker = WORKER_TASKS[spec.worker_task]
                self.assertEqual((spec.handler, spec.max_seconds), (worker.handler, worker.max_seconds))

    def test_explicit_job_metadata_controls_background_release(self):
        coordinator = Coordinator(Path.cwd(), monitoring=False, factory=lambda: Mock(process=None))
        spec = BY_LABEL['최고 모델 학습 반복']
        coordinator.start(JobRequest(('fixture.exe',), spec))
        self.assertTrue(coordinator.continuous_job)
        self.assertEqual(coordinator.pending, ['fixture.exe'])

    def test_legacy_argv_does_not_treat_config_value_as_task_id(self):
        coordinator = Coordinator(Path.cwd(), monitoring=False, factory=lambda: Mock(process=None))
        coordinator.start(['fixture.exe', '--config', 'gamepad-online'])
        self.assertFalse(coordinator.continuous_job)
        coordinator = Coordinator(Path.cwd(), monitoring=False, factory=lambda: Mock(process=None))
        coordinator.start(['python', '-m', 'TrainingController.worker', '--task', 'gamepad-online'])
        self.assertTrue(coordinator.continuous_job)

    def test_request_preserves_argv_and_supplies_catalog_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = '정책 비교 평가'
            job = request(root, task, command_inputs(root, task))
            self.assertIs(job.spec, BY_LABEL[task])
            self.assertEqual(job.argv[job.argv.index('--task') + 1], 'gamepad-policy-compare')
