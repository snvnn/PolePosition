"""Read-only validation of original files; writes a new, separate report."""
import argparse
import csv
import json
import math
from pathlib import Path
import time

REQUIRED = "sequence segment_id received_unix_ns elapsed_ns interval_ns physics_changed_ns graphics_changed_ns queue_drops physics_packet_id graphics_packet_id speed_kmh rpm gear_raw throttle brake steering_raw position_x position_y position_z heading_rad completed_laps lap_time_ms progress sector lap_valid session_type session_index in_pit fuel_litres clock_seconds rain_intensity".split()
BOOLS = {"lap_valid", "in_pit"}


def validate(folder):
    folder = Path(folder)
    errors, warnings = [], []
    report = {"schema_version": 1, "session_path": str(folder.resolve()), "errors": errors, "warnings": warnings}
    try:
        metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
        summary = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
        if metadata.get("schema_version") != 1 or summary.get("schema_version") != 1:
            errors.append("Unsupported schema version")
        if metadata.get("session_id") != summary.get("session_id"):
            errors.append("Session IDs disagree")
        if summary.get("normal_shutdown") is not True:
            errors.append("Incomplete or failed shutdown")
        with (folder / "events.csv").open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != ["elapsed_ns", "segment_id", "kind", "detail"]:
                raise ValueError("Invalid events header")
            events = list(reader)
        segments = {}
        event_laps = []
        previous_event_time = -1
        for event in events:
            t, segment = int(event["elapsed_ns"]), int(event["segment_id"])
            if t < previous_event_time or t < 0:
                errors.append("Events have invalid time ordering")
            previous_event_time = t
            if event["kind"] == "segment_start":
                if segment in segments:
                    errors.append("Duplicate segment start")
                segments[segment] = t
            if event["kind"] == "lap_boundary":
                event_laps.append((segment, t))
        previous = None
        count, gaps, boundaries = 0, 0, []
        seen_segments = set()
        max_gap_ns = 0
        timer_reset = None
        last_boundary = None
        lap_ok = True
        boundary_validity = []
        with (folder / "telemetry.csv").open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != REQUIRED:
                raise ValueError("Missing, extra, or reordered telemetry columns")
            for raw in reader:
                count += 1
                if None in raw or any(v is None for v in raw.values()):
                    raise ValueError(f"Malformed row {count}")
                r = {}
                for key in REQUIRED:
                    if key in BOOLS:
                        if raw[key] not in ("true", "false"):
                            raise ValueError(f"Invalid boolean at row {count}")
                        r[key] = raw[key] == "true"
                    elif key in {"speed_kmh", "throttle", "brake", "steering_raw", "position_x", "position_y", "position_z", "heading_rad", "progress", "fuel_litres", "clock_seconds"}:
                        r[key] = float(raw[key])
                        if not math.isfinite(r[key]):
                            raise ValueError(f"Non-finite {key} at row {count}")
                    else:
                        r[key] = int(raw[key])
                seg, elapsed = r["segment_id"], r["elapsed_ns"]
                seen_segments.add(seg)
                if seg not in segments or segments[seg] > elapsed:
                    errors.append(f"Missing segment event at row {count}")
                if any(not 0 <= r[k] <= 1 for k in ("progress", "throttle", "brake")):
                    errors.append(f"Range error at row {count}")
                if any(r[k] < 0 for k in ("elapsed_ns", "interval_ns", "completed_laps", "lap_time_ms", "queue_drops")):
                    errors.append(f"Negative time/count at row {count}")
                if any(not 0 <= r[k] <= elapsed for k in ("physics_changed_ns", "graphics_changed_ns")):
                    errors.append(f"Invalid freshness timestamp at row {count}")
                if previous is None or seg != previous["segment_id"]:
                    timer_reset = None
                    last_boundary = None
                    lap_ok = True
                if previous is None:
                    gaps += r["sequence"] - 1
                else:
                    step = r["sequence"] - previous["sequence"]
                    if step <= 0 or elapsed <= previous["elapsed_ns"]:
                        errors.append(f"Sequence or monotonic time reversed at row {count}")
                    gaps += max(0, step - 1)
                    if r["received_unix_ns"] < previous["received_unix_ns"]:
                        warnings.append("Host UTC clock moved backwards; use monotonic time")
                    if seg < previous["segment_id"]:
                        errors.append("Segment ID reversed")
                    if step == 1 and r["interval_ns"] != elapsed - previous["elapsed_ns"]:
                        errors.append(f"Incorrect sample interval at row {count}")
                    if seg == previous["segment_id"]:
                        gap = elapsed - previous["elapsed_ns"]
                        max_gap_ns = max(max_gap_ns, gap)
                        if gap > int(metadata["config"]["stale_seconds"] * 1e9):
                            errors.append(f"Unexplained data gap at row {count}")
                        if (r["physics_packet_id"], r["graphics_packet_id"]) == (previous["physics_packet_id"], previous["graphics_packet_id"]):
                            errors.append(f"Duplicate packet pair at row {count}")
                        delta = r["completed_laps"] - previous["completed_laps"]
                        crossed = delta == 1 and (previous["progress"] > .8 or r["progress"] < .2)
                        if r["lap_time_ms"] + 2000 < previous["lap_time_ms"] and delta == 0:
                            if previous["progress"] > .8 or r["progress"] < .2:
                                timer_reset = elapsed
                        if timer_reset is not None:
                            if previous["progress"] > .8 and r["progress"] < .2 and elapsed - timer_reset < 1_000_000_000:
                                crossed = True
                                timer_reset = None
                            elif delta != 0 or elapsed - timer_reset >= 1_000_000_000:
                                timer_reset = None
                        if crossed and (last_boundary is None or elapsed - last_boundary >= 5_000_000_000):
                            boundaries.append((seg, elapsed, r["completed_laps"]))
                            boundary_validity.append(lap_ok)
                            lap_ok = True
                            last_boundary = elapsed
                        if delta < 0 or delta > 1 or (delta == 1 and not (previous["progress"] > .8 or r["progress"] < .2)):
                            errors.append(f"Unmarked lap discontinuity at row {count}")
                lap_ok = lap_ok and r["lap_valid"]
                previous = r
        received = summary["received_samples"]
        dropped = summary["dropped_samples"]
        if previous:
            gaps += received - previous["sequence"]
        else:
            gaps = received
        if count != summary["written_samples"] or received != count + dropped or gaps != dropped:
            errors.append("Sample counts / sequence gaps / drops disagree")
        if len(events) != summary["written_events"]:
            errors.append("Event counts disagree")
        if summary["dropped_events"] or dropped:
            warnings.append("Recording queue dropped data; exclude affected sections from training")
        if [(s, t) for s, t, _ in boundaries] != event_laps:
            errors.append("Lap boundary events do not match recorded rows")
        if seen_segments != set(segments):
            warnings.append("Segment starts and recorded segments differ")
        complete, valid_complete = 0, 0
        laps = []
        for index, (a, b) in enumerate(zip(boundaries, boundaries[1:]), 1):
            if a[0] == b[0]:
                complete += 1
                valid_complete += int(boundary_validity[index])
                laps.append({"segment_id":a[0], "start_elapsed_ns":a[1], "end_elapsed_ns":b[1], "duration_seconds":(b[1]-a[1])/1e9, "all_samples_lap_valid":boundary_validity[index]})
        if not count:
            warnings.append("No live samples recorded")
        if not complete:
            warnings.append("No complete lap between two observed finish-line crossings")
        elif not valid_complete:
            warnings.append("Complete lap recorded, but lap_valid was false within it; not a clean training lap")
        report.update(samples=count, complete_laps=complete, valid_complete_laps=valid_complete,
                      laps=laps, connection_states=[e["detail"] for e in events if e["kind"]=="connection_state"],
                      boundary_count=len(boundaries), max_sample_gap_ms=max_gap_ns / 1e6,
                      read_timing=summary["read_timing"], poll_timing=summary["poll_timing"],
                      first_goal_recording_pass=bool(count and complete and not errors and not dropped and not summary["dropped_events"]))
    except (OSError, ValueError, KeyError, TypeError, csv.Error) as exc:
        errors.append(str(exc))
    report["format_pass"] = not errors
    report.setdefault("first_goal_recording_pass", False)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = validate(args.session)
    output = args.output or args.session / f"validation-{time.time_ns()}.json"
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Report: {output}")
    return 0 if report["format_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
