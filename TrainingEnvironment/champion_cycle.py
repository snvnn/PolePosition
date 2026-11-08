"""Bounded train/select cycles, preserving exact champions and evaluation isolation."""
import math
import threading
import time
from datetime import datetime
from itertools import count
from ExecutionRuntime.timing import TimeBudget, check_cancelled
from .run_dependencies import RunDependencies
from pathlib import Path
from types import SimpleNamespace
from ModelTrainer.common import digest,verify_hashes,new_folder,finish
from .gamepad_online_io import read,save
from .gamepad_online import run as train_run
from .policy_comparison import run as compare_run,verify_models,aggregate,common_metrics
from .gamepad_online_data import validate_shutdown,verify_episode_files

MAX_SECONDS=12660  # 2 x (3600 training + 2700 comparison), plus bookkeeping.
MAX_DEADLINE_SECONDS=43200


def deadline_timestamp(cfg):
    if cfg.get('schema')!='gamepad_champion_deadline_v1':return None
    d=datetime.fromisoformat(cfg['deadline'])
    if d.tzinfo is None:raise ValueError('Deadline requires an explicit timezone')
    return d.timestamp()


class PhaseControl:
    """Only an expected phase deadline is local; user/system stops cancel the cycle."""
    def __init__(self,parent):self.parent=parent;self.reason=None
    def cancel(self,reason):
        if self.reason is not None:return
        self.reason=reason
        if reason!='session_time_limit':self.parent.cancel(reason)
    def stopped(self):
        if self.parent.stopped():
            self.reason=self.parent.reason or 'user_stop'
        return self.reason is not None


def f8_down():
    import sys
    if sys.platform!='win32':return False
    import ctypes
    return bool(ctypes.windll.user32.GetAsyncKeyState(0x77)&0x8000)


def verify_boundary(session):
    """A deadline never bypasses physical release, logging or UI-reset failures."""
    session=Path(session)
    if any(session.glob('reset-*/failure.json')):raise ValueError('Reset interrupted at phase boundary')
    paths=sorted(session.glob('response-*.json'),key=lambda p:int(p.stem.split('-')[1]))
    if not paths:raise ValueError('No completed native response at phase boundary')
    requests={p.stem.split('-')[1] for p in session.glob('request-*.json')}
    responses={p.stem.split('-')[1] for p in paths}
    if requests!=responses:raise ValueError('Unfinished native request at phase boundary')
    if (session/'game-process-exit.json').exists():raise ValueError('Game exited during phase')
    response=read(paths[-1])
    if response.get('error') or not response.get('run'):raise ValueError('Native phase boundary error')
    run=Path(response['run']);manifest=read(run/'manifest.json');summary=read(run/'summary.json')
    verify_episode_files(manifest,run)
    if summary.get('error') or summary.get('logging_failure') or summary.get('correction_samples',0):
        raise ValueError('Unsafe or intervened phase boundary')
    if summary.get('reason') not in ('invalid_lap','progress_stall','evaluation_complete',
            'tracking_changed_before_stop','user_stop','time_limit','session_time_limit'):
        raise ValueError('Unexpected phase boundary reason')
    with (run/'samples.jsonl').open(encoding='utf-8') as f:
        import json
        rows=[json.loads(line) for line in f]
    validate_shutdown(summary,rows)
    return dict(run=str(run),shutdown=summary['shutdown'],manifest_sha256=digest(run/'manifest.json'))


def completion_candidate(run):
    run=Path(run);m=read(run/'manifest.json');s=read(run/'summary.json')
    if m.get('policy_comparison') or not m.get('complete') or s['episode']['valid_laps']<1:
        return None
    verify_episode_files(m,run)
    if s.get('model_sha256')!=m.get('model_sha256'):raise ValueError('Evidence policy mismatch')
    item=dict(path=m['model'],model_sha256=m['model_sha256'])
    cp=Path(item['path']);cm=read(cp/'manifest.json')
    if not cm.get('complete') or not cm.get('parity_passed') or digest(cp/'model.json')!=item['model_sha256']:
        raise ValueError('Completion checkpoint is not verified')
    return dict(**item,evidence_run=str(run),valid_laps=s['episode']['valid_laps'],
        progress_m=s['episode']['progress'],lap_seconds=s['episode']['lap_times_s'])


def candidates(session,historical,incumbent,check):
    pool=[]
    for run in historical:
        check();item=completion_candidate(run)
        if item:pool.append(item)
    for artifact in sorted(Path(session).glob('experience-*')):
        check();m=read(artifact/'manifest.json')
        if not m.get('complete'):continue
        record=read(artifact/'episode.json')
        if not record.get('eligible') or record.get('valid_laps',0)<1:continue
        verify_hashes(m['output_sha256'],artifact)
        item=completion_candidate(record['run'])
        if item and item['model_sha256']!=record['policy_before']:
            raise ValueError('Candidate must be the policy BEFORE its successful drive')
        if item:pool.append(item)
    unique={p['model_sha256']:p for p in pool if p['model_sha256']!=incumbent['model_sha256']}
    return sorted(unique.values(),key=lambda p:(-p['valid_laps'],-p['progress_m'],min(p['lap_seconds']),p['model_sha256']))


def selection_decision(rows):
    groups={k:[r for r in rows if r['model']==k] for k in ('lap','latest')}
    b,c=aggregate(groups['lap']),aggregate(groups['latest'])
    m=common_metrics(groups['latest'],groups['lap'])
    known=all(type(r.get('valid_over500')) is bool for r in rows)
    b500=sum(r.get('valid_over500') is True for r in groups['lap'])
    c500=sum(r.get('valid_over500') is True for r in groups['latest'])
    enough=m['enough'] and m['baseline']['steering_per_100m']>0 and m['baseline']['lateral_rms']>0
    checks=dict(five_each=b['attempts']==c['attempts']==5,
        no_exclusions=not any(r.get('excluded') for r in rows),
        completion_reproducible=c['completions']>=3 and c['completions']>=b['completions'],
        valid_500_not_worse=known and c500>=b500,
        progress_not_worse=c['median_progress_m'] is not None and b['median_progress_m'] is not None
            and c['median_progress_m']>=b['median_progress_m'],
        weaving_not_worse=bool(enough and m['candidate']['steering_per_100m']<=m['baseline']['steering_per_100m']
            and m['candidate']['lateral_rms']<=m['baseline']['lateral_rms']),
        lap_time_not_worse=bool(c['median_lap_seconds'] is not None and b['median_lap_seconds'] is not None
            and c['median_lap_seconds']<=b['median_lap_seconds']*1.05),
        meaningful_improvement=c['completions']>b['completions'] or c500>b500
            or (c['median_progress_m'] is not None and b['median_progress_m'] is not None
                and c['median_progress_m']>b['median_progress_m']+1))
    # Sparse-cell sensitivity must agree before any switch.
    keys={k for k in m['matched_bins'] if all(sum(r.get('bins',{}).get(k,{}).get('distance_m',0)
        for r in g)>=100 for g in groups.values())}
    def filtered(g):return [dict(r,bins={k:v for k,v in r.get('bins',{}).items() if k in keys}) for r in g]
    dense=common_metrics(filtered(groups['latest']),filtered(groups['lap']))
    checks['coverage_check']=bool(dense['enough'] and
        dense['candidate']['steering_per_100m']<=dense['baseline']['steering_per_100m'] and
        dense['candidate']['lateral_rms']<=dense['baseline']['lateral_rms'])
    return dict(passed=all(checks.values()),checks=checks,incumbent=b,candidate=c,
        valid_500=dict(incumbent=b500,candidate=c500),weaving=m,weaving_dense=dense)


def validate_config(cfg,seconds, *, clock=None):
    deadline=deadline_timestamp(cfg)
    bounded=(cfg.get('schema')=='gamepad_champion_cycle_v1' and type(cfg.get('rounds')) is int and cfg['rounds']==2)
    if deadline is not None:
        now = clock.wall() if clock is not None else time.time()
        bounded=0<deadline-now<=MAX_DEADLINE_SECONDS and 'rounds' not in cfg
    if (not bounded
            or cfg.get('training_seconds')!=3600 or cfg.get('comparison_seconds')!=2700
            or not math.isfinite(seconds) or not 0<seconds<=(MAX_DEADLINE_SECONDS if deadline else MAX_SECONDS)):
        raise ValueError('Champion loop requires bounded 1h rounds and an authorized count or absolute deadline')
    verify_models({'incumbent':cfg['incumbent']})


def run(args,control,emit, *, dependencies=None):
    supplied_dependencies = dependencies
    dependencies = dependencies or RunDependencies()
    clock = dependencies.time_source()
    cfg=read(args.config);validate_config(cfg,float(args.seconds),clock=clock)
    out=new_folder('reinforcement_runs','gamepad-champion-cycle');save(out/'config.json',cfg)
    deadline=deadline_timestamp(cfg)
    started=clock.monotonic();budget=TimeBudget(min(float(args.seconds),MAX_SECONDS) if deadline is None else float(args.seconds),clock=clock,deadline=deadline,started=started);done=threading.Event()
    records=[];champion=dict(cfg['incumbent']);reason='incomplete';phase='preparing'
    rejected=set(cfg.get('rejected_candidates',[]));last_session=None
    def expired():return budget.expired()
    def check():
        if expired():control.cancel('deadline_reached' if deadline else 'cycle_time_limit')
        check_cancelled(control)
    def watchdog():
        while not done.wait(.02):
            if (dependencies.f8_down or f8_down)():control.cancel('f8')
            if expired():control.cancel('deadline_reached' if deadline else 'cycle_time_limit')
            control.stopped()
    thread=threading.Thread(target=watchdog,daemon=True);thread.start()
    def progress(**kw):
        save(out/'status.json',dict(phase=phase,champion=champion,rounds=records,**kw),replace=True)
        emit('progress',result_dir=str(out),phase='champion_'+phase,**kw)
    current_child=None
    def phase_emit(event,**kw):
        nonlocal current_child,last_session
        child=kw.pop('result_dir',None)
        if child and child!=current_child:
            current_child=child
            save(out/'child.json',dict(phase=phase,round=len(records)+1,path=str(child)),replace=True)
            if Path(child).name.startswith(('gamepad-online-session-','gamepad-policy-comparison-')):
                last_session=Path(child)
                save(out/f'{phase}-{len(records)+1}.json',dict(path=str(child)),replace=True)
        emit(event,**dict(kw,result_dir=str(out),child_result_dir=child,cycle_phase=phase,cycle_round=len(records)+1))
    try:
        save(out/'champion.json',champion)
        for number in (count(1) if deadline else range(1,cfg['rounds']+1)):
            check();phase='training';verify_models({'champion':champion});progress(cycle_round=number)
            child=PhaseControl(control)
            train_args=SimpleNamespace(config=cfg['training_config'],task='gamepad-online',resume=champion['path'],
                resume_exact=True,model=None,seconds=cfg['training_seconds'])
            training,_,training_reason=(dependencies.train or train_run)(train_args,child,phase_emit,**({'dependencies': dependencies} if dependencies.train is None and supplied_dependencies is not None else {}))
            check()
            if training_reason!='session_time_limit' or child.reason!='session_time_limit':
                raise ValueError('Training did not end at its authorized hour: '+training_reason)
            boundary=verify_boundary(training)
            record=dict(round=number,starting_champion=champion,training=str(training),training_reason=training_reason,boundary=boundary)
            phase='shortlisting';progress(cycle_round=number)
            pool=candidates(training,cfg.get('historical_evidence',[]),champion,check)
            save(out/f'candidates-{number}.json',pool)
            pool=[p for p in pool if p['model_sha256'] not in rejected]
            if pool:
                challenger={k:pool[0][k] for k in ('path','model_sha256')}
                phase='selection';progress(cycle_round=number)
                config=dict(schema='gamepad_champion_pair_v1',models=dict(lap=champion,latest=challenger),
                    seed=cfg['seed']+number,trial_seconds=180,session_seconds=cfg['comparison_seconds'],training_config=cfg['training_config'])
                path=out/f'comparison-{number}.json';save(path,config)
                comparison,complete,comparison_reason=(dependencies.compare or compare_run)(SimpleNamespace(config=str(path),seconds=cfg['comparison_seconds']),PhaseControl(control),phase_emit,**({'dependencies': dependencies} if dependencies.compare is None and supplied_dependencies is not None else {}))
                check()
                if not complete or comparison_reason!='paired_trials_finished':
                    raise ValueError('Selection incomplete; cycle stopped: '+comparison_reason)
                verify_boundary(comparison);report=read(Path(comparison)/'report.json')
                if report['policy_updates'] or report['optimizer_updates']:raise ValueError('Selection modified policy state')
                decision=selection_decision(report['records']);save(out/f'decision-{number}.json',decision)
                record.update(comparison=str(comparison),challenger=challenger,decision=decision)
                if decision['passed']:
                    verify_models({'challenger':challenger});champion=challenger
                else:rejected.add(challenger['model_sha256'])
            else:record['selection_reason']='no_new_completion_candidate'
            record['selected_champion']=champion;records.append(record)
            save(out/'rounds.json',records,replace=True);save(out/'champion.json',champion,replace=True)
            phase='round_complete';progress(cycle_round=number)
        reason='two_rounds_finished'
    except InterruptedError as error:reason=str(error)
    except Exception as error:
        reason=f'{type(error).__name__}: {error}';control.cancel('cycle_error')
        save(out/'failure.json',dict(reason=reason,phase=phase),replace=True)
    finally:
        done.set();thread.join()
        final=dict(kind='gamepad_champion_cycle',reason=reason,phase=phase,rounds=records,
            champion=champion,elapsed_seconds=clock.monotonic()-started,auto_resume=False,
            deadline=cfg.get('deadline'),rejected_candidates=sorted(rejected),last_session=str(last_session) if last_session else None)
        if reason=='deadline_reached' and last_session:
            try:final['final_stop_proof']=verify_boundary(last_session)
            except Exception as error:final['final_stop_proof_error']=str(error)
        save(out/'report.json',final);finish(out,final,complete=reason=='two_rounds_finished')
    return out,reason=='two_rounds_finished',reason
