import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from .live import inspect


class LiveAuditTests(unittest.TestCase):
    def fixture(self,root):
        (root/'model.json').write_text('{}')
        m=dict(complete=True,config={'policy':dict(mode='drive',model_seconds=2,model=str(root))})
        s=dict(normal_shutdown=True,stop_confirmed=True,released=True,dropped=0,termination_reason='scenario_complete',controller_exit_reason='policy_duration_complete',
            poll_interval={},observation_age={},ipc_delivery={},driver_call={},stop_to_zero_send_ns=1000)
        rows=[];commands=[];telemetry=[]
        for i in range(1,5):
            rows.append(dict(now_ns=str(i*10**9),observation_ns=str(i*10**9),observation_sequence=str(i),tracking_epoch='1',control_source='model' if i<3 else 'stop',
                model_throttle='.04',model_brake='0',inference_ns='5000',feature_ns='1000'))
            if i<3:
                commands.append(dict(observation_sequence=str(i),tracking_epoch='1',reason='accepted',mode='Running',send_success='true',requested_throttle='.04',requested_brake='0'))
            telemetry.append(dict(sequence=str(i),tracking_epoch='1',speed_kmh='10',lateral_error_m='.1'))
        with (root/'policy.csv').open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
        return m,s,commands,telemetry,[]

    def test_model_duration_excludes_stopping_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);fixture=self.fixture(root)
            with patch('Evaluator.live.verify',return_value=fixture):result=inspect(root)
            self.assertEqual(result['model_observation_interval_seconds'],2.)
            self.assertEqual(result['successful_model_transmissions'],2)
            self.assertEqual(result['inference']['p99_ms'],.005)

    def test_shadow_and_incorrect_model_request_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);fixture=self.fixture(root)
            with patch('Evaluator.live.verify',return_value=fixture):
                fixture[0]['config']['policy']['mode']='shadow'
                with self.assertRaises(ValueError):inspect(root)
                fixture[0]['config']['policy']['mode']='drive';fixture[2][0]['requested_throttle']='.1'
                with self.assertRaises(ValueError):inspect(root)
