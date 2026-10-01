# DatasetBuilder 규약 v1

Python 3.11 표준 라이브러리 기반 오프라인 도구다. 기록 중인 세션은 입력으로 사용하지 않는다.

## 실행

프로젝트 루트에서 실행한다.

```powershell
python -m DatasetBuilder --session recordings/1790669289916063400-33768-0 --config DatasetBuilder/config.toml
python -m unittest DatasetBuilder.test_builder DatasetBuilder.test_report -v
```

설정 생략 시 모듈에 포함된 config.toml을 사용한다. `output_dir`은 실행 작업 디렉터리 기준이다.
기본값: datasets, max_gap_ms=100, train_ratio=0.70, validation_ratio=0.15, plot_buckets=250.
알 수 없는 설정, 비유한 수, 양수가 아닌 임계값, 평가 집합을 남기지 않는 비율은 거절한다.
plot_buckets는 10~2000 정수다. 추가 패키지 설치는 필요 없다.

## 입력과 채택

[기록 규약](RecordingSpecification.md)의 schema v1 네 파일을 다시 검증한다.
검증기의 `validate()`가 계산한 랩 경계만 사용한다. 형식 오류·비정상 종료·샘플/이벤트 드롭은
세션 전체를 거절한다. 파일 SHA-256을 처리 전후 비교해 실행 중 원본 변경도 거절한다.

완전한 랩은 같은 segment의 연속 경계 사이이며, 시작 포함·끝 제외다.
lap_id는 세션 ID + segment + 시작 sequence다. 게임 완료 랩 카운터를 랩 ID로 사용하지 않는다.
랩의 어느 샘플이라도 lap_valid=false 또는 in_pit=true이면 제외한다.
랩 내부 간격과 마지막 샘플→종료 경계 간격 중 최대가 max_gap_ms를 초과하면 제외한다.
이전 랩에서 넘어온 첫 샘플의 interval_ns는 품질 계산에서 제외한다.

제외 이유는 `invalid_lap`, `in_pit`, `sample_gap`이며 복수 이유는 세미콜론으로 연결한다.
완전한 랩에 속하지 않은 행은 `incomplete_lap`으로 기록한다. 제외 구간 CSV의
first_sequence와 last_sequence는 모두 포함 범위다. 구간 시작·끝의 원인은 원본 events.csv로 추적한다.
사고·스핀의 원인을 확정하지 않으며 원시 사용자 메모도 자동 사고 라벨로 변환하지 않는다.

## 결과

datasets 아래 Unix 나노초와 무작위 접미사로 고유 디렉터리를 만든다. 기존 폴더와 충돌하면 거절한다.
입력 세션 내부를 출력 경로로 지정할 수 없다. 초기 manifest는 complete=false이며 모든 파일이
저장된 뒤 최종 manifest를 교체한다. 실패는 종료 코드 1, 정상 생성은 0이다.
저장 장치 자체의 오류로 최종 오류 메시지를 파일에 쓰지 못하더라도 초기 미완료 상태를 유지한다.

| 파일 | 규약 |
| --- | --- |
| manifest.json | 빌더/스키마 버전, 설정, 원본 경로·해시·metadata, 필드 순서, 원본 단위, 랩별 품질, 집계와 완료 상태 |
| validation.json | 이전 보고서 복사가 아닌 이번 원본 검증 결과 |
| laps.csv | 완전한 랩 전체의 경계·순번·채택/제외·집합·시간·샘플 수·최대 공백·근사 섹터 시간 |
| excluded_intervals.csv | 제외한 랩과 부분 구간의 sequence 범위·segment·샘플 수·이유 |
| laps/*.csv | 채택 랩만, 원본 열/순서/값 보존 후 session_id·lap_id·lap_elapsed_ns 추가 |
| splits.json | train, validation, test의 시간순 lap_id 배열 |
| report.html | 네트워크 의존성 없는 SVG 포함 HTML, 모든 완전한 랩의 품질·그래프 |

추가 ID는 문자열이고 lap_elapsed_ns는 랩 시작 이후 호스트 monotonic 나노초다.
원본 시간 필드는 ns, 위치는 m, speed_kmh는 km/h, heading_rad는 rad다.
조향은 보정하지 않은 ACC 원시값, 페달은 0..1, progress는 0..1 비율이다.
타이밍 통계는 ms, 랩과 섹터는 초이며 CSV에 샘플을 보간하거나 반올림하여 다시 쓰지 않는다.
laps.csv의 accepted는 Python CSV 표기 `True`/`False`, 원본 bool은 `true`/`false`를 유지한다.

## 분리 및 재현성

채택 랩을 원본 시간순으로 배정한다. train=floor(N×0.70), validation=floor(N×0.15), test=나머지다.
N≥3이면 train을 1..N-2로 제한하고 validation을 1..남은 수-1로 제한한다.
N<3이면 세 배열을 비우고 split_complete=false로 기록한다. 추출 성공 complete와 구분한다.
채택했으나 분리하지 못한 랩은 unassigned, 제외 랩은 excluded다.
동일 세션에서 학습 7 / 검증 1 / 평가 3으로 나누어도 다른 조건에 대한 일반화를 보장하지 않는다.
현재 Top 3 기록은 이미 확인했으므로 평가 집합은 완전히 미관측된 벤치마크로 주장하지 않는다.

원본·설정·코드 버전이 같으면 선택, 분리와 CSV 바이트가 같다. 생성 시각과 빌드 ID는 달라진다.
정규화·특징 생성·조작 시차 보정·학습을 수행하지 않는다. 조작 열을 그대로 모델 입력에 넣지 않는다.

## 시각화와 알려진 한계

X–Z는 동일 축척으로 표시한다. 주행 궤적을 도로 중심선이라고 해석하지 않는다.
빌더 0.1.1부터 동일 축척 유지에 필요한 여백도 축 좌표 범위에 포함한다.
눈금과 궤적을 같은 좌표 변환으로 배치하므로 X축 양끝 숫자는 궤적 자체의 최솟값·최댓값과 다를 수 있다.
고정값만 있는 축은 최소 범위를 확보해 중앙에 표시한다.
진행률별 속도·페달·조향 그래프는 순서대로 그리며 출발선 wrap에서 선을 끊는다.
plot_buckets개 이하의 시간 구간에서 첫/끝 및 X/Y 극값 샘플을 유지한다. 원본 CSV에는 축소를 적용하지 않는다.
섹터는 0→1, 1→2 첫 관측 시각으로 계산하고 마지막은 랩 종료 경계까지다.
두 전환이 관측되지 않으면 섹터 시간을 비워둔다. 공식 ACC 타이밍과 약 10ms 이상의 차이가 날 수 있다.
페이지 변경 간격은 서로 다른 changed_ns 사이의 간격이다. 전체 제어 지연이나 게임 생성 시각이 아니다.
현재 구현은 입력 CSV를 메모리에 읽는다. 이번 규모에는 적합하며 대규모 세션 스트리밍은 후속 과제다.

인쇄용 CSS는 A4 세로·12mm 여백이며 각 랩을 새 페이지에서 시작한다. 요약·통계 표와
그림(제목+SVG)의 내부 페이지 분할을 피하고, 그래프는 인쇄 시 두 열로 배치한다.
화면 표시용 반응형 배치는 유지한다. 인쇄 배율·브라우저 설정에 따라 페이지 수가 달라질 수 있으며
0.1.1은 사용자 제공 PDF 16쪽에서 축 눈금과 랩별 페이지 배치를 재검증했다.
요약 1쪽 및 랩별 15쪽이며 각 랩의 통계 표와 다섯 그래프가 같은 페이지에 유지된다.

개발 및 검증 근거: [개발 기록](DatasetBuilderDevelopmentLog.md).

## 재사용 주행 검토 — review 0.1.0

빌더 버전과 별도로 관리하는 오프라인 검사다. Python 3.11 표준 라이브러리를 사용한다.
후보 탐지와 최종 판정을 분리하며 원본·기존 데이터셋·분리 목록은 변경하지 않는다.

```powershell
python -m DatasetBuilder.review scan --dataset datasets/1790672790720927100-cad5b45e --config DatasetBuilder/review.toml
python -m DatasetBuilder.review finalize --review reviews/scan-1790674428796657400-4cac817c --decisions reviews/scan-1790674428796657400-4cac817c/decisions.reviewed.json
python -m unittest DatasetBuilder.test_builder DatasetBuilder.test_report DatasetBuilder.test_review -v
```

첫 명령은 새로운 검사 폴더를 출력한다. 그 폴더의 `decisions.template.json`을 별도 파일로 복사하고
기록·그래프 검토 결과를 작성한 뒤 두 번째 명령에 전달한다. 위 finalize 예시는 이번 검토의 실제 파일이다.
새 검사에서는 출력된 검사 경로와 해당 판정 파일을 사용한다. 설정 생략 시 `DatasetBuilder/review.toml`을 읽는다.

### 입력 검증과 비교 기준

- schema v1, `complete=true`, `split_complete=true`인 데이터셋만 받는다.
- 원본 네 파일의 해시를 manifest와 비교하고 기록 검증기를 다시 실행한다. 샘플·이벤트 누락을 거절한다.
- 완전한 랩의 경계·채택 여부·이유·시간·샘플 수를 재계산하고, 랩 CSV의 모든 원본/추가 필드를 원본 행과 대조한다.
- 분리 규칙과 `splits.json`, 랩 표, CSV 파일 목록이 일치해야 한다. 제외 범위까지 원본의 모든 행을 한 번씩 설명해야 한다.
- 검사 전후, 확정 전후에 입력 해시를 비교한다. 불일치 시 실패 종료하며 결과 manifest를 미완료로 남긴다.

진행률은 기본 1,000구간(0.1%)이다. 구간마다 각 train 랩의 X, Z, 속도, 원시 조향 중앙값을
먼저 구하고 랩별 대표값 사이의 중앙값과 MAD를 계산한다. MAD는 보정 계수를 곱하지 않은
`median(abs(value - median(value)))`다. X–Z 중심은 좌표별 중앙값이며 각 대표 궤적의 중심까지
거리에도 중앙값/MAD를 계산한다. 각 랩은 구간별 한 표를 가지므로 샘플 수로 가중하지 않는다.
후보 검사는 원래 샘플을 기준 통계와 대조하며 값을 보간하지 않는다.

validation/test 검사에는 train 전체의 기준을 쓴다. train 검사에는 해당 랩을 제외한 기준을 쓴다.
구간에 비교 가능한 train 랩이 3개 미만이면 `insufficient_reference` 후보로 남긴다.
판정 후 보류 랩이 생겨도 이번 검사 기준을 자동 재계산하지 않는다. 다음 경로 모델의 입력은
확정 결과의 사용 가능한 train만 사용해야 한다.

### 탐지 설정

기본값 전체는 [review.toml](../DatasetBuilder/review.toml)에 있다. 알 수 없는 키, 잘못된 자료형,
비유한 수와 양수가 아닌 값은 거절한다. 검사 결과를 보고 임계값을 자동 조정하지 않는다.

| 후보 | 기본 조건 |
| --- | --- |
| trajectory_difference | X–Z 중심과의 거리 > max(5m, 기준 거리 중앙값 + 6×MAD) |
| speed_difference | 속도 중앙값과의 절대 차이 > max(20km/h, 6×MAD) |
| steering_difference | 원시 조향 중앙값과의 절대 차이 > max(0.35, 6×MAD) |
| low_speed | 속도 < 20km/h가 2초 이상 |
| reverse_progress | 약 100ms 창의 진행률 감소가 0.002 이상, 정상 wrap 제외 |
| position_discontinuity | 약 100ms의 X–Z 이동 거리 > 창 내부 최대 속도(m/s)×경과 초×2 + 5m |

첫 세 후보는 연속된 임계값 초과 샘플의 처음~마지막 관측 시각이 0.2초 이상일 때 생성한다.
실제 `elapsed_ns`로 계산하며 마지막 초과 샘플 이후까지 지속했다고 추정하지 않는다.
시간 창은 현재 시각보다 최소 100ms 전인 가장 가까운 샘플을 사용하고 기본 허용 범위는 100~130ms다.
그 범위의 샘플이 없거나 랩 시작에서 창이 충분하지 않으면 해당 창 검사를 수행하지 않는다.
따라서 후보 없음은 모든 시각에 충분한 관측이 있었다는 뜻이 아니다.
진행률 0.8 초과→0.2 미만 전환을 넘는 창은 역행·위치 도약 검사에서 제외한다.
각 랩을 독립 검사하고 서로 다른 segment나 역전된 시간이 섞이면 거절한다.
0.3초 이하 간격의 후보를 병합하며 탐지 이유와 해당 증거 샘플은 모두 보존한다.
핫랩 연료·랩타임 초기화는 사고 판정에 사용하지 않는다.

### 출력과 판정

`output_dir` 기본값은 실행 디렉터리 기준 `reviews`다. 고유한 `scan-…`, `final-…` 폴더를 만들고
기존 폴더와 충돌하면 거절한다. 입력 안에 출력 폴더를 만들 수 없다.
모든 파일을 저장·확인한 후에만 manifest의 `complete`를 true로 교체한다.
오류 시 종료 코드 1과 가능한 경우 manifest의 `error`를 남긴다. 저장 장치 오류로 오류 내용조차
기록할 수 없다면 초기 미완료 manifest 또는 manifest 부재 자체를 실패로 취급한다.

| 검사 파일 | 내용 |
| --- | --- |
| manifest.json | 도구 버전, 설정, 입력 경로와 SHA-256, 검사 결과 해시, 집계, 완료 여부 |
| baselines.json | train ID와 전체 train 기준, train별 자신을 제외한 기준; 구간별 중앙값·MAD·적용 임계값·자료 수 |
| findings.json | 채택 랩별 후보, 원본 sequence/시간/진행률, 관측 차이·기준값·기준 통계, 기존 제외 근거·원본 이벤트 |
| report.html | 후보가 있는 랩부터 표시; 모든 채택 랩 전체 그래프와 후보 전후 3초 문맥의 다섯 그래프 |
| decisions.template.json | 모든 랩/후보가 미검토인 판정 초안 |

`report.html`은 독립 HTML/SVG이며 같은 랩의 범위에서만 문맥을 표시한다.
전체 신호는 진행률, 후보 문맥 신호는 랩 시작 이후 호스트 초를 가로축으로 사용한다.
기존 극값 보존 축소를 그림에만 적용한다. 관측된 조향을 패드 축 또는 바퀴 각도로 변환하지 않는다.
후보 수치의 `observed`는 탐지 식에 사용한 거리/절대 차이/속도/진행률 차이이며 원시 행은 sequence로 추적한다.

판정 파일은 schema_version=1과 `laps` 배열을 갖는다. 각 항목에는 `lap_id`, `status`,
`full_lap_reviewed=true`, 비어 있지 않은 `reason`, `reviewer`, 시간대가 있는 ISO 8601 `reviewed_at`,
`candidates`가 필요하다. 후보마다 `candidate_id`, `resolution`, `reason`을 적는다.

| status | 의미 |
| --- | --- |
| eligible | 검토 범위에서 후속 사용을 막는 문제가 확인되지 않음 |
| hold | 해소되지 않은 의심 또는 부족한 증거가 있어 사용 보류 |
| exclude | 사용에 부적합하다는 기록 근거가 있어 사용 제외 |

초안의 `unreviewed`는 확정할 수 없다. 모든 랩/후보가 정확히 한 번 있어야 하며 알 수 없는 ID,
빈 근거, 검토자/시각 누락을 거절한다. `eligible`은 모든 후보가 `resolved`여야 하고,
비교 자료 부족 후보가 있다면 허용하지 않는다. 해소하지 못한 후보는 `unresolved`로 보존한다.
이는 판정 형식과 미해결 상태를 확인하는 장치이며 자유문으로 쓴 근거의 진실성을 자동 인증하지 않는다.

확정 폴더는 `manifest.json`, 입력 판정의 복사본 `decisions.json`, 후속 목록 `usage.json`을 갖는다.
`usage.json`의 `eligible.train/validation/test`는 원래 집합 안에서만 선별한다. `hold`, `exclude`도
보존하며 `trackmodel_candidates`는 `eligible.train`과 같다. 빈 목록이면 `trackmodel_ready=false`이고
경로 생성을 시작해서는 안 된다. 모두 보류한 검토도 판정 자체는 완료될 수 있다.
현재 기능은 후보 목록만 전달하고 경로 생성은 실행하지 않는다.

같은 입력·설정·도구 버전의 기준 통계, 후보, 보고서, 초안은 동일하다. 같은 판정 파일의 확정 목록과
판정 복사본도 동일하다. 실행 경로·생성 시각·고유 ID는 달라진다. `reviews/`는 대용량 로컬 결과로
Git에서 제외되므로 다른 장치로 옮길 때 원본/데이터셋/검토 폴더를 함께 보존해야 한다.

이번 실제 검토: [11랩 판정과 다음 단계 목록](DrivingDataReview.md).
접촉·스핀·도로 경계를 확정하는 필드는 부족하다. 후보나 조향 변화만으로 사고를 확정하지 않으며
검토 통과를 무사고 증명으로 쓰지 않는다. 단일 세션의 품질 점검으로, 독립적인 모델 성능 평가는 아니다.
