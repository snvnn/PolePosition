# 게임패드 학습 실험과 실패 분석

2026-10-18 정리. 행동별 오차는 정규화된 `[stick_x, rt, lt]` 순서의 MSE다. 오프라인 검증, 고정 관측 재생, 실제 시험 종료와 자율 완주 성능을 구분한다. 현재 설정 연결은 [모델 표](CurrentGamepadModels.md), 학습 조건은 [학습 규약](GamepadTrainingSpecification.md)을 따른다.

## 조향 의존성과 출발선 특징 보정

10월 11일 초기 새 교사 모델은 같은 위치의 사람 조향과 다른 방향으로 출발했고, 과거 스틱만 교사값으로 바꾼 재생에서 예측이 크게 달라졌다. [조향 발산 분석](../input_profiles/gamepad/steering-divergence-20261011/analysis.json)은 이전 스틱 의존성과 이후 상태 이탈을 뒷받침한다. 이전 행동 전체 제외 후보의 검증 오차 비율은 기존 대비 약 8.178배여서 이 제거만으로 해결됐다고 볼 수 없었다. 당시 과정은 [개발 로그](TrainingControllerDevelopmentLog.md)에 보존했다.

기준 경로의 마지막/처음 약 5m에서 heading·곡률의 급격한 연결 변화도 관측했다. `seam_bridge_15m_v1`은 출발선 양쪽 15m의 특징만 보정한다. `gamepad-imitation-1791712462218386600-1be3fa19`는 같은 새 교사 split과 이전 행동 제외를 유지한 후보다. 당시 선택 epoch 50, Python/Rust 최대 차이 약 4.128e-7이었다.

동일한 관측을 재생했을 때 문제 시점의 조향 변화량은 기존 0.96693, 특징 보정 모델 0.10925, 보정+30ms LPF 0.04596이었다. 당시 차량은 이미 횡방향으로 약 3.25m 벗어나 있었으므로 특징 변화량 개선을 복구 주행 성공으로 해석하지 않는다. 근거는 [출발선 보정 검토 폴더](../input_profiles/gamepad/seam-smoothing-review), [A/B 계획](../input_profiles/gamepad/lpf-ab-plan.json), [개발 로그](TrainingControllerDevelopmentLog.md)다.

## 조향 필터 실제 시험

10월 11일 저장된 실행의 `Running` 정책은 아래 여섯 회 모두 `authority=model`이었다. 같은 출발선 보정 모델을 사용하며 A는 시간상수 0, B는 30ms다. 표는 각 실행의 요약 판정이며, 출발 상태까지 통제한 반복 실험의 통계적 우열을 뜻하지 않는다.

| 시간 | A 실행과 결과 | B 실행과 결과 |
| --- | --- | --- |
| 5초 | [17664](../actuator_runs/gamepad-1791712658485528500-17664/summary.json): time_limit, passed=true, 정차·해제 true | [28564](../actuator_runs/gamepad-1791712900388741700-28564/summary.json): 같은 판정 |
| 10초 | [17584](../actuator_runs/gamepad-1791713079115127900-17584/summary.json): tracking_changed_before_stop, passed=false | [30572](../actuator_runs/gamepad-1791713257377838000-30572/summary.json): 같은 판정 |
| 30초 | [28624](../actuator_runs/gamepad-1791713172823939900-28624/summary.json): tracking_changed_before_stop, passed=false | [21020](../actuator_runs/gamepad-1791713338323962800-21020/summary.json): invalid_lap, passed=false |

10초 양쪽과 30초 A는 `stop_confirmed=false`, 30초 B는 정차를 확인했지만 무효 랩으로 실패했다. 모두 `released=true`다. 5초 정상 종료는 확인했으며, 긴 구간에서 필터가 주행 문제를 해결했다는 근거는 없다. 후속 비교는 동일 시작 상태·모델·프로필과 사람 개입 여부를 고정하고 유효 구간의 교사 비교를 함께 남긴다. 필터 규칙은 [운영 안내](GamepadOperationGuide.md#조향-필터와-출발선-보정)에 있다.

## 완전한 랩과 혼합 모델 비교

[연속 랩 비교](../input_profiles/gamepad/continuous-training-comparison.json)는 동일한 보류 랩 4,409개 표본으로 출발선 모델과 랩 모델을 비교했다. 랩 모델은 8개 학습 랩의 35,362개 표본을 사용했다. 보류 랩은 학습 자료와 같은 연속 세션에서 나왔으므로 독립 세션 시험은 아니다.

| 모델 | 보류 랩 MSE 조향 | 가속 | 제동 |
| --- | --- | --- | --- |
| 출발선 보정 모델 | 0.073686 | 0.092886 | 0.067043 |
| 랩 모델 | 0.024416 | 0.003769 | 0.002606 |
| 혼합 모델 | 0.023111 | 0.003753 | 0.002753 |

혼합 모델은 랩 35,362개와 핫랩 교사 5,330개를 합친 40,692개를 사용했다. [혼합 비교](../input_profiles/gamepad/mixed-training-comparison.json)에서 보류 핫랩 1,064개의 조향/가속/제동 MSE는 출발선 모델 `[0.012233,0.007530,0.002143]`, 랩 모델 `[0.051135,0.008552,0.020111]`, 혼합 모델 `[0.010292,0.004983,0.009443]`이었다. 혼합의 출발 조향과 가속은 개선됐지만 제동은 출발선 모델보다 나빴다. 학습 중 20km/h 미만 표본은 173개로 저속 출발 자료의 비중도 작다.

실제 랩 모델 [5초 실행](../actuator_runs/gamepad-1791726545944742300-36564/summary.json)과 혼합 모델 [5초 실행](../actuator_runs/gamepad-1791727078577089400-7948/summary.json)은 각각 약 93m 진행 후 정상 종료했다. [랩 15초](../actuator_runs/gamepad-1791726688164895200-35484/summary.json)와 [혼합 15초](../actuator_runs/gamepad-1791727152479321900-22156/summary.json)는 `tracking_changed_before_stop`, `stop_confirmed=false`, `passed=false`였다. 해제는 확인됐지만 자율 코너 성공으로 처리하지 않는다.

## 제동 특징 의존성과 제거 실험

[제동 진단](../input_profiles/gamepad/brake-diagnosis.json)은 랩 정답 35,362개와 핫랩 정답 5,330개를 확인했다. 최대 정답 차이는 0, 랩 정답 읽기 종료 지연 최대는 관측 이후 0.1239ms였다. 강한 제동 정답은 5,953개이며 학습 표본에서 제동 0.5 초과 재현율은 약 97.9%였다.

실패 관측의 예측은 `[0.29624,0.56064,0.03217]`로 제동이 작았다. 같은 관측의 속도 변화율만 가까운 교사 제동 표본값으로 바꾸자 `[0.16244,0.00223,0.86748]`로 달라졌다. 현재 측정된 감속이 이미 제동 중인 상태의 대리 신호가 되는 의존성을 시사한다. 이는 미래 정답 시각 누출의 증거가 아니며, 한 특징 대체는 물리적으로 일관된 가상 주행도 아니다.

종방향 이력·속도 변화율을 제외한 후보 `1791727401043557500-155aa641`와 RPM·기어까지 제외한 후보 `1791727497357778800-4c8c46d5`를 구분한다. [최종 제동 특징 검토](../input_profiles/gamepad/brake-fix-review.json)는 후자를 대상으로 한다.

| 같은 보류 자료 | 혼합 모델 MSE | 엔진 상태까지 제외한 후보 MSE |
| --- | --- | --- |
| 랩 4,409개 | [0.023111, 0.003753, 0.002753] | [0.023411, 0.022790, 0.022271] |
| 핫랩 출발 1,064개 | [0.010292, 0.004983, 0.009443] | [0.010687, 0.037271, 0.015667] |

고정 실패 관측 재생에서 제동 0.2 도달은 기존 10.140초에서 후보 7.900초로 앞당겨졌지만, 독립 주행이 아니며 검증 MSE는 악화됐다. 제외 특징 불변성은 검사했고 기존 모델·실행 설정을 보존했다. 이 후보는 별도 5초 설정만 있으며 실제 성능과 채택은 미확정이다. [보존 해시](../input_profiles/gamepad/brake-fix-preservation.json)를 참조한다.

## 10월 17일 세션 자료와 실제 시험

선별 세션 4개를 train, 별도 세션의 첫 유효 6랩을 validation으로 고정했다. 선별 근거와 보관은 [자료 이력](DataArchiveGuide.md)에 있다. [세션 모델](../reinforcement_runs/gamepad-imitation-1792231181059399600-43934c30/summary.json)은 train 233,320개·validation 27,514개, 선택 epoch 36, MSE `[0.0142715,0.0111740,0.0153354]`, Python/Rust 차이 약 5.439e-7이다.

| 실제 실행 | 판정 |
| --- | --- |
| [5초](../actuator_runs/gamepad-1792231439760527600-32492/summary.json) | 93.47m, time_limit, passed/stop_confirmed/released 모두 true |
| [15초](../actuator_runs/gamepad-1792231570855084600-9996/summary.json) | 504.85m, tracking_changed_before_stop, passed/stop_confirmed false, released true |
| [30초 설정](../actuator_runs/gamepad-1792231710120101700-30252/summary.json) | 421.38m, tracking_changed_before_stop, passed/stop_confirmed false, released true |

세 회차의 Running 제어권은 모두 model이다. 30초는 설정한 상한이며 30초를 정상 주행했다는 뜻이 아니다. [15초 검토](../input_profiles/gamepad/session-model-15s-review.json)는 정책 구간 입력 전달 검사 통과, 정책 중 유효 랩 상태, 최대 횡오차 약 2.390m를 기록하지만 정차 전 추적 변경으로 전체는 실패했다. 전달 지연 최대 2.1975ms는 ACC 적용 지연 측정이 아니다.

## 증강 조건과 제외 구간 경계 수정

실험 코드는 [augmentation_experiment.py](../TrainingEnvironment/augmentation_experiment.py), [augmentation.py](../TrainingEnvironment/augmentation.py)다. train의 유효→무효 전환 2초 전부터 1초 후까지 제외하고, 구간 경계에서 특징 이력을 초기화한다. 제동 정답이 0.2 이상이거나 0/10/20/40m 곡률 절댓값 중 하나가 0.003/m 이상인 표본은 가중치 2, 나머지는 1이다.

| 조건 | train 표본 | 선택/잡음 |
| --- | --- | --- |
| existing | 233,320 | 기존 세션 학습 보존, 비교 기준 |
| A | 229,155 | 선별 원자료의 0ms 위상, 중요 구간 가중 추출 |
| B | 401,369 | 0/10ms 위상을 합친 실제 관측 행, 같은 가중 추출 |
| C | 401,369 | B와 같고 확률 0.5로 횡오차·heading에 제한 잡음 |

C의 횡오차 잡음 표준편차는 0.02m·클립 ±0.06m, heading 잡음은 0.002rad·클립 ±0.006rad다. sin/cos를 일관되게 바꾸고 변화율 특징은 바꾸지 않는다. 정답·validation은 변경하지 않는다. A/B/C 모두 A 원자료 정규화, 같은 seed 42 초기 가중치, epoch당 229,155회 추출과 50 epoch의 44,800회 optimizer 갱신을 사용한다. 기존 모델은 이 재학습 조건과 같다고 가정하지 않는다.

최초 [실험 폴더](../reinforcement_runs/augmentation-comparison-1792232268882390000-bae10bd2)는 관측 시각은 제외 구간 밖이지만 정답 읽기 구간이 경계와 겹치는 표본 3개를 포함했다. [경계 수정](../TrainingEnvironment/augmentation_boundary_revision.py)은 이를 제거하고 A 정규화부터 재생성했다. 최초 결과의 [superseded.json](../reinforcement_runs/augmentation-comparison-1792232268882390000-bae10bd2/superseded.json)은 선정 부적격과 대체 폴더를 가리킨다.

현재 기준은 [수정 후 실험 폴더](../reinforcement_runs/augmentation-comparison-1792233044576944100-923b5038)다. [후검사](../reinforcement_runs/augmentation-comparison-1792233044576944100-923b5038/post-curation-check.json)는 제외 구간과 정답 읽기 겹침 0, 표본 401,369개, 최소 이력 약 0.50962초, validation 동일을 기록한다. [검증 집계](../reinforcement_runs/augmentation-comparison-1792233044576944100-923b5038/verification.json)는 당시 검사 16개, 출력 해시·공통 정규화·초기 가중치·같은 갱신 예산과 프리셋 12개 확인을 담는다.

## 증강 비교 결과와 다음 판단

validation은 27,514개이며 제동 구간 4,628개·곡선 18,470개·직선 9,044개다. 제동 구간과 곡선은 중복할 수 있다. [comparison.json](../reinforcement_runs/augmentation-comparison-1792233044576944100-923b5038/comparison.json)의 전체 MSE와 조향 변화 지표는 다음과 같다.

| 모델 | 조향 MSE | 가속 MSE | 제동 MSE | 세 행동 평균 MSE | 조향 변화율 절댓값 p95 |
| --- | --- | --- | --- | --- | --- |
| existing | 0.0142715 | 0.0111740 | 0.0153354 | 0.0135936 | 5.88481/s |
| A | 0.0150116 | 0.0115457 | 0.0155557 | 0.0140377 | 5.62082/s |
| B | 0.0148374 | 0.0122820 | 0.0148005 | 0.0139733 | 5.28703/s |
| C | 0.0146529 | 0.0122454 | 0.0149088 | 0.0139357 | 5.38222/s |
| 이전 입력 유지 | 0.0004773 | 0.0002126 | 0.0002642 | 0.0003180 | 2.82087/s |

제동 구간의 제동 MSE는 existing 0.067519, A 0.066641, B 0.060626, C 0.061337이다. 곡선의 제동 MSE는 각각 0.022259, 0.022457, 0.021216, 0.021365다. 직선의 제동 MSE는 0.001195, 0.001461, 0.001698, 0.001724로 후보가 나빠졌다. 전체·곡선·직선의 세 행동 전체 수치는 원본 비교에 보존한다.

후보 중 순위는 C→B→A지만 모두 existing보다 전체 평균 오차가 크다. 선정 규칙상 `selected_candidate=null`이며 실제 모델 교체를 하지 않았다. 이전 입력 유지 기준이 훨씬 낮은 오차를 보이는 것도 남은 한계다. 조향 변화율 감소는 더 적절한 주행 궤적이나 실제 안정성의 증명이 아니다.

validation 세션은 epoch와 후보 선택에 재사용했으므로 최종 test가 아니다. 실패 실행 9~13초의 관측을 고정한 재예측도 실제 폐루프 주행이 아니다. [result.json](../reinforcement_runs/augmentation-comparison-1792233044576944100-923b5038/result.json)은 기존/A/B/C의 5·15·30초 프리셋 12개와 `live_tests_pending=true`를 남긴다. 다음 단계는 동일 조건의 짧은 실제 비교와 별도 보류 세션 평가이며, 자율 유효 완주 성능은 계속 미검증이다.
