import unittest
from .run_report import inspect_log,inspect_shutdown,completed_drive

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

if __name__=='__main__':unittest.main()
