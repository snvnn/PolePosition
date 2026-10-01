"""Join teacher decisions to their exact observations, then invoke shared Rust features."""
import csv
import json
from pathlib import Path
import statistics
import subprocess
import tomllib
from collections import Counter

from ModelAndActuator.report import verify
from .common import ROOT, FIELDS, UNITS, classify, config, digest, finish, hashes, new_folder, read, save, verify_hashes


def policy_binary():
    path = ROOT / 'target/release/poleposition-policy.exe'
    if not path.is_file(): raise ValueError('Build poleposition-policy in release mode first')
    return path


def normalizer_for(train):
    if not train: raise ValueError('No training examples')
    mean=[statistics.mean(r['features'][i] for r in train) for i in range(19)]
    std=[statistics.pstdev(r['features'][i] for r in train) or 1. for i in range(19)]
    counts=Counter(r['pedal_class'] for r in train)
    return dict(feature_names=FIELDS,mean=mean,std=std,train_class_counts=dict(counts),
        class_weights={k:len(train)/(len(counts)*v) for k,v in counts.items()},
        baseline_u=statistics.median(r['target_u'] for r in train))


def joined_teacher(folder):
    m, s, commands, telemetry, _ = verify(folder)
    required={'commands.csv','telemetry.csv','events.csv','summary.json','controller.csv','config.json'}
    if not required.issubset(m.get('output_sha256',{})) or not m.get('input_sha256'):
        raise ValueError('Teacher hash inventory incomplete')
    if (m.get('complete') is not True or m.get('scenario') != 'demo' or m.get('mock') is not False
        or not all(s.get(k) is True for k in ('normal_shutdown','recording_valid','stop_confirmed','released'))
        or s.get('termination_reason') not in ('scenario_complete','time_limit')
        or s.get('dropped') != 0 or s.get('device_error') or s.get('log_error')
        or m['config'].get('policy')):
        raise ValueError('Teacher must be a successful, real, unmodified PID demonstration')
    with (Path(folder)/'controller.csv').open(encoding='utf-8',newline='') as f:
        control = list(csv.DictReader(f))
    keys = lambda r: (r['now_ns'], r['observation_sequence'], r['tracking_epoch'])
    diagnostics = {}
    for r in control:
        key=keys(r)
        if key in diagnostics: raise ValueError('Duplicate controller reception key')
        diagnostics[key]=r
    sources={(r['sequence'],r['tracking_epoch']):r for r in telemetry}
    candidates={}; excluded=[]; transmissions={}
    for r in commands:
        if r['send_success']=='true' and r['mode']=='Running':
            transmissions.setdefault((r['sequence'],r['observation_sequence'],r['tracking_epoch']),r)
    for r in commands:
        if r['reason']!='accepted' or r['mode']!='Running': continue
        d=diagnostics.get(keys(r))
        if d is None: raise ValueError('Accepted teacher command lacks diagnostic')
        key=(r['observation_sequence'],r['tracking_epoch'])
        o=sources.get(key)
        if o is None or o['observation_ns']!=d['observation_ns']: raise ValueError('Unknown teacher observation')
        if d['reason']!='tracking' or d['abort']!='false' or d['complete']!='false': continue
        throttle,brake=float(r['requested_throttle']),float(r['requested_brake'])
        if not (0<=throttle<=.20 and 0<=brake<=.60) or (throttle>0 and brake>0):
            raise ValueError('Teacher pedal outside policy representation')
        signature=(throttle,brake,float(d['target_speed_kmh']))
        if key in candidates:
            if candidates[key] is not None and candidates[key]['signature']!=signature:
                candidates[key]=None; excluded.append(dict(sequence=key[0],tracking_epoch=key[1],reason='ambiguous_duplicate'))
            continue
        sent=transmissions.get((r['sequence'],*key))
        candidates[key]=dict(signature=signature,command_sequence=int(r['sequence']),received_ns=int(r['now_ns']),
            created_ns=int(r['created_ns']),target_speed_kmh=signature[2],throttle=throttle,brake=brake,
            throttle_transmitted=float(sent['applied_throttle']) if sent else None,
            brake_transmitted=float(sent['applied_brake']) if sent else None,
            transmitted_ns=int(sent['now_ns']) if sent else None)
    selected=[]
    for o in telemetry:
        key=(o['sequence'],o['tracking_epoch']); c=candidates.get(key)
        if c is None: continue
        t=int(o['observation_ns'])
        if o['state_status']!='ready' or o['in_pit']!='false' or int(o['gear_raw'])<2:
            excluded.append(dict(sequence=key[0],tracking_epoch=key[1],reason='not_ready_forward')); continue
        if not (t<=c['created_ns']<=c['received_ns']<t+100_000_000): raise ValueError('Teacher timestamp mismatch/expired')
        if any(not 0<=t-int(o[k])<100_000_000 for k in ('physics_ns','graphics_ns')):
            excluded.append(dict(sequence=key[0],tracking_epoch=key[1],reason='stale_page')); continue
        selected.append((o,c))
    return m, selected, excluded


def prepare(sources_path, config_path):
    c=config(config_path); src=tomllib.loads(Path(sources_path).read_text(encoding='utf-8'))
    if src.get('schema_version')!=1 or not src.get('train'): raise ValueError('Missing train sources')
    groups={s:[Path(p).resolve() for p in src.get(s,[])] for s in ('train','validation','test')}
    paths=[p for g in groups.values() for p in g]
    if len(paths)!=len(set(paths)): raise ValueError('Run appears in more than one source/split')
    prepared=Path(src['prepared']).resolve(); binary=policy_binary()
    prep=read(prepared/'manifest.json')
    if prep['complete'] is not True: raise ValueError('Incomplete prepared state')
    verify_hashes(prep['input_sha256']); verify_hashes(prep['output_sha256'],prepared)
    out=new_folder(c['dataset_dir'],'teacher')
    inputs=hashes([prepared/'manifest.json'])
    preparation_inputs=hashes([sources_path,config_path,binary])
    manifest=dict(schema_version=1,kind='teacher_dataset',tool_version='0.1.0',complete=False,
        config=c,prepared=str(prepared),prepared_sha256=digest(prepared/'manifest.json'),
        feature_names=FIELDS,feature_units=UNITS,source_splits={s:[str(p) for p in g] for s,g in groups.items()},
        input_sha256=inputs,preparation_inputs_sha256=preparation_inputs,training_ready=bool(groups['validation'] and groups['test']),
        limitations='Same track and teacher. Run-separated holdouts are not independent generalization proof.')
    save(out/'manifest.json',manifest)
    save(out/'sources-snapshot.json',src)
    save(out/'config.json',c)
    all_rows={s:[] for s in groups}; exclusions=[]; run_summaries=[]
    try:
        for split, group in groups.items():
            for index, folder in enumerate(group):
                m, joined, excluded=joined_teacher(folder)
                if not Path(m['prepared_path']).samefile(prepared): raise ValueError('Teacher preparation mismatch')
                inputs.update(hashes([folder/'manifest.json']))
                inputs.update({str(folder/n):h for n,h in m['output_sha256'].items()})
                inputs.update(m['input_sha256'])
                stem=f'{split}-{index}'
                frame_path=out/(stem+'-frames.jsonl'); feature_path=out/(stem+'-features.jsonl')
                with frame_path.open('x',encoding='utf-8',newline='\n') as f:
                    for o, command in joined:
                        frame=dict(sequence=int(o['sequence']),observation_ns=int(o['observation_ns']),tracking_epoch=int(o['tracking_epoch']),
                            ready=True,speed_kmh=float(o['speed_kmh']),target_speed_kmh=command['target_speed_kmh'],rpm=float(o['rpm']),
                            lateral_error_m=float(o['lateral_error_m']),heading_error_rad=float(o['heading_error_rad']),s_m=float(o['path_s_m']))
                        f.write(json.dumps(frame,allow_nan=False)+'\n')
                subprocess.run([str(binary),'features',str(prepared),str(frame_path),str(feature_path)],check=True,capture_output=True,text=True)
                with feature_path.open(encoding='utf-8') as f: feature_rows=[json.loads(line) for line in f]
                if len(feature_rows)!=len(joined): raise ValueError('Feature row inventory mismatch')
                last={}; accepted=0
                for (o, command), result in zip(joined,feature_rows):
                    epoch=int(o['tracking_epoch']); t=int(o['observation_ns'])
                    if result['sequence']!=int(o['sequence']) or result['observation_ns']!=t or result['tracking_epoch']!=epoch:
                        raise ValueError('Rust feature provenance mismatch')
                    if result['features'] is None:
                        excluded.append(dict(sequence=o['sequence'],tracking_epoch=epoch,reason='insufficient_causal_history')); continue
                    if epoch in last and t-last[epoch]<50_000_000:
                        excluded.append(dict(sequence=o['sequence'],tracking_epoch=epoch,reason='50ms_sampling')); continue
                    last[epoch]=t; accepted+=1
                    throttle,brake=command['throttle'],command['brake']
                    row=dict(run_id=folder.name,sequence=int(o['sequence']),tracking_epoch=epoch,observation_ns=t,
                        command_sequence=command['command_sequence'],created_ns=command['created_ns'],received_ns=command['received_ns'],
                        features=result['features'],target_u=throttle/.20-brake/.60,throttle=throttle,brake=brake,
                        pedal_class=classify(throttle,brake),target_speed_kmh=command['target_speed_kmh'],
                        speed_kmh=float(o['speed_kmh']),grade_pct=result['features'][7],
                        throttle_transmitted=command['throttle_transmitted'],brake_transmitted=command['brake_transmitted'],transmitted_ns=command['transmitted_ns'],
                        throttle_observed=float(o['throttle_observed']),brake_observed=float(o['brake_observed']))
                    all_rows[split].append(row)
                exclusions.extend(dict(run_id=folder.name,**e) for e in excluded)
                run_summaries.append(dict(run_id=folder.name,split=split,joined=len(joined),accepted=accepted))
        train=all_rows['train']
        if not train: raise ValueError('No training examples')
        normalizer=normalizer_for(train)
        save(out/'normalizer.json',normalizer)
        for split, data in all_rows.items():
            with (out/(split+'.jsonl')).open('x',encoding='utf-8',newline='\n') as f:
                for row in data: f.write(json.dumps(row,allow_nan=False)+'\n')
        with (out/'excluded.csv').open('x',encoding='utf-8',newline='') as f:
            w=csv.DictWriter(f,fieldnames=['run_id','sequence','tracking_epoch','reason']);w.writeheader();w.writerows(exclusions)
        save(out/'validation.json',dict(runs=run_summaries,counts={s:len(r) for s,r in all_rows.items()},
             classes={s:dict(Counter(r['pedal_class'] for r in data)) for s,data in all_rows.items()},excluded=len(exclusions)))
        manifest['counts']={s:len(r) for s,r in all_rows.items()}
        manifest['training_ready']=manifest['training_ready'] and all(all_rows[s] for s in all_rows)
        verify_hashes(inputs);verify_hashes(preparation_inputs);finish(out,manifest)
    except BaseException as exc:
        finish(out,manifest,False,exc);raise
    return out
