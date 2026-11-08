"""Reusable temporary fixtures; this module contains no TestCase classes."""
import copy
import json
from pathlib import Path
from ModelTrainer.common import save, finish, digest
from TrainingEnvironment.gamepad_online_data import SEMANTICS

def observation(n,epoch=1,valid=True):
    return dict(kind='observation',observation_ns=n*10_000_000,sequence=n,features=[0.]*35,
        state=dict(status='ready',tracking_epoch=epoch,s_m=1000.+n/100,position_x=n/100,position_z=0.,lateral_error_m=0.,heading_error_rad=0.),
        sample=dict(lap_valid=valid,speed_kmh=0.,session_index=1,position_x=n/100,position_z=0.))


def summary(epoch=1):
    return dict(continuous_semantics=SEMANTICS,episode=dict(interrupted=False,lap_events=[],valid_laps=0),
        episode_end=dict(reason='invalid_lap',observation_ns=200_000_000,tracking_epoch=1,event=dict(reason='invalid_lap')),
        shutdown=dict(standstill_confirmed=True,neutralized=True,released=True,observation_ns=800_000_000,
            tracking_epoch=epoch,kind='continuous_stop' if epoch==1 else 'relocated_standstill'))


REPORT=dict(lx=0,ly=0,rx=0,ry=0,rt=0,lt=0,buttons=0)


class OnlineDataFixture:
    def stop_rows(self,epoch=1):
        return [observation(n,epoch) for n in range(20,81)]+[dict(kind='neutralized',now_ns=800_000_001,report=dict(REPORT))]

    def raw_stop_rows(self):
        rows=self.stop_rows()
        for o in rows:
            if o['kind']!='observation':continue
            o['state'].update(status='invalid',reason='outside_projection_range')
            o['stop_telemetry']=dict(live=True,identity_ok=True,
                physics_ns=o['observation_ns'],graphics_ns=o['observation_ns'])
        return rows

    def fixture(self,d):
        root=Path(d);run=root/'run';model=root/'model';dataset=root/'dataset'
        for p in (run,model,dataset):p.mkdir()
        save(model/'model.json',{});save(model/'speed-profile.json',dict(length_m=3881.75))
        save(dataset/'config.json',dict(quarantined_runs={},start_line_radius_m=150,history_clearance_ns=620000000,
            reward=dict(progress=1,time=-.1,lateral_squared=-.2,heading_squared=-.1,applied_action_change_squared=-.01,invalid_lap=-10,valid_full_lap=10)))
        rows=[]
        for n in range(1,81):
            o=observation(n,valid=n<20);rows.append(o);t=o['observation_ns']
            if n%2==0 and n<20:
                rows.append(dict(kind='policy',mode='Running',observation_ns=t,now_ns=t+1,features=[0.]*35,
                    action=[0.,0.,0.],command_action=[0.,0.,0.],selected_action=[0.,0.,0.],authority='model',label_mask=[0,0,0]))
            rows.append(dict(kind='sent',mode='Running' if n<20 else 'Stopping',send_start_ns=t+2,send_end_ns=t+3,
                axes=dict(steering=0.,throttle=0.,brake=0.),report=dict(REPORT),xinput=[1,dict(REPORT)],authority='model'))
        rows.append(dict(kind='neutralized',now_ns=800_000_004,report=dict(REPORT)))
        s=summary();s['model_sha256']=digest(model/'model.json');s['logging_failure']=False
        s['episode_start']=dict(observation_ns=10_000_000,tracking_epoch=1)
        e=s['episode_end'];e['event']={k:e[k] for k in ('reason','observation_ns','tracking_epoch')}
        s['episode']['end']=dict(e['event']);e['policy_sha256']=s['model_sha256']
        rows.insert(next(i for i,r in enumerate(rows) if r['kind']=='sent' and r['mode']=='Stopping'),
                    dict(kind='episode_end',end=copy.deepcopy(e)))
        return run,model,dataset,rows,s

    def store(self,run,rows,s):
        (run/'samples.jsonl').write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf-8')
        save(run/'summary.json',s,replace=True);finish(run,{})
