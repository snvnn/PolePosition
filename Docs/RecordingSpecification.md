# 폴포지션 기록 규약 v1

작성: 2026-09-29. 실제 저장 schema_version은 1이다.

관리 실행과 원시 게임패드 형식 갱신: 2026-10-18. 기본 텔레메트리 schema_version 1을 유지하며 게임패드 기록은 아래 추가 파일과 metadata로 식별한다.

## 모듈과 실행

수집 경로는 루트 Cargo workspace의 TelemetryDataServer와 DrivingRecorder를 사용한다.
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

## 관리 실행과 게임패드 선택

관리 실행은 `--managed --config FILE --duration SECONDS`를 받는다. GUI와 stdin/stdout의 UTF-8 JSON Lines 생존 신호·중지를 주고받으며 [관리 채널](TrainingControllerSpecification.md#관리-채널-v1)을 따른다. 게임패드는 `--gamepad-profile PROFILE --gamepad-slot 0..3`를 함께 지정하고 관리 실행을 요구한다. GUI `게임패드 교사 기록`이 이 인자를 구성한다.

프로필을 사용하는 수집은 100Hz만 허용한다. 선택 슬롯이 유일한 연결 패드여야 하며 capability를 확인한다. F7 전 왼쪽 스틱 절댓값 8192 초과로 장치를 선택하고, 최신 관측과 ACC 전면 상태를 확인해 F7을 기다린다. F7 전에는 결과 폴더를 만들지 않으며 취소도 결과 없음으로 종료할 수 있다. 관리 시간 제한에는 준비 대기가 포함된다.

`--gamepad-diagnostic`은 프로필을 요구하되 실제·가상 응답 검증 자격을 생략하는 진단 전용이다. 해당 기록은 metadata의 `diagnostic_only=true`와 summary의 `gamepad_training_eligible=false`로 남으며 학습에서 거절한다. 이 옵션은 GUI의 일반 교사 기록에 추가되지 않는다.

## 게임패드 추가 파일과 시계

| 파일 또는 필드 | 내용 |
| --- | --- |
| inputs.csv | XInput 원시 입력과 정규화 축, 텔레메트리와 같은 Instant 원점의 읽기 시각 |
| controls.snapshot.json, assists.snapshot.json | 수집 시작 시 프로필에 연결된 ACC 설정의 원본 바이트 |
| metadata.input_semantics | gamepad_v1 |
| metadata.input_profile | 설정·DLL·드라이버·검증 근거와 해시가 포함된 프로필 |
| metadata.gamepad_slot, physical_gamepad_capabilities | 선택한 슬롯과 최초 장치 식별 자료 |
| metadata.input_clock, diagnostic_only | 입력 시계 정의와 진단 여부 |
| summary.input_samples | 입력 표본 수. telemetry 표본 수와 별도 |
| summary.gamepad_training_eligible | 비진단·오류 없음·행 및 이벤트 누락 0의 수집 단계 자격 |

`inputs.csv`의 열 순서는 [InputRow](../GamepadIO/src/lib.rs)의 직렬화 순서를 따른다.

```text
sequence,segment_id,read_start_ns,read_end_ns,slot,connected,packet,buttons,lx,ly,rx,ry,lt,rt,stick_x,stick_y,right_x,right_y,throttle,brake,normalization_version
```

sequence와 두 읽기 시각은 증가해야 하고 `read_start_ns <= read_end_ns`여야 한다. `slot`은 0~3, `connected`는 true/false, packet은 XInput 패킷 번호다. 스틱 정수는 -32768~32767, 트리거는 0~255, 버튼은 16비트 마스크다. 정규화 버전 1은 음수 스틱 /32768, 양수 스틱 /32767, 트리거 /255를 뜻한다. packet이 유지돼도 읽기 행은 유효하며 새로운 조작이 생겼다는 뜻은 아니다.

입력과 telemetry의 `elapsed_ns`는 같은 호스트 단조 원점의 ns다. 입력의 읽기 종료 시각은 차량에 적용된 시각이 아니다. telemetry와 입력은 각각 수집되므로 행 수와 같은 sequence끼리 직접 결합하지 않는다. `segment_id`와 읽기 구간으로 과거 입력·관측 이후 정답을 연결하는 규칙은 [학습 규약](GamepadTrainingSpecification.md#원시-자료와-정답의-시간-연결)에 있다.

## 게임패드 자격과 랩 경계의 추가 검사

수집 중 장치 이탈·장치 집합 변경·capability 변경·설정 변경·저장 오류가 발생하면 오류를 남기며 자동 학습 자격을 해제한다. summary의 자격 true는 수집 단계의 조건일 뿐 최종 모델 학습 채택이 아니다.

기본 `validate_recording.py`의 `format_pass`는 metadata·summary·telemetry·events 및 랩 경계를 검사한다. `inputs.csv` 정규화와 전체 행, 프로필 스냅샷 해시, 관측과 정답의 인과적 연결까지 검사하지는 않는다. 이 부분은 [gamepad.read_inputs와 prepare_recording](../TrainingEnvironment/gamepad.py), 교정 실행은 [corrections.load_corrections](../TrainingEnvironment/corrections.py)가 추가 확인한다.

공유 [FinishLineTracker](../DrivingRecorder/laps.py)는 랩 카운터 +1과 출발선 근방, 또는 카운터 갱신보다 앞선 핫랩 타이머 리셋과 1초 내 progress wrap을 사용한다. 경계 간격은 최소 5초이며 segment 변경 시 상태를 초기화한다. 유효 완전 랩은 같은 segment의 연속한 두 경계 사이에 `lap_valid=false`가 없어야 한다. 랩 학습은 각 랩에서 특징 이력을 새로 만들고, 세션 학습의 무효 구간 사용과 보관용 선별은 [학습 규약](GamepadTrainingSpecification.md)과 [자료 보관](DataArchiveGuide.md)에서 별도로 정의한다.
