"""Read-only discovery for the controller's recent result list."""
from datetime import datetime
from pathlib import Path

RESULT_TYPES = {'training_runs': '학습', 'evaluation_runs': '평가',
                'recordings': '기록', 'actuator_runs': '주행', 'reinforcement_runs': '강화학습'}


def recent_results(root, additional=(), limit=80):
    candidates = set(Path(path).resolve() for path in additional)
    for parent in RESULT_TYPES:
        try:
            candidates.update(path.resolve() for path in (Path(root) / parent).iterdir())
        except OSError:
            continue
    found = []
    for path in candidates:
        try:
            if path.is_dir():
                found.append((path.stat().st_mtime_ns, str(path), path))
        except OSError:
            continue
    return [item[2] for item in sorted(found, reverse=True)[:limit]]


def result_label(path):
    path = Path(path)
    try:
        date = datetime.fromtimestamp(path.stat().st_mtime).strftime('%m-%d %H:%M')
    except OSError:
        date = '경로 없음'
    return f"{date} · {RESULT_TYPES.get(path.parent.name, '결과')} · {path.name}"


def resume_checkpoint(path):
    """Select only; checkpoint content is validated by the existing worker."""
    path = Path(path)
    if (path/'online.json').is_file() and (path/'replay.pkl').is_file() and (path/'model.zip').is_file():
        return path
    candidate = path / 'last.pt'
    return candidate if candidate.is_file() else None
