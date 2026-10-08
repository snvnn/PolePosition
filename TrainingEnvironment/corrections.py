"""Human labels only; model-controlled pedal axes have zero supervision weight."""
import bisect
import json
from pathlib import Path
import numpy as np
from ModelTrainer.common import read,digest,verify_hashes,hashes
from GamepadIO.run_report import inspect_log,inspect_shutdown


def label(policy):
    t=policy.get('teacher');kind=policy.get('authority')
    if not t or kind not in ('human_steer','human_full'):return None
    if not t['connected'] or t['normalization_version']!=1:raise ValueError('Disconnected correction input')
    if not policy['observation_ns']<=t['read_start_ns']<=t['read_end_ns']<=policy['now_ns']:
        raise ValueError('Correction label precedes observation or follows inference')
    if t['read_end_ns']-policy['observation_ns']>20_000_000:raise ValueError('Correction input too late')
    if not -32768<=t['lx']<=32767 or not 0<=t['rt']<=255 or not 0<=t['lt']<=255:raise ValueError('Invalid raw correction')
    buttons=t['buttons'];expected='human_full' if buttons&0x200 else 'human_steer' if buttons&0x100 else 'model'
    if buttons&0x20 or expected!=kind:raise ValueError('Correction authority/button mismatch')
    action=[t['lx']/(32768 if t['lx']<0 else 32767),t['rt']/255,t['lt']/255]
    mask=[1,1,1] if kind=='human_full' else [1,0,0]
    selected=action if kind=='human_full' else [action[0],*policy['action'][1:]]
    x=np.asarray(policy['features'])
    if x.shape!=(35,) or not np.isfinite(x).all() or policy.get('label_mask')!=mask or not np.allclose(selected,policy['selected_action'],atol=1e-7,rtol=0):
        raise ValueError('Invalid correction feature/mask/selected action')
    return dict(features=x.tolist(),action=action,mask=mask,observation_ns=policy['observation_ns'])


def load_corrections(paths,controls_hash,assists_hash):
    examples=[];inventory={};seen=set();counts=[]
    for folder in paths:
        p=Path(folder).resolve();m=read(p/'manifest.json');s=read(p/'summary.json')
        if m.get('semantics')!='gamepad_v1' or not m.get('intervention') or m.get('complete') is not True:
            raise ValueError('Completed intervention recording required')
        if not s.get('correction_eligible') or not s.get('stop_confirmed') or not s.get('released') or s.get('error') or s.get('logging_failure') or s.get('reason')!='time_limit':
            raise ValueError('Corrections require continuous normal stopping; aborted sessions need review')
        verify_hashes(m['output_sha256'],p)
        if digest(p/'samples.jsonl') in seen:raise ValueError('Duplicate correction recording')
        seen.add(digest(p/'samples.jsonl'))
        parent=Path(m['model']);pm=read(parent/'manifest.json');verify_hashes(pm['output_sha256'],parent)
        if pm.get('semantics')!='gamepad_v1' or pm.get('kind')!='gamepad_imitation' or pm.get('complete') is not True:raise ValueError('Only verified gamepad models may generate corrections')
        if pm['controls_sha256']!=controls_hash or pm['assists_sha256']!=assists_hash or digest(parent/'model.json')!=m['model_sha256']:
            raise ValueError('Correction model/profile mismatch')
        rows=[json.loads(line) for line in (p/'samples.jsonl').read_text().splitlines()]
        transport,_=inspect_log(rows)
        if not transport['passed'] or not inspect_shutdown(rows)['passed']:raise ValueError('Correction transmission/stop validation failed')
        raw=[r['input'] for r in rows if r['kind']=='human_input'];raw_by_seq={r['sequence']:r for r in raw}
        for i,r in enumerate(raw):
            if r['sequence']!=i+1 or not r['connected'] or r['slot']!=m['intervention']['teacher_slot'] or (i and r['read_start_ns']<=raw[i-1]['read_end_ns']):raise ValueError('Correction raw input missing or reversed')
        sent=[r for r in rows if r['kind']=='sent'];ends=[r['send_end_ns'] for r in sent]
        observed={r['observation_ns']:r for r in rows if r['kind']=='observation'}
        start_count=len(examples);last_t=-20_000_000
        for r in rows:
            if r['kind']!='policy' or r['mode']!='Running':continue
            example=label(r)
            if example is None:continue
            t=r['observation_ns'];teacher=r['teacher'];o=observed[t]
            if raw_by_seq.get(teacher['sequence'])!=teacher:raise ValueError('Correction raw teacher row changed')
            if o['state']['status']!='ready' or not o['sample']['lap_valid']:continue
            j=bisect.bisect_left(ends,t)-1
            if j<0 or t-ends[j]>=100_000_000:raise ValueError('Missing causal previous applied input')
            previous=[sent[j]['axes'][k] for k in ('steering','throttle','brake')]
            if not np.allclose(example['features'][19:22],previous,atol=1e-6,rtol=0):raise ValueError('Teacher input leaked into previous-action features')
            if t-last_t<20_000_000:continue
            examples.append(example);last_t=t
        if len(examples)==start_count:raise ValueError('No valid human correction labels in run')
        counts.append(dict(run=str(p),examples=len(examples)-start_count))
        inventory.update(hashes([p/'manifest.json',p/'summary.json',p/'samples.jsonl',parent/'manifest.json',parent/'model.json']))
    if len(examples)<16:raise ValueError('At least 16 human correction examples required')
    return examples,inventory,counts
