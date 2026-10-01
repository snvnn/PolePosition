from html import escape
import json
from pathlib import Path

STYLE='<style>body{font:16px system-ui;max-width:1100px;margin:36px auto;padding:0 20px;color:#172c3b}table{border-collapse:collapse}td,th{padding:8px;border:1px solid #bbb}svg{width:100%;height:auto}pre{white-space:pre-wrap}h1{font-size:26px}</style>'


def curve(history):
    maximum=max(max(r['train_loss'],r['validation_loss']) for r in history) or 1
    paths=[]
    for name,color in [('train_loss','#2379b2'),('validation_loss','#d36c27')]:
        points=' '.join(f'{50+850*i/max(1,len(history)-1):.2f},{250-210*r[name]/maximum:.2f}' for i,r in enumerate(history))
        paths.append(f'<polyline fill="none" stroke="{color}" points="{points}"/>')
    return f'<svg viewBox="0 0 950 300" role="img" aria-label="Epoch loss"><path d="M50 25V250H910" fill="none" stroke="#777"/><text x="0" y="25">{maximum:.4g}</text><text x="50" y="280">epoch 1</text><text x="820" y="280">epoch {history[-1]["epoch"]}</text>{"".join(paths)}</svg>'


def write(folder,body):
    with (Path(folder)/'report.html').open('x',encoding='utf-8') as f:
        f.write('<!doctype html><html lang="ko"><meta charset="utf-8"><title>폴포지션 모델 학습</title>'+STYLE+'<h1>저속 교사 모방 모델</h1>'+body+'</html>')


def learning_report(folder,history,manifest):
    write(folder,'<p>파랑: train · 주황: validation. Smoke 실행은 동작 검사이며 독립 평가가 아닙니다.</p>'+curve(history)+
          '<p>실게임 성능은 별도 검증합니다. <a href="summary.json">학습 요약</a> · <a href="history.csv">전체 epoch</a></p><pre>'+escape(json.dumps(manifest['config'],indent=2))+'</pre>')


def evaluation_report(folder,metrics,title):
    body='<p>'+escape(title)+'</p><p>기록된 관측에 대한 예측 오차입니다. 모델을 적용했을 때의 차량 궤적을 재현한 결과가 아닙니다.</p>'
    body+='<table><tr><th>집합</th><th>샘플</th><th>가속 MAE / p95</th><th>브레이크 MAE / p95</th><th>판정</th></tr>'
    for name,m in metrics.items():
        body+=f'<tr><td>{escape(name)}</td><td>{m["samples"]}</td><td>{m["throttle_mae"]:.5f} / {m["throttle_p95"]:.5f}</td><td>{m["brake_mae"]:.5f} / {m["brake_p95"]:.5f}</td><td>{"통과" if m["passed"] else "보류"}</td></tr>'
    body+='</table><pre>'+escape(json.dumps(metrics,ensure_ascii=False,indent=2))+'</pre>'
    prediction_path=Path(folder)/'predictions.csv'
    if prediction_path.exists():
        import csv
        from ModelAndActuator.report import chart
        with prediction_path.open(encoding='utf-8',newline='') as f:data=list(csv.DictReader(f))
        first=int(data[0]['observation_ns'])
        def signal(key):return [((int(r['observation_ns'])-first)/1e9,float(r[key])) for r in data]
        for key,label in [('throttle','가속'),('brake','브레이크')]:
            body+=chart([('교사 요청',signal('target_'+key)),('모델 예측',signal('predicted_'+key)),('ACC 원관측',signal('observed_'+key))],label,'0~1 · 원관측은 해당 요청보다 앞선 관측, 반영 지연을 맞추지 않음')
        body+=chart([('기준 경사',signal('grade_pct'))],'주행선 경사','%')
        body+=chart([('관측 속도',signal('speed_kmh'))],'교사 주행 속도','km/h')
    write(folder,body)
