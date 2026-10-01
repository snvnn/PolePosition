"""Reuse recorder validation boundaries; never infer a second set of laps."""
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import time
import tomllib
import uuid

from DrivingRecorder.validate_recording import REQUIRED, validate
from . import __version__
from .report import render

SOURCES = ("metadata.json", "telemetry.csv", "events.csv", "summary.json")
EXTRA = ["session_id", "lap_id", "lap_elapsed_ns"]
DEFAULTS = dict(output_dir="datasets", max_gap_ms=100.0, train_ratio=.7,
                validation_ratio=.15, plot_buckets=250)


def load_config(path):
    with Path(path).open("rb") as stream:
        config = tomllib.load(stream)
    return check_config(config)


def check_config(config):
    if set(config) - DEFAULTS.keys():
        raise ValueError("Unknown config keys")
    c = DEFAULTS | config
    for key in ("max_gap_ms", "train_ratio", "validation_ratio"):
        if isinstance(c[key], bool) or not isinstance(c[key], (int, float)) or not math.isfinite(c[key]) or c[key] <= 0:
            raise ValueError(f"Invalid {key}")
    if c["train_ratio"] + c["validation_ratio"] >= 1:
        raise ValueError("Split ratios must leave a test set")
    if type(c["plot_buckets"]) is not int or not 10 <= c["plot_buckets"] <= 2000:
        raise ValueError("plot_buckets must be 10..2000")
    if not isinstance(c["output_dir"], str) or not c["output_dir"]:
        raise ValueError("Invalid output_dir")
    return c


def hashes(folder):
    result = {}
    for name in SOURCES:
        with (folder / name).open("rb") as stream:
            result[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def json_write(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def csv_write(path, fields, rows):
    with path.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def stats(values):
    values = sorted(values)
    if not values:
        return dict(count=0, mean_ms=None, p95_ms=None, p99_ms=None, max_ms=None)
    return dict(count=len(values), mean_ms=sum(values)/len(values)/1e6,
                p95_ms=values[math.ceil(.95*len(values))-1]/1e6,
                p99_ms=values[math.ceil(.99*len(values))-1]/1e6, max_ms=values[-1]/1e6)


def split_laps(laps, config):
    ids = [lap["lap_id"] for lap in laps if lap["accepted"]]
    result = dict(train=[], validation=[], test=[])
    if len(ids) < 3:
        return result
    n_train = min(max(1, int(len(ids)*config["train_ratio"])), len(ids)-2)
    n_val = min(max(1, int(len(ids)*config["validation_ratio"])), len(ids)-n_train-1)
    result.update(train=ids[:n_train], validation=ids[n_train:n_train+n_val], test=ids[n_train+n_val:])
    return result


def quality(rows, start, end):
    times = [int(r["elapsed_ns"]) for r in rows]
    # Include the interval to the end boundary, but not the previous lap's interval.
    gaps = [b-a for a, b in zip(times, times[1:]+[end])]
    q = {"sample_intervals": stats(gaps)}
    for page in ("physics", "graphics"):
        changed = [int(r[f"{page}_changed_ns"]) for r in rows]
        unique = [v for i, v in enumerate(changed) if i == 0 or v != changed[i-1]]
        q[f"{page}_change_intervals"] = stats([b-a for a, b in zip(unique, unique[1:])])
    sectors = []
    previous = int(rows[0]["sector"]) if rows else -1
    for row in rows[1:]:
        sector = int(row["sector"])
        if (previous, sector) == (len(sectors), len(sectors)+1) and len(sectors) < 2:
            sectors.append(int(row["elapsed_ns"]))
        previous = sector
    q["sector_seconds"] = ([ (b-a)/1e9 for a, b in zip([start]+sectors, sectors+[end])]
                           if len(sectors) == 2 else None)
    return q


def build(session, config, *, build_id=None):
    config = check_config(config)
    session = Path(session).resolve()
    parent = Path(config["output_dir"]).resolve()
    if parent == session or session in parent.parents:
        raise ValueError("Output must be outside the source session")
    build_id = build_id or f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    if not re.fullmatch(r"[A-Za-z0-9_-]+", build_id):
        raise ValueError("Unsafe build ID")
    parent.mkdir(parents=True, exist_ok=True)
    output = parent / build_id
    output.mkdir()  # Exclusive: a collision must never alter an existing build.
    manifest = dict(schema_version=1, builder_version=__version__, build_id=build_id,
                    created_unix_ns=time.time_ns(), complete=False, source_path=str(session), config=config)
    json_write(output / "manifest.json", manifest)
    try:
        manifest["source_sha256"] = hashes(session)
        report = validate(session)
        json_write(output / "validation.json", report)
        summary = json.loads((session / "summary.json").read_text(encoding="utf-8"))
        metadata = json.loads((session / "metadata.json").read_text(encoding="utf-8"))
        if not report["format_pass"] or summary["dropped_samples"] or summary["dropped_events"]:
            raise ValueError(f"Source validation rejected session: {report['errors']}; queue drops must be zero")
        sid = metadata["session_id"]
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
            raise ValueError("Unsafe session ID")
        manifest.update(session_id=sid, units=metadata.get("units", {}),
                        fields=REQUIRED+EXTRA, derived_units={"lap_elapsed_ns":"host monotonic nanoseconds"},
                        source_metadata=metadata, quality={}, time_basis="host monotonic, not simulation time")
        with (session / "telemetry.csv").open(newline="", encoding="utf-8") as stream:
            raw_rows = list(csv.DictReader(stream))
        by_segment = {}
        for r in raw_rows:
            by_segment.setdefault(int(r["segment_id"]), []).append(r)
        laps, lap_rows, covered = [], {}, set()
        for index, boundary in enumerate(report.get("laps", []), 1):
            seg, start, end = (boundary[k] for k in ("segment_id", "start_elapsed_ns", "end_elapsed_ns"))
            rows = [r for r in by_segment[seg] if start <= int(r["elapsed_ns"]) < end]
            if not rows or int(rows[0]["elapsed_ns"]) != start:
                raise ValueError("Missing start boundary sample")
            lid = f"{sid}-s{seg}-q{rows[0]['sequence']}"
            q = quality(rows, start, end)
            reasons = []
            if any(r["lap_valid"] != "true" for r in rows): reasons.append("invalid_lap")
            if any(r["in_pit"] == "true" for r in rows): reasons.append("in_pit")
            if q["sample_intervals"]["max_ms"] > config["max_gap_ms"]: reasons.append("sample_gap")
            if (not any(r["lap_valid"] != "true" for r in rows)) != boundary["all_samples_lap_valid"]:
                raise ValueError("Validator and extraction validity disagree")
            lap = dict(lap_id=lid, ordinal=index, segment_id=seg, start_elapsed_ns=start,
                       end_elapsed_ns=end, duration_seconds=(end-start)/1e9, samples=len(rows),
                       accepted=not reasons, reasons=";".join(reasons), split="unassigned",
                       max_gap_ms=q["sample_intervals"]["max_ms"])
            for i in range(3): lap[f"sector_{i+1}_seconds"] = q["sector_seconds"][i] if q["sector_seconds"] else ""
            laps.append(lap)
            lap_rows[lid] = rows
            manifest["quality"][lid] = q
            covered.update(r["sequence"] for r in rows)
        splits = split_laps(laps, config)
        for lap in laps:
            for name, ids in splits.items():
                if lap["lap_id"] in ids: lap["split"] = name
            if not lap["accepted"]: lap["split"] = "excluded"
        excluded = []
        for seg, rows in by_segment.items():
            run = []
            for r in rows + [None]:
                if r is not None and r["sequence"] not in covered:
                    run.append(r)
                elif run:
                    excluded.append(dict(segment_id=seg, first_sequence=run[0]["sequence"],
                                         last_sequence=run[-1]["sequence"], reason="incomplete_lap", samples=len(run)))
                    run = []
        for lap in laps:
            if not lap["accepted"]:
                rows = lap_rows[lap["lap_id"]]
                excluded.append(dict(segment_id=lap["segment_id"], first_sequence=rows[0]["sequence"],
                                     last_sequence=rows[-1]["sequence"], reason=lap["reasons"], samples=len(rows)))
        (output / "laps").mkdir()
        for lap in laps:
            if lap["accepted"]:
                csv_write(output / "laps" / f"{lap['lap_id']}.csv", REQUIRED+EXTRA,
                          (r | dict(session_id=sid, lap_id=lap["lap_id"],
                                    lap_elapsed_ns=int(r["elapsed_ns"])-lap["start_elapsed_ns"])
                           for r in lap_rows[lap["lap_id"]]))
        fields = list(laps[0]) if laps else "lap_id ordinal segment_id start_elapsed_ns end_elapsed_ns duration_seconds samples accepted reasons split max_gap_ms sector_1_seconds sector_2_seconds sector_3_seconds".split()
        csv_write(output / "laps.csv", fields, laps)
        csv_write(output / "excluded_intervals.csv", ["segment_id", "first_sequence", "last_sequence", "reason", "samples"], excluded)
        json_write(output / "splits.json", splits)
        with (output / "report.html").open("x", encoding="utf-8") as stream:
            stream.write(render(laps, lap_rows, manifest, report))
        if hashes(session) != manifest["source_sha256"]:
            raise ValueError("Source changed during build")
        manifest.update(complete=True, split_complete=bool(splits["test"]),
                        complete_laps=len(laps), accepted_laps=sum(l["accepted"] for l in laps),
                        excluded_laps=sum(not l["accepted"] for l in laps),
                        split_counts={k:len(v) for k,v in splits.items()})
    except Exception as exc:
        manifest.update(complete=False, error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        # Only the build's own manifest is replaced. The initial false state survives write failure.
        json_write(output / "manifest.final.json", manifest)
        (output / "manifest.final.json").replace(output / "manifest.json")
    return output
