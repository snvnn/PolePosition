# 게임패드 모델과 실행 설정

2026-10-18 기준. 현재 TOML·GUI 프리셋·저장된 실행 요약을 대조한 표다. 가장 최근 폴더가 자동으로 선택되지는 않는다. **10월 17일 증강 실험의 후보는 미선정이고 A/B/C 실게임 시험은 대기 중**이다. [실험 결과](GamepadExperimentReview.md)를 함께 확인한다.

## 모델 식별

모델 폴더는 모두 `reinforcement_runs/` 아래에 있다. 아래 기호는 이 문서의 대응표에서만 사용한다.

| 기호 | 모델 폴더 | 용도와 확인된 범위 |
| --- | --- | --- |
| 초기 | [gamepad-imitation-1791298274998489700-29029793](../reinforcement_runs/gamepad-imitation-1791298274998489700-29029793/summary.json) | 초기 교사 3회 학습, `trial.toml`의 과거 모델 |
| 출발선 | [gamepad-imitation-1791712462218386600-1be3fa19](../reinforcement_runs/gamepad-imitation-1791712462218386600-1be3fa19/summary.json) | 새 교사 5회 학습·1회 검증, 이전 행동 제외·출발선 보정, LPF 비교 기준 |
| 랩 | [gamepad-imitation-1791718728324314300-3b36589d](../reinforcement_runs/gamepad-imitation-1791718728324314300-3b36589d/summary.json) | 8랩 학습·1랩 검증, 5초 실제 시험 통과·15초 전체 판정 실패 |
| 혼합 | [gamepad-imitation-1791726972411867800-d63bca8c](../reinforcement_runs/gamepad-imitation-1791726972411867800-d63bca8c/summary.json) | 8랩과 핫랩 교사 혼합, 5초 통과·15초 정차 전 추적 변경 |
| 제동 특징 실험 | [gamepad-imitation-1791727497357778800-4c8c46d5](../reinforcement_runs/gamepad-imitation-1791727497357778800-4c8c46d5/summary.json) | 이전 행동·종방향 반응·엔진 상태 제외, 실게임 성능 미검증 |
| existing | [gamepad-imitation-1792231181059399600-43934c30](../reinforcement_runs/gamepad-imitation-1792231181059399600-43934c30/summary.json) | 선별 세션 학습, 증강 비교의 보존 기준 모델. 5초 통과·15/30초 전체 판정 실패 |
| A | [gamepad-imitation-1792233085074149200-88374ec8](../reinforcement_runs/gamepad-imitation-1792233085074149200-88374ec8/summary.json) | 제외 구간 경계 수정 후 원자료 선별 모델 |
| B | [gamepad-imitation-1792233167196795000-e7f6e556](../reinforcement_runs/gamepad-imitation-1792233167196795000-e7f6e556/summary.json) | 같은 조건에 표본 확장·가중치 적용 |
| C | [gamepad-imitation-1792233257966821800-4a4dfe57](../reinforcement_runs/gamepad-imitation-1792233257966821800-4a4dfe57/summary.json) | B에 제한된 특징 잡음 추가 |

A/B/C는 최초 실험의 다른 모델 세 개와 구분한다. 이 표는 [경계 수정 후 result.json](../reinforcement_runs/augmentation-comparison-1792233044576944100-923b5038/result.json)에 연결된 모델이다. 기존 모델과 후보를 보존하며 자동 교체하지 않는다.

## 설정과 프리셋

| 목적 | TOML과 GUI 프리셋 | 모델 | 시간과 LPF |
| --- | --- | --- | --- |
| 초기 단독 진단 | [trial.toml](../GamepadIO/trial.toml), `gamepad-model-preset.json` | 초기 | 현재 10초·LPF 0, TOML의 기존 5초 통과 결과 필요 |
| LPF A 5초 | [intervention-lpf-off.toml](../GamepadIO/intervention-lpf-off.toml), GUI에서 직접 선택 | 출발선 | 5초·LPF 0 |
| LPF B 5초 | [intervention-lpf-on.toml](../GamepadIO/intervention-lpf-on.toml), GUI에서 직접 선택 | 출발선 | 5초·LPF 30ms |
| LPF A/B 확대 | `intervention-lpf-a-10s.toml`, `intervention-lpf-a-30s.toml`, `intervention-lpf-b-10s.toml`, `intervention-lpf-b-30s.toml` | 출발선 | A 0 / B 30ms, 각 10·30초 |
| 랩 모델 | [laps-only-test-15s.toml](../GamepadIO/laps-only-test-15s.toml), GUI에서 직접 선택 | 랩 | 15초·LPF 0 |
| 혼합 모델 | [mixed-model-test-5s.toml](../GamepadIO/mixed-model-test-5s.toml), [mixed-model-test-15s.toml](../GamepadIO/mixed-model-test-15s.toml) | 혼합 | 5·15초·LPF 0 |
| 과거 연속 모델 시험 | [continuous-model-test.toml](../GamepadIO/continuous-model-test.toml), [continuous-model-test-preset.json](../TrainingController/continuous-model-test-preset.json) | 혼합 | 15초·LPF 0 |
| 제동 특징 후보 | [brake-fixed-test-5s.toml](../GamepadIO/brake-fixed-test-5s.toml), GUI에서 직접 선택 | 제동 특징 실험 | 5초·LPF 0 |
| 세션 모델 | `session-model-test-5s.toml`, [session-model-test-15s.toml](../GamepadIO/session-model-test-15s.toml), `session-model-test-30s.toml` | existing | 5·15·30초·LPF 0 |
| 세션 GUI 재개 | [session-model-test-preset.json](../TrainingController/session-model-test-preset.json) | existing | 현재 30초 설정을 선택함 |
| 증강 비교 | `augmentation-{existing,A,B,C}-{5s,15s,30s}.toml` 및 같은 이름의 `TrainingController/*-preset.json` | 각 existing/A/B/C | 12조합, 모두 LPF 0 |

위 LPF·랩·혼합·세션·증강 설정은 모두 개입 모드이며 `disable_path_departure_stop=true`다. 기존 경로 거리·방향 게이트를 해제하지만 관측 신선도·추적 연속성·무효 랩·장치·기록 오류 및 F8 중지는 유지한다. 개입 없는 `trial.toml`은 거리·방향 게이트와 직전 단계 검증을 유지한다. 개입 모드에서 사람 입력을 쓰지 않았는지는 원시 버튼과 정책 제어권으로 확인한다.

공통 경로는 `state_runs/first-lap-prepared-1791019376646183400-972dfd54`, 개입 프로필은 `input_profiles/gamepad/intervention-profile.json`, 분리 설정은 `input_profiles/gamepad/isolation.json`이다. `trial.toml`은 자체의 초기 프로필을 따른다. 모든 상대경로는 저장소 루트 기준이다. 프리셋의 절대경로는 이 PC의 `C:\Users\YoonHyeon\Desktop\accad`를 가리키므로 다른 위치에서는 수정 후 각 프로필 해시를 다시 검증한다.

## 현재 실행할 수 없는 기본 조합

| 설정 | 현재 값 | 처리 |
| --- | --- | --- |
| [intervention.toml](../GamepadIO/intervention.toml), [gamepad-intervention-preset.json](../TrainingController/gamepad-intervention-preset.json) | 출발선 모델·60초·LPF 0 | 실행부가 시간 검사에서 거절. 현재 기본 교정 프리셋 사용 보류 |
| [intervention-lpf-a-60s.toml](../GamepadIO/intervention-lpf-a-60s.toml) | 출발선 모델·60초·LPF 0 | 동일하게 거절. 60초 시험의 검증 근거로 사용하지 않음 |

[실행부](../ModelAndActuator/src/gamepad.rs)는 5·10·30초, 그리고 개입 설정이 있을 때 15초만 허용한다. 이 문서 갱신으로 설정이나 시간 상한을 변경하지 않았다. 사용하려는 목적에 맞는 허용 TOML을 GUI의 `게임패드 교정 주행`에서 직접 선택한다. 프리셋의 `--prepare`도 F7을 생성하지 않는다.

## 시험 결과 판정

`summary.passed=true`는 해당 짧은 진단의 종료·정차·해제 등 조건 통과다. 한 코너 주행이나 자율 완주 성능은 같은 경로 위치의 교사 비교와 별도 유효 랩 판정이 필요하다. 모델 summary의 오프라인 `live_performance_verified=false`는 그대로 보존하고 이후 실제 실행 요약을 함께 읽는다.

입력 전달 검사 통과여도 `tracking_changed_before_stop`, `invalid_lap`, `stop_confirmed=false`이면 전체 시험 성공으로 처리하지 않는다. [운영 안내](GamepadOperationGuide.md), [실험별 실제 결과](GamepadExperimentReview.md), [검증 색인](VerificationLog.md)을 따른다.
