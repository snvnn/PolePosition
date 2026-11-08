"""Two episodes through the real Python orchestrator with fake game/learner ports."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from ModelTrainer.common import save,digest
from .gamepad_online import run,Native,validate_session_limits,episode_numbers
from .run_dependencies import RunDependencies


class CycleTests(unittest.TestCase):
    def test_unlimited_count_keeps_two_hour_limit_and_fixed_evaluation(self):
        from itertools import islice
        config=dict(max_episodes=0,session_seconds=7200)
        validate_session_limits(config)
        self.assertEqual(list(islice(episode_numbers(config),12)),list(range(1,13)))
        self.assertEqual(list(episode_numbers(config,True)),[1,2,3])
        for values in [dict(max_episodes=-1,session_seconds=7200),dict(max_episodes=True,session_seconds=7200),
                       dict(max_episodes=0,session_seconds=0),dict(max_episodes=0,session_seconds=7201),
                       dict(max_episodes=0,session_seconds=float('nan'))]:
            with self.subTest(values=values),self.assertRaises(ValueError):validate_session_limits(values)
    def test_native_stop_response_cancels_before_reading_removed_waiting_run(self):
        with tempfile.TemporaryDirectory() as d:
            n=Native.__new__(Native);n.session=Path(d);n.exit_reason=None
            class Control:
                reason=None
                def stopped(self):return self.reason is not None
                def cancel(self,reason):self.reason=reason
            n.control=Control()
            save(n.session/'response-1.json',dict(reason='user_stop',run='already-removed'))
            with self.assertRaises(InterruptedError):n.drive(1,Path('fixture'),{})
            self.assertEqual(n.control.reason,'f8')
    def cycle(self,root,fail_validation=False,cancel_at_reset=False,start_first=False,unlimited=False,recover_first=False,terminal_resets=(),timing_episodes=(),interrupt_save=False):
        events=[];counter=[0]
        profile=root/'profile.json';save(profile,{'profile':True,'timeout_seconds':10})
        pin=root/'pinned';pin.write_text('unchanged')
        binding={str(pin):digest(pin)}
        config=dict(output_dir=str(root/'runs'),max_episodes=0 if unlimited else 2,session_seconds=60,source_model='B',dataset='data',
                    automatic_reset=dict(enabled=True,profile=str(profile),start_first_episode=start_first))
        class Control:
            reason=None
            def cancel(self,reason):self.reason=reason
            def stopped(self):return self.reason is not None
        control=Control()
        class Native:
            def __init__(self,*a):pass
            def reserve_reset_window(self,seconds):self.check()
            def check(self):
                if control.stopped():raise InterruptedError(control.reason)
            def drive(self,number,config,bindings,automatic_start=None):
                if unlimited and number==13:
                    control.cancel('session_time_limit');self.check()
                import tomllib
                cfg=tomllib.loads(config.read_text())
                assert cfg.get('automatic_initial_start',False)==(start_first and number==1)
                events.append(('drive',number,automatic_start is not None))
                folder=root/f'drive-{number}';folder.mkdir();save(folder/'manifest.json',{})
                if recover_first or timing_episodes:save(folder/'summary.json',{})
                return folder
            def close(self):events.append(('close',))
        class Learner:
            warm_updates=20000;pending=0;committed=[]
            def __init__(self,*a):pass
            def publish(self,parent,check,history):
                counter[0]+=1;events.append(('publish',counter[0]))
                folder=parent/f'checkpoint-{counter[0]}';folder.mkdir()
                save(folder/'model.json',dict(number=counter[0]));save(folder/'manifest.json',{})
                if interrupt_save and counter[0]==2:
                    control.cancel('heartbeat_timeout')
                    check()
                return folder
            def commit(self,*a):events.append(('commit',))
            def update(self,*a):events.append(('learn',));return [{'actor_rl_loss':0.}]
        class Reset:
            def __init__(self,path,check,progress):self.check=check
            def reset(self,out,require_garage=False):
                events.append(('reset',))
                if require_garage:events.append(('garage_required',))
                if cancel_at_reset:control.cancel('f8')
                self.check();out.mkdir();save(out/'ready.json',{'complete':True});return out/'ready.json'
        def collect(*a):
            events.append(('collect',))
            if int(a[0].name.split('-')[-1]) in terminal_resets:
                from .gamepad_online_data import TerminalResetObservation
                raise TerminalResetObservation('terminal_reset_observation_unavailable')
            if fail_validation:raise ValueError('Invalid experience')
            return [dict(reward=-1.)],dict(episode=dict(valid_laps=0,progress=4.),
                episode_end=dict(reason='invalid_lap'),shutdown=dict(kind='continuous_stop'))
        args=SimpleNamespace(config='fixture',task='gamepad-online',seconds=60,resume=None)
        with patch('TrainingEnvironment.gamepad_online.configuration',return_value=(config,{},binding)),\
             patch('TrainingEnvironment.gamepad_online.Learner',Learner),\
             patch('TrainingEnvironment.gamepad_online.collect',collect),\
             patch('TrainingEnvironment.gamepad_online.verify_runtime_binding'),\
             patch('TrainingEnvironment.gamepad_online.timing_gap',side_effect=lambda run,*a:dict(excluded=True,eligible=False,reason='observation_timing_gap',gap_ms=28.6581) if int(run.name.split('-')[-1]) in timing_episodes else None),\
             patch('TrainingEnvironment.gamepad_online.relocation',side_effect=lambda run,*a:dict(excluded=True,eligible=False,reason='unattributed_relocation') if recover_first and run.name=='drive-1' else None):
            if fail_validation or terminal_resets==(1,2,3) or timing_episodes==(1,2,3):
                with self.assertRaises(ValueError):run(args,control,lambda *a,**kw:None,dependencies=RunDependencies(native_factory=Native,reset_factory=Reset))
            else:run(args,control,lambda *a,**kw:None,dependencies=RunDependencies(native_factory=Native,reset_factory=Reset))
        return events

    def test_timeout_saving_does_not_erase_verified_failure_or_publish_partial_policy(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            events=self.cycle(root,interrupt_save=True)
            session=next((root/'runs').iterdir())
            m=json.loads((session/'manifest.json').read_text())
            self.assertEqual(m['reason'],'heartbeat_timeout')
            self.assertEqual(m['interrupted_phase'],'saving')
            self.assertEqual(m['last_verified_failure']['reason'],'invalid_lap')
            self.assertTrue((Path(m['last_verified_failure']['experience'])/'experience.json').exists())
            self.assertEqual(m['checkpoint'],json.loads((session/'latest.json').read_text())['checkpoint'])
            self.assertEqual(Path(m['checkpoint']).name,'checkpoint-1')
            self.assertNotIn(('drive',2,True),events)

    def test_short_gap_recovery_skips_training_and_obeys_stop_and_bounds(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);events=self.cycle(root,timing_episodes=(1,))
            self.assertEqual(events.count(('learn',)),1)
            self.assertEqual(events.count(('collect',)),1)
            self.assertIn(('drive',2,True),events)
            r=json.loads(next((root/'runs').iterdir()).joinpath('episodes.json').read_text())[0]
            self.assertTrue(r['excluded']);self.assertEqual(r['policy_before'],r['policy_after'])
        with tempfile.TemporaryDirectory() as d:
            events=self.cycle(Path(d),timing_episodes=(1,),cancel_at_reset=True)
            self.assertNotIn(('drive',2,True),events);self.assertNotIn(('learn',),events)
        with tempfile.TemporaryDirectory() as d:
            events=self.cycle(Path(d),timing_episodes=(1,2,3),unlimited=True)
            self.assertNotIn(('drive',4,True),events);self.assertNotIn(('learn',),events)
        with tempfile.TemporaryDirectory() as d:
            events=self.cycle(Path(d),timing_episodes=(1,2,4,5),unlimited=True)
            self.assertEqual(events.count(('learn',)),8)

    def test_terminal_reset_skips_update_preserves_policy_and_returns(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);events=self.cycle(root,start_first=True,terminal_resets=(1,))
            self.assertEqual(events.count(('commit',)),1)
            self.assertEqual(events.count(('learn',)),1)
            self.assertIn(('garage_required',),events)
            self.assertIn(('drive',2,True),events)
            records=json.loads(next((root/'runs').iterdir()).joinpath('episodes.json').read_text())
            self.assertTrue(records[0]['excluded'])
            self.assertEqual(records[0]['policy_before'],records[0]['policy_after'])

    def test_terminal_reset_recovery_bound_and_success_resets_counter(self):
        with tempfile.TemporaryDirectory() as d:
            events=self.cycle(Path(d),start_first=True,unlimited=True,terminal_resets=(1,2,3))
            self.assertNotIn(('learn',),events)
            self.assertNotIn(('drive',4,True),events)
        with tempfile.TemporaryDirectory() as d:
            events=self.cycle(Path(d),start_first=True,unlimited=True,terminal_resets=(1,2,4,5))
            self.assertEqual(events.count(('learn',)),8)

    def test_f8_during_terminal_reset_blocks_next_drive(self):
        with tempfile.TemporaryDirectory() as d:
            events=self.cycle(Path(d),cancel_at_reset=True,terminal_resets=(1,))
            self.assertNotIn(('learn',),events)
            self.assertNotIn(('drive',2,True),events)

    def test_relocation_skips_learning_and_requires_garage_for_next_episode(self):
        with tempfile.TemporaryDirectory() as d:
            events=self.cycle(Path(d),start_first=True,recover_first=True)
            self.assertEqual(events.count(('learn',)),1)
            self.assertEqual(events.count(('commit',)),1)
            self.assertIn(('garage_required',),events)
            self.assertEqual(sum(e[0]=='publish' for e in events),2)

    def test_unlimited_cycle_exceeds_ten_and_deadline_preserves_last_checkpoint(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);events=self.cycle(root,start_first=True,unlimited=True)
            self.assertEqual(len([e for e in events if e[0]=='learn']),12)
            self.assertIn(('drive',12,True),events)
            self.assertNotIn(('drive',13,True),events)
            self.assertEqual(events[-1],('close',))
            session=next((root/'runs').iterdir())
            manifest=json.loads((session/'manifest.json').read_text())
            self.assertEqual(manifest['reason'],'session_time_limit')
            self.assertTrue(manifest['truncated'])
            latest=json.loads((session/'latest.json').read_text())
            self.assertEqual(Path(latest['checkpoint']).name,'checkpoint-13')

    def test_reset_only_after_checkpoint_and_next_drive_is_automatic(self):
        with tempfile.TemporaryDirectory() as d:
            events=self.cycle(Path(d))
            self.assertEqual(events,[('publish',1),('drive',1,False),('collect',),('commit',),('learn',),
                ('publish',2),('reset',),('drive',2,True),('collect',),('commit',),('learn',),('publish',3),('close',)])

    def test_explicit_first_automatic_start_still_requires_reset_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            events=self.cycle(Path(d),start_first=True)
            self.assertEqual(events[:3],[('publish',1),('reset',),('drive',1,True)])

    def test_invalid_experience_prevents_reset_and_learning(self):
        with tempfile.TemporaryDirectory() as d:
            events=self.cycle(Path(d),fail_validation=True)
            self.assertNotIn(('reset',),events);self.assertNotIn(('learn',),events)
            self.assertNotIn(('drive',2,True),events)

    def test_f8_during_reset_preserves_candidate_without_next_drive(self):
        with tempfile.TemporaryDirectory() as d:
            events=self.cycle(Path(d),cancel_at_reset=True)
            self.assertIn(('publish',2),events);self.assertNotIn(('drive',2,True),events)


if __name__=='__main__':unittest.main()
