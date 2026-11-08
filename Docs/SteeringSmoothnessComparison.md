# 조향 평활화 후보와 정책 비교 평가

**현재 실행 보류:** 2026-11-05 사용자 지시로, 별도 명시적 승인 전까지 ACC 실행·비교 주행·강화학습 재개를 금지한다. 수정과 오프라인 검증만 진행한다. 현재 평활화 후보는 실게임 채택 기준을 검증하지 못했으며 기존 프리셋을 유지한다.

`TrainingEnvironment.steering_experiment`는 기존 완주 체크포인트를 읽어 별도 실험 폴더에 계수 0, 0.1, 1, 10의 후보를 만든다. 각 분기는 같은 actor/optimizer/critic/데이터/난수 상태에서 시작하며 actor만 1,000회 갱신한다. 원본 모델, 경험, 체크포인트는 덮어쓰지 않는다.

## 학습

추가 손실은 `mean(((steer(next)-steer(current))*0.02/dt)^2)`이다. 검증된 연속 관측에서만 계산하며 종료, 리셋, 시간 간격 0 이하 또는 50ms 초과, 비유한 입력은 제외한다. 가속·브레이크에는 적용하지 않는다. 계수 0은 기존 actor 손실을 그대로 계산한다. 보상, 입력/출력 차원, 제어 주기, 실행 중 조향 필터, 긴급 제동과 F8은 변경하지 않는다.

설정 형식은 `smoothness = { method = "temporal_steering_v1", coefficient = 1.0 }`이다. 생략한 경우 체크포인트 값을 이어받으며, 이전 형식은 `none/0`이다. 설정 변경은 `Learner(..., branch=True)`인 새 실험에서만 허용한다. 체크포인트에는 방식, 계수, 부모 해시, optimizer와 Python/NumPy/PyTorch 및 표본 추출 난수 상태를 저장한다.

실행: `.venv-training\Scripts\python.exe -m TrainingEnvironment.steering_experiment`

오프라인 후보 선정은 기존 검증 자료에서 시간 방향 조향 손실이 부모와 계수 0 대조군 모두보다 20% 이상 작고, 교사 모방 MSE의 각 축이 부모보다 5% 넘게 증가하지 않는 후보 중 손실이 가장 작은 하나를 선택한다. 이 조건은 실제 차량 위빙 개선의 증거가 아니다.

## 실게임 비교

컨트롤러의 **강화학습 → 정책 비교 평가**에서 생성된 `comparison-config.json`을 선택한다. 현재 실험은 `TrainingEnvironment/policy-comparison.json`과 `TrainingController/policy-comparison-preset.json`에 연결되어 있다. 시작 후 10초 내에 ACC 차량 또는 차고 화면으로 전환한다. 이 준비 시간도 전체 한도에 포함된다.

- 초기 B, 기존 완주, 최근 저장, 계수 0, 선택된 후보를 각 4회 비교한다. 각 반복의 순서는 난수 42로 섞는다.
- 완주 횟수, 진행 거리 중앙값, 조향 변화량 순으로 가장 좋은 대조 모델을 선택한다. 이후 후보와 대조 모델을 각 5회, 난수 43 순서로 확인한다.
- 시도는 유효한 전체 1랩, 무효 랩, 진행 정체, 주행 180초에 끝난다. 첫 출발선 통과는 랩타임 측정 시작이며 완주로 세지 않는다.
- 정상 종료 후 신선한 관측으로 0.5초 정차, 중립 입력, 장치 해제를 확인해야 다음 복귀를 허용한다. ACC가 이미 차고로 이동했으면 그 위치에서 출발을 준비한다. 화면 전환 중에는 입력 없이 인식 가능한 화면을 기다린다.
- F8, 사용자 중지, 입력 격리·관측·기록 오류 또는 복귀 실패는 전체 비교를 종료한다. 2시간 한도는 준비와 복귀를 포함한다. 중단된 비교는 미검증이며 자동 재개하지 않는다.
- 비교 전후 모든 모델과 checkpoint 파일의 해시를 확인한다. 평가 중 actor/critic/optimizer 갱신은 없다. 비교 기록은 학습 경험 수집에서 명시적으로 거부한다.

사용자의 후속 루프 보완 요청으로 **확인된 재배치의 복귀 경로**를 추가했다. 신선한 연속 원시 관측 사이 50m 초과 이동, 새 추적 epoch, 등록된 출발 위치 1m 이내, 정차·기어 0, 같은 차량/트랙/핫랩, 이후 0.5초 정차와 중립·장치 해제, 차고 화면을 모두 확인해야 한다. 원인을 단정하지 않고 `unattributed_relocation`으로 제외한다. 같은 실행 안에서 최대 두 번만 복귀하며, 학습은 해당 경험을 버리고 같은 정책으로 다음 회차를 준비한다. 비교는 예약된 순서와 횟수를 유지하되 이 제외가 하나라도 있으면 후보 채택을 차단한다. 오래된 관측, 입력 격리 실패, F8·사용자 중지, 기록 오류, 차고 확인 실패는 복귀시키지 않는다. 종료된 실행을 자동으로 재개하지 않는다.

## 지표와 채택

주행 중 속도 80km/h 이상, 현재와 10m 전방 곡률 절댓값이 0.001/m 미만인 직선에서 50m 경로 구간 × 20km/h 속도 구간을 맞춘다. 유효한 연속 관측 사이의 조향 총변동/이동 거리와 경로 횡방향 속도 RMS를 함께 측정한다. 정차 입력 검사는 제외한다. 양쪽에서 3회 이상 나타난 공통 구간이 있어야 하며 양쪽 각각 합계 100m·5초 이상이어야 한다. 구간별 값을 같은 비중으로 평균하므로 어느 한 모델의 특정 속도 구간 비중이 결과를 지배하지 않는다.

추가 확인 5회씩이 모두 기록되고, 후보가 최소 3회 및 대조 이상 완주, 진행 중앙값 대조 이상, 조향 변화/거리 20% 이상 감소, 횡방향 속도 RMS 10% 이상 감소, 양쪽 각각 3회 이상 완주의 랩타임 중앙값이 대조의 105% 이하일 때만 채택한다. 비교 자료가 부족하면 통과하지 않는다. 이 임계값은 이번 실험의 초기 기준이다.

결과 폴더의 `report.json`, `report.html`, `trials.json`에는 모델별 결과와 개별 진행 거리, 랩타임, 공통 구간 지표, 제외 이유가 남는다. 모든 조건을 통과할 때만 이전 프리셋을 결과 폴더에 보존한 후 후보로 교체한다. 실패·중단·부족한 자료는 기존 프리셋을 유지한다. 통과하더라도 장시간 학습을 자동으로 시작하지 않는다.

## 검사

`python -m unittest TrainingEnvironment.test_steering_smoothness TrainingEnvironment.test_policy_comparison TrainingEnvironment.test_acc_reset TrainingEnvironment.test_auto_cycle TrainingEnvironment.test_gamepad_online TrainingEnvironment.test_gamepad_recovery TrainingController.test_gamepad TrainingController.test_coordinator`

Rust의 `automatic_start` 검사는 비교 종료가 학습 자동 복귀 조건으로 새지 않음을 확인한다. `gamepad_episode` 검사는 전체 랩과 첫 출발선 통과를 구분한다. 후보 게시 시 저장 상태의 모든 항목을 다시 읽어 비교하고 Python/Rust 출력 오차 1e-5 이하를 확인한다.

## 2026-11-08 승인된 최고 모델 학습 반복

후속 승인으로 `reviews/champion-until-2200-20261108/config.json`은 같은 학습·선발 루프를 2026-11-08 22:00 KST까지 반복한다. 시각에 도달하면 진행 중인 단계도 중단하며, 시간 비교는 절대 시각과 시작 시 고정한 단조 시계 예산을 함께 사용한다. 재시작으로 종료 시각이 연장되지 않는다. 다섯 번 검증에서 탈락한 동일 정책은 재선발 후보에서 제외한다. 기존 두 회차 설정은 그대로 보존한다.

ACC 종료로 신선한 정차 관측을 받을 수 없었던 사례를 확인했다. 이제 시작 시 ACC 프로세스 핸들을 보유하고, 해당 프로세스의 종료가 OS에서 확인된 경우에만 가상 패드 소유 프로세스를 해제한다. 단순 관측 단절이나 시간 초과만으로 강제 해제하지 않는다. 이 종료는 정상 정차로 인정하지 않으며 해당 주행은 학습에 사용하지 않는다. 요청에 대응하는 응답이 하나라도 빠졌다면 이전 주행의 정차 증거로 다음 단계에 진입할 수 없다.

`reviews/champion-cycle-20261108/loop-config.json`은 기존 완주 정책 `ebd66caa…`에서 시작해 1시간 학습과 선발을 두 번 실행한다. 각 선발은 45분 이내, 전체는 12,660초 이내다. 기존 프리셋은 보존하고 루프 전용 `champion.json`만 갱신한다. 학습 시간은 준비·복귀·저장을 포함한다.

완주 에피소드의 학습 **전** 정책을 후보로 삼는다. 과거 2랩 연속 완주 정책 `75925638…`도 포함하며, 유효 랩 수·진행 거리·랩타임 순으로 한 후보를 골라 기존 최고 모델과 각각 5회 고정 정책 평가한다. 이 평가는 전체 1랩 단위이므로 2랩 연속 완주의 재현을 입증하지는 않는다. 비교 기록은 학습에 사용하지 않는다.

후보는 5회 중 최소 3회 완주하고 기존 모델보다 완주 횟수·유효 500m 초과 횟수·진행 거리 중앙값이 낮지 않아야 한다. 공통 직선/속도 구간의 조향 변화량과 횡속도 RMS가 모두 나빠지지 않고, 구간당 100m 이상 기록으로 제한한 결과도 같아야 한다. 완주 랩타임 중앙값은 5% 이내 악화만 허용한다. 제외나 불명확한 500m 판정은 채택을 막는다. 완주 횟수·500m 횟수 또는 거리 중앙값(1m 초과) 중 하나는 개선되어야 한다. 동률·자료 부족이면 기존 모델을 유지한다. 이는 위의 평활화 후보 배포 기준과 별개인 이번 루프의 학습 시작점 선정 기준이다.

다음 학습은 선택한 체크포인트의 모델·optimizer·replay·난수 상태에서 정확히 분기하고 같은 폴더의 이후 경험을 가져오지 않는다. 예상한 1시간 종료 및 정차·중립 입력·장치 해제 증거를 확인해야 선발로 전환한다. F8, 사용자 중지, 오류, 복귀 실패, 미완료 비교는 전체를 멈추며 자동으로 세 번째 회차를 시작하지 않는다.
# 2026-11-08 19:50 comparison reset interruption

## Approved continuation outcome at 22:00

The user approved a new execution at 20:26 with the same absolute 22:00 deadline.
Session `gamepad-champion-cycle-1794137285468343500-7a9c94c7` ran from 20:28:05
to deadline shutdown at 22:00 (worker exited 22:00:02). It completed a one-hour
training phase, all ten frozen comparison attempts, and a final partial training
phase. There were 38 recorded training episodes, 3,658 actor updates, and two
valid full laps, both from the retained incumbent. One terminal-reset observation
failure was excluded with verified recovery. No whole-loop interruption recurred.

Candidate `903ab9f9...` and incumbent each completed 0/5 comparison attempts and
passed valid 500 m in 5/5. Candidate median progress was 2,530.8 m versus 1,658.5 m,
but matched-straight lateral RMS increased from 1.1912 to 1.5246; it failed the
adoption gates. No promotion occurred.

At the deadline the final native attempt was cancelled before any episode/policy
input. Its only log entry is zero neutralization and device release is confirmed.
The report's `final_stop_proof_error` remains preserved because this never-started
attempt has no native standstill observations. Independent audit verified the
previous actual drive's stop/release and >0.5 seconds of fresh post-exit stationary
neutral telemetry. No worker or gamepad process remained. This is an audit of the
shutdown, not a relaxation of training or phase-transition validation.

Full evidence: `reviews/champion-until-2200-20261108/continuation-result.json`.
Further live execution requires new approval.

The 22:00-deadline champion loop stopped at 19:50:51 KST during round four's
frozen comparison: `Reset menu changed before click: confirm`. Four one-hour
training phases had finished. The comparison's first incumbent drive stopped,
neutralized, and released normally; the next trial never started. The parent
worker and native input owner exited. No candidate was promoted.

The failure occurred between the expected-menu observation and the pre-click
observation. The confirmation dialog was still visible on later inspection;
transition flicker is plausible but cannot be established without the historical
frame. `AutoReset.click_when_stable` now requires 0.2 seconds of matching menu
observations and retries only `MenuNotReady`, raised before any mouse input.
It retains the original reset deadline, foreground/coverage guards, and F8
cancellation. Input failures of uncertain delivery are never retried. All 139
relevant offline tests passed, including transient/permanent mismatches and F8.
This change has not yet been verified live.

Audit: `reviews/champion-until-2200-20261108/interruption-1950.json`.
Separate continuation config/preset are prepared but not launched. They preserve
the absolute 22:00 deadline, selected `ebd66caa...` checkpoint, rejected policies,
and the untested `903ab9f9...` completion candidate as historical evidence.
