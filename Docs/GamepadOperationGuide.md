# 게임패드 수집과 교정 운영 안내

2026-10-18 기준. 실제 Xbox 입력 수집, ViGEm 모델 시험, HidHide를 사용한 사람 교정의 재개 절차다. 실행 조합은 [모델과 설정](CurrentGamepadModels.md), 학습은 [학습 규약](GamepadTrainingSpecification.md)을 따른다.

## 작업에 맞는 입력 경로

| 작업 | 실제 패드 | ACC가 읽는 장치 | HidHide |
| --- | --- | --- | --- |
| 교사 기록·실제 응답 검사 | 연결, 실제 슬롯 선택 | 실제 패드 | 실제 패드가 ACC와 기록기에 보이는 상태 |
| 개입 없는 모델 시험 | 분리 | 가상 Xbox 360 패드 | 실제 패드 노출 없음 확인 |
| 교정 주행 | 연결 | 가상 패드만 | 실제 패드는 제어 실행 파일만 읽도록 숨김 |

휠용 vJoy 보정과 게임패드 프로필을 혼용하지 않는다. 게임패드 프로필은 ACC 왼쪽 스틱 모드, 자동 클러치, Steam Input 비활성 근거와 controls/assists·드라이버·DLL 해시를 고정한다. 학습·모델 시험의 `verified_device()`는 자동 변속과 실제·가상 응답 검증도 요구한다. 수동 N 기어 응답 진단의 통과만으로 학습용 프로필이 되지는 않는다.

## 교사 기록과 연속 주행

1. ACC가 종료되거나 주행을 끝낸 상태에서 controls/assists와 HidHide 상태를 백업한다. 실제 패드 수동 수집은 숨김 상태를 확인해 ACC와 기록기가 같은 실제 입력을 읽을 수 있게 준비한다. 숨김을 바꾸었다면 ACC를 다시 실행한다.
2. GUI의 `게임패드 연결 확인`으로 슬롯 0~3 중 실제 스틱 반응을 확인한다. `게임패드 교사 기록`에서 그 슬롯과 프로필을 선택한다.
3. 짧은 핫랩 수집은 [teacher-hotlap.toml](../GamepadIO/teacher-hotlap.toml)로 동일 시작 프리셋을 사용하고 기록 사이에 수동 재시작한다. 연속 수집은 [teacher-continuous.toml](../GamepadIO/teacher-continuous.toml)과 [30분 프리셋](../TrainingController/gamepad-continuous-preset.json)을 사용한다. 전체 시간 1800초에는 준비 대기가 포함된다.
4. 선택한 스틱을 움직여 장치를 확인하고 버튼을 놓은 뒤, ACC 전면에서 F7을 누른다. 수집 중 F8은 기록을 종료한다. 기록기는 차량 입력을 생성하지 않는다.
5. 저장 종료 후 `metadata.json`, `summary.json`, `telemetry.csv`, `inputs.csv`, `events.csv`, 두 설정 스냅샷을 확인한다. [기록 검증기](../DrivingRecorder/validate_recording.py)와 랩 경계·학습 채택 검사를 적용한다.

연속 세션의 정상 랩 사이에는 재시작하지 않는다. 연료와 타이어 상태는 자연스럽게 변하며 매 랩이 핫랩 최초 상태와 같다고 가정하지 않는다. 완전한 랩은 출발선 사이의 동일 구간·연속 관측을 사용하고, 부분·무효 랩의 사용 여부는 학습 설정과 선별 근거에 따른다. 랩 split은 같은 세션의 인접 랩을 공유할 수 있지만 세션 split은 기록 세션 전체를 고정한다. [자료 선별과 보관](DataArchiveGuide.md)을 참조한다.

## HidHide 교정 준비

설정과 검사 구현은 [isolation.py](../GamepadIO/isolation.py), 현재 로컬 프로필은 [isolation.json](../input_profiles/gamepad/isolation.json)이다. 설치 이력은 [10월 8일 개발 기록](TrainingControllerDevelopmentLog.md)에 있다.

1. ACC를 종료하고 실제 패드의 HID 경로와 XboxComposite 부모를 확인한다. 프로필에 저장된 특정 장치 경로를 사용한다.
2. HidHide에서 숨김을 켜고 inverse mode는 끈다. `poleposition-gamepad.exe`를 허용한다. ACC 실행 파일과 독립 `poleposition-gamepad-probe.exe`는 허용 목록에 넣지 않는다.
3. 허용 실행 파일에서는 실제 패드가 보이고, 독립 프로브에서는 실제 패드가 보이지 않아야 한다. 교정 준비 전 일반 프로그램에 보이는 슬롯은 0개, 가상 장치 생성 중에는 선택된 가상 슬롯 하나여야 한다.
4. 아래 읽기 전용 검사가 통과하는지 확인하고, 필요한 동시 장치 검사는 ACC가 종료된 상태에서 수행한다. 동시 검사는 가상 장치를 생성하고 STOP→NEUTRAL 응답과 해제를 검사한다.

```powershell
.\.venv-training\Scripts\python.exe -m GamepadIO.isolation --config input_profiles/gamepad/isolation.json --preflight
# 동시 장치 검사. 첫 인자는 교정 TOML, 두 번째는 새 결과 JSON 경로.
.\target\release\poleposition-gamepad.exe --intervention-bench GamepadIO/intervention-lpf-off.toml input_profiles/gamepad/intervention-bench-new.json
```

5. 숨김 설정 이후 ACC를 새로 실행한다. 기존 장치 핸들이 남을 수 있어 준비 검사는 ACC 시작 시각도 대조한다. 가상 슬롯은 장치 생성 전후의 유일한 새 XInput 슬롯과 STOP→NEUTRAL 응답으로 식별한다. 실제 슬롯 번호와 같거나 항상 0이라고 가정하지 않는다.

재빌드 후에는 허용 실행 파일과 허용되지 않은 독립 프로브가 같은 바이너리인지 확인해 복사본을 갱신하고 두 SHA256을 함께 갱신한다. CLI·HidHide 드라이버 해시도 실제 설치본과 대조한다. 드라이버나 설정이 바뀐 이유를 확인한 뒤 새 검사 근거를 보존한다. 프로필의 해시 값만 바꿔 기존 검증을 통과한 것으로 취급하지 않는다.

## 모델 시험과 사람 개입

모델·프로필·prepared 경로의 완료·해시·수치 대조 및 설정 일치 검사를 거친다. 현재 기본 교정 60초 설정은 사용할 수 없으므로 [허용 조합](CurrentGamepadModels.md)에서 짧은 시험을 고른다. 개입 없는 진단의 10·30초는 같은 모델·프로필의 직전 5·10초 통과 결과가 필요하다. 개입 모드는 이 자동 단계 검사를 적용하지 않으므로 실행 순서와 결과를 별도로 확인한다.

| 조작 | 동작과 정답 마스크 |
| --- | --- |
| LB 유지 | 사람 조향, 모델 가속·제동. `[1,0,0]` |
| RB 상승 에지 | 전체 사람 제어를 켜거나 끔. 켜진 동안 `[1,1,1]` |
| RB 유지·버튼 해제 | 토글 추가 발생 없음. 해제 중에도 켜진 전체 제어 유지 |
| Back 또는 F8 | 중지. 비상 입력은 학습 정답으로 채택하지 않음 |
| A/X | 현재 검증된 GearUp/GearDown 바인딩. ACC 전면·입력 연결·준비 대기/주행에서만 전달 |
| 개입 없음 | 모델 제어. 교정 정답 마스크 `[0,0,0]` |

`rb_toggle_v1`은 원시 입력당 한 번 버튼 에지를 처리한다. 준비 대기·새 시험·Back에서 토글을 초기화한다. 이전 hold 방식 기록은 별도 규칙으로 읽으며, 교정 학습기는 원시 버튼과 정책의 제어권을 재구성해 검증한다. 전체 사람 제어가 LB보다 우선하고 Back·F8·감시기 중지가 최우선이다.

정상 시간 종료의 제동 중에는 켜져 있는 사람 토글 또는 LB로 조향할 수 있지만 제동은 감시기가 유지한다. 중지·취소·오류와 중립화에서는 기어 버튼을 비운다. 가상 장치는 준비 후 존재하므로 GUI를 준비하지 않았거나 종료한 상태에서는 A/X를 전달하지 않는다. 완전히 정차하고 해제가 끝난 뒤 세션을 재시작한다.

## 조향 필터와 출발선 보정

`steering_smoothing_ms`는 모델 조향의 1차 저역 통과 시간상수이며 0~100ms를 허용한다. 0은 끔이다. `alpha = 1 - exp(-dt/tau)`로 실제 경과 시간을 사용한다. 최초 표본, 비증가 시각 또는 100ms 이상 공백에서는 요청값으로 초기화한다. 사람 조작은 필터를 우회하고 상태를 갱신하며, 중지·비상 명령도 필터를 거치지 않는다. 원시 모델 `action`, 필터 후 `command_action`, 사람 선택 후 `selected_action`을 구분해 기록한다.

5초 A/B는 [필터 끔](../GamepadIO/intervention-lpf-off.toml)과 [30ms 필터](../GamepadIO/intervention-lpf-on.toml)이며 10·30초 설정도 있다. 같은 모델·설정·출발 상태에서 쌍으로 비교한다. 30ms 시간상수의 수학적 90% 응답 약 69.1ms는 차량 반응 지연 측정값이 아니다. `seam_bridge_15m_v1` 특징 보정과 실제 A/B 증거·미확정 상태는 [실험 검토](GamepadExperimentReview.md)에 둔다.

## 실패 진단과 복원

| 현상 | 확인과 재개 순서 |
| --- | --- |
| 재부팅 후 실제 패드가 없음 | ACC 종료 → 기존 숨김 상태 백업 → 해당 패드 숨김 해제 → USB 재연결 → 실제 XInput 확인 → 숨김 재활성화 → 독립 프로브/동시 검사 → ACC 새 실행. 자동 복구는 미구현 |
| HidHideCLI 시간 초과 | Configuration Client를 닫고 `isolation-last-error.json` 확인. 현재 하위 CLI는 stdin을 DEVNULL로 열어 관리 파이프 EOF 대기를 방지함. 검사 제한은 준비 3초·실행 중 1초, ACC 시작 시각 조회 6초 |
| controls 해시 불일치 | `controls_reference` 원본 해시와 비교. 외부 `commandDevices` 열거 순서만 달라지면 동등성 허용, 감마·축/버튼 배열 순서·장치 추가/삭제·그 외 값 변경은 차단 |
| 가상 슬롯 충돌·분리 실패 | 실패 결과 보존 → 시험 중지·장치 해제 확인 → 일반 프로브에 가상 패드가 남는지 검사 → 필요 시 재부팅 후 동시 검사부터 재개 |
| 중립 변속 중 명령 만료 | 원시 gear와 최신 특징 생성 확인. gamepad는 N=1을 보존하고 wheel v2는 전진 전용. F7 시작은 여전히 전진 기어를 요구함 |

교정 종료 후 수동 실제 패드 운전으로 돌아가려면 가상 장치 해제를 확인하고 ACC를 종료한 뒤 백업한 HidHide 상태와 ACC 설정으로 복원한다. 해당 실제 패드가 일반 프로그램에 다시 보이는지 확인하고 ACC를 새로 실행해 스틱·두 트리거를 확인한다. 설정이 달라진 이후 모델 시험은 새 설정 검증을 거친다. 과거 드라이버 설치/복구 기록은 [차량 입력 개발 기록](VehicleInputDevelopmentLog.md)과 [Xbox 조사](XboxUsbInputInvestigation.md)에 보존한다.
