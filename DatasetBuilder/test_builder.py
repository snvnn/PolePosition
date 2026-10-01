import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from .builder import build, hashes, split_laps, check_config, quality
from .report import envelope

# Reuse the recorder's real on-disk fixture and validator regression suite.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'DrivingRecorder'))
import test_validate_recording as recorder_tests


class BuilderTests(recorder_tests.ValidationTests):
    def setUp(self):
        super().setUp()
        self.outputs = tempfile.TemporaryDirectory()
        self.config = dict(output_dir=self.outputs.name, max_gap_ms=20000)

    def tearDown(self):
        self.outputs.cleanup()
        super().tearDown()

    def run_build(self, name='build'):
        self.save()
        return build(self.root, self.config, build_id=name)

    def test_provenance_half_open_and_reproducibility(self):
        self.save(); before = hashes(self.root)
        a = build(self.root, self.config, build_id='a')
        b = build(self.root, self.config, build_id='b')
        self.assertEqual(before, hashes(self.root))
        for file in ['laps.csv','excluded_intervals.csv','splits.json']:
            self.assertEqual((a/file).read_bytes(), (b/file).read_bytes())
        la = next((a/'laps').glob('*.csv'))
        with la.open(newline='') as f: rows = list(csv.DictReader(f))
        self.assertEqual([r['sequence'] for r in rows], ['2','3'])
        self.assertEqual(rows[0]['lap_elapsed_ns'], '0')
        self.assertEqual(la.read_bytes(), (b/'laps'/la.name).read_bytes())
        m = json.loads((a/'manifest.json').read_text())
        self.assertTrue(m['complete']); self.assertFalse(m['split_complete'])

    def test_boundary_validity_belongs_to_next_lap(self):
        self.rows[-1]['lap_valid'] = 'false'
        out = self.run_build()
        self.assertEqual(json.loads((out/'manifest.json').read_text())['accepted_laps'], 1)

    def test_quality_rejections(self):
        for name, change, reason in [('invalid',('lap_valid','false'),'invalid_lap'),('pit',('in_pit','true'),'in_pit')]:
            self.rows[2][change[0]]=change[1]
            out=self.run_build(name)
            with (out/'laps.csv').open() as f: lap=next(csv.DictReader(f))
            self.assertIn(reason,lap['reasons'])
            self.rows[2][change[0]]='true' if change[0]=='lap_valid' else 'false'
        self.config['max_gap_ms']=100
        out=self.run_build('gap')
        self.assertEqual(json.loads((out/'manifest.json').read_text())['accepted_laps'],0)

    def test_bad_source_leaves_incomplete_manifest(self):
        self.metadata['schema_version']=2
        with self.assertRaises(ValueError): self.run_build()
        m=json.loads((Path(self.outputs.name)/'build/manifest.json').read_text())
        self.assertFalse(m['complete']); self.assertIn('error',m)

    def test_drops_rejected(self):
        self.summary.update(received_samples=5,dropped_samples=1)
        with self.assertRaises(ValueError): self.run_build()

    def test_collision_preserves_existing(self):
        out=self.run_build()
        before=(out/'manifest.json').read_bytes()
        with self.assertRaises(FileExistsError): self.run_build()
        self.assertEqual(before,(out/'manifest.json').read_bytes())

    def test_storage_failure(self):
        self.save()
        with patch('DatasetBuilder.builder.csv_write',side_effect=OSError('disk full')):
            with self.assertRaises(OSError): build(self.root,self.config,build_id='failed')
        self.assertFalse(json.loads((Path(self.outputs.name)/'failed/manifest.json').read_text())['complete'])

    def test_source_changed(self):
        self.save(); original=hashes(self.root)
        with patch('DatasetBuilder.builder.hashes',side_effect=[original,{}]):
            with self.assertRaisesRegex(ValueError,'Source changed'): build(self.root,self.config)

    def test_interruption_and_counter_reset_never_join_segments(self):
        # The second crossing follows a pause/reset, so it cannot close the old lap.
        for row in self.rows[2:]:
            row['segment_id'] = 2
            row['completed_laps'] -= 1
        self.events.insert(2,(30_000_000_000,2,'segment_start','paused'))
        self.events[-1]=(40_000_000_000,2,'lap_boundary','completed_laps=1')
        self.summary['written_events']=4
        out=self.run_build()
        self.assertEqual(json.loads((out/'manifest.json').read_text())['complete_laps'],0)
        with (out/'excluded_intervals.csv').open() as f: excluded=list(csv.DictReader(f))
        self.assertEqual(sum(int(e['samples']) for e in excluded),4)

    def test_timer_reset_before_counter_change(self):
        # Insert a sample just before the final counter change; the timer has already reset.
        self.rows[2]['lap_time_ms']=10000
        early=self.rows[-1].copy()
        early.update(sequence=4,completed_laps=1,elapsed_ns=39_990_000_000,
                     received_unix_ns=39_990_000_000,interval_ns=9_990_000_000,
                     physics_changed_ns=39_990_000_000,graphics_changed_ns=39_990_000_000)
        self.rows.insert(3,early)
        self.rows[-1].update(sequence=5,physics_packet_id=5,graphics_packet_id=5,interval_ns=10_000_000)
        self.events[-1]=(39_990_000_000,1,'lap_boundary','timer_and_progress')
        self.summary.update(received_samples=5,written_samples=5)
        out=self.run_build()
        with (out/'laps.csv').open() as f: lap=next(csv.DictReader(f))
        self.assertEqual(lap['end_elapsed_ns'],'39990000000')
        self.assertEqual(lap['samples'],'2')

    def test_failed_shutdown_and_event_drops(self):
        self.summary['normal_shutdown']=False
        with self.assertRaises(ValueError): self.run_build('shutdown')
        self.summary.update(normal_shutdown=True,dropped_events=1)
        with self.assertRaises(ValueError): self.run_build('events')

    def test_unsafe_source_id(self):
        self.metadata['session_id']=self.summary['session_id']='../escape'
        with self.assertRaises(ValueError): self.run_build()


class PureTests(unittest.TestCase):
    def test_splits(self):
        laps=[dict(lap_id=str(i),accepted=True) for i in range(11)]
        s=split_laps(laps,check_config({}))
        self.assertEqual([len(v) for v in s.values()],[7,1,3])
        self.assertEqual(len(set(sum(s.values(),[]))),11)
        for n in (0,1,2): self.assertFalse(any(split_laps(laps[:n],check_config({})).values()))
        self.assertEqual([len(v) for v in split_laps(laps[:3],check_config({})).values()],[1,1,1])

    def test_config(self):
        for c in ({'max_gap_ms':float('nan')},{'plot_buckets':0},{'train_ratio':.9,'validation_ratio':.2},{'unknown':1}):
            with self.assertRaises(ValueError): check_config(c)

    def test_envelope_preserves_spikes(self):
        points=[(i,100 if i==55 else -100 if i==56 else 0) for i in range(1000)]
        reduced=envelope(points,10)
        self.assertIn((55,100),reduced); self.assertIn((56,-100),reduced)
        self.assertEqual(reduced,sorted(reduced))


if __name__=='__main__': unittest.main()
