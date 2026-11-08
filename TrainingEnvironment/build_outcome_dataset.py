"""Build separated BC labels and verified outcome transitions without modifying sources."""
import bisect
import csv
import json
from collections import Counter
from pathlib import Path
import numpy as np
from ModelTrainer.common import read,save,hashes,verify_hashes,finish,digest
from .outcome_data import start_cut, continuous, reward_components
from .continuous_rollout import rebuild
from GamepadIO.run_report import inspect_log


def accepted_pair(o,p,s):
    a=np.asarray(p['selected_action'])
    if p['authority']!='model' or s['authority']!='model' or any(p['label_mask']):return False
    if not np.isfinite(a).all() or a.shape!=(3,) or abs(a[0])>1 or np.any(a[1:]<0) or np.any(a[1:]>1):return False
    if a.tolist()!=p['action'] or a.tolist()!=p['command_action']:return False
    if not o['observation_ns']<=p['now_ns']<=s['send_start_ns']<=s['send_end_ns']<=o['observation_ns']+20_000_000:return False
    return (s['xinput'][1]==s['report'] and
            max(abs(a*[32768 if a[0]<0 else 32767,255,255]-[s['report'][k] for k in ('lx','rt','lt')]))<=1)


def build(folder):
    folder=Path(folder);c=read(folder/'config.json');m=read(folder/'manifest.json')
    if m.get('complete'):raise ValueError('Immutable completed dataset')
    verify_hashes({k:v for k,v in m['input_sha256'].items() if Path(k).suffix not in ('.py','.exe')})
    parent=Path(c['source_model']);pm=read(parent/'manifest.json');verify_hashes(pm['output_sha256'],parent)
    source=Path(c['teacher_dataset']);sm=read(source/'manifest.json');verify_hashes(sm['output_sha256'],source)
    prepared=Path(pm['prepared']);speed=parent/'speed-profile.json';length=read(speed)['length_m']
    train=read(source/'training-pool.json');validation=read(source/'validation.json')
    quarantine=[dict(recording=str((Path(c['run_root'])/name).resolve()),scope='whole_run',reason=reason,
                     evidence=c['quarantine_evidence']) for name,reason in c['quarantined_runs'].items()]
    removed=[];history=c['history_clearance_ns']
    # Use raw segment boundaries; curation-created context changes are not restarts.
    for i,path in enumerate(pm['recordings'],1):
        with (Path(path)/'telemetry.csv').open(newline='',encoding='utf-8') as f:
            segment_by_seq={int(r['sequence']):int(r['segment_id']) for r in csv.DictReader(f)}
        framefile=Path(c['teacher_experiment'])/f'session-{i}/frames.jsonl'
        groups={}
        for line in framefile.open():
            r=json.loads(line)['frame'];seg=segment_by_seq[r['sequence']]
            groups.setdefault(seg,[]).append((r['observation_ns'],r['s_m']))
        for segment,points in groups.items():
            cut=start_cut(points,length,c['start_line_radius_m'],history)
            if cut is not None:
                quarantine.append(dict(recording=path,segment=segment,start_ns=points[0][0],end_ns=cut,
                                       reason='start_within_150m_plus_shared_history_clearance'))
        def retain(rows):
            kept=[]
            for r in rows:
                if r['recording']!=path:kept.append(r);continue
                windows=[q for q in quarantine if q['recording']==path and q.get('segment')==segment_by_seq[r['sequence']]]
                if any(q['start_ns']<=r['observation_ns']<=q['end_ns'] for q in windows):
                    removed.append(dict(recording=path,sequence=r['sequence'],observation_ns=r['observation_ns'],reason='start_line_quarantine'))
                else:kept.append(r)
            return kept
        train=retain(train);validation=retain(validation)
    success=read(parent/'successful-examples.json')
    evidence=read(Path(c['success_archive'])/'successful-laps.json');verify_hashes(evidence['evidence_sha256'])
    success_sha=digest(Path(c['success_archive'])/'source-run/samples.jsonl')
    audit=read(folder/'run-audit.json');transitions=[];reviews=[]
    for e in audit:
        if not e['eligible']:continue
        path=Path(e['run']);end=e['event']['observation_ns']
        rows=[json.loads(line) for line in (path/'samples.jsonl').open(encoding='utf-8')]
        prefix=[r for r in rows if (r['kind']=='observation' and r['observation_ns']<=end)
                or (r['kind']=='policy' and r['now_ns']<end) or (r['kind']=='sent' and r['send_end_ns']<end)]
        review=dict(run=str(path),split=e['split'],event=e['event'],accepted=False)
        try:
            transport,pairs=inspect_log(prefix)
            if not transport['passed'] or any(not accepted_pair(*p) for p in pairs):raise ValueError('strict action/report pairing failed')
            fx,positions=rebuild(prefix,prepared,speed,end_ns=end)
            first=pairs[0][0]['observation_ns'];cut=start_cut([(o['observation_ns'],positions[o['observation_ns']][0]) for o,_,_ in pairs],length,c['start_line_radius_m'],history)
            if cut:
                quarantine.append(dict(recording=str(path),start_ns=first,end_ns=cut,reason='start_within_150m_plus_shared_history_clearance'))
            obs=[r for r in prefix if r['kind']=='observation'];bytime={r['observation_ns']:r for r in obs}
            sent=[r for r in prefix if r['kind']=='sent'];sent_times=[r['send_end_ns'] for r in sent]
            lap_ends=[v['end_observation_ns'] for v in evidence['successful_full_laps']] if e['samples_sha256']==success_sha else []
            run_trans=[]
            for i,(o,p,applied) in enumerate(pairs):
                t=o['observation_ns'];nt=pairs[i+1][0]['observation_ns'] if i+1<len(pairs) else end
                boundary=next((v for v in lap_ends if t<v<=nt),None)
                if boundary:nt=boundary
                if cut and t<=cut:continue
                if t not in fx or nt not in fx or nt not in bytime:raise ValueError('missing native causal features')
                n=bytime[nt];dt=(nt-t)/1e9
                if not applied['send_end_ns']<nt or not 0<dt<=.05 or n['state']['tracking_epoch']!=o['state']['tracking_epoch']:raise ValueError('invalid transition clock/epoch')
                # Repeated reports may hold the action, but cannot insert an override.
                reports=sent[bisect.bisect_left(sent_times,applied['send_end_ns']):bisect.bisect_left(sent_times,nt)]
                if any(r['mode']!='Running' or r['authority']!='model' or any(r['report'][k]!=applied['report'][k] for k in ('lx','rt','lt')) for r in reports):raise ValueError('action changed before next state')
                a=list(positions[t]);b=list(positions[nt]);ds=(b[0]-a[0]+length/2)%length-length/2;b[0]=a[0]+ds
                displacement=np.linalg.norm([n['sample'][k]-o['sample'][k] for k in ('position_x','position_z')])
                if abs(ds)>max(3,displacement+2) or displacement>5:raise ValueError('unexplained progress jump')
                terminal='invalid_lap' if nt==end else ('valid_full_lap' if boundary else None)
                prior_index=bisect.bisect_left(sent_times,t)-1
                if prior_index<0:raise ValueError('missing prior actual action')
                previous=[sent[prior_index]['axes'][k] for k in ('steering','throttle','brake')]
                axes=[applied['axes'][k] for k in ('steering','throttle','brake')]
                components=reward_components(a,b,previous,axes,dt,terminal,c['reward'])
                label='success' if e['samples_sha256']==success_sha and any(v['start_observation_ns']<=t<v['end_observation_ns'] for v in evidence['successful_full_laps']) else 'failure_trajectory'
                run_trans.append(dict(run=str(path),source_log_sha256=e['samples_sha256'],source_model_sha256=e['model_sha256'],split=e['split'],
                    observation_ns=t,send_end_ns=applied['send_end_ns'],next_observation_ns=nt,sequence=o['sequence'],next_sequence=n['sequence'],tracking_epoch=o['state']['tracking_epoch'],
                    features=fx[t],next_features=fx[nt],action=p['selected_action'],applied_axes=axes,s_m=positions[t][0],
                    dt=dt,reward=sum(components.values()),reward_components=components,terminated=bool(terminal),truncated=False,
                    terminal_reason=terminal,outcome=label,mask=[1,1,1]))
            if not run_trans or run_trans[-1]['terminal_reason']!='invalid_lap':raise ValueError('quarantine or gap removed failure endpoint')
            transitions.extend(run_trans);review.update(accepted=True,transitions=len(run_trans),terminal_counts=dict(Counter(r['terminal_reason'] for r in run_trans if r['terminated'])))
        except (ValueError,KeyError,RuntimeError) as error:review['exclusion_reason']=str(error)
        reviews.append(review);print('REBUILD',path.name,review,flush=True)
    # Unknown/failed-to-rebuild sessions are excluded in their entirety, not silently filled.
    accepted_shas={r['source_log_sha256'] for r in transitions}
    if success_sha not in accepted_shas:raise ValueError('Verified two-lap source must remain available')
    # Successful laps lie after the original session start; verify exact time membership.
    successful_ids={(r['sequence'],r['observation_ns']) for r in transitions if r['source_log_sha256']==success_sha and r['outcome']=='success'}
    success=[r for r in success if (r['sequence'],r['observation_ns']) in successful_ids]
    normal=np.array([r['features'] for r in train if 0 in r['phases_ms']],dtype=np.float32)
    mean=normal.mean(0);std=normal.std(0);std[std<1e-6]=1
    validation_logs={r['source_log_sha256'] for r in transitions if r['split']=='validation'}
    if validation_logs & {r['source_log_sha256'] for r in transitions if r['split']=='train'}:raise ValueError('Cross-split session leakage')
    if not validation_logs or not any(r['terminated'] and r['split']=='train' for r in transitions):raise ValueError('Insufficient separated outcomes')
    for name,items in [('teacher-training.json',train),('teacher-validation.json',validation),('success-training.json',success),('transitions.json',transitions),('quarantine.json',quarantine),('removed-samples.json',removed),('transition-audit.json',reviews)]:save(folder/name,items)
    save(folder/'normalization.json',dict(mean=mean.tolist(),std=std.tolist(),count=len(normal),source='retained human training phase 0 only'))
    save(folder/'summary.json',dict(human_training=len(train),human_validation=len(validation),autonomous_success=len(success),
        transition_counts=dict(Counter(r['split'] for r in transitions)),outcomes=dict(Counter(r['outcome'] for r in transitions)),
        terminal_counts=dict(Counter(str((r['split'],r['terminal_reason'])) for r in transitions if r['terminated'])),
        accepted_runs=sum(r['accepted'] for r in reviews),quarantined_teacher_samples=len(removed),live_performance_verified=False))
    m['input_sha256'].update(hashes([parent/'manifest.json',source/'manifest.json',Path(__file__),Path(__file__).with_name('continuous_rollout.py'),Path(__file__).with_name('continuous_reference.py')]))
    m.update(feature_transform='original',prepared=str(prepared),source_model=str(parent),feature_names=read(parent/'model.json')['feature_names'])
    verify_hashes({k:v for k,v in m['input_sha256'].items() if Path(k).suffix not in ('.py','.exe')});finish(folder,m)
    print('DATASET_COMPLETE',folder,flush=True)


if __name__=='__main__':
    import sys
    build(sys.argv[1])
