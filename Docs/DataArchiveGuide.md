# 교사 기록 선별과 보관 이력

2026-10-18 기준. 10월 17일 게임패드 교사 자료의 선별 결과, 원본 위치와 복원 절차다. 이후 추가 수집분을 자동으로 포함하는 목록이 아니며 학습 입력은 설정이 가리키는 고정 split을 따른다.

## 10월 17일 선별 기준과 현재 split

[보관 manifest](../recording_archives/teacher-cleanup-20261017/manifest.json)와 [선별 split](../input_profiles/gamepad/teacher-cleaned-split-20261017.json)은 같은 선별 이력을 담는다. 완전한 출발선 간 랩이 무효 랩을 포함해 2개 이하인 짧은 세션 7개를 `recording_archives/teacher-cleanup-20261017/short-sessions/<원래 세션 ID>/`로 옮겼다. 이동 전·후 절대경로와 7개 원본 파일 각각의 SHA256은 manifest의 `excluded_sessions`에 있다.

검증 세션 `1792229877488002900-2756-0`의 원래 7개 파일은 `recording_archives/teacher-cleanup-20261017/original-validation/1792229877488002900-2756-0/`에 보존했다. 현재 `recordings/`의 이 세션은 첫 유효 6랩이 끝나는 관측 `elapsed_ns=553480827600`까지 유지한다. 마지막 출발선 관측은 6번째 랩의 끝점으로 포함하고 이후 입력·관측은 제외했다. 553.4808276초에는 첫 완전한 랩 이전 접근 구간도 포함된다.

| 용도 | recordings 아래 세션 ID | 유지 시간 | 완전 랩 / 유효 랩 |
| --- | --- | --- | --- |
| train | 1792219086588317300-32076-0 | 830.9473305초 | 9 / 3 |
| train | 1792220398677202600-31248-0 | 861.2971606초 | 9 / 5 |
| train | 1792221610643281600-9544-0 | 1457.6673445초 | 16 / 7 |
| train | 1792228050509355600-30016-0 | 1531.5473664초 | 17 / 13 |
| validation | 1792229877488002900-2756-0 | 553.4808276초 | 6 / 6 |

[집계](../input_profiles/gamepad/teacher-cleaned-totals-20261017.json)의 합계는 5,234.9400296초, 완전 랩 57·유효 랩 34, 이동 거리 약 228,051.85m다. 거리는 같은 segment·0~0.1초 간격·최대 끝점 속도에 따른 이동 한계 안의 3D 변위를 합하며 접근·부분 랩도 포함한다. 랩 길이 합계로 바꾸어 해석하지 않는다.

선별 집계의 `excluded_intervals=0`은 이 보관 작업 당시의 값이다. 후속 증강의 유효→무효 전환 전후 제외 시간대와는 별개다. 세션 train은 무효 관측도 포함할 수 있고 validation은 유효 관측만 선택한다. [학습 규약](GamepadTrainingSpecification.md)을 따른다.

## 경로와 해시의 사용

`TrainingEnvironment/session-teacher-20261017.toml`이 위 split을 읽는다. 과거 결과의 `input_sha256`는 당시 경로·내용을 고정하므로 파일 이동이나 validation 절단 이전 결과의 입력 검증은 현재 위치에서 실패할 수 있다. 이는 과거 결과를 새 자료로 검증한 것으로 바꿀 이유가 아니다.

보관한 원본·당시 설정·manifest를 함께 확보하고, 필요한 재현은 별도 사본에서 수행한다. 모델의 `output_sha256`와 원자료 `input_sha256`를 구분한다. `original-validation` 파일은 보관 위치에 있으며, 보관 manifest가 그 원본 전체의 별도 해시 목록까지 제공하는 것으로 간주하지 않는다. 최초 선별 전 상태는 [teacher-cleanup-before.json](../input_profiles/gamepad/teacher-cleanup-before.json)에 있다.

## 원본 복원 절차

1. 현재 학습·기록 프로세스를 종료하고 복원할 manifest 항목의 `original`, `archive` 절대경로를 확인한다. 두 경로가 의도한 저장소의 `recordings/`와 `recording_archives/` 안에 있는지 확인한다.
2. 짧은 세션은 보관본 7개 파일의 해시를 `excluded_sessions[].sha256`와 비교한다. 원래 위치가 비어 있을 때만 새 디렉터리로 복사한다. 현재 파일을 덮어쓰거나 보관본을 이동하지 않는다.
3. 잘린 validation의 원본은 먼저 별도 재현 폴더에 복사한다. 현재 6랩 사본과 split·현재 모델은 그대로 보존한다. 원래 경로가 꼭 필요한 재현은 현재 사본을 별도 백업하고 그 경로의 사용자를 확인한 뒤 수행한다.
4. 복사 후 파일 해시·기록 기본 검증·입력 stream·랩 경계를 다시 확인하고 새 검증 결과 파일을 만든다. validation 원본의 랩 수와 길이는 현재 6랩 사본과 달라야 하며 원래 summary와 대조한다.
5. 학습하려면 복원 사본을 명시한 새 split·새 설정·새 결과 폴더를 만든다. 과거 모델의 manifest나 검증 결과를 수정하지 않는다.

## 저장과 버전 관리 기준

| 자료 | 보존과 Git 기준 |
| --- | --- |
| 코드, 일반 TOML, 재사용할 GUI 프리셋, Docs | 소스와 함께 관리. 특정 PC 절대경로는 용도와 이전 절차를 문서에 표시 |
| recordings, recording_archives | 원본과 선별 전 사본을 별도 데이터 백업에 보존. 대용량 원자료를 일반 소스 커밋에 일괄 추가하지 않음 |
| input_profiles | ACC 설정·장치 경로·DLL·검증 JSON을 원자료와 함께 로컬 보존. 장치·PC별 자산이므로 소스 복제만으로 제공되지 않음 |
| reinforcement_runs, training_runs, trained_models | 재현 가능한 결과도 당시 선택 모델·manifest·정규화·checkpoint·비교 근거는 함께 보존 |
| actuator_runs, state_runs, track_models, datasets, reviews | 결과 provenance와 선택 근거를 함께 보존. 실패 결과도 분석 근거로 유지 |
| target, venv | 다시 생성할 수 있지만 바이너리·의존성 버전과 실행 해시 관계를 보존 |

[현재 .gitignore](../.gitignore)는 대부분의 원자료·출력 폴더를 제외하지만 `recording_archives/`는 등록하지 않았다. 따라서 해당 폴더가 미추적으로 보이더라도 전부 소스에 추가하지 않는다. 문서 작업에서는 ignore 설정이나 원본 데이터를 변경하지 않았다. 다른 PC에서 재현하려면 소스 외에 위 로컬 자료와 외부 드라이버/DLL을 별도로 확보하고 해시를 확인해야 한다.
