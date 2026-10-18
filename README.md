# 폴포지션 (Pole Position)

ACC 텔레메트리 기반 자율 레이싱 프로젝트입니다. **2026-10-18 기준** 기록·데이터 검토·기준 경로·상태 계산, vJoy 저속 경로 추종과 PID 모방학습, GUI, 게임패드 교사 수집·모방학습·사람 교정, 수동 준비 기반 SAC 실행을 구현했습니다.

저속 기준 제어의 한 바퀴와 SAC의 짧은 실제 제어·정차 후 학습·체크포인트 재개를 검증했습니다. 게임패드 자율 완주 성능, 같은 고정 모델의 3랩 평가, 자동 메뉴 초기화와 vJoy 장치 장애 대응은 남아 있습니다. 10월 17일 증강 실험은 후보를 선정하지 않았으며 실게임 비교 시험도 대기 중입니다.

전체 안내는 [문서 목차](Docs/README.md), 최신 실행 조합은 [게임패드 모델과 설정](Docs/CurrentGamepadModels.md), 남은 개발 과제는 [개발 체크리스트](Docs/DevelopmentChecklist.md)를 참조하세요.

## 빌드 및 실행

Windows x64, Rust 1.95.0 MSVC 빌드 도구, 검증용 Python 3.11 이상을 사용합니다.
프로젝트 루트에서 실행하세요.

GUI는 `.venv-training`에서 시작합니다. 학습 환경과 드라이버·로컬 자산 준비는 [환경 구성](Docs/EnvironmentSetup.md), 작업별 입력과 중지는 [GUI 실행 규약](Docs/TrainingControllerSpecification.md)에 정리했습니다.

```powershell
.\.venv-training\Scripts\python.exe -m TrainingController
```

게임패드 작업은 [수집과 교정 절차](Docs/GamepadOperationGuide.md)를 먼저 확인하세요. 기본 교정 설정의 60초는 현재 실행부가 거절하므로, [설정 대응표](Docs/CurrentGamepadModels.md)에서 허용 시간이 맞는 시험 설정을 선택합니다.

```powershell
cd C:\Users\YoonHyeon\Desktop\accad
cargo build --release --locked

# 10초 텔레메트리 진단
.\target\release\poleposition-telemetry.exe

# 설정 파일로 기록 시작
.\target\release\poleposition-recorder.exe

# 5분 기록 후 자동 종료
.\target\release\poleposition-recorder.exe --duration 300
```

터미널에 메모를 입력하고 Enter를 누르면 사용자 표시가 남습니다.
`/stop` + Enter 또는 `Ctrl+C`로 종료하면 남은 데이터를 저장합니다.
게임 상태가 바뀌면 연결 메시지가, 5초마다 샘플 수가 출력됩니다.
게임을 실행하지 않아도 대기할 수 있습니다. 첫 완전한 랩 검증에는 출발선 통과 두 번이 필요합니다.

설정: [DrivingRecorder/recorder.toml](DrivingRecorder/recorder.toml).
사용자 선언 조건과 실제 자동 관측 조건은 별도로 저장합니다.
자동 관측은 BMW M4 GT3 / Brands Hatch / 핫랩 / 16:00 / 비 없음으로 확인했습니다.

```powershell
python DrivingRecorder\validate_recording.py recordings\<세션ID>
```

새 validation JSON을 만들며 원본은 변경하지 않습니다.
`format_pass`와 `first_goal_recording_pass`는 다른 판정입니다.
단순 파일 정상 여부만으로 한 바퀴 기록 성공을 선언하지 않습니다.

## 개발 검증

```powershell
cargo test --locked --offline
cargo fmt --all -- --check
cargo clippy --locked --offline --all-targets -- -D warnings
python -m unittest discover -s DrivingRecorder -p 'test_*.py' -v
```

최초 빌드에는 의존성 다운로드가 필요하며 이후 --offline으로 검사할 수 있습니다.
위 명령은 검사 방법입니다. 날짜별 통과 결과와 실게임 검증 범위는 [검증 기록](Docs/VerificationLog.md)을 참조하세요.

- [개발 체크리스트](Docs/DevelopmentChecklist.md)
- [기록 규약과 인터페이스](Docs/RecordingSpecification.md)
- [검증 기록](Docs/VerificationLog.md)

## DatasetBuilder

정상 종료한 기록에서 유효 랩을 추출하고 품질 보고서와 랩 단위 학습/검증/평가 분리를 생성합니다.
원본 파일은 변경하지 않습니다. Python 3.11 표준 라이브러리만 사용합니다.

```powershell
python -m DatasetBuilder --session recordings/1790669289916063400-33768-0 --config DatasetBuilder/config.toml
python -m unittest DatasetBuilder.test_builder DatasetBuilder.test_report -v
```

출력은 `datasets/<고유 ID>/`입니다. `report.html`에서 결과를 확인하고 `manifest.json`의
`complete`와 `split_complete`로 변환 완료와 분리 완료를 구분합니다.
현재 출력은 경로 모델과 상태 처리의 입력입니다. 모델별 학습 자료 준비는 별도 단계이며 [PID 학습 규약](Docs/ModelTrainingSpecification.md), [게임패드 학습 규약](Docs/GamepadTrainingSpecification.md), [강화학습 규약](Docs/TrainingEnvironmentSpecification.md)을 따릅니다.

- [DatasetBuilder 규약](Docs/DatasetBuilderSpecification.md)
- [DatasetBuilder 개발 기록](Docs/DatasetBuilderDevelopmentLog.md)

유효 랩의 의심 구간 검사와 명시적 검토 판정은 다음 명령으로 수행합니다.

```powershell
python -m DatasetBuilder.review scan --dataset datasets/1790672790720927100-cad5b45e --config DatasetBuilder/review.toml
python -m DatasetBuilder.review finalize --review reviews/scan-1790674428796657400-4cac817c --decisions reviews/scan-1790674428796657400-4cac817c/decisions.reviewed.json
python -m unittest DatasetBuilder.test_builder DatasetBuilder.test_report DatasetBuilder.test_review -v
```

새 검사에서는 출력된 폴더의 판정 초안을 복사하여 모든 랩과 후보를 검토한 뒤 확정합니다.
위 finalize 명령은 이번 실제 검토 파일로 후속 목록을 재생성하는 예시입니다.
후보 탐지만으로 자동 제외하지 않으며 불확실한 랩은 보류합니다.
기존 데이터셋과 분리는 유지하고, 확정 결과의 `usage.json`에서 집합별 사용 목록을 제공합니다.
TrackModel에는 `trackmodel_candidates`에 있는 train 랩만 전달합니다.

- [11랩 검토 결과와 근거](Docs/DrivingDataReview.md)

## TrackModel

확정 검토의 사용 가능한 train 중 가장 빠른 실제 랩으로 기준 주행선을 만듭니다.
약 1m 간격의 폐곡선 좌표, 누적 거리, 접선과 부호 있는 곡률을 저장합니다. Python 3.11 표준 라이브러리만 사용합니다.

```powershell
python -m TrackModel --review reviews/final-1790674957929734700-befda832 --config TrackModel/config.toml
python -m unittest DatasetBuilder.test_builder DatasetBuilder.test_report DatasetBuilder.test_review TrackModel.test_model -v
```

결과는 `track_models/<고유 ID>/`의 manifest.json, reference.csv, validation.json, report.html입니다.
`from TrackModel.model import load_reference`로 완료·해시·기하 검사를 거쳐 읽을 수 있습니다.
품질 검사에 실패하면 완료 처리하거나 차순위 랩으로 교체하지 않습니다.
StateProcessor는 이 경로로 위치·횡오차·방향·곡률을 계산합니다. 이 경로를 사용한 저속 기준 제어의 한 바퀴 결과는 [고저차 분석](Docs/OneLapTerrainReview.md)에 기록했습니다.

- [TrackModel 규약과 Python 사용법](Docs/TrackModelSpecification.md)
- [TrackModel 개발·검증 기록](Docs/TrackModelDevelopmentLog.md)
- [현재 경로 품질 보고서](track_models/track-1790677262927433400-796fb617/report.html)

## StateProcessor와 차량 입력

- [StateProcessor 실행·상태 규약](Docs/StateProcessorSpecification.md)
- [차량 입력 설치·보정·실행·복원](Docs/VehicleInputSpecification.md)
- [차량 입력 개발 기록과 재부팅 후 재개 지점](Docs/VehicleInputDevelopmentLog.md)

```powershell
# 읽기 전용 vJoy 점검
cargo run --release --locked --offline -p poleposition-vjoy -- probe

# 실제 장치를 사용하지 않는 감시 프로세스 검사
target/release/poleposition-actuator.exe self-test --fault producer-kill

# 실제 시험은 규약의 별도 휠 보정을 마친 뒤 실행. F7 시작 / F8 중지.
cargo run --release --locked --offline -p poleposition-actuator -- test --scenario stationary --prepared state_runs/prepared-1790679300781499500-3e604d8c --profile input_profiles/<확정보정ID>/calibration.json
```

휠 입력은 `VirtualJoyStick`, 게임패드 입력은 `GamepadIO`, 실제 제어 감시는 `ModelAndActuator`가 담당합니다. 실행 결과는 `actuator_runs/`, 설정 백업과 보정은 `input_profiles/`에 저장합니다. Rust 74개·Python 94개 통과는 2026-09-29 차량 입력 개발 당시의 회귀 결과입니다. 이후 검증은 [검증 기록](Docs/VerificationLog.md)에서 날짜와 범위를 구분합니다.

## 게임패드 학습과 실험

교사 기록 → 자료 선별 → 모방학습 → 짧은 모델 시험 → 사람 교정 → 재학습 순서로 진행합니다. 연속 랩·세션 단위 학습과 증강 후보는 별도 설정을 사용합니다.

- [수집과 교정 절차](Docs/GamepadOperationGuide.md)
- [게임패드 학습과 특징 규약](Docs/GamepadTrainingSpecification.md)
- [모델과 프리셋 대응표](Docs/CurrentGamepadModels.md)
- [실험 비교와 실패 분석](Docs/GamepadExperimentReview.md)
- [기록 선별과 보관](Docs/DataArchiveGuide.md)
