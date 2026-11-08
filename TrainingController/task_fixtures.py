"""Temporary command inputs. No executable in this fixture is ever launched."""
from pathlib import Path


def command_inputs(root, task):
    root = Path(root)
    for relative in ('.venv-training/Scripts/python.exe', '.venv-controller/Scripts/python.exe',
                     'target/release/poleposition-actuator.exe', 'target/release/poleposition-recorder.exe',
                     'target/release/poleposition-gamepad.exe', 'config.toml', 'profile.json', 'last.pt'):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    for name in ('dataset', 'model', 'prepared', 'checkpoint', 'previous_run'):
        (root / name).mkdir(exist_ok=True)
    values = dict(seconds='60', epochs='', dataset='dataset', model='model', prepared='prepared',
                  checkpoint='checkpoint' if task in ('강화학습', '게임패드 회차 학습', '고정 정책 연속주행 평가') else 'last.pt',
                  config='config.toml', profile='profile.json', controller_config='config.toml',
                  previous_run='previous_run', mode='0' if task == '게임패드 교사 기록' else
                  'verify5' if task == '강화학습' else 'shadow', model_seconds='5', split='test')
    return values


def command_outcome(command, root, task, values, resume):
    try:
        return {'argv': [value.replace(str(root.resolve()), '<ROOT>').replace(str(root), '<ROOT>')
                         for value in command(root, task, values, resume)]}
    except (ValueError, OSError) as error:
        return {'error': type(error).__name__, 'message': str(error)}
