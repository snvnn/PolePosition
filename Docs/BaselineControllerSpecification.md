# 저속 경로 추종 데모 0.1.0

2026-10-01. 목표는 기존 TrackModel의 사람 주행선을 잠시 따라가는 데모다. 자동 한 바퀴 완주, 도로 경계·장애물·접촉 판정, 레이싱 속도 제어는 범위 밖이다.

## 실행

```powershell
cargo run --release -p poleposition-baseline -- verify --prepared state_runs/prepared-1790679300781499500-3e604d8c
python -m BaselineController steering-audit --run actuator_runs/crawl-1790823341496811500-23964
cargo run --release -p poleposition-actuator -- demo --prepared state_runs/prepared-1790679300781499500-3e604d8c --controller-config BaselineController/config.toml --profile input_profiles/calibration-1790823244181544300-71cd97f2/calibration.json
```

앞 두 명령은 장치 출력을 하지 않는다. `verify`는 원본·경로 검증, 실제 관측에 대한 시작 조건 검사, 기준 경로 조향 요구량과 단순 가상 차량 계산을 `baseline_runs/<고유 ID>/`에 저장한다. 기록 재생의 속도를 바꿔 실제 응답인 것처럼 평가하지 않는다. 가상 차량은 실제 ACC 차량 모델이 아니다.

`demo`는 기존 장치 감시기에서 실행하며 F7 전에는 주행 명령을 적용하지 않는다. Xbox 패드는 분리하고, BMW M4 GT3 / Brands Hatch의 휠 모드에서 앞이 빈 트랙에 정차한다. 피트 안에서는 시작하지 않는다. F7 후 1초간 제동하며, N이면 사용자가 Space로 1단을 선택한다. F8 중단, ACC 전면 이탈·일시정지·데이터 만료·추적 상실·출처 변경·기록 실패에 대한 기존 중단 정책을 유지한다.

## 계산

- [Rust 계산 코어](../BaselineController/src/lib.rs), [설정](../BaselineController/config.toml).
- 순서가 보존된 폐곡선에서 `s + lookahead`를 선형 보간한다. 기본 lookahead 8m. 차량 기준 전방 투영이 양수가 아니면 중단한다.
- X–Z의 차량 방향을 `h`, 목표점 차이를 `(dx,dz)`라 할 때 요청 곡률은 `2 × (cos(h)×dz − sin(h)×dx)/(dx²+dz²)`다. [Coulter의 Pure Pursuit 보고서](https://publications.ri.cmu.edu/implementation-of-the-pure-pursuit-path-tracking-algorithm)의 원호 기하를 사용한다. 목표점은 경로 거리로 선택하고 분모는 실제 목표점 직선거리의 제곱이다.
- 초기 조향은 `곡률 × 7.3m`를 정규화 가상 축에 대응한다. 부호는 기존 저속 실험의 성공한 전송값과 X–Z 이동에서 확인했다. 이 작은 입력 범위의 근삿값은 실제 바퀴 각도·ACC 원시 조향의 변환식이 아니다. 자동으로 계수를 갱신하지 않는다.
- 목표 속도는 10km/h, 0에서 초당 2km/h로 증가한다. 속도 PID는 미분 측정값 필터, 조건부 적분·적분 제한을 사용한다. 감속 때 양의 적분을 버리고 제동을 우선한다. 같은 관측을 여러 번 수신해도 적분을 반복하지 않는다. 가속·제동을 동시에 요청하지 않는다.
- 장치 소유는 기존 감시 프로세스, 제어 계산은 별도 명령 생성 프로세스가 담당한다. 실제 계산 입력은 StateProcessor의 변환된 방향·투영·거리이며 ACC progress를 경로 거리로 대체하지 않는다.

## 제한과 종료

| 항목 | 기본값 |
| --- | --- |
| 최대 속도 / 가속 / 조향 | 20km/h / 0.15 / ±0.15 |
| 정상 PID 최대 제동 / 중단 제동 | 0.60 / 1.0 |
| 시간 / 이동 거리 제한 | F7 후 30초 / 추적 누적 거리 100m |
| 시작 투영 거리 / 시작 방향 오차 | 2m / 15도 이내 |
| 실행 투영 거리 / 실행 방향 오차 | 3m / 30도 이내 |
| 관측 만료 | 100ms |

조향 요구량이 한계를 넘으면 값을 숨겨 계속 달리는 대신 중단 사유 `steering_envelope`를 남긴다. 후진 진행 0.5m 초과, 주행 시작 후 전진 기어 상실, 시간·sequence 역전, 추적 구간 변경도 중단한다. 한 번 중단된 제어기는 새 실행 없이 재개하지 않는다. 시작 조건 검사도 감시기에서 수행한다. `lap_valid=false`는 단독 중단 조건이 아니다.

**장치 장애 한계:** [실제 vJoy 장애 시험](../input_profiles/stop-fault-check-20261001-1790824036572159900-5db76b4d/verification.json)에서 중단 명령 전송 실패와 ACC 페달 50%/50%를 관측했다. 이 문제가 해결됐다고 표시하지 않는다. 드라이버가 사용 불가능하면 F8도 축을 바꿀 수 없으므로 사용자가 Esc로 게임을 중단한다. 데모는 기존 장치 장애 검증의 실패를 덮어쓰지 않는다.

## 결과와 판정

`actuator_runs/demo-<ID>/`에 기존 manifest·commands·telemetry·events·summary·report·validation 및 `controller.csv`를 저장한다. 제한된 기존 저장 큐와 별도 저장 스레드를 사용한다. 큐 누락은 실행 실패다.

- `controller.csv`: 입력 관측 sequence·시각·추적 구간, 목표 속도·목표점, 요청 곡률·조향, 판정과 중단·완료 여부. telemetry의 같은 출처와 연결해 보고서에서 검증한다.
- telemetry 추가 열: `path_s_m`, `lateral_error_m`, `heading_error_rad`, `vehicle_heading_rad`, `projection_distance_m`.
- 보고서: 목표·실제 속도, 횡오차·방향 오차, 같은 축척의 기준 경로·관측 궤적·목표점. 기존 성공·실패 전송과 타이밍 구분을 유지한다.
- manifest `complete`는 파일 생성·검증과 정상 장치 반환 여부다. 주행 성공이나 자동 완주를 의미하지 않는다. F7을 누르지 않은 종료, 제어 중단, 실제 이동량과 오차를 별도로 확인해야 한다.
- 입력과 기존 기록·경로·분리는 변경하지 않는다. validation/test로 계수를 보정하지 않는다.

검증 근거와 미완료 사항은 [개발 기록](BaselineControllerDevelopmentLog.md)에 남긴다.

## 2026-10-01 추가: 한 바퀴 고저차 관측 모드

사용자 요청으로 [lap.toml](../BaselineController/lap.toml)을 분리했다. 짧은 데모의 기존 설정은 유지한다.

```powershell
cargo run --release -p poleposition-baseline -- verify --prepared state_runs/prepared-1790679300781499500-3e604d8c --config BaselineController/lap.toml
cargo run --release -p poleposition-actuator -- demo --prepared state_runs/prepared-1790679300781499500-3e604d8c --controller-config BaselineController/lap.toml --profile input_profiles/calibration-1790823244181544300-71cd97f2/calibration.json
```

- `single_lap=true`일 때 현재 출발 위치에서 같은 추적 구간의 누적 경로 거리가 모델 전체 길이(약 3,885.199m)에 도달하면 `lap_complete`로 중단·정지한다. 출발선을 처음 지났다는 사실만으로 끝내지 않는다. ACC 출발선부터 다음 출발선까지의 공식 랩타임과 구분한다.
- 사용자 요청으로 F7 이후 시간 상한은 **60분**, F7 전 준비 대기는 **10분**이다. 시간 초과는 완주가 아니다. 기본 10km/h라면 이동만 약 23분이지만 경사·출발·속도 변화에 따라 달라진다.
- 한 바퀴 설정의 최대 조향은 **±0.35**, 가속은 **0.20**, 최대 속도는 기존 **20km/h**다. 기준 경로 요구 조향 최대 0.2769를 확인하고 고정한 범위다. 설정을 주행 중 자동 확장하지 않는다. 기본 데모 ±0.15·30초 범위는 바꾸지 않았다.
- 목표 10km/h, PID 계수와 전방 목표점 8m는 기존 값을 유지한다. 이번에는 경사 기반 가감속 선행 보정을 넣지 않고 실제 반응을 관측한다. ±0.1에서 추정한 조향 계수를 더 큰 입력에도 적용하므로 실제 코너 반응은 별도 검증 대상이다.
- telemetry 추가 열: 연속 `accumulated_m`, `reference_elevation_m`, `reference_grade_pct`, ACC `completed_laps`, `lap_time_ms`, `progress`, `sector`. 관측 Y 좌표는 기존 `position_y`에 보존한다.
- 경사 추정은 기준 경로의 `100×(Y(s+5m)−Y(s−5m))/10m`다. 양수는 진행 방향 오르막, 음수는 내리막이며 앞뒤 5m 조회는 폐곡선에서 순환한다. 측량 도로 경사나 차량 pitch의 측정값이 아니다.
- controller 로그의 `distance_travelled_m`, `lap_fraction`은 한 바퀴 진행을 표시한다. `lap_fraction=1`은 모델 전체 길이이며 ACC progress와 다르다. 순방향 거리 누적과 추적 구간 연속성을 전제로 한다.
- 보고서와 `terrain.json`은 tracking 관측을 오르막(>2%)·평지 범위(±2%)·내리막(<−2%)으로 묶어 속도 오차와 관측 페달을 집계한다. 경사와 결과의 상관을 기술하며 경사를 유일한 원인으로 단정하지 않는다. 조기 중단 결과도 그대로 보존한다.
- summary의 `lap_completed`는 실제 `lap_complete`와 정상 기록·정지 확인·장치 반환을 함께 만족할 때만 true다. manifest의 파일 완료와 구분한다.
