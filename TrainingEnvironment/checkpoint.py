"""SB3 policy/critics/optimizers + replay and RNG state, in immutable new folders."""
from pathlib import Path
import random
import sys

import gymnasium
import numpy as np
import stable_baselines3
from stable_baselines3 import SAC
import torch

from ModelTrainer.common import finish, hashes, load_artifact, new_folder, read, save, verify_hashes
from .lifecycle import SPEED_CAPS


def verify_environment():
    if (sys.version_info[:2] != (3, 11) or torch.__version__ != '2.8.0+cpu'
            or np.__version__ != '2.3.3' or stable_baselines3.__version__ != '2.7.0'
            or gymnasium.__version__ != '1.2.0'):
        raise ValueError('Use the separate pinned controller environment')


def validate_policy(model):
    verify_environment()
    if not isinstance(model, SAC) or model.action_space.shape != (2,):
        raise ValueError('A two-action SAC policy is required; v1 imitation is incompatible')
    if (model.policy.net_arch != [128, 128] or model.critic.n_critics != 2
            or model.batch_size != 256 or model.buffer_size != 200000
            or model.learning_rate != .0003 or model.seed != 42 or model.device.type != 'cpu'):
        raise ValueError('SAC configuration does not match the explicit plan')


def save_checkpoint(model, env, *, speed_cap, cumulative_episodes, input_paths,
                    stationary_confirmed, parent='reinforcement_runs', online_state=None):
    validate_policy(model)
    if (speed_cap not in SPEED_CAPS or type(cumulative_episodes) is not int or cumulative_episodes < 0):
        raise ValueError('Invalid speed stage/episode count')
    if not stationary_confirmed():
        raise RuntimeError('Checkpointing requires confirmed stop')
    inputs = hashes(input_paths)
    verify_hashes(inputs)
    out = new_folder(parent, 'sac-checkpoint')
    manifest = dict(schema_version=1, kind='sac_checkpoint', complete=False, feature_schema_version=2,
                    speed_cap=speed_cap, cumulative_episodes=cumulative_episodes,
                    input_sha256=inputs, environment=dict(python=sys.version, torch=torch.__version__,
                    numpy=np.__version__, sb3=stable_baselines3.__version__, gymnasium=gymnasium.__version__))
    save(out/'manifest.json', manifest)
    try:
        # SAC.save includes policy/critics plus actor, critic and entropy optimizers.
        model.save(out/'model.zip')
        model.save_replay_buffer(out/'replay.pkl')
        numpy_rng = np.random.get_state()
        torch.save(dict(python=random.getstate(), torch=torch.get_rng_state(),
                        numpy=[numpy_rng[0], numpy_rng[1].tolist(), *numpy_rng[2:]]), out/'rng.pt')
        save(out/'environment_rng.json', dict(environment=env.np_random.bit_generator.state,
                                             action=env.action_space.np_random.bit_generator.state,
                                             observation=env.observation_space.np_random.bit_generator.state))
        if online_state is not None:
            save(out/'online.json', online_state)
        if not stationary_confirmed():
            raise RuntimeError('Stationary confirmation lost during checkpoint save')
        verify_hashes(inputs)
        finish(out, manifest)
    except BaseException as error:
        finish(out, manifest, False, error)
        raise
    return out


def load_checkpoint(folder, env, *, expected_input_paths):
    verify_environment()
    # Validate the complete local inventory BEFORE loading SB3/pickled replay data.
    folder, manifest = load_artifact(folder, 'sac_checkpoint')
    if manifest.get('feature_schema_version') != 2:
        raise ValueError('Unsupported feature schema')
    if manifest['input_sha256'] != hashes(expected_input_paths):
        raise ValueError('Checkpoint input configuration differs from prepared job')
    required = {'model.zip', 'replay.pkl', 'rng.pt', 'environment_rng.json'}
    if not required <= manifest['output_sha256'].keys():
        raise ValueError('Incomplete SAC checkpoint inventory')
    if (manifest['speed_cap'] not in SPEED_CAPS or type(manifest['cumulative_episodes']) is not int
            or manifest['cumulative_episodes'] < 0):
        raise ValueError('Invalid checkpoint stage/counter')
    model = SAC.load(folder/'model.zip', env=env, device='cpu', force_reset=True)
    validate_policy(model)
    model.load_replay_buffer(folder/'replay.pkl')
    rng = torch.load(folder/'rng.pt', map_location='cpu', weights_only=True)
    environment_rng = read(folder/'environment_rng.json')
    random.setstate(rng['python'])
    np_rng = rng['numpy']
    np.random.set_state((np_rng[0], np.array(np_rng[1], dtype=np.uint32), *np_rng[2:]))
    torch.set_rng_state(rng['torch'])
    env.np_random.bit_generator.state = environment_rng['environment']
    env.action_space.np_random.bit_generator.state = environment_rng['action']
    env.observation_space.np_random.bit_generator.state = environment_rng['observation']
    return model, manifest
