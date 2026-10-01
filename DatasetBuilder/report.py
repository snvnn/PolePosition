"""Self-contained SVG report; plotted reduction never changes exported samples."""
from html import escape
import math


STYLE = """
body{font:16px system-ui;margin:32px auto;padding:0 24px;max-width:1200px;color:#17313a;background:#f7fafb}
table{border-collapse:collapse;width:100%}
td,th{padding:8px;border:1px solid #c8d5da;text-align:left}
figure{background:white;margin:12px 0;padding:12px;min-width:0;break-inside:avoid;page-break-inside:avoid}
figcaption{break-after:avoid;page-break-after:avoid}
svg{display:block;width:100%;height:auto}
svg text{font-size:11px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(340px,100%),1fr));gap:12px}
code{overflow-wrap:anywhere}
@page{size:A4 portrait;margin:12mm}
@media print{
  body{font-size:10pt;margin:0;padding:0;max-width:none;background:white}
  h1{font-size:18pt} h2{font-size:14pt;margin:0 0 3mm;break-after:avoid;page-break-after:avoid}
  p{margin:2mm 0} code{font-size:8pt}
  section{break-before:page;page-break-before:always}
  .lap-summary,.lap-stats{break-inside:avoid;page-break-inside:avoid}
  thead{display:table-header-group} tr{break-inside:avoid;page-break-inside:avoid}
  td,th{padding:1.5mm;font-size:9pt}
  .grid{display:block;font-size:0}
  figure{display:inline-block;box-sizing:border-box;width:49%;margin:2mm 0;padding:2mm;vertical-align:top;font-size:9pt}
  figcaption{font-size:9pt;line-height:1.3}
  svg{width:100%;height:auto}
  svg text{font-size:20px}
}
"""


def envelope(points, buckets):
    if len(points) <= buckets*4:
        return points
    result = []
    size = math.ceil(len(points)/buckets)
    for offset in range(0, len(points), size):
        chunk = points[offset:offset+size]
        indices = {0, len(chunk)-1}
        for axis in (0, 1):
            indices.add(min(range(len(chunk)), key=lambda i: chunk[i][axis]))
            indices.add(max(range(len(chunk)), key=lambda i: chunk[i][axis]))
        result.extend(chunk[i] for i in sorted(indices))
    return result


def chart(rows, xkey, ykey, label, buckets, equal=False):
    points = [(float(r[xkey]), float(r[ykey])) for r in rows]
    points = envelope(points, buckets)
    if not points:
        return ""
    xmin, xmax = min(x for x,y in points), max(x for x,y in points)
    ymin, ymax = min(y for x,y in points), max(y for x,y in points)
    # Expand the displayed coordinate range when preserving aspect ratio. Both
    # ticks and data then use this same domain, including the centered padding.
    xmid, ymid = (xmin+xmax)/2, (ymin+ymax)/2
    dx, dy = max(xmax-xmin, .0001), max(ymax-ymin, .0001)
    sx, sy = 630/dx, 180/dy
    if equal:
        sx = sy = min(sx, sy)
        dx, dy = 630/sx, 180/sy
    xmin, xmax = xmid-dx/2, xmid+dx/2
    ymin, ymax = ymid-dy/2, ymid+dy/2
    # Break progress-wrap lines instead of drawing a horizontal line across the chart.
    paths, path = [], []
    previous = None
    for x,y in points:
        if xkey == "progress" and previous is not None and previous-x > .5:
            paths.append(path); path = []
        path.append(f"{90+(x-xmin)*sx:.2f},{25+(ymax-y)*sy:.2f}")
        previous = x
    paths.append(path)
    lines = ''.join('<polyline fill="none" stroke="#167582" stroke-width="1" points="'+ ' '.join(p)+'"/>' for p in paths)
    xticks = ''.join(
        f'<path d="M{90+630*f:.2f} 205v4" stroke="#777"/>'
        f'<text class="x-tick" x="{90+630*f:.2f}" y="228" text-anchor="middle">{xmin+dx*f:.3f}</text>'
        for f in (0, .25, .5, .75, 1))
    yticks = ''.join(
        f'<path d="M86 {25+180*f:.2f}h4" stroke="#777"/>'
        f'<text class="y-tick" x="84" y="{25+180*f:.2f}" text-anchor="end" dominant-baseline="middle">{ymax-dy*f:.2f}</text>'
        for f in (0, .5, 1))
    return (f'<figure><figcaption>{escape(label)}</figcaption><svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 780 270" width="780" height="270" role="img" aria-label="{escape(label)}">'
            '<path d="M90 25V205H720" fill="none" stroke="#777"/>' + lines +
            xticks + yticks +
            f'<text x="405" y="258" text-anchor="middle">{escape(xkey)}</text></svg></figure>')


def render(laps, rows, manifest, validation):
    accepted = sum(l['accepted'] for l in laps)
    parts = ['<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>DatasetBuilder 품질 보고서</title>',
             f'<style>{STYLE}</style></head><body>',
             '<h1>폴포지션 · 주행 데이터 품질</h1>',
             f'<p>완전한 랩 {len(laps)} · 채택 {accepted} · 제외 {len(laps)-accepted}</p>',
             '<p>같은 세션의 시간순 랩 분리입니다. 다른 세션·조건에 대한 일반화 성능을 보장하지 않습니다. '
             '궤적은 도로 중심선이 아니며, 조작값은 ACC 관측값입니다. 사고·스핀을 자동 확정하지 않습니다.</p>',
             '<p>섹터와 랩 시간은 호스트 관측 시각의 근삿값입니다. 그래프만 축소하며 CSV는 전체 샘플을 보존합니다.</p>',
             '<p><a href="manifest.json">설정·해시·단위·품질 통계</a> · <a href="validation.json">원본 검증</a> · <a href="excluded_intervals.csv">제외 범위</a> · <a href="splits.json">데이터 분리</a></p>']
    if accepted < 3: parts.append('<p><strong>분리 미완료: 채택 랩이 3개 미만입니다.</strong></p>')
    for warning in validation['warnings']: parts.append(f'<p>검증 주의: {escape(warning)}</p>')
    parts.append('<table><thead><tr><th>완주 순번</th><th>랩 시간(초)</th><th>집합</th><th>제외 이유</th><th>샘플</th></tr></thead><tbody>')
    for lap in laps:
        parts.append(f'<tr><td><a href="#lap{lap["ordinal"]}">{lap["ordinal"]}</a></td><td>{lap["duration_seconds"]:.3f}</td><td>{lap["split"]}</td><td>{escape(lap["reasons"])}</td><td>{lap["samples"]}</td></tr>')
    parts.append('</tbody></table>')
    for lap in laps:
        lid = lap['lap_id']; q = manifest['quality'][lid]
        parts.append(f'<section id="lap{lap["ordinal"]}"><div class="lap-summary"><h2>랩 {lap["ordinal"]} · {lap["split"]}</h2><code>{escape(lid)}</code>')
        if lap['accepted']: parts.append(f'<p><a href="laps/{escape(lid)}.csv">전체 샘플 CSV</a></p>')
        parts.append('<table class="lap-stats"><thead><tr><th>관측 간격</th><th>평균 ms</th><th>p95 ms</th><th>p99 ms</th><th>최대 ms</th></tr></thead><tbody>')
        for name in ('sample_intervals','physics_change_intervals','graphics_change_intervals'):
            vals = [q[name][k] for k in ('mean_ms','p95_ms','p99_ms','max_ms')]
            parts.append('<tr><td>'+name+'</td>'+''.join(f'<td>{v:.3f}</td>' if v is not None else '<td>관측 부족</td>' for v in vals)+'</tr>')
        parts.append('</tbody></table><p>섹터 1 / 2 / 3 (근사 초): '+(' / '.join(f'{s:.3f}' for s in q['sector_seconds']) if q['sector_seconds'] else '전환 관측 부족')+'</p></div><div class="grid">')
        for x,y,label in [('position_x','position_z','주행 궤적 X–Z (m, 동일 축척)'),
                          ('progress','speed_kmh','속도 (km/h)'),('progress','throttle','가속 페달 (0..1)'),
                          ('progress','brake','브레이크 (0..1)'),('progress','steering_raw','조향 (ACC 원시값)')]:
            parts.append(chart(rows[lid], x,y,label,manifest['config']['plot_buckets'],x=='position_x'))
        parts.append('</div></section>')
    parts.append('</body></html>')
    return '\n'.join(parts)
