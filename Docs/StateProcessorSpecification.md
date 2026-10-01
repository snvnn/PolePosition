# StateProcessor 0.1.0 규약

## 목적과 실행 환경

Rust 1.95의 `poleposition-state`가 경로 파일을 읽고 위치 투영·거리·횡오차·방향 오차·전방 곡률을 계산한다. 기록 재생과 Windows ACC 공유 메모리 진단은 같은 `Processor`를 사용한다. Python 3.11 표준 라이브러리는 입력 재검증·방향각 검증·보고서를 담당한다. 네트워크 서버나 차량 입력을 사용하지 않는다.

현재 경로는 BMW M4 GT3 / Brands Hatch의 학습 5번 랩에서 만든 3,886점·3,885.199156m 기준 주행선이다. 도로 중심선이나 도로 폭을 의미하지 않는다. 원본, TrackModel, 검토 판정과 기존 분리 목록은 읽기만 한다.

프로젝트 루트에서 실행한다. 의존성은 `Cargo.lock`, Rust는 `rust-toolchain.toml`로 고정한다. SHA-256은 `sha2 = 0.10.9`를 사용한다.

```powershell
python -m StateProcessor prepare --track-model track_models/track-1790677262927433400-796fb617 --review reviews/final-1790674957929734700-befda832 --config StateProcessor/config.toml
cargo run --release --locked -p poleposition-state -- replay --prepared state_runs/prepared-1790679300781499500-3e604d8c
cargo run --release --locked -p poleposition-state -- live --prepared state_runs/prepared-1790679300781499500-3e604d8c --duration-seconds 180
python -m StateProcessor report --run state_runs/replay-1790680057813285000-31944
```

새 준비 결과를 생성하면 첫 명령이 출력한 폴더를 후속 명령의 `--prepared`에 사용한다. 위의 고정 폴더는 실제 검증 결과다. `live`는 기본 180초, 지정한 시간 또는 Ctrl+C로 종료한다. 화면은 2Hz로 갱신한다. 진단을 시작해도 게임을 자동 실행하거나 조작하지 않는다.

## 입력 검증과 방향각

`prepare`는 [TrackModel 로더](../TrackModel/model.py)와 [확정 검토 검사](../TrackModel/inputs.py)를 재사용한다. 원본 네 파일, 데이터셋, 검사 결과, 판정과 사용 목록, 경로 산출물의 완료 상태·해시·일치를 재검사한다. 경로 생성에 사용한 검토 결과와 지정한 검토 결과도 일치해야 한다.

방향각은 사용 가능한 train 5랩만으로 고정한다. 같은 랩·구간 내에서 중심 앞뒤 100ms 이상 떨어진 가장 가까운 실제 관측점을 고른다. 창의 초과 폭은 `max_age_ms` 이내이며 내부 공백이 이 값을 넘으면 사용하지 않는다. 보간하지 않는다. 중심 속도 50km/h 이상, 앞뒤 이동 방향 차이 2도 이하, 채택 중심 시각 간격 200ms 이상이어야 한다.

`±heading + {0, π/2, π, -π/2}`의 8후보를 비교한다. 최소 200창, 절대 오차 중앙값 3도 이하, p95 10도 이하, 차순위 중앙값과 20도 이상 차이가 필요하다. p95는 정렬한 값의 nearest-rank다. 실패하면 준비 완료로 표시하지 않는다. 프로필에는 사용 train 랩, 각 중심의 sequence·시각, 후보 통계, 차량·트랙·공유 메모리 식별값을 저장한다. 실시간 재학습은 하지 않는다.

현재 공식 검증은 1,640창에서 `wrap(heading + π/2)`를 선택했다. 이동 방향과의 대응 검증이며 차량 자세의 절대 측정 정확도를 뜻하지 않는다. validation/test와 보류 랩으로 변환이나 임계값을 조정하지 않는다.

Rust 로더도 준비 결과와 필수 입력 목록·파일 해시를 확인하고, 경로의 스키마·열 순서·단위·좌표 규약·인덱스·엄격한 누적 거리·폐곡선 길이·유한값·단위 접선을 검사한다. 프로필의 선택 변환과 후보 통계도 대조한다. 실행 후 다시 해시를 확인한다. 해시는 우발적인 변경 검출용이며 서명된 신뢰 체계가 아니다.

## Rust 인터페이스

소스: [processor.rs](../StateProcessor/src/processor.rs), [model.rs](../StateProcessor/src/model.rs).

- `Processor::update(observation, now_ns) -> State`: 새 관측을 계산한다.
- `Processor::tick(now_ns) -> Option<State>`: 새 관측이 없어도 만료 또는 시각 역전을 알린다. 변화가 없으면 `None`이다.
- `Processor::reset(reason)`: 연속성을 해제한다. 이후 새 구간에서 3회 확인한다.
- `reset_state(reason, now_ns)`: reset과 함께 진단용 unavailable 행을 만든다.

`Observation`은 sequence·원본 segment·호스트 monotonic ns, 페이지별 변경 시각과 패킷 ID, XYZ·속도·ACC heading, identity, session 종류/번호·완료 랩·lap_valid·in_pit·game_status를 받는다. 기록 CSV에 없는 game_status는 live로 간주하고 기록 이벤트를 통해 reset을 재현한다. 라이브 입력은 TelemetryDataServer가 live 샘플만 제공하고 다른 상태는 이벤트로 전달한다. 외부 호출자는 검증된 Model과 Config를 전달하고 같은 monotonic 기준의 시각을 사용해야 한다.

| status | 의미 | 투영 필드 |
| --- | --- | --- |
| acquiring | 연속 새 관측 확인 중 | 진단 후보만 존재; 유효한 추적 아님 |
| ready | 이번 관측의 투영과 연속성 확인 | 존재 |
| waiting | 동일 패킷 반복 | 비어 있음 |
| invalid | 오래됨·모호성·불일치·도약 등 | 비어 있음 |
| unavailable | 일시정지·off·연결 중단 등 | 비어 있음 |

`reason`과 상태 전환 이벤트에 근거를 남긴다. `lap_valid=false`나 피트 여부는 원시 게임 정보이며 그 자체로 계산을 실패시키지 않는다. ready는 차량 제어 허가나 무사고 판정이 아니다.

## 계산과 연속성

- 마지막→첫 점을 포함하는 모든 선분에 X–Z 직교 투영한다. 선분 비율은 [0,1]로 제한하고 Y는 같은 비율로 보간한다.
- `s_m`은 선분 시작 누적 거리 + 비율×평면 선분 길이이며 [0,L)로 순환한다. ACC progress와 같다고 가정하지 않는다.
- 저장 접선을 선형 보간 후 정규화한다. `lateral_error_m = tx*(z-qz) - tz*(x-qx)`이다.
- 경로 방향은 `atan2(tz,tx)`, 차량 방향은 고정 프로필 변환이다. 방향 오차는 `wrap(차량−경로)`로 [-π,π] 범위다.
- 곡률을 거리로 선형 보간하며 전방 0·10·20·40·80m 조회는 출발선을 순환한다.
- 최초 획득에서 전역 최소 거리를 고른다. 경로상 순환 거리가 20m 이상 떨어진 다른 후보가 1m 이내로 비슷하면 ambiguous다.
- 추적 중 허용 이동량은 `2×max(이전,현재 속도)/3.6×dt + 5m`이다. 그 범위 내 후보를 우선한다. 전역 후보가 1m 넘게 더 가깝고 연속성 후보와 충돌하면 추적을 해제한다.
- 위치 도약, 100ms 이상 관측 공백, 페이지/관측 나이 100ms 이상, 시간 역전, identity 불일치를 무효화한다. 같은 패킷은 획득 확인 횟수를 늘리거나 수신 시각을 새롭게 하지 않는다.
- 출발선에서 최소 순환 거리 차이를 누적하며 후진은 음수다. `accumulated_m`은 각 `tracking_epoch`의 첫 획득 후보에서 0으로 시작한다. 첫 후보 시점과 ready 시점은 다를 수 있다. 중단된 epoch 사이를 이어 붙이지 않는다.
- 정상 랩 증가와 핫랩 차량 상태 초기화는 reset 사유가 아니다. 라이브 구간은 기존 [Tracker](../DrivingRecorder/src/session.rs)를 재사용한다. 재연결·세션 변경·랩 카운터 감소는 새 구간이다.

모든 기준은 [config.toml](../StateProcessor/config.toml)에 있다. 경로 거리 20m는 투영 적용 범위이며 도로 폭 또는 이탈 판정 기준이 아니다. 임계값을 검사 결과에 맞춰 자동 변경하지 않는다.

## 파일 규약과 재현성

출력 루트 기본값은 실행 디렉터리 기준 `state_runs/`다. 실행마다 배타적으로 새 폴더를 만든다.

| 종류 | 파일 | 내용 |
| --- | --- | --- |
| prepared | manifest.json | 스키마·버전·완료·경로·입력 및 출력 SHA-256 |
| prepared | config.json | 적용된 전체 설정 |
| prepared | heading_profile.json | train 기반 방향 변환·후보 통계·사용 중심 |
| prepared | replay.json | 원본 경로·사용 가능 9랩의 원래 집합·시간/구간 범위·보류/제외 |
| replay/live | manifest.json | 실행 종류·준비 결과 해시·설정·열·좌표/단위·완료·출력 해시 |
| replay/live | states.csv | 매 관측 계산 결과와 만료/reset 진단 행 |
| replay/live | events.csv | 시각·종류·상태 전환 또는 원본 이벤트 |
| replay/live | summary.json | 정상 종료·기록 유효성·누락·집계·시간 통계 |
| replay/live | report.html | Rust 종료 시 기본 요약, Python report 실행 시 상세 HTML/SVG |

`states.csv`는 원본 sequence/segment, observation_ns/now_ns/tracking_epoch, status/reason, 관측·physics·graphics 나이(ns), 원시 XYZ·속도·lap_valid·in_pit, 선분 번호/비율, 투영 XYZ·평면 거리, s_m·accumulated_m·횡오차, 차량/경로/오차 방향각(rad), 현재 곡률(1/m), 전방 곡률 배열의 JSON 열을 갖는다. 전방 배열의 순서는 manifest 설정의 `curvature_offsets_m`와 같다. 빈 값은 계산 불가를 뜻하며 0으로 대체하지 않는다. 정확한 열 순서는 manifest의 `fields`다.

원본 재생은 전체 182,992행과 원본 이벤트를 시각순으로 처리한다. 보류·무효·부분 구간도 진단하지만 후속 사용 목록에 편입하지 않는다. 사용 가능한 9랩은 고정 train 5 / validation 1 / test 3으로 보고한다. 이벤트나 다음 샘플 시각에 tick을 수행하며 저장되지 않은 폴링 시각을 재구성했다고 주장하지 않는다.

재생은 벽시계 대기 없이 실행하고 저장 큐에 여유가 생길 때까지 대기하여 행을 버리지 않는다. 라이브는 제한 큐에 즉시 전달하고 포화 시 누락을 센다. 파일 저장과 2Hz 표시는 별도 스레드다. 정상 종료는 큐를 비우고 flush/sync한다. 저장 실패, 큐 누락, 입력 변경은 실패 종료하며 완료로 표시하지 않는다. summary가 없거나 manifest.complete가 false면 불완전 실행이다. 상세 report는 원본 및 출력 해시, 시간 순서, 필드, 행/이벤트 수, 잘못된 상태의 잔존 투영값을 다시 검사한다.

상태 CSV와 이벤트 CSV는 같은 준비 입력에 대해 바이트 단위로 재현된다. 실행 ID·생성 시각·실제 처리 시간은 재현성 비교에서 제외한다. 새 기록·경로·검토 결과가 생기면 prepare를 다시 실행한다.

## 성능, 검증과 한계

`core_timing`: update 호출만의 벽시계 비용. `poll_timing`: 실제 live 폴링 시작 사이 간격. `observation_age`: 관측에서 계산 진입까지 호스트 시각 차이. 페이지 변경 나이는 CSV에 별도로 있다. 호스트 수신 이전의 게임 내부 처리 지연은 측정하지 않는다. 재생의 관측 나이 0은 원본 관측 시각에 계산하기 때문이며 실제 시스템 지연이 0이라는 뜻이 아니다.

기존 recorder의 Timing을 재사용한다. p95/p99는 10µs 올림 버킷, 최대 버킷 1초이며 최대값 자체는 실제값이다. release 코어 p99 목표는 1ms 이하이고, Windows 스케줄링에 의한 최대값 초과를 숨기지 않는다.

```powershell
cargo build --locked -p poleposition-state
cargo test --locked --workspace
cargo clippy --locked --workspace --all-targets -- -D warnings
python -m unittest DatasetBuilder.test_builder DatasetBuilder.test_report DatasetBuilder.test_review TrackModel.test_model StateProcessor.test_state -v
```

Rust debug CLI가 없으면 Python의 교차 언어 실행 테스트는 skip되므로 먼저 build한다. 최종 실측과 시험별 범위는 [개발 기록](StateProcessorDevelopmentLog.md)에 있다. 실제 리플레이 모드 전환과 의도적인 데이터 페이지 동결은 이번 실게임에서 유발하지 않았으며 합성 테스트 범위다.

최종 학습 상태, 조작값 시간 정렬, 정규화, 목표 속도, 차량 제어와 모델 학습은 미구현이다. 원시 조향 부호나 바퀴 각도 변환도 이 모듈의 결과에서 추론하지 않는다.
