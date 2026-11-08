from .online_fixtures import OnlineDataFixture, observation, summary, REPORT
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import torch
from ModelTrainer.common import save,finish,digest
from TrainingEnvironment.gamepad_online_data import SEMANTICS,derive_seed,validate_stop,collect,TerminalResetObservation,verify_failure_end
from TrainingEnvironment.gamepad_online_io import read as stream_read,save as stream_save,rows_digest,same_bindings
from TrainingEnvironment.gamepad_online_learning import Learner,finite
from TrainingEnvironment.gamepad_online import drive_config,resolve_resume








class OnlineDataTests(OnlineDataFixture, unittest.TestCase):
    def test_binding_still_rejects_changed_content_or_different_file(self):
        with tempfile.TemporaryDirectory() as d:
            first=str(Path(d)/'profile.json'); second=str(Path(d)/'other.json')
            self.assertTrue(same_bindings({first:'a'},{first:'a'}))
            self.assertFalse(same_bindings({first:'a'},{first:'b'}))
            self.assertFalse(same_bindings({first:'a'},{second:'a'}))
            with self.assertRaises(ValueError):same_bindings({}, {})

    @unittest.skipUnless(__import__('os').name=='nt', 'Windows path identity')
    def test_windows_restored_account_case_and_duplicate_alias(self):
        old=r'C:\Users\YoonHyeon\Desktop\accad\profile.json'
        restored=r'C:\Users\Yoonhyeon\Desktop\accad\profile.json'
        self.assertTrue(same_bindings({old:'a'},{restored:'a'}))
        self.assertFalse(same_bindings({old:'a'},{restored:'changed'}))
        with self.assertRaises(ValueError):same_bindings({old:'a',restored:'b'},{restored:'b'})


    def test_separate_relocated_stop(self):
        validate_stop(summary(2),self.stop_rows(2))
        s=summary(2);s['shutdown']['kind']='continuous_stop'
        with self.assertRaises(ValueError):validate_stop(s,self.stop_rows(2))


    def test_off_track_standstill_does_not_need_projection(self):
        s=summary();s['shutdown']['kind']='untracked_standstill'
        validate_stop(s,self.raw_stop_rows())
        for bad in ('identity','paused','stale_physics','stale_graphics','moving','nan','negative','gap','teleport'):
            rows=self.raw_stop_rows()
            for o in rows:
                if o['kind']!='observation':continue
                raw=o['stop_telemetry']
                if bad=='identity':raw['identity_ok']=False
                if bad=='paused':raw['live']=False
                if bad=='stale_physics':raw['physics_ns']=0
                if bad=='stale_graphics':raw['graphics_ns']=0
                if bad=='moving':o['sample']['speed_kmh']=2.
                if bad=='nan':o['sample']['speed_kmh']=float('nan')
                if bad=='negative':o['sample']['speed_kmh']=-1.
                if bad=='gap' and o['sequence']==50:o['sequence']=0
                if bad=='teleport' and o['sequence']==50:o['sample']['position_x']=1000.
            with self.subTest(bad=bad), self.assertRaises(ValueError):validate_stop(s,rows)

    def test_off_track_braking_keeps_real_terminal_validation(self):
        with tempfile.TemporaryDirectory() as d:
            run,model,data,rows,s=self.fixture(d)
            for o in rows:
                if o['kind']=='observation' and o['sequence']>20:
                    o['state']['status']='invalid'
                    o['features']=None
                    o['stop_telemetry']=dict(live=True,identity_ok=True,
                        physics_ns=o['observation_ns'],graphics_ns=o['observation_ns'])
            s['shutdown']['kind']='untracked_standstill'
            self.store(run,rows,s)
            transitions,_=collect(run,model,data)
            self.assertTrue(transitions[-1]['terminated'])
            next(o for o in rows if o['kind']=='observation' and o['sequence']==20)['features']=None
            self.store(run,rows,s)
            with self.assertRaises(ValueError):collect(run,model,data)

    def test_no_standstill_across_reset_or_missing_observations(self):
        for rows in (self.stop_rows()[:30]+self.stop_rows()[31:], self.stop_rows(2)):
            s=summary();s['shutdown']['tracking_epoch']=1
            with self.assertRaises(ValueError):validate_stop(s,rows)

    def test_unknown_stop_kind_and_commands_after_neutralization_block_return(self):
        s=summary();s['shutdown']['kind']='unknown'
        with self.assertRaises(ValueError):validate_stop(s,self.stop_rows())
        rows=self.stop_rows()+[dict(kind='sent',send_end_ns=810_000_000)]
        with self.assertRaises(ValueError):validate_stop(summary(),rows)

    def test_missing_log_hash_blocks_training(self):
        with tempfile.TemporaryDirectory() as d:
            run,model,data,rows,s=self.fixture(d);self.store(run,rows,s)
            m=stream_read(run/'manifest.json');del m['output_sha256']['samples.jsonl']
            stream_save(run/'manifest.json',m,replace=True)
            with self.assertRaisesRegex(ValueError,'Missing hashes'):collect(run,model,data)

    def test_cancel_fault_intervention_and_release_block_training(self):
        for field in ('cancelled','error','logging_failure'):
            s=summary();s[field]=True
            with self.assertRaises(ValueError):validate_stop(s,self.stop_rows())
        for field in ('released','neutralized','standstill_confirmed'):
            s=summary();s['shutdown'][field]=False
            with self.assertRaises(ValueError):validate_stop(s,self.stop_rows())
        s=summary();s['episode']['interrupted']=True
        with self.assertRaises(ValueError):validate_stop(s,self.stop_rows())

    def test_seed_lap_reward_nonterminal_and_validation_excluded(self):
        r=dict(split='train',source_log_sha256='a',observation_ns=1,send_end_ns=2,next_observation_ns=3,
            next_features=[0.]*35,terminal_reason='valid_full_lap',reward_components=dict(terminal=10),terminated=True)
        result=derive_seed([r,dict(r,split='validation',source_log_sha256='b')])
        self.assertEqual(len(result),1);self.assertFalse(result[0]['terminated']);self.assertTrue(r['terminated'])
        with self.assertRaises(ValueError):derive_seed([r,dict(r,split='validation')])
        with self.assertRaises(ValueError):derive_seed([dict(r,next_observation_ns=2)])

    def test_stream_io_and_hash(self):
        import hashlib
        rows=[dict(i=i,features=list(range(35))) for i in range(4000)]
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'rows.json';stream_save(p,rows)
            self.assertEqual(rows,stream_read(p))
        self.assertEqual(rows_digest(rows),hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest())



    def test_real_terminal_pair_and_negative_reward(self):
        with tempfile.TemporaryDirectory() as d:
            run,model,data,rows,s=self.fixture(d);self.store(run,rows,s)
            transitions,_=collect(run,model,data)
            self.assertEqual(len(transitions),9);self.assertTrue(transitions[-1]['terminated'])
            self.assertEqual(transitions[-1]['reward_components']['terminal'],-10)
            self.assertLess(transitions[-1]['send_end_ns'],transitions[-1]['next_observation_ns'])

    def test_conflicting_or_duplicate_end_blocks_learning(self):
        for change in ('time','epoch','policy','marker','duplicate','old_invalid'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as d:
                run,model,data,rows,s=self.fixture(d)
                marker=next(r for r in rows if r['kind']=='episode_end')
                if change=='time':s['episode_end']['event']['observation_ns']-=1
                if change=='epoch':s['episode']['end']['tracking_epoch']+=1
                if change=='policy':s['episode_end']['policy_sha256']='other'
                if change=='marker':rows.remove(marker)
                if change=='duplicate':rows.append(marker)
                if change=='old_invalid':rows[0]['sample']['lap_valid']=False
                self.store(run,rows,s)
                with self.assertRaises(ValueError):collect(run,model,data)

    def test_stall_requires_ten_seconds_and_less_than_half_meter(self):
        for count,delta,passes in [(1000,0,False),(1001,0.49,True),(1001,0.5,False),(1001,1.,False)]:
            with self.subTest(count=count,delta=delta):
                rows=[observation(i+1) for i in range(count)]
                # Exactly representable endpoints straddle the start/finish seam.
                for r in rows:r['state']['s_m']=99.75
                rows[-1]['state']['s_m']=(99.75+delta)%100
                event=dict(reason='progress_stall',observation_ns=rows[-1]['observation_ns'],tracking_epoch=1)
                end=dict(event,policy_sha256='model',event=event)
                s=dict(model_sha256='model',episode_end=end,episode=dict(end=event),
                       episode_start=dict(observation_ns=rows[0]['observation_ns'],tracking_epoch=1))
                rows.append(dict(kind='episode_end',end=end))
                if passes:verify_failure_end(s,rows,100.)
                else:
                    with self.assertRaises(ValueError):verify_failure_end(s,rows,100.)

    def test_terminal_reset_excluded_only_after_validation(self):
        for bad in (None,'ordinary_missing','stale','unreleased','cancelled','nonterminal','nan','override'):
            with self.subTest(bad=bad),tempfile.TemporaryDirectory() as d:
                run,model,data,rows,s=self.fixture(d)
                s['shutdown'].update(kind='relocated_standstill',tracking_epoch=2)
                for o in rows:
                    if o['kind']=='observation' and o['sequence']>20:o['state']['tracking_epoch']=2
                end=next(o for o in rows if o['kind']=='observation' and o['sequence']==20)
                end['features']=None
                end['sample'].update(gear_raw=0,rpm=0,speed_kmh=0,fuel_litres=0,session_type=3)
                end['stop_telemetry']=dict(live=True,identity_ok=True,physics_ns=200_000_000,graphics_ns=200_000_000)
                if bad=='ordinary_missing':end['sample']['gear_raw']=5
                if bad=='stale':end['stop_telemetry']['physics_ns']=0
                if bad=='unreleased':s['shutdown']['released']=False
                if bad=='cancelled':s['cancelled']=True
                if bad=='nonterminal':next(o for o in rows if o['kind']=='observation' and o['sequence']==18)['features']=None
                if bad=='nan':end['features']=[float('nan')]*35
                if bad=='override':next(o for o in rows if o['kind']=='sent' and o['send_end_ns']==30_000_003)['report']['rt']=5
                self.store(run,rows,s)
                with self.assertRaises(ValueError) as caught:collect(run,model,data)
                self.assertEqual(isinstance(caught.exception,TerminalResetObservation),bad is None)

    def test_missing_terminal_and_input_override_rejected(self):
        for change in ('missing','override','reversal','epoch'):
            with tempfile.TemporaryDirectory() as d:
                run,model,data,rows,s=self.fixture(d)
                if change=='missing':rows=[r for r in rows if not (r['kind']=='observation' and r['sequence']==20)]
                if change=='override':next(r for r in rows if r['kind']=='sent' and r['send_end_ns']==30_000_003)['report']['rt']=5
                if change=='reversal':next(r for r in rows if r['kind']=='observation' and r['sequence']==10)['observation_ns']=1
                if change=='epoch':next(r for r in rows if r['kind']=='observation' and r['sequence']==20)['state']['tracking_epoch']=2
                self.store(run,rows,s)
                with self.assertRaises(ValueError):collect(run,model,data)

    def test_no_update_below_256_and_cancel_before_update(self):
        learner=Learner.__new__(Learner);learner.online=[{}]*255
        self.assertEqual(learner.update(lambda:None,lambda **kw:None),[])
        learner.online=[{}]*256;learner.pending=256;learner.q_updates=0;learner.actor_updates=0
        learner.tensors=lambda x:None
        def stop():raise InterruptedError('F8')
        with self.assertRaises(InterruptedError):learner.update(stop,lambda **kw:None)
        self.assertEqual(learner.q_updates,0)

    def test_update_credit_cap_and_actor_schedule(self):
        learner=Learner.__new__(Learner);learner.online=[{}]*3000;learner.pending=3000
        learner.q_updates=0;learner.actor_updates=0;learner.tensors=lambda rows:None;learner.batch=lambda _:None
        calls=[]
        def step(_,actor):calls.append(actor);return {}
        learner.step=step
        self.assertEqual(len(learner.update(lambda:None,lambda **kw:None)),500)
        self.assertEqual(sum(calls),125);self.assertEqual(learner.pending,1000)

    def test_nonfinite_optimizer_rejected(self):
        with self.assertRaises(ValueError):finite(dict(optimizer=[torch.tensor(float('nan'))]))

    def test_gamepad_job_does_not_route_to_speed_limited_sac(self):
        from TrainingController.jobs import command
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'online.toml').write_text('');(root/'checkpoint').mkdir()
            values=dict(seconds='7200',config='online.toml',checkpoint='checkpoint',model='checkpoint')
            job=command(root,'게임패드 회차 학습',values,True)
            self.assertIn('gamepad-online',job);self.assertIn('--resume',job);self.assertNotIn('--stage',job)
            job=command(root,'고정 정책 연속주행 평가',values)
            self.assertIn('gamepad-online-evaluate',job);self.assertIn('--model',job);self.assertNotIn('--stage',job)

    def test_validation_commit_rejected_and_duplicate_not_added(self):
        learner=Learner.__new__(Learner);learner.heldout_logs={'held'}
        with self.assertRaises(ValueError):learner.commit([dict(split='train',source_log_sha256='held')],'.')
        with tempfile.TemporaryDirectory() as d:
            artifact=Path(d);save(artifact/'experience.json',[])
            learner.committed=[digest(artifact/'experience.json')]
            with self.assertRaises(ValueError):learner.commit([],artifact)


if __name__=='__main__':unittest.main()
