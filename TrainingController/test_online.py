import unittest
from pathlib import Path
from unittest.mock import patch
from .jobs import command
from .protocol import validate_event

class OnlineGuiContractTests(unittest.TestCase):
    def test_online_resume_uses_folder_and_training_worker(self):
        root=Path(__file__).resolve().parents[1]
        values=dict(seconds='7200',mode='train',config='TrainingEnvironment/online.toml',
                    prepared='state_runs',profile='ModelAndActuator/config.toml',
                    controller_config='BaselineController/config.toml',checkpoint='reinforcement_runs')
        args=command(root,'강화학습',values,resume=True)
        self.assertIn('online',args)
        self.assertEqual(args[args.index('--resume')+1],str((root/'reinforcement_runs').resolve()))
        self.assertNotIn('--model',args)
    def test_online_progress_checks_reward_and_episode(self):
        valid=dict(version=1,event='progress',episode=1,updates=2,reward=-1.5,phase='learning')
        validate_event(valid)
        for key,value in [('episode',True),('reward',float('nan'))]:
            with self.assertRaises(ValueError):validate_event(dict(valid,**{key:value}))

if __name__=='__main__':unittest.main()
