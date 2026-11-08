"""Task-specific argv builders. They validate paths but never launch processes."""


def champion_cycle(root, task, values, resume, seconds, path):
    return [str(root / '.venv-training/Scripts/python.exe'), '-u', '-m', 'TrainingController.worker', '--task', 'gamepad-champion-cycle', '--config', path('config'), '--seconds', str(seconds)]


def comparison(root, task, values, resume, seconds, path):
    return [str(root / '.venv-training/Scripts/python.exe'), '-u', '-m', 'TrainingController.worker', '--task', 'gamepad-policy-compare', '--config', path('config'), '--seconds', str(seconds)]


def gamepad_online(root, task, values, resume, seconds, path):
    args = [str(root / '.venv-training/Scripts/python.exe'), '-u', '-m', 'TrainingController.worker', '--task', 'gamepad-online' if task == '게임패드 회차 학습' else 'gamepad-online-evaluate', '--config', path('config'), '--seconds', str(seconds)]
    if resume:
        args += ['--resume', path('checkpoint', True)]
    elif task == '고정 정책 연속주행 평가':
        args += ['--model', path('model', True)]
    return args


def response(root, task, values, resume, seconds, path):
    return [str(root / 'target/release/poleposition-gamepad.exe'), '--managed', '--response', '--config', path('config')]


def probe(root, task, values, resume, seconds, path):
    return [str(root / '.venv-training/Scripts/python.exe'), '-u', '-m', 'TrainingController.worker', '--task', 'gamepad-probe', '--seconds', str(seconds)]


def gamepad_train(root, task, values, resume, seconds, path):
    return [str(root / '.venv-training/Scripts/python.exe'), '-u', '-m', 'TrainingController.worker', '--task', 'gamepad-train', '--config', path('config'), '--seconds', str(seconds)]


def teacher(root, task, values, resume, seconds, path):
    slot = values.get('mode', '')
    if slot not in ('0', '1', '2', '3'):
        raise ValueError('연결 확인에서 표시된 게임패드 번호 0~3을 선택하세요.')
    return [str(root / 'target/release/poleposition-recorder.exe'), '--managed', '--config', path('config'), '--duration', str(seconds), '--gamepad-profile', path('profile'), '--gamepad-slot', slot]


def gamepad_drive(root, task, values, resume, seconds, path):
    return [str(root / 'target/release/poleposition-gamepad.exe'), '--managed', '--config', path('config')]


def online(root, task, values, resume, seconds, path):
    stage = values.get('mode', 'verify5')
    if stage not in ('verify5', 'verify10', 'verify30', 'train', 'evaluate', 'evaluate_initial'):
        raise ValueError('5초 → 10초 → 30초 검증 후 train을 선택하세요.')
    python = root / '.venv-controller/Scripts/python.exe'
    if not python.is_file() or not (root / 'target/release/poleposition-actuator.exe').is_file():
        raise ValueError('강화학습 환경과 주행 실행 파일을 확인하세요.')
    args = [str(python), '-u', '-m', 'TrainingController.worker', '--task', 'online', '--config', path('config'), '--seconds', str(seconds), '--stage', stage, '--prepared', path('prepared', True), '--profile', path('profile'), '--controller-config', path('controller_config')]
    resume = resume or stage in ('evaluate', 'evaluate_initial')
    args += ['--resume', path('checkpoint', True)] if resume else ['--model', path('model', True)]
    if not resume and stage != 'verify5':
        args += ['--previous-run', path('previous_run', True)]
    return args


def offline(root, task, values, resume, seconds, path):
    venv = '.venv-controller' if task.startswith('강화학습') else '.venv-training'
    python = root / venv / 'Scripts/python.exe'
    if not python.is_file():
        raise ValueError('기존 학습 환경을 찾을 수 없습니다.')
    args = [str(python), '-u', '-m', 'TrainingController.worker', '--seconds', str(seconds)]
    if task == '강화학습 자료 준비':
        args += ['--task', 'prepare-v2', '--config', path('config')]
    elif task in ('모방학습', '강화학습 사전학습'):
        if task == '강화학습 사전학습':
            args += ['--task', 'pretrain-v2']
        args += ['--config', path('config')]
        args += ['--resume', path('checkpoint')] if resume else ['--dataset', path('dataset', True)]
        epochs = values.get('epochs', '').strip()
        if not resume and epochs:
            if not epochs.isascii() or not epochs.isdecimal() or (not 1 <= int(epochs) <= 200):
                raise ValueError('새 학습 epoch 상한은 1~200의 정수 또는 빈칸이어야 합니다.')
            args += ['--epochs', str(int(epochs))]
    else:
        if values.get('split') not in ('validation', 'test'):
            raise ValueError('평가 집합은 validation 또는 test여야 합니다.')
        args += ['--task', 'evaluate', '--dataset', path('dataset', True), '--model', path('model', True), '--split', values['split']]
    return args


def record(root, task, values, resume, seconds, path):
    exe = root / 'target/release/poleposition-recorder.exe'
    args = [str(exe), '--managed', '--config', path('config'), '--duration', str(seconds)]
    if not exe.is_file():
        raise ValueError('Rust 실행 파일을 먼저 빌드해야 합니다.')
    return args


def demo(root, task, values, resume, seconds, path):
    exe = root / 'target/release/poleposition-actuator.exe'
    args = [str(exe), 'demo', '--managed', '--prepared', path('prepared', True), '--profile', path('profile'), '--controller-config', path('controller_config'), '--config', path('config'), '--managed-seconds', str(seconds)]
    if not exe.is_file():
        raise ValueError('Rust 실행 파일을 먼저 빌드해야 합니다.')
    return args


def shadow(root, task, values, resume, seconds, path):
    exe = root / 'target/release/poleposition-actuator.exe'
    args = [str(exe), 'v2-shadow', '--managed', '--prepared', path('prepared', True), '--profile', path('profile'), '--model', path('model', True), '--controller-config', path('controller_config'), '--config', path('config'), '--managed-seconds', str(seconds)]
    if not exe.is_file():
        raise ValueError('Rust 실행 파일을 먼저 빌드해야 합니다.')
    return args


def policy(root, task, values, resume, seconds, path):
    exe = root / 'target/release/poleposition-actuator.exe'
    if values.get('mode') not in ('shadow', 'drive'):
        raise ValueError('주행 모드를 선택하세요.')
    args = [str(exe), 'policy', '--managed', '--prepared', path('prepared', True), '--profile', path('profile'), '--model', path('model', True), '--mode', values['mode'], '--controller-config', path('controller_config'), '--config', path('config')]
    duration = values.get('model_seconds', '5')
    if duration not in ('5', '10', '15'):
        raise ValueError('기존 모델 주행 단계는 5·10·15초입니다.')
    args += ['--model-seconds', duration, '--managed-seconds', str(seconds)]
    if values['mode'] == 'drive':
        args += ['--previous-run', path('previous_run', True)]
    if not exe.is_file():
        raise ValueError('Rust 실행 파일을 먼저 빌드해야 합니다.')
    return args
