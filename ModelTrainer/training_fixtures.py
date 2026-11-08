"""Reusable temporary fixtures; this module contains no TestCase classes."""
import json
from pathlib import Path
import tempfile
from .common import ROOT, classify, config, finish, hashes, save
from .dataset import normalizer_for

def examples():
    result=[]
    for i in range(90):
        u=[-.3,0.,.3][i%3]
        x=[0.]*19;x[0]=u;x[3]=1000.
        t,b=max(u,0)*.2,max(-u,0)*.6
        result.append(dict(features=x,target_u=u,throttle=t,brake=b,pedal_class=classify(t,b),
            grade_pct=0.,target_speed_kmh=10.,throttle_observed=0.,brake_observed=0.))
    return result


class TrainingFixture:
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.dataset=self.root/'dataset';self.dataset.mkdir()
        source=self.root/'source';source.write_text('immutable')
        self.data=examples()
        for split in ['train','validation','test']:
            (self.dataset/(split+'.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in self.data),encoding='utf-8')
        save(self.dataset/'normalizer.json',normalizer_for(self.data))
        m=dict(schema_version=1,kind='teacher_dataset',complete=False,training_ready=True,input_sha256=hashes([source]))
        save(self.dataset/'manifest.json',m);finish(self.dataset,m)
        self.c=config(ROOT/'ModelTrainer/config.toml');self.c.update(max_epochs=4,patience=20,batch_size=16,
            output_dir=str(self.root/'runs'),dataset_dir=str(self.root/'datasets'),export_dir=str(self.root/'exports'))
        self.cp=self.root/'config.toml'
        self.cp.write_text('\n'.join(k+' = '+json.dumps(v) for k,v in self.c.items()),encoding='utf-8')

    def tearDown(self):self.temp.cleanup()
