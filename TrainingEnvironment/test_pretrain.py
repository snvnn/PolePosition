import json
from pathlib import Path
import tempfile
import unittest

import torch

from ModelTrainer.common import finish, hashes, read, save
from .pretrain import load_dataset, train
from .preparation import causal_frames


class PretrainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dataset = self.root/'data'
        self.dataset.mkdir()
        self.cp = self.root/'config.toml'
        config = Path('TrainingEnvironment/pretrain.toml').read_text()
        config = config.replace('max_epochs = 100','max_epochs = 3').replace(
            'output_dir = "reinforcement_runs"', 'output_dir = '+json.dumps(str(self.root/'outputs')))
        self.cp.write_text(config)
        save(self.dataset/'normalizer.json',dict(feature_names=['x','y','z'],mean=[0,0,0],std=[1,1,1]))
        for split in ('train','validation','test'):
            records = [dict(run_id=split,features=[i/64,0,1],action=[i/128,.1]) for i in range(64)]
            (self.dataset/f'{split}.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
        self.manifest = dict(schema_version=1,kind='teacher_features_v2',input_sha256=hashes([self.cp]),
                             feature_schema_version=2,policy_training_ready=True,feature_names=['x','y','z'],
                             source_splits={s:[s] for s in ('train','validation','test')})
        finish(self.dataset,self.manifest)

    def test_stop_resume_matches_uninterrupted_and_preserves_critics(self):
        complete = train(self.dataset,self.cp)
        request = [False]
        stopped = train(self.dataset,self.cp,should_stop=lambda: request[0],
                        on_progress=lambda p: request.__setitem__(0,True) if p.get('epoch') == 1 else None)
        self.assertFalse(read(stopped/'manifest.json')['complete'])
        resumed = train(None,self.cp,stopped/'last.pt')
        a,b = (torch.load(p/'last.pt',weights_only=True) for p in (complete,resumed))
        for key in a['policy']:
            self.assertTrue(torch.equal(a['policy'][key],b['policy'][key]),key)
        summary = read(resumed/'summary.json')
        self.assertTrue(summary['critic_unchanged'])
        self.assertEqual(summary['replay_samples'],0)
        self.assertTrue(read(Path(summary['candidate'])/'parity.json')['passed'])

    def test_split_leakage_and_tampering_rejected(self):
        path = self.dataset/'test.jsonl'
        path.write_text(path.read_text().replace('"test"','"train"'))
        with self.assertRaises(ValueError):
            load_dataset(self.dataset)
        self.manifest['source_splits']['test']=['train']
        finish(self.dataset,self.manifest)
        with self.assertRaisesRegex(ValueError,'crosses split'):
            load_dataset(self.dataset)

    def test_causal_axes_exclude_future_and_equal_timestamp(self):
        example = dict(observation_ns=100,tracking_epoch=1,sequence=1,target_speed_kmh=10,
                       observation=dict(speed_kmh=10,rpm=2000,lateral_error_m=0,heading_error_rad=0,path_s_m=0,gear_raw=2))
        def sent(t,steering):
            return dict(send_success='true',now_ns=str(t),mode='Running',tracking_epoch='1',
                        applied_steering=str(steering),applied_throttle='0.1',applied_brake='0')
        pair = list(causal_frames([example],[sent(90,.2),sent(100,.5),sent(110,.8)]))
        self.assertEqual(pair[0][1]['previous_axes'],(.2,.1,0.))
        self.assertEqual(list(causal_frames([example],[sent(100,.5)])),[])


if __name__ == '__main__': unittest.main()
