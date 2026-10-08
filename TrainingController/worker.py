"""Offline managed worker. stdout contains only protocol events."""
import argparse
import contextlib
import sys
import threading
import time

from .protocol import Control, message


def terminal_state(complete, reason):
    if complete:
        return 'completed'
    return 'stopped' if reason in ('user_stop', 'time_limit') else 'failed'


def main():
    # Windows console defaults can be CP949; the management protocol is UTF-8.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', choices=['train', 'evaluate', 'prepare-v2', 'pretrain-v2', 'online', 'gamepad-probe', 'gamepad-train'], default='train')
    parser.add_argument('--model')
    for name in ('prepared','profile','controller-config','previous-run','stage'):
        parser.add_argument('--'+name)
    parser.add_argument('--split', choices=['validation', 'test'], default='test')
    parser.add_argument('--dataset')
    parser.add_argument('--resume')
    parser.add_argument('--config', default='ModelTrainer/config.toml')
    parser.add_argument('--seconds', type=float, default=7200)
    parser.add_argument('--epochs', type=int)
    args = parser.parse_args()
    if (args.task in ('train', 'pretrain-v2') and bool(args.dataset) == bool(args.resume)) or not 0 < args.seconds <= 7200:
        parser.error('Specify dataset or resume and a time limit in (0, 7200]')
    if args.task == 'evaluate' and (not args.model or not args.dataset or args.resume):
        parser.error('Evaluation requires model and dataset, without resume')
    if args.epochs is not None and (args.task not in ('train', 'pretrain-v2') or args.resume or not 1 <= args.epochs <= 200):
        parser.error('Epoch cap requires new training and must be in 1..200')
    output = sys.stdout
    latest_result = None
    result = None

    def emit(event, **fields):
        nonlocal latest_result
        if fields.get('result_dir'):
            latest_result = fields['result_dir']
        if event == 'progress':
            fields['elapsed_seconds'] = time.monotonic() - started
        output.write(message(event=event, **fields))
        output.flush()

    control = Control(sys.stdin)
    threading.Thread(target=control.listen, daemon=True).start()
    started = time.monotonic()

    def stopped():
        if args.task=='gamepad-train' and sys.platform=='win32':
            import ctypes
            if ctypes.windll.user32.GetAsyncKeyState(0x77) & 0x8000:
                control.cancel('user_stop')
        if time.monotonic() - started >= args.seconds:
            control.cancel('time_limit')
        return control.stopped()

    try:
        if args.task == 'gamepad-probe':
            import subprocess, json
            from pathlib import Path
            exe=Path(__file__).resolve().parents[1]/'target/release/poleposition-gamepad.exe'
            data=json.loads(subprocess.check_output([str(exe),'--probe'],text=True,encoding='utf-8',timeout=10))
            emit('finished',state='completed',reason='게임패드 연결: '+json.dumps(data['slots'],ensure_ascii=False))
            return 0
        emit('state', state='evaluating' if args.task == 'evaluate' else 'training')
        with contextlib.redirect_stdout(sys.stderr):
            from ModelTrainer.training import train
            from ModelTrainer.common import read
            if args.task == 'online':
                from TrainingEnvironment.online import run
                result, complete, reason = run(args, control, emit)
                metrics = read(result/'training-summary.json') if (result/'training-summary.json').is_file() else {}
                emit('finished',state='completed' if complete else 'stopped',reason=reason,result_dir=str(result),updates=metrics.get('updates',0))
                return 0
            if args.task == 'gamepad-train':
                from TrainingEnvironment.gamepad import train as gamepad_train
                result=gamepad_train(args.config,should_stop=stopped,on_progress=lambda data:emit('progress',**data))
            elif args.task == 'train':
                result = train(args.dataset, args.config, args.resume, epoch_limit=args.epochs, should_stop=stopped,
                               on_progress=lambda data: emit('progress', **data))
            elif args.task == 'pretrain-v2':
                from TrainingEnvironment.pretrain import train as pretrain
                result = pretrain(args.dataset, args.config, args.resume, epoch_limit=args.epochs,
                                  should_stop=stopped, on_progress=lambda data: emit('progress', **data))
            elif args.task == 'prepare-v2':
                from TrainingEnvironment.teacher import prepare_actions
                from TrainingEnvironment.preparation import prepare_features, PreparationStopped
                try:
                    actions = prepare_actions(args.config, should_stop=stopped,
                                              on_progress=lambda data: emit('progress', **data))
                    result = prepare_features(actions, should_stop=stopped,
                                              on_progress=lambda data: emit('progress', **data))
                except PreparationStopped:
                    emit('finished', state=terminal_state(False, control.reason), reason=control.reason,
                         result_dir=latest_result)
                    return 0 if control.reason in ('user_stop', 'time_limit') else 1
            else:
                from Evaluator.evaluate import replay
                result = replay(args.model, args.dataset, args.split, should_stop=stopped,
                                on_progress=lambda data: emit('progress', **data))
            manifest = read(result / 'manifest.json')
        status = terminal_state(manifest['complete'], control.reason)
        reason = control.reason or manifest.get('error')
        if reason is None and (result / 'summary.json').is_file():
            reason = read(result / 'summary.json').get('termination')
        emit('finished', state=status, reason=reason, result_dir=str(result))
        return 0 if status in ('completed', 'stopped') else 1
    except Exception as error:
        if isinstance(error,InterruptedError) and control.reason in ('user_stop','time_limit'):
            emit('finished',state='stopped',reason=control.reason,result_dir=latest_result)
            return 0
        emit('finished', state='failed', reason=f'{type(error).__name__}: {error}',
             result_dir=str(result) if result is not None else latest_result)
        return 1


if __name__ == '__main__':
    sys.exit(main())
