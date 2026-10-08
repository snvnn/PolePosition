import tempfile
import unittest
from pathlib import Path
from .jobs import command
from .single_instance import SingleInstance
from .protocol import validate_event

class GamepadGuiTests(unittest.TestCase):
    def test_jobs_use_new_backend_and_do_not_run_wheel(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);(r/'config').write_text('');(r/'profile').write_text('')
            v=dict(seconds='120',config='config',profile='profile',mode='0')
            self.assertIn('poleposition-gamepad.exe',' '.join(command(r,'게임패드 모델 시험',v)))
            self.assertIn('poleposition-gamepad.exe',' '.join(command(r,'게임패드 교정 주행',v)))
            self.assertIn('gamepad-train',command(r,'게임패드 교정 학습',v))
            for task in ('실제 패드 응답 검사','가상 패드 응답 검사'):
                self.assertIn('--response',command(r,task,v))
            self.assertIn('--gamepad-profile',command(r,'게임패드 교사 기록',v))
            v['mode']='4'
            with self.assertRaises(ValueError):command(r,'게임패드 교사 기록',v)
    def test_progress_accepted(self):
        validate_event(dict(version=1,event='progress',phase='training',epoch=1,updates=2,loss=.1,validation_mse=[.1,.2,.3]))
    @unittest.skipUnless(__import__('os').name=='nt','Windows mutex')
    def test_single_window_lock_released(self):
        with tempfile.TemporaryDirectory() as d:
            first=SingleInstance(Path(d))
            try:
                with self.assertRaises(RuntimeError):SingleInstance(Path(d))
            finally:first.close()
            second=SingleInstance(Path(d));second.close()

if __name__=='__main__':unittest.main()
