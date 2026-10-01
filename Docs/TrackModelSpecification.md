# TrackModel 규약 v1

도구 버전 0.1.0. Python 3.11 표준 라이브러리로, 기록 기반 검토를 통과한 train 중 가장 빠른 실제
1랩을 폐곡선 기준 주행선으로 변환한다. 도로 중심선·최적 레이싱 라인·추종 가능성을 보장하는 모델은 아니다.

## 실행과 Python 인터페이스

프로젝트 루트에서 실행한다. 설정을 생략하면 모듈에 포함된 config.toml을 읽는다.

```powershell
python -m TrackModel --review reviews/final-1790674957929734700-befda832 --config TrackModel/config.toml
python -m unittest DatasetBuilder.test_builder DatasetBuilder.test_report DatasetBuilder.test_review TrackModel.test_model -v
```

```python
from TrackModel.model import build, load_reference

folder = build("reviews/final-1790674957929734700-befda832")
reference = load_reference(folder)
metadata = reference["manifest"]
points = reference["rows"]       # CSV 순서의 dict 목록; index는 int, 나머지는 float
validation = reference["validation"]
```

`build(review, values=None)`의 두 번째 인자는 TOML과 같은 설정 dict다. 설정 생략 시 같은 기본값을 쓴다.
반환값은 새 출력 폴더의 Path다. 입력 불일치·기하 품질 실패는 ValueError 계열, 저장 실패는 OSError를
발생시킨다. CLI는 실패 시 이유를 출력하고 종료 코드 1, 성공 시 결과 경로와 종료 코드 0을 반환한다.

`load_reference(folder)`는 원본 세션 없이 완성된 결과 폴더만 읽는다. 완료 상태·지원 버전·세 출력의
해시·CSV 열과 단위·좌표 규약을 검사한다. 좌표에서 거리·접선·곡률·품질 통계를 다시 계산해 저장 값과
대조한다. 생성에 사용된 원본 해시는 출처 기록이며 로더가 원본의 존재를 요구하지 않는다.
Rust 실시간 로더, 위치 투영·횡오차·heading 오차·목표 속도는 이번 인터페이스에 포함하지 않는다.

## 입력 검증과 선택

입력은 DatasetBuilder review 0.1.0의 `kind=final`, `complete=true`, schema v1 폴더다.
원본 검사 결과의 경로를 따라 아래 항목을 다시 확인한다.

1. 확정 manifest가 저장한 원본/데이터셋/검사/원래 판정 파일 해시와 실제 파일의 일치.
2. 확정 폴더의 판정 복사본과 원래 판정의 의미상 일치, 검사 결과와 원본 데이터셋의 랩 목록 일치.
3. 기존 `load_dataset()`을 이용한 원본 검증, 랩 CSV 전체 행·분리 목록·제외 범위 재대조.
4. 공통 `decision_usage()`로 판정에서 재계산한 집합별 사용 목록과 usage.json·집계의 일치.

생성 전후 입력 파일 해시를 확인한다. 무결성 검사에서는 기존 데이터셋 전체를 읽지만 경로 선택과
수치 계산은 `trackmodel_candidates`의 eligible train에만 의존한다. 보류·validation·test는 후보로 복원하지 않는다.
원래 리뷰가 사용한 절대 경로의 파일들이 생성 시 접근 가능해야 한다. 입력/설정 자체가 읽히지 않으면
출력 폴더 생성 전에 실패할 수 있다.

후보의 `(end_elapsed_ns - start_elapsed_ns)`를 정수 나노초로 비교한다. 최솟값을 선택하고 동률이면
start_elapsed_ns, 마지막으로 lap_id 순서로 정한다. 후보별 시간과 선택된 ID를 manifest에 남긴다.
이는 ACC 공식 랩타임이 아닌 기존 기록의 호스트 관측 랩 시간이다.
후보가 없거나 선택 랩이 품질 검사에 실패하면 실패 종료한다. 차순위 랩으로 자동 교체하지 않는다.

## 경로 생성 알고리즘

- 시간순 XYZ에서 연속 X–Z 거리 1e-6m 이하인 점을 합치며 첫 점의 Y를 유지한다. 끝에 첫 점이 명시적으로
  반복되면 그 끝점을 제거한다. 세 점 미만, 비유한 XYZ는 거절한다. 원본 CSV는 변경하지 않는다.
- 원본 끝→시작의 X–Z 간격이 설정된 max_closure_m 이하일 때 직선으로 닫는다. 닫힘 간격은 원본 끝점 기준으로 보고한다.
- 각 변의 X–Z 길이를 합한 L에 대해 `N=ceil(L/spacing_m)`, `step=L/N`으로 정하고 0..N-1 지점에 선형 보간한다.
  XYZ 모두 보간하지만 거리 계산에는 Y를 쓰지 않는다. 3~1,000,000점 범위를 벗어나면 거절한다.
- 균등 거리 격자에 순환 2차 최소제곱 평활화를 한 번 적용한다. 기본 창은 중심과 앞뒤 5점, 총 11점이다.
  대칭 오프셋 k에 대해 `S2=sum(k²)`, `S4=sum(k⁴)`, 가중치 `(S4-S2*k²)/(window*S4-S2²)`로 중심 예측값을 구한다.
  중심과의 좌표 차이에 가중치를 적용해 월드 좌표가 클 때의 정밀도 손실을 줄인다.
- 평활 전후 같은 격자 점의 X–Z 이동량을 측정한다. 평활 곡선을 다시 같은 방식으로 거리 재표본화하고
  최종 좌표의 실제 현 길이로 누적 거리와 전체 길이를 계산한다. 보간 목표 간격과 실제 현 길이는 약간 다를 수 있다.
- 접선은 앞뒤 tangent_span점 사이의 X–Z 현 방향을 정규화한다. 앞→현재 벡터 u, 현재→뒤 벡터 v일 때
  곡률은 `2*(u.x*v.z-u.z*v.x)/(|u|*|v|*|u+v|)`다. 세 점이 일직선이면 0, 현이나 분모가 퇴화하면 실패한다.

모든 처리는 원본 순서를 유지한다. 가까운 좌표 순으로 재정렬하거나 랩/segment를 이어 붙이지 않는다.
폐곡선의 끝과 시작에서도 같은 순환 이웃을 사용한다. 곡률은 공간 창에서 계산한 근삿값이며 작은 변동이 남을 수 있다.

## 설정과 품질 상한

[기본 설정](../TrackModel/config.toml)의 output_dir은 실행 디렉터리 기준이다. 알 수 없는 키,
잘못된 자료형, 비유한 값과 양수가 아닌 값은 거절한다. smoothing_window는 5~201 홀수,
tangent_span은 1~201 정수다. 경로 점 수가 창 또는 앞뒤 span을 지원하지 못하면 실패한다.

| 설정 | 기본값 | 의미 |
| --- | ---: | --- |
| output_dir | track_models | 결과 폴더의 상위 경로 |
| spacing_m | 1.0 | 목표 X–Z 간격(m) |
| smoothing_window | 11 | 2차 평활 창의 점 수 |
| tangent_span | 5 | 접선·곡률에서 양쪽으로 참조할 점 수 |
| max_closure_m | 5.0 | 원본 끝→시작 거리 상한(m) |
| max_smoothing_displacement_m | 0.5 | 평활 전후 대응점 이동 상한(m) |
| max_length_change_ratio | 0.005 | 원래 폐곡선 대비 최종 길이 변화의 절대 비율 상한, 0.5% |
| max_spacing_error_ratio | 0.01 | 최종 재표본화 목표 간격 대비 실제 각 변 오차 상한, 1% |
| max_tangent_step_degrees | 10.0 | 인접 접선의 감긴 각도 차이 상한, 출발선 포함 |

전체 경로·닫힘 변을 검사한다. 출력에는 영 길이 변·중복 끝점이 없어야 하고 누적 거리는 엄격히 증가해야 한다.
좌표·접선·방향·곡률은 모두 유한해야 한다. 품질 기준 초과는 실패이며 임계값을 자동 조정하지 않는다.

## 출력 규약과 오류

`track_models/track-<Unix ns>-<무작위 접미사>/`를 독점 생성한다. 입력 원본·데이터셋·scan·final 내부에
출력할 수 없으며 기존 결과와 충돌하면 거절한다. `/track_models/`는 Git에서 제외한다.

| 파일 | 내용 |
| --- | --- |
| manifest.json | schema/tool 버전, 완료 상태, 입력/출력 SHA-256, 차량·트랙 identity, 후보/선택, 설정, 필드·단위·좌표 규약, 점 수와 길이 |
| reference.csv | 아래 필드 순서의 기준 경로; 첫 점을 끝에 중복 저장하지 않음 |
| validation.json | input_pass, passed, metrics, 적용 상한 limits, 오류 errors |
| report.html | 외부 자원 없는 HTML/SVG, 선택·수치 표, 전체/출발선 중첩과 네 신호 그림 |

CSV 열 순서는 다음과 같다.

```text
index,s_m,position_x,position_y,position_z,tangent_x,tangent_z,heading_path_rad,curvature_per_m
```

- index: 0부터 연속 정수. s_m: 최종 경로의 첫 점부터 잰 X–Z 누적 거리(m).
- XYZ: ACC 원시 월드 좌표계, 미터. 보간·평활화된 위치다. 단위 접선은 X–Z 성분이다.
- heading_path_rad: `atan2(tangent_z,tangent_x)`, [-π,π]. ACC heading과의 부호·원점 대응을 가정하지 않는다.
- curvature_per_m: X에서 Z 쪽으로 회전할 때 양수인 평면 곡률(1/m). ACC 조향 부호와는 별도다.
- `s=0`: 원본 첫 관측점에 대응하는 평활점. 물리적 출발선의 정확한 측량 위치가 아니다.
- 전체 길이는 마지막 점→첫 점 변까지 포함한다. 마지막 행 s_m은 전체 길이보다 작다.
  ACC progress와 `s_m/전체 길이`를 같은 값으로 간주하지 않는다.

처음 manifest는 complete=false다. 모든 출력 저장, 저장한 CSV의 기하 재검사와 입력 해시 재검사가
끝나야 true로 교체한다. 실패 시 미완료와 오류를 남기며 가능한 진단 보고서를 보존한다.
저장 장치 자체가 실패하면 오류 파일도 저장되지 않을 수 있고, manifest 부재/미완료 결과는 사용할 수 없다.
실패 폴더 안의 reference.csv는 진단용일 수 있으므로 파일 존재만으로 완료라고 판단하지 않는다.

동일 입력·설정·도구 버전은 reference.csv, validation.json, report.html을 바이트 단위로 재생성한다.
manifest의 빌드 ID·시각·출력 위치는 달라질 수 있다. 출력 해시는 우발적인 변경 감지용이며 외부 서명은 아니다.

## 실제 검증 결과와 다음 단계

[실제 결과 manifest](../track_models/track-1790677262927433400-796fb617/manifest.json),
[품질 보고서](../track_models/track-1790677262927433400-796fb617/report.html),
[경로 CSV](../track_models/track-1790677262927433400-796fb617/reference.csv).

5번 랩(89.1700052초), 3,886점, X–Z 길이 3,885.199156m를 생성했다. 최대 평활 이동 0.049097m,
길이 변화 약 0.000539%, 최종 간격 오차 약 0.000217%, 인접 방향 변화 최대 2.171183도,
출발선 연결부 0.253597도다. 모든 기본 상한을 통과했다.

전체와 출발선 확대, 높이·방향·곡률·이동량의 SVG 6개를 독립 문서 렌더러로 표시해 확인했다.
원래/생성 궤적이 일치하는 범위와 출발선 연결을 확인했으며 HTML 브라우저 배치·인쇄 검증을 의미하지 않는다.
67개 자동 테스트, 두 실제 빌드의 동일 출력, 27개 입력 해시 불변과 파일 재읽기를 검증했다.
상세 내용은 [개발 기록](TrackModelDevelopmentLog.md)에 있다.

후속 작업은 StateProcessor의 경로 투영·횡오차·방향 오차다. 제어 사용 전 ACC heading·조향 대응과
실제 추종을 검증해야 한다. 이번에는 속도 목표나 제어 입력을 생성하지 않는다.
