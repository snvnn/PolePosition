import unittest
import csv
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
import numpy as np
from .gamepad import align,read_inputs,train

class AlignmentTests(unittest.TestCase):
    def test_reference_finish_seam_resets_but_teleports_and_other_jumps_fail(self):
        from .human_imitation import projection_breaks
        rows=[dict(elapsed_ns=1, speed_kmh=220,position_x=0,position_z=0),
              dict(elapsed_ns=10_000_001,speed_kmh=220,position_x=.31,position_z=0)]
        geom=np.array([[96.4,0,0],[99.5,0,0]])
        with self.assertRaises(ValueError):projection_breaks(rows,geom,100)
        self.assertEqual(projection_breaks(rows,geom,100,True),[1])
        with self.assertRaises(ValueError):projection_breaks(rows,geom- [40,0,0],100,True)
        rows[1]['position_x']=8
        with self.assertRaises(ValueError):projection_breaks(rows,geom,100,True)

    def test_causal_label_and_previous(self):
        inputs=[dict(read_start_ns=t,read_end_ns=t+1,segment_id=1) for t in (10,20,30)]
        r=align([dict(elapsed_ns=20,segment_id=1)],inputs)
        self.assertEqual(r[0][1],inputs[0]);self.assertEqual(r[0][2],inputs[1])
    def test_no_cross_epoch_or_gap_or_future_previous(self):
        self.assertEqual(align([dict(elapsed_ns=20,segment_id=2)],[dict(read_start_ns=10,read_end_ns=11,segment_id=1),dict(read_start_ns=21,read_end_ns=22,segment_id=2)]),[])
        self.assertEqual(align([dict(elapsed_ns=20,segment_id=1)],[dict(read_start_ns=21,read_end_ns=22,segment_id=1)]),[])
        self.assertEqual(align([dict(elapsed_ns=20,segment_id=1)],[dict(read_start_ns=10,read_end_ns=11,segment_id=1),dict(read_start_ns=30_000_000,read_end_ns=30_000_001,segment_id=1)]),[])

class RawInputTests(unittest.TestCase):
    def row(self,seq):
        return dict(sequence=seq,segment_id=1,read_start_ns=seq*10_000_000,read_end_ns=seq*10_000_000+100,
            slot=0,packet=7,buttons=0,lx=-32768,ly=0,rx=0,ry=0,lt=255,rt=255,normalization_version=1,
            connected='true',stick_x=-1,stick_y=0,right_x=0,right_y=0,throttle=1,brake=1)
    def read(self,rows):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'inputs.csv'
            with p.open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=rows[0]);w.writeheader();w.writerows(rows)
            return read_inputs(p)
    def test_packet_holds_and_overlapping_triggers_valid(self):
        self.assertEqual(len(self.read([self.row(1),self.row(2)])),2)
    def test_disconnect_missing_sequence_device_change_and_corruption(self):
        for key,value in [('connected','false'),('sequence',3),('slot',1),('read_start_ns',1),('right_x',float('nan')),('ry',32768),('throttle',.5)]:
            with self.subTest(key=key):
                row=self.row(2);row[key]=value
                with self.assertRaises(ValueError):self.read([self.row(1),row])

class TrainingIntegrationTests(unittest.TestCase):
    def test_train_export_native_parity_independent_split_and_stop(self):
        # Synthetic data is confined to a temp test directory; never labeled teacher evidence.
        from ModelTrainer.common import save,read
        with tempfile.TemporaryDirectory() as d:
            d=Path(d);recordings=[]
            for i in range(3):
                r=d/f'r{i}';r.mkdir();recordings.append(r)
                for n in ('metadata.json','summary.json','telemetry.csv','inputs.csv','controls.snapshot.json','assists.snapshot.json'):
                    (r/n).write_text(str(i))
            prepared=d/'prepared';prepared.mkdir();save(prepared/'manifest.json',{})
            save(d/'speed.json',{'test_only':True})
            c=d/'train.toml';c.write_text('recordings = '+json.dumps([str(p) for p in recordings])+'\nprepared = '+json.dumps(str(prepared))+'\nspeed_profile = '+json.dumps(str(d/'speed.json'))+'\noutput_dir = '+json.dumps(str(d/'out'))+'\nepochs = 2\n')
            rng=np.random.default_rng(42)
            features=rng.normal(size=(300,35)).astype(np.float32)
            actions=np.column_stack((np.tanh(features[:,0]),1/(1+np.exp(-features[:,1])),1/(1+np.exp(-features[:,2]))))
            import subprocess
            from ModelTrainer.dataset import policy_binary
            names=json.loads(subprocess.check_output([str(policy_binary()),'gamepad-schema'],text=True))['feature_names']
            rows=[dict(features=x.tolist(),action=y.tolist()) for x,y in zip(features,actions)]
            profile=dict(input_sha256={'c':'a','a':'b'},controls='c',assists='a',steam_input_disabled=True)
            with patch('TrainingEnvironment.gamepad.prepare_recording',return_value=(rows,names,profile)):
                result=train(c);manifest=read(result/'manifest.json');summary=read(result/'summary.json')
                self.assertTrue(manifest['complete']);self.assertTrue(manifest['parity_passed']);self.assertLessEqual(summary['parity_max_error'],1e-5)
                self.assertEqual(manifest['split'],['train','train','validation']);self.assertFalse(summary['live_performance_verified'])
                fine=d/'correction.toml';fine.write_text(c.read_text()+'parent_model = '+json.dumps(str(result))+'\nrequire_corrections = true\ncorrections = ["synthetic-only"]\n')
                corrected=[dict(rows[i],mask=[1,0,0]) for i in range(20)]
                with patch('TrainingEnvironment.corrections.load_corrections',return_value=(corrected,{},[dict(run='synthetic-only',examples=20)])):
                    updated=train(fine);um=read(updated/'model.json');base=read(result/'model.json');us=read(updated/'summary.json')
                    self.assertEqual(um['mean'],base['mean']);self.assertEqual(um['std'],base['std'])
                    self.assertEqual(us['correction_examples'],20);self.assertEqual(us['validation_examples'],300)
                    self.assertLessEqual(us['parity_max_error'],1e-5)
                with self.assertRaises(InterruptedError):train(c,should_stop=lambda:True)

if __name__=='__main__':unittest.main()
