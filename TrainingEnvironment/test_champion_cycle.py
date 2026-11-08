import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
from datetime import datetime,timezone
from ModelTrainer.common import save
from . import champion_cycle as cycle
from .gamepad_online import resolve_resume
from .gamepad_online_data import validate_stop,validate_shutdown
from .online_fixtures import OnlineDataFixture,summary
from .comparison_fixtures import trial,ComparisonFixture
from .policy_comparison import crossed_valid_500


class Control:
    reason=None
    def cancel(self,r):self.reason=self.reason or r
    def stopped(self):return self.reason is not None


class CycleTests(unittest.TestCase):
    def test_deadline_loop_continues_beyond_two_rounds_and_stops_at_wall_deadline(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);out=root/'out';out.mkdir();clock=[1000.];calls=[]
            cfg=dict(schema='gamepad_champion_deadline_v1',deadline=datetime.fromtimestamp(10000,timezone.utc).isoformat(),
                training_seconds=3600,comparison_seconds=2700,seed=42,training_config='fixture',
                incumbent=dict(path='champion',model_sha256='old'))
            path=root/'config.json';save(path,cfg)
            def train(args,child,emit):
                calls.append(args);clock[0]+=3600;child.cancel('session_time_limit')
                return root,False,'session_time_limit'
            with patch.object(cycle,'verify_models'),patch.object(cycle,'new_folder',return_value=out),\
                 patch.object(cycle,'train_run',train),patch.object(cycle,'verify_boundary',return_value={}),\
                 patch.object(cycle,'candidates',return_value=[]),patch.object(cycle,'f8_down',return_value=False),\
                 patch.object(cycle.time,'time',side_effect=lambda:clock[0]),\
                 patch.object(cycle.time,'monotonic',side_effect=lambda:clock[0]):
                _,complete,reason=cycle.run(SimpleNamespace(config=str(path),seconds=10000),Control(),lambda *a,**k:None)
            self.assertFalse(complete);self.assertEqual(reason,'deadline_reached');self.assertEqual(len(calls),3)

    def test_deadline_validation_rejects_naive_past_and_unbounded(self):
        cfg=dict(schema='gamepad_champion_deadline_v1',training_seconds=3600,comparison_seconds=2700,incumbent={})
        for deadline in ('2026-11-08T22:00:00','2000-01-01T22:00:00+09:00','2100-01-01T22:00:00+09:00'):
            with self.assertRaises(ValueError):cycle.validate_config(dict(cfg,deadline=deadline),43200)

    def test_pending_request_cannot_reuse_previous_stop_proof(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);save(root/'request-1.json',{});save(root/'response-1.json',{})
            save(root/'request-2.json',{})
            with self.assertRaisesRegex(ValueError,'Unfinished native request'):cycle.verify_boundary(root)

    def test_release_requires_confirmed_process_exit_not_missing_telemetry(self):
        from .game_process import release_after_game_exit
        with tempfile.TemporaryDirectory() as d:
            game=Mock(pids=[123]);game.exited.return_value=False;job=Mock();job.process.poll.return_value=None
            parent=Control();child=cycle.PhaseControl(parent)
            self.assertFalse(release_after_game_exit(game,job,child,Path(d)));job.process.terminate.assert_not_called()
            child.cancel('session_time_limit');game.exited.return_value=True
            self.assertTrue(release_after_game_exit(game,job,child,Path(d)))
            self.assertEqual(parent.reason,'game_process_exited');job.process.terminate.assert_called_once()
            self.assertFalse(json.loads((Path(d)/'game-process-exit.json').read_text())['training_eligible'])

    def test_expected_budget_is_local_but_user_and_error_stop_parent(self):
        p=Control();c=cycle.PhaseControl(p);c.cancel('session_time_limit')
        c.cancel('user_stop') # native acknowledgement of the planned stop
        self.assertTrue(c.stopped());self.assertFalse(p.stopped())
        p.cancel('f8');self.assertTrue(c.stopped());self.assertEqual(c.reason,'f8')
        for reason in ('f8','heartbeat_timeout','comparison_error','user_stop'):
            p=Control();c=cycle.PhaseControl(p);c.cancel(reason)
            self.assertEqual(p.reason,reason)

    def test_exact_resume_does_not_import_later_sibling_experiences(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cp=root/'checkpoint';cp.mkdir();(cp/'checkpoint.pt').write_bytes(b'fixture')
            save(root/'latest.json',{})
            self.assertEqual(resolve_resume(cp,exact=True),(cp.resolve(),None))
            self.assertEqual(resolve_resume(cp)[1],root.resolve())
            with self.assertRaises(ValueError):resolve_resume(root,exact=True)

    def test_physical_stop_proof_does_not_relax_training_acceptance(self):
        f=OnlineDataFixture();s=summary();s['cancelled']=True
        validate_shutdown(s,f.stop_rows())
        with self.assertRaises(ValueError):validate_stop(s,f.stop_rows())

    def test_500_requires_valid_continuous_progress(self):
        start=dict(observation_ns=0,tracking_epoch=1)
        rows=[dict(kind='episode_start',start=start)]
        for i,d in enumerate([0,250,500,501]):
            rows.append(dict(kind='observation',observation_ns=i*10_000_000,
                state=dict(status='ready',tracking_epoch=1,accumulated_m=d),sample=dict(lap_valid=True)))
        self.assertTrue(crossed_valid_500(rows,40_000_000))
        self.assertFalse(crossed_valid_500(rows,30_000_000))
        rows[-1]['sample']['lap_valid']=False
        self.assertFalse(crossed_valid_500(rows,40_000_000))
        rows[-1]['observation_ns']=100_000_000
        self.assertIsNone(crossed_valid_500(rows,110_000_000))

    def records(self):
        baseline=[dict(trial('lap',complete=i<3,variation=10,lateral=1),valid_over500=True) for i in range(5)]
        candidate=[dict(trial('latest',complete=i<4,variation=7,lateral=.7),valid_over500=True) for i in range(5)]
        return baseline+candidate

    def test_selection_rejects_lucky_win_regression_missing_data_and_tie(self):
        rows=self.records();self.assertTrue(cycle.selection_decision(rows)['passed'])
        for field,value in [('valid_over500',None),('excluded',True),('lap_seconds',106.)]:
            changed=[dict(r,**{field:value}) if r['model']=='latest' else r for r in rows]
            self.assertFalse(cycle.selection_decision(changed)['passed'])
        bad=[dict(r,bins={'1:5':dict(r['bins']['1:5'],steering_variation=11)})
            if r['model']=='latest' else r for r in rows]
        self.assertFalse(cycle.selection_decision(bad)['passed'])
        tie=[dict(r,model='latest') for r in rows[:5]]
        self.assertFalse(cycle.selection_decision(rows[:5]+tie)['passed'])

    def test_five_each_comparison_schema(self):
        original=__import__('TrainingEnvironment.policy_comparison',fromlist=['read']).read
        def config_read(p):
            data=original(p)
            if Path(p).name=='config.json':data['schema']='gamepad_champion_pair_v1'
            return data
        with tempfile.TemporaryDirectory() as d,patch('TrainingEnvironment.policy_comparison.read',config_read):
            (out,complete,reason),events=ComparisonFixture().orchestration(Path(d),pair=True)
            self.assertTrue(complete);self.assertEqual(reason,'paired_trials_finished')
            self.assertEqual(sum(e[0]=='drive' for e in events),10)

    def orchestration(self,root,fail_train=False,fail_compare=False,stop_at_selection=False):
        incumbent=dict(path=str(root/'incumbent'),model_sha256='old')
        candidate=dict(path=str(root/'candidate'),model_sha256='new')
        config=root/'cfg.json';save(config,dict(schema='gamepad_champion_cycle_v1',rounds=2,
            training_seconds=3600,comparison_seconds=2700,incumbent=incumbent,seed=42,training_config='fixture'))
        out=root/'cycle';out.mkdir();calls=[];control=Control()
        def train(args,child,emit):
            calls.append(('train',args.resume,args.resume_exact,args.seconds));p=root/f'train-{len(calls)}';p.mkdir()
            if fail_train:child.cancel('heartbeat_timeout');return p,False,'heartbeat_timeout'
            child.cancel('session_time_limit');return p,False,'session_time_limit'
        def compare(args,child,emit):
            calls.append(('compare',args.seconds));p=root/f'compare-{len(calls)}';p.mkdir()
            if stop_at_selection:child.cancel('f8')
            save(p/'report.json',dict(policy_updates=0,optimizer_updates=0,records=self.records()))
            return p,not fail_compare,'failed' if fail_compare else 'paired_trials_finished'
        def shortlist(session,history,current,check):
            check();return [candidate] if current['model_sha256']=='old' else []
        with patch.object(cycle,'verify_models'),patch.object(cycle,'new_folder',return_value=out),\
             patch.object(cycle,'train_run',train),patch.object(cycle,'compare_run',compare),\
             patch.object(cycle,'verify_boundary',return_value={}),patch.object(cycle,'candidates',shortlist),\
             patch.object(cycle,'f8_down',return_value=False):
            result=cycle.run(SimpleNamespace(config=str(config),seconds=12660),control,lambda *a,**kw:None)
        return result,calls

    def test_two_rounds_resume_selected_exact_snapshot_and_finish(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(out,complete,reason),calls=self.orchestration(root)
            self.assertTrue(complete);self.assertEqual(reason,'two_rounds_finished')
            self.assertEqual(calls,[('train',str(root/'incumbent'),True,3600),('compare',2700),('train',str(root/'candidate'),True,3600)])
            self.assertEqual(len(json.loads((out/'rounds.json').read_text())),2)

    def test_errors_f8_and_incomplete_comparison_never_start_next_training(self):
        for kw in (dict(fail_train=True),dict(fail_compare=True),dict(stop_at_selection=True)):
            with tempfile.TemporaryDirectory() as d:
                (_,complete,_),calls=self.orchestration(Path(d),**kw)
                self.assertFalse(complete);self.assertEqual(sum(c[0]=='train' for c in calls),1)

    def test_loop_budget_does_not_extend_other_gui_tasks(self):
        from TrainingController.jobs import command
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'config').write_text('{}')
            v=dict(seconds='12660',config='config')
            self.assertIn('gamepad-champion-cycle',command(root,'최고 모델 학습 반복',v))
            with self.assertRaises(ValueError):command(root,'정책 비교 평가',v)

if __name__=='__main__':unittest.main()
