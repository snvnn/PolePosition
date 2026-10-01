# Xbox USB 입력 불능: 원인 가능성 조사

작성: 2026-09-29. 사용자의 요청으로 복구 조작을 중단하고 기존 기록·읽기 전용 조회·공식 문서와 개발자 저장소의 직접 보고를 조사했다. 아래 우선순위는 원인 확률이나 확정 진단이 아니라 현재 증상과의 부합 정도다.

## 가장 중요한 관측

- USB 패드 본체는 Windows에 열거되며 Microsoft의 `dc1-controller.inf`를 사용한다. 드라이버 실행과 문제 코드 0만으로 게임 입력이 정상임을 보장할 수 없다.
- 23:38경 사용자는 USB 단독 조건의 Xbox 액세서리 앱에서 패드 그림·구성 버튼과 키 입력을 확인했다. [사용자 확인](../input_profiles/xbox-accessories-user-confirmation-20260929.json)
- 같은 시기 별도 XInput 1.4 조회는 [90초·893회 모두 연결 실패](../input_profiles/xbox-accessories-recovery-monitor-1790692724066073200.json)였다. XInput 9.1.0 조회도 실패했고, 사용자는 이후 ACC가 처음부터 입력을 받지 않는다고 회신했다.
- 공식 앱이 사용하는 내부 API는 확인하지 않았다. 따라서 공식 앱=GameInput, ACC=XInput이라고 단정하지 않는다. 확인된 차이는 프로그램별 인식·입력 성공 여부다.
- 장치 재등록 이후 공식 앱 입력은 다시 확인하지 않았다. 이전 사용자 확인을 최신 시점의 모든 입력 경로 정상으로 확대하지 않는다.

## 가능한 원인과 근거

| 가능성 | 현재 평가 | 부합하는 근거 | 제한·반대 근거 |
|---|---|---|---|
| Windows 게임패드 노출·입력 호환 경로의 문제 | 우선 조사 범위 | 공식 앱에서 키 입력이 읽혔지만 XInput·Steam·ACC는 실패했다. 여러 입력 API·드라이버 인터페이스의 차이가 존재한다. | 범위만 좁혀졌을 뿐 고장 난 구성요소나 설정값은 확인하지 못했다. |
| vJoy 설치 뒤 장치 등록·열거 또는 호환성 변화 | 남아 있는 주요 후보 | 가상 입력 장치 설치·시험 이후 문제를 확인했고, 상류 vJoy 저장소에도 설치 후 Xbox 패드가 게임에서 사라졌다는 직접 보고가 있다. | 해당 보고는 다른 배포·환경이다. 이 PC에서는 vJoy 임시 비활성화로 복구되지 않았다. 설치가 남긴 효과까지 배제한 시험은 아니다. |
| Windows GameInput 구성·호환 계층 문제 | 가능성 있음, 직접 증거 부족 | Microsoft는 GameInput 서비스가 장치 접근·필터링·호환 계층을 담당한다고 설명한다. 이 PC에는 기본 서비스와 재배포 서비스가 모두 실행돼 있었다. | 두 서비스의 존재나 버전 차이만으로 충돌이라고 판단할 수 없다. 해당 서비스의 오류나 원인 관계는 확인되지 않았다. XboxGipSvc는 다른 서비스이며 이를 시작해도 복구되지 않았다. |
| 패드 펌웨어와 USB 드라이버 사이의 호환 문제 | 미확인 후보 | 공식 앱 통신과 게임용 입력 기능이 모두 같은 방식으로 검증된 것은 아니다. | 현재 패드 펌웨어 버전·최근 변경 이력을 확보하지 못했다. 업데이트가 필요하거나 해결한다는 근거는 없다. |
| Steam Input 또는 ACC 설정만의 문제 | 단독 원인으로는 우선순위 낮음 | Steam은 게임 입력 API를 가로채 가상 입력을 전달할 수 있다. 실제 Steam이 이전 장치 재시작을 막은 기록도 있다. | Steam·ACC 종료 중에도 XInput이 실패했다. ACC 원본 설정은 복원됐다. Steam의 복구 작업 방해와 최초 입력 불량 원인은 별개다. |
| USB 케이블·포트·패드의 물리적 문제 | 완전 단선·전원 전용 케이블 설명은 약해짐 | 처음에는 연결 해제 알림과 유선 입력 실패가 있었다. 간헐적 문제는 남는다. | USB 단독 공식 앱에서 실제 키 입력을 확인했다. 다른 USB 루트 경로로 이동해도 게임 입력 실패는 유지됐다. 다른 PC·케이블과의 완전한 교차 검증은 없다. |
| HidHide/HidGuardian 등의 입력 숨김 필터 | 조사한 범위에서는 근거 낮음 | 일부 앱만 보이는 증상과 형태가 비슷할 수 있다. | 관련 드라이버를 발견하지 못했고 HIDClass·XboxComposite·XnaComposite 클래스의 UpperFilters/LowerFilters도 비어 있었다. Xbox 장치 자체의 정상 xboxgip 상위 필터와 혼동하지 않는다. |

Microsoft의 [입력 장치 인터페이스 설명](https://learn.microsoft.com/en-us/gaming/gdk/docs/features/common/input/hardware/input-hardware-interfaces?view=gdk-2604)과 [XUSB/DirectInput 설명](https://learn.microsoft.com/en-us/windows/win32/xinput/directinput-and-xusb-devices)은 장치와 API 사이에 여러 인터페이스·매핑이 있음을 보여 준다. 여기서 특정 드라이버 결함이나 공식 앱의 내부 구현을 추정해 확정하지 않는다.

vJoy 관련 근거는 [상류 저장소의 Xbox 미인식 보고 #71](https://github.com/shauleiz/vJoy/issues/71)다. 이 보고는 사용자 경험이며 BrunnerInnovation 2.2.2.0의 확정 결함 공지가 아니다. 현재 배포의 [공식 안내](https://github.com/BrunnerInnovation/vJoy/releases/tag/v2.2.2.0)는 Windows 10/11 지원을 명시한다. 확인한 [레지스트리 관련 #7](https://github.com/BrunnerInnovation/vJoy/issues/7)은 SDK DLL 위치 값, [식별자 관련 #4](https://github.com/BrunnerInnovation/vJoy/issues/4)는 여러 vJoy 인스턴스 구분 문제를 다룬다. 제목만으로 이번 Xbox 입력 불능과 같은 원인으로 분류하지 않는다.

[GameInput 공식 FAQ](https://learn.microsoft.com/en-us/gaming/gdk/docs/features/common/input/overviews/input-faq?view=gdk-2510), [Steam 입력 에뮬레이션 설명](https://partner.steamgames.com/doc/features/steam_controller/steam_input_gamepad_emulation_bestpractices), [HidHide 개발자의 필터 확인 설명](https://github.com/nefarius/HidHide/discussions/135)을 참고했다. 인터넷의 유사 증상과 이 PC에서 재현한 결과를 구분한다.

## 기존 가설 중 약해진 것

- **빠른 시작이 유일한 원인:** 23:31:01의 완전 재시작 후에도 실패했다. [시스템 근거](../input_profiles/xbox-full-restart-system-20260929.json)
- **중간 USB 허브가 유일한 원인:** 다른 루트 허브에 직결한 뒤에도 실패했다. [뒷면 포트 근거](../input_profiles/xbox-rear-port-20260929.json)
- **장치 재시작·등록 초기화만으로 해결:** Steam 종료 후 재시작과 장치 재등록은 성공했지만 XInput은 실패했다. [재등록](../input_profiles/xbox-usb-reregister-20260929/result.json), [입력 조회](../input_profiles/xbox-usb-reregister-20260929/xinput.json)
- **XboxGipSvc가 단순히 중지되어 발생:** 사용자 중단 요청 이전에 해당 서비스만 일시 시작했으나 100회 조회 모두 실패했다. 효과가 없어 기존 Stopped/Manual 상태로 돌렸다. [시험 결과](../input_profiles/xbox-service-check-20260929/result.json)
- **오늘 Windows 업데이트가 Xbox 드라이버를 교체:** SetupAPI 로그의 해당 업데이트 구간은 **2026-09-11 20:11**이다. 시간만 읽어 9월 29일 변경으로 오인하면 안 된다. 이 정보만으로 기존 드라이버 자체의 호환성까지 배제하지는 않는다.

## 조사 종료 시점의 결론과 중단 상태

현재 증상은 Windows의 게임패드 노출·입력 호환 경로 문제와 가장 잘 부합한다. 그 원인 후보로 vJoy 설치 영향, Windows 입력 구성, 펌웨어 호환성을 구분해 남긴다. vJoy 원인 확정, 패드 고장 확정, 펌웨어 업데이트 필요 확정은 하지 않는다.

사용자가 복구 시도를 중단하도록 요청한 뒤에는 추가 설치·장치 재시작·서비스 변경·펌웨어 변경을 수행하지 않았다. 읽기 전용 조사와 기록만 남겼다. 마지막 서비스 시험은 중단 요청 전에 끝났으며 기존 상태 복원을 확인했다. 공식 Xbox 액세서리 앱은 설치된 상태이고 vJoy는 다시 활성화된 상태다. USB 패드의 ACC 복원 검증과 차량 입력 연결 전체 목표는 미완료로 유지한다.

## 후속 사용자 요청: vJoy 제거 비교 — 2026-09-29~30

사용자가 vJoy를 제거하고 비교하자고 요청하여 해당 범위만 재개했다. 기존 조사 중단 이후의 별도 승인된 작업이며, 다른 드라이버·서비스·펌웨어 수정을 재개한 것은 아니다.

- vJoy 서비스 설정·제거 등록과 ACC의 controls/assists·별도 휠 프리셋을 [비교 폴더](../input_profiles/vjoy-uninstall-20260929-compare01/)에 보존했다. 제거 직전 XInput 1.4/9.1.0 네 슬롯을 100회 조회했으며 성공은 없었다.
- 9월 29일 23:56에 등록된 vJoy 2.2.2.0 제거 프로그램을 실행했다. [실행 결과](../input_profiles/vjoy-uninstall-20260929-compare01/uninstall-result.json)는 코드 0, [제거 로그](../input_profiles/vjoy-uninstall-20260929-compare01/uninstall.log)는 성공·재부팅 요구 없음이다. 자동 재부팅을 금지했다. 사용자는 해당 시점 장치 연결 해제·재연결 소리를 들었다고 알렸으나, 이를 Xbox 입력 회복의 증거로 해석하지 않는다.
- 가상 장치와 프로그램 등록 및 게시된 `oem5.inf`는 없어졌고 드라이버는 중지됐다. 그러나 `vjoy` 서비스 등록과 `System32/drivers/vjoy.sys` 파일이 남아 있다. 이를 모든 잔여 구성까지 제거한 상태라고 표현하지 않는다. 수동 레지스트리·드라이버 파일 삭제는 하지 않았다.
- [제거 직후 비교](../input_profiles/vjoy-uninstall-20260929-compare01/comparison.json): 약 34초 동안 300회 조회 모두 연결 성공 없음. ACC 설정 세 파일의 해시는 제거 전과 같다.
- 사용자가 재부팅한 뒤 커널 시작 시각이 23:31:01에서 **23:58:50**으로 바뀐 것을 확인했다. [재부팅 후 비교](../input_profiles/vjoy-uninstall-20260929-compare01/post-restart-comparison.json): Steam·ACC·액세서리 앱 없음, vJoy 장치 없음, 드라이버 Stopped/1077(이번 부팅에서 실행되지 않음), 100회 조회 모두 연결 성공 없음. Xbox USB 본체는 여전히 열거된다. ACC 설정 해시는 유지됐다.

**갱신된 결론:** 등록된 제거 프로그램 실행과 재부팅으로도 XInput 회복은 관측하지 못했다. 현재 실행 중인 vJoy와의 단순한 동시 동작 충돌 가능성은 낮아졌지만 설치가 남긴 영향까지 배제하지 않는다. 게임에서의 패드 복원은 미완료이며, 이번 비교를 근거로 다른 원인을 확정하지 않는다.

## Xbox 컨트롤러 드라이버 삭제 요청 — 2026-09-30

사용자가 Xbox 컨트롤러 드라이버의 완전 삭제를 요청했고 Steam 종료·USB 분리를 확인했다. 기존 장치의 부모 관계·컨테이너·INF를 대조하고 드라이버 파일을 복사해 해시를 검증한 뒤, 해당 USB 패드 본체와 하위 USB/HID 입력 장치 2개의 등록을 제거했다. 세 작업 모두 코드 0이며 [독립 재조회](../input_profiles/xbox-driver-removal-20260930/verification.json)에서도 세 등록은 없다.

[실행 결과](../input_profiles/xbox-driver-removal-20260930/result.json)의 DISM 조회는 `dc1-controller.inf`, Microsoft, 버전 `10.0.19041.7725`, **Inbox=true**를 확인했다. `pnputil /delete-driver dc1-controller.inf /uninstall`은 “지정한 파일은 설치되지 않은 OEM INF입니다”라는 이유로 패키지 삭제를 거부했다. INF·DriverStore 패키지·드라이버 바이너리는 남아 있고 바이너리 해시도 유지됐다. 출력 중 “패키지를 제거했습니다”라는 문장만을 완전 삭제 성공으로 해석하지 않는다.

결과는 **장치 등록 3개 제거 완료, Windows 기본 제공 드라이버 패키지 완전 삭제 미완료**다. Microsoft의 [PnPUtil 규약](https://learn.microsoft.com/en-us/windows-hardware/drivers/devtest/pnputil-command-syntax)과 [장치·패키지 제거 설명](https://learn.microsoft.com/en-us/windows-hardware/drivers/install/how-devices-and-driver-packages-are-uninstalled)에 따른 표준 제거만 시도했다. 파일 소유권 변경·DriverStore 수동 삭제·공용 `input.inf` 삭제·Windows Update 설정 변경은 하지 않았다. 자동 재검색·재설치도 하지 않았으며, 패드 재연결 후 입력 복구는 확인하지 않았다.

## vJoy 재설치 중 입력 상실 재현 — 2026-09-30

이후 사용자는 컨트롤러 복구를 확인하고 같은 vJoy의 재설치를 요청했다. ACC·Steam 종료, 블루투스 해제, USB 패드 연결 유지를 확인받아 비교했다. 서명 Valid·Brunner Elektronik AG와 기존 고정 SHA-256이 일치하는 2.2.2.0 설치 파일을 사용했고 자동 재부팅을 금지했다.

| 관측 | 설치 전 | 설치 중 장치 생성 후 |
| --- | --- | --- |
| XInput 1.4 연결 성공 | 100/100 | 0/100 |
| XInput 9.1.0 연결 성공 | 100/100 | 0/100 |
| 같은 Xbox HID 하위 장치의 INF | `xinputhid.inf` | `input.inf` |
| 장치 표시 | XINPUT 호환 HID 장치 | HID 규격 게임 컨트롤러 |

[설치 전](../input_profiles/vjoy-reinstall-20260930/before.json), [설치 중](../input_profiles/vjoy-reinstall-20260930/after.json), [비교·소스 해시](../input_profiles/vjoy-reinstall-20260930/reproduction.json)를 보존했다. `after.json`이라는 파일명은 설치 완료를 뜻하지 않는다. 관측 당시 설치 프로그램은 드라이버 갱신 단계에서 대기 중이었다. ACC 설정과 Xbox 드라이버 바이너리 해시는 두 관측 사이 동일했다. 바뀐 것은 동일 Xbox HID 장치에 선택된 드라이버다.

공식 2.2.2.0 소스의 [vJoyInstall.h](../input_profiles/vjoy-reinstall-20260930/vJoyInstall.h)는 `COMPATIBLE_ID`를 `hid_device_system_game`으로 정의한다. [vJoyInstallLib.cpp](../input_profiles/vjoy-reinstall-20260930/vJoyInstallLib.cpp)의 `Installation()`은 1193행에서 `cmdUpdateNI(INSTALLFLAG_FORCE, Input.inf, COMPATIBLE_ID)`를 호출한다. 이 경로는 최종적으로 `UpdateDriverForPlugAndPlayDevices`를 사용한다. Windows SetupAPI 로그에도 설치 프로그램이 이 식별자를 대상으로 갱신을 시작한 기록이 있다. [상류 소스](https://github.com/BrunnerInnovation/vJoy/blob/v2.2.2.0/apps/vJoyInstall/vJoyInstallLib.cpp#L1193).

**현재 근거는 단순한 의심을 넘어, 이 PC에서 vJoy 재설치 중 Xbox HID 드라이버 연결 변경과 입력 상실이 재현됐다는 것이다.** 앞선 vJoy 비활성화·제거만으로 회복되지 않은 현상도 이미 바뀐 Xbox 장치 연결이 남았다는 설명과 부합한다. 모든 PC·버전의 동일 결함으로 일반화하지 않는다. 설치 종료와 Xbox 입력 연결 복원은 아직 확인 전이며, 사용자가 설치 관련 확인 창이 있는지 확인 중이다.

### 이후 설치 마무리

사용자는 재시작 안내를 보지 못했다고 회신했다. 멈춘 설치 프로세스를 종료했고, 첫 설치는 정상 완료로 처리하지 않았다. 이후 Xbox USB 분리를 확인하고 영향을 받은 HID 하위 장치 등록 하나를 제거한 다음 동일 설치 파일을 다시 실행했다.

9월 30일 00:58:51에 [설치 정상 완료](../input_profiles/vjoy-reinstall-20260930/disconnected-install-result.json)를 확인했다. 종료 코드 0·재부팅 요구 없음, vJoy 장치 정상 상태다. [읽기 전용 검사](../input_profiles/vjoy-reinstall-20260930/vjoy-probe.json)도 드라이버/SDK API 0x0222 일치와 장치 1의 X/Y/Z 축 범위를 확인했다. 재설치 중 차량 입력을 보내지 않았다. Xbox 재연결 뒤 입력 복구는 후속 관측 대상으로 남긴다.

### USB 재연결 후 복구 확인

사용자가 USB 재연결을 회신한 뒤, 같은 Xbox HID 장치가 `xinputhid.inf`로 돌아왔고 [후속 관측](../input_profiles/vjoy-reinstall-20260930/after_repair.json)에서 XInput 1.4와 9.1.0 각각 **300/300 연결 성공**을 확인했다. 그동안 vJoy도 활성 상태였다. ACC 설정과 Xbox 드라이버 파일의 해시는 설치 전과 같았다. [전후 비교 집계](../input_profiles/vjoy-reinstall-20260930/completed-comparison.json).

이 PC에서는 **vJoy 설치 과정의 Xbox HID 드라이버 연결 변경으로 실패가 재현되고, 올바른 연결을 다시 만들자 vJoy가 존재하는 상태에서도 입력이 회복**됐다. 기본 드라이버 파일을 완전히 지우거나 다른 버전으로 바꿀 필요 없이 복구했다. USB 패드를 분리한 설치는 정상 완료됐다는 사실까지 확인했으며, 다른 환경에서의 보편적인 결과나 장기 안정성을 보장하지 않는다. ACC 사용자 반응 확인과 재설치 후 가상 입력 보정은 별도 검증 범위다.

### ACC 최종 확인

사용자는 Steam·ACC를 실행해 약 1분간 기존 게임패드 설정의 스틱·RT·LT를 확인한 뒤 **정상 입력 유지·연결 해제 없음**을 회신했다. [확인 기록](../input_profiles/vjoy-reinstall-20260930/acc-confirmation.json). 이번 재설치와 유선 패드 복원 검증은 완료했다. 자동 차량 입력은 보내지 않았으며 재설치 후 vJoy 축 중립값·기존 보정은 후속 자동 입력 전에 다시 확인한다.

ACC·Steam 실행 중에도 XInput 두 API 각각 300/300 연결 성공을 추가 확인했다. [최종 검증 집계](../input_profiles/vjoy-reinstall-20260930/final-verification.json)에 정상 설치·vJoy probe·원인 재현·입력 복구·사용자 확인을 연결했다.

이 PC의 재설치 절차는 **실제 패드 분리 → vJoy 설치 정상 종료 확인 → 필요 시 잘못 바뀐 해당 패드 HID 등록만 제거 → 패드 재연결 → `xinputhid.inf`·XInput·ACC 확인**으로 남긴다. 공용 HID 드라이버나 Xbox 기본 제공 드라이버 파일 전체 삭제는 이 복구에 필요하지 않았다.
