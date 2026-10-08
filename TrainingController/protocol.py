"""Versioned JSON-lines management channel, separate from actuator commands."""
import json
import math
import threading
import time

VERSION = 1
HEARTBEAT_TIMEOUT = 3.0


def validate_event(event):
    """Validate the fields consumed by Tk before they can break its poll loop."""
    if (not isinstance(event, dict) or type(event.get('version')) is not int
            or event['version'] != VERSION or event.get('event') not in ('state', 'progress', 'finished')):
        raise ValueError('Invalid worker event')
    for name in ('phase', 'state', 'connection', 'reason', 'controller_reason', 'result_dir'):
        if name in event and event[name] is not None and not isinstance(event[name], str):
            raise ValueError(f'Invalid event text: {name}')
    for name in ('episode', 'epoch', 'updates', 'samples', 'total_samples'):
        if name in event and (type(event[name]) is not int or event[name] < 0):
            raise ValueError(f'Invalid event count: {name}')
    for name in ('reward', 'loss', 'elapsed_seconds', 'speed_kmh', 'age_ms'):
        value = event.get(name)
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            raise ValueError(f'Invalid event number: {name}')
    if 'epoch' in event and ('updates' not in event or event.get('loss') is None):
        raise ValueError('Incomplete epoch progress')
    identity = event.get('identity')
    if identity is not None and (not isinstance(identity, dict) or any(
            not isinstance(identity.get(name, ''), str) for name in ('car', 'track'))):
        raise ValueError('Invalid event identity')
    for name in ('fresh', 'tracking_ready', 'foreground', 'read_only'):
        if name in event and type(event[name]) is not bool:
            raise ValueError(f'Invalid event flag: {name}')
    if 'preparation' in event:
        check = event['preparation']
        if not isinstance(check, dict) or any(not isinstance(check.get(name), str) for name in ('check', 'state')):
            raise ValueError('Invalid preparation event')
    if '\x00' in (event.get('result_dir') or ''):
        raise ValueError('Invalid result path')
    if event['event'] == 'finished' and event.get('state') not in ('completed', 'stopped', 'failed', 'needs_attention'):
        raise ValueError('Invalid final state')


class Control:
    def __init__(self, stream, timeout=HEARTBEAT_TIMEOUT):
        self.stream = stream
        self.timeout = timeout
        self.last_heartbeat = time.monotonic()
        self.reason = None
        self.lock = threading.Lock()

    def cancel(self, reason):
        with self.lock:
            if self.reason is None:
                self.reason = reason

    def listen(self):
        try:
            while True:
                line = self.stream.readline(4097)
                if not line:
                    break
                if len(line) > 4096:
                    raise ValueError('Management command too long')
                command = json.loads(line)
                if (not isinstance(command, dict) or set(command) != {'version', 'command'}
                        or type(command['version']) is not int or command['version'] != VERSION):
                    raise ValueError('Unsupported management protocol')
                if command.get('command') == 'heartbeat':
                    self.last_heartbeat = time.monotonic()
                elif command.get('command') == 'stop':
                    self.cancel('user_stop')
                else:
                    raise ValueError('Unknown management command')
        except (ValueError, OSError, AttributeError):
            self.cancel('protocol_error')
        finally:
            self.cancel('connection_closed')

    def stopped(self):
        if time.monotonic() - self.last_heartbeat > self.timeout:
            self.cancel('heartbeat_timeout')
        return self.reason is not None


def message(**fields):
    return json.dumps(dict(version=VERSION, **fields), ensure_ascii=False, allow_nan=False) + '\n'
