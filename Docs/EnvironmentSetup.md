# 개발과 실행 환경 구성

2026-10-18 기준. 저장소의 버전 고정 파일과 기존 Windows 실행 경로를 기준으로 빌드·GUI·학습 환경을 구성하는 순서다. 게임 데이터와 장치별 자산은 소스에 포함되지 않으므로 [자료 보관](DataArchiveGuide.md)과 [게임패드 운영 안내](GamepadOperationGuide.md)에서 함께 준비한다.

## 공통 환경과 빌드

Windows x64, Python 3.11과 Tkinter, MSVC C++ 빌드 도구 및 Windows SDK를 사용한다. Rust는 [rust-toolchain.toml](../rust-toolchain.toml)의 1.95.0이며 rustfmt·clippy를 포함한다. 저장소 루트에서 명령을 실행한다.

```powershell
cd C:\Users\YoonHyeon\Desktop\accad
rustup show
py -3.11 --version
cargo build --release --locked
```

Cargo workspace는 수집기·기록기·상태·vJoy·GamepadIO·actuator·기준 제어·정책·관리 라이브러리를 묶는다. GUI가 사용하는 실행 파일은 `target/release/poleposition-telemetry.exe`, `poleposition-recorder.exe`, `poleposition-policy.exe`, `poleposition-actuator.exe`, `poleposition-gamepad.exe`다. 처음에는 의존성을 받아야 하며 캐시가 있으면 `--offline`을 사용할 수 있다.

## Python 환경 분리

| 환경 | 용도 | 직접 의존성 |
| --- | --- | --- |
| .venv-training | GUI, PID/게임패드 모방학습, 기존 평가 | torch 2.8.0+cpu, numpy 2.3.3 |
| .venv-controller | v2 자료 준비·사전학습·온라인 SAC | 위 버전 + stable-baselines3 2.7.0, gymnasium 1.2.0 |

[ModelTrainer 잠금 목록](../ModelTrainer/requirements.lock.txt)과 [TrainingEnvironment 잠금 목록](../TrainingEnvironment/requirements.lock.txt)을 각각 사용한다. 기존 환경은 유지하고 새로 구성할 때만 다음 생성 명령을 실행한다.

```powershell
py -3.11 -m venv .venv-training
.\.venv-training\Scripts\python.exe -m pip install -r ModelTrainer/requirements.lock.txt
.\.venv-training\Scripts\python.exe -m pip check

py -3.11 -m venv .venv-controller
.\.venv-controller\Scripts\python.exe -m pip install -r TrainingEnvironment/requirements.lock.txt
.\.venv-controller\Scripts\python.exe -m pip check
```

직접 요구 버전은 각 디렉터리의 `requirements.txt`, 전체 의존성 재현은 `requirements.lock.txt`다. 설치 파일에 CPU PyTorch index가 명시돼 있다. GUI는 `.venv-training`에서 실행하고 SAC 작업을 선택하면 별도 `.venv-controller` 자식으로 연결한다. 데이터 처리 모듈의 표준 라이브러리 전용 경로도 있지만 모델 학습에는 위 환경이 필요하다.

```powershell
.\.venv-training\Scripts\python.exe -m TrainingController
# 필요한 경우 바로가기 생성. 다른 대상을 가리키는 같은 이름의 바로가기는 거절한다.
.\TrainingController\install_shortcut.ps1
```

관리 워커는 stdin/stdout을 UTF-8로 설정한다. 콘솔의 기본 CP949에 의존해 관리 이벤트를 해석하지 않는다. GUI 실행 파일·작업별 인자·재개 형식은 [GUI 규약](TrainingControllerSpecification.md)에 있다.

## 입력 장치별 설치 자산

| 경로 | 기존 검증의 버전과 필요한 자산 |
| --- | --- |
| vJoy 휠 | 2.2.2.0 드라이버·x64 SDK DLL, 장치 1 X/Y/Z, 검증된 휠 프로필·calibration JSON |
| ViGEm 게임패드 | 설치 패키지 1.22.0, 당시 드라이버 파일 1.21.442.0, 공식 1.16.18.0 소스로 빌드한 x64 ViGEmClient DLL |
| HidHide 교정 | 설치 패키지 1.5.230, 당시 HidHide.sys 1.4.181.0, CLI·허용 제어 실행 파일·독립 프로브·설정 JSON |

이는 로컬 검증 이력의 버전이며 최신 배포판을 뜻하지 않는다. 설치본 경로와 해시는 프로필·설치 근거를 따른다. 게임패드 설치 증거는 `input_profiles/gamepad-install-1791294115`, HidHide는 `input_profiles/hidhide-install-20261008`, vJoy는 [차량 입력 규약](VehicleInputSpecification.md)과 [복구 기록](XboxUsbInputInvestigation.md)에 있다.

설치·재부팅·실제 응답 확인 후 해당 프로필의 DLL·드라이버·ACC 설정 및 검증 근거가 현재 환경과 맞아야 한다. 단순 빌드 성공으로 드라이버 설치와 차량 입력 검증이 완료되지는 않는다. HidHide는 바이너리 복사본과 해시 고정도 있으므로 재빌드 뒤 [동시 슬롯 검사](GamepadOperationGuide.md#hidhide-교정-준비)를 다시 수행한다.

## 로컬 자료와 경로 준비

게임패드 현재 실행은 prepared 경로·heading profile·기준 경로 모델, speed-profile, 입력 프로필·ACC controls/assists, device validation, 선택 모델 manifest·model·parity·출력 해시를 요구한다. 학습은 원자료·split·필요한 과거 교정 모델까지 요구한다. 정확한 파일은 선택한 TOML과 `input_sha256`를 따라 확보한다.

TOML의 상대경로는 저장소 루트 작업 디렉터리에서 해석한다. 일부 GUI 프리셋과 cached feature 설정은 `C:\Users\YoonHyeon\Desktop\accad` 절대경로를 가진다. 경로를 바꾸면 프리셋과 프로필을 새 환경에 맞추고 새 결과로 검증한다. 과거 모델의 해시와 provenance를 덮어쓰지 않는다.

증강 스크립트는 기존 세션 모델과 실패 실행 두 개를 코드 상수로 지정한다. 경계 수정 스크립트는 최초 dataset 경로와 제거 표본 3개를 전제로 한다. 범용 새 데이터 CLI로 간주하지 않는다. 당시 실험을 재현하려면 [실험 검토](GamepadExperimentReview.md)의 원자료·당시 설정/코드·dataset과 manifest를 먼저 확보한다. 스크립트는 새 결과 폴더를 만들지만 증강 프리셋을 다시 쓰므로 현재 프리셋을 백업한 별도 작업 사본에서 재현한다.

## 검증 순서

소스·의존성 버전 확인 → 오프라인 검사 → 자료 및 모델 해시 확인 → 장치 응답·입력 분리 → 사용자 F7의 짧은 실제 시험 순서로 범위를 넓힌다. 검사 명령과 날짜별 기존 결과는 [검증 기록](VerificationLog.md)에서 구분한다. 현재 기본 교정의 60초 설정은 실행부 허용값과 맞지 않으므로 [설정 표](CurrentGamepadModels.md)의 사용 보류 항목을 확인한다.
