"""Fresh imitation from whole, independently split human takeover runs."""
from pathlib import Path
from ModelTrainer.common import read, digest, hashes
from ModelTrainer.dataset import policy_binary
from .corrections import load_corrections

def prepare(c, config):
    train=[Path(p).resolve() for p in c['train_runs']]
    validation=Path(c['validation_run']).resolve()
    runs=train+[validation]
    if not train or len(set(runs))!=len(runs):raise ValueError('Training/validation runs overlap')
    if len({digest(p/'samples.jsonl') for p in runs})!=len(runs):raise ValueError('Duplicate teacher content')
    inventory=hashes([Path(config),Path(__file__),Path(__file__).with_name('gamepad.py'),Path(__file__).with_name('corrections.py'),policy_binary(),Path(c['prepared'])/'manifest.json',Path(c['speed_profile'])])
    sets=[];signature=None;names=None;counts=[]
    for p in runs:
        m=read(p/'manifest.json');parent=read(Path(m['model'])/'manifest.json')
        current=(parent['controls_sha256'],parent['assists_sha256'])
        if signature is not None and current!=signature:raise ValueError('Teacher settings differ')
        signature=current
        if Path(m['prepared']).resolve()!=Path(c['prepared']).resolve():raise ValueError('Teacher path reference differs')
        ex,extra,_=load_corrections([p],*signature)
        # No model pedals or model-only frames become fresh imitation targets.
        ex=[r for r in ex if r['mask']==[1,1,1]]
        if len(ex)<256:raise ValueError('Need 256 full human labels per run')
        if c.get('feature_transform'):
            if c['feature_transform']!='seam_bridge_15m_v1':raise ValueError('Unknown feature transform')
            from .seam_features import rebuild
            rebuilt=rebuild(p,c['prepared'],c['speed_profile'])
            for e in ex:
                if e['observation_ns'] not in rebuilt:raise ValueError('Rebuilt feature missing')
                e['features']=rebuilt[e['observation_ns']]
            inventory.update(hashes([Path(__file__).with_name('seam_features.py')]))
        feature_names=read(Path(m['model'])/'model.json')['feature_names']
        if names is not None and names!=feature_names:raise ValueError('Feature schema mismatch')
        names=feature_names;sets.append(ex);inventory.update(extra)
        counts.append(dict(run=str(p),examples=len(ex),split='validation' if p==validation else 'train'))
    profile={'controls':'controls','assists':'assists','input_sha256':{'controls':signature[0],'assists':signature[1]},'steam_input_disabled':True}
    manifest=dict(schema_version=1,kind='gamepad_imitation',semantics='gamepad_v1',complete=False,
        teacher_source='intervention_logs',initialization='random_seed_42',normalization='training runs only',
        recordings=list(map(str,runs)),split=['train']*len(train)+['validation'],validation_run=str(validation),
        prepared=str(Path(c['prepared']).resolve()),input_sha256=inventory,teacher_runs=counts)
    manifest['feature_transform']=c.get('feature_transform','original')
    return sum(sets[:-1],[]),sets[-1],names,[profile],inventory,manifest
