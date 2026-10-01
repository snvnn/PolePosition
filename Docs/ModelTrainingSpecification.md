# 저속 PID 교사 모방 모델 규약 v0.1.0

## 범위와 실행 환경

BMW M4 GT3 / Brands Hatch, 목표 10km/h에서 기존 PID의 **요청 가속·브레이크**를 모방한다. Pure Pursuit 조향, 출발, 최종 정지, vJoy 감시는 기존 Rust 모듈이 담당한다. 사람 주행·강화학습·온라인 갱신·고속 주행·모델 한 바퀴 완주는 포함하지 않는다. 장치 장애 대응 보완은 별도 과제로 유지한다.

Python 3.11, PyTorch 2.8.0+cpu, NumPy 2.3.3과 Rust 1.95를 사용한다. 학습은 `.venv-training` 환경에서 실행한다. [의존성](../ModelTrainer/requirements.txt), [설치 버전 목록](../ModelTrainer/requirements.lock.txt), [설정](../ModelTrainer/config.toml).

```powershell
cargo build --release -p poleposition-policy -p poleposition-actuator
.\.venv-training\Scripts\python.exe -m ModelTrainer prepare --sources ModelTrainer/sources.toml --config ModelTrainer/config.toml
.\.venv-training\Scripts\python.exe -m ModelTrainer train --dataset <teacher-folder> --config ModelTrainer/config.toml
.\.venv-training\Scripts\python.exe -m ModelTrainer train --resume <training-folder>/last.pt
.\.venv-training\Scripts\python.exe -m Evaluator replay --model <training-folder> --dataset <teacher-folder> --split test
.\.venv-training\Scripts\python.exe -m ModelTrainer export --run <training-folder>
```

`--smoke`는 최대 3 epoch 동작 검사다. train을 자체 평가하므로 독립 검증과 실게임 배포 근거로 사용할 수 없다. `Evaluator --model`은 best 체크포인트가 들어 있는 학습 결과 폴더를 받는다.

실행별 실제 모델 담당 시간은 `Evaluator.live.inspect(run)`으로 재계산한다. 통합 집계는 `python -m Evaluator.integration --dataset <자료> --repeated <재생성자료> --model <실제사용모델> --equivalent <동일가중치재내보내기> --runs <shadow> <5초> <10초> <15초>`로 새 폴더에 생성한다. 이 집계는 모델 관측→중단 관측 구간과 정차 확인 시간을 구분한다.

## 교사 자료·출처·시간

[sources.toml](../ModelTrainer/sources.toml)에서 실행 전체를 train / validation / test로 배정한다. 원래 한 바퀴와 첫 추가 실행은 train, 두 번째·세 번째 추가 실행은 각각 validation/test다. 실행을 여러 집합에 나누거나 사람 데이터와 합치지 않는다.

기존 입력 시험 검증기를 재사용해 완료·해시·행 수·시간 순서·유한값·관측 출처를 검사한다. 실제 PID demo, 정상 정지·반환, 기록 누락 0, 예정된 `time_limit` 또는 `scenario_complete`만 채택한다. F8·장애·미시작 종료는 거절한다.

`controller.csv`의 수신 시각·관측 sequence·tracking_epoch를 `commands.csv`의 accepted/Running 요청과 연결한다. 해당 원관측의 sequence·epoch·observation_ns를 대조하고, 동일 관측·동일 정답 반복은 한 번만 쓴다. 동일 출처의 서로 다른 정답은 모두 제외한다. ready, 전진 기어(원시값 ≥2), 비피트, 관측·페이지 나이 <100ms만 사용한다.

정답은 PID 요청값이다. 첫 성공 전송의 실제 제한 후 값은 동일 명령 sequence로 별도 연결하며 전송이 없으면 null이다. ACC 페달은 **그 요청보다 앞선 원관측 값**이므로 응답 지연으로 해석하지 않는다. IPC 전달 시간과 요청/전송/관측 값 차이를 별도 집계하며 임의 시간 이동은 하지 않는다. 정확한 인과적 ACC 반영 지연 추정은 지원하지 않는다.

## 공통 19차원 특징

Rust `poleposition-policy::Features`를 오프라인 JSONL 처리와 실시간 worker가 공유한다. 순서는 아래와 같으며 `feature_names`에도 저장한다.

| 순번 | 특징 | 단위 |
| --- | --- | --- |
| 0–2 | 속도, 목표 속도, 목표−속도 | km/h |
| 3 | RPM | 회전/분 |
| 4 | 부호 있는 횡오차 | m |
| 5–6 | 방향 오차 sin, cos | 무차원 |
| 7 | 기준 주행선 앞뒤 5m로 계산한 경사 | % |
| 8–12 | 전방 0/10/20/40/80m 곡률 | 1/m |
| 13–18 | 100/200/500ms 전 속도·속도 오차 | km/h |

과거 목표 시각 이하의 가장 최근 실관측만 사용하고 오차 20ms를 허용한다. 미래 대체·보간은 없다. 100ms 이상 공백·epoch 변경·not ready에서 이력을 초기화하고 500ms 이력이 없는 행을 제외한다. 출발선 wrap은 경로 조회에서 순환 처리한다. 준비 시 원관측으로 먼저 계산한 뒤 epoch별 최소 50ms 간격으로 고른다. 실시간에서는 새 관측마다 같은 특징을 계산하며 중복 관측은 계산 결과를 재사용한다.

평균·모표준편차, 집단 가중치, 중앙값 기준 모델은 train만 사용한다. 상수 특징의 분모는 1이다. 절대 좌표·시각·랩 번호·현재 페달·교사 출력은 모델 특징에 들어가지 않는다.

## 학습·저장·평가

19→64→64→1, ReLU/ReLU/tanh, float32 CPU 순전파다. 출력 u≥0이면 throttle=0.20u, u<0이면 brake=−0.60u이며 다른 페달은 0이다. 정답은 throttle/0.20−brake/0.60이다. 조향과 중단 제동 1.0은 별도 처리한다.

기본값은 Smooth L1 beta=0.1, AdamW lr=0.001/weight_decay=0.0001, batch=256, seed=42, gradient norm clip=1.0, 최대 200 epoch, validation 개선 없는 20 epoch에서 조기 종료다. epoch마다 train의 각 행을 한 번 사용한다. 한 optimizer 갱신이 minibatch iteration이고, epoch당 횟수는 ceil(train 수/256)다. 종료 후 자동 재시작하지 않는다.

교사 pedal >0.005로 가속/제동/무입력 집단을 구분한다. train 내 존재하는 각 집단의 총 가중치를 같게 둔다. test는 체크포인트 선택에 쓰지 않는다. best는 validation 손실 최솟값이며 last에는 모델·optimizer·완료 epoch·best 상태·Python/NumPy/Torch 난수 상태를 저장한다. 재개는 새 폴더에서 수행한다. Ctrl+C 시 마지막 완료 epoch를 남기고 실행을 미완료로 표시한다.

| 출력 루트 | 주요 결과 |
| --- | --- |
| training_datasets/teacher-ID | manifest, sources-snapshot, config, 원관측 frame/feature JSONL, train/validation/test JSONL, normalizer, excluded.csv, validation |
| training_runs/train-ID | manifest, normalizer, best.pt, last.pt, history.csv, summary, report.html |
| evaluation_runs/SPLIT-ID | manifest, predictions.csv, metrics, report.html |
| trained_models/policy-ID | manifest, model.json, evaluations, parity-input/output JSONL, report.html |

원본·기존 폴더를 덮어쓰지 않는다. 출력 해시와 원본 입력 해시를 재검사한다. 작업용 sources/config는 내용 스냅샷을 결과에 넣고, 실행 시 코드·실행 파일 해시는 별도 provenance에 기록한다. 이후 정상적인 소스 수정 때문에 이미 생성한 결과의 원본 데이터가 변조됐다고 오판하지 않도록 구분했다.

validation과 test 각각 throttle MAE≤0.01/p95≤0.03, brake MAE≤0.02/p95≤0.05, 세 집단 존재, 집단 동일 가중 페달 MAE가 train 중앙값 기준보다 작아야 한다. 임계값 자동 완화는 없다. Python/Rust 최대 예측 차이 ≤1e-5도 필요하다. 불합격 모델 파일은 분석용으로 보존하지만 `eligible_for_live=false`로 입력을 막는다.

## 실시간 실행과 중단

```powershell
.\target\release\poleposition-actuator.exe policy --mode shadow --model <export-folder> --prepared <state-prepared> --profile <calibration.json>
.\target\release\poleposition-actuator.exe policy --mode drive --model-seconds 5 --previous-run <shadow-run> --model <export-folder> --prepared <state-prepared> --profile <calibration.json>
```

10초 시험은 성공한 5초 실행, 15초는 성공한 10초 실행을 `--previous-run`으로 지정한다. 선행 실행의 해시·정상 정지·누락 0·추론 p99≤1ms·모델 동일성·완료 시간을 검사한다. 각 실행은 사용자 F7로 시작하고 F8로 중지한다. 실제 Windows 사용자 세션에서 실행해야 ACC 전면과 키를 감지할 수 있다.

shadow는 PID만 전송한다. drive는 PID로 출발하고 목표=10, 실제 속도 8~12km/h, 유효 특징·ready가 1초 유지되면 모델로 가감속을 넘긴다. F7 이후 10초 내 인계 조건을 충족하지 못하면 중단한다. 모델 중 PID 가속 차이 >0.05 또는 제동 차이 >0.10이 100ms 연속이면 중단하며 자동 PID 주행 재개는 없다.

전체 30초/100m, 속도 20km/h, 조향 ±0.15, throttle≤0.20, 일반 brake≤0.60과 기존 데이터 만료·전면·추적·차량/트랙·보정·로깅 감시는 유지한다. 이력 손실·비유한 값·추론 실패도 중단한다. 가속 0/조향 중앙/제동 1을 거쳐 신선한 정차 확인 후 중립 전송·반환한다.

기존 actuator_runs에 policy.csv와 정책 설정·모델 해시·제어 주체 전환을 추가한다. policy.csv에는 19특징, 원관측 식별자, PID/모델 요청, 원출력, 특징·추론 비용, 모델 담당 시간이 있다. 실제 전송과 ACC 관측은 기존 CSV에서 출처로 연결한다. 큐 누락·저장 실패는 정상 시험으로 처리하지 않는다. timing은 특징·추론·IPC·드라이버·폴링·관측 나이를 구분하며 코어 비용을 전체 지연으로 표현하지 않는다.

## 한계와 검증 근거

동일 차량·트랙·교사 자료에 대한 작은 첫 모델이다. 검증/평가의 세 집단 표본 수와 경사별 범위를 확인해야 한다. 없는 지형의 성능이나 독립 일반화를 주장하지 않는다. 기준 경사는 실제 차량 pitch가 아니며 PID의 내부 적분 상태를 직접 관측하지도 않는다. 입력 모방 오차가 작아도 폐루프 안정성을 보장하지 않는다.

실제 수행 결과·실패·후속 과제는 [개발 기록](ModelTrainingDevelopmentLog.md), 전체 진행도는 [체크리스트](DevelopmentChecklist.md)에 기록한다.

최종 실제 결과는 [통합 보고서](../evaluation_runs/integration-1790832864652455200-4441a208/report.html)에 있다. 보고서·문서의 로컬 근거 링크와 HTML/SVG 구조를 검사했으며 브라우저의 모든 인쇄 배치를 검증한 것은 아니다.
