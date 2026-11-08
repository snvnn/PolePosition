"""Compatibility command API backed by the shared task catalog."""
from dataclasses import dataclass
from pathlib import Path
from .task_catalog import BY_LABEL, TASKS, TASK_GROUPS, TaskSpec


@dataclass(frozen=True)
class JobRequest:
    argv: tuple[str, ...]
    spec: TaskSpec


def request(root, task, values, resume=False):
    return JobRequest(tuple(command(root, task, values, resume)), BY_LABEL[task])


def command(root, task, values, resume=False):
    root = Path(root)
    seconds = float(values['seconds'])
    spec = BY_LABEL.get(task)
    maximum = spec.max_seconds if spec else 7200
    if not (0 < seconds <= maximum or (spec and spec.allow_zero and seconds == 0)):
        raise ValueError('시간 제한은 0초 초과 7200초 이하여야 합니다.')
    if resume and not (spec and spec.resumable):
        raise ValueError('현재 체크포인트 재개는 모방학습에 연결되어 있습니다.')

    def path(name, directory=False):
        value = values.get(name, '').strip()
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        if not value or not (candidate.is_dir() if directory else candidate.is_file()):
            raise ValueError(f'{name}: 유효한 경로를 선택하세요.')
        return str(candidate.resolve())
    if spec is None:
        raise ValueError('강화학습 실행부가 연결되지 않았습니다. 남은 연결 과제는 개발 기록에 있습니다.')
    return spec.build(root, values, resume, seconds, path)
