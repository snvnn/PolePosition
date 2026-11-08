"""Reusable temporary fixtures; this module contains no TestCase classes."""
import json
from pathlib import Path
import tempfile
from ModelTrainer.common import finish, hashes, save

class PretrainFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
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


    def tearDown(self):
        self.temp.cleanup()
