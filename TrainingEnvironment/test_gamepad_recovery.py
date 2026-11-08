import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ModelTrainer.common import finish,save,digest
from .gamepad_online_io import save as atomic_save
from .gamepad_recovery import relocation,timing_gap
from .gamepad_online_data import collect,SEMANTICS

class RecoveryTests(unittest.TestCase):
    def fixture(self,p):
        rows=[]
        for n in range(1,54):
            t=n*10_000_000
            rows.append(dict(kind='observation',sequence=n,observation_ns=t,
                state=dict(status='ready' if n!=2 else 'warming',reason='suspected_teleport' if n==2 else '',tracking_epoch=1 if n==1 else 2),
                sample=dict(position_x=100. if n==1 else 0.,position_z=0.,speed_kmh=0.,session_type=3,session_index=0,gear_raw=0),
                stop_telemetry=dict(live=True,identity_ok=True,physics_ns=t,graphics_ns=t)))
        rows.append(dict(kind='neutralized',now_ns=530_000_001,report=dict(lx=0,ly=0,rx=0,ry=0,lt=0,rt=0,buttons=0)))
        s=dict(policy_comparison=True,continuous_semantics=SEMANTICS,model_sha256='model',cancelled=False,error=None,logging_failure=False,
            episode=dict(interrupted=False),episode_end=dict(reason='tracking_not_ready',event=None,observation_ns=20_000_000,tracking_epoch=2),
            shutdown=dict(kind='relocated_standstill',standstill_confirmed=True,neutralized=True,released=True,observation_ns=530_000_000,tracking_epoch=2),
            relocation_evidence=dict(verified=True,previous_observation_ns=10_000_000,observation_ns=20_000_000,displacement_m=100.,spawn_distance_m=.1))
        return rows,s
    def store(self,p,rows,s):
        end=s['episode_end']
        if end['event'] is not None:
            end['event']={k:end[k] for k in ('reason','tracking_epoch','observation_ns')}
        end['policy_sha256']=s['model_sha256']
        s['episode']['end']=end['event']
        rows.insert(-1,dict(kind='episode_end',end=end))
        (p/'samples.jsonl').write_text('\n'.join(map(json.dumps,rows)))
        save(p/'summary.json',s,replace=True);finish(p,{})
    def test_timing_gap_excluded_with_strict_stop_and_freshness(self):
        for bad in (None,'epoch','sequence','jump','stale','long','short','comparison','cancel','release','wrong_reason','human'):
            with self.subTest(bad=bad),tempfile.TemporaryDirectory() as d:
                p=Path(d);rows,s=self.fixture(p)
                s.update(policy_comparison=False,episode_start=dict(observation_ns=10_000_000))
                s['episode_end']=dict(reason='observation_timing_gap',event=dict(reason='observation_timing_gap'),observation_ns=40_000_000,tracking_epoch=1)
                s['shutdown'].update(kind='continuous_stop',continuous_stop_confirmed=True,tracking_epoch=1,observation_ns=550_000_000)
                for r in rows:
                    if r['kind']=='neutralized':r['now_ns']+=20_000_000;continue
                    if r['sequence']>=2:r['observation_ns']+=20_000_000
                    r['state'].update(status='ready',reason='',tracking_epoch=1)
                    r['sample'].update(position_x=0.)
                    r['stop_telemetry'].update(physics_ns=r['observation_ns'],graphics_ns=r['observation_ns'])
                if bad=='epoch':rows[0]['state']['tracking_epoch']=2
                if bad=='sequence':rows[0]['sequence']=0
                if bad=='jump':rows[0]['sample']['position_x']=100.
                if bad=='stale':rows[1]['stop_telemetry']['physics_ns']=0
                if bad=='long':rows[0]['observation_ns']=-20_000_000;s['episode_start']['observation_ns']=-20_000_000
                if bad=='short':rows[0]['observation_ns']=20_000_000;s['episode_start']['observation_ns']=20_000_000
                if bad=='comparison':s['policy_comparison']=True
                if bad=='cancel':s['cancelled']=True
                if bad=='release':s['shutdown']['released']=False
                if bad=='wrong_reason':s['episode_end']['reason']='observation_discontinuity'
                if bad=='human':rows.append(dict(kind='policy',mode='Running',authority='human',label_mask=[1,0,0]))
                self.store(p,rows,s)
                if bad is None:
                    result=timing_gap(p,'model');self.assertTrue(result['excluded']);self.assertEqual(result['gap_ms'],30.)
                else:
                    try:self.assertIsNone(timing_gap(p,'model'))
                    except ValueError:pass
    def test_verified_relocation_is_excluded_and_cannot_train(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);rows,s=self.fixture(p);self.store(p,rows,s)
            r=relocation(p,'model',True)
            self.assertTrue(r['excluded']);self.assertFalse(r['eligible'])
            self.assertIsNone(relocation(p,'model',False))
            with self.assertRaisesRegex(ValueError,'evaluation-only'):collect(p,p,p)
    def test_cancel_stale_wrong_evidence_or_unreleased_never_recovers(self):
        for edit in ('cancel','stale','jump','unreleased','reason','epoch'):
            with self.subTest(edit=edit),tempfile.TemporaryDirectory() as d:
                p=Path(d);rows,s=self.fixture(p)
                if edit=='cancel':s['cancelled']=True
                if edit=='stale':rows[1]['stop_telemetry']['physics_ns']=-100_000_000
                if edit=='jump':rows[0]['sample']['position_x']=0.
                if edit=='unreleased':s['shutdown']['released']=False
                if edit=='reason':s['episode_end']['reason']='telemetry_expired'
                if edit=='epoch':rows[0]['state']['tracking_epoch']=2
                self.store(p,rows,s)
                try:self.assertIsNone(relocation(p,'model',True))
                except ValueError:pass
    def test_json_is_invisible_until_complete_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'request.json';value={'config':'drive','bindings':{'file':'hash'}}
            import os
            actual=os.fsync
            def fsync(fd):
                self.assertFalse(p.exists())
                temp=next(Path(d).glob('*.tmp'))
                self.assertEqual(json.loads(temp.read_text()),value)
                actual(fd)
            with patch('TrainingEnvironment.gamepad_online_io.os.fsync',fsync):atomic_save(p,value)
            self.assertEqual(json.loads(p.read_text()),value)
            with self.assertRaises(FileExistsError):atomic_save(p,{'bad':True})
            self.assertEqual(json.loads(p.read_text()),value)
            bad=Path(d)/'bad.json'
            with patch('TrainingEnvironment.gamepad_online_io.os.fsync',side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):atomic_save(bad,value)
            self.assertFalse(bad.exists());self.assertFalse(list(Path(d).glob('*.tmp')))

if __name__=='__main__':unittest.main()
