import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

AVAILABLE = importlib.util.find_spec('stable_baselines3') is not None


@unittest.skipUnless(AVAILABLE, 'Use the separate .venv-controller environment')
class PolicyTests(unittest.TestCase):
    def test_real_sac_python_and_rust_mean_outputs_match(self):
        import gymnasium as gym
        import numpy as np
        from stable_baselines3 import SAC
        from .policy import export_candidate
        class Synthetic(gym.Env):
            observation_space = gym.spaces.Box(-np.inf,np.inf,(3,),dtype=np.float32)
            action_space = gym.spaces.Box(-1.,1.,(2,),dtype=np.float32)
        # No real feature schema or unspecified SAC coefficients are decided here.
        model = SAC('MlpPolicy',Synthetic(),policy_kwargs=dict(net_arch=[128,128],n_critics=2),
                    buffer_size=200000,batch_size=256,learning_rate=.0003,seed=42,device='cpu')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/'test-schema.json'
            source.write_text('{"synthetic":true}',encoding='utf-8')
            raw=np.random.default_rng(42).normal(size=(128,3)).astype(np.float32)
            out=export_candidate(model,['test_x','test_y','test_z'],[.1,-.2,.3],[.2,3.,1.],raw,[source],root/'exports')
            manifest=json.loads((out/'manifest.json').read_text())
            self.assertTrue(manifest['complete'])
            self.assertFalse(manifest['eligible_for_live'])
            self.assertLessEqual(manifest['parity_max_abs'],1e-5)
            self.assertEqual(json.loads((out/'parity.json').read_text())['samples'],128)
            with self.assertRaises(ValueError):
                export_candidate(model,['test_x','test_x','test_z'],[0]*3,[1]*3,raw,[source],root/'bad')


if __name__ == '__main__': unittest.main()
