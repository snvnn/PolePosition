"""Read-only discovery for the controller's recent result list."""
from datetime import datetime
from pathlib import Path
import shutil

RESULT_TYPES = {'training_runs': '학습', 'evaluation_runs': '평가',
                'recordings': '기록', 'actuator_runs': '주행', 'reinforcement_runs': '강화학습'}


def delete_result(root, path):
    root = Path(root).resolve()
    path = Path(path)
    target = path.resolve(strict=True)
    # Only a direct result folder inside this workspace may be removed.
    if (not target.is_dir() or path.is_symlink()
            or target.parent.parent != root or target.parent.name not in RESULT_TYPES):
        raise ValueError('프로젝트의 개별 결과 폴더만 삭제할 수 있습니다.')
    shutil.rmtree(target)


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
    if (path/'latest.json').is_file():return path
    if (path/'checkpoint.pt').is_file():
        from ModelTrainer.common import read
        if read(path/'manifest.json').get('continuous_semantics')=='gamepad_continuous_iql_v1':return path
    if (path/'online.json').is_file() and (path/'replay.pkl').is_file() and (path/'model.zip').is_file():
        return path
    candidate = path / 'last.pt'
    return candidate if candidate.is_file() else None
