"""Native supervisor ownership, request publication and cooperative release."""
import threading
from pathlib import Path
from ExecutionRuntime.process import ManagedProcess
from ExecutionRuntime.timing import SystemClock, TimeBudget
from ModelTrainer.common import hashes
from .gamepad_online_io import read, save

ROOT = Path(__file__).resolve().parents[1]

class Native:
    def __init__(self,session,control,emit,seconds, *, clock=None, process_factory=None,
                 game_factory=None, release_after_exit=None):
        from .game_process import GameProcess
        from .game_process import release_after_game_exit
        self.clock = clock if clock is not None else SystemClock()
        self.release_after_exit = release_after_exit or release_after_game_exit
        self.game_process=(game_factory or GameProcess)()
        self.session=session;self.control=control;self.emit=emit;self.seconds=seconds
        self.started=self.clock.monotonic();self.exit_reason=None;self.closing=False
        self.budget=TimeBudget(seconds,clock=self.clock,started=self.started)
        self.job=(process_factory or ManagedProcess)()
        try:self.job.start([str(ROOT/'target/release/poleposition-gamepad.exe'),'--managed','--supervise',str(session)],ROOT)
        except BaseException:self.game_process.close();raise
        self.thread=threading.Thread(target=self.pump,daemon=True);self.thread.start()

    def pump(self):
        next_game_check=0;game_exited=False
        while self.job.process is not None:
            if not game_exited and self.clock.monotonic()>=next_game_check:
                next_game_check=self.clock.monotonic()+1
                game_exited=self.release_after_exit(self.game_process,self.job,self.control,self.session)
            self.budget.cancel_if_expired(self.control,'session_time_limit')
            if self.control.stopped():self.job.stop()
            for e in self.job.poll():
                if e['event']=='finished':
                    self.exit_reason=e.get('reason','native_exit')
                    if not self.closing:self.control.cancel(self.exit_reason)
                else:self.emit('progress',**{k:v for k,v in e.items() if k not in ('event','version')})
            self.clock.sleep(.02)

    def check(self):
        if self.control.stopped():raise InterruptedError(self.control.reason or 'stopped')
        if self.exit_reason:raise RuntimeError('Native supervisor exited: '+self.exit_reason)

    def reserve_reset_window(self, seconds):
        """Keep a verified stopped car idle if a full reset cannot fit this phase."""
        self.check()
        if self.budget.seconds-self.budget.elapsed() >= seconds:
            return
        self.emit('progress',phase='waiting_for_phase_deadline')
        while True:
            self.budget.cancel_if_expired(self.control,'session_time_limit')
            self.check()
            self.clock.sleep(.02)

    def drive(self,number,config,bindings,automatic_start=None):
        self.check()
        request=dict(config=str(config),bindings=bindings)
        if automatic_start:
            receipt=Path(automatic_start).resolve()
            request['automatic_start']=str(receipt)
            request['bindings'].update(hashes([receipt]))
        save(self.session/f'request-{number}.json',request)
        response=self.session/f'response-{number}.json'
        while not response.exists():self.check();self.clock.sleep(.02)
        value=read(response);self.check()
        if value.get('error'):raise RuntimeError(value['error'])
        # The response can arrive before the supervisor's F8 completion event.
        # Do not collect a cancelled waiting run (the GUI may already clean it).
        if value.get('reason') in ('user_stop','f8'):
            self.control.cancel('f8');self.check()
        return Path(value['run'])

    def close(self):
        self.closing=True
        if self.control.stopped():self.job.stop()
        else:save(self.session/'done.json',{},replace=True)
        # Do not kill a process while it still owns a moving car's device.
        while self.thread.is_alive():self.thread.join(.1)
        self.game_process.close()
