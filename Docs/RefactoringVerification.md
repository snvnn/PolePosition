# 리팩토링 오프라인 검증

2026-11-09. 실제 ACC 실행·주행·온라인 학습 없이 실행 계층의 구조와 기존 동작을 검증한다. 실제 주행 검증을 통과했다는 의미가 아니다.

## 기본 검사

프로젝트 루트에서 실행한다.

```powershell
.\.venv-training\Scripts\python.exe -X utf8 -m Checks
.\.venv-training\Scripts\python.exe -X utf8 -m Checks --suite unit
.\.venv-training\Scripts\python.exe -X utf8 -m Checks --suite mock_process
.\.venv-training\Scripts\python.exe -X utf8 -m Checks --list
cargo test --workspace --locked --offline --lib
cargo check --workspace --all-targets --locked --offline
```

`Checks`는 검토한 테스트만 명시적으로 선택한다. 기본 Python 목록에서는 실제 게임 프로세스 어댑터와 Windows 메뉴 어댑터의 생성을 차단한다. fixture가 주입을 빠뜨리면 실게임에 연결하는 대신 실패한다. 검사 결과가 추가된다고 실게임 실행이 승인되지는 않는다.

| 분류 | 내용 | 기본 실행 |
| --- | --- | --- |
| unit | 명령 계약, 취소·시간 제한, 경험·정차 검증, 모의 학습·비교·복귀, 임시 파일 원자적 게시 | 포함 |
| mock_process | 제어된 Python 자식 프로세스, 임시 합성 데이터 worker, 숨긴 Tk 창의 작업·결과 상태 | 포함 |
| local_assets | 저장된 모델·기록, native binary parity, 기존 GUI 전체 workflow·사전학습 fixture | 제외, 목록만 제공 |
| device | 설치된 드라이버 검사와 수동 bench·응답 검사·실주행 | 제외 |

기존 Python 테스트는 같은 네 분류에 포함한다. 실행 계층 회귀의 기본 목록에 포함하지 않은 데이터 처리 등 영역별 단위 테스트도 `--list`에 `unit`으로 별도 표시한다. 새 테스트 모듈은 분류를 등록해야 목록 검사가 통과한다. 전용 가상환경이 필요한 SAC 검사는 `local_assets`에 포함한다. Rust의 기존 `#[ignore]` 장치·실제 기록 검사는 해제하지 않는다.

## 고정한 계약과 시나리오

- `task_contract.json`은 변경 전 명령 생성 결과와 오류 메시지를 192개 입력 조합으로 고정했다. 작업 종류, 재개, 0/7200/43200초 경계, 잘못된 시간·단계·epoch를 검사한다. 임시 경로만 정규화하며 argv의 나머지 값과 순서는 비교한다.
- 동일 Control/ManagedProcess의 기존 import, worker 종료 이벤트, heartbeat·EOF·오류·사용자 중지, GUI 잠금과 체크포인트 선택을 검사한다.
- 절대 시각 도달과 시계 역행에도 시간 예산이 연장되지 않는지, 실제 드라이버 없이 native 핸들 수명과 시작 실패 정리가 가능한지 검사한다.
- 학습·비교 fixture는 native·메뉴 복귀를 명시적으로 주입한다. 중지·불완전 응답·복귀 실패 뒤 다음 주행 금지, exact resume, 동결 평가와 승격 차단을 검사한다.
- 원자적 파일 게시의 부분 노출·덮어쓰기 금지·해시 확인과 관측→정차→중립→해제의 기존 증거 요구를 유지한다.
- 공용 fixture에는 `TestCase`가 없다. 테스트 모듈에서 다른 테스트 클래스를 가져와 중복 수집하지 않는다.

## 환경 조건과 보존

하드링크 기반 게시 테스트는 Windows의 `os.link`가 허용되는 임시 디렉터리가 필요하다. 제한된 샌드박스의 `WinError 5`는 환경 제약으로 기록하고 같은 테스트를 허용된 환경에서 실행한다. 게시를 일반 쓰기로 바꾸거나 해당 테스트를 건너뛰지 않는다.

이 작업 환경에서는 Rust가 기본 PATH에 없었으며 보관된 `1.95.0-x86_64-pc-windows-msvc` 도구와 기존 Cargo 캐시, 설치된 MSVC Build Tools를 명시적으로 지정해 오프라인 검증했다. 전역 PATH·도구체인·패키지는 변경하지 않았다. 일반 개발 환경에서는 `rust-toolchain.toml`의 버전을 사용한다.

`TrainingEnvironment/requirements.txt`의 Pillow 11.3.0과 lock 파일의 12.3.0이 불일치한다. 이번 구조 변경에서는 어느 버전도 변경·설치하지 않았다. 환경 재현성 수정은 별도 변경으로 다룬다.

기존 미커밋 변경, 모델·경험·체크포인트·실행 프리셋과 과거 검증 기록을 보존한다. 전역 포맷 변경이나 자동 커밋은 하지 않는다. 최신 검사 결과는 아래에 범위와 함께 기록한다.

## 2026-11-09 결과

- 기본 Python 오프라인 검사: 141개 통과. 명령 계약 테스트 안에서 192개 입력 조합을 확인했다.
- 추가 합성 데이터·GUI workflow 검사: `.venv-training`에서 16개 통과. SAC 사전학습 검사 3개는 해당 작업의 `.venv-controller`에서 통과. `.venv-training`에 `gymnasium`이 없어 발생한 초기 import 오류는 환경을 바꿔 확인했으며 패키지를 설치하지 않았다.
- 기록·데이터셋·경로·상태 처리의 공용 fixture 검사: 58개 통과. `DrivingRecorder.test_validate_recording`, `DatasetBuilder.test_builder`, `TrackModel.test_model`, `StateProcessor.test_state`를 함께 실행했다. 기록 검증 테스트를 데이터셋 테스트가 다시 상속하지 않으며 각 테스트는 원래 모듈에서 한 번씩 수집된다. 이번 작업에서 통과한 Python 검사는 합계 218개다.
- Rust 워크스페이스 라이브러리: 121개 통과, 기존 로컬 자산·설치 드라이버 의존 검사 3개 제외. `cargo check --workspace --all-targets --locked --offline` 통과.
- `clippy -- -D warnings`는 기존 경고로 미통과. 워크스페이스에서는 변경하지 않은 `GamepadIO/src/profile.rs`의 `nonminimal_bool`, `intervention.rs`의 `identity_op`가 먼저 보고됐다. actuator 단독 검사에서도 기존 core/rl/episode 코드와 그대로 이동한 주행·진단 코드의 스타일 경고가 남아 있다. 경고 억제나 무관한 코드 수정은 하지 않았다. 로그: `target/refactor-clippy.log`, `target/refactor-actuator-clippy.log`.
- 게임패드 rustfmt check는 보존한 변경 전 소스와 분할 후 소스 모두 미통과. 기존 압축 표기를 유지했으며 전역 포맷 변경을 섞지 않았다. 로그: `target/refactor-format-baseline.log`, `target/refactor-format-current.log`.
- 기존 설정·프리셋 66개를 작업 전 사본과 바이트 비교해 변경 없음을 확인했다. 이동한 관리 프로토콜·프로세스 구현과 Rust 주행 루프·감독 루프도 원문 일치를 확인했다.
- 실게임 검증: 수행하지 않음. 실행 보류 유지.
