"""Fail-closed continuous gamepad replay validation; never invent a terminal state."""
import bisect
import copy
import json
from pathlib import Path
import numpy as np
from ModelTrainer.common import read, digest, verify_hashes
from GamepadIO.run_report import inspect_log
from .build_outcome_dataset import accepted_pair
from .outcome_data import continuous, start_cut, reward_components

SEMANTICS = 'gamepad_continuous_iql_v1'


class TerminalResetObservation(ValueError):
    """Validated invalidation followed by reset physics; exclude the whole episode."""


def derive_seed(rows):
    train = []
    held = {r['source_log_sha256'] for r in rows if r['split'] == 'validation'}
    for original in rows:
        if original['split'] != 'train':
            continue
        r = copy.deepcopy(original)
        if r['source_log_sha256'] in held:
            raise ValueError('Cross-split replay leakage')
        if not r['observation_ns'] <= r['send_end_ns'] < r['next_observation_ns']:
            raise ValueError('Seed next observation is not after application')
        if len(r['next_features']) != 35 or not np.isfinite(r['next_features']).all():
            raise ValueError('Seed has no real finite next state')
        if r['terminal_reason'] == 'valid_full_lap':
            if r['reward_components']['terminal'] != 10:
                raise ValueError('Missing valid-lap reward')
            r['terminated'] = False
        r['continuous_semantics'] = SEMANTICS
        train.append(r)
    if not train:
        raise ValueError('Empty training replay')
    return train


def stop_observation_valid(o):
    speed = o['sample']['speed_kmh']
    if not np.isfinite(speed) or not 0 <= speed <= 1:
        return False
    raw = o.get('stop_telemetry')
    if raw is None:  # Historical recordings used projected-state evidence.
        return o['state']['status'] == 'ready'
    t = o['observation_ns']
    return (raw.get('live') is True and raw.get('identity_ok') is True
            and all(isinstance(raw.get(k), int) and 0 <= t-raw[k] < 100_000_000
                    for k in ('physics_ns', 'graphics_ns')))


def stop_observations_continuous(a, b):
    # Stopping outside the projection range still has real, fresh ACC telemetry.
    return (a['state']['tracking_epoch'] == b['state']['tracking_epoch']
            and a['sample'].get('session_index') == b['sample'].get('session_index')
            and a['sequence'] + 1 == b['sequence']
            and 0 < b['observation_ns'] - a['observation_ns'] <= 20_000_000
            and np.linalg.norm(np.array([b['sample'][k]-a['sample'][k]
                                         for k in ('position_x','position_z')])) <= 5)


def verify_episode_files(manifest, run):
    inventory = manifest.get('output_sha256', {})
    if not isinstance(inventory, dict) or not {'samples.jsonl', 'summary.json'}.issubset(inventory):
        raise ValueError('Missing hashes for episode evidence')
    verify_hashes(inventory, run)


def validate_stop(summary, rows):
    if summary.get('cancelled') or summary.get('error') or summary.get('logging_failure'):
        raise ValueError('Cancelled/system-error episode cannot train')
    if summary.get('continuous_semantics') != SEMANTICS:
        raise ValueError('Old episode semantics')
    if summary['episode']['interrupted'] or summary.get('correction_samples', 0):
        raise ValueError('Human intervention episode cannot train')
    validate_shutdown(summary,rows)


def validate_shutdown(summary,rows):
    """Physical stop evidence only. This does NOT make an episode trainable."""
    end = summary['episode_end']
    shutdown = summary['shutdown']
    if not all(shutdown.get(k) is True for k in ('standstill_confirmed', 'neutralized', 'released')):
        raise ValueError('Standstill, neutralization and device release required')
    t = shutdown['observation_ns']
    obs = [r for r in rows if r['kind'] == 'observation' and end['observation_ns'] <= r['observation_ns'] <= t]
    if not obs or obs[-1]['observation_ns'] != t:
        raise ValueError('Missing shutdown observation')
    stable = []
    for o in reversed(obs):
        if (o['state']['tracking_epoch'] != shutdown['tracking_epoch'] or not stop_observation_valid(o)):
            break
        if stable and not stop_observations_continuous(o, stable[-1]):
            break
        stable.append(o)
    if not stable or t-stable[-1]['observation_ns'] < 500_000_000:
        raise ValueError('No fresh same-epoch 0.5-second standstill')
    same = end['tracking_epoch'] == shutdown['tracking_epoch']
    kind = shutdown['kind']
    if kind not in ('continuous_stop', 'untracked_standstill', 'relocated_standstill'):
        raise ValueError('Unknown shutdown kind')
    # A relocation can itself be the terminal observation (same terminal/stop epoch).
    if kind != 'relocated_standstill' and not same:
        raise ValueError('Tracking epoch changed before non-relocated stop')
    if 'continuous_stop_confirmed' in shutdown and shutdown['continuous_stop_confirmed'] is not (kind == 'continuous_stop'):
        raise ValueError('Conflicting continuous stop evidence')
    if summary.get('released', True) is not True or summary.get('standstill_confirmed', True) is not True:
        raise ValueError('Conflicting shutdown evidence')
    neutral = [r for r in rows if r['kind'] == 'neutralized']
    if len(neutral) != 1 or neutral[0]['now_ns'] < t:
        raise ValueError('Neutralization must follow confirmed stop')
    if any(neutral[0]['report'][k] for k in ('lx','ly','rx','ry','lt','rt','buttons')):
        raise ValueError('Non-neutral final report')
    if any(r['kind']=='sent' and r['send_end_ns'] >= neutral[0]['now_ns'] for r in rows):
        raise ValueError('Input sent after verified neutralization')


def verify_logged_end(summary, rows, *, requires_event):
    end = summary['episode_end']
    event = {key: end[key] for key in ('reason', 'observation_ns', 'tracking_epoch')} if requires_event else None
    if end.get('event') != event or summary['episode'].get('end') != event:
        raise ValueError('Failure event disagrees with terminal timestamp, epoch or reason')
    markers = [row['end'] for row in rows if row['kind'] == 'episode_end']
    if markers != [end] or end.get('policy_sha256') != summary['model_sha256']:
        raise ValueError('Missing or conflicting recorded policy end')


def verify_failure_end(summary, rows, length):
    """Match the recorded event to an independently observed invalidation or stall."""
    verify_logged_end(summary, rows, requires_event=True)
    end = summary['episode_end']
    reason = end['reason']
    start = summary['episode_start']
    stop = end['observation_ns']
    observations = [r for r in rows if r['kind'] == 'observation'
                    and start['observation_ns'] <= r['observation_ns'] <= stop]
    if (len(observations) < 2 or observations[0]['observation_ns'] != start['observation_ns']
            or observations[-1]['observation_ns'] != stop
            or observations[0]['state']['tracking_epoch'] != start['tracking_epoch']
            or observations[-1]['state']['tracking_epoch'] != end['tracking_epoch']
            or not all(continuous(a, b) for a, b in zip(observations, observations[1:]))):
        raise ValueError('Failure lacks a continuous observed episode')
    for o in observations[-2:]:
        raw = o.get('stop_telemetry')
        if raw is not None and not (raw.get('live') is True and raw.get('identity_ok') is True
                and all(type(raw.get(key)) is int and 0 <= o['observation_ns'] - raw[key] < 100_000_000
                        for key in ('physics_ns', 'graphics_ns'))):
            raise ValueError('Stale or mismatched terminal telemetry')
    if any(o['sample']['lap_valid'] is not True for o in observations[:-1]):
        raise ValueError('Episode already invalid before recorded end')
    last_valid = observations[-1]['sample']['lap_valid']
    if reason == 'invalid_lap' and last_valid is False:
        return
    if reason != 'progress_stall' or last_valid is not True:
        raise ValueError('No observed valid-to-invalid transition or valid stall')

    # Same rolling window as Episode::observe: the last frame at or before
    # the 10-second boundary, including cyclic progress across the start line.
    boundary = stop - 10_000_000_000
    anchor = next((i for i in range(len(observations)-1, -1, -1)
                   if observations[i]['observation_ns'] <= boundary), None)
    if anchor is None or not np.isfinite(length) or length <= 0:
        raise ValueError('Insufficient history for 10-second stall')
    positions = [o['state']['s_m'] for o in observations[anchor:]]
    progress = sum((b-a+length/2) % length-length/2 for a,b in zip(positions, positions[1:]))
    if not np.isfinite(progress) or progress >= 0.5:
        raise ValueError('Observed progress does not confirm stall')


def collect(run, model, dataset, evaluation=False):
    run, model, dataset = map(Path, (run, model, dataset))
    m, s = read(run/'manifest.json'), read(run/'summary.json')
    if m.get('policy_comparison') or s.get('policy_comparison'):
        raise ValueError('Policy comparison observations are evaluation-only')
    if not m['complete']:
        raise ValueError('Incomplete run')
    verify_episode_files(m, run)
    if s['model_sha256'] != digest(model/'model.json'):
        raise ValueError('Policy changed during episode')
    with (run/'samples.jsonl').open(encoding='utf-8') as stream:
        rows = [json.loads(line) for line in stream]
    validate_stop(s, rows)
    end = s['episode_end']; reason = end['reason']; t_end = end['observation_ns']
    allowed = ('evaluation_complete',) if evaluation else ('invalid_lap', 'progress_stall')
    if reason not in allowed or not end.get('event') or end['event']['reason'] != reason:
        raise ValueError('No confirmed eligible episode end: '+reason)
    length = read(model/'speed-profile.json')['length_m']
    if not evaluation:
        verify_failure_end(s, rows, length)
    c = read(dataset/'config.json')
    if run.name in c['quarantined_runs']:
        raise ValueError('Quarantined weaving run')
    policies = [r for r in rows if r['kind'] == 'policy' and r['mode'] == 'Running']
    if not policies or any(p['authority'] != 'model' or any(p['label_mask']) for p in policies):
        raise ValueError('Episode was not autonomous')
    prefix = [r for r in rows if (r['kind']=='observation' and r['observation_ns']<=t_end)
              or (r['kind']=='policy' and r['now_ns']<t_end)
              or (r['kind']=='sent' and r['send_end_ns']<t_end)]
    transport, pairs = inspect_log(prefix)
    if not transport['passed'] or not pairs or any(not accepted_pair(*p) for p in pairs):
        raise ValueError('Input/observation transport validation failed: '+str(transport))
    obs = [r for r in prefix if r['kind']=='observation' and r['observation_ns']>=pairs[0][0]['observation_ns']]
    if (not obs or obs[-1]['observation_ns'] != t_end
            or (not evaluation and obs[0]['observation_ns'] < s['episode_start']['observation_ns'])
            or (evaluation and any(not continuous(a,b) for a,b in zip(obs,obs[1:])))):
        raise ValueError('Missing terminal observation or interrupted trajectory')
    bytime = {o['observation_ns']:o for o in obs}
    sent = [r for r in prefix if r['kind']=='sent']; times = [r['send_end_ns'] for r in sent]
    cut = start_cut([(o['observation_ns'],o['state']['s_m']) for o in obs], length, c['start_line_radius_m'], c['history_clearance_ns'])
    laps = s['episode']['lap_events']; output=[]
    for i,(o,p,applied) in enumerate(pairs):
        t = o['observation_ns']; nt = pairs[i+1][0]['observation_ns'] if i+1<len(pairs) else t_end
        if cut and t <= cut:
            continue
        n = bytime.get(nt)
        if n is None or not applied['send_end_ns'] < nt or not 0 < nt-t <= 50_000_000:
            raise ValueError('No causal next observation')
        fx,nx = o.get('features'),n.get('features')
        if fx is None or len(fx)!=35 or not np.isfinite(fx).all():
            raise ValueError('Missing real native features')
        if not np.allclose(fx,p['features'],rtol=0,atol=0):
            raise ValueError('Policy observation mismatch')
        held = sent[bisect.bisect_left(times,applied['send_end_ns']):bisect.bisect_left(times,nt)]
        if any(r['mode']!='Running' or r['authority']!='model' or r['report']!=applied['report'] for r in held):
            raise ValueError('Applied action not held to next observation')
        prior=bisect.bisect_left(times,t)-1
        if prior<0:raise ValueError('Missing strictly earlier input')
        a,b=o['state'],n['state']; ds=(b['s_m']-a['s_m']+length/2)%length-length/2
        displacement=np.hypot(b['position_x']-a['position_x'],b['position_z']-a['position_z'])
        if abs(ds)>displacement+3:raise ValueError('Progress discontinuity')
        if nx is None or len(nx)!=35 or not np.isfinite(nx).all():
            raw=n.get('stop_telemetry',{});sample=n['sample']
            # Never synthesize next_features or relabel the preceding valid state.
            # Only this specific terminal reset is recoverable, after transport,
            # trajectory, invalidation, fresh standstill and release validation.
            reset_terminal=(not evaluation and reason=='invalid_lap' and nt==t_end and nx is None
                and s['shutdown']['kind']=='relocated_standstill'
                and s['shutdown']['tracking_epoch']!=end['tracking_epoch']
                and all(sample.get(k)==0 for k in ('gear_raw','rpm','speed_kmh','fuel_litres'))
                and sample.get('session_type')==3 and raw.get('live') is True
                and raw.get('identity_ok') is True
                and all(isinstance(raw.get(k),int) and 0<=nt-raw[k]<100_000_000
                        for k in ('physics_ns','graphics_ns')))
            if reset_terminal:raise TerminalResetObservation('terminal_reset_observation_unavailable')
            raise ValueError('Missing real native features')
        axes=[applied['axes'][k] for k in ('steering','throttle','brake')]
        previous=[sent[prior]['axes'][k] for k in ('steering','throttle','brake')]
        event=reason if nt==t_end and not evaluation else ('valid_full_lap' if any(t<at<=nt for at in laps) else None)
        rewards=dict(c['reward'],progress_stall=-10.)
        components=reward_components([a['s_m'],a['lateral_error_m'],a['heading_error_rad']],
            [a['s_m']+ds,b['lateral_error_m'],b['heading_error_rad']],previous,axes,(nt-t)/1e9,event,rewards)
        output.append(dict(features=fx,next_features=nx,action=p['action'],applied_axes=axes,
            observation_ns=t,send_end_ns=applied['send_end_ns'],next_observation_ns=nt,
            report=applied['report'],tracking_epoch=a['tracking_epoch'],dt=(nt-t)/1e9,
            reward=sum(components.values()),reward_components=components,terminated=event in ('invalid_lap','progress_stall'),
            truncated=False,terminal_reason=event,split='train',source_log_sha256=digest(run/'samples.jsonl') if i==0 else '',
            continuous_semantics=SEMANTICS))
    if not output or (not evaluation and not output[-1]['terminated']):
        raise ValueError('Quarantine or missing endpoint prevents training')
    # One content identity for every record, without rehashing a large log per sample.
    sha=digest(run/'samples.jsonl')
    for r in output:r['source_log_sha256']=sha
    return output,s
