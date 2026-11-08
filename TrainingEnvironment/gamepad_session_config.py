"""Validated session inputs and native drive configuration; no process creation."""
import json
import math
import tomllib
from itertools import count
from pathlib import Path
from ModelTrainer.common import digest, hashes, verify_hashes
from .gamepad_online_io import read
from .gamepad_online_data import SEMANTICS
from .lap_gamepad import verified_controls_reference

def validate_session_limits(c):
    episodes=c['max_episodes'];seconds=c['session_seconds']
    if (type(episodes) is not int or episodes<0 or isinstance(seconds,bool)
            or not isinstance(seconds,(int,float)) or not math.isfinite(seconds)
            or not 0<seconds<=7200):
        raise ValueError('Invalid continuous gamepad session limits')


def episode_numbers(c,evaluation=False):
    if evaluation:return range(1,4)
    return count(1) if c['max_episodes']==0 else range(1,c['max_episodes']+1)


def configuration(path):
    c=tomllib.loads(Path(path).read_text(encoding='utf-8-sig'))
    if c['semantics']!=SEMANTICS:raise ValueError('Invalid continuous gamepad semantics')
    validate_session_limits(c)
    if not math.isfinite(c['evaluation_start_tolerance_m']) or c['evaluation_start_tolerance_m']<=0:
        raise ValueError('Invalid evaluation start repeatability tolerance')
    source=Path(c['source_model']).resolve()
    if digest(source/'model.json')!=c['source_policy_sha256']:
        raise ValueError('Bootstrap must be the selected B release')
    template=tomllib.loads(Path(c['drive_config']).read_text(encoding='utf-8-sig'))
    if template['seconds']!=0 or template.get('steering_smoothing_ms',0)!=0:
        raise ValueError('Continuous IQL must not alter duration or LPF')
    pm=read(source/'manifest.json')
    if Path(template['prepared']).resolve()!=Path(pm['prepared']).resolve():raise ValueError('Wrong reference path')
    profile=read(template['profile'])
    binding=hashes([c['drive_config'],template['profile'],template['guardian_config'],template['controller_config'],
        Path(template['prepared'])/'manifest.json',source/'speed-profile.json',verified_controls_reference(profile),profile['assists']])
    return c,template,binding


def verify_runtime_binding(binding,template):
    verify_hashes(binding)
    verified_controls_reference(read(template['profile']))


def drive_config(out,template,model,evaluation,automatic_initial_start=False, *, evaluation_laps=None):
    c=dict(template,model=str(model),continuous_learning=True,evaluation_laps=3 if evaluation else 0)
    if evaluation_laps is not None:c['evaluation_laps']=evaluation_laps
    if automatic_initial_start:c['automatic_initial_start']=True
    lines=[]
    for k,v in c.items():
        if not isinstance(v,dict):lines.append(f'{k} = {json.dumps(v)}')
    for k,v in c.items():
        if isinstance(v,dict):
            lines.append(f'[{k}]')
            lines.extend(f'{n} = {json.dumps(x)}' for n,x in v.items())
    out.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return out
