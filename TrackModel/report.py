"""Standalone HTML/SVG diagnostics; raw and generated paths share one transform."""
from html import escape
import math

from DatasetBuilder.report import STYLE, chart, envelope
from .geometry import arc_lengths


def overlay(raw, generated, title, closing=None):
    all_points = [p for p in raw+generated if p is not None]
    xmin,xmax = min(p[0] for p in all_points),max(p[0] for p in all_points)
    zmin,zmax = min(p[2] for p in all_points),max(p[2] for p in all_points)
    dx,dz = max(xmax-xmin,.01)*1.08,max(zmax-zmin,.01)*1.08
    scale = min(740/dx,440/dz)
    dx,dz = 740/scale,440/scale
    xmin,zmin = (xmin+xmax-dx)/2,(zmin+zmax-dz)/2
    def project(p): return f'{100+(p[0]-xmin)*scale:.3f},{35+(zmin+dz-p[2])*scale:.3f}'
    result = [f'<figure><figcaption>{escape(title)}</figcaption>',
              '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 560" width="900" height="560">',
              '<path d="M100 35V475H840" fill="none" stroke="#87999d"/>']
    for points,color,width in ((raw,'#d08428',2.6),(generated,'#087d8b',1.3)):
        run = []
        for point in points+[None]:
            if point is not None:
                run.append((point[0],point[2]))
            elif run:
                coordinates = ' '.join(project((x,0,z)) for x,z in envelope(run,1000))
                result.append(f'<polyline fill="none" stroke="{color}" stroke-width="{width}" points="{coordinates}"/>')
                run = []
    if closing:
        result.append('<polyline fill="none" stroke="#b62541" stroke-width="2" stroke-dasharray="4 3" points="'+
                      ' '.join(project(p) for p in closing)+'"/>')
    for f in (0,.25,.5,.75,1):
        result.append(f'<text x="{100+740*f}" y="500" text-anchor="middle">{xmin+dx*f:.3f}</text>')
    for f in (0,.5,1):
        result.append(f'<text x="92" y="{35+440*f}" text-anchor="end">{zmin+dz*(1-f):.3f}</text>')
    result.append('<text x="470" y="525" text-anchor="middle">X (m); vertical axis Z (m); equal scale</text></svg></figure>')
    return ''.join(result)


def render(manifest, validation, geometry=None):
    selected = manifest.get('selected_lap',{})
    parts = ['<!doctype html><html lang="ko"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             f'<title>TrackModel 기준 경로</title><style>{STYLE}</style></head><body>',
             '<h1>폴포지션 · 기준 주행선</h1>',
             f'<p>품질 검사: {"통과" if validation["passed"] else "실패"} · 선택 랩 {selected.get("ordinal","미선정")}</p>',
             '<p>검토를 통과한 train 중 호스트 관측 시간이 가장 짧은 실제 랩을 사용합니다. '
             '도로 중심선이나 실제 자동 주행 성공을 의미하지 않습니다. validation/test는 경로 결정에 사용하지 않습니다.</p>',
             '<p><a href="manifest.json">입력·선택·규약</a> · <a href="validation.json">검증 상세</a></p>']
    if geometry: parts.append('<p><a href="reference.csv">기준 경로 CSV</a></p>')
    parts.append('<table><tr><th>후보 랩</th><th>관측 시간(초)</th><th>선택</th></tr>')
    for lap in manifest.get('candidates',[]):
        parts.append(f'<tr><td>{lap["ordinal"]}</td><td>{lap["duration_seconds"]:.6f}</td>'
                     f'<td>{"선택" if lap["lap_id"]==selected.get("lap_id") else ""}</td></tr>')
    parts.append('</table><h2>품질 측정값</h2><table><tr><th>항목</th><th>측정값</th><th>상한</th></tr>')
    for name,value in validation.get('metrics',{}).items():
        limit=validation.get('limits',{}).get(name,'')
        parts.append(f'<tr><td>{escape(name)}</td><td>{value:.9g}</td><td>{limit}</td></tr>')
    parts.append('</table>')
    for error in validation.get('errors',[]):parts.append(f'<p>{escape(error)}</p>')
    if geometry:
        raw,rows=geometry['raw'],geometry['rows']
        points=[tuple(r[k] for k in ('position_x','position_y','position_z')) for r in rows]
        parts.append('<p>주황: 원래 주행선 · 청록: 생성 경로 · 붉은 점선: 원본 끝→시작 연결. 두 궤적은 같은 축척입니다.</p>')
        parts.append(overlay(raw,points+[points[0]],'전체 주행선 중첩', [raw[-1],raw[0]]))
        cumulative=arc_lengths(raw);length=cumulative[-1]
        tail=[p for p,s in zip(raw,cumulative) if length-s <= 30]
        head=[p for p,s in zip(raw,cumulative) if s <= 30]
        # Separate source pieces: joining across the seam is drawn explicitly as a dashed line.
        near_raw=tail+[None]+head
        near_points=[p for p,r in zip(points,rows) if geometry['length_m']-r['s_m']<=30]
        near_points += [p for p,r in zip(points,rows) if r['s_m']<=30]
        parts.append(overlay(near_raw,near_points,'출발선 앞뒤 30m 확대', [raw[-1],raw[0]]))
        unwrapped=[];angle=rows[0]['heading_path_rad'];previous=angle
        for row in rows:
            current=row['heading_path_rad']
            angle += math.atan2(math.sin(current-previous), math.cos(current-previous))
            unwrapped.append(dict(row,heading_unwrapped_rad=angle));previous=current
        parts.append('<p>방향각 그림은 ±π 경계의 표시 점프를 풀어서 그립니다. CSV 방향각은 atan2의 원래 범위를 유지합니다.</p><div class="grid">')
        for key,label in [('position_y','높이 Y (m)'),('heading_unwrapped_rad','기준 경로 방향 (rad)'),('curvature_per_m','부호 있는 곡률 (1/m)')]:
            parts.append(chart(unwrapped,'s_m',key,label,500))
        parts.append(chart(geometry['displacement'],'s_m','displacement_m','평활 전후 대응점 X–Z 이동량 (m)',500))
        parts.append('</div>')
    parts.append('<p>거리는 X–Z 평면 기준이며 Y를 보존합니다. 접선·곡률의 부호와 ACC heading·조향의 대응은 아직 보정하지 않았습니다.</p>')
    return '\n'.join(parts+['</body></html>'])
