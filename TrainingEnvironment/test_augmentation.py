import unittest
import torch
from .augmentation import invalid_windows, excluded, phase_select, sample_weight, noisy_features

class AugmentationTests(unittest.TestCase):
    def test_exclusions_and_segment_boundaries(self):
        rows=[dict(segment_id='1',elapsed_ns=str(t*10**9),lap_valid=v) for t,v in [(0,'true'),(5,'false'),(9,'false')]]
        windows=invalid_windows(rows)
        self.assertEqual(windows,[('1',3*10**9,6*10**9)])
        self.assertTrue(excluded(dict(segment_id='1',elapsed_ns=6*10**9),windows))
        self.assertFalse(excluded(dict(segment_id='2',elapsed_ns=5*10**9),windows))

    def test_phases_no_duplicates_and_epoch_reset(self):
        rows=[dict(sequence=i,features=[1],tracking_epoch=i//6,observation_ns=i*10**7) for i in range(12)]
        out=list(phase_select(rows,[0,10]))
        self.assertEqual(len(out),12)
        self.assertEqual(len({r['sequence'] for r,_ in out}),12)
        self.assertEqual([r['sequence'] for r,p in out if 0 in p],[0,2,4,6,8,10])

    def test_late_observation_is_not_duplicated_for_two_phases(self):
        rows=[dict(sequence=i,features=[1],tracking_epoch=1,observation_ns=t) for i,t in enumerate([0,35_000_000,40_000_000])]
        out=list(phase_select(rows,[0,10]))
        self.assertEqual([r['sequence'] for r,_ in out],[0,1,2])
        self.assertEqual(out[1][1],[0,10])

    def test_session_overlap_rejected(self):
        import tempfile,json
        from pathlib import Path
        from .session_gamepad import prepare
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'split.json';p.write_text(json.dumps(dict(training_sessions=[tmp],validation_session=tmp)))
            with self.assertRaisesRegex(ValueError,'overlap'):
                prepare({'session_split':str(p)},'unused')

    def test_weights_do_not_stack(self):
        f=[0.]*35;f[8]=.004
        c=dict(brake_threshold=.2,curvature_threshold=.003,important_weight=2.)
        self.assertEqual(sample_weight(f,[0,0,1],c),2)
        f[8]=0
        self.assertEqual(sample_weight(f,[0,1,0],c),1)

    def test_noise_reproducible_bounded_and_local(self):
        x=torch.zeros(1000,35);x[:,6]=1
        c=dict(probability=.5,lateral_std_m=.02,lateral_clip_m=.06,heading_std_rad=.002,heading_clip_rad=.006)
        a=noisy_features(x,torch.Generator().manual_seed(42),c)
        b=noisy_features(x,torch.Generator().manual_seed(42),c)
        self.assertTrue(torch.equal(a,b));self.assertTrue(torch.equal(x[:,4],torch.zeros(1000)))
        self.assertLessEqual(a[:,4].abs().max().item(),.060001)
        self.assertLessEqual(torch.atan2(a[:,5],a[:,6]).abs().max().item(),.006001)
        self.assertTrue(torch.allclose(a[:,5]**2+a[:,6]**2,torch.ones(1000)))
        cols=[i for i in range(35) if i not in (4,5,6)]
        self.assertTrue(torch.equal(a[:,cols],x[:,cols]))

if __name__=='__main__': unittest.main()
