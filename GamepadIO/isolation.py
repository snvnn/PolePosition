"""Read-only HidHide verification; never configures a driver during driving."""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from ModelTrainer.common import read,digest


def command_lines(text):
    return [line.strip() for line in text.splitlines() if line.strip()]


def validate_configuration(text, config):
    lines=command_lines(text)
    if '--cloak-on' not in lines or '--inv-off' not in lines:
        raise ValueError('HidHide must be active with inverse mode OFF')
    apps={x.casefold() for x in re.findall(r'--app-reg "([^"\r\n]+)"',text)}
    hidden={x.casefold() for x in re.findall(r'--dev-hide "([^"\r\n]+)"',text)}
    if str(Path(config['controller']).resolve()).casefold() not in apps:
        raise ValueError('Only the controller needs physical-pad access; executable is not allowed')
    forbidden=[config['visibility_probe'],*config['game_executables']]
    if any(str(Path(p).resolve()).casefold() in apps for p in forbidden):
        raise ValueError('ACC or the independent visibility probe can bypass hiding')
    if not config['hidden_device_paths'] or not all(x.casefold() in hidden for x in config['hidden_device_paths']):
        raise ValueError('Selected physical controller is not hidden')


def call(args, timeout=1):
    # HidHideCLI treats inherited redirected stdin as script input and waits for EOF.
    # Never inherit the GUI/Rust management pipe into this read-only subprocess.
    return subprocess.check_output(args,stdin=subprocess.DEVNULL,text=True,encoding='utf-8-sig',errors='strict',timeout=timeout,
                                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))


def verify(config_path, virtual_slot=None, preflight=False):
    c=read(config_path)
    if c.get('schema')!='gamepad_intervention_isolation_v1':raise ValueError('Wrong isolation profile')
    for key in ('cli','controller','visibility_probe','driver'):
        if digest(c[key])!=c[key+'_sha256']:raise ValueError(f'Changed isolation component: {key}')
    if c['controller_sha256']!=c['visibility_probe_sha256']:raise ValueError('Probe must be an identical non-allowlisted copy')
    text=call([c['cli'],'--cloak-state','--inv-state','--app-list','--dev-list'],timeout=3 if preflight else 1)
    validate_configuration(text,c)
    visible=json.loads(call([c['visibility_probe'],'--probe']))
    expected=[] if virtual_slot is None else [virtual_slot]
    if sorted(r['slot'] for r in visible['slots'])!=expected:
        raise ValueError('Unprivileged application can see a physical pad, or virtual output is missing')
    if preflight:
        # Existing ACC handles can predate cloaking. Require a fresh game launch.
        script="Get-Process | Where-Object { $_.ProcessName -in @('acc','AC2-Win64-Shipping') } | ForEach-Object { [DateTimeOffset]::new($_.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds() }"
        powershell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        times=call([str(powershell),'-NoProfile','-Command',script],timeout=6)
        if any(int(t.strip())*1_000_000 < c['configured_at_unix_ns'] for t in times.splitlines() if t.strip()):
            raise ValueError('Restart ACC after HidHide configuration; old device handles are not safe')
    return dict(passed=True,visible_slots=expected,hidden_device_paths=c['hidden_device_paths'])


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--virtual-slot',type=int);p.add_argument('--preflight',action='store_true');a=p.parse_args()
    try:
        print(json.dumps(verify(a.config,a.virtual_slot,a.preflight)))
    except Exception as error:
        detail=dict(at_unix_ns=time.time_ns(),error_type=type(error).__name__,message=str(error),
                    preflight=a.preflight,virtual_slot=a.virtual_slot,
                    returncode=getattr(error,'returncode',None))
        evidence=Path(a.config).parent/'isolation-last-error.json'
        try:
            evidence.write_text(json.dumps(detail,ensure_ascii=False,indent=2),encoding='utf-8')
        except OSError:
            pass
        if isinstance(error,subprocess.TimeoutExpired):
            message=f"Input isolation check timed out after {error.timeout}s. Close HidHide Configuration Client and retry preparation."
        else:
            message=f"{type(error).__name__}: {error}"
        print(message,file=sys.stderr)
        raise SystemExit(1)
