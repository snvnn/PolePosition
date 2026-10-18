# 구현 및 검증 기록

현재 색인 갱신: 2026-10-18. 날짜별 수치와 통과는 당시 기록의 범위다. 같은 테스트를 여러 실행에서 보고한 경우 합산하지 않는다. 현재 사용법은 [문서 목차](README.md), 모델별 결과는 [실험 검토](GamepadExperimentReview.md)를 따른다.

## 검증 범위와 판정

| 종류 | 확인하는 것 | 별도로 남는 판정 |
| --- | --- | --- |
| 단위·통합·합성 자료 검사 | 계산·스키마·프로세스 관리·오류 경계 | 실제 드라이버·ACC 반응 |
| 기록 재생과 수치 대조 | 같은 관측의 특징·추론·정답 연결 | 해당 모델이 만든 실제 궤적과 성능 |
| 장치 응답·입력 분리 | 읽기/전송·슬롯·설정·해제 | 주행 동역학과 코너/랩 성능 |
| 짧은 실게임 시험 | 해당 조건의 시작·전달·정차·해제·종료 이유 | 고정 모델의 자율 유효 완주와 반복 신뢰도 |
| 고정 모델 랩 평가 | 사람 개입 없는 출발선 간 유효 랩·동일 모델 조건 | 현재는 실제 3랩 성능 미완료 |

`format_pass`, 입력 전달 통과, `summary.passed`, `stop_confirmed`, `released`, `correction_eligible`는 서로 다른 조건이다. 특히 장치 해제가 성공해도 정차 전에 추적이 바뀐 실행은 전체 성공으로 바꾸지 않는다. validation 반복 선택은 최종 test가 아니며 체크리스트의 기능 구현과 성능 완료를 구분한다.

## 이후 검증 색인

| 날짜 | 범위와 결과 | 근거 |
| --- | --- | --- |
| 2026-09-29 | 초기 차량 입력 개발 회귀 Rust 74·Python 94 통과. 현재 코드 전체 결과로 재사용하지 않음 | [차량 입력 개발 기록](VehicleInputDevelopmentLog.md) |
| 2026-09-30 | vJoy 재설치 후 Xbox XInput 두 API 각 300/300 및 사용자 ACC 반응 확인. 드라이버 장애 대응 완료와 별개 | [복구 집계](../input_profiles/vjoy-reinstall-20260930/final-verification.json), [Xbox 조사](XboxUsbInputInvestigation.md) |
| 2026-10-01 | vJoy 실제 전면 이탈 중지 통과. 실제 장치 장애는 소유권 상실·중단 전송 실패·페달 50%/50%로 미해결 | [장애 집계](../input_profiles/stop-fault-check-20261001-1790824036572159900-5db76b4d/verification.json) |
| 2026-10-01 | 저속 기준 제어 한 바퀴 완료, 속도 정밀도·3회 반복은 별도. PID 모방 전체 연결 확인 | [기준 제어 로그](BaselineControllerDevelopmentLog.md), [PID 학습 로그](ModelTrainingDevelopmentLog.md) |
| 2026-10-03 | v2 자료/사전학습 Python 44, GUI·기존 회귀 51, Rust 121 통과·장치/전역 핫키 2개 제외. 당시 각 묶음의 수치 | [컨트롤러 로그](TrainingControllerDevelopmentLog.md)의 GUI 기반 v2 자료 준비 |
| 2026-10-03 | 실제 30초 v2 비교: PID가 명령, 정책은 기록 전용, 정상 정차·해제. 입력 인과성 및 추론 지연 확인 | [실제 실행](../actuator_runs/demo-1791011251595117500-11944/summary.json), [컨트롤러 로그](TrainingControllerDevelopmentLog.md) |
| 2026-10-03 | 직접 정책 제어 5·10·30초, 60초 학습 회차 2회·replay 5,298·갱신 1,324·체크포인트 재개 확인. episode_timeout, 완주 아님 | [SAC 집계](../reinforcement_runs/sac-integration-verification-1791014592961144200-b5e0c420/summary.json) |
| 2026-10-08 | HidHide 재부팅 후 실제 0/가상 1 슬롯 분리·해제 통과, GUI 관리 파이프의 CLI 대기 수정과 controls 외부 장치 순서 허용 검사 | [동시 검사](../input_profiles/gamepad/intervention-bench-20261008-slot-discovery.json), [관리 회귀](../input_profiles/gamepad/managed-isolation-regression.json), [설정 회귀](../input_profiles/gamepad/controls-order-regression.json) |
| 2026-10-09 | 자동 변속 N 관측의 특징 유지, 실패 기록 재생 25개 중립 관측 확인. 실제 반복은 별도 | [중립 변속 재생](../input_profiles/gamepad/neutral-shift-replay-1791471504363645600/summary.json), [컨트롤러 로그](TrainingControllerDevelopmentLog.md) |
| 2026-10-11 | RB 토글·전체/조향 마스크·기어 전달·새 교사/특징 제외. 최종 출발선/LPF 변경의 당시 검사: gamepad 9·feature 5·actuator 47·Python 10, 역사 replay 1개 ignored | [컨트롤러 로그](TrainingControllerDevelopmentLog.md) |
| 2026-10-11 | 실제 LPF 5초 양쪽 통과, 10·30초 전체 실패. 랩/혼합 모델 5초 통과·15초 실패, 제동 특징 제거 후보는 오프라인 한계 유지 | [실험 검토](GamepadExperimentReview.md)와 연결된 실행 summary |
| 2026-10-17 | 선별 세션 모델의 5초 통과·15/30초 정차 전 추적 변경. 15초 전달 검사는 통과했지만 전체 passed=false | [세션 모델 검토](../input_profiles/gamepad/session-model-15s-review.json), [실험 검토](GamepadExperimentReview.md) |
| 2026-10-17 | 증강 경계 수정 후 당시 검사 16개, 프리셋 12개·원자료/출력 해시·같은 정규화/초기 가중치/갱신 예산 확인. live_tests_executed=false | [증강 검증](../reinforcement_runs/augmentation-comparison-1792233044576944100-923b5038/verification.json), [겹침 후검사](../reinforcement_runs/augmentation-comparison-1792233044576944100-923b5038/post-curation-check.json) |

## 재현할 검사 명령

아래는 실행 방법이며 새 통과 실적이 아니다. 최초 의존성 다운로드 후 오프라인 모드를 사용할 수 있다. 장치·전역 핫키가 필요한 검사는 해당 환경에서 따로 수행하고 생략 이유를 남긴다.

```powershell
cargo test --locked --offline --workspace
cargo fmt --all -- --check
cargo clippy --locked --offline --all-targets -- -D warnings
.\.venv-training\Scripts\python.exe -m unittest discover -s DrivingRecorder -p 'test_*.py' -v
.\.venv-training\Scripts\python.exe -m unittest GamepadIO.test_isolation GamepadIO.test_validation GamepadIO.test_reports TrainingEnvironment.test_gamepad TrainingEnvironment.test_corrections TrainingEnvironment.test_lap_gamepad TrainingEnvironment.test_augmentation -v
.\.venv-controller\Scripts\python.exe -m unittest TrainingEnvironment.test_checkpoint TrainingEnvironment.test_online TrainingEnvironment.test_policy -v
```

## 문서 확인 2026년 10월 18일

기존 문서와 현재 코드·TOML·프리셋·저장된 summary/manifest를 대조해 안내·규약·결과 색인을 보완했다. 실제 시험 날짜와 문서 정리 날짜를 구분하고 과거 통과 개수를 현재 새 검사로 합산하지 않았다. 코드·모델 변경이나 실게임 시험은 수행하지 않았다.

문서 보완 18개 항목의 반영 위치를 확인한 뒤 임시 목록 파일을 삭제했다. 최종 Markdown 30개의 로컬 링크 738개와 내부 제목 연결, 코드 블록 닫힘을 확인했다. 모델 시험 TOML 29개·증강 프리셋 12개, 비교 수치와 특징 순서를 대조했고 보관한 짧은 세션 7개의 파일 해시 49개가 manifest와 일치했다. 60초 설정 두 개의 실행부 불일치와 후보 미선정·실게임 대기 상태는 현재 안내에 명시했다.

## 2026-09-29

- 루트 Cargo workspace에 TelemetryDataServer와 DrivingRecorder 구현.
- 현재 조건: BMW M4 GT3 / Brands Hatch / 핫랩 / 16:00 / 강수 0.
- 사용자 선언: 현재 셋업 유지, 자동 변속, 유선 Xbox 패드, 핫랩 피니시의 차량 상태 리셋.
- Rust 14개 테스트 통과: ABI, freshness, native mapping, hotlap 시차, 구간 분리,
  큐 포화, 파일 오류, 덮어쓰기 방지, 종료 drain 등.
- Python 7개 테스트 통과: 온전한 랩, 비정상 종료, 시간·NaN, 이벤트 불일치, 드롭, 중복 패킷, 무효 핫랩.
- 최종 fmt/clippy 통과.

### 5초 실제 파일 저장 시험

세션: `recordings/1790666857036209000-10700-0`.
검사: `validation-1790666868416940600.json`.
497샘플, 큐 누락 0, 형식 검사 통과. 온전한 랩은 0이므로 목표 달성 아님.
poll 평균 간격 약 9.990ms, p99 약 10.040ms, 최대 약 10.050ms.
읽기 평균 약 5.091µs, 최대 28.7µs. 전체 제어 지연이 아닌 해당 실행의 관측값이다.

### 장시간 실제 검증

- 첫 진단 세션 `recordings/1790666876978224400-32440-0`: 핫랩 타이머가 랩 수보다
  먼저 갱신되어 lap_timer_reset으로 잘못 분리되는 것을 발견. 원본을 보존하고
  학습용으로 채택하지 않는다. 600초 시간 제한으로 정상 종료됨.
- 중간 보정 세션 `recordings/1790667023216429200-396-0`: /stop으로 정상 종료.
  무효 핫랩에서 완료 랩 수가 증가하지 않는 현상을 발견. 최신 검증기의 랩 경계 검사에서
  실패하므로 이 파일은 진단용으로만 보존한다.
- 최종 세션 `recordings/1790667304709851800-26868-0`: 360초 기록 정상 종료.
  21,462샘플, 행·이벤트 큐 드롭 0, 파일 형식 통과, 온전한 1랩 기록 통과.
  [최종 검사 결과](../recordings/1790667304709851800-26868-0/validation-1790668046250320700.json).
- lap 경계 elapsed_ns: 249880191600 → 341450185600, 약 91.570초.
  그 사이 9,157샘플 중 3,532샘플이 lap_valid=false다. 따라서 깨끗한 학습용 유효 랩은 0이다.
- 연결 상태 live→paused→off→disconnected→off→live→paused→live를 실제 기록했다.
  리플레이 전환은 중간 보정 세션에서 확인했다. 사용자 메모 입력도 최종 세션에 기록했다.
- 최종 poll 간격 평균 10.004ms, p99 10.060ms, 최대 55.727ms; 건너뛴 주기 16.
  read 평균 4.534µs, p99 30µs, 최대 6033.2µs. 게임 종료·재연결 부하를 포함한다.
  모든 샘플이 정확히 10ms 간격이었다는 의미는 아니며 timing에 초과를 남겼다.
- 최종 루트 release 빌드의 추가 30초 시험 `recordings/1790668053448029900-23148-0`:
  2,998샘플, 시간 제한 정상 종료, 파일 검사 통과. 콘솔 도구의 Ctrl+C 바이트 주입은
  OS 제어 신호를 발생시키지 않아 Ctrl+C 실사용 검증 근거로 사용하지 않는다.
  /stop 및 시간 제한 종료는 실제 검증 완료, Ctrl+C 핸들러는 구현되어 있다.

첫 목표 전체 완료 여부: **완료 — 주행 기록 확보와 재연결 검증 완료**.
학습용 유효 랩 수집, 조향 방향 보정, 차량 제어·모델 학습은 다음 단계다.
