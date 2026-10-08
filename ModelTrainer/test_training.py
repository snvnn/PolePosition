"""Deterministic training, artifact failures, split isolation and teacher provenance."""
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import torch
from .common import FIELDS, ROOT, classify, config, digest, finish, hashes, load_artifact, read, save
from .dataset import joined_teacher, normalizer_for
from .training import train, load_trained, export
from Evaluator.evaluate import metrics


def examples():
    result=[]
    for i in range(90):
        u=[-.3,0.,.3][i%3]
        x=[0.]*19;x[0]=u;x[3]=1000.
        t,b=max(u,0)*.2,max(-u,0)*.6
        result.append(dict(features=x,target_u=u,throttle=t,brake=b,pedal_class=classify(t,b),
            grade_pct=0.,target_speed_kmh=10.,throttle_observed=0.,brake_observed=0.))
    return result


class TrainingTests(unittest.TestCase):
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

    def states(self,p):return torch.load(p/'last.pt',weights_only=True)['model_state_dict']

    def test_resume_and_repeat_match_continuous_training(self):
        a=train(self.dataset,self.cp)
        partial=train(self.dataset,self.cp,epoch_limit=2)
        b=train(resume=partial/'last.pt')
        c=train(self.dataset,self.cp)
        for key,value in self.states(a).items():
            self.assertTrue(torch.equal(value,self.states(b)[key]),key)
            self.assertTrue(torch.equal(value,self.states(c)[key]),key)
        self.assertEqual(read(a/'summary.json')['updates'],24)
        self.assertNotEqual(a,b)
        _,_,_,model=load_trained(a)
        self.assertTrue(torch.isfinite(model(torch.zeros(1,19))).all())

    def test_interrupt_keeps_completed_epoch_and_resume(self):
        original=torch.optim.AdamW.step;calls=0
        def interrupt(opt,*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==8:raise KeyboardInterrupt()
            return original(opt,*args,**kwargs)
        with patch.object(torch.optim.AdamW,'step',interrupt):p=train(self.dataset,self.cp)
        self.assertFalse(read(p/'manifest.json')['complete'])
        self.assertEqual(read(p/'summary.json')['epochs'],1)
        with self.assertRaises(ValueError):load_trained(p)
        resumed=train(resume=p/'last.pt');full=train(self.dataset,self.cp)
        for k,v in self.states(full).items():self.assertTrue(torch.equal(v,self.states(resumed)[k]))

    def test_storage_failure_is_incomplete(self):
        with patch('ModelTrainer.training.checkpoint',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):train(self.dataset,self.cp)
        folders=list((self.root/'runs').iterdir());self.assertEqual(len(folders),1)
        self.assertFalse(read(folders[0]/'manifest.json')['complete'])

    def test_managed_stop_resumes_identically(self):
        progress=[]
        partial=train(self.dataset,self.cp,should_stop=lambda: len(progress)==1,on_progress=progress.append)
        self.assertEqual(progress[0]['epoch'],1)
        self.assertTrue(read(partial/'summary.json')['interrupted'])
        resumed=train(resume=partial/'last.pt')
        full=train(self.dataset,self.cp)
        for key,value in self.states(full).items():
            self.assertTrue(torch.equal(value,self.states(resumed)[key]))

    def test_managed_stop_before_epoch_is_incomplete(self):
        partial=train(self.dataset,self.cp,should_stop=lambda: True)
        self.assertFalse(read(partial/'manifest.json')['complete'])
        self.assertFalse((partial/'last.pt').exists())

    def test_managed_evaluation_is_frozen_and_cancellable(self):
        from Evaluator.evaluate import replay
        from .common import new_folder
        data=[dict(r,run_id='teacher-test',sequence=i,observation_ns=i*50_000_000,speed_kmh=10.)
              for i,r in enumerate(self.data)]
        (self.dataset/'test.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in data),encoding='utf-8')
        manifest=read(self.dataset/'manifest.json');finish(self.dataset,manifest)
        run=train(self.dataset,self.cp)
        before=digest(run/'best.pt')
        progress=[]
        with patch('Evaluator.evaluate.new_folder',side_effect=lambda _,prefix:new_folder(self.root/'evaluations',prefix)):
            complete=replay(run,self.dataset,'test',on_progress=progress.append)
            stopped=replay(run,self.dataset,'test',should_stop=lambda:True)
        self.assertTrue(read(complete/'manifest.json')['complete'])
        self.assertEqual(progress[-1]['samples'],len(data))
        self.assertFalse(read(stopped/'manifest.json')['complete'])
        self.assertEqual(digest(run/'best.pt'),before)

    def test_checkpoint_and_source_tamper_rejected(self):
        run=train(self.dataset,self.cp,epoch_limit=1)
        with (run/'last.pt').open('ab') as f:f.write(b'changed')
        with self.assertRaises(ValueError):train(resume=run/'last.pt')
        (self.root/'source').write_text('changed')
        with self.assertRaises(ValueError):load_artifact(self.dataset,'teacher_dataset')

    def test_smoke_cannot_export_and_empty_holdouts_cannot_train(self):
        run=train(self.dataset,self.cp,smoke=True)
        with self.assertRaises(ValueError):export(run)
        m=read(self.dataset/'manifest.json');m['training_ready']=False;save(self.dataset/'manifest.json',m,replace=True)
        with self.assertRaises(ValueError):train(self.dataset,self.cp)

    def test_train_statistics_ignore_holdout_and_constant_columns(self):
        before=normalizer_for(self.data)
        (self.dataset/'test.jsonl').write_text('{"features":[999]}')
        self.assertEqual(before,normalizer_for(self.data))
        self.assertEqual(before['std'][3],1.)
        totals=[before['class_weights'][k]*v for k,v in before['train_class_counts'].items()]
        self.assertEqual(len(set(totals)),1)

    def test_metric_gates_missing_group_baseline_and_nonfinite(self):
        target=[r['target_u'] for r in self.data]
        self.assertTrue(metrics(self.data,target,0.)['passed'])
        self.assertFalse(metrics(self.data,[0.]*90,0.)['passed'])
        coast=[r for r in self.data if r['pedal_class']=='coast']
        self.assertFalse(metrics(coast,[0.]*len(coast),0.)['passed'])
        with self.assertRaises(ValueError):metrics(self.data,[float('nan')]*90,0.)

    def test_collision_and_output_traversal(self):
        p=self.root/'unique.json';save(p,{'a':1})
        with self.assertRaises(FileExistsError):save(p,{'a':2})
        m=read(self.dataset/'manifest.json');m['output_sha256']['../source']=digest(self.root/'source');save(self.dataset/'manifest.json',m,replace=True)
        with self.assertRaises(ValueError):load_artifact(self.dataset,'teacher_dataset')

    def test_teacher_dedup_ambiguity_and_time_mismatch(self):
        folder=self.root/'teacher';folder.mkdir()
        fields=['now_ns','observation_sequence','tracking_epoch','observation_ns','reason','abort','complete','target_speed_kmh']
        ds=[dict(zip(fields,[str(101+i),'1','1','100','tracking','false','false','10'])) for i in range(2)]
        with (folder/'controller.csv').open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(ds)
        commands=[dict(d,sequence=str(i+1),created_ns='100',mode='Running',reason='accepted',send_success='false',requested_throttle='.1',requested_brake='0') for i,d in enumerate(ds)]
        obs=dict(sequence='1',tracking_epoch='1',observation_ns='100',physics_ns='100',graphics_ns='100',state_status='ready',in_pit='false',gear_raw='2')
        m=dict(complete=True,scenario='demo',mock=False,config={},input_sha256={'fixture':'verified by stub'},
               output_sha256={k:'verified by stub' for k in ['commands.csv','telemetry.csv','events.csv','summary.json','controller.csv','config.json']})
        s=dict(normal_shutdown=True,recording_valid=True,stop_confirmed=True,released=True,termination_reason='time_limit',dropped=0)
        with patch('ModelTrainer.dataset.verify',return_value=(m,s,commands,[obs],[])):
            self.assertEqual(len(joined_teacher(folder)[1]),1)
            commands[1]['requested_throttle']='.2';_,rows,excluded=joined_teacher(folder)
            self.assertEqual(len(rows),0);self.assertEqual(excluded[0]['reason'],'ambiguous_duplicate')
            commands[1]['requested_throttle']='.1';commands[0]['created_ns']='99'
            with self.assertRaises(ValueError):joined_teacher(folder)


if __name__=='__main__':unittest.main()
