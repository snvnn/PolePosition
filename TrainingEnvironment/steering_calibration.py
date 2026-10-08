"""Fit a recorded vJoy-to-ACC map on separate runs, verify on held-out runs."""
import json
from pathlib import Path
import numpy as np
from ModelAndActuator.report import verify
from ModelTrainer.common import new_folder, save, hashes, finish, verify_hashes


def pairs(run, delay_ms=0):
    _, summary, commands, telemetry, _ = verify(run)
    if not summary['stop_confirmed'] or not summary['released']:
        raise ValueError('Require completed stop/release evidence')
    commands=[c for c in commands if c['send_success']=='true' and c['mode']=='Running']
    times=np.array([int(c['now_ns']) for c in commands])
    sent=np.array([float(c['applied_steering']) for c in commands])
    obs=np.array([int(t['observation_ns']) for t in telemetry])
    raw=np.array([float(t['steering_raw']) for t in telemetry])
    # Strictly earlier successful sends; no future command information.
    ids=np.searchsorted(times,obs-delay_ms*1_000_000,side='left')-1
    keep=(ids>=0)&(obs<=times[-1])
    return sent[ids[keep]],raw[keep]


def build(train_runs, validation_runs, recording, parent='reinforcement_runs'):
    train_runs=[Path(p).resolve() for p in train_runs];validation_runs=[Path(p).resolve() for p in validation_runs]
    if set(train_runs)&set(validation_runs):raise ValueError('Calibration split overlap')
    inputs=hashes([p/n for p in train_runs+validation_runs for n in
        ('manifest.json','summary.json','commands.csv','telemetry.csv')]+[Path(__file__)])
    options=[]
    for delay in (0,10,20,30,40,50):
        sets=[pairs(p,delay) for p in train_runs]
        x=np.concatenate([s[0] for s in sets]);y=np.concatenate([s[1] for s in sets])
        linear=np.abs(y)<.95
        gain=float(x[linear]@y[linear]/(x[linear]@x[linear]))
        error=float(np.sqrt(np.mean((np.clip(gain*x,-1,1)-y)**2)))
        options.append(dict(delay_ms=delay,gain=gain,rmse=error))
    best=min(options,key=lambda o:o['rmse'])
    measurements=[]
    for p in validation_runs:
        x,y=pairs(p,best['delay_ms']);error=np.clip(best['gain']*x,-1,1)-y
        measurements.append(dict(run=str(p),samples=len(x),rmse=float(np.sqrt(np.mean(error**2))),
                                 max_error=float(np.max(np.abs(error)))))
    x,y=map(np.concatenate,zip(*(pairs(p,best['delay_ms']) for p in train_runs)))
    usable=x[np.abs(y)<.95]
    import csv
    from DrivingRecorder.validate_recording import validate
    source=Path(recording).resolve();lap=validate(source)['laps'][0]
    with (source/'telemetry.csv').open() as f:
        targets=[float(r['steering_raw'])/best['gain'] for r in csv.DictReader(f)
                 if int(r['elapsed_ns'])<=lap['end_elapsed_ns'] and int(r['segment_id'])==lap['segment_id']]
    coverage=bool(min(usable)<=min(targets) and max(targets)<=max(usable))
    passed=best['rmse']<.002 and all(v['rmse']<.002 and v['max_error']<.02 for v in measurements) and coverage
    if not passed:raise ValueError('Measured steering map is insufficient for demonstrated range')
    out=new_folder(parent,'human-steering-calibration')
    inputs.update(hashes([source/'telemetry.csv',source/'summary.json']))
    meta=dict(schema_version=1,kind='human_steering_calibration',complete=False,input_sha256=inputs,
        train_runs=list(map(str,train_runs)),validation_runs=list(map(str,validation_runs)))
    save(out/'manifest.json',meta)
    save(out/'mapping.json',dict(passed=True,gain=best['gain'],delay_ms=best['delay_ms'],
        model='ACC raw steering = clip(gain * transmitted vJoy steering, -1, 1)',
        inverse='vJoy steering = requested ACC raw steering / gain',candidates=options,
        validation=measurements,demonstrated_axis_range=[min(targets),max(targets)],
        measured_unsaturated_axis_range=[float(min(usable)),float(max(usable))],coverage=coverage))
    verify_hashes(inputs);finish(out,meta);print(out,flush=True);return out

if __name__=='__main__':
    build(['actuator_runs/'+p for p in ['demo-1791015225746378900-14196',
          'demo-1791015469341323600-3476','demo-1791015897553798800-5024']],
          ['actuator_runs/'+p for p in ['demo-1790827045494771000-17632','demo-1791018920585936900-12716']],
          'recordings/1791016197077177200-20392-0')
