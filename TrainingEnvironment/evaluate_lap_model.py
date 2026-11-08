"""Compare a candidate and preserved A on the same held-out whole lap."""
import json
from pathlib import Path
import numpy as np
from ModelTrainer.common import read,verify_hashes

def evaluate(candidate,baseline):
    candidate,baseline=Path(candidate),Path(baseline)
    cm=read(candidate/'manifest.json');verify_hashes(cm['output_sha256'],candidate);verify_hashes(cm['input_sha256'])
    ex=read(candidate/'validation-examples.json');train=read(candidate/'training-examples.json')
    key=lambda e:(e['recording'],e['lap_index'])
    assert not {key(e) for e in ex}&{key(e) for e in train}
    assert len({key(e) for e in ex})==1 and len({key(e) for e in train})==8
    x=np.asarray([e['features'] for e in ex],dtype=np.float32);y=np.asarray([e['action'] for e in ex],dtype=np.float32)
    results={}
    for name,folder in [('A',baseline),('whole_lap',candidate)]:
        m=read(folder/'manifest.json');verify_hashes(m['output_sha256'],folder);net=read(folder/'model.json')
        a=(x-np.asarray(net['mean'],dtype=np.float32))/np.asarray(net['std'],dtype=np.float32)
        for i,l in enumerate(net['layers']):
            a=a@np.asarray(l['weight'],dtype=np.float32).T+np.asarray(l['bias'],dtype=np.float32)
            if i<2:a=np.maximum(a,0)
        pred=np.column_stack([np.tanh(a[:,0]),1/(1+np.exp(-np.clip(a[:,1],-80,80))),1/(1+np.exp(-np.clip(a[:,2],-80,80)))])
        assert np.isfinite(pred).all()
        results[name]={'mse':np.mean((pred-y)**2,axis=0).tolist(),'model':str(folder)}
    results['heldout_samples']=len(ex);results['training_samples']=len(train);results['live_verified']=False
    return results
