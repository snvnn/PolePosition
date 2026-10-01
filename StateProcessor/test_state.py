import copy
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from DatasetBuilder.builder import build as build_dataset, csv_write, json_write, REQUIRED
from DatasetBuilder.review import scan, finalize, read_json, digest, snapshot
from TrackModel.model import build as build_track, load_reference
from TrackModel.test_model import make_source
from .prepare import calibrate, config_check, prepare
from .report import report, load_run, signal_chart


def straight_laps():
    result={}
    for lap,angle in enumerate((0.2,1.1,2.1)):
        result[str(lap)]=[dict(sequence=i,segment_id=1,elapsed_ns=i*10_000_000,speed_kmh=72,
            position_x=i*.2*math.cos(angle),position_z=i*.2*math.sin(angle),heading_rad=angle-math.pi/2)
            for i in range(2000)]
    return result


class HeadingTests(unittest.TestCase):
    def test_signal_plot_does_not_bridge_tracking_epochs(self):
        rows=[dict(seconds=0,v=1,plot_group=1),dict(seconds=1,v=2,plot_group=1),dict(seconds=5,v=3,plot_group=2)]
        svg=signal_chart(rows,'v','test');self.assertEqual(svg.count('<polyline '),2)
        ET.fromstring(re.search(r'<svg\b.*?</svg>',svg,re.S)[0])
    def test_mapping_and_actual_center_spacing(self):
        profile=calibrate(straight_laps(),['0','1','2'],{},config_check({}))
        self.assertEqual(profile['sign'],1)
        self.assertEqual(profile['offset_rad'],math.pi/2)
        self.assertLess(profile['candidates'][0]['p95_abs_deg'],1e-10)
        for lid in ('0','1','2'):
            times=[r['elapsed_ns'] for r in profile['selected_windows'] if r['lap_id']==lid]
            self.assertTrue(all(b-a>=200_000_000 for a,b in zip(times,times[1:])))

    def test_held_validation_test_do_not_influence_profile(self):
        rows=straight_laps();before=calibrate(rows,list(rows),{},config_check({}))
        rows['test']=[dict(elapsed_ns=0,heading_rad=math.nan)]
        rows['hold']=[dict(elapsed_ns=-1)]
        self.assertEqual(before,calibrate(rows,['0','1','2'],{},config_check({})))

    def test_ambiguous_heading_fails(self):
        rows=straight_laps()
        for lap in rows.values():
            for i,r in enumerate(lap):r['heading_rad']=i*.73
        with self.assertRaisesRegex(ValueError,'ambiguous'):calibrate(rows,list(rows),{},config_check({}))

    def test_insufficient_and_time_reversal_fail(self):
        rows=straight_laps()
        with self.assertRaisesRegex(ValueError,'Insufficient'):calibrate(rows,['0'],{},config_check({}))
        rows['0'][100]['elapsed_ns']=0
        with self.assertRaisesRegex(ValueError,'time order'):calibrate(rows,['0'],{},config_check({}))

    def test_low_speed_and_sharp_turns_excluded(self):
        rows=straight_laps()
        for r in rows['0']:r['speed_kmh']=49
        with self.assertRaisesRegex(ValueError,'Insufficient'):calibrate(rows,list(rows),{},config_check({}))

    def test_config_rejects_nonfinite_unknown_and_invalid_integer(self):
        for values in ({'poll_hz':math.nan},{'poll_hz':True},{'acquisition_samples':3.1},{'curvature_offsets_m':[0,20,10]},
                       {'curvature_offsets_m':[0,math.inf]},{'queue_capacity':0},{'unknown':1}):
            with self.subTest(values=values),self.assertRaises(ValueError):config_check(values)


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        source=cls.root/'source';source.mkdir();make_source(source)
        metadata=read_json(source/'metadata.json')
        metadata['automatic_identity']=dict(car='synthetic',track='circle',sm_version='test',game_version='test')
        (source/'metadata.json').write_text(json.dumps(metadata),encoding='utf-8')
        with (source/'telemetry.csv').open(newline='') as f:rows=list(csv.DictReader(f))
        for row in rows:row['heading_rad']=float(row['progress'])*2*math.pi
        (source/'telemetry.csv').unlink();csv_write(source/'telemetry.csv',REQUIRED,rows)
        dataset=build_dataset(source,dict(output_dir=str(cls.root/'datasets')))
        review=scan(dataset,dict(output_dir=str(cls.root/'reviews')))
        decisions=read_json(review/'decisions.template.json')
        for lap in decisions['laps']:
            lap.update(status='eligible',full_lap_reviewed=True,reason='Synthetic circle',reviewer='test',reviewed_at=datetime.now(timezone.utc).isoformat())
        path=cls.root/'decisions.json';json_write(path,decisions)
        cls.final=finalize(review,path)
        cls.track=build_track(cls.final,dict(output_dir=str(cls.root/'models')))
        cls.config=dict(output_dir=str(cls.root/'runs'),heading_max_turn_deg=20.0,heading_min_windows=30,max_age_ms=250.0)
        cls.binary=Path('target/debug/poleposition-state.exe').resolve()

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def prepared(self):return prepare(self.track,self.final,self.config)

    def replay(self,prepared):
        if not self.binary.exists():self.skipTest('Build Rust debug CLI for cross-language integration tests')
        result=subprocess.run([str(self.binary),'replay','--prepared',str(prepared)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        return Path(result.stdout.strip())

    def test_prepare_repeat_and_input_hashes(self):
        before=snapshot([p for p in self.root.rglob('*') if p.is_file()])
        a,b=self.prepared(),self.prepared()
        for name in ('config.json','heading_profile.json','replay.json'):self.assertEqual((a/name).read_bytes(),(b/name).read_bytes())
        self.assertEqual(before,snapshot([Path(p) for p in before]))

    def test_output_collision(self):
        prepare(self.track,self.final,self.config,output_id='collision')
        with self.assertRaises(FileExistsError):prepare(self.track,self.final,self.config,output_id='collision')

    def test_storage_failure_leaves_incomplete(self):
        from . import prepare as module
        original=module.json_write
        def fail(path,data):
            if path.name=='heading_profile.json':raise OSError('injected storage failure')
            original(path,data)
        with patch.object(module,'json_write',side_effect=fail),self.assertRaises(OSError):
            prepare(self.track,self.final,self.config,output_id='storage-failure')
        meta=read_json(self.root/'runs/storage-failure/manifest.json')
        self.assertFalse(meta['complete']);self.assertIn('storage failure',meta['error'])

    def test_rust_replay_matches_loader_and_is_reproducible(self):
        prepared=self.prepared();first=self.replay(prepared);second=self.replay(prepared)
        self.assertEqual((first/'states.csv').read_bytes(),(second/'states.csv').read_bytes())
        self.assertEqual((first/'events.csv').read_bytes(),(second/'events.csv').read_bytes())
        model=load_reference(self.track);meta=read_json(first/'manifest.json')
        self.assertEqual(meta['track_points'],len(model['rows']))
        self.assertAlmostEqual(meta['track_length_m'],model['manifest']['total_length_m'],places=10)
        report(first);text=(first/'report.html').read_text(encoding='utf-8')
        svgs=re.findall(r'<svg\b.*?</svg>',text,re.S);self.assertTrue(svgs)
        for svg in svgs:ET.fromstring(svg)
        for href in re.findall(r'href="([^"]+)"',text):self.assertTrue((first/href).exists())
        load_run(first)

    def test_profile_tampering_rejected_by_rust(self):
        if not self.binary.exists():self.skipTest('Build Rust CLI')
        prepared=self.prepared();path=prepared/'heading_profile.json';path.write_bytes(path.read_bytes()+b'\n')
        result=subprocess.run([str(self.binary),'replay','--prepared',str(prepared)],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0);self.assertIn('Input changed',result.stderr)

    def test_incomplete_prepare_rejected(self):
        if not self.binary.exists():self.skipTest('Build Rust CLI')
        prepared=self.prepared();meta=read_json(prepared/'manifest.json');meta['complete']=False
        (prepared/'manifest.json').write_text(json.dumps(meta),encoding='utf-8')
        result=subprocess.run([str(self.binary),'replay','--prepared',str(prepared)],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0);self.assertIn('Incomplete',result.stderr)

    def test_missing_input_inventory_rejected(self):
        if not self.binary.exists():self.skipTest('Build Rust CLI')
        prepared=self.prepared();meta=read_json(prepared/'manifest.json')
        del meta['input_sha256'][str(self.track/'reference.csv')]
        (prepared/'manifest.json').write_text(json.dumps(meta),encoding='utf-8')
        result=subprocess.run([str(self.binary),'replay','--prepared',str(prepared)],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0);self.assertIn('absent from hash inventory',result.stderr)

    def test_rehashed_inconsistent_heading_transform_rejected(self):
        if not self.binary.exists():self.skipTest('Build Rust CLI')
        prepared=self.prepared();path=prepared/'heading_profile.json';profile=read_json(path)
        profile['sign']=-1;path.write_text(json.dumps(profile),encoding='utf-8')
        meta=read_json(prepared/'manifest.json');meta['output_sha256']['heading_profile.json']=digest(path)
        (prepared/'manifest.json').write_text(json.dumps(meta),encoding='utf-8')
        result=subprocess.run([str(self.binary),'replay','--prepared',str(prepared)],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0);self.assertIn('selected candidate',result.stderr)

    def test_report_detects_changed_run(self):
        run=self.replay(self.prepared());path=run/'states.csv';path.write_bytes(path.read_bytes()+b'\n')
        with self.assertRaisesRegex(ValueError,'Input changed'):report(run)


if __name__=='__main__':unittest.main()
