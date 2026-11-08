"""Reusable temporary fixtures; this module contains no TestCase classes."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from ModelTrainer.common import save
from .policy_comparison import run
from .run_dependencies import RunDependencies

def trial(label='candidate',complete=True,variation=7.,lateral=.7):
    return dict(model=label,excluded=False,completed=complete,valid_laps=int(complete),lap_seconds=100. if complete else None,
        progress_m=4000. if complete else 500.,reason='evaluation_complete' if complete else 'invalid_lap',
        bins={'1:5':dict(distance_m=100.,seconds=5.,steering_variation=variation,lateral_sq_time=5*lateral*lateral,samples=250)})


class ComparisonFixture:
    def orchestration(self,root,cancel_at=None,reset_failure=False,deadline_at_reset=False,recover_at=(),pair=False):
        profile=root/'reset.json';save(profile,{})
        labels=('lap','latest') if pair else ('initial','best','latest','control','candidate')
        models={k:dict(path=str(root/k),model_sha256=k) for k in labels}
        for item in models.values():
            p=Path(item['path']);p.mkdir();save(p/'model.json',{});save(p/'manifest.json',{})
        config=root/'config.json';save(config,dict(schema='gamepad_corner_pair_v1' if pair else 'gamepad_policy_comparison_v1',models=models,
            screening_repeats=4,confirmation_repeats=5,trial_seconds=180,seed=42,session_seconds=7200,training_config='fixture'))
        class Control:
            reason=None
            def cancel(self,r):self.reason=self.reason or r
            def stopped(self):return self.reason is not None
        control=Control();events=[];clock=[0.];current=[0]
        class FakeNative:
            def __init__(self,session,control,emit,seconds):
                self.control=control;events.append(('native',seconds))
            def check(self):
                if self.control.stopped():raise InterruptedError(self.control.reason)
            def drive(self,number,config,bindings,receipt):
                current[0]=number
                import tomllib
                c=tomllib.loads(config.read_text());label=Path(c['model']).name
                assert c['evaluation_laps']==1 and c['policy_comparison']
                events.append(('drive',number,label))
                if number==cancel_at:self.control.cancel('session_time_limit');self.check()
                return root/label
            def close(self):events.append(('close',))
        class Reset:
            def __init__(self,*a):pass
            def reset(self,path,require_garage=False):
                if require_garage:events.append(('garage_required',))
                if reset_failure:raise ValueError('Screen mismatch')
                if deadline_at_reset:clock[0]=7200.
                return profile
        def analyze(path,sha):
            if current[0] in recover_at:raise ValueError('Non-comparison stop: tracking_not_ready')
            return trial(Path(path).name,variation=7 if sha=='candidate' else 10,lateral=.7 if sha=='candidate' else 1.)
        args=SimpleNamespace(config=str(config),seconds=7200)
        def new_folder(*a):p=root/'output';p.mkdir();return p
        dependencies=RunDependencies(native_factory=FakeNative,reset_factory=Reset,
            clock=SimpleNamespace(monotonic=lambda:clock[0],wall=lambda:clock[0]))
        with patch('TrainingEnvironment.policy_comparison.verify_models',return_value={}),patch('TrainingEnvironment.policy_comparison.verify_hashes'),\
             patch('TrainingEnvironment.policy_comparison.configuration',return_value=(dict(automatic_reset=dict(profile=str(profile))),{},{})),\
             patch('TrainingEnvironment.policy_comparison.verify_runtime_binding'),patch('TrainingEnvironment.policy_comparison.analyze_trial',analyze),\
             patch('TrainingEnvironment.policy_comparison.new_folder',new_folder),patch('TrainingEnvironment.policy_comparison.prepare_focus'),\
             patch('TrainingEnvironment.policy_comparison.promote',return_value=True):
            with patch('TrainingEnvironment.policy_comparison.relocation',return_value=dict(excluded=True,reason='unattributed_relocation',eligible=False)):
                result=run(args,control,lambda *a,**kw:None,dependencies=dependencies)
        return result,events
