from pathlib import Path
import time
import unittest
import tempfile
import json
import hashlib

from .coordinator import Coordinator
from .process import ManagedProcess

ROOT = Path(__file__).resolve().parents[1]


class FakeProcess:
    def __init__(self):
        self.process = None
        self.starts = []
        self.stopped = False
        self.events = []
    def start(self, command, cwd):
        if self.process is not None: raise RuntimeError('Already running')
        self.process = object()
        self.starts.append(command)
        self.stopped = False
    def stop(self): self.stopped = True
    def finish(self):
        self.process = None
        self.events.append(dict(event='finished',state='stopped',reason='user_stop'))
    def poll(self):
        events, self.events = self.events, []
        return events


class CoordinatorTests(unittest.TestCase):
    def setUp(self): self.runner = Coordinator(ROOT,factory=FakeProcess)

    def test_waits_for_actual_monitor_exit_before_job_start(self):
        self.runner.poll()
        self.runner.start(['actual-job'])
        self.runner.poll()
        self.assertTrue(self.runner.monitor.stopped)
        self.assertFalse(self.runner.job.starts)
        self.runner.monitor.finish()
        self.runner.poll()
        self.assertEqual(self.runner.job.starts,[['actual-job']])

    def test_pending_cancellation_does_not_launch_job(self):
        self.runner.poll(); self.runner.start(['actual-job']); self.runner.stop()
        self.runner.monitor.finish(); self.runner.poll()
        self.assertFalse(self.runner.job.starts)
        self.assertIsNotNone(self.runner.monitor.process)

    def test_close_cancels_pending_and_waits_for_both_children(self):
        self.runner.poll(); self.runner.start(['actual-job']); self.runner.close()
        self.assertFalse(self.runner.closed)
        self.runner.monitor.finish(); self.runner.poll()
        self.assertTrue(self.runner.closed)
        self.assertFalse(self.runner.job.starts)

    def test_job_finished_restarts_only_readonly_monitor(self):
        self.runner.start(['actual-job']); self.runner.poll()
        self.runner.job.finish(); self.runner.poll()
        self.assertEqual(len(self.runner.job.starts),1)
        self.assertEqual(len(self.runner.monitor.starts),1)
        self.assertIn('poleposition-telemetry.exe',self.runner.monitor.starts[0][0])

    def test_monitor_failure_does_not_spin_restart(self):
        self.runner.poll(); self.runner.monitor.finish()
        self.runner.poll(); self.runner.poll()
        self.assertEqual(len(self.runner.monitor.starts),1)
        self.runner.refresh(); self.runner.poll()
        self.assertEqual(len(self.runner.monitor.starts),2)

    def test_second_request_is_rejected_even_while_monitor_stops(self):
        self.runner.poll(); self.runner.start(['first'])
        with self.assertRaises(RuntimeError): self.runner.start(['second'])

    def test_released_gamepad_can_prepare_while_previous_report_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)
            runner=Coordinator(path,monitoring=False,factory=FakeProcess)
            runner.start(['gamepad']);runner.poll()
            old=runner.job
            old.events.append(dict(event='progress',model_type='gamepad_v1',result_dir=str(path)))
            runner.stop();runner.poll()
            self.assertTrue(runner.busy)
            summary=dict(semantics='gamepad_v1',reason='user_stop',released=False,error=None,logging_failure=False)
            def publish():
                data=json.dumps(summary).encode()
                (path/'summary.json').write_bytes(data)
                (path/'manifest.json').write_text(json.dumps(dict(complete=True,output_sha256={'summary.json':hashlib.sha256(data).hexdigest()})))
            publish();runner.poll()
            self.assertTrue(runner.busy)
            summary['released']=True;publish()
            events=runner.poll()
            self.assertFalse(runner.busy)
            self.assertIn(old,runner.background)
            self.assertTrue(any(source=='job' and e['state']=='stopped' for source,e in events))
            runner.start(['next']);runner.poll()
            old.finish()
            events=runner.poll()
            self.assertTrue(runner.busy)
            self.assertEqual(runner.job.starts,[['next']])
            self.assertTrue(any(source=='background' for source,e in events))
            self.assertFalse(runner.background)

    def test_close_waits_for_background_result_cleanup(self):
        runner=Coordinator(ROOT,monitoring=False,factory=FakeProcess)
        worker=FakeProcess();worker.start(['report'],ROOT)
        runner.background.append(worker)
        runner.close();runner.poll()
        self.assertFalse(runner.closed)
        worker.finish();runner.poll()
        self.assertTrue(runner.closed)


@unittest.skipUnless((ROOT/'target/release/poleposition-telemetry.exe').is_file(), 'Build Rust release binaries')
class MonitorIntegrationTests(unittest.TestCase):
    def test_actual_readonly_monitor_reports_and_stops(self):
        job = ManagedProcess()
        job.start([str(ROOT/'target/release/poleposition-telemetry.exe'),'--managed'],ROOT)
        seen = None
        result = None
        try:
            deadline = time.monotonic()+8
            while time.monotonic()<deadline and job.process is not None:
                for event in job.poll():
                    if event['event'] == 'progress':
                        seen=event; job.stop()
                    if event['event'] == 'finished': result=event
                time.sleep(.02)
            self.assertIsNotNone(seen)
            self.assertTrue(seen['read_only'])
            self.assertEqual(seen['phase'],'monitoring')
            self.assertIn('fresh',seen)
            self.assertIsNotNone(result)
            self.assertEqual(result['reason'],'user_stop')
            self.assertIsNone(job.process)
        finally:
            if job.process is not None:
                job.stop()
                deadline=time.monotonic()+4
                while job.process is not None and time.monotonic()<deadline:
                    job.poll();time.sleep(.05)


if __name__ == '__main__': unittest.main()
