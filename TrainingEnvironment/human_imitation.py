"""Train-only human demonstration experiment; never accepted by the live loader.

ACC raw steering is a recorded target, NOT a calibrated vJoy command. The
35-dimensional feature layout is shared, but previous steering uses ACC units.
No independent validation, deployment export, critic update or replay insertion.
"""
import argparse
import copy
import csv
import json
from pathlib import Path
import subprocess

import numpy as np
import torch

from DrivingRecorder.validate_recording import validate
from ModelTrainer.common import finish, hashes, new_folder, read, save, verify_hashes
from ModelTrainer.dataset import policy_binary
from TrackModel.model import load_reference
from .actions import from_axes


def select_first_lap(rows, report):
    if not report['format_pass'] or report['errors'] or report['warnings']:
        raise ValueError('Recording must pass loss/continuity validation')
    lap = report['laps'][0]
    if not lap['all_samples_lap_valid']:
        raise ValueError('First lap is not valid')
    selected = [r for r in rows if int(r['segment_id']) == lap['segment_id']
                and int(r['elapsed_ns']) <= lap['end_elapsed_ns']]
    times = np.array([int(r['elapsed_ns']) for r in selected])
    if len(times) < 100 or np.any(np.diff(times) <= 0) or np.any(np.diff(times) >= 100_000_000):
        raise ValueError('Discontinuous first-lap recording')
    return selected, lap


def projection_breaks(rows, geom, length, allow_finish_seam=False):
    """Only a small reference seam may be split; physical jumps still fail."""
    ds = (np.diff(geom[:, 0])+length/2) % length-length/2
    dt = np.diff([int(r['elapsed_ns']) for r in rows])/1e9
    speed = np.array([float(r['speed_kmh']) for r in rows])
    limit = np.maximum(speed[:-1], speed[1:])/3.6*dt+2
    pos = np.array([[float(r['position_x']), float(r['position_z'])] for r in rows])
    if np.any(dt <= 0) or np.any(np.linalg.norm(np.diff(pos, axis=0), axis=1) > limit):
        raise ValueError('Discontinuous physical position/time')
    bad = np.flatnonzero(np.abs(ds) > limit)
    for i in bad:
        near_finish = np.minimum(geom[i:i+2, 0], length-geom[i:i+2, 0]) <= 5
        if not allow_finish_seam or not near_finish.all() or abs(ds[i]) > 5:
            raise ValueError('Discontinuous projected distance')
    return (bad+1).tolist()


def project(rows, reference, length, heading, allow_finish_seam=False):
    """Offline nearest segment projection, matching the native geometric formula."""
    xy = np.array([[r['position_x'], r['position_z']] for r in reference])
    delta = np.roll(xy, -1, axis=0) - xy
    tangent = np.array([[r['tangent_x'], r['tangent_z']] for r in reference])
    ss = np.array([r['s_m'] for r in reference])
    pos = np.array([[float(r['position_x']), float(r['position_z'])] for r in rows])
    result = []
    for offset in range(0, len(pos), 128):
        p = pos[offset:offset+128]
        u = np.clip(np.sum((p[:, None]-xy)*delta, axis=2)/np.sum(delta**2, axis=1), 0, 1)
        projected = xy + u[:, :, None]*delta
        distances = np.sum((p[:, None]-projected)**2, axis=2)
        indices = distances.argmin(axis=1)
        for j, i in enumerate(indices):
            f = u[j, i]
            t = tangent[i] + f*(tangent[(i+1) % len(xy)]-tangent[i])
            t /= np.linalg.norm(t)
            error = p[j]-projected[j, i]
            s = (ss[i]+f*np.linalg.norm(delta[i])) % length
            vehicle = heading['sign']*float(rows[offset+j]['heading_rad'])+heading['offset_rad']
            angle = (vehicle-np.arctan2(t[1], t[0])+np.pi) % (2*np.pi)-np.pi
            result.append((s, t[0]*error[1]-t[1]*error[0], angle))
    result = np.asarray(result)
    if not np.isfinite(result).all() or np.max(np.abs(result[:, 1])) > 15:
        raise ValueError('Invalid/off-track projection')
    projection_breaks(rows, result, length, allow_finish_seam)
    return result


def train(recording, prepared, epochs=50, parent='reinforcement_runs'):
    from stable_baselines3 import SAC
    from .pretrain import OfflineSpaces
    if type(epochs) is not int or not 1 <= epochs <= 200:
        raise ValueError('Epoch limit must be 1..200')
    recording, prepared = Path(recording).resolve(), Path(prepared).resolve()
    report = validate(recording)
    with (recording/'telemetry.csv').open(newline='') as stream:
        selected, lap = select_first_lap(list(csv.DictReader(stream)), report)
    pm = read(prepared/'manifest.json')
    verify_hashes(pm['input_sha256']); verify_hashes(pm['output_sha256'], prepared)
    heading = read(prepared/'heading_profile.json')
    if read(recording/'summary.json')['first_identity'] != heading['identity']:
        raise ValueError('Recording vehicle/track differs from prepared geometry')
    reference = load_reference(pm['track_model_path'])
    length = reference['manifest']['total_length_m']
    geom = project(selected, reference['rows'], length, heading)
    binary = policy_binary()
    inputs = hashes([recording/k for k in ('telemetry.csv','events.csv','metadata.json','summary.json')]
                    + [prepared/'manifest.json', Path(__file__), binary])
    inputs.update(pm['input_sha256'])
    inputs.update({str(prepared/k): v for k,v in pm['output_sha256'].items()})
    out = new_folder(parent, 'human-first-lap-imitation')
    manifest = dict(schema_version=1, kind='human_raw_imitation', complete=False,
                    eligible_for_live=False, input_sha256=inputs, recording=str(recording),
                    prepared=str(prepared), selected_lap=lap, include_approach=True,
                    split='train_only', independent_validation=False,
                    action_names=['acc_steering_raw','observed_throttle_minus_brake'],
                    previous_axes_semantics='previous observed ACC axes, not proven vJoy transmissions',
                    config=dict(epochs=epochs, seed=42, batch_size=256, learning_rate=.0003,
                                hidden_layers=[128,128], device='cpu'),
                    deployment_blockers=['Uncalibrated human ACC steering to vJoy mapping',
                                         'No independent validation lap',
                                         'Human speed profile has not passed live speed-stage checks'])
    save(out/'manifest.json', manifest)
    print(f'Preparing first lap: {out}', flush=True)
    try:
        save(out/'recording-validation.json', report)
        # Only the first complete lap defines the spatial target speed, not later laps.
        grid = np.arange(0, length, 5.)
        bins = [[] for _ in grid]
        for r,g in zip(selected, geom):
            if int(r['elapsed_ns']) >= lap['start_elapsed_ns']:
                bins[min(int(g[0]/5),len(grid)-1)].append(float(r['speed_kmh']))
        known = np.array([i for i,b in enumerate(bins) if b])
        if len(known) < .8*len(grid):
            raise ValueError('Insufficient first-lap spatial speed coverage')
        speeds = np.interp(grid, grid[known], [np.median(bins[i]) for i in known], period=length)
        feature_cap = next(c for c in (10,20,40,80,120,160,200,240) if c >= max(speeds))
        save(out/'speed-profile.json', dict(schema_version=2,length_m=length,s_m=grid.tolist(),speed_kmh=speeds.tolist()))
        axes = [from_axes(float(r['steering_raw']),float(r['throttle']),float(r['brake'])) for r in selected]
        actions_by_sequence = {int(r['sequence']): a for r,a in zip(selected,axes)}
        with (out/'frames.jsonl').open('x') as stream:
            for i in range(1,len(selected)):
                r, prev, g = selected[i], selected[i-1], geom[i]
                if not 2 <= int(r['gear_raw']) <= 9:
                    continue
                frame = dict(sequence=int(r['sequence']),observation_ns=int(r['elapsed_ns']),
                             tracking_epoch=int(r['segment_id']),ready=True,speed_kmh=float(r['speed_kmh']),
                             target_speed_kmh=float(np.interp(g[0],grid,speeds,period=length)),rpm=float(r['rpm']),
                             lateral_error_m=float(g[1]),heading_error_rad=float(g[2]),s_m=float(g[0]))
                stream.write(json.dumps(dict(frame=frame,speed_cap_kmh=float(feature_cap),
                    previous_axes=[float(prev[k]) for k in ('steering_raw','throttle','brake')],
                    previous_applied_ns=int(prev['elapsed_ns']),gear_raw=int(r['gear_raw'])),allow_nan=False)+'\n')
        subprocess.run([str(binary),'features-v2',str(prepared),str(out/'frames.jsonl'),
                        str(out/'features.jsonl'),str(out/'speed-profile.json')],check=True)
        data=[];last=-20_000_000
        with (out/'features.jsonl').open() as stream:
            for i,line in enumerate(stream,1):
                row=json.loads(line)
                if row['features'] is None or row['observation_ns']-last < 20_000_000:
                    continue
                last=row['observation_ns']
                data.append(dict(sequence=row['sequence'],observation_ns=last,features=row['features'],
                                 action=actions_by_sequence[row['sequence']]))
                names=row['feature_names']
        if len(data)<256: raise ValueError('Insufficient examples')
        with (out/'train.jsonl').open('x') as stream:
            for row in data: stream.write(json.dumps(row,allow_nan=False)+'\n')
        raw=np.asarray([r['features'] for r in data],np.float32)
        mean=raw.mean(0);std=raw.std(0);std[std<1e-6]=1
        save(out/'normalizer.json',dict(feature_names=names,mean=mean.tolist(),std=std.tolist()))
        x=torch.from_numpy((raw-mean)/std);y=torch.tensor([r['action'] for r in data],dtype=torch.float32)
        if not torch.isfinite(x).all(): raise ValueError('Nonfinite examples')
        torch.set_num_threads(1)
        model=SAC('MlpPolicy',OfflineSpaces(35),policy_kwargs=dict(net_arch=[128,128],n_critics=2),
                  buffer_size=200000,batch_size=256,learning_rate=.0003,seed=42,device='cpu')
        critic=copy.deepcopy(model.critic.state_dict());target=copy.deepcopy(model.critic_target.state_dict())
        optimizer=torch.optim.Adam(list(model.actor.latent_pi.parameters())+list(model.actor.mu.parameters()),lr=.0003)
        rng=torch.Generator().manual_seed(42)
        def mse():
            with torch.inference_mode(): return (model.actor(x,deterministic=True)-y).square().mean(0).tolist()
        initial=mse();history=[]
        for epoch in range(1,epochs+1):
            for ids in torch.randperm(len(x),generator=rng).split(256):
                loss=(model.actor(x[ids],deterministic=True)-y[ids]).square().mean()
                if not torch.isfinite(loss): raise ValueError('Nonfinite loss')
                optimizer.zero_grad();loss.backward();optimizer.step()
            errors=mse();history.append(dict(epoch=epoch,training_mse=errors))
            if epoch==1 or epoch%10==0: print(f'Epoch {epoch}/{epochs}: training MSE {errors}',flush=True)
        if any(not torch.equal(v,model.critic.state_dict()[k]) for k,v in critic.items()) or any(
                not torch.equal(v,model.critic_target.state_dict()[k]) for k,v in target.items()):
            raise RuntimeError('Critic changed during imitation')
        model.save(out/'model.zip')
        torch.save(dict(optimizer=optimizer.state_dict(),shuffle_rng=rng.get_state(),epoch=epochs),out/'training-state.pt')
        with torch.inference_mode(): predictions=model.actor(x,deterministic=True).numpy()
        np.savez_compressed(out/'training-predictions.npz',predictions=predictions,targets=y.numpy())
        summary=dict(samples=len(data),epochs=epochs,initial_training_mse=initial,final_training_mse=mse(),
                     history=history,independent_validation=False,eligible_for_live=False,
                     critic_unchanged=True,replay_samples=model.replay_buffer.size(),
                     training_only_note='Fit error is not evidence of driving performance or generalization')
        save(out/'summary.json',summary)
        verify_hashes(inputs);finish(out,manifest)
        print(json.dumps(dict(result=str(out),**{k:v for k,v in summary.items() if k!='history'})),flush=True)
    except BaseException as error:
        finish(out,manifest,False,error);raise
    return out


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--recording',required=True);parser.add_argument('--prepared',required=True)
    parser.add_argument('--epochs',type=int,default=50)
    args=parser.parse_args()
    train(args.recording,args.prepared,args.epochs)
