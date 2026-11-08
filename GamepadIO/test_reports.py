import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from ModelTrainer.common import digest, read, save
from .run_report import inspect_log,inspect_shutdown,completed_drive,report

class ReportTests(unittest.TestCase):
    def test_shutdown_requires_same_tracking_until_release(self):
        def observation(epoch,status='ready'):
            return dict(kind='observation',state=dict(tracking_epoch=epoch,status=status))
        rows=[observation(1),dict(kind='sent',mode='Running'),observation(1),dict(kind='sent',mode='Stopping'),observation(1)]
        self.assertTrue(inspect_shutdown(rows)['passed'])
        rows += [observation(2,'acquiring'),observation(2)]
        self.assertFalse(inspect_shutdown(rows)['passed'])
        self.assertFalse(inspect_shutdown([observation(1)])['passed'])
    def test_report_repair_cannot_promote_failed_or_unstarted_driving(self):
        good=dict(reason='time_limit',stop_confirmed=True,released=True,error=None,logging_failure=False,policy_samples=250)
        self.assertTrue(completed_drive(good))
        for key,value in [('reason','user_stop'),('stop_confirmed',False),('released',False),('error','fault'),('logging_failure',True),('policy_samples',0)]:
            with self.subTest(key=key):
                self.assertFalse(completed_drive(dict(good,**{key:value})))
    def fixture(self):
        return [dict(kind='observation',observation_ns=10),
                dict(kind='policy',mode='Running',observation_ns=10,now_ns=12,action=[-1.,1.,1.]),
                dict(kind='sent',mode='Running',send_start_ns=13,send_end_ns=15,report=dict(lx=-32768,rt=255,lt=255)),dict(kind='observation',observation_ns=19)]
    def test_causal_command_and_independent_triggers(self):
        self.assertTrue(inspect_log(self.fixture())[0]['passed'])
    def test_missing_observation_untransmitted_policy_and_changed_axes(self):
        rows=self.fixture();rows[2]['report']['rt']=0
        self.assertIn('requested_report_mismatch',inspect_log(rows)[0]['errors'])
        self.assertIn('policy_not_sent',inspect_log(self.fixture()[:2])[0]['errors'])
        self.assertIn('policy_without_prior_observation',inspect_log(self.fixture()[1:])[0]['errors'])
    def test_duplicate_observation_rejected(self):
        rows=self.fixture();rows.insert(1,dict(rows[0]));self.assertFalse(inspect_log(rows)[0]['passed'])
    def test_explicit_stop_may_supersede_last_inference(self):
        rows=self.fixture();rows += [dict(kind='observation',observation_ns=20),dict(kind='policy',mode='Running',observation_ns=20,now_ns=22,action=[1,1,0]),dict(kind='sent',mode='Stopping',send_start_ns=23,send_end_ns=24,report=dict(lx=0,rt=0,lt=255))]
        self.assertTrue(inspect_log(rows)[0]['passed'])

    def make_run(self, root, rows):
        model=root/'model';model.mkdir();save(model/'model.json',{})
        save(model/'manifest.json',dict(output_sha256={'model.json':digest(model/'model.json')}))
        run=root/'run';run.mkdir()
        (run/'samples.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
        save(run/'summary.json',dict(reason='user_stop',passed=False,correction_eligible=False))
        save(run/'manifest.json',dict(semantics='gamepad_v1',model=str(model),model_sha256=digest(model/'model.json'),
            output_sha256={name:digest(run/name) for name in ('samples.jsonl','summary.json')}))
        return run

    def test_user_stop_keeps_checks_without_teacher_loading(self):
        # No teacher/prepared paths exist: cancelled drives must not need them.
        for started in (False,True):
            with self.subTest(started=started),tempfile.TemporaryDirectory() as tmp:
                rows=self.fixture() if started else []
                for r in rows:
                    if r['kind']=='observation':r['state']=dict(tracking_epoch=1,status='ready')
                run=self.make_run(Path(tmp),rows)
                with patch('GamepadIO.run_report.read_inputs',side_effect=AssertionError('teacher loaded')):
                    result=report(run)
                self.assertEqual(result['transport']['passed'],started)
                self.assertEqual(result['shutdown']['passed'],started)
                self.assertEqual(result['comparison_status'],'deferred_user_stop')
                self.assertFalse(result['first_corner_pass_provisional'])
                self.assertFalse(read(run/'summary.json')['correction_eligible'])
                self.assertFalse((run/'gamepad-comparison.png').exists())

    def test_cancelled_report_still_rejects_changed_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=self.make_run(Path(tmp),[])
            (run/'samples.jsonl').write_text('changed')
            with self.assertRaisesRegex(ValueError,'Changed input/output'):report(run)

    def test_offline_output_preserves_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);run=self.make_run(root,[])
            before={p.name:digest(p) for p in run.iterdir()}
            report(run,output_dir=root/'offline')
            self.assertTrue((root/'offline'/'gamepad-comparison.json').is_file())
            self.assertEqual(before,{p.name:digest(p) for p in run.iterdir()})
            with self.assertRaisesRegex(ValueError,'separate output'):report(run,output_dir=run)

    def test_pending_report_requires_validation_before_correction_eligibility(self):
        from .run_report import repair_report
        for passed in (False,True):
            with self.subTest(passed=passed),tempfile.TemporaryDirectory() as tmp:
                run=self.make_run(Path(tmp),[])
                summary=dict(reason='time_limit',stop_confirmed=True,released=True,error=None,
                    logging_failure=False,policy_samples=10,passed=False,correction_eligible=False,
                    comparison_report_created=False,report_status='pending',intervention=True,correction_samples=5)
                save(run/'summary.json',summary,replace=True)
                manifest=read(run/'manifest.json');manifest['complete']=True
                manifest['output_sha256']['summary.json']=digest(run/'summary.json')
                save(run/'manifest.json',manifest,replace=True)
                with patch('GamepadIO.run_report.report',return_value=dict(transport=dict(passed=passed),shutdown=dict(passed=True))):
                    repair_report(run)
                self.assertEqual(read(run/'summary.json')['correction_eligible'],passed)
                self.assertEqual(read(run/'summary.json')['report_status'],'completed')

if __name__=='__main__':unittest.main()
