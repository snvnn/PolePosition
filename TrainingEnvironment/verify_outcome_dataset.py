"""Artifact-level checks, independent of the dataset construction pass."""
from collections import Counter
from pathlib import Path
import numpy as np
from ModelTrainer.common import read,verify_hashes,digest


def verify(folder):
    folder=Path(folder);m=read(folder/'manifest.json');verify_hashes(m['output_sha256'],folder)
    c=read(folder/'config.json');quarantine=read(folder/'quarantine.json');transitions=read(folder/'transitions.json')
    blocked={Path(q['recording']).resolve() for q in quarantine if q.get('scope')=='whole_run'}
    assert len(blocked)==len(c['quarantined_runs'])
    seen=set();groups={};run_split={};hash_split={};counts=Counter()
    for r in transitions:
        run=Path(r['run']).resolve();t=r['observation_ns'];nt=r['next_observation_ns'];key=(run,t)
        assert run not in blocked and key not in seen;seen.add(key)
        assert t<=r['send_end_ns']<nt and 0<r['dt']<=.05
        assert abs((nt-t)/1e9-r['dt'])<1e-9 and r['next_sequence']>r['sequence']
        for name in ['features','next_features']:
            a=np.array(r[name]);assert a.shape==(35,) and np.isfinite(a).all()
        a=np.array(r['action']);assert a.shape==(3,) and -1<=a[0]<=1 and np.all((a[1:]>=0)&(a[1:]<=1))
        assert abs(sum(r['reward_components'].values())-r['reward'])<1e-9
        assert not r['truncated']
        assert r['terminated']==(r['terminal_reason'] is not None)
        if r['terminated']:
            assert r['terminal_reason'] in ('invalid_lap','valid_full_lap')
            assert r['reward_components']['terminal']==c['reward'][r['terminal_reason']]
            counts[(r['split'],r['terminal_reason'])]+=1
        else:assert r['reward_components']['terminal']==0
        run_split.setdefault(run,set()).add(r['split']);hash_split.setdefault(r['source_log_sha256'],set()).add(r['split'])
        groups.setdefault(run,[]).append(r)
        for q in quarantine:
            if Path(q['recording']).resolve()==run and q.get('scope')!='whole_run':assert t>q['end_ns'] or nt<q['start_ns']
    assert all(len(v)==1 for v in list(run_split.values())+list(hash_split.values()))
    audit={Path(e['run']).resolve():e for e in read(folder/'transition-audit.json') if e['accepted']}
    for run,rows in groups.items():
        assert rows[-1]['terminal_reason']=='invalid_lap'
        assert rows[-1]['next_observation_ns']==audit[run]['event']['observation_ns']
        assert digest(run/'samples.jsonl')==rows[0]['source_log_sha256']
    human=read(folder/'teacher-training.json');val=read(folder/'teacher-validation.json');success=read(folder/'success-training.json')
    assert not {r['recording'] for r in human}&{r['recording'] for r in val}
    assert not {(r['recording'],r['sequence']) for r in human}&{(r['recording'],r['sequence']) for r in val}
    normal=read(folder/'normalization.json');raw=np.array([r['features'] for r in human if 0 in r['phases_ms']],dtype=np.float32)
    assert len(raw)==normal['count'];assert np.array_equal(raw.mean(0),np.array(normal['mean'],dtype=np.float32))
    std=raw.std(0);std[std<1e-6]=1;assert np.array_equal(std,np.array(normal['std'],dtype=np.float32))
    cs=read(folder/'correction-training.json');cv=read(folder/'correction-validation.json')
    assert not {r['source_log_sha256'] for r in cs}&{r['source_log_sha256'] for r in cv}
    assert not {r['source_log_sha256'] for r in cs+cv}&set(hash_split)
    assert all(r['mask'] in ([1,0,0],[1,1,1]) for r in cs+cv)
    assert all(r['source_kind']=='autonomous_success' for r in success)
    return dict(passed=True,transitions=len(transitions),runs=len(groups),terminal_counts={str(k):v for k,v in counts.items()},
        human_training=len(human),human_validation=len(val),success=len(success),correction_training=len(cs),correction_validation=len(cv),
        whole_run_quarantines=len(blocked),normalization_human_only_verified=True,no_session_overlap=True)


if __name__=='__main__':
    import sys,json
    print(json.dumps(verify(sys.argv[1]),indent=2))
