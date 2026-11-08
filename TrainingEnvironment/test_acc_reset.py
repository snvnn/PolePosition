import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from ModelTrainer.common import digest
from .acc_reset import AutoReset, MenuNotReady, WindowsPort, start_matches, anchor_score


PROFILE=dict(schema='acc_hotlap_auto_reset_v1',assets={},car='bmw_m4_gt3',track='brands_hatch',
    position=[1.,2.,3.],heading=0.,position_tolerance_m=1.,heading_tolerance_rad=.1,
    stopped_kmh=1.,timeout_seconds=3,actions=dict(restart=[1,2],confirm=[3,4],drive=[5,6]))


class Port:
    def __init__(self):
        self.memory=self;self.phase='driving';self.actions=[];self.packet=0;self.now=0.;self.frozen=False
        self.wrong=False;self.cancel_after=None
    def check(self):
        if self.cancel_after is not None and len(self.actions)>=self.cancel_after:raise InterruptedError('f8')
    def bounds(self):self.check()
    def read(self):
        if not self.frozen:self.packet+=1
        return dict(car='bmw_m4_gt3',track='brands_hatch',position=[99.,2.,3.] if self.wrong else [1.,2.,3.],
            heading=0.,speed_kmh=0.,status=2,session_type=3,lap_valid=True,gear_raw=1,
            physics_packet=self.packet,graphics_packet=self.packet,observed_at=self.now)
    def screen(self,name):return self.phase==name
    def key(self,k):self.actions.append('escape');self.phase='pause'
    def click(self,xy,screen):
        assert self.phase==screen
        self.actions.append(screen);self.phase={'pause':'confirm','confirm':'garage','garage':'driving'}[screen]
    def sleep(self,n):self.now+=n


class ResetTests(unittest.TestCase):
    def test_windows_preclick_mismatch_sends_no_input(self):
        port=WindowsPort.__new__(WindowsPort)
        port.screen=lambda name:False
        # No user32 handle is supplied: any input access would fail the test.
        with self.assertRaises(MenuNotReady):port.click([1,2],'confirm')

    def test_preinput_menu_flicker_reobserves_without_duplicate_input(self):
        with tempfile.TemporaryDirectory() as folder:
            port=Port();click=port.click;attempts=[]
            def flicker(xy,screen):
                attempts.append(screen)
                if screen=='confirm' and attempts.count('confirm')==1:
                    raise MenuNotReady('transition frame')
                click(xy,screen)
            port.click=flicker
            receipt=self.run_reset(port,folder)
            self.assertEqual(port.actions,['escape','pause','confirm','garage'])
            events=json.loads(receipt.read_text())['events']
            self.assertEqual(sum(e['action']=='wait_before_input' for e in events),1)

    def test_preinput_mismatch_cannot_extend_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            port=Port();click=port.click
            def mismatch(xy,screen):
                if screen=='confirm':raise MenuNotReady('unrecognized')
                click(xy,screen)
            port.click=mismatch
            with self.assertRaisesRegex(RuntimeError,'timeout'):self.run_reset(port,folder)
            self.assertEqual(port.actions,['escape','pause'])
            self.assertLess(port.now,PROFILE['timeout_seconds']+.1)
            self.assertFalse((Path(folder)/'reset/ready.json').exists())

    def test_input_or_focus_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            port=Port();click=port.click;attempts=[]
            def covered(xy,screen):
                attempts.append(screen)
                if screen=='confirm':raise RuntimeError('target covered')
                click(xy,screen)
            port.click=covered
            with self.assertRaisesRegex(RuntimeError,'target covered'):self.run_reset(port,folder)
            self.assertEqual(attempts,['pause','confirm'])
            self.assertEqual(port.actions,['escape','pause'])

    def test_f8_interrupts_preinput_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            port=Port();click=port.click
            def mismatch(xy,screen):
                if screen=='confirm':
                    port.cancel_after=len(port.actions)
                    raise MenuNotReady('transition')
                click(xy,screen)
            port.click=mismatch
            with self.assertRaises(InterruptedError):self.run_reset(port,folder)
            self.assertEqual(port.actions,['escape','pause'])

    def run_reset(self,port,folder,require_garage=False):
        asset=Path(folder)/'fixture';asset.write_text('test')
        profile=dict(PROFILE,assets={str(asset):digest(asset)})
        path=Path(folder)/'profile.json';path.write_text(json.dumps(profile),encoding='utf-8')
        with patch('TrainingEnvironment.acc_reset.time.monotonic',lambda:port.now),patch('TrainingEnvironment.acc_reset.time.sleep',port.sleep):
            return AutoReset(path,port.check,lambda **kw:None,port).reset(Path(folder)/'reset',require_garage=require_garage)

    def test_recovery_requires_garage_before_any_input(self):
        with tempfile.TemporaryDirectory() as folder:
            port=Port()
            with self.assertRaisesRegex(RuntimeError,'requires the garage'):self.run_reset(port,folder,True)
            self.assertEqual(port.actions,[])

    def test_observed_flow_publishes_receipt_after_fresh_stationary_state(self):
        with tempfile.TemporaryDirectory() as folder:
            port=Port();receipt=self.run_reset(port,folder)
            self.assertEqual(port.actions,['escape','pause','confirm','garage'])
            self.assertTrue(json.loads(receipt.read_text())['complete'])

    def test_already_relocated_garage_uses_drive_then_verifies_spawn(self):
        with tempfile.TemporaryDirectory() as folder:
            port=Port();port.phase='garage'
            receipt=self.run_reset(port,folder)
            self.assertEqual(port.actions,['garage'])
            self.assertTrue(json.loads(receipt.read_text())['complete'])

    def test_blue_highlight_requires_matching_glyph_geometry(self):
        normal=Image.new('RGB',(20,20),'black')
        normal.paste('white',(2,3,8,17))
        current=Image.new('RGB',normal.size,'red')
        current.paste((180,210,250),(2,3,8,17))
        self.assertLess(anchor_score(current,normal),.8)
        self.assertGreaterEqual(anchor_score(current,normal,blue_highlight=True),.8)
        self.assertLess(anchor_score(current.transpose(Image.Transpose.FLIP_LEFT_RIGHT),normal,blue_highlight=True),.8)

    def test_garage_fade_waits_without_input_and_unknown_screen_times_out(self):
        for fades in (True,False):
            with self.subTest(fades=fades),tempfile.TemporaryDirectory() as folder:
                port=Port();port.phase='fade'
                def sleep(n):
                    port.now+=n
                    if fades and port.now>=.8:port.phase='garage' if port.phase=='fade' else port.phase
                port.sleep=sleep
                if fades:
                    self.run_reset(port,folder);self.assertEqual(port.actions,['garage'])
                else:
                    with self.assertRaisesRegex(RuntimeError,'timeout'):self.run_reset(port,folder)
                    self.assertEqual(port.actions,[])

    def test_cancellation_never_sends_next_menu_input(self):
        for count in range(5):
            with self.subTest(count=count),tempfile.TemporaryDirectory() as folder:
                port=Port();port.cancel_after=count
                with self.assertRaises(InterruptedError):self.run_reset(port,folder)
                self.assertEqual(len(port.actions),count)
                self.assertFalse((Path(folder)/'reset/ready.json').exists())

    def test_stale_before_reset_sends_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            port=Port();port.frozen=True
            with self.assertRaisesRegex(RuntimeError,'timeout'):self.run_reset(port,folder)
            self.assertEqual(port.actions,[])

    def test_transient_unreadable_page_or_roll_requires_new_stationary_dwell(self):
        for initial in ('missing','rolling'):
            with self.subTest(initial=initial),tempfile.TemporaryDirectory() as folder:
                port=Port();read=port.read
                def sample():
                    value=read()
                    if port.now<.32:return None if initial=='missing' else dict(value,speed_kmh=3.)
                    return value
                port.read=sample
                times=[]
                key=port.key
                def record_key(v):times.append(port.now);key(v)
                port.key=record_key
                self.run_reset(port,folder)
                self.assertGreaterEqual(times[0],.32+.5)

    def test_moving_or_wrong_session_never_gets_a_menu_command(self):
        for field,value in [('speed_kmh',5.),('car','other'),('status',3)]:
            with self.subTest(field=field),tempfile.TemporaryDirectory() as folder:
                port=Port();read=port.read;port.read=lambda:dict(read(),**{field:value})
                with self.assertRaises(RuntimeError):self.run_reset(port,folder)
                self.assertEqual(port.actions,[])
                failure=json.loads((Path(folder)/'reset/failure.json').read_text())
                self.assertEqual(failure['last_stationary_observation'][field],value)

    def test_wrong_spawn_does_not_publish_start_permission(self):
        with tempfile.TemporaryDirectory() as folder:
            port=Port();port.wrong=True
            with self.assertRaisesRegex(RuntimeError,'timeout'):self.run_reset(port,folder)
            self.assertFalse((Path(folder)/'reset/ready.json').exists())

    def test_start_conditions_reject_invalid_lap_wrong_session_speed_heading(self):
        state=Port().read();self.assertTrue(start_matches(state,PROFILE))
        for key,value in [('lap_valid',False),('car','other'),('session_type',0),('speed_kmh',5),
                          ('heading',float('nan')),('heading',1),('gear_raw',0)]:
            self.assertFalse(start_matches(dict(state,**{key:value}),PROFILE))

    def test_anchor_requires_matching_text_not_only_brightness(self):
        black=Image.new('RGB',(10,10),'black');white=Image.new('RGB',(10,10),'white')
        self.assertEqual(anchor_score(white,white),1.)
        self.assertEqual(anchor_score(black,white),0.)
        self.assertEqual(anchor_score(black,black),0.)


if __name__=='__main__':unittest.main()
