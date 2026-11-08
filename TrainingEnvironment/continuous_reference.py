"""Immutable continuous reference geometry and exact bounded spatial projection."""
import csv, math, shutil
from pathlib import Path
import numpy as np
from ModelTrainer.common import read,save,hashes,finish,new_folder,verify_hashes
from TrackModel.geometry import path_rows,resample,quality,FIELDS
from TrackModel.model import load_reference
from DatasetBuilder.builder import csv_write


def fast_project(rows, reference, length, heading, allow_finish_seam=False):
    from .human_imitation import projection_breaks
    xy=np.array([[r['position_x'],r['position_z']] for r in reference]);delta=np.roll(xy,-1,axis=0)-xy
    tangent=np.array([[r['tangent_x'],r['tangent_z']] for r in reference]);ss=np.array([r['s_m'] for r in reference])
    pos=np.array([[float(r['position_x']),float(r['position_z'])] for r in rows]);cells={};groups={};size=20.;radius=15.00001
    # Expanded segment AABBs include every segment whose projection could pass
    # the existing 15m projection bound. Candidate pruning cannot change the winner.
    for i,(a,b) in enumerate(zip(xy,np.roll(xy,-1,axis=0))):
        lo=np.floor((np.minimum(a,b)-radius)/size).astype(int);hi=np.floor((np.maximum(a,b)+radius)/size).astype(int)
        for ix in range(lo[0],hi[0]+1):
            for iz in range(lo[1],hi[1]+1):cells.setdefault((ix,iz),[]).append(i)
    for j,cell in enumerate(np.floor(pos/size).astype(int)):groups.setdefault(tuple(cell),[]).append(j)
    result=np.zeros((len(rows),3))
    for cell,ids in groups.items():
        candidates=np.array(cells.get(cell,[]),dtype=int)
        if not len(candidates):raise ValueError('Off-track spatial cell')
        for start in range(0,len(ids),256):
            ix=np.array(ids[start:start+256]);p=pos[ix];a=xy[candidates];d=delta[candidates]
            u=np.clip(np.sum((p[:,None]-a)*d,axis=2)/np.sum(d*d,axis=1),0,1)
            projected=a+u[:,:,None]*d;dist=np.sum((p[:,None]-projected)**2,axis=2)
            local=dist.argmin(axis=1);idx=candidates[local];f=u[np.arange(len(ix)),local]
            t=tangent[idx]+f[:,None]*(tangent[(idx+1)%len(xy)]-tangent[idx]);t/=np.linalg.norm(t,axis=1)[:,None]
            error=p-projected[np.arange(len(ix)),local]
            result[ix,0]=(ss[idx]+f*np.linalg.norm(delta[idx],axis=1))%length
            result[ix,1]=t[:,0]*error[:,1]-t[:,1]*error[:,0]
            vehicle=heading['sign']*np.array([float(rows[j]['heading_rad']) for j in ix])+heading['offset_rad']
            result[ix,2]=(vehicle-np.arctan2(t[:,1],t[:,0])+np.pi)%(2*np.pi)-np.pi
            if np.any(dist[np.arange(len(ix)),local]>15**2):raise ValueError('Off-track projection')
    if not np.isfinite(result).all():raise ValueError('Nonfinite projection')
    projection_breaks(rows,result,length,allow_finish_seam)
    return result


def bridge_points(reference,length,width=30.):
    ss=np.array([r['s_m'] for r in reference]);xyz=np.array([[r[k] for k in ('position_x','position_y','position_z')] for r in reference])
    def at(s):return np.array([np.interp(s%length,ss,xyz[:,i],period=length) for i in range(3)])
    ends=[length-width,width];span=2*width;values=[]
    for s in ends:
        h=np.interp(s,ss,np.unwrap([r['heading_path_rad'] for r in reference]));k=np.interp(s,ss,[r['curvature_per_m'] for r in reference])
        v=np.array([math.cos(h),(at(s+1)[1]-at(s-1)[1])/2,math.sin(h)])
        accel=np.array([-k*math.sin(h),at(s+1)[1]-2*at(s)[1]+at(s-1)[1],k*math.cos(h)])
        values.append((at(s),v*span,accel*span**2))
    p0,v0,a0=values[0];p1,v1,a1=values[1]
    coeff=np.zeros((6,3));coeff[:3]=[p0,v0,a0/2]
    coeff[3:]=np.linalg.solve([[1,1,1],[3,4,5],[6,12,20]],np.array([p1-(p0+v0+a0/2),v1-(v0+a0),a1-a0]))
    result=xyz.copy();q=(ss+length/2)%length-length/2;selected=abs(q)<=width
    u=(q[selected]+width)/span;result[selected]=sum(u[:,None]**i*coeff[i] for i in range(6))
    displacement=np.linalg.norm((result-xyz)[:,[0,2]],axis=1)
    return result,displacement


def build_reference(old_prepared):
    old_prepared=Path(old_prepared).resolve();pm=read(old_prepared/'manifest.json');verify_hashes(pm['output_sha256'],old_prepared)
    old=load_reference(pm['track_model_path']);rows=old['rows'];length=old['manifest']['total_length_m']
    points,displacements=bridge_points(rows,length);points,step=resample(points,.5);new_rows,new_length=path_rows(points,5)
    cfg=dict(old['manifest']['config'],max_smoothing_displacement_m=2.)
    validation=dict(schema_version=1,input_pass=True,**quality(new_rows,length,step,displacements.tolist(),0.,cfg))
    if not validation['passed']:raise ValueError(validation)
    near=[r for r in new_rows if min(r['s_m'],new_length-r['s_m'])<25]
    if max(abs(r['curvature_per_m']) for r in near)>.01:raise ValueError('Repaired seam curvature too large')
    out=new_folder('track_models','continuous-seam')
    m=dict(old['manifest']);m.update(complete=False,config=cfg,points=len(new_rows),total_length_m=new_length,
        geometry_revision='quintic_position_seam_30m_v1',parent_track=pm['track_model_path'],
        repair=dict(width_m=30.,max_planar_displacement_m=float(max(displacements)),old_length_m=length),
        input_sha256=hashes([old_prepared/'manifest.json',Path(pm['track_model_path'])/'manifest.json',Path(pm['track_model_path'])/'reference.csv',Path(__file__)]))
    csv_write(out/'reference.csv',FIELDS,new_rows);save(out/'validation.json',validation)
    (out/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>Continuous seam repair</title><pre>'+__import__('json').dumps(m['repair'],indent=2)+'</pre>',encoding='utf-8')
    finish(out,m);load_reference(out)
    prepared=new_folder('state_runs','continuous-seam-prepared')
    for name in ('config.json','heading_profile.json','replay.json'):shutil.copyfile(old_prepared/name,prepared/name)
    inventory=dict(pm['input_sha256']);inventory.update(hashes([out/n for n in ('manifest.json','reference.csv','validation.json','report.html')]+[old_prepared/'manifest.json',Path(__file__)]))
    metadata=dict(pm,complete=False,track_model_path=str(out),geometry_revision=m['geometry_revision'],parent_prepared=str(old_prepared),input_sha256=inventory)
    finish(prepared,metadata)
    return prepared,out
