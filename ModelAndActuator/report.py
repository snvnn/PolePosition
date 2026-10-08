"""Independent HTML/SVG diagnostics; successful writes are not proof of ACC response."""
import csv
import html
import json
import math
from pathlib import Path
from .setup import digest, save_json


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def stats(values):
    values = sorted(values)
    if not values:
        return {"count": 0, "mean_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None}
    return {"count": len(values), "mean_ms": sum(values) / len(values) / 1e6,
        "p95_ms": values[math.ceil(len(values) * .95) - 1] / 1e6,
        "p99_ms": values[math.ceil(len(values) * .99) - 1] / 1e6, "max_ms": values[-1] / 1e6}


def read_rows(path, required):
    with Path(path).open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if not set(required).issubset(reader.fieldnames or []):
            raise ValueError(f"Missing columns in {path}")
        rows = list(reader)
    last = -1
    for row in rows:
        now = int(row["now_ns"])
        if now < last:
            raise ValueError(f"Time reversal in {path}")
        last = now
        for key, value in row.items():
            if key=='features_json':
                values=json.loads(value)
                version = row.get('feature_schema_version') or '1'
                width = {'1':19,'2':35}.get(version)
                if width is None or len(values) not in (0,width) or any(not math.isfinite(v) for v in values):
                    raise ValueError('Invalid policy feature vector')
                continue
            if key in ("mode", "reason", "kind", "detail", "state_status", "state_reason", "send_success", "lap_valid", "in_pit", "abort", "complete", "control_source", "policy_reason") or not value:
                continue
            if not math.isfinite(float(value)):
                raise ValueError(f"Non-finite value in {path}: {key}")
    return rows


def verify(run):
    run = Path(run).resolve()
    manifest = load(run / "manifest.json")
    if manifest.get("kind") != "actuator_run" or manifest.get("schema_version") != 1:
        raise ValueError("Unsupported actuator run")
    for name, expected in manifest.get("output_sha256", {}).items():
        p = (run / name).resolve()
        if not p.is_relative_to(run) or digest(p) != expected:
            raise ValueError(f"Output changed: {name}")
    for path, expected in manifest.get("input_sha256", {}).items():
        if digest(path) != expected:
            raise ValueError(f"Input changed: {path}")
    commands = read_rows(run / "commands.csv", ("now_ns", "applied_throttle", "send_success", "mode"))
    telemetry = read_rows(run / "telemetry.csv", ("now_ns", "sequence", "throttle_observed", "steering_raw"))
    events = read_rows(run / "events.csv", ("now_ns", "kind", "detail"))
    summary = load(run / "summary.json")
    counts = {"commands": len(commands), "telemetry": len(telemetry), "events": len(events)}
    controller = read_rows(run / "controller.csv", ("now_ns", "observation_sequence", "target_speed_kmh")) if (run / "controller.csv").exists() else []
    if controller:
        counts["controller"] = len(controller)
        sources = {(r["sequence"], r["observation_ns"], r["tracking_epoch"]) for r in telemetry}
        if any((r["observation_sequence"], r["observation_ns"], r["tracking_epoch"]) not in sources for r in controller):
            raise ValueError("Controller observation provenance missing")
    if manifest.get("scenario") == "demo" and not (run / "controller.csv").exists():
        raise ValueError("Demo controller log missing")
    policy=read_rows(run/'policy.csv',('now_ns','observation_sequence','control_source','features_json')) if (run/'policy.csv').exists() else []
    if policy:
        counts['policy']=len(policy)
        sources={(r['sequence'],r['observation_ns'],r['tracking_epoch']) for r in telemetry}
        if any((r['observation_sequence'],r['observation_ns'],r['tracking_epoch']) not in sources for r in policy):
            raise ValueError('Policy observation provenance missing')
    if ((manifest.get('config') or {}).get('policy') or (manifest.get('config') or {}).get('v2_shadow')) and not (run/'policy.csv').exists():
        raise ValueError('Policy log missing')
    if summary.get("written") != counts:
        raise ValueError("Summary row counts disagree with files")
    previous = 0
    for row in telemetry:
        sequence = int(row["sequence"])
        if sequence <= previous:
            raise ValueError("Duplicated/reversed telemetry sequence")
        previous = sequence
    return manifest, summary, commands, telemetry, events


def extrema(points, limit=1200):
    if len(points) <= limit:
        return points
    size = math.ceil(len(points) / (limit // 4))
    result = []
    for start in range(0, len(points), size):
        bucket = points[start:start + size]
        indices = sorted({0, len(bucket) - 1, min(range(len(bucket)), key=lambda i: bucket[i][1]), max(range(len(bucket)), key=lambda i: bucket[i][1])})
        result.extend(bucket[i] for i in indices)
    return result


def chart(series, title, unit):
    all_points = [p for _, points in series for p in points]
    if not all_points:
        return f"<h3>{html.escape(title)}</h3><p>관측 없음</p>"
    x0, x1 = min(p[0] for p in all_points), max(p[0] for p in all_points)
    y0, y1 = min(p[1] for p in all_points), max(p[1] for p in all_points)
    if y0 == y1:
        y0 -= .05
        y1 += .05
    colors = ("#2563eb", "#e45632", "#07966c")
    paths = []
    for index, (name, points) in enumerate(series):
        coords = " ".join(f"{60+(x-x0)/max(x1-x0,1e-9)*840:.2f},{200-(y-y0)/(y1-y0)*170:.2f}" for x, y in extrema(points))
        paths.append(f'<polyline fill="none" stroke="{colors[index % 3]}" stroke-width="1.5" points="{coords}"/><text x="{65+index*260}" y="245" fill="{colors[index % 3]}">{html.escape(name)}</text>')
    return f'<section><h3>{html.escape(title)}</h3><svg viewBox="0 0 940 265" role="img" aria-label="{html.escape(title)}"><path d="M60 25V200H910" fill="none" stroke="#999"/><text x="2" y="25">{y1:.3g}</text><text x="2" y="200">{y0:.3g}</text><text x="60" y="220">{x0:.2f}s</text><text x="845" y="220">{x1:.2f}s</text>{"".join(paths)}</svg><p>{html.escape(unit)}</p></section>'


def report(run):
    run = Path(run).resolve()
    manifest, summary, commands, telemetry, events = verify(run)
    path = run / "report.html"
    if path.exists():
        expected = manifest.get("output_sha256", {}).get("report.html")
        if expected and digest(path) == expected:
            return path
        raise FileExistsError("Report exists; refusing to overwrite")
    first = min([int(r["now_ns"]) for r in commands + telemetry + events], default=0)
    def signal(rows, field):
        return [((int(r["now_ns"]) - first) / 1e9, float(r[field])) for r in rows if r.get(field)]
    timing = {key: stats([int(r[key]) for r in commands if r.get(key)]) for key in ("validation_ns", "ipc_ns", "driver_ns", "update_interval_ns")}
    # Fresh-page observations only. Crossing a level is descriptive, never assumed causal.
    validation = {"schema_version": 1, "row_counts_verified": True, "recording_valid": summary.get("recording_valid") is True,
        "time_order_verified": True, "finite_values_verified": True, "counts": summary["written"], "command_timing_recomputed": timing,
        "source_sha256": {name: digest(run / name) for name in ("commands.csv", "telemetry.csv", "events.csv", "summary.json")}}
    save_json(run / "validation.json", validation)
    panels = []
    if (manifest.get('config') or {}).get('v2_shadow'):
        policy=read_rows(run/'policy.csv',('now_ns','control_source','prediction_steering'))
        panels.append('<p>새 정책 비교 주행: 실제 입력은 기존 제어기입니다. 아래 새 정책 출력은 장치에 전송하지 않았으며 학습 갱신도 하지 않았습니다.</p>')
        panels.append(chart([('새 정책 조향',signal(policy,'prediction_steering'))], '새 정책 조향 판단', '정규화 조향'))
        panels.append(chart([('새 정책 가속',signal(policy,'model_throttle')),('새 정책 제동',signal(policy,'model_brake'))], '새 정책 페달 판단', '정규화 입력'))
    if (manifest.get('config') or {}).get('policy'):
        policy=read_rows(run/'policy.csv',('now_ns','control_source'))
        unique=list({(r['observation_sequence'],r['tracking_epoch']):r for r in policy}.values())
        timing.update({key:stats([int(r[key]) for r in unique if r.get(key)]) for key in ('feature_ns','inference_ns')})
        for pedal,label in [('throttle','가속'),('brake','제동')]:
            panels.append(chart([('모델 요청',signal(policy,'model_'+pedal)),('PID 요청',signal(policy,'pid_'+pedal)),('ACC 관측',signal(telemetry,pedal+'_observed'))],label+' 모델·PID·관측 비교','페달 0~1 · 모델 요청과 실제 사용된 명령은 control_source로 구분'))
        transitions=[]
        for r in policy:
            if not transitions or (r['control_source'],r['policy_reason'])!=(transitions[-1]['control_source'],transitions[-1]['policy_reason']):
                transitions.append({k:r[k] for k in ['now_ns','observation_sequence','control_source','policy_reason']})
        panels.append('<h3>모델 인계·중단</h3><pre>'+html.escape(json.dumps(transitions,ensure_ascii=False,indent=2))+'</pre>')
    if manifest.get("scenario") == "demo":
        controller = read_rows(run / "controller.csv", ("now_ns", "target_speed_kmh"))
        single_lap=((manifest.get("config") or {}).get("baseline") or {}).get("config",{}).get("single_lap",False)
        note="km/h · 현재 위치에서 기준 경로 한 바퀴, 제한 60분" if single_lap else "km/h · 30초 제한 데모이며 자동 완주 검증이 아님"
        panels.append(chart([("목표", signal(controller,"target_speed_kmh")),("ACC",signal(telemetry,"speed_kmh"))],"데모 목표·관측 속도",note))
        if telemetry and "reference_grade_pct" in telemetry[0]:
            from .terrain import analyze
            terrain=analyze(telemetry,controller,summary)
            save_json(run / "terrain.json",terrain)
            panels.append('<h3>고저차·속도 반응 분석</h3><p>주행 제어 중인 관측만 집계. 경사 ±2%를 경계로 구분하며 기준 주행선의 높이 차이 추정치입니다. 경사가 원인임을 단독으로 입증하지 않습니다.</p><pre>'+html.escape(json.dumps(terrain,ensure_ascii=False,indent=2))+'</pre>')
            panels.append(chart([("ACC Y",signal(telemetry,"position_y")),("기준 경로 Y",signal(telemetry,"reference_elevation_m"))],"주행 높이","m · ACC 세계 좌표, 해발고도 아님"))
            panels.append(chart([("기준 경로 추정",signal(telemetry,"reference_grade_pct"))],"진행 방향 경사","% · 경로 앞뒤 5m의 높이 차이 / 평면 거리 10m, 양수 오르막"))
            panels.append(chart([("한 바퀴 비율",signal(controller,"lap_fraction"))],"연속 추적 구간의 한 바퀴 진행","1.0 = 기준 경로 전체 길이만큼 진행, ACC progress와 다름"))
        for field,label,unit in (("lateral_error_m","부호 있는 경로 횡오차","m"),("heading_error_rad","경로 방향 오차","rad"),("projection_distance_m","경로 투영 거리","m"),("path_s_m","경로상 거리","m · 출발선에서 순환")):
            panels.append(chart([("상태 계산",signal(telemetry,field))],label,unit))
        panels.append(demo_trajectory(manifest, telemetry, controller))
        reasons = {}
        for row in controller:
            reasons[row["reason"]] = reasons.get(row["reason"],0)+1
        panels.append(f'<h3>제어 판정별 행 수</h3><pre>{html.escape(json.dumps(reasons,ensure_ascii=False,indent=2))}</pre>')
    for field, label in (("steering", "조향 명령"), ("throttle", "가속 명령"), ("brake", "제동 명령")):
        panels.append(chart([("요청", signal(commands, f"requested_{field}")), ("제한 적용·전송 시도", signal(commands, f"applied_{field}"))], label, "정규화 가상 축. 실패한 전송은 commands.csv의 send_success=false로 구분."))
    for field, label, unit in (("speed_kmh", "ACC 관측 속도", "km/h"), ("throttle_observed", "ACC 관측 가속", "ACC pedal 0~1"),
                               ("brake_observed", "ACC 관측 제동", "ACC pedal 0~1"), ("steering_raw", "ACC 원시 조향", "가상 축·바퀴 각도와 동일하지 않음")):
        panels.append(chart([("ACC 관측", signal(telemetry, field))], label, unit))
    for field, label, unit in (("gear_raw", "ACC 관측 기어", "0=후진, 1=중립, 2=1단"), ("rpm", "엔진 회전수", "RPM")):
        if telemetry and field in telemetry[0]:
            panels.append(chart([("ACC 관측", signal(telemetry, field))], label, unit))
    timing_rows = "".join(f"<tr><td>{html.escape(key)}</td><td>{v['count']}</td><td>{v['mean_ms']}</td><td>{v['p95_ms']}</td><td>{v['p99_ms']}</td><td>{v['max_ms']}</td></tr>" for key, v in timing.items())
    event_rows = "".join(f"<tr><td>{(int(r['now_ns'])-first)/1e9:.3f}</td><td>{html.escape(r['kind'])}</td><td>{html.escape(r['detail'])}</td></tr>" for r in events)
    body = f'''<!doctype html><html lang="ko"><meta charset="utf-8"><title>차량 입력 시험 보고서</title>
<style>body{{font:15px system-ui;max-width:1080px;margin:auto;padding:24px;color:#172435}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{padding:7px;border:1px solid #ccc;overflow-wrap:anywhere}}svg{{width:100%;background:#f5f7fb}}section{{break-inside:avoid;margin:25px 0}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}.notice{{padding:16px;background:#fff3ce}}@media print{{section{{page-break-inside:avoid}}}}</style>
<h1>차량 입력 시험 보고서</h1><p>시나리오: {html.escape(manifest.get('scenario',''))} · 모의 장치: {manifest.get('mock',False)}</p>
<p class="notice">전송 성공과 ACC 반영은 별도입니다. 원시 조향을 가상 축이나 실제 바퀴 각도로 환산하지 않습니다. 기존 패드와 휠 프리셋의 입력 특성이 다릅니다. 드라이버·감시 프로세스 강제 종료의 제동은 보장하지 않습니다.</p>
<h2>종료와 중단</h2><pre>{html.escape(json.dumps(summary,ensure_ascii=False,indent=2))}</pre>
<p>완료 여부는 manifest.json을 확인하십시오. 관측 가속 0은 중단 이후 처음 확인한 값으로, 이미 0이었을 수 있습니다. 반응을 인과적으로 확정한 수치가 아닙니다.</p>
<h2>시간 비용 (ms)</h2><table><tr><th>측정 범위</th><th>수</th><th>평균</th><th>p95</th><th>p99</th><th>최대</th></tr>{timing_rows}</table>
<p>명령 검사, 프로세스 전달, 드라이버 호출, 실제 출력 간격을 구분합니다. 전체 시스템 지연으로 표현하지 않습니다. 폴링과 관측 나이는 요약에 별도 기록합니다.</p>
{''.join(panels)}<h2>상태 이벤트</h2><table><tr><th>경과 초</th><th>종류</th><th>내용</th></tr>{event_rows}</table>
<p>그래프만 최솟값·최댓값을 보존해 축소합니다. CSV에는 전체 저장 행을 유지합니다.</p></html>'''
    with path.open("x", encoding="utf-8", newline="\n") as out:
        out.write(body)
    return path


def demo_trajectory(manifest, telemetry, controller):
    observed = [(float(r["position_x"]),float(r["position_z"])) for r in telemetry]
    targets = [(float(r["target_x"]),float(r["target_z"])) for r in controller if r.get("target_x") and r.get("target_z")]
    if not observed:
        return "<p>데모 위치 관측 없음</p>"
    prepared = load(Path(manifest["prepared_path"]) / "manifest.json")
    reference_path = Path(prepared["track_model_path"]) / "reference.csv"
    if digest(reference_path) != prepared["input_sha256"].get(str(reference_path)):
        raise ValueError("Demo reference changed")
    with reference_path.open(encoding="utf-8",newline="") as stream:
        reference=[(float(r["position_x"]),float(r["position_z"])) for r in csv.DictReader(stream)]
    all_points=observed+targets
    x0=min(p[0] for p in all_points)-12; x1=max(p[0] for p in all_points)+12
    z0=min(p[1] for p in all_points)-12; z1=max(p[1] for p in all_points)+12
    scale=min(840/(x1-x0),480/(z1-z0))
    def xy(p): return f'{50+(p[0]-x0)*scale:.2f},{530-(p[1]-z0)*scale:.2f}'
    ref=" ".join(xy(p) for p in reference+[reference[0]])
    obs=" ".join(xy(p) for p in observed)
    marks="".join(f'<circle cx="{50+(x-x0)*scale:.2f}" cy="{530-(z-z0)*scale:.2f}" r="1.5" fill="#e45632"/>' for x,z in targets[::max(1,len(targets)//300)])
    return f'<section><h3>데모 구간 궤적 (X–Z 동일 축척)</h3><svg viewBox="0 0 940 580"><defs><clipPath id="demo-clip"><rect x="40" y="35" width="870" height="505"/></clipPath></defs><g clip-path="url(#demo-clip)"><polyline points="{ref}" fill="none" stroke="#aaa" stroke-width="3"/><polyline points="{obs}" fill="none" stroke="#2563eb" stroke-width="2"/>{marks}</g><text x="45" y="560">회색: 사람의 기준 주행선 · 파랑: ACC 관측 · 주황: 전방 목표점</text></svg><p>도로 경계·충돌 감지 기능은 없습니다. 위치와 목표점 표시이며 도로 중심선이 아닙니다.</p></section>'
