"""Read back a run, check inventories, and render standalone HTML/SVG."""
import csv
from html import escape
import json
import math
from pathlib import Path
import re

from DatasetBuilder.builder import json_write
from DatasetBuilder.report import STYLE, chart, envelope
from DatasetBuilder.review import digest, finish_manifest, read_json, verify_snapshot
from TrackModel.report import overlay

GEOMETRY = ('segment_index','fraction','projected_x','projected_y','projected_z','projection_distance_m',
            's_m','accumulated_m','lateral_error_m','vehicle_heading_rad','path_heading_rad','heading_error_rad','curvature_per_m')


def signal_chart(rows, key, label):
    """Keep a common axis while breaking polylines at every invalid interval/epoch."""
    base=chart(rows,'seconds',key,label,600)
    if not rows:return base
    xs=[r['seconds'] for r in rows];ys=[r[key] for r in rows]
    xmid=(min(xs)+max(xs))/2;ymid=(min(ys)+max(ys))/2
    dx=max(max(xs)-min(xs),.0001);dy=max(max(ys)-min(ys),.0001)
    xmin=xmid-dx/2;ymax=ymid+dy/2
    groups=[];current=[];previous=None
    for row in rows:
        if previous is not None and row['plot_group']!=previous:
            groups.append(current);current=[]
        current.append((row['seconds'],row[key]));previous=row['plot_group']
    groups.append(current)
    paths=[]
    for group in groups:
        coords=' '.join(f'{90+(x-xmin)*630/dx:.2f},{25+(ymax-y)*180/dy:.2f}' for x,y in envelope(group,600))
        paths.append(f'<polyline fill="none" stroke="#167582" stroke-width="1" points="{coords}"/>')
    return re.sub(r'<polyline\b[^>]*/>',lambda match:''.join(paths),base,count=1)


def load_run(folder):
    folder = Path(folder).resolve()
    meta = read_json(folder/'manifest.json')
    if meta.get('kind') != 'state_run' or meta.get('schema_version') != 1 or meta.get('tool_version') != '0.1.0' or meta.get('complete') is not True:
        raise ValueError('Incomplete or unsupported state run')
    if set(meta['output_sha256']) != {'states.csv','events.csv','summary.json','report.html'}:
        raise ValueError('State output inventory mismatch')
    verify_snapshot({str(folder/k):v for k,v in meta['output_sha256'].items()})
    prepared = Path(meta['prepared_path'])
    if digest(prepared/'manifest.json') != meta['prepared_manifest_sha256']: raise ValueError('Preparation changed')
    prep = read_json(prepared/'manifest.json')
    if prep['complete'] is not True: raise ValueError('Incomplete preparation')
    verify_snapshot(prep['input_sha256'])
    verify_snapshot({str(prepared/k):v for k,v in prep['output_sha256'].items()})
    summary = read_json(folder/'summary.json')
    if not summary['normal_shutdown'] or not summary['recording_valid'] or summary['dropped_states'] or summary['dropped_events']:
        raise ValueError('Run recording verification failed')
    with (folder/'states.csv').open(encoding='utf-8',newline='') as stream:
        reader=csv.DictReader(stream)
        if reader.fieldnames != meta['fields']: raise ValueError('State columns mismatch')
        rows=list(reader)
    with (folder/'events.csv').open(encoding='utf-8',newline='') as stream:
        reader=csv.DictReader(stream)
        if reader.fieldnames != ['now_ns','kind','detail']: raise ValueError('Event columns mismatch')
        events=list(reader)
    if len(rows) != summary['written_rows'] or len(rows) != summary['calculated_rows'] or len(events) != summary['written_events'] or len(events) != summary['generated_events']:
        raise ValueError('State/event counts mismatch')
    last = -1
    allowed={'ready','acquiring','invalid','waiting','unavailable'}
    for row in rows:
        now=int(row['now_ns'])
        if now < last: raise ValueError('Run time order')
        last=now
        if row['status'] not in allowed: raise ValueError('Unknown calculation status')
        for k,v in row.items():
            if k not in {'status','reason','lap_valid','in_pit','forward_curvature_json'} and v:
                if not math.isfinite(float(v)): raise ValueError('Nonfinite state value')
        geometry=any(row[k] for k in GEOMETRY)
        if row['status'] in {'ready','acquiring'}:
            if not all(row[k] for k in GEOMETRY): raise ValueError('Missing projection')
            if not 0 <= float(row['s_m']) < meta['track_length_m'] or not 0<=float(row['fraction'])<=1:
                raise ValueError('Invalid projection range')
            curvature=json.loads(row['forward_curvature_json'])
            if len(curvature)!=len(meta['config']['curvature_offsets_m']) or not all(math.isfinite(v) for v in curvature):
                raise ValueError('Invalid curvature lookahead')
        elif geometry or json.loads(row['forward_curvature_json']) != []:
            raise ValueError('Invalid/waiting result retained stale geometry')
    return meta,summary,rows,events,read_json(prepared/'replay.json')


def report(folder):
    folder=Path(folder).resolve()
    meta,summary,rows,events,replay=load_run(folder)
    parts=['<!doctype html><html lang="ko"><head><meta charset="utf-8">',
           f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>StateProcessor 진단</title><style>{STYLE}</style></head><body>',
           '<h1>폴포지션 · 경로 기준 상태 진단</h1>',
           '<p>사람의 기준 주행선에 대한 기하 상태입니다. 계산 성공은 차량 제어 허가·무사고 판정이 아닙니다. 단일 세션 검증이며 독립적인 모델 성능 평가가 아닙니다.</p>',
           '<p>주황: 관측 위치 · 청록: 투영점. 횡오차 양수는 X–Z 접선의 왼쪽입니다. ACC 진행률을 경로 거리의 정답으로 사용하지 않습니다.</p>',
           '<p><a href="manifest.json">규약·입력</a> · <a href="summary.json">시간·상태 통계</a> · <a href="states.csv">전체 상태 CSV</a> · <a href="events.csv">이벤트</a></p>',
           f'<p>실행: {escape(meta["mode"])} · 관측 {summary["observations"]:,}개 · 로그 누락 {summary["dropped_states"] + summary["dropped_events"]}개</p>',
           '<h2>처리 시간과 데이터 나이</h2><p>단위 µs. 코어 계산 비용·실제 폴링 간격·관측 나이를 구분합니다. 전체 시스템 지연을 측정한 값이 아닙니다. 재생에는 실시간 폴링 간격이 없습니다.</p>',
           '<table><tr><th>측정</th><th>평균</th><th>p95</th><th>p99</th><th>최대</th></tr>']
    for name,key in [('코어 계산','core_timing'),('실제 폴링 간격','poll_timing'),('관측 나이','observation_age')]:
        stats=summary[key]
        parts.append(f'<tr><td>{name}</td>'+''.join(f'<td>{stats[k]:.2f}</td>' if stats['count'] else '<td>해당 없음</td>' for k in ('mean_us','p95_us','p99_us','max_us'))+'</tr>')
    parts.append('</table><h2>상태 집계</h2><pre>'+escape(json.dumps(summary['states'],indent=2))+'</pre>')
    parts.append('<p>초기 획득은 새 관측 3회 확인 후 ready가 됩니다. acquiring의 투영은 진단 후보이며 유효한 추적 상태가 아닙니다. invalid/waiting/unavailable에는 투영값이 없습니다.</p>')
    sections = replay['laps'] if meta['mode']=='replay' else [dict(lap_id='live',ordinal='실시간 전체',split='diagnostic',start_ns=0,end_ns=2**64,segment_id=None)]
    assessments=[]
    for lap in sections:
        selected=[r for r in rows if r['observation_ns'] and lap['start_ns']<=int(r['observation_ns'])<lap['end_ns']
                  and (lap['segment_id'] is None or int(r['segment_id'])==lap['segment_id'])]
        ready=[r for r in selected if r['status']=='ready']
        faults=[r for r in selected if r['status']=='invalid']
        assessments.append(dict(lap_id=lap['lap_id'],split=lap['split'],rows=len(selected),ready=len(ready),invalid=len(faults),reasons=sorted({r['reason'] for r in faults})))
        parts.append(f'<section class="lap"><h2>{escape(str(lap["ordinal"]))} · {escape(lap["split"])}</h2><p>{escape(lap["lap_id"])} · ready {len(ready):,} · invalid {len(faults):,}</p>')
        raw=[];projected=[];signals=[];previous_epoch=None;plot_group=0
        for r in selected:
            if r['status']!='ready':
                if raw and raw[-1] is not None: raw.append(None);projected.append(None)
                plot_group+=1
                continue
            if previous_epoch is not None and r['tracking_epoch']!=previous_epoch:
                raw.append(None);projected.append(None);plot_group+=1
            previous_epoch=r['tracking_epoch']
            raw.append(tuple(float(r[k]) for k in ('position_x','position_y','position_z')))
            projected.append(tuple(float(r[k]) for k in ('projected_x','projected_y','projected_z')))
            signals.append(dict(seconds=(int(r['observation_ns'])-lap['start_ns'])/1e9,plot_group=plot_group,
                                **{k:float(r[k]) for k in ('s_m','lateral_error_m','heading_error_rad','curvature_per_m')}))
        if signals:
            parts.append(overlay(raw,projected,'관측 위치와 경로 투영점'))
            parts.append('<div class="grid">')
            for key,label in [('s_m','경로 거리 s (m)'),('lateral_error_m','부호 있는 횡오차 (m)'),('heading_error_rad','방향 오차 (rad)'),('curvature_per_m','현재 곡률 (1/m)')]:
                parts.append(signal_chart(signals,key,label))
            parts.append('</div>')
        else: parts.append('<p>표시할 ready 관측이 없습니다.</p>')
        parts.append('</section>')
    parts.append('<h2>상태 전환과 기록 이벤트</h2><table><tr><th>시각 (s)</th><th>종류</th><th>내용</th></tr>')
    for e in events:
        parts.append(f'<tr><td>{int(e["now_ns"])/1e9:.3f}</td><td>{escape(e["kind"])}</td><td>{escape(e["detail"])}</td></tr>')
    parts.append('</table><p>모든 CSV 샘플을 보존하며 그래프에만 최솟값·최댓값 보존 축소를 적용합니다. 보류·제외 구간은 재생 진단에만 사용하고 기존 판정·집합을 변경하지 않습니다.</p></body></html>')
    text='\n'.join(parts)
    temp=folder/'report.next.html'
    with temp.open('x',encoding='utf-8') as stream:stream.write(text)
    temp.replace(folder/'report.html')
    meta['output_sha256']['report.html']=digest(folder/'report.html')
    meta['report_complete']=True
    meta['report_lap_assessments']=assessments
    finish_manifest(folder,meta)
    load_run(folder)
    return folder/'report.html'
