"""Standalone evidence report. No network, no executable user content."""
from html import escape
import json

from .report import STYLE, chart


def plots(rows, time_axis=False):
    axis = 'lap_elapsed_seconds' if time_axis else 'progress'
    labels = [('position_x','position_z','주행 궤적 X–Z (m)'),
              (axis,'speed_kmh','속도 (km/h)'), (axis,'throttle','가속 페달 (0..1)'),
              (axis,'brake','브레이크 (0..1)'), (axis,'steering_raw','조향 (원시값)')]
    return '<div class="grid">'+''.join(chart(rows,x,y,label,250,x=='position_x') for x,y,label in labels)+'</div>'


def render_review(findings, rows_by_lap, config):
    count = sum(len(l['candidates']) for l in findings['laps'])
    parts = ['<!doctype html><html lang="ko"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             f'<title>주행 데이터 검토</title><style>{STYLE}</style></head><body>',
             '<h1>주행 데이터 · 의심 구간 검토</h1>',
             f'<p>검토 대상 {len(findings["laps"])}랩 · 후보 {count}개. 이 보고서는 판정 전 증거입니다.</p>',
             '<p>후보는 사고 확정이나 자동 제외가 아닙니다. 후보 없음도 무사고 증명이 아닙니다. '
             '비교 기준은 train만 사용하며 해당 train 랩 검사 시 자기 자신을 제외합니다. 불확실한 랩은 보류합니다.</p>',
             '<p><a href="findings.json">원본 sequence·관측값·기준값</a> · <a href="baselines.json">학습 기준 통계</a> · '
             '<a href="decisions.template.json">판정 초안</a> · <a href="manifest.json">설정·해시</a></p>',
             '<table><thead><tr><th>랩</th><th>집합</th><th>후보 수</th><th>종류</th></tr></thead><tbody>']
    for lap in findings['laps']:
        reasons = sorted({r for c in lap['candidates'] for r in c['reasons']})
        parts.append(f'<tr><td><a href="#lap-{lap["ordinal"]}">{lap["ordinal"]}</a></td><td>{lap["split"]}</td><td>{len(lap["candidates"])}</td><td>{escape(", ".join(reasons))}</td></tr>')
    parts.append('</tbody></table>')
    # Candidates first; all full-lap plots still appear, including zero-hit laps.
    order = sorted(findings['laps'], key=lambda l:(not bool(l['candidates']),l['ordinal']))
    for lap in order:
        rows = [dict(r, lap_elapsed_seconds=int(r['lap_elapsed_ns'])/1e9) for r in rows_by_lap[lap['lap_id']]]
        parts.append(f'<section id="lap-{lap["ordinal"]}"><h2>랩 {lap["ordinal"]} · {lap["split"]} · 전체 구간</h2><code>{escape(lap["lap_id"])}</code>')
        parts.append(f'<p>{lap["samples"]}샘플 · {lap["duration_seconds"]:.3f}초 · 후보 {len(lap["candidates"])}개</p>')
        parts.append(plots(rows)+'</section>')
        for candidate in lap['candidates']:
            cid = candidate['candidate_id']
            low = candidate['start_elapsed_ns']-round(config['context_seconds']*1e9)
            high = candidate['end_elapsed_ns']+round(config['context_seconds']*1e9)
            context = [r for r in rows if low <= int(r['elapsed_ns']) <= high]
            start = (candidate['start_elapsed_ns']-int(rows[0]['elapsed_ns']))/1e9
            end = (candidate['end_elapsed_ns']-int(rows[0]['elapsed_ns']))/1e9
            parts.append(f'<section id="{escape(cid)}"><h2>랩 {lap["ordinal"]} · 검토 후보</h2><code>{escape(cid)}</code>')
            parts.append(f'<p>후보 시간: 랩 시작 후 {start:.3f}~{end:.3f}초 · 원본 sequence {candidate["start_sequence"]}~{candidate["end_sequence"]} · 진행률 {candidate["start_progress"]:.5f}~{candidate["end_progress"]:.5f}</p>')
            parts.append('<table><thead><tr><th>탐지 이유</th><th>관측 범위</th><th>기준 범위</th></tr></thead><tbody>')
            for reason in candidate['reasons']:
                evidence = [e for e in candidate['evidence'] if e['reason']==reason]
                observed = [e['observed'] for e in evidence if e['observed'] is not None]
                thresholds = [e['threshold'] for e in evidence]
                desc = f'{min(observed):.5f}~{max(observed):.5f}' if observed else '비교 자료 부족'
                parts.append(f'<tr><td>{reason}</td><td>{desc}</td><td>{min(thresholds):.5f}~{max(thresholds):.5f}</td></tr>')
            parts.append('</tbody></table><p>궤적 차이: m · 속도/저속: km/h · 조향: 원시값 · 역행: 진행률 비율. '
                         '시간 그래프는 랩 시작 이후 호스트 초이며 후보 앞뒤 문맥을 포함합니다.</p>')
            parts.append(plots(context,True)+'</section>')
    for title,key in [('기존 제외 랩','excluded_laps'),('부분·제외 구간','excluded_intervals'),('원본 이벤트','source_events')]:
        parts.append(f'<section><h2>{title}</h2><pre style="white-space:pre-wrap;overflow-wrap:anywhere">{escape(json.dumps(findings[key],ensure_ascii=False,indent=2))}</pre></section>')
    return '\n'.join(parts+['</body></html>'])
