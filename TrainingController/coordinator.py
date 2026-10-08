"""Transition from read-only monitoring to exactly one requested job."""
from pathlib import Path

from .process import ManagedProcess


class Coordinator:
    def __init__(self, root, monitoring=True, factory=ManagedProcess):
        self.root = Path(root)
        self.monitor = factory()
        self.job = factory()
        self.monitoring = monitoring
        self.monitor_requested = monitoring
        self.pending = None
        self.closing = False

    @property
    def busy(self):
        return self.pending is not None or self.job.process is not None

    @property
    def closed(self):
        return self.closing and self.monitor.process is None and self.job.process is None

    def start(self, command):
        if self.busy or self.closing:
            raise RuntimeError('A job is already active or closing')
        self.pending = list(command)
        self.monitor_requested = False
        if self.monitor.process is not None:
            self.monitor.stop()

    def stop(self):
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
        for event in self.monitor.poll():
            result.append(('monitor', event))
        for event in self.job.poll():
            result.append(('job', event))
            if event['event'] == 'finished':
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
