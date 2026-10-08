"""Independent finish-line proof and time-weighted driving metrics; no replay writes."""
from pathlib import Path
import json
import math
import numpy as np
from ModelTrainer.common import digest, read
from ModelAndActuator.report import verify, read_rows
from .evaluation import LapEvaluation, ControlSpan


def metrics(telemetry, commands, start, end):
    rows=[r for r in telemetry if start<=int(r['observation_ns'])<=end]
    if len(rows)<2 or end<=start: raise ValueError('Insufficient measured interval')
    times=np.array([int(r['observation_ns']) for r in rows],dtype=np.int64)
    dt=np.diff(times)/1e9
    if np.any(dt<=0) or np.any(dt>=.1): raise ValueError('Discontinuous metric observations')
    def values(key): return np.array([float(r[key]) for r in rows])
    lateral=values('lateral_error_m'); heading=values('heading_error_rad');speed=values('speed_kmh')
    def mean(x): return float(np.sum((x[1:]+x[:-1])*.5*dt)/dt.sum())
    sent=[r for r in commands if r['send_success']=='true' and r['mode']=='Running' and start<=int(r['now_ns'])<=end]
    axes=np.array([[float(r['applied_'+k]) for k in ('steering','throttle','brake')] for r in sent])
    return dict(duration_seconds=(end-start)/1e9,progress_m=float(rows[-1]['accumulated_m'])-float(rows[0]['accumulated_m']),
                mean_speed_kmh=mean(speed),speed_error_rmse_kmh=math.sqrt(mean((speed-10)**2)),
                lateral_rmse_m=math.sqrt(mean(lateral**2)),lateral_max_m=float(np.max(np.abs(lateral))),
                heading_rmse_deg=math.degrees(math.sqrt(mean(heading**2))),
                input_variation_per_second=(np.abs(np.diff(axes,axis=0)).sum(axis=0)/((end-start)/1e9)).tolist() if len(axes)>1 else None)


def evaluate_run(run):
    # Called only after transitions() has independently checked every action,
    # acknowledgement and terminal observation in this immutable run.
    m,s,commands,telemetry,events=verify(run)
    rl=m['config']['reinforcement']
    if not rl.get('evaluation') or rl.get('deterministic') is not True: raise ValueError('Require frozen deterministic lap evaluation')
    policy=read_rows(Path(run)/'policy.csv',('features_json','observation_sequence'))
    model=[r for r in policy if r['control_source']=='model']
    if not model or policy[-1]['control_source']!='model_terminal': raise ValueError('Missing model span')
    source=int(model[0]['observation_sequence'])
    first=next(int(c['now_ns']) for c in commands if c['send_success']=='true' and c['mode']=='Running' and c['observation_sequence']==str(source))
    end=int(policy[-1]['observation_ns'])
    policy_hash=digest(Path(rl['bundle'])/'model.json')
    monitor=LapEvaluation(digest(Path(run)/'manifest.json'),policy_hash,10)
    span=ControlSpan(first,end,policy_hash,'model','model',True,0,0)
    for t in telemetry:
        ns=int(t['observation_ns'])
        if first<=ns<=end:
            row=dict(elapsed_ns=ns,segment_id=int(t['segment_id']),completed_laps=int(t['completed_laps']),
                     lap_time_ms=int(t['lap_time_ms']),lap_valid=t['lap_valid']=='true',progress=float(t['progress']))
            monitor.observe(row,span,continuous=t['state_status']=='ready')
    laps=[dict(start_ns=l.start_ns,end_ns=l.end_ns,valid=l.valid,reasons=list(l.reasons),
               metrics=metrics(telemetry,commands,l.start_ns,l.end_ns)) for l in monitor.laps]
    valid=[l for l in laps if l['valid']]
    if s['controller_exit_reason']=='lap_complete' and len(valid)!=1: raise ValueError('Native completion lacks independent full-lap proof')
    conditions=[json.loads(e['detail']) for e in events if e['kind']=='rl_conditions']
    return dict(schema_version=1,kind='lap_evaluation',run=str(Path(run).resolve()),policy_sha256=policy_hash,
                prepared_sha256=read(Path(rl['bundle'])/'manifest.json')['prepared_sha256'],
                speed_target_kmh=10,deterministic=True,learning_updates=0,replay_insertions=0,
                reason=s['controller_exit_reason'],completed_valid_laps=len(valid),laps=laps,
                controlled_interval=metrics(telemetry,commands,first,end),conditions=conditions,
                limitations='One lap is a measured outcome, not statistical proof of improvement. Tyre/road temperature is not recorded.')


def compare(before,after):
    if before['prepared_sha256']!=after['prepared_sha256'] or before['speed_target_kmh']!=after['speed_target_kmh']:
        raise ValueError('Different comparison path or speed stage')
    a=[l for l in before['laps'] if l['valid']];b=[l for l in after['laps'] if l['valid']]
    if len(a)!=1 or len(b)!=1: return dict(comparable=False,reason='Both policies need a valid full lap')
    a,b=a[0]['metrics'],b[0]['metrics']
    fields=['duration_seconds','lateral_rmse_m','heading_rmse_deg','speed_error_rmse_kmh']
    ac,bc=before.get('conditions',[]),after.get('conditions',[])
    same_conditions=bool(ac and bc) and {c['rain_intensity'] for c in ac}=={c['rain_intensity'] for c in bc} and ac[0]['session_type']==bc[0]['session_type'] and abs(ac[0]['fuel_litres']-bc[0]['fuel_litres'])<=1 and abs(ac[0]['clock_seconds']-bc[0]['clock_seconds'])<=600
    return dict(comparable=same_conditions,reason='matched recorded conditions' if same_conditions else 'conditions differ or are missing',delta_updated_minus_initial={k:b[k]-a[k] for k in fields},
                observed_improvement=same_conditions and b['duration_seconds']<a['duration_seconds'] and b['lateral_rmse_m']<=a['lateral_rmse_m'],
                statistical_improvement_established=False)
