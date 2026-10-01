"""Byte-preserving ACC settings snapshots. No game settings are edited implicitly."""
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
import subprocess


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_json(path, value):
    with Path(path).open("x", encoding="utf-8", newline="\n") as out:
        json.dump(value, out, ensure_ascii=False, indent=2, allow_nan=False)
        out.write("\n")


def unique(parent, prefix):
    path = Path(parent) / f"{prefix}-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path.resolve()


def settings_files(root):
    root = Path(root).resolve()
    files = [root / "Config/controls.json", root / "Config/assists.json"]
    for folder in (root / "Customs/Controls", root / "Config/Controls"):
        if folder.is_dir():
            files.extend(p for p in folder.rglob("*") if p.is_file())
    for path in files:
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Missing or unsafe settings file: {path}")
    return sorted(set(files))


def backup(root, parent="input_profiles"):
    root = Path(root).resolve()
    files = settings_files(root)
    output = unique(parent, "backup")
    entries = []
    for src in files:
        data = src.read_bytes()
        relative = src.relative_to(root)
        dest = output / "files" / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("xb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        if dest.read_bytes() != data or src.read_bytes() != data:
            raise ValueError(f"Settings changed during backup: {src}")
        entries.append({"relative": relative.as_posix(), "bytes": len(data), "sha256": digest(dest)})
    save_json(output / "manifest.json", {"schema_version": 1, "kind": "acc_settings_backup",
        "complete": True, "created_unix_ns": time.time_ns(), "root": str(root), "files": entries})
    return output


def verify_backup(folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("kind") != "acc_settings_backup" or manifest.get("schema_version") != 1 or manifest.get("complete") is not True:
        raise ValueError("Incomplete/unsupported backup")
    seen = set()
    for entry in manifest["files"]:
        rel = Path(entry["relative"])
        src = (folder / "files" / rel).resolve()
        if rel.is_absolute() or not src.is_relative_to(folder / "files") or str(rel) in seen:
            raise ValueError("Unsafe/duplicate backup path")
        seen.add(str(rel))
        if digest(src) != entry["sha256"] or src.stat().st_size != entry["bytes"]:
            raise ValueError("Backup hash mismatch")
    if not {"Config/controls.json", "Config/assists.json"}.issubset({x["relative"] for x in manifest["files"]}):
        raise ValueError("Missing required backup entries")
    return manifest


def restore(folder, *, game_closed=False, destination=None, parent="input_profiles"):
    if not game_closed:
        raise ValueError("Close ACC first and use --game-closed")
    if destination is None and os.name == "nt":
        tasklist = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/tasklist.exe"
        processes = subprocess.run([str(tasklist), "/FI", "IMAGENAME eq AC2-Win64-Shipping.exe", "/FO", "CSV", "/NH"],
                                   check=True, capture_output=True).stdout.lower()
        if b"ac2-win64-shipping.exe" in processes:
            raise ValueError("ACC is still running; close it before restoration")
    manifest = verify_backup(folder)
    root = Path(destination or manifest["root"]).resolve()
    previous = backup(root, parent)
    for entry in manifest["files"]:
        target = (root / entry["relative"]).resolve()
        if not target.is_relative_to(root):
            raise ValueError("Unsafe restore destination")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + f".poleposition-{uuid.uuid4().hex}.tmp")
        with temp.open("xb") as out:
            out.write((Path(folder) / "files" / entry["relative"]).read_bytes())
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, target)
        if digest(target) != entry["sha256"]:
            raise ValueError("Restored settings hash mismatch")
    save_json(previous / "restore.json", {"restored_from": str(Path(folder).resolve()),
        "destination": str(root), "verified": True, "created_unix_ns": time.time_ns()})
    return previous
