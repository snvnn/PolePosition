# 차량 입력 연결 규약 v1

작성: 2026-09-29 · 소프트웨어 0.1.0 · Windows 10 x64 / Rust 1.95 / Python 3.11

## 범위와 현재 상태

Rust 명령을 vJoy 장치 1번의 X(조향), Y(가속), Z(브레이크)에 전달한다. 명령 생성 자식 프로세스와 장치를 단독 소유하는 감시 프로세스를 분리한다. 실제 시험은 별도 휠 프리셋, Xbox 패드 분리·전원 끔, BMW M4 GT3 / Brands Hatch / 단독 세션 / 자동 변속 조건이다.

이번 구현은 정차와 20km/h 이하의 짧은 입력 시험까지다. 경로 추종·목표 속도·Pure Pursuit·PID·모델 학습은 포함하지 않는다. StateProcessor의 `ready`는 여러 시작 조건 중 하나이며 제어 허가 자체가 아니다.

2026-09-29 재부팅과 vJoy 2.2.2.0 인식, 실제 장치 확보·중립 전송·반환, ACC 설정 화면의 세 축 지정과 해제를 확인했다. 초기값 변경 후 22:23 재부팅에서 첫 입력 전 SDK 위치 `[16384,1,1]`도 확인했다. 페달 1은 드라이버의 0% 초기화 수식에 따른 정수 한 단계다. 새 Windows 관측 클라이언트의 중간값을 실제 드라이버 초기값으로 해석하지 않는다. 이후 정차·저속 반응, F8, 일시정지, 명령 생성자 종료/정지와 ACC 관측 만료를 실제 검증했다. 감시자 강제 종료에서는 마지막 제동값이 유지되는 한계를 확인했다. 기존 패드 설정은 원본 바이트로 복원했지만 Xbox USB 입력이 일시 회복 후 다시 끊겨 **패드 복원 검증은 미완료**다. 실제 전면 해제·드라이버 장애 주입도 남아 있다. 근거는 [실게임 검증 집계](../actuator_runs/live-verification-20260929.json)와 [개발 기록](VehicleInputDevelopmentLog.md)에 남긴다.

2026-09-30 갱신: vJoy 재설치 중 Xbox HID 드라이버 연결 변경을 재현했고, 패드 분리 설치·잘못된 HID 등록 제거·재연결로 복구했다. vJoy 활성 상태의 XInput 두 API 각각 300/300 성공과 ACC 약 1분 정상 입력 유지·연결 해제 없음 사용자 확인으로 **패드 복원 검증을 완료**했다. [복구 근거](../input_profiles/vjoy-reinstall-20260930/completed-comparison.json), [ACC 확인](../input_profiles/vjoy-reinstall-20260930/acc-confirmation.json). 재설치 후 가상 축 중립값·기존 보정은 자동 입력 재개 전에 다시 검증하며 실제 전면 해제·드라이버 장애 주입도 미완료다.

2026-10-01 재시험: [현재 보정](../input_profiles/calibration-1790823244181544300-71cd97f2/calibration.json)으로 재설치 후 중립·정차·저속·F8 검증을 마쳤다. [집계](../input_profiles/ingame-retest-20261001-1790822633251880700-1d54b992/verification.json), [개발 기록](VehicleInputDevelopmentLog.md). 최초 메뉴 표시 불일치와 시작되지 않은 두 시도는 실패/대기 근거로 보존했다. 시험 중 설정 파일이 바뀌면 자동으로 새 해시를 인정하지 않고 종료한다. 휠 모드 적용 후 실제 저장값을 재검사해 새 보정을 만든다.

주행 시험은 `events.csv`에 `acc_foreground` 전환과 `start_requested`를 기록한다. F7 요청의 시작 전후 모드, 전면·신선도·위치 계산·차량/트랙 일치·속도·명령 만료 조건을 남긴다. F7이 수신됐지만 대기 상태면 터미널에도 조건을 표시한다. 시작 키가 수신되지 않은 경우와 수신 후 조건에서 차단된 경우를 구분하며, 어느 경우에도 전면/정차/데이터 검사를 우회하지 않는다. 실제 정차 시험은 F7 수신·Running 전환과 ACC 관측으로 확인한다.

## 의존성 고정과 보존

- [공식 vJoy 2.2.2.0 배포](https://github.com/BrunnerInnovation/vJoy/releases/tag/v2.2.2.0), 같은 배포의 SDK.zip x64 DLL을 사용한다.
- 설치 파일 서명은 Brunner Elektronik AG / Valid로 확인했다. URL·SHA-256·ABI는 [dependency-lock.json](../VirtualJoyStick/dependency-lock.json)에 고정한다.
- [PrepareVJoy.ps1](../VirtualJoyStick/PrepareVJoy.ps1)은 다운로드와 해시·서명 검사만 수행한다. 자동 설치·재부팅·대체 드라이버 선택은 하지 않는다.
- 2026-09-30 이 PC에서는 Xbox USB 패드를 연결한 채 vJoy 설치 시 Xbox HID 드라이버가 `xinputhid.inf`에서 `input.inf`로 바뀌며 입력 상실이 재현됐다. 설치 전 실제 패드를 분리하고 ACC·Steam을 종료한다. 이 조건의 재설치는 정상 완료했으며, 설치 후 패드 재연결·드라이버 선택·입력을 별도로 검증한다. 자세한 [원인과 전후 근거](XboxUsbInputInvestigation.md)를 따른다. 설치 성공만으로 기존 축 보정이나 패드 입력 복원까지 통과 처리하지 않는다.
- DLL 파일 리소스 버전은 배포본에서도 `0.0.0`이다. 이 문자열로 SDK API 버전을 판정하지 않는다. 배포 ZIP/DLL 해시, `DriverMatch`, 드라이버의 `0x0222`와 설치된 sys 파일의 `2.2.2.0`을 구분해 보존한다.
- [V3 SDK 헤더](https://github.com/BrunnerInnovation/vJoy/blob/v2.2.2.0/SDK/inc/public.h)와 [인터페이스](https://github.com/BrunnerInnovation/vJoy/blob/v2.2.2.0/SDK/inc/vjoyinterface.h)에 맞춘 `repr(C)` 구조는 124바이트·정렬 4바이트다. X/Y/Z 오프셋은 16/20/24다. `ResetVJD`는 호출하지 않는다.

설치 전 백업: [backup-1790682001894554500-e7e15cde](../input_profiles/backup-1790682001894554500-e7e15cde/manifest.json). `controls.json`과 `assists.json`의 원본 바이트·길이·해시를 보존했다. 프리셋 폴더가 있으면 함께 백업한다. ACC assists는 BOM 없는 UTF-16LE일 수 있어 파싱과 바이트 보존을 분리한다.

```powershell
# 프로젝트 루트에서 실행
python -m ModelAndActuator backup
& .\VirtualJoyStick\PrepareVJoy.ps1
cargo build --release --locked --offline --workspace
cargo run --release --locked --offline -p poleposition-vjoy -- probe
```

`probe`는 DLL·드라이버·축 범위·소유권과 가능한 읽기 전용 Windows 축 관측을 출력한다. 장치를 확보하거나 축을 전송하지 않는다. SDK `GetPosition`은 미소유 상태에서 4319를 반환할 수 있다. 이 경우 `initial_axes=null`로 남기며 중립으로 간주하지 않는다. WinMM 목록이 비어도 초기값을 추정하지 않는다.

Windows 축은 약 10ms 간격 21회의 관측을 `windows_position_observations`에 보존하고 마지막 값을 `windows_positions`에도 제공한다. 새 관측 클라이언트의 중간값만으로 실제 드라이버 초기값이나 ACC 입력을 단정하지 않는다. 전송 전부터 켜 둔 별도 관측과 SDK·ACC 결과를 대조한다. SDK는 프로세스당 한 번만 로드하며 추가 로드는 오류로 거절한다. 여러 조회에는 같은 `Native` 인스턴스를 재사용한다. 공식 SDK가 내부 창 클래스를 다시 등록할 때 팝업으로 멈추는 경우를 방지하기 위한 제약이다.

추가 진단은 [neutral_check](../VirtualJoyStick/examples/neutral_check.rs)(ACC 종료 확인, 중립만 전송)와 [observe_axes](../VirtualJoyStick/examples/observe_axes.rs)(3초간 독립 읽기) 예제를 사용한다. 전자는 냉간 시작·ACC 반응 검증을 의미하지 않는다. [SetNeutralDefaults.ps1](../VirtualJoyStick/SetNeutralDefaults.ps1)은 관리자 권한으로 Y/Z 초기값을 설정하고 이전 값과 복원 파일을 고유 폴더에 남긴다. 이 PC에서 레지스트리 저장과 장치 재활성화는 확인했지만 새 WinMM 클라이언트의 초기값 관측은 실패했다. 해당 실패 결과를 보존하고 재부팅이나 실행을 반복해 통과로 표시하지 않는다.

## 재부팅 후 장치·보정 절차

1. ACC 종료, Xbox 패드 분리·전원 끔 상태에서 재부팅한다. 재부팅 후 `probe`와 Windows 게임 컨트롤러/공식 vJoy 모니터에서 장치를 확인한다.
2. 장치 1에 X/Y/Z를 활성화한다. FFB·버튼·POV는 시험에 사용하지 않는다. 초기 X 중앙·Y/Z 해제를 **실측**한다. 기본값이 다르면 초기값 설정 방법을 확인하여 수정하고 재검증한다. 초기값이 중립이라는 가정으로 보정을 확정하지 않는다.
3. ACC에서 기존 패드 설정을 보존하고 자동 제어용 휠 프리셋 `PolePosition-vJoy`를 별도로 저장한다. 축을 하나씩 지정한다. 반전·중앙·전체 범위, 선형 조향, 페달 감마 1, 불필요한 데드존 제거를 확인한다.
4. `bind`를 사용할 때 ACC 컨트롤 설정 화면에 머문다. 기본 모드는 신선한 일시정지/오프 공유 메모리가 필요하다. 이 PC에서는 Esc → 컨트롤에서도 live 상태가 관측되었다. 이 경우 세션을 종료하고 메인 메뉴의 휠 설정 화면임을 직접 확인한 뒤 `--main-menu-confirmed`를 추가한다. 이 모드는 ACC 전면, 세 공유 메모리가 모두 없음(0.5초 연속)을 요구한다. 하나라도 나타나거나 조회 오류가 발생하면 해당 실행은 다시 허용되지 않는다. 공유 메모리 부재 자체가 설정 화면을 증명하지 않으므로 사용자의 화면 확인을 대신하지 않는다. F7을 누르면 해당 축만 8초 동안 전체 범위를 왕복한다. F8·Ctrl+C·전면 해제·세션 감지·기록 실패 시 중립으로 복귀하고 해제한다.
5. 장치 해제 뒤 X 중앙·Y/Z 해제를 Windows와 ACC에서 확인하고 근거를 보정 초안에 남긴다. 단순 SDK 전송 성공은 게임 반영의 증거가 아니다.

```powershell
cargo run --release --locked --offline -p poleposition-actuator -- bind --axis steering --settings-confirmed
cargo run --release --locked --offline -p poleposition-actuator -- bind --axis throttle --settings-confirmed
cargo run --release --locked --offline -p poleposition-actuator -- bind --axis brake --settings-confirmed

# 사용자 확인: 주행 세션 밖의 메인 메뉴 > 컨트롤 > 휠 화면에서만 사용
cargo run --release --locked --offline -p poleposition-actuator -- bind --axis steering --settings-confirmed --main-menu-confirmed

python -m ModelAndActuator calibration-draft --backup input_profiles/backup-1790682001894554500-e7e15cde --prepared state_runs/prepared-1790679300781499500-3e604d8c
python -m ModelAndActuator calibration-finalize --draft input_profiles/<초안ID>/calibration.json
```

초안은 `complete=false`, 모든 확인 항목이 false다. 실제 확인 뒤 `checks.<항목>.passed`, `evidence`, `reviewer`, ISO 8601 `reviewed_at`, `preset_name`, `driver_file_version`을 작성하고 필요한 축 반전을 지정한다. 확인하지 않은 항목을 true로 만들지 않는다. 확정 명령은 새 폴더를 만들며 초안을 덮어쓰지 않는다. 출력된 보정 파일 경로를 `--profile`로 전달하거나 config.toml의 profile에 지정한다.

보정 파일은 드라이버 파일·ACC controls/assists·원본 백업 명세·휠 백업 명세 해시, 축 범위/중앙/반전, 관측 버전과 차량/트랙 식별을 포함한다. Rust도 파일·설정·범위를 다시 검사한다. 실행 중 보정·설정 파일 변경은 별도 감시 스레드가 확인해 시험을 중단한다. 프로필 해시는 변조·우발 변경 검사이며 서명된 신뢰 증명은 아니다.

활성 controls에는 vJoy의 Steer=0(X), Gas=1(Y), Brake=2(Z)가 각각 한 번씩 있어야 하고, 분리 축·전체 -1~1 범위를 검사한다. 저장한 프리셋과 활성 파일이 다르면 게임을 종료하고 [ActivateSavedPreset.py](../VirtualJoyStick/ActivateSavedPreset.py)로 적용할 수 있다. 이 도구는 현재 설정을 먼저 백업하고 저장된 `PolePosition-vJoy.json` 바이트를 controls.json에만 적용한다. 재실행 후 실제 적용 여부는 확인해야 한다. 2026-09-29 재부팅 초기값과 ACC 재실행 후 세 축 유지·조향 부호 확인을 마치고 [첫 보정 프로필](../input_profiles/calibration-1790688498795891900-c6781475/calibration.json)을 확정했다. 이후 SpaceBar 기어 올리기 설정을 반영한 [시험 당시 보정](../input_profiles/calibration-1790689382729320700-2b7af072/calibration.json)을 사용했다. 현재는 패드 설정으로 복원했으므로 이 휠 보정이 활성 설정과 일치한다고 간주하지 않는다. 다음 휠 시험 전 설정과 보정을 다시 검증한다. config.toml의 기본 경로에 복제하지 않는다.

## 명령과 감시

실제 강제 종료 결과(2026-09-29): 감시자 종료 뒤 마지막 브레이크 1이 약 10초 계속 유지됐으며 사용자도 확인했다. 정상 종료 때는 중립을 명시적으로 전송해 ACC 세 축 0을 확인했다. 따라서 감시자 강제 종료를 중립 복귀 수단으로 사용하지 않는다. 가속 중 감시자 자체 장애가 나면 마지막 가속이 해제된다고 보장할 수 없다. 시험 후 새 소유자의 중립 전송으로 복구했으며 드라이버 자체 장애 시험은 별도다. 근거는 [개발 기록](VehicleInputDevelopmentLog.md)의 감시자 강제 종료 항목이다.

현재 시험용 보정은 [스페이스 바 기어 올리기를 반영한 프로필](../input_profiles/calibration-1790689382729320700-2b7af072/calibration.json)이다. 이 PC에서 휠 프리셋 파일은 키보드 버튼 목록을 저장하지 않았으므로 활성 controls 전체도 별도 백업했다. 이전 보정 파일은 변경 전 설정의 기록으로 보존하며 현재 시험에 사용하지 않는다.

`VirtualJoyStick / poleposition-vjoy`는 SDK 로드·조회·확보·변환·V3 일괄 전송·해제와 모의 장치를 제공한다. `ModelAndActuator / poleposition-actuator`는 검사·변화율·상태 전환·프로세스·시험·기록을 담당한다.

명령 필드: `sequence`, `created_ns`, `expires_ns`, `observation_sequence`, `observation_ns`, `tracking_epoch`, `steering`, `throttle`, `brake`.

- 조향 -1=왼쪽, 0=중앙, +1=오른쪽. 페달 0=해제, 1=최대. 비유한 값·범위 밖·중복/역순·미래/만료 시각·맞지 않는 관측 출처는 거절한다.
- 감시자는 최근 자체 관측 목록과 명령의 sequence/시각/추적 구간을 대조한다. 오래된 관측을 새 시각으로 포장한 명령을 허용하지 않는다.
- Windows QPC를 ns로 환산해 프로세스 간 공통 시간으로 사용한다. UTC·게임 시각과 혼용하지 않는다.
- 익명 로컬 파이프 두 개를 사용한다. 읽기/쓰기 스레드와 단일 최신값 슬롯으로 출력 루프를 파이프 대기와 분리한다. 오래된 명령을 순서대로 재생하지 않는다. 메시지는 8KiB로 제한한다.
- 감시자가 직접 TelemetryDataServer를 읽고 Recorder Tracker·StateProcessor를 적용한다. 자식은 시험 명령만 생성한다. 자식 강제 종료·정지·파이프 단절 시에도 감시자가 중단을 처리한다.
- F7/F8은 Windows 전역 키로 등록한다. 등록 실패면 장치 확보 전에 종료한다. 전면 여부는 창 제목 대신 실행 파일 이름 `AC2-Win64-Shipping.exe`로 판정한다.
- F7 시작에는 유효 보정, 신선한 관측·두 페이지, 차량/트랙 일치, `ready`, ACC 전면, 1km/h 미만 0.5초 연속 확인과 유효 명령이 필요하다. `lap_valid=false`만으로 중단하지 않는다.

[config.toml](../ModelAndActuator/config.toml)의 기본값은 100Hz / 100ms / F7·F8 / 1km/h·0.5초 / 20km/h / 조향 1.0·가속 0.20 / 변화율 1.0·0.5·2.0 / 30초 / 표시 2Hz / 큐 4,096이다. 조향은 2026-10-03 사용자 요청으로 기존 0.15에서 전체 정규화 축 범위 1.0으로 변경했다. 시작 대기는 120초다. 설정 검증 범위를 넘는 속도·입력·시간·만료값은 거절한다. 설정 화면의 전체 축 왕복에는 주행 상한을 적용하지 않으며 별도의 비주행 상태 조건을 적용한다.

가속과 제동 동시 요청에서는 가속을 즉시 0으로 만든다. 제동을 해제하는 변화율 처리 중에도 제동이 남아 있으면 가속을 내보내지 않는다. 축 최솟값·최댓값은 SDK 조회값을 쓰고 조향 중앙은 보정값이다. 세 축은 하나의 V3 보고서로 전송한다.

## 중단·해제

2026-10-01 실제 장치 장애 관측: 정차·N·가속 0·제동 1에서 해당 vJoy 장치만 비활성화하려 했으나 Windows 요청이 일반 오류를 반환했다. SDK는 실제 소유권 상실과 전송 실패를 보고했고 ACC에는 가속/브레이크가 각각 0.5로 나타났다. 재활성화 뒤에도 기존 장치 연결로는 전송이 실패했다. 사용자 일시정지 후 새 프로세스에서 중립을 전송해 복구했다. [근거](../input_profiles/stop-fault-check-20261001-1790824036572159900-5db76b4d/driver-fault-observation.json). 이는 정상 제동·입력 해제 성공이 아니며, 장애 중에는 F8 중단 명령도 장치에 전달되지 않을 수 있다. 게임 중단·복구 대응의 보완과 재검증 전에는 경로 추종 단계로 진행하지 않는다. 예정한 2초 비활성화 구간은 성립하지 않았고, 이번 관측을 모든 드라이버 장애에 일반화하지 않는다.

F8·Ctrl+C·30초·속도 초과·관측/페이지/명령 만료·연결/세션 변화·추적 상실·전면 해제·자식 종료·기록 실패가 중단 원인이다. 중단은 한 번 걸리면 해제되지 않으며 재실행과 F7이 필요하다.

F7로 한 번도 시작하지 않은 실행에서는 오류·중지에도 비중립 입력을 내보내지 않고 중립 상태로 반환한다. 아래 제동 절차는 시작한 시험에 적용한다.

1. 일반 변화율 제한을 건너뛰고 조향 0·가속 0·브레이크 1을 일괄 전송한다.
2. 신선한 서로 다른 관측에서 1km/h 미만을 0.5초 확인한다. 같은 관측을 반복해 정지 확인 시간을 채우지 않는다.
3. 확인 후 중립 페달을 전송하고 장치를 반환한다. 명시적인 게임 일시정지/오프 관측 또는 별도 프로세스 조회로 확인한 게임 종료에서도 반환할 수 있다. 이때 `stop_confirmed=false`, `released_while_halted=true`로 실제 정차와 구분한다.
4. 정지를 확인할 수 없으면 제동 상태를 계속 유지한다. 추가 F8/Ctrl+C를 강제 해제로 취급하지 않는다. 사용자는 ACC를 일시정지하거나 종료해야 한다. 관측 불명 상태에 시간 제한만으로 입력을 해제하지 않는다.

드라이버 호출 실패는 전송 성공으로 기록하지 않는다. 장치 오류 중에는 출력 상태를 보장할 수 없다. 감시자 자체 강제 종료·OS 종료·드라이버 장애에도 제동 보장은 없다. 예외 경로에서 무조건 중립을 전송하는 소멸자를 두지 않았다. 마지막 입력 유지/초기화 동작은 이 PC에서 별도로 검증해야 한다.

## 정차·저속 시험과 결과

`--scenario brake-hold`는 장애 진단용 제동 유지 모드다. 기존 F7 시작 조건을 통과한 뒤 조향 0·가속 0·제동 1만 요청하며 일반 제동 변화율을 적용한다. 시나리오가 임의로 완료되지 않고 기존 감시자의 30초 제한·F8·전면 이탈·기타 중단 조건으로 끝난다. `stationary`의 9초 좌우 조향 및 `crawl`의 저속 이동 시험과 구분한다. 장치 장애 시험은 N 기어·정차를 별도로 확인한다.

```powershell
cargo run --release --locked --offline -p poleposition-actuator -- test --scenario stationary --prepared state_runs/prepared-1790679300781499500-3e604d8c --profile input_profiles/<확정보정ID>/calibration.json
cargo run --release --locked --offline -p poleposition-actuator -- test --scenario crawl --prepared state_runs/prepared-1790679300781499500-3e604d8c --profile input_profiles/<확정보정ID>/calibration.json
python -m ModelAndActuator report --run actuator_runs/<시험ID>
```

정차 시험은 제동 상태에서 작은 좌우 조향·중앙 복귀를 약 9초 수행한다. 저속 시험은 **사용자가 직선 정차 위치를 선택**한 뒤 작은 가속과 제동, 정지 확인, 제한된 좌/우 시험을 수행한다. 각 이동 구간은 약 2초이고 중간 정지 확인이 없으면 다음으로 넘어가지 않는다. 경로 추종이나 일정 속도 유지는 하지 않는다. 처음에는 가속 0.15, 좌/우 구간은 가속 0.10·조향 절댓값 0.10이며 전체 상한도 다시 적용한다.

실행 결과는 고유한 `actuator_runs/<ID>/`에 저장한다. bind도 별도 폴더에 기록한다.

| 파일 | 의미 |
|---|---|
| manifest.json | 버전·설정·보정·입력/출력 해시·완료 여부 |
| config.json | 주행/모의 실행 당시 고정 설정; bind는 manifest에 포함 |
| commands.csv | 요청, 제한 적용값, 전송 시도와 SDK 반환값, 축 정수, 거절/중단 이유와 비용 |
| telemetry.csv | ACC 관측 페달·원시 조향·속도·상태·관측/페이지 시각·sequence/segment/epoch |
| events.csv | 연결, 추적 구간, 실행 상태, 중단·해제 이벤트 |
| summary.json | 종료 원인·실제 정차 확인·반환 여부·행 수·누락·오류·시간 통계 |
| validation.json | Python의 해시·열·행 수·시간순·유한값 재검사 |
| report.html | 외부 의존성 없는 HTML/SVG 신호·비용·이벤트 보고서 |

명령 검사 행은 `applied_*`와 `driver_ns`가 비어 있다. 실제 전송 시도 행에는 제한 적용값, 드라이버 비용과 `send_success`가 있다. SDK 성공과 ACC 반영은 구분한다. CSV는 전체 행을 보존하고 그래프만 극값을 유지해 축소한다.

`complete=true`는 파일 생성·검증 완료다. `scenario_completed`, `normal_shutdown`, `recording_valid`, `stop_confirmed`를 별도로 확인한다. 시작 대기 후 종료한 기록이 있어도 차량 반응 검증 성공은 아니다. 큐 포화와 저장 실패는 중단 원인이며 기록 검증 실패다. 정상 반환 후 큐를 비우고, 보고서 저장까지 끝난 뒤 완료 상태를 쓴다. 기존 결과를 덮어쓰지 않는다. 보정 등 사전 검사에서 차단되면 장치 입력 없이 오류로 종료한다.

보정 실행은 `main_menu_confirmed`를 명세에 남긴다. F7 시작이 차단되면 `binding_blocked` 이벤트에 상태·관측 나이·전면 여부·세션 부재 검사 결과를 저장한다. 메인 메뉴 모드는 보정 `bind`에만 적용되며 주행 시험의 텔레메트리·StateProcessor·보정 필수 조건은 변경하지 않는다.

비용은 명령 검사, 명령 생성→감시자 수신, 장치 전송 함수, 실제 갱신 간격, 관측 데이터 나이를 구분한다. `driver_ns`는 축 변환·소유권 조회·UpdateVJD를 포함한 `device.send` 범위이며 해제 행은 중립 전송·반환의 합계다. 만료 원인의 시작 시각은 검출 시각이 아닌 만료 기한이다. F8/Ctrl+C는 이벤트 수신 스레드에서 관측한 시각부터 측정한다. **가속 0 전송까지 150ms 이하**가 실측 목표이며 실게임 결과가 있어야 판단한다. ACC에서 신선한 가속 0을 처음 본 시각은 따로 저장하되 이미 0이었을 수 있어 인과적 반응 시간이라고 단정하지 않는다.

저속 시험 시작 기어가 중립인 경우 F7을 누르고 제동 표시가 찬 뒤 지정한 키(현재 스페이스 바)로 1단을 넣는다. crawl은 중립/후진에서 가속하지 않고 제동을 유지한다. 자동 변속 설정과 입력 제한은 그대로다. 추가된 `telemetry.csv`의 `gear_raw`(0=후진·1=중립·2=1단), `rpm` 열로 기어·엔진 상태를 남긴다. 기존 v1 CSV에는 이 두 열이 없을 수 있으며 보고서는 존재할 때 그래프를 표시한다. 명령 순서의 `scenario_completed`와 실제 차량 이동 검증은 구분한다.

## 설정 복원

ACC를 종료한 뒤 원본 백업을 복원한다. 명령은 실제 게임 프로세스가 없는지 확인하고 현재 휠 설정도 새 백업으로 남긴다. 원본의 파일만 바이트 그대로 교체하고 다시 해시를 확인한다. 새 휠 프리셋 파일을 임의로 삭제하지 않는다.

```powershell
python -m ModelAndActuator restore --backup input_profiles/backup-1790682001894554500-e7e15cde --game-closed
```

복원 기능은 임시 디렉터리 검사와 이 PC의 실제 controls/assists 원본 바이트·해시 일치를 확인했다. [복원 직전 휠·키보드 백업](../input_profiles/backup-1790690200443560500-0c30359c/manifest.json)과 [복원 결과](../input_profiles/backup-1790690200443560500-0c30359c/restore.json)를 보존했다. 게임 재실행 후 장치 목록이 갱신될 수 있으므로 전체 파일 해시와 패드 설정값 일치를 구분한다. Xbox 유선 입력은 일시 회복 후 연결 해제가 재발해 실제 패드 복원 검증은 미완료다. 파일 복원 성공을 하드웨어 입력 정상으로 간주하지 않는다. 문서 폴더 쓰기는 실행 환경에 따라 별도 파일 권한이 필요할 수 있다.

## 재현 검사와 남은 실제 검증

```powershell
cargo test --locked --offline --workspace
cargo clippy --locked --offline --workspace --all-targets -- -D warnings
python -m unittest DatasetBuilder.test_builder DatasetBuilder.test_report DatasetBuilder.test_review TrackModel.test_model StateProcessor.test_state ModelAndActuator.test_actuator

# 실제 장치에 접근하지 않는 별도 프로세스 모의 시험
target/release/poleposition-actuator.exe self-test --fault producer-kill
target/release/poleposition-actuator.exe self-test --fault producer-stall
target/release/poleposition-actuator.exe self-test --fault pipe-disconnect
target/release/poleposition-actuator.exe self-test --fault telemetry-stale
```

모의 시험은 `mock=true`로 표시한다. 생성 프로세스·파이프는 실제로 실행하지만 관측과 장치는 가짜다. 단위 통합 검사에서는 생성 프로세스를 Windows에서 실제 일시 정지시키는 경우도 검사한다. 그 결과를 vJoy·ACC 지연 측정으로 해석하지 않는다.

2026-10-01 현재 초기값·보정, 정차·저속, 전면 상태 F8, 일시정지·관측 만료·생성자 종료/정지, 감시자 강제 종료 후 마지막 제동 유지 관측, 기존 설정의 바이트 복원, vJoy 재설치 후 Xbox 유선 입력과 ACC 반응, 실제 전면 이탈 중단을 확인했다. 실제 장치 장애는 주입 중 소유권 상실·중단 전송 실패·ACC 페달 50%/50%를 관측했으므로 해결 완료가 아니다. 감시자 종료 때 제동이 남은 관측은 다른 마지막 입력의 해제나 드라이버 장애 시 제동을 보장하지 않는다.

사용자 요청으로 추가한 `demo` 명령은 [BaselineController 규약](BaselineControllerSpecification.md)을 따른다. 기존 F7/F8·감시·저장 큐를 재사용하고 `controller.csv`와 경로 상태 열을 추가한다. 기존 시나리오는 그대로 유지한다. 첫 데모 결과는 [개발 기록](BaselineControllerDevelopmentLog.md)에 따로 남기며 단계 5의 장치 장애 미해결 항목을 완료로 바꾸지 않는다.

## 별도 게임패드 경로 (2026-10-06)

`GamepadIO`는 XInput 원시 수집과 ViGEmClient 직접 Xbox 360 출력을 제공한다. vJoy로 변환하지 않는다. 공통 `Device` 인터페이스는 종류, 연결 확인, 전송 결과, 중립화, 해제를 노출하며 기존 vJoy 구현을 유지한다. 새 모델 태그는 `gamepad_v1`, 행동 순서는 `[stick_x, rt, lt]`다. 정수 스틱의 음수는 32768, 양수는 32767로 나누며 트리거는 255로 나눈다. 역변환은 반올림하며 감마·데드존·휠 배율·페달 상호 배제 처리를 추가하지 않는다.

설치 패키지는 ViGEmBus 1.22.0, 실제 드라이버 파일 버전은 1.21.442.0이다. 클라이언트는 공식 v1.16.18.0 소스를 x64 DLL로 빌드했다. 설치본 Authenticode, 파일 해시, 설치 전 ACC 설정과 장치 목록, 최초 관리자 권한 부족(1925/1603)과 동일 설치본 UAC 승격 후 성공을 `input_profiles/gamepad-install-1791294115`에 남겼다. 기존 vJoy는 정상 인식 상태를 유지했다. 로컬 빌드 DLL에는 배포자 서명이 있다고 주장하지 않는다.

장치 최초 연결 시 ViGEmBus의 초기 USB 보고서와 내부 0 캐시 때문에 중립만 반복 전송하면 초기 스틱 값이 남았다. 공식 `XusbPdo.cpp`의 초기 blob/변경 보고서 비교 동작을 확인했고, 제동 보고서 다음 중립 보고서를 보내 각각 XInput 반영을 확인한 뒤 준비를 완료한다. 이 초기화는 출발 PID가 아니다. 장치 전송·중립화·해제는 반환 코드와 XInput 상태를 구분하며 장치 오류는 정차 성공으로 처리하지 않는다.

GUI의 연결 확인은 읽기 전용이다. 실제 패드 응답 검사는 가상 장치를 만들지 않는다. 실제 패드를 스틱으로 선택하고 F7 이후 안내된 각 입력을 3초 유지한다. 가상 패드 응답 검사는 실제 패드를 분리한 상태에서 동일 단계 입력을 전송한다. 수동 중립 검사는 개별 트리거를 확인하고, 자동 변속 검사는 가속 중 제동을 유지한다. 전면 이탈·관측 만료·움직임·장치 오류는 중단한다. 정차 상태의 수치 일치와 화면 반응 확인을 주행 동역학 또는 첫 코너 성공으로 확대하지 않는다.

교사 기록은 기존 `telemetry.csv`와 같은 단조 시계의 `inputs.csv`를 100Hz로 기록한다. 슬롯·연결·packet·sequence·읽기 구간·모든 원시 축·버튼·정규화 규칙, 설정 원본과 해시를 저장한다. packet 유지는 유효 입력이다. 장치 이탈·교체·기록 큐 초과·설정 변경은 자동 학습 자격을 해제한다. 진단 전용 기록도 교사 자료로 거부한다. 수동 변속 진단만으로 자동 변속 주행 성능을 검증했다고 간주하지 않는다.

`TrainingEnvironment/gamepad.py`는 새 주행 3개를 앞 2개 학습/마지막 1개 검증으로 고정한다. 관측 이전 입력만 특징으로 쓰고 관측 이후 최초 읽기(최대 20ms)를 정답으로 사용한다. 기존 ACC 조향은 스틱 정답으로 쓰지 않는다. CPU/seed 42/128×128/Adam 0.0003/배치 256/최대 50 epoch, tanh 조향·sigmoid 두 트리거를 사용한다. 학습 자료 정규화는 고정하고 독립 검증 MSE 최저 epoch 및 이전 입력 유지 기준값을 보고한다. 내보내기 후 Rust 예측 최대 오차 1e-5 검사를 통과해야 모델이 실행 가능하다.

`GamepadIO/trial.toml`의 새 직접 제어 진단은 5→10→30초이며 같은 모델/프로필과 직전 통과 증거를 요구한다. 정차 중 특징 이력을 준비하고 F7부터 정책이 양 축을 담당한다. 절대 20ms 정책 예약, 10ms 입력 감시, 관측·임대 만료 및 기존 경로·방향 중지를 유지한다. 제동 후 1km/h 이하 0.5초, 중립화와 분리 확인을 기록한다. 정차 후 `GamepadIO.run_report`가 요청·전송·다음 관측 대응과 공통 경로 위치의 교사/모델 비교를 저장하며 실패 시 다음 진단 단계는 차단한다. 이 진단 시간은 기존 일반 휠 실행 제한을 변경하지 않는다.

실제 완료 증거와 남은 단계는 `input_profiles/gamepad/implementation-status.json`에 기록한다. 교사 3회 수집·실제 새 모델 학습·5/10/30초 모델 주행·첫 코너 비교가 완료되기 전에는 전체 학습 연결 또는 주행 성공으로 표시하지 않는다. 온라인 SAC/자동 초기화/복귀 학습/FFB는 이 경로에 추가하지 않았다.

정차 검사 정정: 자동 변속·가속/브레이크 동시 100% 시험에서 실제 속도 1.015km/h를 관측했다. 따라서 브레이크 유지로 정차를 보장한다는 가정은 폐기했다. 해당 회차는 `tracking_lost`로 중단했고 정차 확인·해제만 성공했다. 이후 응답 검사는 엔진 OFF(RPM 0), 중립, 신선한 정차 관측에서만 F7을 허용한다. 엔진 기동은 별도 종료 원인이며 가상 입력은 가속 0 제동으로 바뀐다. 실제 패드 검사는 끝까지 읽기 전용이다. 엔진이 켜진 비교 기록과 엔진 OFF 기록을 같은 검사로 합치지 않는다.

사용자가 엔진 OFF 유지 불가를 확인했으므로 정차 가속 검사는 반복하지 않는다. 성공했던 수동 중립의 두 장치 기록과 화면 반응 확인을 축 전달 증거로 보존한다. 현재 설정은 당시와 정확히 `autoGear: 0→1`만 다른지 원본 바이트 해시로 검증했다. `axis-transport-qualified.json`은 이 제한된 자격만 부여하며 `automatic_drivetrain_verified=false`, `in_motion_equivalence_verified=false`를 유지한다. 필터 등 다른 설정 차이는 이 자격으로 허용하지 않는다. 새 교사 수집은 허용하되 자동 변속의 모델 주행 반응은 학습 후 별도 진단으로 검증한다.

교사 수집 시작 조건은 사용자 요청으로 출발선 직전 정차에서 **핫랩 세션 시작점**으로 변경했다. `GamepadIO/teacher-hotlap.toml`로 같은 세션 프리셋에서 첫 코너 탈출까지 세 번 기록한다. 세션 재시작은 기록 사이에 수동으로 수행한다. 첫 두 회는 학습, 마지막 회는 독립 검증으로 고정하며 시작 속도·기어·위치를 실제 관측으로 비교한다. 핫랩이 이동 상태로 시작한다면 정차 출발과 같다고 취급하지 않는다. 모델 시험의 기존 정차 준비 조건을 해제한 것은 아니므로 실제 핫랩 초기 상태를 확인한 뒤 일치시키는 작업이 남아 있다.
