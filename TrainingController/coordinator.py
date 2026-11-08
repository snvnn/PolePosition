"""Transition from read-only monitoring to exactly one requested job."""
from pathlib import Path
import hashlib
import json

from ExecutionRuntime.process import ManagedProcess
from .jobs import JobRequest
from .task_catalog import WORKER_TASKS


class Coordinator:
    def __init__(self, root, monitoring=True, factory=ManagedProcess):
        self.root = Path(root)
        self.monitor = factory()
        self.job = factory()
        self.monitoring = monitoring
        self.monitor_requested = monitoring
        self.pending = None
        self.closing = False
        self.factory = factory
        self.background = []
        self.stop_requested = False
        self.gamepad_result = None
        self.continuous_job = False

    @property
    def busy(self):
        return self.pending is not None or self.job.process is not None

    @property
    def closed(self):
        return self.closing and self.monitor.process is None and self.job.process is None and not self.background

    def start(self, command):
        if self.busy or self.closing:
            raise RuntimeError('A job is already active or closing')
        if isinstance(command, JobRequest):
            self.pending = list(command.argv)
            self.continuous_job = command.spec.continuous
        else:
            # Compatibility for existing callers passing argv. Parse only the worker's
            # --task option; a config path or unrelated argument is not task metadata.
            self.pending = list(command)
            spec = None
            if 'TrainingController.worker' in self.pending:
                try:
                    index = self.pending.index('--task')
                    spec = WORKER_TASKS.get(self.pending[index + 1])
                except (ValueError, IndexError):
                    pass
            self.continuous_job = bool(spec and spec.continuous)
        self.stop_requested = False
        self.gamepad_result = None
        self.monitor_requested = False
        if self.monitor.process is not None:
            self.monitor.stop()

    def stop(self):
        self.stop_requested = True
        if self.pending is not None:
            self.pending = None
            self.monitor_requested = self.monitoring
        self.job.stop()

    def close(self):
        self.closing = True
        self.pending = None
        self.monitor_requested = False
        self.monitor.stop()
        self.job.stop()

    def refresh(self):
        if not self.busy and not self.closing:
            self.monitor_requested = self.monitoring
            self.monitor.stop()

    def poll(self):
        result = []
        for worker in self.background[:]:
            for event in worker.poll():
                if event['event'] == 'finished':
                    result.append(('background', event))
            if worker.process is None:
                self.background.remove(worker)
        for event in self.monitor.poll():
            result.append(('monitor', event))
        for event in self.job.poll():
            if event.get('model_type') == 'gamepad_v1' and event.get('result_dir'):
                self.gamepad_result = Path(event['result_dir'])
            result.append(('job', event))
            if event['event'] == 'finished':
                self.monitor_requested = self.monitoring and not self.closing
        if (self.stop_requested and not self.continuous_job and self.job.process is not None and self._gamepad_released()):
            self.background.append(self.job)
            self.job = self.factory()
            result.append(('job', dict(event='finished', state='stopped',
                reason='장치 해제 완료 · 나머지 결과 처리는 백그라운드에서 진행 중',
                result_dir=str(self.gamepad_result))))
            self.stop_requested = False
            self.monitor_requested = self.monitoring and not self.closing
        if self.pending is not None and self.monitor.process is None:
            command, self.pending = self.pending, None
            try:
                self.job.start(command, self.root)
            except (OSError, RuntimeError) as error:
                result.append(('job', dict(event='finished',state='failed',reason=str(error))))
                self.monitor_requested = self.monitoring
        if (self.monitor_requested and not self.busy and self.monitor.process is None and not self.closing):
            self.monitor_requested = False
            binary = self.root/'target/release/poleposition-telemetry.exe'
            try:
                self.monitor.start([str(binary), '--managed'], self.root)
            except OSError as error:
                result.append(('monitor',dict(event='finished',state='failed',reason=str(error))))
        return result

    def _gamepad_released(self):
        # Native finalization publishes this manifest only AFTER release,
        # reader/logger shutdown and profile verification. Never unlock from
        # the stop request or from a progress message alone.
        if self.gamepad_result is None:
            return False
        path = self.gamepad_result
        if not path.is_absolute():
            path = self.root/path
        try:
            data = (path/'summary.json').read_bytes()
            summary = json.loads(data)
            manifest = json.loads((path/'manifest.json').read_bytes())
            return (summary.get('semantics') == 'gamepad_v1'
                    and summary.get('reason') == 'user_stop'
                    and summary.get('released') is True
                    and summary.get('error') is None
                    and summary.get('logging_failure') is False
                    and manifest.get('complete') is True
                    and manifest.get('output_sha256', {}).get('summary.json') == hashlib.sha256(data).hexdigest())
        except (OSError, ValueError, TypeError, AttributeError):
            return False
