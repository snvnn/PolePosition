import copy
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .builder import REQUIRED, build, csv_write
from .review import config_check, load_dataset, scan, finalize, validate_decisions, read_json, digest
from .review_analysis import baseline, detect, reference_models, representatives


def signal_rows(seconds=3, dt=.05, **changes):
    rows = []
    for i in range(round(seconds/dt)+1):
        t=i*dt
        row=dict(sequence=i+1,segment_id=1,elapsed_ns=round(t*1e9),progress=.2+t*.01,
                 position_x=t*10,position_z=0,speed_kmh=36,steering_raw=0,throttle=.5,brake=0)
        row.update({k:(v(i,t) if callable(v) else v) for k,v in changes.items()})
        rows.append(row)
    return rows


def own_reference(rows, config):
    profile=representatives(rows,config['progress_bins'])
    return baseline({str(i):profile for i in range(3)},['0','1','2'],config)


class AnalysisTests(unittest.TestCase):
    def setUp(self): self.config=config_check({})

    def reasons(self, rows, ref=None):
        ref=own_reference(rows,self.config) if ref is None else ref
        return {r for c in detect(rows,ref,self.config) for r in c['reasons']}

    def test_normal_input_changes_and_wrap(self):
        rows=signal_rows(steering_raw=lambda i,t:.6 if i%2 else -.6)
        # Alternating input can differ from its bin median; it remains a review
        # candidate, not an automatic crash/exclusion decision.
        self.assertLessEqual(self.reasons(rows), {'steering_difference'})
        rows=signal_rows(progress=lambda i,t:(.995+t*.01)%1,
                         fuel_litres=lambda i,t:10 if t>=.5 else 4,lap_time_ms=lambda i,t:0 if t>=.5 else 80000)
        self.assertEqual(self.reasons(rows),set())

    def test_relative_duration_and_all_three_metrics(self):
        normal=signal_rows();ref=own_reference(normal,self.config)
        rows=copy.deepcopy(normal)
        for r in rows[10:17]:r.update(position_z=7,speed_kmh=65,steering_raw=.6)
        self.assertTrue({'trajectory_difference','speed_difference','steering_difference'}<=self.reasons(rows,ref))
        rows=copy.deepcopy(normal)
        for r in rows[10:13]:r['steering_raw']=.7
        self.assertNotIn('steering_difference',self.reasons(rows,ref))

    def test_low_speed_reverse_jump(self):
        self.assertIn('low_speed',self.reasons(signal_rows(speed_kmh=10,position_x=lambda i,t:t*2)))
        self.assertIn('reverse_progress',self.reasons(signal_rows(progress=lambda i,t:.5-t*.03)))
        self.assertIn('position_discontinuity',self.reasons(signal_rows(position_x=lambda i,t:t*10+(30 if t>=1 else 0))))

    def test_merge_preserves_reasons_and_gap(self):
        normal=signal_rows();ref=own_reference(normal,self.config)
        rows=copy.deepcopy(normal)
        for r in rows[4:11]:r['steering_raw']=.6
        for r in rows[15:22]:r['speed_kmh']=70
        groups=detect(rows,ref,self.config)
        relative=[g for g in groups if any(r.endswith('difference') for r in g['reasons'])]
        self.assertEqual(len(relative),1)
        self.assertEqual(relative[0]['reasons'],['speed_difference','steering_difference'])
        config=self.config|{'merge_gap_seconds':.1}
        self.assertEqual(len(detect(rows,ref,config)),2)

    def test_no_cross_segment_and_short_window(self):
        rows=signal_rows(seconds=.05)
        rows[-1]['position_x']=100
        self.assertNotIn('position_discontinuity',self.reasons(rows))
        rows[-1]['segment_id']=2
        with self.assertRaises(ValueError):self.reasons(rows)

    def test_missing_reference_and_zero_mad(self):
        rows=signal_rows()
        self.assertIn('insufficient_reference',self.reasons(rows,{}))
        ref=own_reference(rows,self.config)
        self.assertEqual(next(iter(ref.values()))['speed_threshold_kmh'],20)
        self.assertEqual(next(iter(ref.values()))['trajectory_threshold_m'],5)

    def test_each_lap_one_vote_and_holdout_isolation(self):
        rows={str(i):signal_rows(speed_kmh=30+i) for i in range(7)}
        models=reference_models(rows,['0','1','2','3'],self.config)
        rows['5']=signal_rows(speed_kmh=10000)
        rows['6']=signal_rows(position_z=10000)
        self.assertEqual(models,reference_models(rows,['0','1','2','3'],self.config))
        bin_id=next(iter(models['global_baseline']))
        self.assertEqual(models['global_baseline'][bin_id]['speed_kmh'],31.5)
        self.assertEqual(models['leave_one_out']['0'][bin_id]['speed_kmh'],32)
        rows['0']=rows['0']*5
        self.assertEqual(models,reference_models(rows,['0','1','2','3'],self.config))


def make_source(folder):
    rows=[]
    # Eight crossings create seven complete laps: train 4 / validation 1 / test 2.
    for i in range(97):
        t=i*.5;local=t%6
        row=dict.fromkeys(REQUIRED,0)
        row.update(sequence=i+1,segment_id=1,elapsed_ns=round(t*1e9),received_unix_ns=round(t*1e9),
                   interval_ns=500_000_000 if i else 0,physics_changed_ns=round(t*1e9),graphics_changed_ns=round(t*1e9),
                   physics_packet_id=i,graphics_packet_id=i,speed_kmh=36,progress=local/6,
                   position_x=local*10,completed_laps=int(t//6),lap_time_ms=int(local*1000),
                   sector=int(local//2),lap_valid='true',in_pit='false')
        rows.append(row)
    metadata=dict(schema_version=1,session_id='test',config=dict(stale_seconds=2))
    events=[dict(elapsed_ns=0,segment_id=1,kind='segment_start',detail='start')]
    events.extend(dict(elapsed_ns=t*1_000_000_000,segment_id=1,kind='lap_boundary',detail='lap') for t in range(6,49,6))
    summary=dict(schema_version=1,session_id='test',normal_shutdown=True,received_samples=len(rows),written_samples=len(rows),
                 dropped_samples=0,dropped_events=0,written_events=len(events),read_timing={},poll_timing={})
    for name,obj in [('metadata.json',metadata),('summary.json',summary)]:
        (folder/name).write_text(json.dumps(obj),encoding='utf-8')
    csv_write(folder/'telemetry.csv',REQUIRED,rows)
    csv_write(folder/'events.csv',['elapsed_ns','segment_id','kind','detail'],events)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'source';self.source.mkdir();make_source(self.source)
        self.dataset=build(self.source,dict(output_dir=str(self.root/'datasets'),max_gap_ms=600))
        self.config=config_check(dict(output_dir=str(self.root/'reviews')))

    def tearDown(self):self.temp.cleanup()

    def reviewed(self,review,status='hold'):
        d=read_json(review/'decisions.template.json')
        for lap in d['laps']:
            lap.update(status=status,full_lap_reviewed=True,reason='Insufficient reference; retained on hold',
                       reviewer='test reviewer',reviewed_at=datetime.now(timezone.utc).isoformat())
            for c in lap['candidates']:c.update(resolution='unresolved',reason='Insufficient reference')
        path=self.root/'decisions.json';path.write_text(json.dumps(d),encoding='utf-8')
        return path

    def test_scan_repeat_and_finalize_hold_preserves_splits(self):
        before={p:digest(p) for p in self.dataset.rglob('*') if p.is_file()}
        a=scan(self.dataset,self.config);b=scan(self.dataset,self.config)
        for name in ['findings.json','baselines.json','report.html','decisions.template.json']:
            self.assertEqual((a/name).read_bytes(),(b/name).read_bytes())
        final=finalize(a,self.reviewed(a))
        usage=read_json(final/'usage.json')
        self.assertEqual(usage['trackmodel_candidates'],[]);self.assertFalse(usage['trackmodel_ready'])
        self.assertEqual(before,{p:digest(p) for p in before})

    def test_export_and_split_tampering(self):
        f=next((self.dataset/'laps').glob('*.csv'))
        text=f.read_text();f.write_text(text.replace(',36,',',37,'),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'differs from source'):load_dataset(self.dataset)

    def test_split_tampering(self):
        (self.dataset/'splits.json').write_text('{}')
        with self.assertRaises(ValueError):load_dataset(self.dataset)

    def test_eligible_preserves_holdouts_and_trackmodel_train_only(self):
        review=scan(self.dataset,self.config)
        decisions=self.reviewed(review,'eligible')
        d=read_json(decisions)
        for lap in d['laps']:
            lap['reason']='Full lap examined; no blocking issue in this fixture'
            for c in lap['candidates']:c.update(resolution='resolved',reason='Reviewed')
        decisions.write_text(json.dumps(d),encoding='utf-8')
        result=finalize(review,decisions)
        usage=read_json(result/'usage.json');splits=read_json(self.dataset/'splits.json')
        self.assertEqual(usage['eligible'],splits)
        self.assertEqual(usage['trackmodel_candidates'],splits['train'])
        self.assertTrue(usage['trackmodel_ready'])

    def test_scan_artifact_tampering(self):
        review=scan(self.dataset,self.config);decisions=self.reviewed(review)
        with (review/'findings.json').open('a') as stream:stream.write(' ')
        with self.assertRaisesRegex(ValueError,'Input changed'):finalize(review,decisions)

    def test_finalize_rejects_unreviewed_and_changed_input(self):
        review=scan(self.dataset,self.config)
        with self.assertRaises(ValueError):finalize(review,review/'decisions.template.json')
        decisions=self.reviewed(review)
        with (self.source/'events.csv').open('a') as f:f.write('\n')
        with self.assertRaisesRegex(ValueError,'Input changed'):finalize(review,decisions)

    def test_incomplete_and_collision_and_storage_failure(self):
        review=scan(self.dataset,self.config,output_id='fixed')
        before=digest(review/'manifest.json')
        with self.assertRaises(FileExistsError):scan(self.dataset,self.config,output_id='fixed')
        self.assertEqual(before,digest(review/'manifest.json'))
        with patch('DatasetBuilder.review.render_review',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):scan(self.dataset,self.config,output_id='failed')
        self.assertFalse(read_json(self.root/'reviews/failed/manifest.json')['complete'])

    def test_finalize_repeat_collision_and_storage_failure(self):
        review=scan(self.dataset,self.config);decisions=self.reviewed(review)
        first=finalize(review,decisions,output_id='final-fixed')
        second=finalize(review,decisions)
        for name in ('decisions.json','usage.json'):
            self.assertEqual((first/name).read_bytes(),(second/name).read_bytes())
        before=digest(first/'manifest.json')
        with self.assertRaises(FileExistsError):
            finalize(review,decisions,output_id='final-fixed')
        self.assertEqual(before,digest(first/'manifest.json'))
        from .review import json_write
        def fail_usage(path, value):
            if path.name=='usage.json': raise OSError('disk full')
            return json_write(path,value)
        with patch('DatasetBuilder.review.json_write',side_effect=fail_usage):
            with self.assertRaises(OSError):finalize(review,decisions,output_id='final-failed')
        failed=read_json(self.root/'reviews/final-failed/manifest.json')
        self.assertFalse(failed['complete']);self.assertIn('disk full',failed['error'])

    def test_incomplete_or_unsupported_dataset(self):
        path=self.dataset/'manifest.json';original=read_json(path)
        for changed in (dict(complete=False),dict(split_complete=False),dict(schema_version=2)):
            path.write_text(json.dumps(original|changed),encoding='utf-8')
            with self.subTest(changed=changed),self.assertRaises(ValueError):
                load_dataset(self.dataset)


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.findings={'laps':[dict(lap_id='a',split='train',candidates=[dict(candidate_id='c',reasons=['speed_difference'])]),
                               dict(lap_id='b',split='test',candidates=[])]}
        self.decisions=dict(schema_version=1,laps=[dict(lap_id=l['lap_id'],status='eligible',full_lap_reviewed=True,
            reason='Reviewed all evidence',reviewer='test',reviewed_at='2026-09-29T10:00:00+00:00',
            candidates=[dict(candidate_id=c['candidate_id'],resolution='resolved',reason='Explained speed difference') for c in l['candidates']])
            for l in self.findings['laps']])

    def test_valid(self):validate_decisions(self.decisions,self.findings)

    def test_incomplete_duplicate_unknown_ambiguous(self):
        cases=[]
        d=copy.deepcopy(self.decisions);d['laps'].pop();cases.append(d)
        d=copy.deepcopy(self.decisions);d['laps'].append(d['laps'][0]);cases.append(d)
        d=copy.deepcopy(self.decisions);d['laps'][0]['lap_id']='wrong';cases.append(d)
        d=copy.deepcopy(self.decisions);d['laps'][0]['reason']=' ';cases.append(d)
        d=copy.deepcopy(self.decisions);d['laps'][0]['candidates'][0]['resolution']='unresolved';cases.append(d)
        d=copy.deepcopy(self.decisions);d['laps'][0]['candidates']=[];cases.append(d)
        d=copy.deepcopy(self.decisions);d['laps'][0]['full_lap_reviewed']=False;cases.append(d)
        for d in cases:
            with self.subTest(decision=d),self.assertRaises(ValueError):validate_decisions(d,self.findings)

    def test_unsupported_comparison_cannot_be_eligible(self):
        self.findings['laps'][0]['candidates'][0]['reasons']=['insufficient_reference']
        with self.assertRaises(ValueError):validate_decisions(self.decisions,self.findings)

    def test_missing_reviewer_time_and_candidate_reason(self):
        for field,value in (('reviewer',''),('reviewed_at','2026-09-29T10:00:00'),('status','unreviewed')):
            decision=copy.deepcopy(self.decisions);decision['laps'][0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):
                validate_decisions(decision,self.findings)
        decision=copy.deepcopy(self.decisions);decision['laps'][0]['candidates'][0]['reason']=' '
        with self.assertRaises(ValueError):validate_decisions(decision,self.findings)


if __name__=='__main__':unittest.main()
