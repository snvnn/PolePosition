import importlib.util
from pathlib import Path
import tempfile
import unittest

AVAILABLE = importlib.util.find_spec('stable_baselines3') is not None


@unittest.skipUnless(AVAILABLE, 'Use the separate .venv-controller environment')
class CheckpointTests(unittest.TestCase):
    def setUp(self):
        import gymnasium as gym
        import numpy as np
        import torch
        from stable_baselines3 import SAC
        from stable_baselines3.common.logger import configure
        class Synthetic(gym.Env):
            observation_space = gym.spaces.Box(-1., 1., (2,), dtype=np.float32)
            action_space = gym.spaces.Box(-1., 1., (2,), dtype=np.float32)
            def reset(self, *, seed=None, options=None):
                super().reset(seed=seed)
                return np.zeros(2, dtype=np.float32), {}
            def step(self, action): raise AssertionError('No game collection in checkpoint tests')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'synthetic-v2-schema.json'
        self.source.write_text('{"test_only":true}', encoding='utf-8')
        self.env = Synthetic()
        self.env.reset(seed=42)
        self.env.action_space.seed(43); self.env.observation_space.seed(44)
        torch.set_num_threads(1)
        torch.use_deterministic_algorithms(True)
        # Remaining library defaults here are fixture-only, not a production choice.
        self.model = SAC('MlpPolicy', self.env, policy_kwargs=dict(net_arch=[128,128], n_critics=2),
                         buffer_size=200000, batch_size=256, learning_rate=.0003, seed=42, device='cpu')
        self.model.set_logger(configure(folder=str(self.root/'logs'), format_strings=[]))
        for index in range(20):
            self.model.replay_buffer.add(np.array([[index/20,0]],dtype=np.float32),
                                        np.array([[(index+1)/20,0]],dtype=np.float32),
                                        np.array([[.1,-.1]],dtype=np.float32), np.array([.5]),
                                        np.array([False]), [{}])
        self.model.train(gradient_steps=1, batch_size=256)

    def save(self):
        from .checkpoint import save_checkpoint
        return save_checkpoint(self.model, self.env, speed_cap=10, cumulative_episodes=2,
                               input_paths=[self.source], stationary_confirmed=lambda:True,
                               parent=self.root/'checkpoints')

    def test_resume_restores_rng_replay_optimizers_and_next_update(self):
        import random
        import numpy as np
        import torch
        from stable_baselines3.common.logger import configure
        from .checkpoint import load_checkpoint
        saved = self.save()
        expected_rng = (random.random(), np.random.rand(), torch.rand(3), self.env.action_space.sample())
        self.model.train(gradient_steps=1, batch_size=256)
        expected = {k:v.clone() for k,v in self.model.policy.state_dict().items()}
        restored, manifest = load_checkpoint(saved, self.env, expected_input_paths=[self.source])
        restored.set_logger(configure(folder=str(self.root/'restored-logs'), format_strings=[]))
        actual_rng = (random.random(), np.random.rand(), torch.rand(3), self.env.action_space.sample())
        self.assertEqual(actual_rng[:2], expected_rng[:2])
        self.assertTrue(torch.equal(actual_rng[2], expected_rng[2]))
        self.assertTrue(np.array_equal(actual_rng[3], expected_rng[3]))
        self.assertEqual(restored.replay_buffer.size(),20)
        self.assertEqual(manifest['cumulative_episodes'],2)
        restored.train(gradient_steps=1, batch_size=256)
        for key,value in restored.policy.state_dict().items():
            self.assertTrue(torch.equal(value,expected[key]),key)

    def test_tampered_replay_rejected_before_library_load(self):
        from unittest.mock import patch
        from .checkpoint import load_checkpoint
        saved = self.save()
        with (saved/'replay.pkl').open('ab') as output: output.write(b'changed')
        with patch('TrainingEnvironment.checkpoint.SAC.load') as load:
            with self.assertRaises(ValueError): load_checkpoint(saved,self.env,expected_input_paths=[self.source])
            load.assert_not_called()

    def test_unconfirmed_stop_and_changed_inputs_rejected(self):
        from .checkpoint import save_checkpoint, load_checkpoint
        with self.assertRaises(RuntimeError):
            save_checkpoint(self.model,self.env,speed_cap=10,cumulative_episodes=2,
                            input_paths=[self.source],stationary_confirmed=lambda:False,parent=self.root/'bad')
        self.assertFalse((self.root/'bad').exists())
        saved = self.save()
        self.source.write_text('changed')
        with self.assertRaises(ValueError): load_checkpoint(saved,self.env,expected_input_paths=[self.source])


if __name__ == '__main__': unittest.main()
