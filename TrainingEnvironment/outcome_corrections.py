"""Retain only already verified human correction axes, with a session-held-out split."""
import json
import shutil
from pathlib import Path
from ModelTrainer.common import read,save,hashes,verify_hashes,new_folder,finish,digest
from .corrections import load_corrections
from .continuous_rollout import rebuild
from .outcome_data import start_cut


def supplement(dataset):
    dataset=Path(dataset);m=read(dataset/'manifest.json');verify_hashes(m['output_sha256'],dataset)
    c=read(dataset/'config.json');parent=Path(c['source_model']);pm=read(parent/'manifest.json');speed=parent/'speed-profile.json'
    length=read(speed)['length_m'];audit=[];accepted=[];seen=set();inventory={};quarantine=read(dataset/'quarantine.json')
    for p in sorted(Path(c['run_root']).glob('gamepad-*')):
        if not (p/'summary.json').exists():continue
        s=read(p/'summary.json')
        if not s.get('correction_eligible'):continue
        entry=dict(run=str(p.resolve()),accepted=False)
        try:
            if p.name in c['quarantined_runs']:raise ValueError('Known weaving quarantine')
            sha=digest(p/'samples.jsonl')
            if sha in seen:raise ValueError('Duplicate correction source')
            seen.add(sha)
            examples,extra,counts=load_corrections([p],pm['controls_sha256'],pm['assists_sha256'])
            rows=[json.loads(line) for line in (p/'samples.jsonl').open()]
            policies=[r for r in rows if r['kind']=='policy' and r['mode']=='Running']
            features,positions=rebuild(rows,pm['prepared'],speed,end_ns=policies[-1]['observation_ns']+20_000_000)
            cut=start_cut([(r['observation_ns'],positions[r['observation_ns']][0]) for r in policies],length,c['start_line_radius_m'],c['history_clearance_ns'])
            if cut:quarantine.append(dict(recording=str(p.resolve()),start_ns=policies[0]['observation_ns'],end_ns=cut,reason='start_within_150m_plus_shared_history_clearance'))
            kept=[]
            for r in examples:
                t=r['observation_ns']
                if cut and t<=cut:continue
                if t not in features:raise ValueError('Correction features could not be reconstructed')
                kept.append(dict(r,features=features[t],s_m=positions[t][0],recording=str(p.resolve()),source_kind='verified_human_correction',source_log_sha256=sha,weight=1.))
            if not kept:raise ValueError('No labels after quarantine')
            accepted.append((entry,kept));entry.update(accepted=True,examples=len(kept));inventory.update(extra)
        except (ValueError,KeyError,FileNotFoundError) as error:entry['exclusion_reason']=str(error)
        audit.append(entry);print('CORRECTIONS',entry,flush=True)
    if len(accepted)<2:raise ValueError('Not enough independently verified correction sessions')
    training=[];validation=[]
    for i,(entry,rows) in enumerate(accepted):
        split='validation' if i==len(accepted)-1 else 'train';entry['split']=split
        for r in rows:r['split']=split
        (training if split=='train' else validation).extend(rows)
    out=new_folder(c['output_dir'],'outcome-dataset')
    for p in dataset.iterdir():
        if p.is_file() and p.name!='manifest.json':shutil.copyfile(p,out/p.name)
    save(out/'correction-training.json',training);save(out/'correction-validation.json',validation);save(out/'correction-audit.json',audit)
    save(out/'quarantine.json',quarantine,replace=True)
    summary=read(out/'summary.json');summary.update(correction_training=len(training),correction_validation=len(validation),correction_sessions=len(accepted))
    save(out/'summary.json',summary,replace=True)
    m.update(parent_dataset=str(dataset.resolve()),revision='Verified masked correction labels; last eligible correction session held out')
    m['input_sha256'].update(inventory);m['input_sha256'].update(hashes([dataset/'manifest.json',Path(__file__),Path(__file__).with_name('corrections.py')]))
    finish(out,m);print('CORRECTIONS_DATASET',out,flush=True)


if __name__=='__main__':
    import sys
    supplement(sys.argv[1])
