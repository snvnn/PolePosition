"""Frozen-policy randomized comparisons; evaluation never commits training replay."""
import json
import math
import random
import statistics
import time
import html
from collections import defaultdict
from pathlib import Path
from ModelTrainer.common import digest,hashes,verify_hashes,finish,new_folder
from .gamepad_online_io import read,save
from .gamepad_session_config import configuration,verify_runtime_binding,drive_config
from .gamepad_runtime import Native
from .run_dependencies import RunDependencies
from ExecutionRuntime.timing import TimeBudget
from .gamepad_online_data import validate_stop
from .acc_reset import AutoReset
from .gamepad_recovery import relocation,MAX_RECOVERIES
from GamepadIO.run_report import inspect_log

NORMAL=('invalid_lap','progress_stall','evaluation_complete','time_limit')

def prepare_focus(check,progress, *, clock=None):
    clock = clock or RunDependencies().time_source()
    # Give the operator time to leave the controller window. Cancellation and
    # the whole-session deadline remain active throughout preparation.
    for remaining in range(10,0,-1):
        check();progress(phase='comparison_focus',countdown=remaining)
        for _ in range(10):check();clock.sleep(.1)

def schedule(labels,repeats,seed):
    rng=random.Random(seed);out=[]
    for repeat in range(repeats):
        order=list(labels);rng.shuffle(order)
        out.extend(dict(model=label,repeat=repeat+1) for label in order)
    return out

def verify_models(models):
    inventory={}
    for label,item in models.items():
        folder=Path(item['path']);m=read(folder/'manifest.json')
        if not m.get('complete') or not m.get('parity_passed'):raise ValueError('Unverified policy: '+label)
        verify_hashes(m['output_sha256'],folder)
        if digest(folder/'model.json')!=item['model_sha256']:raise ValueError('Pinned model changed: '+label)
        inventory.update(hashes([folder/'manifest.json',folder/'model.json',folder/'checkpoint.pt']))
    return inventory


def crossed_valid_500(rows,end_ns):
    starts=[r['start'] for r in rows if r['kind']=='episode_start']
    if len(starts)!=1:return None
    start=starts[0];previous=None;base=None
    for r in rows:
        if r['kind']!='observation':continue
        t=r['observation_ns']
        if t<start['observation_ns']:continue
        if t>=end_ns:return False
        st=r['state']
        if (st['status']!='ready' or st['tracking_epoch']!=start['tracking_epoch']
                or (previous is not None and not 0<t-previous<=50_000_000)):
            return None
        if not r['sample']['lap_valid']:return False
        distance=st.get('accumulated_m')
        if distance is None or not math.isfinite(distance):return None
        if base is None:
            if t!=start['observation_ns']:return None
            base=distance
        if distance-base>500:return True
        previous=t
    return None

def analyze_trial(run,expected_hash):
    run=Path(run);m=read(run/'manifest.json');s=read(run/'summary.json')
    if not m.get('complete') or not m.get('policy_comparison') or not s.get('policy_comparison'):
        raise ValueError('Not a completed policy-comparison run')
    verify_hashes(m['output_sha256'],run)
    if s['model_sha256']!=expected_hash:raise ValueError('Trial policy differs')
    rows=[]
    with (run/'samples.jsonl').open(encoding='utf-8') as stream:
        for line in stream:rows.append(json.loads(line))
    validate_stop(s,rows)
    end=s['episode_end'];reason=end['reason'];t=end['observation_ns']
    if reason not in NORMAL:raise ValueError('Non-comparison stop: '+reason)
    if reason!='time_limit' and (not end.get('event') or end['event']['reason']!=reason):
        raise ValueError('Unconfirmed trial ending')
    prefix=[r for r in rows if (r['kind']=='observation' and r['observation_ns']<=t)
        or (r['kind']=='policy' and r['now_ns']<t) or (r['kind']=='sent' and r['send_end_ns']<t)]
    transport,_=inspect_log(prefix)
    if not transport['passed']:raise ValueError('Trial transport failed: '+str(transport))
    policies=[r for r in prefix if r['kind']=='policy' and r['mode']=='Running']
    if not policies or any(r['authority']!='model' or any(r['label_mask']) for r in policies):
        raise ValueError('Trial was not autonomous')
    obs={r['observation_ns']:r for r in prefix if r['kind']=='observation'}
    bins={};previous=None
    for p in policies:
        o=obs.get(p['observation_ns']);f=p['features']
        if not o or o['state']['status']!='ready':previous=None;continue
        key=f"{int(o['state']['s_m']//50)}:{int(o['sample']['speed_kmh']//20)}"
        straight=abs(f[8])<.001 and abs(f[9])<.001 and o['sample']['speed_kmh']>=80
        if previous and straight:
            pp,po,pk,ps=previous;dt=(o['observation_ns']-po['observation_ns'])/1e9
            distance=math.hypot(o['sample']['position_x']-po['sample']['position_x'],o['sample']['position_z']-po['sample']['position_z'])
            if ps and key==pk and 0<dt<=.05 and 0<distance<=5 and o['state']['tracking_epoch']==po['state']['tracking_epoch']:
                b=bins.setdefault(key,dict(distance_m=0.,seconds=0.,steering_variation=0.,lateral_sq_time=0.,samples=0))
                b['distance_m']+=distance;b['seconds']+=dt;b['samples']+=1
                b['steering_variation']+=abs(p['command_action'][0]-pp['command_action'][0])
                b['lateral_sq_time']+=f[24]**2*dt
        previous=p,o,key,straight
    lap_times=s['episode'].get('lap_times_s',[])
    complete=reason=='evaluation_complete' and s['episode']['valid_laps']==1
    if complete and (len(lap_times)!=1 or not math.isfinite(lap_times[0]) or lap_times[0]<=0):
        raise ValueError('Missing measured complete lap time')
    return dict(run=str(run),model_sha256=expected_hash,reason=reason,completed=complete,
        valid_laps=s['episode']['valid_laps'],progress_m=s['episode']['progress'],
        lap_seconds=lap_times[0] if complete else None,bins=bins,excluded=False,evaluation_only=True,
        valid_over500=crossed_valid_500(rows,t),
        stop_kind=s['shutdown']['kind'],manifest_sha256=digest(run/'manifest.json'))

def aggregate(rows):
    usable=[r for r in rows if not r.get('excluded')]
    laps=[r['lap_seconds'] for r in usable if r['completed']]
    return dict(attempts=len(rows),usable=len(usable),completions=len(laps),
        median_progress_m=statistics.median([r['progress_m'] for r in usable]) if usable else None,
        median_lap_seconds=statistics.median(laps) if laps else None,
        progress_m=[r['progress_m'] for r in usable])

def common_metrics(candidate,baseline):
    def combine(rows):
        bins={}
        for r in rows:
            if r.get('excluded'):continue
            for k,v in r['bins'].items():
                b=bins.setdefault(k,dict(distance_m=0.,seconds=0.,steering_variation=0.,lateral_sq_time=0.,trials=0))
                for field in ('distance_m','seconds','steering_variation','lateral_sq_time'):b[field]+=v[field]
                b['trials']+=1
        return bins
    c,b=combine(candidate),combine(baseline)
    keys=sorted(k for k in c.keys() & b.keys() if min(c[k]['trials'],b[k]['trials'])>=3)
    def metrics(data):
        distance=sum(data[k]['distance_m'] for k in keys);seconds=sum(data[k]['seconds'] for k in keys)
        return dict(distance_m=distance,seconds=seconds,
            steering_per_100m=statistics.mean(data[k]['steering_variation']/data[k]['distance_m']*100 for k in keys) if keys else None,
            lateral_rms=math.sqrt(statistics.mean(data[k]['lateral_sq_time']/data[k]['seconds'] for k in keys)) if keys else None)
    cm,bm=metrics(c),metrics(b)
    enough=bool(keys) and min(cm['distance_m'],bm['distance_m'])>=100 and min(cm['seconds'],bm['seconds'])>=5
    return dict(enough=enough,matched_bins=keys,candidate=cm,baseline=bm,
        matching='50 m path bins, 20 km/h speed bins; curvature < 0.001 /m, speed >= 80 km/h; >=3 trials/bin')

def decision(candidate,baseline):
    c,b=aggregate(candidate),aggregate(baseline);m=common_metrics(candidate,baseline)
    complete=c['usable']==b['usable']==5 and c['attempts']==b['attempts']==5
    laps=c['completions']>=3 and c['completions']>=b['completions']
    distance=c['median_progress_m'] is not None and b['median_progress_m'] is not None and c['median_progress_m']>=b['median_progress_m']
    # At least three complete laps on both sides to compare median lap time.
    time_ok=c['completions']>=3 and b['completions']>=3 and c['median_lap_seconds']<=1.05*b['median_lap_seconds']
    smooth=m['enough'] and m['baseline']['steering_per_100m']>0 and m['baseline']['lateral_rms']>0 and (
        m['candidate']['steering_per_100m']<=.8*m['baseline']['steering_per_100m']
        and m['candidate']['lateral_rms']<=.9*m['baseline']['lateral_rms'])
    checks=dict(complete_comparison=complete,completion_rate=laps,progress=distance,lap_time=time_ok,weaving=smooth)
    return dict(passed=all(checks.values()),checks=checks,candidate=c,baseline=b,common_metrics=m)

def select_baseline(records,labels):
    def rank(label):
        rows=[r for r in records if r['model']==label];a=aggregate(rows)
        # Equal completion/distance: use per-run steering variation; then stable label order.
        variation=sum(v['steering_variation'] for r in rows if not r.get('excluded') for v in r['bins'].values())
        distance=sum(v['distance_m'] for r in rows if not r.get('excluded') for v in r['bins'].values())
        return (-a['completions'],-(a['median_progress_m'] or 0),variation/max(distance,1e-9),label)
    return min(labels,key=rank)

def promote(out,cfg,result,models,inventory):
    if not result['passed']:return False
    verify_hashes(inventory)
    target=Path(cfg['preset'])
    if digest(target)!=cfg['preset_sha256']:raise ValueError('Preset changed during comparison; promotion withheld')
    old=read(target);save(out/'preset-before.json',old)
    candidate=Path(models['candidate']['path'])
    updated=dict(old,checkpoint=str(candidate))
    save(target,updated,replace=True);save(out/'promotion.json',dict(preset=str(target),before=old,after=updated))
    return True

def write_report(out,report):
    names=dict(initial='초기 B',best='기존 완주 모델',latest='최근 저장 모델',control='추가 학습 대조군',candidate='평활화 후보')
    body=[]
    for phase in ('screening','confirmation'):
        body.append('<h2>'+('초기 비교' if phase=='screening' else '추가 확인')+'</h2><table><tr><th>모델</th><th>유효 완주 / 시도</th><th>진행 중앙값(m)</th><th>완주 랩타임 중앙값(초)</th></tr>')
        for label in names:
            rows=[r for r in report['records'] if r['phase']==phase and r['model']==label]
            if not rows:continue
            a=aggregate(rows)
            def number(v):return '자료 부족' if v is None else f'{v:.2f}'
            body.append(f"<tr><td>{names[label]}</td><td>{a['completions']} / {a['attempts']}</td><td>{number(a['median_progress_m'])}</td><td>{number(a['median_lap_seconds'])}</td></tr>")
        body.append('</table>')
    for r in report['records']:
        if r.get('excluded'):body.append('<p>제외: '+html.escape(str(r))+'</p>')
    decision=report.get('decision')
    if decision:body.append('<h2>채택 검사</h2><pre>'+html.escape(json.dumps(decision,ensure_ascii=False,indent=2))+'</pre>')
    text='<!doctype html><meta charset="utf-8"><title>정책 비교 평가</title><style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:20px}table{border-collapse:collapse;width:100%}td,th{padding:12px;border-bottom:1px solid #ccc;text-align:left}pre{white-space:pre-wrap}</style>'
    text+='<h1>정책 비교 평가</h1><p>'+html.escape(report['reason'])+'</p><p>평가 중 모델·optimizer 갱신: 0회. 미검증 결과는 채택하지 않습니다.</p>'+''.join(body)
    (out/'report.html').write_text(text,encoding='utf-8')

def run(args,control,emit, *, dependencies=None):
    dependencies = dependencies or RunDependencies()
    clock = dependencies.time_source()
    cfg=read(args.config)
    pair_only=cfg.get('schema') in ('gamepad_corner_pair_v1','gamepad_champion_pair_v1')
    if pair_only:
        if set(cfg['models'])!={'lap','latest'} or cfg.get('preset') or cfg['trial_seconds']!=180:
            raise ValueError('Invalid two-policy corner comparison')
    elif cfg.get('schema')!='gamepad_policy_comparison_v1' or cfg['screening_repeats']!=4 or cfg['confirmation_repeats']!=5 or cfg['trial_seconds']!=180:
        raise ValueError('Invalid fixed comparison protocol')
    seconds=min(float(args.seconds),float(cfg['session_seconds']))
    if not 0<seconds<=7200:raise ValueError('Comparison time limit must be <= 7200 seconds')
    out=new_folder('reinforcement_runs','gamepad-policy-comparison');save(out/'config.json',cfg)
    native_factory = dependencies.native_factory or Native
    native_options = {'clock': dependencies.clock} if dependencies.clock is not None and dependencies.native_factory is None else {}
    native=native_factory(out,control,emit,seconds,**native_options);started=clock.monotonic();records=[];result=None;reason='incomplete';inventory={}
    budget=TimeBudget(seconds,clock=clock,started=started)
    progress=lambda **kw:emit('progress',result_dir=str(out),**kw)
    def check():
        budget.cancel_if_expired(control,'session_time_limit')
        native.check()
    try:
        models=cfg['models']
        if cfg.get('preset'):cfg['preset_sha256']=digest(cfg['preset'])
        if not pair_only and set(models)!={'initial','best','latest','control','candidate'}:raise ValueError('Comparison requires five pinned models')
        inventory=verify_models(models);check()
        c,template,binding=configuration(cfg['training_config']);profile=Path(c['automatic_reset']['profile']);profile_hash=digest(profile)
        if pair_only:
            repeats=5 if cfg['schema']=='gamepad_champion_pair_v1' else 10
            starts=(['lap','latest']*5)[:repeats]
            random.Random(cfg['seed']).shuffle(starts)
            first=[dict(model=label,repeat=i+1) for i,lead in enumerate(starts)
                   for label in (lead,'latest' if lead=='lap' else 'lap')]
        else:
            first=schedule(list(models),4,cfg['seed'])
        save(out/'screening-schedule.json',first)
        prepare_focus(check,progress,**({'clock': clock} if dependencies.clock is not None else {}))
        number=0;recoveries=0;need_garage=False
        phases=[('paired',first)] if pair_only else [('screening',first),('confirmation',None)]
        for phase,order in phases:
            if order is None:
                baseline=select_baseline(records,[k for k in models if k!='candidate'])
                order=schedule([baseline,'candidate'],5,cfg['seed']+1)
                save(out/'confirmation-schedule.json',dict(baseline=baseline,trials=order))
            for trial in order:
                check();verify_hashes(inventory);verify_runtime_binding(binding,template)
                if digest(profile)!=profile_hash:raise ValueError('Reset profile changed')
                number+=1;label=trial['model'];model=Path(models[label]['path'])
                progress(phase='comparison_reset',episode=number,comparison_model=label)
                runpath=None
                try:
                    reset=(dependencies.reset_factory or AutoReset)(profile,check,progress)
                    receipt=reset.reset(out/f'reset-{number:03d}',require_garage=True) if need_garage else reset.reset(out/f'reset-{number:03d}')
                    need_garage=False
                    check()
                    config=drive_config(out/f'drive-{number}.toml',dict(template,policy_comparison=True,allow_verified_relocation_recovery=True),model,False,number==1,evaluation_laps=1)
                    runpath=native.drive(number,config,dict(binding,**hashes([config,model/'model.json',model/'manifest.json'])),receipt)
                    check();record=analyze_trial(runpath,models[label]['model_sha256']);verify_hashes(inventory)
                except Exception as error:
                    try:
                        recovery=relocation(runpath,models[label]['model_sha256'],True) if runpath and not control.stopped() else None
                    except Exception as validation_error:
                        recovery=None;error=validation_error
                    if recovery and recoveries<MAX_RECOVERIES:
                        verify_hashes(inventory);recoveries+=1;need_garage=True
                        recovery.update(model=label,phase=phase,repeat=trial['repeat'],recovery_number=recoveries)
                        records.append(recovery);save(out/'trials.json',records,replace=True)
                        progress(phase='relocation_recovery',episode=number,comparison_model=label,recovery_number=recoveries)
                        continue
                    if recovery:error=ValueError('Verified relocation recovery limit reached')
                    records.append(dict(model=label,phase=phase,repeat=trial['repeat'],excluded=True,reason=str(error),run=str(runpath)))
                    save(out/'trials.json',records,replace=True);raise error
                record.update(model=label,phase=phase,repeat=trial['repeat']);records.append(record)
                save(out/'trials.json',records,replace=True)
                progress(phase='comparison_saved',episode=number,comparison_model=label,valid_laps=record['valid_laps'],progress_m=record['progress_m'])
        if pair_only:
            check();verify_hashes(inventory);reason='paired_trials_finished'
        else:
            confirmation=[r for r in records if r['phase']=='confirmation']
            result=decision([r for r in confirmation if r['model']=='candidate'],[r for r in confirmation if r['model']==baseline])
            result['checks']['no_unattributed_relocation']=recoveries==0
            result['passed']=all(result['checks'].values())
            result['baseline_model']=baseline;save(out/'decision.json',result)
            check();result['promoted']=promote(out,cfg,result,models,inventory)
            save(out/'decision.json',result,replace=True);reason='passed' if result['passed'] else 'not_accepted'
    except InterruptedError as error:reason=str(error)
    except Exception as error:
        reason=f'{type(error).__name__}: {error}';control.cancel('comparison_error')
        save(out/'failure.json',dict(reason=reason),replace=True)
    finally:
        native.close()
        report=dict(kind='gamepad_policy_comparison',evaluation_only=True,reason=reason,records=records,
                    decision=result,elapsed_seconds=clock.monotonic()-started,input_sha256=inventory,
                    policy_updates=0,optimizer_updates=0,live_stability_passed=bool(result and result['passed']))
        complete=reason in ('passed','not_accepted','paired_trials_finished')
        save(out/'report.json',report)
        if not pair_only:write_report(out,report)
        finish(out,report,complete=complete)
    return out,complete,reason
