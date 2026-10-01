import copy
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from DatasetBuilder.builder import REQUIRED, build as build_dataset, csv_write, json_write
from DatasetBuilder.review import scan, finalize, read_json, digest, snapshot
from .config import check_config
from .geometry import generate, path_rows, resample, FIELDS
from .inputs import load_review, select_lap
from .model import build, load_reference


def circle(radius=50, count=720, irregular=False):
    return [(radius*math.cos(2*math.pi*(i/count)**(1.15 if irregular else 1)),
             3*math.sin(2*math.pi*i/count),
             radius*math.sin(2*math.pi*(i/count)**(1.15 if irregular else 1))) for i in range(count)]


class GeometryTests(unittest.TestCase):
    def setUp(self): self.config=check_config({})

    def test_circle_length_tangents_curvature_and_seam(self):
        result=generate(circle(),self.config)
        self.assertTrue(result['validation']['passed'],result['validation'])
        self.assertAlmostEqual(result['length_m']/(100*math.pi),1,delta=.0001)
        rows=result['rows'];self.assertEqual(rows[0]['s_m'],0)
        for row in rows:
            self.assertAlmostEqual(row['curvature_per_m'],.02,delta=.00005)
            self.assertAlmostEqual(math.hypot(row['tangent_x'],row['tangent_z']),1,places=12)
            radius=math.hypot(row['position_x'],row['position_z'])
            self.assertAlmostEqual(row['tangent_x'],-row['position_z']/radius,delta=.0001)
            self.assertAlmostEqual(row['tangent_z'],row['position_x']/radius,delta=.0001)
        self.assertTrue(all(b['s_m']>a['s_m'] for a,b in zip(rows,rows[1:])))
        self.assertLess(rows[-1]['s_m'],result['length_m'])
        self.assertLess(result['validation']['metrics']['seam_tangent_step_degrees'],2)

    def test_reverse_circle_sign(self):
        result=generate(list(reversed(circle())),self.config)
        self.assertTrue(result['validation']['passed'])
        for row in result['rows']:self.assertAlmostEqual(row['curvature_per_m'],-.02,delta=.00005)

    def test_irregular_spacing_and_duplicate_coordinates(self):
        raw=circle(irregular=True)
        baseline=generate(raw,self.config)
        duplicates=[p for p in raw for _ in range(2)]+[raw[0]]
        actual=generate(duplicates,self.config)
        self.assertEqual(actual['rows'],baseline['rows'])
        self.assertTrue(actual['validation']['passed'])
        self.assertEqual(actual['validation']['metrics']['deduplicated_points'],len(raw))

    def test_nonfinite_short_and_large_closure(self):
        for raw in ([(0,0,0)]*20,[(0,0,0),(math.nan,0,1),(1,0,1)],
                    [(0,0,0),(1,math.inf,0),(1,0,1)],circle()[:300]):
            with self.subTest(raw=raw[:3]),self.assertRaises(ValueError):generate(raw,self.config)

    def test_degenerate_tangent_is_rejected(self):
        points=[(i,0,0) for i in range(10)]+[(i,0,0) for i in range(10,0,-1)]
        with self.assertRaisesRegex(ValueError,'Degenerate'):path_rows(points,5)

    def test_near_parallel_sections_keep_source_order(self):
        # Stadium: nearby parallel straights separated by 20m and rounded ends.
        raw=[(i*.25,0,0) for i in range(400)]
        raw += [(100+10*math.cos(-math.pi/2+i*math.pi/200),0,10+10*math.sin(-math.pi/2+i*math.pi/200)) for i in range(200)]
        raw += [(100-i*.25,0,20) for i in range(400)]
        raw += [(10*math.cos(math.pi/2+i*math.pi/200),0,10+10*math.sin(math.pi/2+i*math.pi/200)) for i in range(200)]
        result=generate(raw,self.config)
        self.assertTrue(result['validation']['passed'],result['validation'])
        rows=result['rows']
        lower=[r for r in rows if 20<r['position_x']<80 and abs(r['position_z'])<.1]
        upper=[r for r in rows if 20<r['position_x']<80 and abs(r['position_z']-20)<.1]
        self.assertGreater(len(lower),50);self.assertGreater(len(upper),50)
        self.assertLess(lower[-1]['index'],upper[0]['index'])
        self.assertTrue(all(r['tangent_x']>.99 for r in lower))
        self.assertTrue(all(r['tangent_x']<-.99 for r in upper))

    def test_quality_thresholds_fail_without_retuning(self):
        for key in ('max_smoothing_displacement_m','max_length_change_ratio',
                    'max_spacing_error_ratio','max_tangent_step_degrees'):
            config=self.config|{key:1e-12}
            with self.subTest(key=key):
                result=generate(circle(),config)
                self.assertFalse(result['validation']['passed'])
                self.assertTrue(result['validation']['errors'])
                self.assertEqual(config[key],1e-12)

    def test_config_and_resampling_size_limits(self):
        for values in ({'unknown':1},{'spacing_m':0},{'spacing_m':True},{'spacing_m':float('nan')},
                       {'smoothing_window':10},{'tangent_span':1.5},{'output_dir':''},
                       {'max_spacing_error_ratio':1},{'max_tangent_step_degrees':180}):
            with self.subTest(values=values),self.assertRaises(ValueError):check_config(values)
        with self.assertRaises(ValueError):resample(circle(),.00001)
        with self.assertRaises(ValueError):resample(circle(),1e-310)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.laps=[dict(lap_id=str(i),ordinal=i,split='train',accepted=True,
                       start_elapsed_ns=i*1000,end_elapsed_ns=i*1000+duration)
                   for i,duration in enumerate((110,90,90,50))]
        self.laps[-1]['split']='test'

    def test_fastest_tie_and_source_order(self):
        selected,candidates=select_lap(self.laps,['2','0','1'])
        self.assertEqual(selected['lap_id'],'1')
        self.assertEqual([l['lap_id'] for l in candidates],['1','2','0'])

    def test_no_candidates_unknown_and_holdout(self):
        for candidates in ([],['unknown'],['3'],['0','0']):
            with self.subTest(candidates=candidates),self.assertRaises(ValueError):select_lap(self.laps,candidates)


def make_source(folder):
    rows=[]
    # Eight finish crossings, seven full laps, identical circles, default 100ms gaps.
    for i in range(481):
        phase=(i%60)/60;elapsed=i*100_000_000
        row=dict.fromkeys(REQUIRED,0)
        row.update(sequence=i+1,segment_id=1,elapsed_ns=elapsed,received_unix_ns=elapsed,
                   interval_ns=100_000_000 if i else 0,physics_changed_ns=elapsed,graphics_changed_ns=elapsed,
                   physics_packet_id=i,graphics_packet_id=i,speed_kmh=113,progress=phase,
                   position_x=30*math.cos(2*math.pi*phase),position_z=30*math.sin(2*math.pi*phase),
                   completed_laps=i//60,lap_time_ms=(i%60)*100,sector=int(phase*3),lap_valid='true',in_pit='false')
        rows.append(row)
    events=[dict(elapsed_ns=0,segment_id=1,kind='segment_start',detail='start')]
    events += [dict(elapsed_ns=t*1_000_000_000,segment_id=1,kind='lap_boundary',detail='lap') for t in range(6,49,6)]
    json_write(folder/'metadata.json',dict(schema_version=1,session_id='track-test',config=dict(stale_seconds=2)))
    json_write(folder/'summary.json',dict(schema_version=1,session_id='track-test',normal_shutdown=True,
               received_samples=len(rows),written_samples=len(rows),dropped_samples=0,dropped_events=0,
               written_events=len(events),read_timing={},poll_timing={}))
    csv_write(folder/'telemetry.csv',REQUIRED,rows)
    csv_write(folder/'events.csv',['elapsed_ns','segment_id','kind','detail'],events)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'source';self.source.mkdir();make_source(self.source)
        self.dataset=build_dataset(self.source,dict(output_dir=str(self.root/'datasets')))
        self.scan=scan(self.dataset,dict(output_dir=str(self.root/'reviews')))
        decisions=read_json(self.scan/'decisions.template.json')
        for lap in decisions['laps']:
            lap.update(status='eligible',full_lap_reviewed=True,reason='Synthetic circle inspected',
                       reviewer='test',reviewed_at=datetime.now(timezone.utc).isoformat())
            self.assertFalse(lap['candidates'])
        self.decisions=self.root/'decisions.json';json_write(self.decisions,decisions)
        self.final=finalize(self.scan,self.decisions)
        self.config=dict(output_dir=str(self.root/'models'))

    def tearDown(self):self.temp.cleanup()

    def test_roundtrip_reproducible_and_report(self):
        before=snapshot([p for p in self.root.rglob('*') if p.is_file()])
        first=build(self.final,self.config);second=build(self.final,self.config)
        loaded=load_reference(first)
        self.assertEqual(loaded['manifest']['selected_lap']['ordinal'],1)
        self.assertEqual(loaded['manifest']['points'],len(loaded['rows']))
        for name in ('reference.csv','validation.json','report.html'):
            self.assertEqual((first/name).read_bytes(),(second/name).read_bytes())
        self.assertEqual(before,snapshot([Path(p) for p in before]))
        report=(first/'report.html').read_text(encoding='utf-8')
        svgs=re.findall(r'<svg\b.*?</svg>',report,re.S);self.assertEqual(len(svgs),6)
        for svg in svgs:ET.fromstring(svg)
        for href in re.findall(r'href="([^"]+)"',report):self.assertTrue((first/href).exists())

    def test_usage_tampering(self):
        path=self.final/'usage.json';usage=read_json(path)
        usage['trackmodel_candidates'].append(usage['eligible']['test'][0])
        path.write_text(json.dumps(usage),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'Usage list'):build(self.final,self.config,output_id='bad-usage')
        self.assertFalse(read_json(self.root/'models/bad-usage/manifest.json')['complete'])

    def test_final_decision_copy_tampering(self):
        path=self.final/'decisions.json';decisions=read_json(path);decisions['laps'][0]['status']='hold'
        path.write_text(json.dumps(decisions),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'decision copy'):load_review(self.final)

    def test_hold_fastest_and_no_candidates(self):
        decisions=read_json(self.decisions);train=set(read_json(self.final/'usage.json')['eligible']['train'])
        first=next(lap for lap in decisions['laps'] if lap['lap_id'] in train);first['status']='hold'
        # Preserve the earlier review and its decision file.
        changed=self.root/'changed.json';json_write(changed,decisions)
        final=finalize(self.scan,changed)
        model=load_reference(build(final,self.config))
        self.assertEqual(model['manifest']['selected_lap']['ordinal'],2)
        for lap in decisions['laps']:
            if lap['lap_id'] in train:lap['status']='hold'
        none=self.root/'none.json';json_write(none,decisions)
        empty=finalize(self.scan,none)
        with self.assertRaisesRegex(ValueError,'No eligible train'):build(empty,self.config)

    def test_source_and_scan_tampering(self):
        for path in (self.source/'telemetry.csv',self.scan/'findings.json'):
            original=path.read_bytes();path.write_bytes(original+b'\n')
            with self.subTest(path=path),self.assertRaisesRegex(ValueError,'Input changed'):load_review(self.final)
            path.write_bytes(original)

    def test_input_changed_during_build(self):
        from .model import render
        def change(*args):
            with (self.source/'events.csv').open('a') as stream:stream.write('\n')
            return render(*args)
        with patch('TrackModel.model.render',side_effect=change):
            with self.assertRaisesRegex(ValueError,'Input changed'):build(self.final,self.config,output_id='changing')
        self.assertFalse(read_json(self.root/'models/changing/manifest.json')['complete'])

    def test_quality_failure_keeps_selected_lap_and_diagnostics(self):
        config=self.config|dict(max_smoothing_displacement_m=1e-12)
        with self.assertRaisesRegex(ValueError,'no fallback'):build(self.final,config,output_id='quality-failed')
        folder=self.root/'models/quality-failed';manifest=read_json(folder/'manifest.json')
        self.assertEqual(manifest['selected_lap']['ordinal'],1)
        self.assertFalse(manifest['complete']);self.assertTrue((folder/'report.html').exists())
        self.assertFalse(read_json(folder/'validation.json')['passed'])
        with self.assertRaises(ValueError):load_reference(folder)

    def test_output_collision_and_protected_directories(self):
        first=build(self.final,self.config,output_id='fixed');before=digest(first/'manifest.json')
        with self.assertRaises(FileExistsError):build(self.final,self.config,output_id='fixed')
        self.assertEqual(before,digest(first/'manifest.json'))
        for folder in (self.source,self.dataset,self.scan,self.final):
            with self.subTest(folder=folder),self.assertRaises(ValueError):build(self.final,dict(output_dir=str(folder)))

    def test_storage_failure_leaves_incomplete(self):
        with patch('TrackModel.model.csv_write',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):build(self.final,self.config,output_id='disk-failed')
        folder=self.root/'models/disk-failed'
        self.assertFalse(read_json(folder/'manifest.json')['complete'])
        self.assertIn('disk full',read_json(folder/'manifest.json')['error'])

    def test_loader_rejects_hash_header_nonfinite_and_incomplete(self):
        model=build(self.final,self.config);original=(model/'reference.csv').read_bytes()
        with (model/'reference.csv').open('a') as stream:stream.write('\n')
        with self.assertRaisesRegex(ValueError,'Input changed'):load_reference(model)
        (model/'reference.csv').write_bytes(original)
        initial=read_json(model/'manifest.json')
        for body in (original.replace(b'curvature_per_m',b'wrong_field'),original.replace(b',0.0,',b',nan,',1)):
            (model/'reference.csv').write_bytes(body)
            manifest=copy.deepcopy(initial);manifest['output_sha256']['reference.csv']=digest(model/'reference.csv')
            (model/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
            with self.assertRaises(ValueError):load_reference(model)
        (model/'reference.csv').write_bytes(original)
        for change in (dict(complete=False),dict(schema_version=2)):
            (model/'manifest.json').write_text(json.dumps(initial|change),encoding='utf-8')
            with self.assertRaises(ValueError):load_reference(model)


if __name__ == '__main__':unittest.main()
