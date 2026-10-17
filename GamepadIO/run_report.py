"""Causal transmission checks and teacher/model comparison at common path positions."""
import argparse
import bisect
import csv
import json
from pathlib import Path
import numpy as np
from ModelTrainer.common import read,save,digest,verify_hashes
from TrainingEnvironment.gamepad import read_inputs,align


def inspect_log(rows):
    observations={};policies=[];sent=[];errors=[];last_observed=-1
    for r in rows:
        kind=r['kind']
        if kind=='observation':
            t=r['observation_ns']
            if t<=last_observed:errors.append('nonmonotonic_observation')
            last_observed=t;observations[t]=r
        elif kind=='policy' and r['mode']=='Running':
            if r['observation_ns'] not in observations or r['now_ns']<r['observation_ns']:errors.append('policy_without_prior_observation')
            if policies and (r['observation_ns']<=policies[-1]['observation_ns'] or r['now_ns']<=policies[-1]['now_ns']):errors.append('duplicate_or_reversed_policy')
            a=np.array(r['action'])
            if a.shape!=(3,) or not np.isfinite(a).all() or abs(a[0])>1 or any(v<0 or v>1 for v in a[1:]):errors.append('invalid_policy_action')
            policies.append(r)
        elif kind=='sent':
            if r['send_end_ns']<r['send_start_ns'] or (sent and r['send_start_ns']<sent[-1]['send_end_ns']):errors.append('reversed_transmission')
            sent.append(r)
    starts=[r['send_start_ns'] for r in sent];observation_times=sorted(observations);delays=[];pairs=[]
    for i,p in enumerate(policies):
        j=bisect.bisect_left(starts,p['now_ns'])
        if j>=len(sent):errors.append('policy_not_sent');continue
        s=sent[j]
        if s['mode']!='Running':continue  # an explicit guardian stop may supersede the last inference
        a=p.get('selected_action',p['action']);expected=[a[0]*(32768 if a[0]<0 else 32767),a[1]*255,a[2]*255]
        actual=[s['report'][k] for k in ('lx','rt','lt')]
        if max(abs(v-e) for v,e in zip(actual,expected))>1:errors.append('requested_report_mismatch')
        delay=(s['send_end_ns']-p['observation_ns'])/1e6;delays.append(delay)
        if delay>=100:errors.append('stale_applied_action')
        next_index=bisect.bisect_right(observation_times,s['send_end_ns'])
        if next_index>=len(observation_times):errors.append('no_observation_after_transmission')
        obs=observations.get(p['observation_ns'])
        if obs:pairs.append((obs,p,s))
    if not policies or not delays:errors.append('no_policy_control')
    return dict(passed=not errors,errors=sorted(set(errors)),policy_samples=len(policies),
                maximum_observation_to_send_ms=max(delays,default=None),game_application_time_known=False),pairs


def inspect_shutdown(rows):
    """A reset after policy control ends is not evidence of successful braking."""
    epoch=None; latest=None; active=False; errors=[]
    for r in rows:
        if r['kind']=='observation':
            latest=r.get('state')
            if active and (not latest or latest.get('tracking_epoch')!=epoch or latest.get('status')!='ready'):
                errors.append('tracking_changed_before_release')
        elif r['kind']=='sent' and r['mode']=='Running' and not active:
            active=True
            if not latest or latest.get('status')!='ready':errors.append('missing_initial_tracking')
            epoch=latest.get('tracking_epoch') if latest else None
    if not active:errors.append('no_policy_control')
    return dict(passed=not errors,errors=sorted(set(errors)),tracking_epoch=epoch)


def report(run, *, replace=False, render_plot=True):
    from TrackModel.model import load_reference
    from TrainingEnvironment.human_imitation import project
    run=Path(run).resolve();m=read(run/'manifest.json');summary=read(run/'summary.json')
    if m['semantics']!='gamepad_v1':raise ValueError('Wrong live model format')
    verify_hashes(m['output_sha256'],run)
    model=read(Path(m['model'])/'manifest.json')
    verify_hashes(model['output_sha256'],m['model'])
    if digest(Path(m['model'])/'model.json')!=m['model_sha256']:raise ValueError('Different live model')
    fresh=model.get('teacher_source')=='intervention_logs'
    lap_teacher=model.get('teacher_source')=='raw_gamepad_laps'
    heldout=None
    if lap_teacher:
        candidates=[r for r in model['teacher_laps'] if r['split']=='validation']
        if len(candidates)!=1:raise ValueError('Exactly one held-out comparison lap required')
        heldout=candidates[0]
    session_teacher=model.get('teacher_source')=='raw_gamepad_sessions'
    if session_teacher:
        candidates=[r for r in model['teacher_sessions'] if r['split']=='validation']
        if len(candidates)!=1:raise ValueError('Exactly one held-out session required')
    recording=Path(candidates[0]['recording'] if session_teacher else heldout['recording'] if heldout else model['validation_run'] if fresh else model['recordings'][2])
    if not fresh and read(recording/'metadata.json').get('input_semantics')!='gamepad_v1':raise ValueError('Raw gamepad teacher required')
    rows=[json.loads(s) for s in (run/'samples.jsonl').read_text().splitlines()]
    transport,pairs=inspect_log(rows)
    shutdown=inspect_shutdown(rows)
    prepared=Path(m['prepared']);pm=read(prepared/'manifest.json');heading=read(prepared/'heading_profile.json')
    ref=load_reference(pm['track_model_path']);length=ref['manifest']['total_length_m']
    if fresh:
        from TrainingEnvironment.corrections import load_corrections
        labels,_,_=load_corrections([recording],model['controls_sha256'],model['assists_sha256'])
        observations={r['observation_ns']:r for r in (json.loads(line) for line in (recording/'samples.jsonl').read_text().splitlines()) if r['kind']=='observation'}
        bins={}
        for label in labels:
            o=observations[label['observation_ns']];state=o['state'];sample=o['sample']
            bins.setdefault(int(state['s_m']),[]).append(label['action']+[sample['steering_raw'],sample['speed_kmh'],state['heading_error_rad'],state['lateral_error_m']])
    else:
        with (recording/'telemetry.csv').open(newline='') as f:teacher=list(csv.DictReader(f))
        if heldout is not None:
            teacher=[r for r in teacher if int(r['segment_id'])==heldout['segment_id'] and heldout['start_elapsed_ns']<=int(r['elapsed_ns'])<heldout['end_elapsed_ns']]
            if not teacher or any(r['lap_valid']!='true' for r in teacher):raise ValueError('Invalid held-out comparison lap')
        # Comparison bins never join sequential history over the reference seam.
        geom=project(teacher,ref['rows'],length,heading,allow_finish_seam=True)
        geometry={int(r['sequence']):g for r,g in zip(teacher,geom)}
        bins={}
        for r,_,a in align(teacher,read_inputs(recording/'inputs.csv')):
            s,e,h=geometry[int(r['sequence'])][:3]
            bins.setdefault(int(s),[]).append([a['stick_x'],a['throttle'],a['brake'],float(r['steering_raw']),float(r['speed_kmh']),h,e])
    comparison=[]
    after_rows=sorted((r for r in rows if r['kind']=='observation'),key=lambda r:r['observation_ns'])
    after_times=[r['observation_ns'] for r in after_rows]
    for obs,policy,sent in pairs:
        state=obs['state'];s=state['s_m']
        if s is None or int(s) not in bins:continue
        human=np.array(bins[int(s)]).mean(0).tolist()
        live=list(policy.get('selected_action',policy['action']))+[obs['sample']['steering_raw'],obs['sample']['speed_kmh'],state['heading_error_rad'],state['lateral_error_m']]
        index=bisect.bisect_right(after_times,sent['send_end_ns']);following=after_rows[index] if index<len(after_rows) else None
        comparison.append(dict(s_m=s,observation_ns=obs['observation_ns'],send_end_ns=sent['send_end_ns'],
            next_observation_ns=following['observation_ns'] if following else None,
            next_acc_response={k:following['sample'][k] for k in ('steering_raw','throttle','brake')} if following else None,teacher=human,model=live))
    # Reference curvature locates a comparison interval, not an in-game racing line.
    curve=sorted((float(r['s_m']),abs(float(r['curvature_per_m']))) for r in ref['rows'])
    active=[s for s,k in curve if 25<s<min(1000,length/2) and k>.003]
    first_corner=None;cleared=False
    if active:
        begin=active[0];end=begin
        for s in active[1:]:
            if s-end>40:break
            end=s
        first_corner=[begin,end+30]
        positions=[r['s_m'] for r in comparison]
        cleared=bool(positions and min(positions)<=begin and max(positions)>=end+30 and summary['passed'] and not summary.get('intervention'))
    result=dict(semantics='gamepad_v1',transport=transport,shutdown=shutdown,comparison_fields=['stick_x','rt','lt','acc_steering','speed_kmh','heading_error_rad','lateral_error_m'],
        comparison=comparison,first_corner_interval_m=first_corner,corner_interval_method='reference curvature > 0.003 /m, gaps <= 40 m; verify against track',
        first_corner_pass_provisional=cleared,performance_verified=False,reason=summary['reason'],
        attribution='inspect requested stick versus transmitted report, then ACC response; driving failure is not automatically device error')
    if comparison and render_plot:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        x=np.array([r['s_m'] for r in comparison]);order=np.argsort(x)
        human=np.array([r['teacher'] for r in comparison]);live=np.array([r['model'] for r in comparison])
        fig,axes=plt.subplots(5,1,figsize=(10,12),sharex=True)
        for ax,i,label in zip(axes,[0,3,4,5,6],['Stick X','ACC steering','Speed (km/h)','Heading error (rad)','Lateral error (m)']):
            ax.plot(x[order],human[order,i],label='Teacher',alpha=.8);ax.plot(x[order],live[order,i],label='Model',alpha=.8)
            ax.set_ylabel(label);ax.grid(alpha=.2)
        axes[0].legend();axes[-1].set_xlabel('Reference path position (m)')
        fig.suptitle('Gamepad diagnostic — matched path positions; performance not yet certified')
        fig.tight_layout();fig.savefig(run/'gamepad-comparison.png',dpi=150);plt.close(fig)
    save(run/'gamepad-comparison.json',result,replace=replace)
    return result


def completed_drive(summary):
    return (summary.get('reason')=='time_limit' and summary.get('stop_confirmed') is True
            and summary.get('released') is True and summary.get('error') is None
            and summary.get('logging_failure') is False and summary.get('policy_samples',0)>0)


def repair_report(run):
    """Rebuild a failed post-drive report, preserving original failure evidence."""
    import shutil
    import time
    run=Path(run).resolve();m=read(run/'manifest.json');s=read(run/'summary.json')
    verify_hashes(m['output_sha256'],run)
    if m.get('complete') is not True or not completed_drive(s) or s.get('comparison_report_created') is not False or not s.get('report_error'):
        raise ValueError('Only a completed drive with a failed report may be repaired')
    backup=run/f'report-repair-{time.time_ns()}';backup.mkdir()
    for name in ('manifest.json','summary.json','gamepad-comparison.json','gamepad-comparison.png'):
        if (run/name).exists():shutil.copy2(run/name,backup/name)
    original_log_hash=digest(run/'samples.jsonl')
    result=report(run,replace=True)
    if digest(run/'samples.jsonl')!=original_log_hash:raise ValueError('Run log changed during repair')
    s['passed']=bool(result['transport']['passed'] and result['shutdown']['passed'] and completed_drive(s))
    s['comparison_report_created']=True
    s['original_report_error']=s.pop('report_error')
    s['report_repair_evidence']=str(backup)
    save(run/'summary.json',s,replace=True)
    for name in ('summary.json','gamepad-comparison.json','gamepad-comparison.png'):
        if (run/name).exists():m['output_sha256'][name]=digest(run/name)
    save(run/'manifest.json',m,replace=True)
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('run');p.add_argument('--repair',action='store_true');a=p.parse_args();r=repair_report(a.run) if a.repair else report(a.run)
    print(json.dumps(r['transport']))
    raise SystemExit(0 if r['transport']['passed'] and r['shutdown']['passed'] else 1)
