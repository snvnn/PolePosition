"""Bounded recovery of observed relocation, never synthetic training transitions."""
import json
import math
from pathlib import Path
from ModelTrainer.common import read,digest
from .gamepad_online_data import validate_stop, verify_episode_files, verify_logged_end

MAX_RECOVERIES=2

def timing_gap(run,model_hash):
    """Exclude a short timing gap only after independent log and stop checks."""
    run=Path(run);m=read(run/'manifest.json');s=read(run/'summary.json')
    end=s.get('episode_end',{});shutdown=s.get('shutdown',{})
    if not (m.get('complete') and not m.get('policy_comparison') and not s.get('policy_comparison')
            and s.get('model_sha256')==model_hash and end.get('reason')=='observation_timing_gap'
            and (end.get('event') or {}).get('reason')==end['reason']
            and shutdown.get('kind')=='continuous_stop' and shutdown.get('continuous_stop_confirmed') is True
            and shutdown.get('tracking_epoch')==end.get('tracking_epoch')):return None
    verify_episode_files(m,run)
    with (run/'samples.jsonl').open(encoding='utf-8') as stream:rows=[json.loads(line) for line in stream]
    validate_stop(s,rows)
    verify_logged_end(s, rows, requires_event=True)
    start=s['episode_start']['observation_ns'];stop=end['observation_ns']
    obs=[r for r in rows if r['kind']=='observation' and start<=r['observation_ns']<=stop]
    if len(obs)<2 or obs[0]['observation_ns']!=start or obs[-1]['observation_ns']!=stop:return None
    for a,b in zip(obs,obs[1:]):
        dt=b['observation_ns']-a['observation_ns']
        distance=math.hypot(b['sample']['position_x']-a['sample']['position_x'],b['sample']['position_z']-a['sample']['position_z'])
        terminal=b['observation_ns']==stop
        if not (a['state']['status']==b['state']['status']=='ready'
                and a['state']['tracking_epoch']==b['state']['tracking_epoch']==end['tracking_epoch']
                and a['sequence']+1==b['sequence'] and 0<=distance<=5
                and a['sample']['session_index']==b['sample']['session_index']
                and a['sample']['session_type']==b['sample']['session_type']==3
                and ((20_000_000<dt<=50_000_000) if terminal else (0<dt<=20_000_000))):return None
    for o in obs:
        raw=o.get('stop_telemetry',{});t=o['observation_ns']
        if not (raw.get('live') is True and raw.get('identity_ok') is True
                and all(isinstance(raw.get(k),int) and 0<=t-raw[k]<20_000_000
                        for k in ('physics_ns','graphics_ns'))):return None
    if any(r['kind']=='policy' and r['mode']=='Running' and
           (r['authority']!='model' or any(r['label_mask'])) for r in rows):return None
    return dict(run=str(run),excluded=True,recovery_only=True,eligible=False,
        reason='observation_timing_gap',gap_ms=(obs[-1]['observation_ns']-obs[-2]['observation_ns'])/1e6,
        manifest_sha256=digest(run/'manifest.json'),model_sha256=model_hash)


def relocation(run,model_hash,comparison):
    run=Path(run);m=read(run/'manifest.json');s=read(run/'summary.json')
    e=s.get('relocation_evidence') or {}
    if not (m.get('complete') and bool(s.get('policy_comparison'))==comparison
            and s.get('model_sha256')==model_hash and e.get('verified') is True
            and s.get('episode_end',{}).get('reason')=='tracking_not_ready'
            and s['episode_end'].get('event') is None
            and s.get('shutdown',{}).get('kind')=='relocated_standstill'):
        return None
    verify_episode_files(m,run)
    with (run/'samples.jsonl').open(encoding='utf-8') as stream:rows=[json.loads(line) for line in stream]
    validate_stop(s,rows)
    verify_logged_end(s, rows, requires_event=False)
    observations={r['observation_ns']:r for r in rows if r['kind']=='observation'}
    a=observations.get(e['previous_observation_ns']);b=observations.get(e['observation_ns'])
    if not a or not b or b['observation_ns']!=s['episode_end']['observation_ns']:return None
    x,y=a['sample'],b['sample'];dt=b['observation_ns']-a['observation_ns']
    distance=math.hypot(x['position_x']-y['position_x'],x['position_z']-y['position_z'])
    if not (a['state']['status']=='ready' and b['state']['reason']=='suspected_teleport'
            and a['sequence']+1==b['sequence'] and 0<dt<=50_000_000
            and a['state']['tracking_epoch']!=b['state']['tracking_epoch']
            and distance>50 and math.isclose(distance,e['displacement_m'],abs_tol=1e-6)
            and 0<=y['speed_kmh']<=1 and y['gear_raw']==0 and y['session_type']==3
            and 0<=e['spawn_distance_m']<=1):return None
    for row in (a,b):
        raw=row.get('stop_telemetry',{});t=row['observation_ns']
        if not (raw.get('live') is True and raw.get('identity_ok') is True
                and all(isinstance(raw.get(k),int) and 0<=t-raw[k]<100_000_000 for k in ('physics_ns','graphics_ns'))):
            return None
    if any(r['kind']=='policy' and r['mode']=='Running' and
           (r['authority']!='model' or any(r['label_mask'])) for r in rows):return None
    return dict(run=str(run),excluded=True,recovery_only=True,evaluation_only=comparison,
        reason='unattributed_relocation',manifest_sha256=digest(run/'manifest.json'),
        evidence=e,eligible=False,model_sha256=model_hash)
