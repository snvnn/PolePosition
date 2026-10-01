"""Read-only integration audit for a fixed training run and staged policy trials."""
import argparse
import json
import os
from pathlib import Path
from html import escape

from ModelTrainer.common import digest, finish, hashes, load_artifact, new_folder, read, save
from .live import inspect
from .report import STYLE


def build(dataset,repeated,model,equivalent,runs):
    dataset,dm=load_artifact(dataset,'teacher_dataset')
    repeated,_=load_artifact(repeated,'teacher_dataset')
    model,mm=load_artifact(model,'policy_export')
    equivalent,_=load_artifact(equivalent,'policy_export')
    training,tm=load_artifact(mm['training_run'],'policy_training')
    files=['train.jsonl','validation.jsonl','test.jsonl','normalizer.json','excluded.csv']
    repeat={name:digest(dataset/name)==digest(repeated/name) for name in files}
    if not all(repeat.values()) or digest(model/'model.json')!=digest(equivalent/'model.json'):
        raise ValueError('Data/model reproduction mismatch')
    details=[inspect(run) for run in runs]
    drives=[d for d in details if d['mode']=='drive']
    if [d['requested_model_seconds'] for d in drives]!=[5.,10.,15.]:raise ValueError('Staged trial inventory mismatch')
    for d in details:
        if d['model_json_sha256']!=digest(model/'model.json'):raise ValueError('Trial used a different model')
        if not all(d[k] for k in ['complete','normal_shutdown','stop_confirmed','released']) or d['dropped']!=0 or d['inference']['p99_ms']>1:
            raise ValueError('Trial did not pass recording/stop/timing checks')
        if d['mode']=='drive' and (d['controller_exit_reason']!='policy_duration_complete' or d['model_observation_interval_seconds']<d['requested_model_seconds'] or d['successful_model_transmissions']==0):
            raise ValueError('Model did not complete its assigned duration')
    out=new_folder('evaluation_runs','integration')
    inputs=[p/'manifest.json' for p in [dataset,repeated,model,equivalent,training,*map(Path,runs)]]
    result=dict(schema_version=1,kind='policy_integration',complete=False,input_sha256=hashes(inputs))
    save(out/'manifest.json',result)
    try:
        evidence=dict(dataset=str(dataset),repeated_dataset=str(repeated),model=str(model),equivalent_export=str(equivalent),training=str(training),
            counts=dm['counts'],training_summary=read(training/'summary.json'),evaluations=mm['evaluations'],parity_max_abs=mm['parity_max_abs'],
            reproduced_files=repeat,model_json_sha256=digest(model/'model.json'),trials=details,
            caveats=['Same vehicle, track and teacher; not independent generalization.',
                'No uphill holdout examples; throttle/coast validation groups have only 7/3 examples.',
                'Target 10 km/h but model trials reach about 11-12 km/h. No claim of better PID performance.',
                'Older raw summary policy_model_elapsed_ns includes stopping tail. Trial durations here are recomputed from model-to-stop observation transitions.'])
        save(out/'verification.json',evidence)
        def link(p,label):return '<a href="'+escape(os.path.relpath(p,out).replace('\\','/'))+'">'+escape(label)+'</a>'
        body='<h1>저속 교사 모방 모델 · 통합 검증</h1><p>학습·재개·Rust 추론·실게임 5/10/15초 가감속 연결을 검증했습니다. PID보다 좋은 제어나 모델 한 바퀴 완주를 뜻하지 않습니다.</p>'
        body+='<p>'+link(training/'report.html','학습 곡선')+' · '+link(model/'report.html','고정 모델 평가')+' · '+link(out/'verification.json','전체 검증 수치')+'</p>'
        ts=evidence['training_summary']
        body+=f'<p>train {dm["counts"]["train"]:,} / validation {dm["counts"]["validation"]} / test {dm["counts"]["test"]}행. {ts["epochs"]} epoch · {ts["updates"]:,}회 갱신 · best epoch {ts["best_epoch"]}. Python/Rust 최대 차이 {mm["parity_max_abs"]:.3g}.</p>'
        body+='<h2>실제 모델 담당 시간</h2><p>F7 이후 PID 출발·인계와 최종 정지 시간은 아래 모델 담당 시간에 포함하지 않습니다.</p><table><tr><th>시험</th><th>모델 시간</th><th>성공 전송</th><th>속도 km/h</th><th>최대 횡오차 m</th><th>추론 p99 ms</th><th>정차·반환</th></tr>'
        for path,d in zip(runs,details):
            speeds='—' if d['model_speed_min_kmh'] is None else f'{d["model_speed_min_kmh"]:.3f}–{d["model_speed_max_kmh"]:.3f}'
            lateral='—' if d['model_max_abs_lateral_m'] is None else f'{d["model_max_abs_lateral_m"]:.4f}'
            name='shadow' if d['mode']=='shadow' else f'{d["requested_model_seconds"]:.0f}초'
            body+=f'<tr><td>{link(Path(path)/"report.html",name)}</td><td>{d["model_observation_interval_seconds"]:.6f}초</td><td>{d["successful_model_transmissions"]}</td><td>{speeds}</td><td>{lateral}</td><td>{d["inference"]["p99_ms"]:.4f}</td><td>확인 · 누락 0</td></tr>'
        body+='</table><h2>후속 분석</h2><p>목표 10km/h 대비 약 1~1.7km/h의 속도 오차가 남았습니다. 검증·평가에는 오르막 표본이 없고 가속·무입력 표본도 적습니다. 재학습이나 한 바퀴 확대 전에 이 범위와 PID 내부 상태를 관측 특징으로 얼마나 설명할 수 있는지 분석할 근거가 확보됐습니다.</p>'
        body+='<p>최초 5초 시도는 인계 전 이력 부족으로 중단됐으며 성공 목록에 넣지 않았습니다. 수신 후 고정 대기로 관측을 건너뛰던 처리 타이밍을 수정했으며 기준값과 모델 가중치는 유지했습니다. 상세 기록은 '+link(Path('Docs/ModelTrainingDevelopmentLog.md'),'개발 기록')+'에 있습니다.</p>'
        (out/'report.html').write_text('<!doctype html><html lang="ko"><meta charset="utf-8"><title>폴포지션 통합 검증</title>'+STYLE+body+'</html>',encoding='utf-8')
        finish(out,result)
    except BaseException as e:finish(out,result,False,e);raise
    return out


if __name__=='__main__':
    p=argparse.ArgumentParser()
    for name in ['dataset','repeated','model','equivalent']:p.add_argument('--'+name,required=True)
    p.add_argument('--runs',nargs='+',required=True)
    print(build(**vars(p.parse_args())))
