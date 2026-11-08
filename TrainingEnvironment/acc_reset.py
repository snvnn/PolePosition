"""Observed ACC hotlap restart flow; no policy input during menu/reset steps.

This adapter is used by the requested unattended-training feature. Screen anchors
are registered from this installation, and every transition has a bounded wait.
"""
import ctypes as C
from ctypes import wintypes as W
import json
import math
from pathlib import Path
import struct
import time

import numpy as np
from PIL import Image, ImageGrab

from ModelTrainer.common import digest, verify_hashes
from .gamepad_online_io import save


class MenuNotReady(RuntimeError):
    """The expected menu disappeared before any mouse input was sent."""


def start_matches(value, profile):
    try:
        numbers = [*value['position'], value['heading'], value['speed_kmh']]
        return (all(math.isfinite(x) for x in numbers)
            and value['car'] == profile['car'] and value['track'] == profile['track']
            and value['session_type'] == 3 and value['status'] == 2
            and value['lap_valid'] and value['gear_raw'] in (1,2)
            and 0 <= value['speed_kmh'] <= profile['stopped_kmh']
            and math.dist(value['position'], profile['position']) <= profile['position_tolerance_m']
            and abs(math.atan2(math.sin(value['heading']-profile['heading']),
                              math.cos(value['heading']-profile['heading']))) <= profile['heading_tolerance_rad'])
    except (KeyError, TypeError, ValueError):
        return False


def anchor_score(current, template, dark=False, blue_highlight=False):
    # Compare text silhouettes, ignoring the changing scene behind a menu.
    a, b = np.asarray(current.convert('RGB')), np.asarray(template.convert('RGB'))
    if a.shape != b.shape:
        return 0.
    mask = lambda x: (x.max(2) < 80) if dark else (x.min(2) > 210)
    aa, bb = mask(a), mask(b)
    union = np.logical_or(aa, bb).sum()
    score=float(np.logical_and(aa, bb).sum()/union) if union else 0.
    if not blue_highlight or dark or score >= .80:
        return score
    # The selected DRIVE label keeps its glyphs but gains a blue tint. The
    # blue channel separates text from both the red tab and dark background.
    def highlighted_text(rgb):
        return (rgb.min(2)>160) & (rgb[:,:,2]>210)
    aa,bb=highlighted_text(a),highlighted_text(b)
    union=np.logical_or(aa,bb).sum()
    return max(score,float(np.logical_and(aa,bb).sum()/union)) if union else score


class SharedMemory:
    """Open existing ACC pages read-only; never create absent named mappings."""
    def __init__(self):
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.kernel.OpenFileMappingW.argtypes = [W.DWORD, W.BOOL, W.LPCWSTR]
        self.kernel.OpenFileMappingW.restype = W.HANDLE
        self.kernel.MapViewOfFile.argtypes = [W.HANDLE,W.DWORD,W.DWORD,W.DWORD,C.c_size_t]
        self.kernel.MapViewOfFile.restype = C.c_void_p
        self.kernel.UnmapViewOfFile.argtypes = [C.c_void_p]
        self.kernel.CloseHandle.argtypes = [W.HANDLE]

    def page(self, name, size):
        h = self.kernel.OpenFileMappingW(4, False, 'Local\\acpmf_'+name)
        if not h:
            return None
        ptr = None
        try:
            ptr = self.kernel.MapViewOfFile(h,4,0,0,size)
            if not ptr:
                return None
            for _ in range(3):
                first, second = C.string_at(ptr,size), C.string_at(ptr,size)
                if first == second:
                    return first
            return None
        finally:
            if ptr: self.kernel.UnmapViewOfFile(ptr)
            self.kernel.CloseHandle(h)

    def read(self):
        p,g,s = self.page('physics',212),self.page('graphics',1588),self.page('static',200)
        if any(v is None for v in (p,g,s)): return None
        i = lambda b,o: struct.unpack_from('<i',b,o)[0]
        f = lambda b,o: struct.unpack_from('<f',b,o)[0]
        wide = lambda b: b.decode('utf-16-le').split('\0')[0]
        count = i(g,252)
        if not 1 <= count <= 60: return None
        slots = [n for n in range(count) if i(g,976+n*4)==i(g,1216)]
        if len(slots)!=1: return None
        n=slots[0]
        return dict(car=wide(s[68:134]),track=wide(s[134:200]),status=i(g,4),
            session_type=i(g,8),session_index=i(g,1320),lap_valid=i(g,1408)==1,
            physics_packet=i(p,0),graphics_packet=i(g,0),gear_raw=i(p,16),
            speed_kmh=f(p,28),heading=f(p,208),position=[f(g,256+n*12+j*4) for j in range(3)],
            observed_at=time.monotonic())


class WindowsPort:
    def __init__(self, profile, check):
        self.profile,self.check=profile,check
        self.user=C.WinDLL('user32',use_last_error=True)
        self.kernel=C.WinDLL('kernel32',use_last_error=True)
        self.user.GetForegroundWindow.restype=W.HWND
        self.user.GetWindowThreadProcessId.argtypes=[W.HWND,C.POINTER(W.DWORD)]
        self.user.GetClientRect.argtypes=[W.HWND,C.POINTER(W.RECT)]
        self.user.ClientToScreen.argtypes=[W.HWND,C.POINTER(W.POINT)]
        self.user.WindowFromPoint.argtypes=[W.POINT];self.user.WindowFromPoint.restype=W.HWND
        self.kernel.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];self.kernel.OpenProcess.restype=W.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes=[W.HANDLE,W.DWORD,W.LPWSTR,C.POINTER(W.DWORD)]
        self.kernel.CloseHandle.argtypes=[W.HANDLE]
        self.user.SetProcessDPIAware()
        self.memory=SharedMemory()

    def bounds(self):
        self.check()
        hwnd=self.user.GetForegroundWindow();pid=W.DWORD()
        self.user.GetWindowThreadProcessId(hwnd,C.byref(pid))
        h=self.kernel.OpenProcess(0x1000,False,pid.value)
        if not h: raise RuntimeError('Cannot identify foreground process')
        try:
            buf=C.create_unicode_buffer(32768);size=W.DWORD(len(buf))
            if not self.kernel.QueryFullProcessImageNameW(h,0,buf,C.byref(size)):
                raise RuntimeError('Foreground process query failed')
            if Path(buf.value)!=Path(self.profile['executable']):
                raise RuntimeError('ACC foreground lost during automatic reset')
        finally: self.kernel.CloseHandle(h)
        rect=W.RECT();point=W.POINT(0,0)
        if not self.user.GetClientRect(hwnd,C.byref(rect)) or not self.user.ClientToScreen(hwnd,C.byref(point)):
            raise RuntimeError('Cannot locate ACC client area')
        if [rect.right,rect.bottom]!=self.profile['viewport']:
            raise RuntimeError('ACC resolution differs from registered reset profile')
        return hwnd,(point.x,point.y,point.x+rect.right,point.y+rect.bottom)

    def screen(self, name):
        _,box=self.bounds()
        frame=ImageGrab.grab(bbox=box)
        anchors=self.profile['screens'][name]
        for a in anchors:
            with Image.open(a['image']) as reference:
                if anchor_score(frame.crop(a['box']),reference,a.get('dark',False),
                                name=='garage' and a.get('blue_highlight',False)) < .80:
                    return False
        return True

    def click(self, xy, screen):
        if not self.screen(screen): raise MenuNotReady('Reset menu changed before click: '+screen)
        hwnd,box=self.bounds();x,y=box[0]+xy[0],box[1]+xy[1]
        if self.user.WindowFromPoint(W.POINT(x,y))!=hwnd:
            raise RuntimeError('Reset target is covered by another window')
        self.user.SetCursorPos(x,y)
        self.bounds()
        self.user.mouse_event(2,0,0,0,0)
        try: time.sleep(.04)
        finally: self.user.mouse_event(4,0,0,0,0)

    def key(self, vk):
        self.bounds()
        self.user.keybd_event(vk,0,0,0)
        try: time.sleep(.06)
        finally: self.user.keybd_event(vk,0,2,0)


class AutoReset:
    def __init__(self, profile_path, check, emit, port=None):
        self.path=Path(profile_path);self.profile=json.loads(self.path.read_text(encoding='utf-8'))
        if self.profile.get('schema')!='acc_hotlap_auto_reset_v1': raise ValueError('Unknown reset profile')
        verify_hashes(self.profile['assets'])
        self.check,self.emit=check,emit
        self.port=port or WindowsPort(self.profile,check)

    def wait(self, predicate, deadline):
        while True:
            self.check()
            if time.monotonic()>=deadline: raise RuntimeError('Automatic reset timeout')
            if predicate(): return
            time.sleep(.08)

    def click_when_stable(self, name, action, deadline, events):
        # Never retry a possibly delivered input. Only the pre-input menu
        # mismatch is retryable, within the original reset/F8 deadline.
        stable_since = None
        def ready():
            nonlocal stable_since
            if not self.port.screen(name):
                stable_since = None
                return False
            now = time.monotonic()
            if stable_since is None:
                stable_since = now
            return now - stable_since >= .2
        while True:
            self.wait(ready, deadline)
            self.check()
            try:
                self.port.click(self.profile['actions'][action], name)
            except MenuNotReady:
                events.append(dict(screen=name, action='wait_before_input', at=time.time_ns()))
                stable_since = None
                continue
            events.append(dict(screen=name, action=action, at=time.time_ns()))
            return

    def reset(self, output, require_garage=False):
        output=Path(output);output.mkdir()
        deadline=time.monotonic()+self.profile['timeout_seconds']
        events=[]
        last_stationary_observation=None
        try:
            self.emit(phase='auto_reset')
            stationary_since=None
            packets=None
            changed=[0.,0.]
            unready_since=time.monotonic()
            def reset_screen_ready():
                nonlocal stationary_since,packets,unready_since,last_stationary_observation
                self.port.bounds()
                v=self.port.memory.read()
                now=time.monotonic()
                last_stationary_observation=v
                if v is not None:
                    if (v['car'],v['track'],v['session_type'])!=(self.profile['car'],self.profile['track'],3):
                        raise RuntimeError('Automatic reset only supports the registered offline hotlap')
                    if v['status']!=2:
                        raise RuntimeError('ACC left live hotlap before reset')
                # Reading both shared-memory pages can briefly return None. Wait
                # without input and require a fresh stationary dwell from scratch.
                if v is None or not 0<=v['speed_kmh']<=1:
                    stationary_since=packets=None
                    if now-unready_since>=5:
                        raise RuntimeError('Live stationary telemetry timeout before reset')
                    return False
                ids=(v['physics_packet'],v['graphics_packet'])
                for i in range(2):
                    if packets is None or ids[i]!=packets[i]:
                        if packets is not None and now-changed[i]>.2:
                            stationary_since=None
                        changed[i]=now
                packets=ids
                if now-min(changed)>.2:
                    stationary_since=None
                    if now-unready_since>=5:
                        raise RuntimeError('Fresh stationary telemetry timeout before reset')
                    return False
                if stationary_since is None:stationary_since=now
                if now-stationary_since<.5:return False
                unready_since=now
                return self.port.screen('garage') or self.port.screen('driving')
            self.wait(reset_screen_ready,deadline)
            if require_garage and not self.port.screen('garage'):
                raise RuntimeError('Verified relocation recovery requires the garage screen')
            if self.port.screen('garage'):
                # ACC hotlap invalidation can already relocate to its garage.
                # The native stop/release is verified before calling this adapter.
                transitions=[('garage','drive')]
            else:
                if not self.port.screen('driving'):raise RuntimeError('Driving HUD or garage not visible before reset')
                self.check();self.port.key(0x1b)
                transitions=[('pause','restart'),('confirm','confirm'),('garage','drive')]
            for name, action in transitions:
                self.click_when_stable(name, action, deadline, events)
            # A reset proves a new live stream by observing both packet counters advance.
            initial=None;settled=None;current=None
            packet_ids=None;packet_times=[time.monotonic(),time.monotonic()]
            def ready():
                nonlocal initial,settled,current,packet_ids
                self.port.bounds();current=self.port.memory.read()
                if not current or current['status']!=2: return False
                ids=(current['physics_packet'],current['graphics_packet'])
                for j in range(2):
                    if packet_ids is None or ids[j]!=packet_ids[j]:packet_times[j]=time.monotonic()
                packet_ids=ids
                if time.monotonic()-min(packet_times)>.2:
                    settled=None;return False
                if initial is None:
                    initial=current;return False
                if current['physics_packet']==initial['physics_packet'] or current['graphics_packet']==initial['graphics_packet']:
                    return False
                if not start_matches(current,self.profile) or not self.port.screen('driving'):
                    settled=None;return False
                if settled is None:settled=current
                return (current['observed_at']-settled['observed_at']>=.5
                    and current['physics_packet']!=settled['physics_packet']
                    and current['graphics_packet']!=settled['graphics_packet'])
            self.wait(ready,deadline)
            self.check()
            result=dict(schema='acc_auto_start_v1',complete=True,created_unix_ns=time.time_ns(),
                position=current['position'],heading=current['heading'],car=current['car'],track=current['track'],
                position_tolerance_m=self.profile['position_tolerance_m'],
                heading_tolerance_rad=self.profile['heading_tolerance_rad'],
                profile_sha256=digest(self.path),observation=current,events=events)
            save(output/'ready.json',result)
            self.emit(phase='auto_reset_ready')
            return output/'ready.json'
        except BaseException as e:
            save(output/'failure.json',dict(error=str(e),events=events,
                                           last_stationary_observation=last_stationary_observation))
            raise
