"""Versioned raw-XInput imitation. Never converts ACC steering into stick labels."""
import bisect
import copy
import csv
import json
from pathlib import Path
import subprocess
import tomllib
import numpy as np
from ModelTrainer.common import read, save, hashes, finish, new_folder, digest, verify_hashes

SEMANTICS = 'gamepad_v1'
ACTION_NAMES = ['stick_x', 'rt', 'lt']

def masked_mse(prediction,target,mask):
    return ((prediction-target).square()*mask).sum()/mask.sum()


def read_inputs(path):
    with Path(path).open(newline='', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    previous = None
    slot = None
    for row in rows:
        for k in ('sequence','segment_id','read_start_ns','read_end_ns','slot','packet','buttons','lx','ly','rx','ry','lt','rt','normalization_version'):
            row[k] = int(row[k])
        for k in ('stick_x','stick_y','right_x','right_y','throttle','brake'):
            row[k] = float(row[k])
        if row['connected'] != 'true' or row['normalization_version'] != 1 or not 0 <= row['slot'] <= 3:
            raise ValueError('Disconnected/unsupported gamepad input')
        if slot is None: slot = row['slot']
        if row['slot'] != slot or row['read_start_ns'] < 0 or row['segment_id'] < 0 or row['read_start_ns'] > row['read_end_ns']:
            raise ValueError('Changed device or reversed read interval')
        if previous and (row['sequence'] != previous['sequence']+1 or row['read_start_ns'] <= previous['read_end_ns']):
            raise ValueError('Missing input or reversed clock')
        if not previous and row['sequence'] != 1: raise ValueError('Missing initial input')
        expected = [row[k]/(32768 if row[k]<0 else 32767) for k in ('lx','ly','rx','ry')] + [row['rt']/255,row['lt']/255]
        actual = [row[k] for k in ('stick_x','stick_y','right_x','right_y','throttle','brake')]
        if (any(not -32768<=row[k]<=32767 for k in ('lx','ly','rx','ry')) or
            any(not 0<=row[k]<=255 for k in ('lt','rt')) or not 0<=row['buttons']<=65535 or not 0<=row['packet']<=2**32-1 or
            not np.isfinite(actual).all() or not np.allclose(actual,expected,atol=1e-12,rtol=0)):
            raise ValueError('Input normalization mismatch')
        previous = row
    if not rows: raise ValueError('Empty input recording')
    return rows


def align(observations, inputs):
    """Inputs after state are labels; only strictly prior inputs enter features."""
    starts = [r['read_start_ns'] for r in inputs]
    ends = [r['read_end_ns'] for r in inputs]
    result = []
    for r in observations:
        t, segment = int(r['elapsed_ns']), int(r['segment_id'])
        before = bisect.bisect_left(ends,t)-1
        after = bisect.bisect_left(starts,t)
        if before < 0 or after >= len(inputs): continue
        p, a = inputs[before], inputs[after]
        if p['segment_id'] != segment or a['segment_id'] != segment: continue
        if a['read_end_ns']-t > 20_000_000 or t-p['read_end_ns'] >= 100_000_000: continue
        # Long gaps cannot become artificial transitions through a held last value.
        if a['read_start_ns']-p['read_end_ns'] > 20_000_000: continue
        result.append((r,p,a))
    return result


def prepare_recording(recording, prepared, speed_file, output, should_stop=lambda:False):
    from DrivingRecorder.validate_recording import validate
    from TrackModel.model import load_reference
    from .human_imitation import project, projection_breaks
    from ModelTrainer.dataset import policy_binary
    recording, prepared = Path(recording).resolve(), Path(prepared).resolve()
    report, meta, summary = validate(recording), read(recording/'metadata.json'), read(recording/'summary.json')
    if not report['format_pass'] or report['errors'] or not summary['normal_shutdown'] or summary['dropped_samples'] or summary['dropped_events']:
        raise ValueError('Invalid/lossy recording')
    if meta.get('input_semantics') != SEMANTICS: raise ValueError('Raw gamepad recording required')
    if meta.get('diagnostic_only') is not False or summary.get('gamepad_training_eligible') is not True:
        raise ValueError('Only validated teacher recordings may train a model')
    profile = meta['input_profile']
    for name, field in [('controls','controls'),('assists','assists')]:
        if digest(recording/f'{name}.snapshot.json') != profile['input_sha256'][profile[field]]:
            raise ValueError('Recording settings snapshot mismatch')
    with (recording/'telemetry.csv').open(newline='') as f: rows=list(csv.DictReader(f))
    pm=read(prepared/'manifest.json'); heading=read(prepared/'heading_profile.json')
    if summary['first_identity']!=heading['identity']: raise ValueError('Car/track mismatch')
    reference=load_reference(pm['track_model_path']); length=reference['manifest']['total_length_m']
    if should_stop(): raise InterruptedError('user_stop')
    geom=project(rows, reference['rows'], length, heading, allow_finish_seam=True)
    seam_breaks=projection_breaks(rows,geom,length,allow_finish_seam=True)
    # A seam is a history reset, never a fabricated smooth path or extra travel.
    contexts={}; epoch=0; start_ns=0; previous_segment=None
    for i,row in enumerate(rows):
        if i in seam_breaks or row['segment_id']!=previous_segment:
            epoch+=1;start_ns=int(row['elapsed_ns'])
        contexts[int(row['sequence'])]=(epoch,start_ns)
        previous_segment=row['segment_id']
    save(output/'alignment-review.json',dict(
        reference_seam_break_sequences=[int(rows[i]['sequence']) for i in seam_breaks],
        history_reset_at_each_break=True,source_termination=summary['reason']))
    by_sequence={int(r['sequence']): g for r,g in zip(rows,geom)}
    speed=read(speed_file); cap=next(v for v in (10,20,40,80,120,160,200,240) if v>=max(speed['speed_kmh']))
    inputs=read_inputs(recording/'inputs.csv')
    if len(inputs)!=summary.get('input_samples'):raise ValueError('Input log tail missing')
    aligned=align(rows,inputs)
    actions={}; frames=output/'frames.jsonl'; features=output/'features.jsonl'
    with frames.open('x') as f:
        for row,prev,action in aligned:
            if should_stop(): raise InterruptedError('user_stop')
            if not 2<=int(row['gear_raw'])<=9 or row['lap_valid']!='true': continue
            seq=int(row['sequence']); g=by_sequence[seq]; t=int(row['elapsed_ns'])
            epoch,start_ns=contexts[seq]
            if prev['read_end_ns']<start_ns: continue
            target=float(np.interp(g[0],speed['s_m'],speed['speed_kmh'],period=length))
            entry=dict(frame=dict(sequence=seq,observation_ns=t,tracking_epoch=epoch,ready=True,
                speed_kmh=float(row['speed_kmh']),target_speed_kmh=target,rpm=float(row['rpm']),lateral_error_m=float(g[1]),heading_error_rad=float(g[2]),s_m=float(g[0])),
                speed_cap_kmh=cap,previous_axes=[prev['stick_x'],prev['throttle'],prev['brake']],previous_applied_ns=prev['read_end_ns'],gear_raw=int(row['gear_raw']))
            f.write(json.dumps(entry,allow_nan=False)+'\n')
            actions[seq]=[action['stick_x'],action['throttle'],action['brake']]
    subprocess.run([str(policy_binary()),'features-gamepad',str(prepared),str(frames),str(features),str(speed_file)],check=True)
    selected=[]; deadline=None; epoch=None
    with features.open() as f:
        for line in f:
            r=json.loads(line)
            if r['features'] is None: continue
            if epoch!=r['tracking_epoch']: epoch=r['tracking_epoch'];deadline=r['observation_ns']
            if r['observation_ns']<deadline: continue
            deadline+=((r['observation_ns']-deadline)//20_000_000+1)*20_000_000
            selected.append(dict(features=r['features'],action=actions[r['sequence']],sequence=r['sequence'],observation_ns=r['observation_ns']))
    if len(selected)<256: raise ValueError('Need at least 256 valid examples per demonstration')
    save(output/'examples.json',selected)
    return selected, r['feature_names'], profile


def train(config, should_stop=lambda:False, on_progress=lambda x:None):
    import torch
    from ModelTrainer.dataset import policy_binary
    c=tomllib.loads(Path(config).read_text(encoding='utf-8'))
    correction_paths=c.get('corrections',[])
    if c.get('require_corrections') and not correction_paths:raise ValueError('No completed human correction recordings selected yet')
    if correction_paths and not c.get('parent_model'):raise ValueError('Correction learning requires the previous gamepad model')
    recordings=[Path(p).resolve() for p in c['recordings']]
    if len(recordings)!=3 or len(set(recordings))!=3: raise ValueError('Exactly three distinct recordings required, ordered 2 train then 1 validation')
    paths=[p/n for p in recordings for n in ('metadata.json','summary.json','telemetry.csv','inputs.csv','controls.snapshot.json','assists.snapshot.json')]
    # Copies cannot manufacture an independent validation drive.
    if len({digest(p/'telemetry.csv') for p in recordings})!=3: raise ValueError('Duplicate demonstrations')
    paths += [Path(config),Path(c['prepared'])/'manifest.json',Path(c['speed_profile']),Path(__file__),policy_binary()]
    inventory=hashes(paths)
    out=new_folder(c.get('output_dir','reinforcement_runs'),'gamepad-imitation')
    m=dict(schema_version=1,kind='gamepad_imitation',semantics=SEMANTICS,complete=False,input_sha256=inventory,
           recordings=list(map(str,recordings)),split=['train','train','validation'],prepared=str(Path(c['prepared']).resolve()))
    save(out/'manifest.json',m); on_progress(dict(phase='preparing',result_dir=str(out)))
    try:
        sets=[]; profiles=[]
        for i,p in enumerate(recordings):
            d=out/f'demonstration-{i+1}'; d.mkdir()
            rows,names,profile=prepare_recording(p,c['prepared'],c['speed_profile'],d,should_stop)
            sets.append(rows);profiles.append(profile)
        settings=lambda p:(p['input_sha256'][p['controls']],p['input_sha256'][p['assists']],p['steam_input_disabled'])
        if len({settings(p) for p in profiles})!=1: raise ValueError('Teacher settings differ between demonstrations')
        corrections=[];parent_network=None
        if correction_paths:
            from .corrections import load_corrections
            parent=Path(c['parent_model']).resolve();pm=read(parent/'manifest.json')
            verify_hashes(pm['output_sha256'],parent)
            if not pm['complete'] or pm['semantics']!=SEMANTICS or pm['recordings']!=list(map(str,recordings)) or pm['controls_sha256']!=settings(profiles[0])[0] or pm['assists_sha256']!=settings(profiles[0])[1]:raise ValueError('Parent model/split/profile mismatch')
            parent_network=read(parent/'model.json')
            if parent_network['feature_names']!=names or parent_network['action_names']!=ACTION_NAMES:raise ValueError('Correction feature/action schema mismatch')
            corrections,extra,counts=load_corrections(correction_paths,*settings(profiles[0])[:2])
            inventory.update(extra);inventory.update(hashes([parent/'manifest.json',parent/'model.json',Path(__file__).with_name('corrections.py')]))
            m.update(input_sha256=inventory,parent_model=str(parent),corrections=counts,normalization='frozen parent model',correction_label_semantics='steer-only mask [1,0,0]; full takeover mask [1,1,1]')
            save(out/'manifest.json',m,replace=True);save(out/'correction-examples.json',corrections)
        if should_stop():raise InterruptedError('user_stop')
        torch.set_num_threads(1);torch.manual_seed(42);np.random.seed(42)
        training=sets[0]+sets[1]+corrections; validation=sets[2]
        raw=np.array([r['features'] for r in training],dtype=np.float32); mean=raw.mean(0);std=raw.std(0);std[std<1e-6]=1
        if parent_network is not None:
            mean=np.array(parent_network['mean'],dtype=np.float32);std=np.array(parent_network['std'],dtype=np.float32)
        if mean.shape!=(35,) or std.shape!=(35,) or not np.isfinite(mean).all() or not np.isfinite(std).all() or np.any(std<=0):raise ValueError('Invalid frozen normalization')
        x=torch.tensor((raw-mean)/std);y=torch.tensor([r['action'] for r in training],dtype=torch.float32)
        masks=torch.tensor([r.get('mask',[1,1,1]) for r in training],dtype=torch.float32)
        vx_raw=np.array([r['features'] for r in validation],dtype=np.float32)
        vx=torch.tensor((vx_raw-mean)/std);vy=torch.tensor([r['action'] for r in validation],dtype=torch.float32)
        if not torch.isfinite(x).all() or not torch.isfinite(vx).all(): raise ValueError('Nonfinite training observations')
        network=torch.nn.Sequential(torch.nn.Linear(35,128),torch.nn.ReLU(),torch.nn.Linear(128,128),torch.nn.ReLU(),torch.nn.Linear(128,3))
        if parent_network is not None:
            with torch.no_grad():
                for i,layer in zip((0,2,4),parent_network['layers']):
                    network[i].weight.copy_(torch.tensor(layer['weight']));network[i].bias.copy_(torch.tensor(layer['bias']))
        def predict(a):
            z=network(a);return torch.cat((z[:,:1].tanh(),z[:,1:].sigmoid()),1)
        optimizer=torch.optim.Adam(network.parameters(),lr=.0003)
        rng=torch.Generator().manual_seed(42);best=float('inf');best_state=None;history=[];updates=0
        epochs=c.get('epochs',50)
        if type(epochs)!=int or not 1<=epochs<=50: raise ValueError('Epoch limit 1..50')
        for epoch in range(1,epochs+1):
            for ids in torch.randperm(len(x),generator=rng).split(256):
                if should_stop(): raise InterruptedError('user_stop')
                loss=masked_mse(predict(x[ids]),y[ids],masks[ids])
                if not torch.isfinite(loss): raise ValueError('Nonfinite loss')
                optimizer.zero_grad();loss.backward();optimizer.step()
                updates+=1
            with torch.inference_mode(): errors=(predict(vx)-vy).square().mean(0).tolist()
            score=sum(errors)/3
            if score<best:
                best=score;best_epoch=epoch;best_state=copy.deepcopy(network.state_dict())
                torch.save(dict(semantics=SEMANTICS,model=best_state,optimizer=optimizer.state_dict(),epoch=epoch,torch_rng=torch.get_rng_state(),shuffle_rng=rng.get_state(),mean=mean,std=std),out/'best.tmp')
                (out/'best.tmp').replace(out/'checkpoint.pt')
            history.append(dict(epoch=epoch,validation_mse=errors));on_progress(dict(phase='training',epoch=epoch,updates=updates,loss=float(loss.detach()),validation_mse=errors,result_dir=str(out)))
        if should_stop(): raise InterruptedError('user_stop')
        network.load_state_dict(best_state)
        layers=[dict(weight=network[i].weight.detach().tolist(),bias=network[i].bias.detach().tolist()) for i in (0,2,4)]
        save(out/'model.json',dict(semantics=SEMANTICS,feature_names=names,action_names=ACTION_NAMES,mean=mean.tolist(),std=std.tolist(),layers=layers))
        probes=vx_raw[np.linspace(0,len(vx_raw)-1,min(256,len(vx_raw)),dtype=int)]
        with (out/'parity-input.jsonl').open('x') as f:
            for a in probes:f.write(json.dumps(a.tolist())+'\n')
        subprocess.run([str(policy_binary()),'predict-gamepad',str(out/'model.json'),str(out/'parity-input.jsonl'),str(out/'parity-output.jsonl')],check=True)
        with torch.inference_mode(): expected=predict(torch.tensor((probes-mean)/std)).numpy()
        actual=np.array([json.loads(s) for s in (out/'parity-output.jsonl').read_text().splitlines()])
        error=float(abs(actual-expected).max())
        if not np.isfinite(actual).all() or error>1e-5: raise ValueError('Python/Rust parity failed')
        baseline=((vx_raw[:,19:22]-vy.numpy())**2).mean(0).tolist()
        save(out/'summary.json',dict(semantics=SEMANTICS,best_epoch=best_epoch,training_examples=len(training),correction_examples=len(corrections),validation_examples=len(validation),validation_mse=history[best_epoch-1]['validation_mse'],persistence_mse=baseline,history=history,parity_max_error=error,live_performance_verified=False))
        import shutil
        shutil.copyfile(c['speed_profile'],out/'speed-profile.json')
        m.update(controls_sha256=settings(profiles[0])[0],assists_sha256=settings(profiles[0])[1],parity_passed=True,prepared_sha256=digest(Path(c['prepared'])/'manifest.json'))
        if should_stop(): raise InterruptedError('user_stop')
        verify_hashes(inventory);finish(out,m);return out
    except BaseException as e:
        finish(out,m,False,str(e));raise
