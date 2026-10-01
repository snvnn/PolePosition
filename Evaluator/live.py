"""Reconstruct model command ownership separately from stopping time."""
import csv
from pathlib import Path
from ModelAndActuator.report import verify, stats
from ModelTrainer.common import digest


def inspect(run):
    run=Path(run)
    m,s,commands,telemetry,events=verify(run)
    if not m.get('config',{}).get('policy'):raise ValueError('Not a policy run')
    with (run/'policy.csv').open(encoding='utf-8',newline='') as f:policy=list(csv.DictReader(f))
    key=lambda r:(r['observation_sequence'],r['tracking_epoch'])
    accepted={key(r) for r in commands if r['reason']=='accepted' and r['mode']=='Running'}
    active=[r for r in policy if key(r) in accepted]
    model=[r for r in active if r['control_source']=='model']
    by_source={key(r):r for r in model}
    sends=[r for r in commands if r['send_success']=='true' and r['mode']=='Running' and key(r) in by_source]
    for r in sends:
        d=by_source[key(r)]
        if abs(float(r['requested_throttle'])-float(d['model_throttle']))>1e-7 or abs(float(r['requested_brake'])-float(d['model_brake']))>1e-7:
            raise ValueError('Model request/source mismatch')
    if m['config']['policy']['mode']=='shadow' and model:raise ValueError('Shadow used model output')
    duration=0.
    if model:
        first=int(model[0]['observation_ns']);last=int(model[-1]['observation_ns'])
        stops=[int(r['observation_ns']) for r in policy if r['control_source']=='stop' and int(r['observation_ns'])>=last]
        duration=((min(stops) if stops else last)-first)/1e9
    source_rows={(r['sequence'],r['tracking_epoch']):r for r in telemetry}
    observations=[source_rows[key(r)] for r in model]
    unique=list({key(r):r for r in policy}.values())
    return dict(run_id=run.name,mode=m['config']['policy']['mode'],requested_model_seconds=m['config']['policy']['model_seconds'],
        model_json_sha256=digest(Path(m['config']['policy']['model'])/'model.json'),
        complete=m['complete'],normal_shutdown=s['normal_shutdown'],stop_confirmed=s['stop_confirmed'],released=s['released'],dropped=s['dropped'],
        termination_reason=s['termination_reason'],controller_exit_reason=s['controller_exit_reason'],model_observation_interval_seconds=duration,
        model_rows=len(model),successful_model_transmissions=len(sends),request_provenance_verified=True,
        model_speed_min_kmh=min((float(r['speed_kmh']) for r in observations),default=None),
        model_speed_max_kmh=max((float(r['speed_kmh']) for r in observations),default=None),
        model_max_abs_lateral_m=max((abs(float(r['lateral_error_m'])) for r in observations),default=None),
        inference=stats([int(r['inference_ns']) for r in unique if r['inference_ns']]),
        feature=stats([int(r['feature_ns']) for r in unique]),poll_interval=s['poll_interval'],
        observation_age=s['observation_age'],ipc=s['ipc_delivery'],driver=s['driver_call'],
        stop_to_zero_send_ns=s['stop_to_zero_send_ns'],
        duration_caveat='First model observation to first stop observation, excluding stop-confirmation tail. Driver send success and ACC reaction are distinct.')
