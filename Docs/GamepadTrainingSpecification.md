# 게임패드 학습과 특징 규약

2026-10-18 기준. XInput 원시 입력을 정답으로 사용하는 `gamepad_v1`의 자료 준비·학습·내보내기 규약이다. PID와 SAC 모델 형식은 아래 버전 표로 구분한다. [수집 절차](GamepadOperationGuide.md), [현재 모델](CurrentGamepadModels.md), [실험 결과](GamepadExperimentReview.md)를 함께 따른다.

## 학습 진입점과 방식 선택

GUI의 `게임패드 모방학습`과 `게임패드 교정 학습`은 모두 `.venv-training`의 `TrainingController.worker --task gamepad-train`을 통해 [gamepad.train](../TrainingEnvironment/gamepad.py)을 호출한다. 학습 방식은 작업 이름이 아니라 TOML 내용으로 결정한다. 현재 `python -m TrainingEnvironment`의 공개 하위 명령은 `prepare-teacher`뿐이다.

| 방식 | 설정 키와 예시 | split과 초기화 |
| --- | --- | --- |
| 초기 교사 3회 | `recordings` 3개, [gamepad-train.toml](../TrainingEnvironment/gamepad-train.toml) | 앞 2회 train·마지막 1회 validation, 다른 경로 및 telemetry 해시 필요 |
| 누적 교정 | 위 3회 + `parent_model`, `corrections`, `require_corrections`, [correction-train.toml](../TrainingEnvironment/correction-train.toml) | 기존 검증 기록 고정, 부모 가중치와 정규화 유지, 교정 마스크 적용 |
| 새 교사 실행 | `fresh_teacher_runs=true`, `train_runs`, `validation_run`, [fresh-hotlap-train.toml](../TrainingEnvironment/fresh-hotlap-train.toml) | 실행 전체로 분리, seed 42 새 가중치, train만 정규화 |
| 완전한 랩 | `lap_teacher_recordings=true`, `[[laps]]`의 recording/lap_index/split, [continuous-laps-train.toml](../TrainingEnvironment/continuous-laps-train.toml) | 현재 8랩 train·마지막 1랩 validation, 매 랩 이력 초기화 |
| 랩과 핫랩 혼합 | 랩 설정 + `hotlap_train_runs`, `hotlap_validation_run`, [mixed-hotlap-laps-train.toml](../TrainingEnvironment/mixed-hotlap-laps-train.toml) | 8랩 + 핫랩 5회로 train, 랩 validation이 epoch 선택, 핫랩 validation은 별도 진단 |
| 세션 | `session_teacher_recordings=true`, `session_split`, [session-teacher-20261017.toml](../TrainingEnvironment/session-teacher-20261017.toml) | 서로 다른 세션 전체로 train/validation 분리 |
| 증강 | 세션 설정 + `augmentation_dataset`, `variant`, `shared_normalization`, `weighted_sampling`, `epoch_samples`, 선택적 `noise` | 고정 validation, A 원자료의 공통 정규화·같은 초기 가중치·같은 갱신 예산 |

새 자료 방식은 `parent_model`, `corrections`, 초기 `recordings`와 혼용하면 거절한다. 여러 방식 플래그를 함께 켜는 사용법은 제공하지 않는다. 실제 분기는 세션 → 랩 → 새 교사 순서다. 예제의 `epochs`는 1~50이며 GUI의 일반 학습 epoch 상한과 별개다. 게임패드 학습은 `이어서 학습` 명령으로 재개하지 않고 누적 교정에서 부모 모델을 지정한다.

## 원시 자료와 정답의 시간 연결

[기록 형식](RecordingSpecification.md)에 따라 기록한 `telemetry.csv`와 `inputs.csv`를 사용한다. `format_pass`, 정상 저장·누락 0, `input_semantics=gamepad_v1`, `diagnostic_only=false`, `gamepad_training_eligible=true`, 차량·트랙, controls/assists 스냅샷과 프로필을 확인한다. `inputs.csv`는 연결·슬롯 고정, 연속 sequence, 순서 있는 읽기 구간, 정수 범위와 정규화의 일치를 추가 검사한다. 같은 XInput packet의 반복은 유지 입력으로 유효하다.

관측 시각 `t`에 대해 다음을 만족하는 행만 연결한다.

- 과거 입력은 `read_end_ns < t`인 가장 최근 행이며 나이는 100ms 미만이다.
- 정답은 `read_start_ns >= t`인 첫 입력이며 `read_end_ns - t <= 20ms`다.
- 양쪽 입력과 관측은 같은 segment이며 과거 입력 종료부터 정답 시작까지 20ms를 넘는 공백은 거절한다.
- 정답은 `[stick_x, throttle, brake]`의 실제 원시 입력이다. ACC의 `steering_raw`를 스틱 정답으로 바꾸지 않는다.

조향 정수는 음수일 때 32768, 양수일 때 32767로 나누며 트리거는 255로 나눈다. 스틱 범위는 -1~1, 두 트리거는 독립적인 0~1이다. 감마·데드존 보정이나 두 페달의 상호 배제를 추가하지 않는다. 읽기 시각은 호스트 시각이며 ACC가 입력을 적용한 시각은 아니다.

## 자료별 채택과 이력

초기 교사와 랩 방식은 유효한 관측만 채택한다. 완전한 랩은 [랩 선택기](../TrainingEnvironment/lap_gamepad.py)가 기본 검증의 경계와 `all_samples_lap_valid`를 확인한다. 중복 `(recording, lap_index)`를 거절하며 마지막 항목 하나만 validation이어야 한다. 같은 연속 세션의 인접 랩이므로 독립 세션 검증으로 표현하지 않는다.

[세션 방식](../TrainingEnvironment/session_gamepad.py)은 경로와 telemetry 내용의 중복을 거절한다. train은 `include_invalid=true`여서 무효 랩 관측을 포함할 수 있고 사람의 실수와 성공적인 복구를 자동 구별하지 않는다. validation은 유효 관측만 사용한다. 증강을 적용한 train은 유효→무효 전환의 지정 시간대를 제외하며, 그 구간과 겹치는 정답 읽기 구간도 제외한다. 전체 세션의 모든 행을 무조건 학습한다는 뜻은 아니다.

투영의 출발선 단절, segment 변경, 제외 구간 진입·이탈에서 tracking epoch와 이력 시작을 새로 설정한다. 과거 입력이 새 시작 이전이면 버리고 500ms 특징 이력을 다시 확보한다. 실제 관측 행을 최소 20ms 간격으로 선택하며, 증강의 0/10ms 위상은 각 20ms 예약에 도달한 실제 행을 선택한다. 없는 관측을 보간해 만들지 않고 여러 위상에 속하는 같은 행은 한 번만 저장한다.

[새 교사 방식](../TrainingEnvironment/fresh_gamepad.py)은 개입 로그의 전체 사람 제어 마스크 `[1,1,1]`만 사용하고 실행당 최소 256개를 요구한다. 모델 페달과 조향만 교정한 표본을 새 전체 행동 정답으로 사용하지 않는다. geometry·속도 기준은 관측 문맥으로 재사용하며 과거 모델의 행동을 정답으로 사용하지 않는다.

누적 교정은 [corrections.py](../TrainingEnvironment/corrections.py)가 정상 시간 종료·추적 연속성·정차·해제·출력 해시·전달 및 종료 검사를 확인한다. 원시 버튼으로 hold/toggle 제어권을 재구성하고 정책에 기록된 정답·마스크·실제 선택값을 대조한다. 교정 합계 최소 16개, 각 표본 간격 최소 20ms다. 실패·비상 중지·사람 개입 없는 실행을 자동 교정 정답으로 채택하지 않는다.

`prepared_examples_model`은 기존 랩/핫랩 split과 비 Python 원자료 해시, 저장 표본 해시를 확인해 특징 표본만 재사용한다. 현재 코드와 설정의 해시는 새 manifest에 추가하고 정규화·가중치는 다시 만든다. 과거 코드 해시를 새 코드와 같다고 기록하지 않는다.

## 모델 버전과 행동

| 형식 | 입력 | 행동과 실행 |
| --- | --- | --- |
| PID v1 | 19개 | 부호 있는 출력에서 제한된 가속·제동 생성, 조향은 기존 제어기. [PID 규약](ModelTrainingSpecification.md) |
| SAC v2 | 35개 | 조향·부호 있는 가감속 두 행동, 휠 v2는 전진 기어·이전 페달 상호 배제 요구. [강화학습 규약](TrainingEnvironmentSpecification.md) |
| gamepad_v1 | 35개 | `[stick_x, rt, lt]` 세 행동, 조향 tanh·트리거 sigmoid, 동시 페달 허용 |

같은 입력 차원이어도 의미·행동·네트워크 형식이 다르므로 모델을 혼용하지 않는다. [gamepad Rust 모델](../ModelTrainer/rust/src/gamepad.rs)은 이름·차원·유한값·정규화를 검증한다.

## 특징 순서와 단위

인덱스는 0부터다. [기본 특징](../ModelTrainer/rust/src/lib.rs)의 0~18과 [v2 특징](../ModelTrainer/rust/src/features_v2.rs)의 19~34를 공통으로 사용한다. 게임패드의 19~21 이름만 원시 스틱·트리거 의미로 바꾼다.

| 인덱스 | 특징 이름 | 단위 |
| --- | --- | --- |
| 0, 1, 2 | speed_kmh, target_speed_kmh, speed_error_kmh | km/h |
| 3 | rpm | 회전/분 |
| 4 | lateral_error_m | m |
| 5, 6 | heading_error_sin, heading_error_cos | 무차원 |
| 7 | reference_grade_pct | % |
| 8~12 | curvature_0_per_m, curvature_10_per_m, curvature_20_per_m, curvature_40_per_m, curvature_80_per_m | 1/m |
| 13, 14 | speed_100ms_kmh, error_100ms_kmh | km/h |
| 15, 16 | speed_200ms_kmh, error_200ms_kmh | km/h |
| 17, 18 | speed_500ms_kmh, error_500ms_kmh | km/h |
| 19~21 | previous_stick_x, previous_rt, previous_lt | -1~1, 0~1, 0~1 |
| 22 | gear_raw | 1=N, 2=1단, …, 9=8단 |
| 23 | speed_rate_kmh_s | km/h/s |
| 24 | lateral_rate_m_s | m/s |
| 25 | heading_rate_rad_s | rad/s |
| 26, 27 | curvature_160_per_m, curvature_320_per_m | 1/m |
| 28~34 | reference_speed_0_kmh, reference_speed_10_kmh, reference_speed_20_kmh, reference_speed_40_kmh, reference_speed_80_kmh, reference_speed_160_kmh, reference_speed_320_kmh | km/h |

속도 오차는 목표−실제다. 이력은 목표 시각 이하의 가장 최근 관측을 쓰고 20ms까지 허용하며 미래값으로 대체하지 않는다. not ready·epoch 변경·100ms 이상 관측 공백에서는 이력을 초기화한다. 같은 sequence의 내용이 바뀌거나 시각이 역전되면 실패한다. 변화율은 연속 실제 관측의 시각 차이로 계산한다.

게임패드는 자동 변속 중 실제 N=1을 보존하고 후진 0 및 범위 밖 기어를 거절한다. F7 시작은 전진 기어를 요구한다. 실시간 이전 행동은 관측보다 엄격히 과거의 성공 전송이며 100ms 미만이어야 한다. 원시 교사 기록에서는 같은 조건의 과거 입력을 사용한다.

`feature_transform=seam_bridge_15m_v1`은 출발선 양쪽 15m의 기준 heading·곡률을 끝점 heading/곡률을 맞춘 Hermite 곡선으로 연결한다. 방향 오차·그 변화율과 전방 곡률 특징을 갱신하며 원래 위치 투영·주행 거리·실제 관측·정답을 바꾸지 않는다. [seam_features.py](../TrainingEnvironment/seam_features.py)는 저장된 관측과 과거 전송을 동일 Rust `features-gamepad-seam`에 재입력한다. 학습과 실시간 실행은 같은 변환을 선택해야 한다.

## 특징 제외와 정규화

다음 옵션은 새 가중치 학습에만 허용한다. 입력은 계속 35차원이며 정규화된 열과 첫 층의 해당 가중치를 0으로 만들어 export와 Rust 실행에서도 영향을 없앤다.

| 옵션 | 제외 인덱스 |
| --- | --- |
| exclude_previous_actions | 19~21 |
| exclude_longitudinal_response | 13~18, 23 |
| exclude_engine_state | 3, 22 |

일반 새 학습은 train의 float32 평균·모표준편차만 사용하고 표준편차가 1e-6 미만이면 1로 둔다. 교정은 부모 정규화를 고정하고, 증강 비교는 A의 선별 원자료로 만든 공통 정규화를 사용한다. validation이나 시험 자료로 다시 맞추지 않는다. 제외 옵션은 검증 MSE 개선이나 실게임 성공을 보장하지 않으며 [특징 의존성 분석](GamepadExperimentReview.md)의 결과를 함께 확인한다.

## 학습과 저장 및 모델 시험 자격

CPU 1스레드, PyTorch·NumPy seed 42, 35→128→128→3 ReLU 네트워크, Adam 0.0003, 배치 256, 최대 50 epoch를 사용한다. 손실은 마스크가 1인 행동의 제곱 오차 합을 유효 마스크 수로 나눈다. validation 세 행동의 평균 MSE가 최소인 epoch를 선택한다. 가중 표본 선택은 복원 추출이며 C의 잡음은 train 입력에만 적용한다.

결과는 새 `reinforcement_runs/gamepad-imitation-*` 폴더에 `manifest.json`, `model.json`, `checkpoint.pt`, `summary.json`, parity 입력·출력, `speed-profile.json`을 저장한다. 방식별 학습·검증·교정 표본 JSON도 보존한다. `checkpoint.pt`는 최선 epoch의 모델·optimizer·난수·정규화 상태다. GUI의 일반 `last.pt` 재개 형식과 다르다.

선택한 모델을 raw validation 표본 최대 256개로 Python/Rust 대조하고 최대 오차 1e-5 이하여야 한다. 입력 해시 재검증 뒤 완료 처리하며 summary에는 행동별 MSE·이전 입력 유지 기준·수치 차이·표본 수·epoch·갱신 수를 남긴다. 중지나 예외는 미완료로 남기고 기존 결과를 덮어쓰지 않는다.

실시간 준비는 완료·`kind=gamepad_imitation`·`semantics=gamepad_v1`·`parity_passed`·모델 출력 해시·prepared 해시·controls/assists 일치를 별도로 검사한다. 오프라인 summary의 `live_performance_verified=false`는 실제 짧은 시험 요약과 함께 해석한다. 검증 세션을 반복 선택에 썼으면 최종 test로 표현하지 않는다.
