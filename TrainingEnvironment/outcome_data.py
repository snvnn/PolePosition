"""Evidence-only outcome selection. Unknown outcomes never become failure labels."""
import bisect
import csv
import json
from collections import Counter
from pathlib import Path

import numpy as np

from ModelTrainer.common import read, save, digest, hashes, verify_hashes, finish, new_folder
from GamepadIO.run_report import inspect_log
from .continuous_rollout import rebuild


def continuous(a, b):
    return (a['state']['status'] == b['state']['status'] == 'ready'
            and a['state']['tracking_epoch'] == b['state']['tracking_epoch']
            and a['sample'].get('session_index') == b['sample'].get('session_index')
            and a['sequence'] + 1 == b['sequence']
            and 0 < b['observation_ns'] - a['observation_ns'] <= 20_000_000
            and np.linalg.norm(np.array([b['sample'][k]-a['sample'][k] for k in ('position_x','position_z')])) <= 5)


def confirmed_failure(rows):
    """Observed invalidation while model commands still own the car, before braking."""
    sent=[r for r in rows if r['kind']=='sent']
    running=[r for r in sent if r['mode']=='Running']
    if not running:return None
    begin=running[0]['send_end_ns']
    stop=next((r['send_start_ns'] for r in sent if r['mode']!='Running' and r['send_start_ns']>begin),float('inf'))
    obs=[r for r in rows if r['kind']=='observation']
    for a,b in zip(obs,obs[1:]):
        t=b['observation_ns']
        if (begin<t<stop and a['sample']['lap_valid'] is True and b['sample']['lap_valid'] is False
                and continuous(a,b)):
            return dict(reason='observed_lap_invalidation',observation_ns=t,sequence=b['sequence'],
                        stopping_ns=None if stop==float('inf') else stop,
                        attribution='ACC validity changed during model control; cause not inferred')
    return None


def start_cut(points, length, radius, clearance):
    """A declared quarantine rule, not a learned failure or weaving detector."""
    if not points:return None
    if min(points[0][1]%length,length-points[0][1]%length)>radius:return None
    end=next((t for t,s in points if min(s%length,length-s%length)>radius),points[-1][0])
    return end+clearance


def reward_components(a,b,previous_action,action,dt,terminal,c):
    # a/b: rebuilt path position, lateral error, heading error; progress already unwrapped.
    return dict(progress=c['progress']*(b[0]-a[0]),time=c['time']*dt,
                lateral=c['lateral_squared']*a[1]**2*dt,
                heading=c['heading_squared']*a[2]**2*dt,
                input_change=c['applied_action_change_squared']*float(np.square(np.array(action)-previous_action).sum()),
                terminal=c.get(terminal,0.))


def audit(config):
    c=read(config);parent=Path(c['source_model']);pm=read(parent/'manifest.json')
    verify_hashes(pm['output_sha256'],parent)
    out=new_folder(c['output_dir'],'outcome-dataset');save(out/'config.json',c)
    entries=[];seen=set();inventory=hashes([Path(config),parent/'manifest.json',Path(c['quarantine_evidence']),Path(__file__)])
    for path in sorted(Path(c['run_root']).glob('gamepad-*')):
        e=dict(run=str(path.resolve()),eligible=False)
        try:
            m=read(path/'manifest.json');s=read(path/'summary.json')
            e.update(summary_reason=s.get('reason'),shutdown={k:s.get(k) for k in ('stop_confirmed','released','standstill_confirmed')})
            if m.get('semantics')!='gamepad_v1' or not m.get('complete'):raise ValueError('incomplete_or_wrong_semantics')
            verify_hashes(m['output_sha256'],path)
            sha=digest(path/'samples.jsonl');e['samples_sha256']=sha
            if sha in seen:raise ValueError('duplicate_run_contents')
            seen.add(sha)
            inventory.update(hashes([path/n for n in ('manifest.json','summary.json','samples.jsonl')]))
            if path.name in c['quarantined_runs']:raise ValueError(c['quarantined_runs'][path.name])
            if s.get('logging_failure') or s.get('error'):raise ValueError('recording_or_system_error')
            if m.get('steering_smoothing_ms',0)!=0:raise ValueError('LPF_action_semantics_excluded')
            if m.get('controls_teacher_sha256')!=pm['controls_sha256']:raise ValueError('controls_profile_mismatch')
            source=Path(m['model']);sm=read(source/'manifest.json')
            if (sm.get('assists_sha256')!=pm['assists_sha256'] or sm.get('observation_variant')
                    or digest(source/'model.json')!=m['model_sha256']):raise ValueError('source_model_or_assists_mismatch')
            rows=[json.loads(line) for line in (path/'samples.jsonl').open(encoding='utf-8')]
            policies=[r for r in rows if r['kind']=='policy' and r['mode']=='Running']
            if not policies:raise ValueError('no_policy_control')
            if any(p.get('authority')!='model' or any(p.get('label_mask',[1,1,1])) for p in policies):
                raise ValueError('human_intervention_not_autonomous_outcome')
            event=confirmed_failure(rows)
            if event is None:raise ValueError('no_confirmed_failure_event; not labelled from summary')
            # Limit integrity checks to the policy and its real final observation.
            end=event['observation_ns']
            prefix=[r for r in rows if (r['kind']=='observation' and r['observation_ns']<=end)
                    or (r['kind']=='policy' and r['now_ns']<end)
                    or (r['kind']=='sent' and r['send_end_ns']<end)]
            transport,pairs=inspect_log(prefix)
            if not transport['passed']:raise ValueError('transport: '+','.join(transport['errors']))
            if not pairs:raise ValueError('empty_policy_pairs')
            running_obs=[r for r in prefix if r['kind']=='observation' and r['observation_ns']>=pairs[0][0]['observation_ns']]
            if any(not continuous(a,b) for a,b in zip(running_obs,running_obs[1:])):raise ValueError('discontinuous_running_observations')
            if any(not r['sample']['lap_valid'] for r in running_obs[:-1]):raise ValueError('already_invalid_during_run')
            e.update(eligible=True,event=event,model_sha256=m['model_sha256'],transport=transport,policy_pairs=len(pairs))
        except (ValueError,KeyError,FileNotFoundError,TypeError) as error:e['exclusion_reason']=str(error)
        entries.append(e)
        print('AUDIT',path.name,e['eligible'],e.get('exclusion_reason',''),flush=True)
    good=[e for e in entries if e['eligible']]
    success_sha=read(Path(c['success_archive'])/'successful-laps.json')['model_sha256']
    success_log_sha=digest(Path(c['success_archive'])/'source-run/samples.jsonl')
    for i,e in enumerate(good):
        # The two successful laps and third failed lap share a session: always same split.
        e['split']='train' if e['samples_sha256']==success_log_sha else ('validation' if i%c['validation_every_n_runs']==0 else 'train')
    save(out/'run-audit.json',entries)
    save(out/'audit-summary.json',dict(runs=len(entries),confirmed_failure_runs=len(good),
        exclusions=dict(Counter(e.get('exclusion_reason') for e in entries if not e['eligible'])),
        success_model_sha256=success_sha))
    save(out/'manifest.json',dict(schema_version=1,kind='outcome_dataset',complete=False,input_sha256=inventory))
    print('AUDIT_READY',out,flush=True)
    return out


if __name__=='__main__':
    import sys
    audit(sys.argv[1])
