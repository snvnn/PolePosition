"""Single-job ownership with nonblocking GUI consumption and cooperative stop."""
import json
import queue
import subprocess
import threading
import time

from .protocol import message, validate_event


class ManagedProcess:
    def __init__(self):
        self.process = None
        self.events = queue.Queue(maxsize=256)
        self.latest = None
        self.terminal = None
        self.last_heartbeat = 0
        self.reader = None
        self.error_reader = None
        self.command_writer = None
        self.heartbeat_pending = threading.Event()
        self.stop_pending = threading.Event()
        self.wake_writer = threading.Event()

    @property
    def active(self):
        return self.process is not None and (self.process.poll() is None or self.reader.is_alive() or self.error_reader.is_alive())

    def start(self, command, cwd):
        if self.process is not None:
            raise RuntimeError('A job is already running')
        self.latest = self.terminal = None
        self.events = queue.Queue(maxsize=256)
        # Readers may request a stop as soon as they see the child's first byte.
        # Reset the previous job's flags before starting any new reader.
        self.heartbeat_pending.clear()
        self.stop_pending.clear()
        self.wake_writer.clear()
        self.process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding='utf-8', bufsize=1,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.error_reader = threading.Thread(target=self._drain_errors, daemon=True)
        self.error_reader.start()
        self.command_writer = threading.Thread(target=self._write, args=(self.process,), daemon=True)
        self.command_writer.start()
        self.send('heartbeat')

    def _drain_errors(self):
        for line in self.process.stderr:
            self.latest = line.rstrip()[-2000:]

    def _read(self):
        try:
            for line in self.process.stdout:
                if not line.strip():
                    continue
                event = json.loads(line)
                validate_event(event)
                if event['event'] == 'finished':
                    if event.get('state') not in ('completed', 'stopped', 'failed', 'needs_attention'):
                        raise ValueError('Invalid final state')
                    if self.terminal is not None:
                        raise ValueError('Duplicate final event')
                    self.terminal = event
                else:
                    try:
                        self.events.put_nowait(event)
                    except queue.Full:
                        pass  # Progress is lossy; final results are never dropped.
        except (ValueError, OSError, AttributeError) as error:
            self.terminal = dict(event='finished', state='failed', reason=str(error))
            self.stop()
            # Keep draining so a cooperative worker cannot block on a full pipe.
            for _ in self.process.stdout:
                pass

    def send(self, command):
        if self.process and self.process.poll() is None:
            (self.stop_pending if command == 'stop' else self.heartbeat_pending).set()
            self.wake_writer.set()

    def _write(self, process):
        # A blocked child stdin must never block Tk. Heartbeats are requested only
        # by Tk; this thread does not renew a frozen GUI's lease itself.
        while process.poll() is None:
            self.wake_writer.wait(.2)
            self.wake_writer.clear()
            command = None
            if self.stop_pending.is_set():
                self.stop_pending.clear()
                command = 'stop'
            elif self.heartbeat_pending.is_set():
                self.heartbeat_pending.clear()
                command = 'heartbeat'
            if command is None:
                continue
            try:
                process.stdin.write(message(command=command))
                process.stdin.flush()
            except (OSError, ValueError):
                return

    def stop(self):
        self.send('stop')

    def poll(self):
        # Called by Tk, not by a background heartbeat: a frozen GUI loses its lease.
        if time.monotonic() - self.last_heartbeat >= .5:
            self.send('heartbeat')
            self.last_heartbeat = time.monotonic()
        result = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                break
        if self.process and not self.active:
            self.wake_writer.set()
            self.command_writer.join(timeout=1)
            terminal = self.terminal or dict(event='finished', state='failed', reason='Worker exited without a result')
            if self.process.returncode != 0:
                terminal = dict(terminal, state='failed', reason=terminal.get('reason') or self.latest or 'Worker failed')
            result.append(terminal)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    # A child may exit between heartbeat write and buffered stdin
                    # close. The process and readers are already terminal here;
                    # a broken pipe during cleanup must not strand GUI ownership.
                    pass
            self.process = None
        return result
