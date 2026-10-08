"""Lifetime lock for one controller window per checkout; never owns driving inputs."""
import ctypes
import hashlib
import os

class SingleInstance:
    def __init__(self, root):
        self.handle = None
        if os.name == 'nt':
            api = ctypes.WinDLL('kernel32', use_last_error=True)
            api.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            api.CreateMutexW.restype = ctypes.c_void_p
            api.CloseHandle.argtypes = [ctypes.c_void_p]
            self.api = api
            name = 'Local\\PolePositionController-' + hashlib.sha256(str(root.resolve()).lower().encode()).hexdigest()[:24]
            handle = api.CreateMutexW(None, 0, name)
            if not handle: raise OSError(ctypes.get_last_error(), 'Controller mutex failed')
            if ctypes.get_last_error() == 183:
                api.CloseHandle(handle)
                raise RuntimeError('학습 컨트롤러가 이미 열려 있습니다. 기존 창을 사용하세요.')
            self.handle = handle
    def close(self):
        if self.handle: self.api.CloseHandle(self.handle); self.handle = None
