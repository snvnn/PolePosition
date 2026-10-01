# 폴포지션 기록 규약 v1

작성: 2026-09-29. 실제 저장 schema_version은 1이다.

## 모듈과 실행

루트 Cargo workspace에 TelemetryDataServer와 DrivingRecorder를 등록한다.
전자는 Rust 라이브러리와 10초 진단 실행 파일, 후자는 기록 실행 파일이다.
두 모듈은 직접 함수 호출로 연결하며 네트워크 서버나 차량 입력 기능은 없다.
Rust 1.95.0과 Cargo.lock을 사용한다. Python 검증기는 3.11 표준 라이브러리만 사용한다.

설정은 DrivingRecorder/recorder.toml이다. 기본 100Hz, stale 2초, reconnect 1초,
큐 4096건, 순간이동 의심 허용 여유 30m다. 경로는 실행 작업 디렉터리 기준이다.
잘못된 옵션·TOML·비유한 수·0 이하 주기·과도한 큐 크기는 실행 전 거절한다.

## 수집 인터페이스

`TelemetrySource::poll()`은 `Sample(Frame)`, `NoData`, `State`, `Error`를 반환한다.
State: live, disconnected, off, replay, paused, stale, unstable, unknown_status.
오류 값은 Error로 전달하며 기록기는 read_error 이벤트를 남긴다.
연결 후 Physics와 Graphics ID가 각각 최초 관측값에서 변경되어야 live로 인정한다.
고정된 좌표나 0km/h 자체를 연결 끊김으로 판단하지 않는다.

기존 매핑만 FILE_MAP_READ로 열며 RAII로 닫는다. 전체 prefix를 두 번 복사해
동일한 경우만 채택하고 세 번 실패하면 다음 poll로 넘긴다. 이것이 ACC 페이지 간
원자성을 보장하는 것은 아니다. 모든 host timestamp는 게임 생성 시각과 구분한다.

## ABI 근거

[ACC 공개 리더의 필드 순서](https://github.com/rrennoir/PyAccSharedMemory/blob/main/src/pyaccsharedmemory.py)를
대조하고 실제 게임에서 값을 확인했다. 32비트 정수·float와 UTF-16LE를 바이트에서 해석한다.
Physics 212바이트, Graphics 1588바이트, Static 앞부분 200바이트를 읽는다.

| 페이지 | 필드 및 바이트 오프셋 |
| --- | --- |
| Physics | packet 0, throttle 4, brake 8, fuel 12, gear 16, RPM 20, steering 24, speed 28, heading 208 |
| Graphics | status 4, session type 8, completed laps 132, lap time 140, in pit 160, sector 164, progress 248, active cars 252 |
| Graphics | coordinates 256, car IDs 976, player ID 1216, session index 1320, valid lap 1408, clock 1488, rain 1560 |
| Static | shared-memory version 0, game version 30, car 68, track 134 |

Static에서 읽은 버전 문자열은 SDK가 제공하는 원시값이며 설치된 게임 패치 버전이라고
단정하지 않는다. ABI 자동 호환 변환은 없다. 오프셋·실제 필드 의미가 바뀌면 재검증한다.

## 파일 형식

출력: `recordings/<Unix나노초-PID-충돌방지번호>/`.
원자적 디렉터리 생성 및 create_new로 덮어쓰기를 차단한다.

- metadata.json: 설정, 사용자 선언 조건, 단위, schema와 소프트웨어 버전. 정상 종료 시
  최초 자동 식별·조건을 추가한 임시 파일을 rename하여 교체한다. 진행 중에는 자동값이
  events.csv에 있으며, 종료 실패 시 metadata의 자동값이 미완성일 수 있다.
- telemetry.csv: 아래 고정 열 순서로 원본 상태를 기록한다. Rust CSV 인코더로 escaping한다.
- events.csv: elapsed_ns, segment_id, kind, detail. detail은 CSV 문자열이며 identity는 JSON이다.
- summary.json: 수신·저장·드롭 수, 이벤트 수, 구간 수, 종료 상태, timing 통계.
  파일이 없거나 파싱 불가하거나 normal_shutdown=false면 미완료다.
- validation-*.json: Python 검사 결과. 원본 네 파일을 수정하지 않고 새 파일만 생성한다.

### 샘플 열

| 그룹 | 필드 |
| --- | --- |
| 순서·구간 | sequence, segment_id, queue_drops |
| 시간(ns) | received_unix_ns, elapsed_ns, interval_ns, physics_changed_ns, graphics_changed_ns |
| 패킷 | physics_packet_id, graphics_packet_id |
| 상태·조작 | speed_kmh, rpm, gear_raw, throttle, brake, steering_raw |
| 위치·방향 | position_x, position_y, position_z, heading_rad |
| 랩·세션 | completed_laps, lap_time_ms, progress, sector, lap_valid, session_type, session_index, in_pit |
| 조건 | fuel_litres, clock_seconds, rain_intensity |

정확한 열 순서는 writer의 SAMPLE_FIELDS 및 검증기의 REQUIRED로 검사한다.
sequence는 저장 성공 여부와 관계없이 수신할 때 증가한다. 따라서 누락을 복원할 수 있다.
interval_ns는 직전 수신 샘플과의 간격으로 저장된 직전 행과 다를 수 있다.
elapsed_ns는 프로세스 기록 시작 후 monotonic 시간으로 대기·일시정지도 포함한다.
UTC가 역행하더라도 monotonic 시간은 따로 검사한다. changed_ns는 호스트가 페이지 ID의
변화를 발견한 시각으로 실제 게임 데이터 나이의 정확한 측정값이 아니다.

위치는 ACC 원시 XYZ 미터, 속도는 km/h, heading은 원시 라디안, 페달은 0..1이다.
조향은 원시값을 유지하며 좌/우 부호와 바퀴 각도 변환은 제어 단계에서 별도 보정한다.
gear_raw: 0=후진, 1=중립, 2=1단. progress: 0..1. clock_seconds: 게임 하루의 초.
rain_intensity 0은 비 없음. 누락 조건을 0으로 만들지 않으며 수동 설정 미확인은 unknown으로 둔다.

## 현재 시험 조건

자동 관측: BMW M4 GT3, Brands Hatch, session_type=3(핫랩), clock_seconds=57600(16:00),
rain_intensity=0. 최초 5초 저장 시험의 연료는 10L였다.
사용자 선언: 차량·트랙·시각·기상 유지, 자동 변속, 유선 Xbox 게임패드, 다른 셋업 변경 없음.
연료·타이어 상태는 핫랩 피니시 통과 때 리셋될 수 있으므로 주행 중 고정값으로 간주하지 않는다.
타이어·그립 상세 값은 이번 schema에 없으며 검증했다고 표시하지 않는다.

## 경계와 오류 처리

연결 중단·재연결, 차량/트랙/세션 변경, 랩 수 감소, 비정상 랩 수 도약은 새 segment다.
위치 이동이 `max(이전속도, 현재속도) × dt × 2 + 30m`를 넘으면 suspected_teleport로 분리한다.
이는 물리적 사실 판정이 아닌 설정 가능한 휴리스틱이다.

랩 수가 1 증가하고 이전 progress>0.8 또는 현재 progress<0.2일 때 lap_boundary를 남긴다.
출발선 근처에서 타이머가 먼저 초기화되면 1초간 랩 수 갱신을 기다린다. 실제 핫랩 시험에서
타이머가 랩 수보다 약 20ms 먼저 바뀌어 추가한 처리다. 기한 안에 확인되면 구간을 유지한다.
타이머 초기화 1초 이내에 진행률이 0.8 초과에서 0.2 미만으로 순환하면 랩 수가 증가하지
않더라도 경계로 인정한다. 실제 무효 핫랩에서 완료 랩 수가 유지되는 현상을 반영했다.
두 근거가 같은 통과를 중복 계산하지 않도록 경계 사이 최소 5초를 적용한다.
확인되지 않으면 unconfirmed_lap_timer_reset으로 분리한다. 단순 progress wrap만으로 완주를 선언하지 않는다.

저장 큐는 try_send를 사용한다. 행·이벤트 누락은 별도로 합산하며 검증기의 목표 통과를 막는다.
파일 오류는 실패 종료하고 가능한 경우 요약을 남긴다. 수동 메모는 별도 stdin 스레드에서 받는다.
Ctrl+C, /stop, 시간 제한 종료는 송신 채널을 닫고 저장 스레드를 join하여 남은 큐를 저장한다.
정상 flush 후 sync_all한다. 강제 종료·전원 중단에 대해 모든 마지막 데이터를 보장하지 않는다.

## 통계와 검증 판정

read_timing은 poll 호출 비용이고 poll_timing은 호출 시작 간격이다. 후자는 대기·게임 상태와
OS 스케줄링 영향을 포함한다. 전체 입력 반영 지연은 아니다. p95/p99는 10µs 구간의 히스토그램으로
추정하고 1초에서 상한 처리한다. 평균·최댓값은 상한 없이 집계한다.

format_pass는 구조·행·이벤트·집계 일치 검사다. 빈 세션도 형식상 정상일 수 있다.
first_goal_recording_pass에는 온전한 랩, 실제 샘플, 형식 통과, 행·이벤트 드롭 0이 필요하다.
온전한 랩은 같은 segment의 두 연속 lap_boundary 사이에서만 센다. valid_complete_laps는
그 구간의 lap_valid를 별도 집계한다. 무효 랩을 유효한 학습 랩이라고 보고하지 않는다.
기록 통과와 미실행→재실행 등 전체 연결 시험 통과는 별개다.
