"""Reconstruct transitions only from acknowledged commands and native decision boundaries."""
import bisect
import json
import math
from pathlib import Path
import numpy as np
from ModelAndActuator.report import verify, read_rows
from ModelTrainer.common import read, save, hashes, new_folder, finish

TASK_FAILURES={'path_distance_limit','heading_limit','invalid_lap','stalled'}
TRAINABLE=TASK_FAILURES|{'lap_complete','episode_timeout'}
DEFAULT_REWARD=dict(time=.1,lateral=.2,heading=.1,input_change=.01,finish=10.,failure=-10.)

def transitions(run, coefficients=None, verification=False):
    run=Path(run)
    m,s,commands,telemetry,_=verify(run)
    if m.get('complete') is not True or m.get('mock') is not False or not m['config'].get('reinforcement'):
        raise ValueError('Require a complete real reinforcement run')
    if (not all(s.get(k) is True for k in ['normal_shutdown','recording_valid','stop_confirmed','released'])
        or s.get('dropped') != 0 or s.get('rejected_commands') != 0 or s.get('device_error') or s.get('log_error')
        or s.get('released_while_halted') or s.get('termination_reason') != 'scenario_complete'):
        raise ValueError('Episode lacks valid stop/release/recording or was interrupted')
    reason=s['controller_exit_reason']
    if reason not in ({'verification_complete'} if verification else TRAINABLE):
        raise ValueError(f'Non-trainable episode: {reason}')
    policy=read_rows(run/'policy.csv',('features_json','observation_sequence','control_source'))
    if len(policy)<2 or policy[-1]['control_source']!='model_terminal' or any(p['control_source']!='model' for p in policy[:-1]):
        raise ValueError('Missing continuous model control or real terminal observation')
    if reason != policy[-1]['policy_reason']: raise ValueError('Terminal reason mismatch')
    c=dict(DEFAULT_REWARD if coefficients is None else coefficients)
    if set(c)!=set(DEFAULT_REWARD) or any(not math.isfinite(v) for v in c.values()): raise ValueError('Invalid reward coefficients')
    tmap={int(t['sequence']):t for t in telemetry}
    sends=[x for x in commands if x['send_success']=='true' and x['sequence'] and x['applied_steering']]
    times=[int(x['now_ns']) for x in sends]
    ack={}
    for x in sends:
        if x['mode']=='Running': ack.setdefault(int(x['observation_sequence']),x)
    states=[]
    for p in policy:
        t=tmap[int(p['observation_sequence'])]
        ns=int(p['observation_ns']); values=np.asarray(json.loads(p['features_json']),np.float32)
        if values.shape!=(35,) or not np.isfinite(values).all() or t['observation_ns']!=p['observation_ns'] or t['tracking_epoch']!=p['tracking_epoch']:
            raise ValueError('Invalid policy observation provenance')
        previous=bisect.bisect_left(times,ns)-1
        if previous<0: raise ValueError('Missing causal applied input')
        a=sends[previous]
        axes=np.array([float(a['applied_'+k]) for k in ('steering','throttle','brake')])
        if a['mode']!='Running' or a['tracking_epoch']!=p['tracking_epoch'] or ns-times[previous]>=100_000_000 or not np.allclose(values[19:22],axes,atol=1e-6,rtol=0):
            raise ValueError('Feature applied-input provenance mismatch')
        states.append((p,t,values,axes))
    first,last=int(policy[0]['observation_ns']),int(policy[-1]['observation_ns'])
    epoch=policy[0]['tracking_epoch']
    span=[t for t in telemetry if first<=int(t['observation_ns'])<=last]
    for i,t in enumerate(span):
        if t['tracking_epoch']!=epoch or t['state_status']!='ready': raise ValueError('Discontinuous episode')
        if i and not 0<int(t['observation_ns'])-int(span[i-1]['observation_ns'])<100_000_000:
            raise ValueError('Telemetry gap/reversal')
    result=[]
    for (p,t,x,axes),(q,u,y,next_axes) in zip(states,states[1:]):
        source=int(p['observation_sequence']); ns=int(p['observation_ns']); next_ns=int(q['observation_ns'])
        a=ack.get(source)
        if a is None or not ns<=int(a['now_ns'])<next_ns or int(q['observation_sequence'])<=source:
            raise ValueError('No acknowledged action before the next observation')
        if not 0<next_ns-ns<100_000_000: raise ValueError('Policy transition gap')
        action=np.array([float(p['prediction_steering']),float(p['prediction_u'])],np.float32)
        if not np.isfinite(action).all() or np.any(np.abs(action)>1): raise ValueError('Invalid policy action')
        b=m['config']['baseline']['config']
        requested=[float(np.clip(action[0],-b['max_steering'],b['max_steering'])),
                   min(max(float(action[1]),0),b['max_throttle']),min(max(-float(action[1]),0),b['max_brake'])]
        if not np.allclose(requested,[float(a['requested_'+k]) for k in ('steering','throttle','brake')],atol=1e-6,rtol=0):
            raise ValueError('Policy action did not control the command')
        dt=(next_ns-ns)/1e9
        ds=float(u['accumulated_m'])-float(t['accumulated_m'])
        if abs(ds)>20/3.6*dt+.5: raise ValueError('Implausible progress/teleport')
        terminal=q['control_source']=='model_terminal'
        bonus=(c['finish'] if reason=='lap_complete' else c['failure'] if reason in TASK_FAILURES else 0.) if terminal else 0.
        parts=dict(progress=ds,time=-c['time']*dt,lateral=-c['lateral']*float(u['lateral_error_m'])**2*dt,
                   heading=-c['heading']*float(u['heading_error_rad'])**2*dt,
                   input_change=-c['input_change']*float(np.square(next_axes-axes).sum()),terminal=bonus)
        reward=sum(parts.values())
        if not math.isfinite(reward): raise ValueError('Nonfinite reward')
        result.append(dict(observation=x.tolist(),action=action.tolist(),reward=reward,next_observation=y.tolist(),
                           terminated=terminal and reason!='episode_timeout',truncated=terminal and reason=='episode_timeout',
                           source_sequence=source,next_sequence=int(q['observation_sequence']),observation_ns=ns,
                           applied_ns=int(a['now_ns']),next_observation_ns=next_ns,reward_parts=parts,reason=reason if terminal else ''))
    # Every successful model command is accounted for; no missing decision may hide in a latest-value channel.
    known={int(p['observation_sequence']) for p in policy[:-1]}
    if any(int(a['observation_sequence']) not in known for a in sends if a['mode']=='Running' and int(a['now_ns'])>=int(ack[int(policy[0]['observation_sequence'])]['now_ns']) and int(a['now_ns'])<last):
        raise ValueError('Unlogged model command')
    return result,s,m

def save_experience(run, rows, parent, coefficients):
    out=new_folder(parent,'sac-experience')
    m=dict(schema_version=1,kind='sac_experience',complete=False,input_sha256=hashes([Path(run)/'manifest.json']),
           reward=coefficients,transitions=len(rows))
    save(out/'manifest.json',m)
    with (out/'transitions.jsonl').open('x',encoding='utf-8') as f:
        for row in rows: f.write(json.dumps(row,allow_nan=False)+'\n')
    finish(out,m)
    return out

def add_replay(model, rows, normalizer):
    mean,std=np.asarray(normalizer['mean'],np.float32),np.asarray(normalizer['std'],np.float32)
    for row in rows:
        x=(np.asarray(row['observation'],np.float32)-mean)/std
        y=(np.asarray(row['next_observation'],np.float32)-mean)/std
        if not np.isfinite(x).all() or not np.isfinite(y).all(): raise ValueError('Invalid normalized replay')
        done=row['terminated'] or row['truncated']
        model.replay_buffer.add(x[None],y[None],np.asarray([row['action']],np.float32),np.array([row['reward']]),
                               np.array([done]),[{'TimeLimit.truncated':row['truncated']}])
        model.num_timesteps+=1
