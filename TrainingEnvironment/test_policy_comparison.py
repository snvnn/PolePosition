from .comparison_fixtures import ComparisonFixture, trial
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from ModelTrainer.common import save
from .policy_comparison import schedule,decision,common_metrics,run,promote,analyze_trial,prepare_focus
from .online_fixtures import OnlineDataFixture
from .gamepad_online_data import collect


class ComparisonTests(ComparisonFixture, unittest.TestCase):
    def test_preparation_honors_f8_before_any_drive(self):
        calls=[]
        def check():
            calls.append(1)
            if len(calls)==4:raise InterruptedError('f8')
        with patch('TrainingEnvironment.policy_comparison.time.sleep'):
            with self.assertRaisesRegex(InterruptedError,'f8'):prepare_focus(check,lambda **kw:None)

    def test_schedule_balanced_and_reproducible(self):
        labels=['initial','best','latest','control','candidate'];s=schedule(labels,4,42)
        self.assertEqual(s,schedule(labels,4,42));self.assertEqual(len(s),20)
        for r in range(4):self.assertEqual({v['model'] for v in s[r*5:r*5+5]},set(labels))
        self.assertNotEqual(s[:5],s[5:10])

    def test_acceptance_requires_all_metrics_and_sufficient_laps(self):
        c=[trial() for _ in range(5)];b=[trial('best',variation=10.,lateral=1.) for _ in range(5)]
        self.assertTrue(decision(c,b)['passed'])
        self.assertFalse(decision(c[:4],b)['passed'])
        for field,value in [('lap_seconds',106.),('progress_m',3999.),('completed',False)]:
            modified=[dict(r,**{field:value}) for r in c]
            self.assertFalse(decision(modified,b)['passed'])
        self.assertFalse(decision([trial(variation=9.) for _ in range(5)],b)['passed'])
        self.assertFalse(decision([trial(lateral=.95) for _ in range(5)],b)['passed'])
        self.assertFalse(decision(c,[trial('best',complete=False) for _ in range(5)])['passed'])
        self.assertFalse(common_metrics(c,[dict(r,bins={'2:5':r['bins']['1:5']}) for r in b])['enough'])

    def test_comparison_record_is_validated_but_cannot_enter_training(self):
        f=OnlineDataFixture()
        with tempfile.TemporaryDirectory() as d:
            folder,model,data,rows,s=f.fixture(d);s['policy_comparison']=True
            s['episode']['progress']=12.;f.store(folder,rows,s)
            m=json.loads((folder/'manifest.json').read_text());m['policy_comparison']=True
            save(folder/'manifest.json',m,replace=True)
            result=analyze_trial(folder,s['model_sha256'])
            self.assertFalse(result['completed']);self.assertTrue(result['evaluation_only'])
            with self.assertRaisesRegex(ValueError,'evaluation-only'):collect(folder,model,data)

    def test_no_promotion_on_failure(self):
        with tempfile.TemporaryDirectory() as d:
            out=Path(d);p=out/'preset.json';save(p,dict(checkpoint='original'))
            self.assertFalse(promote(out,dict(preset=str(p)),dict(passed=False),{},{}))
            self.assertEqual(json.loads(p.read_text())['checkpoint'],'original')


    def test_pair_runs_ten_each_balanced_order_without_promotion(self):
        with tempfile.TemporaryDirectory() as d:
            (out,complete,reason),events=self.orchestration(Path(d),pair=True)
            self.assertTrue(complete);self.assertEqual(reason,'paired_trials_finished')
            drives=[e[2] for e in events if e[0]=='drive']
            self.assertEqual(drives.count('lap'),10);self.assertEqual(drives.count('latest'),10)
            self.assertEqual(drives[::2].count('lap'),5)
            for i in range(0,20,2):self.assertEqual(set(drives[i:i+2]),{'lap','latest'})
            report=json.loads((out/'report.json').read_text())
            self.assertEqual(report['policy_updates'],0);self.assertEqual(report['optimizer_updates'],0)
            self.assertIsNone(report['decision']);self.assertFalse((out/'promotion.json').exists())

    def test_full_comparison_thirty_frozen_trials_and_confirmation(self):
        with tempfile.TemporaryDirectory() as d:
            (out,complete,reason),events=self.orchestration(Path(d))
            self.assertTrue(complete);self.assertEqual(reason,'passed')
            self.assertEqual(sum(e[0]=='drive' for e in events),30)
            report=json.loads((out/'report.json').read_text())
            self.assertEqual(report['policy_updates'],0);self.assertEqual(report['optimizer_updates'],0)
            self.assertEqual(len(report['records']),30)

    def test_deadline_or_reset_error_keeps_incomplete_unpromoted(self):
        for cancel,fail in [(3,False),(None,True)]:
            with tempfile.TemporaryDirectory() as d:
                (out,complete,reason),events=self.orchestration(Path(d),cancel,fail)
                self.assertFalse(complete);self.assertFalse((out/'promotion.json').exists())
                self.assertEqual(events[-1],('close',))

    def test_two_hour_wall_clock_includes_reset_and_prevents_next_drive(self):
        with tempfile.TemporaryDirectory() as d:
            (out,complete,reason),events=self.orchestration(Path(d),deadline_at_reset=True)
            self.assertFalse(complete);self.assertEqual(reason,'session_time_limit')
            self.assertFalse(any(e[0]=='drive' for e in events))
            self.assertEqual(json.loads((out/'report.json').read_text())['elapsed_seconds'],7200.)

    def test_recovery_preserves_schedule_exclusion_and_cannot_qualify_candidate(self):
        with tempfile.TemporaryDirectory() as d:
            (out,complete,reason),events=self.orchestration(Path(d),recover_at=(1,))
            self.assertTrue(complete);self.assertEqual(reason,'not_accepted')
            self.assertEqual(sum(e[0]=='drive' for e in events),30)
            self.assertEqual(events.count(('garage_required',)),1)
            report=json.loads((out/'report.json').read_text())
            self.assertEqual(len(report['records']),30)
            self.assertFalse(report['decision']['checks']['no_unattributed_relocation'])
        with tempfile.TemporaryDirectory() as d:
            (out,complete,reason),events=self.orchestration(Path(d),recover_at=(1,2,3))
            self.assertFalse(complete);self.assertEqual(sum(e[0]=='drive' for e in events),3)

if __name__=='__main__':unittest.main()
