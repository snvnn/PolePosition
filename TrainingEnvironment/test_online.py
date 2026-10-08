import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
import torch
from stable_baselines3 import SAC
from stable_baselines3.common.logger import configure
from ModelTrainer.common import save
from TrainingEnvironment.experience import transitions,add_replay
from TrainingEnvironment.online_policy import BoundedPolicy,BoundedActor
from TrainingEnvironment.pretrain import OfflineSpaces
from TrainingEnvironment.checkpoint import save_checkpoint,load_checkpoint

class ExperienceTests(unittest.TestCase):
    def fixture(self,reason='episode_timeout'):
        config=dict(reinforcement=dict(deterministic=False),baseline=dict(config=dict(max_steering=1.,max_throttle=.15,max_brake=.6)))
        manifest=dict(complete=True,mock=False,config=config)
        summary=dict(normal_shutdown=True,recording_valid=True,stop_confirmed=True,released=True,dropped=0,rejected_commands=0,
                     released_while_halted=False,termination_reason='scenario_complete',controller_exit_reason=reason)
        telemetry=[];policy=[]
        for i in range(3):
            ns=1_000_000_000+i*20_000_000
            telemetry.append(dict(sequence=str(i+1),observation_ns=str(ns),tracking_epoch='1',state_status='ready',
                                  accumulated_m=str(i*.05),lateral_error_m='0',heading_error_rad='0'))
            x=[0.]*35
            if i: x[19:22]=[.1,.05,0]
            policy.append(dict(observation_sequence=str(i+1),observation_ns=str(ns),tracking_epoch='1',features_json=json.dumps(x),
                               control_source='model_terminal' if i==2 else 'model',policy_reason=reason if i==2 else 'model_control',
                               prediction_steering='.1',prediction_u='.05'))
        def cmd(t,obs,steer,gas):
            return dict(now_ns=str(t),sequence=str(obs+1),observation_sequence=str(obs),tracking_epoch='1',send_success='true',mode='Running',
                        applied_steering=str(steer),applied_throttle=str(gas),applied_brake='0',requested_steering=str(steer),requested_throttle=str(gas),requested_brake='0')
        commands=[cmd(990_000_000,0,0,0),cmd(1_001_000_000,1,.1,.05),cmd(1_021_000_000,2,.1,.05)]
        return (manifest,summary,commands,telemetry,[]),policy
    def read(self,fixture):
        evidence,policy=fixture
        with patch('TrainingEnvironment.experience.verify',return_value=evidence),patch('TrainingEnvironment.experience.read_rows',return_value=policy):
            return transitions('unit-test')[0]
    def test_timeout_bootstraps_and_failure_penalty(self):
        rows=self.read(self.fixture())
        self.assertTrue(rows[-1]['truncated']);self.assertFalse(rows[-1]['terminated'])
        failure=self.read(self.fixture('stalled'))
        self.assertTrue(failure[-1]['terminated']);self.assertAlmostEqual(rows[-1]['reward']-failure[-1]['reward'],10)
    def test_missing_ack_future_input_and_missing_terminal_are_rejected(self):
        f=self.fixture();f[0][2].pop(1)
        with self.assertRaises(ValueError):self.read(f)
        f=self.fixture();f[1][-1]['control_source']='model'
        with self.assertRaises(ValueError):self.read(f)
        f=self.fixture();f[0][2][0]['now_ns']='1000000000'
        with self.assertRaises(ValueError):self.read(f)
    def test_interrupt_unconfirmed_stop_and_teleport_are_rejected(self):
        for key,value in [('termination_reason','f8'),('stop_confirmed',False),('dropped',1),('released_while_halted',True)]:
            f=self.fixture();f[0][1][key]=value
            with self.assertRaises(ValueError):self.read(f)
        f=self.fixture();f[0][3][-1]['accumulated_m']='100'
        with self.assertRaises(ValueError):self.read(f)
    def test_reverse_is_negative_progress(self):
        f=self.fixture();f[0][3][-1]['accumulated_m']='0.01'
        rows=self.read(f);self.assertLess(rows[-1]['reward_parts']['progress'],0)

class OnlineUpdateTests(unittest.TestCase):
    def test_real_sac_update_checkpoint_restores_bounded_actor_and_replay(self):
        torch.set_num_threads(1)
        env=OfflineSpaces(35)
        model=SAC(BoundedPolicy,env,policy_kwargs=dict(net_arch=[128,128],n_critics=2),buffer_size=200000,
                  batch_size=256,learning_rate=.0003,seed=42,device='cpu',gamma=.99,tau=.005)
        model.set_logger(configure(folder=None,format_strings=[]))
        norm=dict(mean=[0.]*35,std=[1.]*35)
        rows=[]
        rng=np.random.default_rng(42)
        for i in range(256):
            rows.append(dict(observation=rng.normal(size=35).tolist(),next_observation=rng.normal(size=35).tolist(),
                             action=[.05,-.02],reward=.1,terminated=False,truncated=i==255))
        add_replay(model,rows,norm)
        self.assertEqual(model.replay_buffer.size(),256)
        self.assertEqual(model.replay_buffer.timeouts[255,0],1.)
        before=copy.deepcopy(model.actor.state_dict())
        model.train(gradient_steps=2,batch_size=256)
        self.assertTrue(any(not torch.equal(before[k],v) for k,v in model.actor.state_dict().items()))
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);save(path/'input.json',{'fixture':True})
            cp=save_checkpoint(model,env,speed_cap=10,cumulative_episodes=1,input_paths=[path/'input.json'],
                               stationary_confirmed=lambda:True,parent=path,online_state={'rng_state':42})
            restored,_=load_checkpoint(cp,OfflineSpaces(35),expected_input_paths=[path/'input.json'])
            self.assertIsInstance(restored.actor,BoundedActor)
            self.assertEqual(restored.replay_buffer.size(),256)
            restored.set_logger(configure(folder=None,format_strings=[]))
            _,std,_=restored.actor.get_action_dist_params(torch.zeros((1,35)))
            self.assertTrue(torch.all((std>=-5)&(std<=-2)))
            for k,v in model.actor.state_dict().items():self.assertTrue(torch.equal(v,restored.actor.state_dict()[k]))
            # Capture and restore all RNG before the next real optimizer step.
            model.train(gradient_steps=1,batch_size=256)
            restored,_=load_checkpoint(cp,OfflineSpaces(35),expected_input_paths=[path/'input.json'])
            restored.set_logger(configure(folder=None,format_strings=[]));restored.train(gradient_steps=1,batch_size=256)
            for k,v in model.policy.state_dict().items():self.assertTrue(torch.equal(v,restored.policy.state_dict()[k]))

if __name__=='__main__':unittest.main()
