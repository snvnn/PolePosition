"""Manual F7 -> continuous episode -> verified stop -> IQL -> immutable next policy."""
import json
import math
from pathlib import Path
from ModelTrainer.common import read,save,digest,hashes,verify_hashes,new_folder,finish
from .gamepad_online_data import SEMANTICS, collect, TerminalResetObservation
from .gamepad_online_learning import Learner
from .gamepad_online_io import read,save,same_bindings
from .gamepad_recovery import relocation,timing_gap,MAX_RECOVERIES

from .gamepad_runtime import Native
from .gamepad_session_config import (configuration, verify_runtime_binding, drive_config,
                                    validate_session_limits, episode_numbers)
from .run_dependencies import RunDependencies


def resolve_resume(path,exact=False):
    path=Path(path).resolve()
    if exact:
        if not (path/'checkpoint.pt').is_file():raise ValueError('Exact resume requires a checkpoint folder')
        return path,None
    if (path/'latest.json').is_file():
        pointer=read(path/'latest.json');cp=Path(pointer['checkpoint'])
        if digest(cp/'manifest.json')!=pointer['manifest_sha256']:raise ValueError('Changed checkpoint pointer')
        return cp,path
    if (path/'checkpoint.pt').is_file():return path,path.parent if (path.parent/'latest.json').is_file() else None
    raise ValueError('Select online checkpoint or session folder')


def run(args,control,emit,prepare_only=False, *, dependencies=None):
    dependencies = dependencies or RunDependencies()
    c,template,binding=configuration(args.config)
    evaluation=args.task=='gamepad-online-evaluate'
    auto=c.get('automatic_reset',{})
    if auto.get('enabled') and (evaluation or not auto.get('profile')):
        raise ValueError('Automatic reset requires a profile and is for training only')
    reset_profile=Path(auto['profile']) if auto.get('enabled') else None
    reset_hash=digest(reset_profile) if reset_profile else None
    session=new_folder(c['output_dir'],'gamepad-online-evaluation' if evaluation else 'gamepad-online-session')
    save(session/'config.json',c)
    metadata=dict(kind='gamepad_online_evaluation' if evaluation else 'gamepad_online_session',
        continuous_semantics=SEMANTICS,complete=False,input_sha256=binding,stability_passed=False)
    save(session/'manifest.json',metadata)
    native_factory = dependencies.native_factory or Native
    native_options = {'clock': dependencies.clock} if dependencies.clock is not None and dependencies.native_factory is None else {}
    native=native_factory(session,control,emit,min(args.seconds,c['session_seconds']),**native_options)
    phase='preparing'
    def progress(**details):
        nonlocal phase
        phase=details.get('phase',phase)
        emit('progress',result_dir=str(session),**details)
    records=[];model=None;learner=None;reason='session_complete';evaluation_start=None;runpath=None;recoveries=0;need_garage=False;terminal_resets=0;timing_recoveries=0
    try:
        progress(phase='preparing')
        cp,previous=resolve_resume(args.resume,exact=getattr(args,'resume_exact',False)) if args.resume else (None,None)
        metadata['resume_exact']=bool(getattr(args,'resume_exact',False))
        metadata['resume_checkpoint']=str(cp) if cp else None
        if evaluation:
            model=cp or Path(args.model or c['source_model']).resolve()
            manifest=read(model/'manifest.json');verify_hashes(manifest['output_sha256'],model)
        else:
            learner=Learner(c['source_model'],c['dataset'],binding,cp,**({'smoothness':c['smoothness']} if 'smoothness' in c else {}))
            # Replay commits survive interruption of candidate learning/export. Recover once.
            if previous:
                for artifact in sorted(previous.glob('experience-*')):
                    if not (artifact/'manifest.json').is_file():continue
                    m=read(artifact/'manifest.json')
                    if not m.get('complete'):continue
                    verify_hashes(m['output_sha256'],artifact)
                    if not same_bindings(m['binding'],binding):raise ValueError('Recovered experience binding mismatch')
                    if digest(artifact/'experience.json') not in learner.committed:
                        learner.commit(read(artifact/'experience.json'),artifact)
            if learner.warm_updates<20000:
                progress(phase='critic_warmup')
                learner.warm(native.check,progress)
            progress(phase='saving')
            model=learner.publish(session,native.check,[])
            save(session/'latest.json',dict(checkpoint=str(model),manifest_sha256=digest(model/'manifest.json')),replace=True)
            metadata['checkpoint']=str(model)
        if prepare_only:
            reason='initial_B_actor_and_continuous_critics_ready'
        else:
            fixed_hash=digest(model/'model.json')
            for number in episode_numbers(c,evaluation):
                native.check();verify_runtime_binding(binding,template)
                before=digest(model/'model.json')
                if evaluation and before!=fixed_hash:raise ValueError('Evaluation policy changed')
                receipt=None
                if reset_profile and (number>1 or auto.get('start_first_episode',False)):
                    # This point is reached only after validated experience and an
                    # atomically published candidate. F8 stays owned by Native.
                    if digest(reset_profile)!=reset_hash:raise ValueError('Automatic reset profile changed')
                    # No menu input near the hour boundary: leave enough room
                    # for the registered full reset timeout and its final check.
                    native.reserve_reset_window(float(read(reset_profile)['timeout_seconds'])+1.)
                    reset=dependencies.reset(reset_profile,native.check,progress)
                    receipt=reset.reset(session/f'reset-{number:03d}',require_garage=True) if need_garage else reset.reset(session/f'reset-{number:03d}')
                    need_garage=False
                    native.check();verify_runtime_binding(binding,template)
                progress(phase='auto_start' if receipt else 'manual_reset',episode=number,policy_before=before)
                cfg=drive_config(session/f'drive-{number}.toml',dict(template,allow_verified_relocation_recovery=bool(reset_profile),
                                 allow_verified_timing_gap_recovery=bool(reset_profile) and not evaluation),model,evaluation,
                                 automatic_initial_start=number==1 and receipt is not None)
                runpath=native.drive(number,cfg,dict(binding,**hashes([cfg,model/'model.json',model/'manifest.json'])),receipt)
                verify_runtime_binding(binding,template)
                native.check();progress(phase='validating',episode=number)
                gap=timing_gap(runpath,before) if reset_profile and not evaluation and (runpath/'summary.json').is_file() else None
                if gap:
                    timing_recoveries+=1
                    gap.update(episode=number,policy_before=before,policy_after=before,
                        samples=0,q_updates=0,actor_updates=0,consecutive_timing_recoveries=timing_recoveries)
                    records.append(gap);save(session/'episodes.json',records,replace=True)
                    if timing_recoveries>MAX_RECOVERIES:raise ValueError('Consecutive timing gap recovery limit reached')
                    progress(phase='timing_gap_recovery',episode=number,gap_ms=gap['gap_ms'])
                    continue
                recovery=relocation(runpath,before,False) if reset_profile and (runpath/'summary.json').is_file() else None
                if recovery:
                    recoveries+=1
                    recovery.update(episode=number,policy_before=before,policy_after=before,
                        samples=0,q_updates=0,actor_updates=0,recovery_number=recoveries)
                    records.append(recovery);save(session/'episodes.json',records,replace=True)
                    if recoveries>MAX_RECOVERIES:raise ValueError('Verified relocation recovery limit reached')
                    progress(phase='relocation_recovery',episode=number,recovery_number=recoveries)
                    need_garage=True
                    continue
                try:
                    experience,summary=collect(runpath,model,c['dataset'],evaluation)
                except TerminalResetObservation:
                    if not reset_profile or evaluation:raise
                    native.check()
                    terminal_resets+=1
                    records.append(dict(episode=number,run=str(runpath),excluded=True,recovery_only=True,
                        eligible=False,reason='terminal_reset_observation_unavailable',
                        manifest_sha256=digest(runpath/'manifest.json'),policy_before=before,policy_after=before,
                        samples=0,q_updates=0,actor_updates=0,consecutive_terminal_resets=terminal_resets))
                    save(session/'episodes.json',records,replace=True)
                    if terminal_resets>MAX_RECOVERIES:
                        raise ValueError('Consecutive terminal reset recovery limit reached')
                    progress(phase='terminal_reset_recovery',episode=number)
                    need_garage=True
                    continue
                terminal_resets=0
                timing_recoveries=0
                native.check()
                record=dict(episode=number,run=str(runpath),policy_before=before,samples=len(experience),
                    reward=sum(r['reward'] for r in experience),valid_laps=summary['episode']['valid_laps'],
                    progress_m=summary['episode']['progress'],end_reason=summary['episode_end']['reason'],
                    stop_kind=summary['shutdown']['kind'],eligible=True)
                if evaluation:
                    if record['valid_laps']!=3:raise ValueError('Evaluation requires three consecutive valid laps')
                    start=summary['episode_start']
                    if evaluation_start is None:evaluation_start=start
                    distance=math.hypot(start['position_x']-evaluation_start['position_x'],start['position_z']-evaluation_start['position_z'])
                    if (distance>c['evaluation_start_tolerance_m'] or start['gear_raw']!=evaluation_start['gear_raw']
                            or not 0<=start['speed_kmh']<=1):
                        raise ValueError('Evaluation starting conditions differ')
                    record.update(start=start,start_difference_m=distance)
                    record['policy_after']=before;records.append(record)
                    save(session/'evaluations.json',records,replace=True)
                    continue
                # First durable commit contains only validated experience, never a partial model.
                artifact=session/f'experience-{number:03d}';artifact.mkdir()
                save(artifact/'experience.json',experience);save(artifact/'episode.json',record)
                finish(artifact,dict(kind='gamepad_online_experience',binding=binding,
                    input_sha256=hashes([runpath/'manifest.json']),continuous_semantics=SEMANTICS))
                metadata['last_verified_failure']=dict(episode=number,reason=record['end_reason'],
                    shutdown=record['stop_kind'],experience=str(artifact),policy_before=before)
                learner.commit(experience,artifact)
                progress(phase='learning',**record)
                history=learner.update(native.check,progress)
                native.check();progress(phase='saving',episode=number)
                candidate=learner.publish(session,native.check,history)
                native.check()
                save(session/'latest.json',dict(checkpoint=str(candidate),manifest_sha256=digest(candidate/'manifest.json')),replace=True)
                model=candidate;metadata['checkpoint']=str(model)
                record.update(policy_after=digest(model/'model.json'),q_updates=len(history),
                    actor_updates=sum('actor_rl_loss' in h for h in history),pending=learner.pending)
                records.append(record);save(session/'episodes.json',records,replace=True)
                progress(phase='next_f7',**record)
        metadata.update(checkpoint=str(model),stability_passed=evaluation and len(records)==3)
    except InterruptedError as error:
        reason=str(error)
        metadata['interrupted_phase']=phase
    except Exception as error:
        reason=f'{type(error).__name__}: {error}'
        control.cancel('session_error')
        save(session/'failure.json',dict(reason=reason,records=records,run=str(runpath) if runpath else None,
            shutdown_and_event=read(runpath/'summary.json') if runpath and (runpath/'summary.json').is_file() else None),replace=True)
        raise
    finally:
        native.close()
        metadata.update(reason=reason,records=records,truncated=reason=='session_time_limit')
        finish(session,metadata,complete=reason in ('session_complete','initial_B_actor_and_continuous_critics_ready'))
    return session,metadata['complete'],reason


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',default='TrainingEnvironment/gamepad-online.toml')
    parser.add_argument('--resume');parser.add_argument('--model');parser.add_argument('--seconds',type=float,default=7200)
    parser.add_argument('--prepare-only',action='store_true');parser.add_argument('--task',default='gamepad-online')
    args=parser.parse_args()
    # Standalone preparation has no GUI heartbeat. F8 remains owned by native supervisor.
    class Control:
        reason=None
        def cancel(self,reason):self.reason=self.reason or reason
        def stopped(self):return self.reason is not None
    print(run(args,Control(),lambda event,**kw:print(json.dumps(dict(event=event,**kw)),flush=True),args.prepare_only))
