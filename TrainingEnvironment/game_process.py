"""Hold ACC process handles: signalled handles prove exit, unlike stale telemetry."""
import ctypes
import sys
from ctypes import wintypes as W


class GameProcess:
    def __init__(self):
        self.handles=[];self.pids=[]
        if sys.platform!='win32':return
        class Entry(ctypes.Structure):
            _fields_=[('dwSize',W.DWORD),('cntUsage',W.DWORD),('th32ProcessID',W.DWORD),
                ('th32DefaultHeapID',ctypes.c_size_t),('th32ModuleID',W.DWORD),('cntThreads',W.DWORD),
                ('th32ParentProcessID',W.DWORD),('pcPriClassBase',W.LONG),('dwFlags',W.DWORD),('szExeFile',W.WCHAR*260)]
        self.k=ctypes.WinDLL('kernel32',use_last_error=True)
        for name,args,result in [
            ('CreateToolhelp32Snapshot',[W.DWORD,W.DWORD],W.HANDLE),
            ('Process32FirstW',[W.HANDLE,ctypes.POINTER(Entry)],W.BOOL),
            ('Process32NextW',[W.HANDLE,ctypes.POINTER(Entry)],W.BOOL),
            ('OpenProcess',[W.DWORD,W.BOOL,W.DWORD],W.HANDLE),
            ('WaitForSingleObject',[W.HANDLE,W.DWORD],W.DWORD),
            ('CloseHandle',[W.HANDLE],W.BOOL)]:
            f=getattr(self.k,name);f.argtypes=args;f.restype=result
        snapshot=self.k.CreateToolhelp32Snapshot(2,0)
        if snapshot==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry=Entry();entry.dwSize=ctypes.sizeof(entry)
            ok=self.k.Process32FirstW(snapshot,ctypes.byref(entry))
            while ok:
                if entry.szExeFile.casefold()=='ac2-win64-shipping.exe':
                    h=self.k.OpenProcess(0x100000,False,entry.th32ProcessID)
                    if not h:raise ctypes.WinError(ctypes.get_last_error())
                    self.handles.append(h);self.pids.append(entry.th32ProcessID)
                ok=self.k.Process32NextW(snapshot,ctypes.byref(entry))
        except BaseException:
            self.close();raise
        finally:self.k.CloseHandle(snapshot)
        if not self.handles:raise ValueError('ACC must be running before starting a live phase')

    def exited(self):
        # WAIT_OBJECT_0 proves process termination. Failure/timeout never does.
        return bool(self.handles) and all(self.k.WaitForSingleObject(h,0)==0 for h in self.handles)

    def close(self):
        for h in self.handles:self.k.CloseHandle(h)
        self.handles=[]


def release_after_game_exit(game,job,control,session):
    """Never force release solely because observations stopped or a timer expired."""
    if not game.exited():return False
    from .gamepad_online_io import save
    import time
    control.cancel('game_process_exited')
    if hasattr(control,'parent'):control.parent.cancel('game_process_exited')
    save(session/'game-process-exit.json',dict(reason='game_process_exited',game_pids=game.pids,
        at_unix_ns=time.time_ns(),stop_confirmed=False,training_eligible=False),replace=True)
    if job.process is not None and job.process.poll() is None:job.process.terminate()
    return True
