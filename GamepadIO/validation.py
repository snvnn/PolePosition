"""Compare stationary physical/virtual response evidence; never infer driving success."""
import argparse
import json
import hashlib
import re
from pathlib import Path
import numpy as np
from ModelTrainer.common import read,save,digest,hashes,verify_hashes


def qualify_gearing_only_change(manual_evidence,profile,output):
    """Keep the actual manual-neutral test evidence; qualify only axis transport.

    A hash-verified one-field gearing change does not establish moving equivalence.
    This explicit qualification allows new teacher collection and guarded policy
    diagnostics, not an automatic driving/performance pass.
    """
    m=read(manual_evidence);p=read(profile)
    if m.get('semantics')!='gamepad_v1' or not m.get('passed') or not m.get('numeric_passed') or m.get('visual_response_confirmed') is not True:
        raise ValueError('Verified physical/virtual manual response required')
    verify_hashes(m['evidence_sha256'])
    for key,path in [('controls_sha256',p['controls']),('dll_sha256',p['dll']),('driver_sha256',p['driver_path'])]:
        if digest(path)!=m[key]:raise ValueError('Input settings or device binaries changed')
    raw=Path(p['assists']).read_bytes()
    encoding='utf-16le' if raw.startswith(b'\xff\xfe') or raw[1:2]==b'\x00' else 'utf-8'
    text=raw.decode(encoding);settings=json.loads(text.lstrip('\ufeff'))
    if settings.get('autoGear')!=1 or settings.get('autoClutch')!=1:raise ValueError('Automatic gearing/clutch profile required')
    candidate,count=re.subn(r'("autoGear"\s*:\s*)1',r'\g<1>0',text,count=1)
    if count!=1 or hashlib.sha256(candidate.encode(encoding)).hexdigest()!=m['assists_sha256']:
        raise ValueError('Change exceeds the single verified autoGear field')
    inventory=dict(m['evidence_sha256']);inventory.update(hashes([manual_evidence,p['assists'],p['controls']]))
    qualified=dict(semantics='gamepad_v1',kind='qualified_axis_transport',passed=True,visual_response_confirmed=True,
        validation_scope='Raw axis delivery verified in manual neutral; only autoGear 0→1 changed',
        response_assists_sha256=m['assists_sha256'],assists_sha256=digest(p['assists']),
        controls_sha256=m['controls_sha256'],dll_sha256=m['dll_sha256'],driver_sha256=m['driver_sha256'],
        evidence_sha256=inventory,in_motion_equivalence_verified=False,automatic_drivetrain_verified=False,
        required_next_test='guarded 5/10/30-second learned-policy driving after new teacher training')
    save(output,qualified);return qualified


def load_capture(folder, physical):
    folder=Path(folder).resolve();m=read(folder/'summary.json')
    if m.get('kind')!='device_response' or m.get('semantics')!='gamepad_v1' or m.get('physical') is not physical or not m.get('capture_passed'):
        raise ValueError('Incomplete or wrong device response capture')
    if digest(folder/'samples.jsonl')!=m['samples_sha256']:raise ValueError('Changed capture log')
    rows=[json.loads(s) for s in (folder/'samples.jsonl').read_text(encoding='utf-8').splitlines()]
    response=[r for r in rows if r['kind']=='response' and r['mode']=='Running']
    if m.get('engine_off_required') and any(r['sample']['rpm']!=0 or r['sample']['gear_raw']!=1 for r in response):raise ValueError('Engine-off response condition violated')
    if any(b['observation_ns']<=a['observation_ns'] for a,b in zip(response,response[1:])):raise ValueError('Reversed response time')
    if any((r['sample']['gear_raw']!=1 and not (m.get('brake_held') and r['sample']['gear_raw']>=2)) or r['sample']['speed_kmh']>1 or not r['input']['connected'] for r in response):raise ValueError('Response test must remain stationary in declared gear mode')
    stages=[]
    for i in range(10):
        stage=[r for r in response if r['stage']==i]
        if len(stage)<100 or stage[-1]['observation_ns']-stage[0]['observation_ns']<2_000_000_000:raise ValueError('Missing response stage')
        tail=[r for r in stage if r['observation_ns']>=stage[-1]['observation_ns']-500_000_000]
        raw=np.array([[r['input'][k] for k in ('stick_x','throttle','brake')] for r in tail])
        target=np.array(stage[-1]['target'])
        if not np.isfinite(raw).all() or np.max(abs(raw-target))>.06:raise ValueError(f'Stage {i}: human/virtual input did not reach target')
        values=np.array([[r['sample'][k] for k in ('steering_raw','throttle','brake')] for r in tail])
        if not np.isfinite(values).all():raise ValueError('Nonfinite ACC response')
        # Full time trace is retained for filter comparison, including transient onset.
        trace=[dict(seconds=(r['observation_ns']-stage[0]['observation_ns'])/1e9,
                    input=[r['input'][k] for k in ('stick_x','throttle','brake')],
                    acc=[r['sample'][k] for k in ('steering_raw','throttle','brake')]) for r in stage]
        stages.append(dict(stage=i,target=target.tolist(),input_mean=raw.mean(0).tolist(),acc_mean=values.mean(0).tolist(),trace=trace))
    if not physical:
        sent=[r for r in rows if r['kind']=='sent']
        if not sent or not m['released'] or not m['stop_confirmed']:raise ValueError('Virtual stop/release not confirmed')
    return folder,m,stages


def compare(physical,virtual,profile,output,visual_response_confirmed=False):
    pf,pm,ps=load_capture(physical,True);vf,vm,vs=load_capture(virtual,False)
    p=read(profile);fields=('controls_sha256','assists_sha256','dll_sha256','driver_sha256')
    if any(pm[k]!=vm[k] for k in fields) or pm['identity']!=vm['identity'] or pm.get('brake_held')!=vm.get('brake_held') or pm.get('engine_off_required')!=vm.get('engine_off_required'):raise ValueError('Different device settings or car/track')
    if pm['controls_sha256']!=digest(p['controls']) or pm['assists_sha256']!=digest(p['assists']) or any(pm[k]!=p[k] for k in ('dll_sha256','driver_sha256')):raise ValueError('Profile changed')
    a=np.array([s['acc_mean'] for s in ps]);b=np.array([s['acc_mean'] for s in vs])
    delta=abs(a-b).max(0)
    # Provisional diagnostic tolerances; this does not assert equal moving dynamics.
    coverage=all(abs(v[1,0]-v[3,0])>.1 and v[5,1]>.9 and v[7,2]>.9 and np.max(abs(v[9]))<.1 for v in (a,b))
    numeric=coverage and bool(np.all(delta<=.1))
    result=dict(semantics='gamepad_v1',kind='device_validation',passed=numeric and bool(visual_response_confirmed),numeric_passed=numeric,
        visual_response_confirmed=bool(visual_response_confirmed),in_motion_equivalence_verified=False,
        maximum_steady_response_difference=delta.tolist(),tolerance=.1,physical=ps,virtual=vs,
        evidence_sha256=hashes([pf/'samples.jsonl',pf/'summary.json',vf/'samples.jsonl',vf/'summary.json']),**{k:pm[k] for k in fields})
    save(output,result)
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('physical');parser.add_argument('virtual');parser.add_argument('profile');parser.add_argument('output');parser.add_argument('--visual-response-confirmed',action='store_true')
    a=parser.parse_args();r=compare(a.physical,a.virtual,a.profile,a.output,a.visual_response_confirmed)
    print(json.dumps(dict(passed=r['passed'],numeric_passed=r['numeric_passed'])))
