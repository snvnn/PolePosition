"""Managed manual-reset batch port: native F7/F8 ownership, Python learning only after release."""
from pathlib import Path
import math
import time
import tomllib
import torch
from stable_baselines3 import SAC
from stable_baselines3.common.logger import configure
from ModelTrainer.common import read, save, hashes, digest, new_folder, finish
from TrainingController.process import ManagedProcess
from .lifecycle import BatchRunner, Limits, EpisodeResult, BatchStopped
from .checkpoint import save_checkpoint, load_checkpoint
from .online_policy import bootstrap, export_live, verified_outputs, BoundedActor
from .pretrain import OfflineSpaces
from .experience import transitions, save_experience, add_replay, DEFAULT_REWARD

ROOT=Path(__file__).resolve().parents[1]
STAGES={'verify5':5,'verify10':10,'verify30':30,'train':None,'evaluate':None,'evaluate_initial':None}

def configuration(path):
    c=tomllib.loads(Path(path).read_text(encoding='utf-8-sig'))
    if set(c)!={'schema_version','output_dir','episodes','episode_seconds','stage','actuator_config','reward'} or c['schema_version']!=1 or c['stage'] not in STAGES:
        raise ValueError('Invalid online configuration')
    Limits(c['episodes'],7200,c['episode_seconds'])
    if set(c['reward'])!=set(DEFAULT_REWARD) or any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in c['reward'].values()):
        raise ValueError('Explicit finite reward coefficients required')
    return c

class NativeBatchPort:
    def __init__(self,args,control,emit):
        self.args,self.control,self.emit=args,control,emit
        self.c=configuration(args.config)
        self.stage=args.stage or self.c['stage']
        if self.stage not in STAGES: raise ValueError('Unknown online stage')
        self.parent=Path(self.c['output_dir']).resolve()
        self.session=new_folder(self.parent,'sac-session')
        self.meta=dict(schema_version=1,kind='sac_session',complete=False,stage=self.stage,input_sha256=hashes([args.config,args.profile,args.controller_config,Path(args.prepared)/'manifest.json']))
        save(self.session/'manifest.json',self.meta)
        self.native=ManagedProcess();self.request_number=0;self.current_run=None;self.summary=None
        self.rows=[];self.records=[];self.updates=0;self.cumulative=0;self.rng=42;self.gates=[];self.last_checkpoint=None
        self.cancelled=False;self.verified_stop=False;self.bundle=None
        self.binding=dict(prepared=digest(Path(args.prepared)/'manifest.json'),profile=digest(args.profile),
                          controller=digest(args.controller_config),reward=self.c['reward'])
        torch.set_num_threads(1)
        if args.resume:
            if self.stage not in ('train','evaluate','evaluate_initial'): raise ValueError('Select training or lap evaluation')
            cp,cm=verified_outputs(args.resume,'sac_checkpoint')
            state=read(cp/'online.json')
            if state['binding']!=self.binding: raise ValueError('Resume path/calibration/controller/reward differs')
            self.boot=Path(state['bootstrap']);self.bundle=Path(state['bundle']);self.gates=state['gates'];self.rng=state['rng_state']
            self.env=OfflineSpaces(35)
            self.model,cm=load_checkpoint(cp,self.env,expected_input_paths=cm['input_sha256'])
            self.cumulative=cm['cumulative_episodes'];self.last_checkpoint=cp
            if self.stage=='evaluate_initial':
                self.model=SAC.load(self.boot/'model.zip',device='cpu')
                self.bundle=None
        elif args.previous_run:
            previous,pm=verified_outputs(args.previous_run,'sac_session')
            state=read(previous/'online.json')
            if state['binding']!=self.binding or state['source_model_sha256']!=digest(Path(args.model)/'model.zip'):
                raise ValueError('Verification belongs to different inputs')
            self.boot=Path(state['bootstrap']);self.gates=state['gates']
            self.model=SAC.load(self.boot/'model.zip',device='cpu');self.env=OfflineSpaces(35)
        else:
            if self.stage!='verify5': raise ValueError('Select the preceding successful verification result')
            self.boot,self.model,self.env=bootstrap(args.model,args.prepared,self.parent)
        if (not isinstance(self.model.actor,BoundedActor) or self.model.gamma != .99 or self.model.tau != .005
            or self.model.ent_coef != 'auto' or self.model.target_entropy != -2.):
            raise ValueError('Wrong online exploration/SAC configuration')
        self.model.set_logger(configure(folder=None,format_strings=[]))
        verified_outputs(self.boot,'sac_bootstrap')
        self.norm=read(self.boot/'normalizer.json')
        if self.bundle is None: self.bundle=export_live(self.model,self.boot,self.parent)
        self.verify_gates()
        self.meta['bootstrap']=str(self.boot)
        self.emit('progress',phase='preparing',result_dir=str(self.session))
        self.native.start([str(ROOT/'target/release/poleposition-actuator.exe'),'rl-supervisor','--managed',
                           '--session',str(self.session),'--managed-seconds',str(args.seconds)],ROOT)

    def verify_gates(self):
        required={'verify5':0,'verify10':1,'verify30':2,'train':3,'evaluate':3,'evaluate_initial':3}[self.stage]
        if len(self.gates)!=required: raise ValueError('Complete direct-control 5, 10, 30 second stages in order')
        initial_policy = None if self.args.resume else self.policy_identity()
        for gate,seconds in zip(self.gates,[5,10,30]):
            rows,s,m=transitions(gate,verification=True)
            rl=m['config']['reinforcement']
            if rl['seconds']!=seconds or not rl['deterministic']: raise ValueError('Wrong verification duration/mode')
            gate_bundle=Path(rl['bundle'])
            gate_policy=digest(gate_bundle/'model.json')
            if initial_policy is None: initial_policy=gate_policy
            if gate_policy!=initial_policy: raise ValueError('Verification policy changed between stages')
            if read(gate_bundle/'manifest.json')['bootstrap']!=str(self.boot.resolve()): raise ValueError('Verification belongs to another initial policy')
            if m['input_sha256'].get(str(Path(self.args.profile).resolve()))!=digest(self.args.profile):
                # Native canonical paths may use the Windows extended prefix.
                if not any(Path(p).samefile(self.args.profile) and h==digest(self.args.profile) for p,h in m['input_sha256'].items()):
                    raise ValueError('Verification calibration differs')

    def poll(self):
        if self.control.stopped(): self.native.stop();self.cancelled=True
        for e in self.native.poll():
            if e['event']=='finished':
                self.cancelled=True
                if not self.control.stopped(): self.control.cancel(e.get('reason') or 'native_exit')
            else:
                fields={k:v for k,v in e.items() if k not in ('event','version')}
                self.emit('progress',**fields)
        return self.cancelled

    def policy_identity(self): return digest(self.bundle/'model.json')
    def reset(self,deadline):
        self.poll();self.verified_stop=False;self.summary=None
        self.emit('progress',phase='manual_reset',episode=self.cumulative+1)
    def prepare_start(self,deadline): pass
    def collect(self,deadline,speed_cap,deterministic,check):
        self.request_number+=1
        config=tomllib.loads(Path(self.c['actuator_config']).read_text(encoding='utf-8-sig'))
        controller=tomllib.loads(Path(self.args.controller_config).read_text(encoding='utf-8-sig'))
        if controller.get('single_lap',False) or controller['target_speed_kmh']!=10: raise ValueError('Use the short 10km/h controller configuration')
        seconds=STAGES[self.stage] or self.c['episode_seconds']
        config.update(baseline=dict(prepared=str(Path(self.args.prepared).resolve()),config=controller),
                      reinforcement=dict(bundle=str(self.bundle),seconds=seconds,deterministic=self.stage!='train',rng_state=self.rng,evaluation=self.stage in ('evaluate','evaluate_initial')),
                      test_seconds=seconds+30,arm_wait_seconds=min(600,max(1,deadline-time.monotonic())))
        self.current_run=None
        save(self.session/f'request-{self.request_number}.json',dict(config=config,prepared=str(Path(self.args.prepared).resolve()),profile=str(Path(self.args.profile).resolve())),replace=True)
        response=self.session/f'response-{self.request_number}.json'
        while not response.exists():
            self.poll()
            if self.cancelled:
                raise BatchStopped(self.control.reason or 'native_exit')
            check();time.sleep(.02)
        # Native publishes response by rename, after the complete run inventory.
        value=read(response)
        if 'error' in value: raise RuntimeError(value['error'])
        self.current_run=Path(value['run']);self.summary=read(self.current_run/'summary.json')
        reason=self.summary['controller_exit_reason']
        self.rng=self.summary.get('rl_rng_state',self.rng)
        self.verified_stop=all(self.summary.get(k) is True for k in ['normal_shutdown','stop_confirmed','released']) and not self.summary.get('released_while_halted')
        if value.get('cancelled'):
            self.control.cancel(value['cancelled']);raise BatchStopped(value['cancelled'])
        if self.summary['termination_reason']!='scenario_complete':
            if self.summary['termination_reason'] in ('f8','user_stop','ctrl_c','time_limit'):
                self.control.cancel(self.summary['termination_reason']);raise BatchStopped(self.summary['termination_reason'])
            return EpisodeResult(reason,True,False,self.summary['termination_reason'])
        return EpisodeResult(reason,reason!='episode_timeout',reason=='episode_timeout')
    def stop_and_confirm(self):
        if self.verified_stop: return True
        self.native.stop()
        # Never report stop while native still owns the actuator; retain ownership until exit.
        while self.native.process is not None:
            self.poll();time.sleep(.05)
        response=self.session/f'response-{self.request_number}.json'
        if response.exists():
            value=read(response)
            if value.get('run'):
                s=read(Path(value['run'])/'summary.json')
                return s.get('stop_confirmed') is True and s.get('released') is True
        return self.request_number==0
    def validate_experience(self,result):
        self.rows,s,_=transitions(self.current_run,self.c['reward'],verification=self.stage.startswith('verify'))
        self.records.append(dict(run=str(self.current_run),policy_before=self.policy_identity(),samples=len(self.rows),
                                 reward=sum(r['reward'] for r in self.rows),reason=result.reason,updates=0))
        self.emit('progress',phase='validating',samples=len(self.rows),reward=self.records[-1]['reward'],reason=result.reason)
        return len(self.rows)
    def commit_experience(self):
        if not self.verified_stop: raise RuntimeError('Cannot commit while driving')
        self.records[-1]['experience']=str(save_experience(self.current_run,self.rows,self.parent,self.c['reward']))
        add_replay(self.model,self.rows,self.norm)
    def replay_ready(self): return self.model.replay_buffer.size()>=256
    def update_once(self):
        if not self.verified_stop or self.poll(): raise BatchStopped('cancelled')
        self.model.train(gradient_steps=1,batch_size=256);self.updates+=1
        self.records[-1]['updates']+=1
        if self.updates%25==0: self.emit('progress',phase='learning',updates=self.updates,episode=self.cumulative+1)
    def state(self):
        return dict(bootstrap=str(self.boot),bundle=str(self.bundle),gates=self.gates,rng_state=self.rng,binding=self.binding,
                    source_model_sha256=digest(Path(read(self.boot/'manifest.json')['source_training'])/'model.zip'))
    def save_checkpoint(self,episode,speed_cap):
        if self.poll(): raise BatchStopped('cancelled')
        self.bundle=export_live(self.model,self.boot,self.parent)
        self.last_checkpoint=save_checkpoint(self.model,self.env,speed_cap=10,cumulative_episodes=self.cumulative+1,
            input_paths=[self.boot/'manifest.json',self.boot/'model.zip',self.bundle/'manifest.json'],stationary_confirmed=lambda:self.verified_stop and not self.poll(),
            parent=self.parent,online_state=self.state())
        self.cumulative+=1
        self.records[-1].update(checkpoint=str(self.last_checkpoint),policy_after=self.policy_identity())
        self.emit('progress',phase='saved',updates=self.updates,episode=self.cumulative,result_dir=str(self.last_checkpoint))
    def save_evaluation(self,result,policy,speed_cap):
        if self.stage in ('evaluate','evaluate_initial'):
            from .lap_report import evaluate_run
            report=evaluate_run(self.current_run)
            report['learning_updates']=0
            report['replay_insertions']=0
            save(self.session/'lap-evaluation.json',report,replace=True)
            self.records[-1]['lap_evaluation']=report
            self.emit('progress',phase='lap_evaluated',reason=report['reason'],result_dir=str(self.session))
            return
        self.gates.append(str(self.current_run));self.cumulative+=1
        save(self.session/'online.json',self.state(),replace=True)
    def close(self,complete,error=None):
        if self.native.process:
            if complete: save(self.session/'done.json',dict(complete=True),replace=True)
            else: self.native.stop()
            while self.native.process is not None:
                self.poll();time.sleep(.05)
        if self.bundle:
            save(self.session/'online.json',self.state(),replace=True)
        save(self.session/'training-summary.json',dict(episodes=self.records,updates=self.updates,cumulative_episodes=self.cumulative),replace=True)
        self.meta['checkpoint']=str(self.last_checkpoint) if self.last_checkpoint else None
        finish(self.session,self.meta,complete,error)

def run(args,control,emit):
    port=None
    try:
        port=NativeBatchPort(args,control,emit)
        runner=BatchRunner(port,10,Limits(1 if port.stage!='train' else port.c['episodes'],args.seconds,port.c['episode_seconds']),
                           cancelled=port.poll,evaluation=port.stage!='train',progress=lambda p:emit('progress',phase=p))
        result=runner.run()
        complete=result.reason=='episode_limit'
        port.close(complete,None if complete else result.reason)
        reason=result.reason
        if port.stage in ('evaluate','evaluate_initial') and complete:
            report=read(port.session/'lap-evaluation.json')
            reason='유효 한 랩 완주' if report['completed_valid_laps'] else '한 랩 미완료: '+report['reason']
        return port.session,complete,reason
    except BaseException as error:
        if port is not None:
            port.stop_and_confirm();port.close(False,error)
        raise
