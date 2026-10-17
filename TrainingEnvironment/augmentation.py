"""Conservative teacher curation and reproducible training-only augmentation."""
import math
import numpy as np


def invalid_windows(rows, before_s=2., after_s=1.):
    windows = []
    for a,b in zip(rows, rows[1:]):
        if a['segment_id']==b['segment_id'] and a['lap_valid']=='true' and b['lap_valid']=='false':
            t=int(b['elapsed_ns'])
            windows.append((b['segment_id'],t-int(before_s*1e9),t+int(after_s*1e9)))
    return windows


def excluded(row, windows):
    return any(seg==row['segment_id'] and a<=int(row['elapsed_ns'])<=b for seg,a,b in windows)


def phase_select(rows, offsets_ms):
    """Return actual feature rows once, with all phase memberships preserved."""
    deadlines={}; epoch=None
    for r in rows:
        if r['features'] is None:
            continue
        if epoch!=r['tracking_epoch']:
            epoch=r['tracking_epoch']
            deadlines={o:r['observation_ns']+int(o*1e6) for o in offsets_ms}
        phases=[]
        for o,d in deadlines.items():
            if r['observation_ns']>=d:
                phases.append(o)
                deadlines[o]+=((r['observation_ns']-d)//20_000_000+1)*20_000_000
        if phases:
            yield r,phases


def sample_weight(features, action, config):
    important=action[2]>=config['brake_threshold'] or max(abs(features[i]) for i in (8,9,10,11))>=config['curvature_threshold']
    return config['important_weight'] if important else 1.


def noisy_features(raw, generator, config):
    """Small local measurement offsets; derivative features remain unchanged."""
    import torch
    out=raw.clone()
    mask=torch.rand(len(raw),generator=generator)<config['probability']
    lateral=torch.randn(len(raw),generator=generator).mul(config['lateral_std_m']).clamp(-config['lateral_clip_m'],config['lateral_clip_m'])
    angle=torch.randn(len(raw),generator=generator).mul(config['heading_std_rad']).clamp(-config['heading_clip_rad'],config['heading_clip_rad'])
    out[mask,4]+=lateral[mask]
    theta=torch.atan2(raw[:,5],raw[:,6])+angle
    out[mask,5]=theta[mask].sin();out[mask,6]=theta[mask].cos()
    return out
