# 폴포지션 문서 목차

2026-10-18 갱신. 현재 실행은 아래 운영·규약 문서를 따르고, 날짜별 개발 로그는 당시 결과와 결정 근거로 읽는다. 소스·설정·저장 결과가 충돌하면 현재 파일을 대조해 적용 범위와 미완료 상태를 갱신한다.

## 실행과 현재 상태

| 목적 | 문서 |
| --- | --- |
| 프로젝트 시작과 기본 실행 | [루트 README](../README.md), [환경 구성](EnvironmentSetup.md) |
| 현재 구현과 남은 개발 과제 | [개발 체크리스트](DevelopmentChecklist.md) |
| GUI 작업·중지·결과·재개 | [GUI 실행 규약](TrainingControllerSpecification.md) |
| 게임패드 수집·HidHide·교정·복원 | [운영 안내](GamepadOperationGuide.md) |
| 실제 선택할 모델·프리셋·허용 시간 | [모델과 설정](CurrentGamepadModels.md) |
| 날짜별 검증과 판정 범위 | [검증 기록](VerificationLog.md) |

## 데이터와 모델 규약

| 단계 | 규약과 관련 결과 |
| --- | --- |
| 기록과 랩 경계 | [기록 규약](RecordingSpecification.md), [기록 선별과 보관](DataArchiveGuide.md) |
| 데이터셋·검토 | [DatasetBuilder](DatasetBuilderSpecification.md), [주행 자료 검토](DrivingDataReview.md) |
| 기준 경로·상태 | [TrackModel](TrackModelSpecification.md), [StateProcessor](StateProcessorSpecification.md) |
| vJoy 입력·저속 기준 제어 | [차량 입력](VehicleInputSpecification.md), [BaselineController](BaselineControllerSpecification.md), [한 바퀴 고저차 분석](OneLapTerrainReview.md) |
| PID v1 학습 | [모델 학습 규약](ModelTrainingSpecification.md) |
| 원시 게임패드 학습·특징·증강 | [게임패드 학습 규약](GamepadTrainingSpecification.md), [실험과 실패 분석](GamepadExperimentReview.md) |
| 수동 준비 기반 SAC v2 | [강화학습 실행 규약](TrainingEnvironmentSpecification.md) |

## 계획과 개발 이력

- [컨트롤러 최초 계획과 현재 연결 상태](TrainingControllerPlan.md)
- [컨트롤러 개발 기록](TrainingControllerDevelopmentLog.md)
- [DatasetBuilder 개발 기록](DatasetBuilderDevelopmentLog.md)
- [TrackModel 개발 기록](TrackModelDevelopmentLog.md)
- [StateProcessor 개발 기록](StateProcessorDevelopmentLog.md)
- [차량 입력 개발 기록](VehicleInputDevelopmentLog.md), [Xbox USB 입력 조사와 복구](XboxUsbInputInvestigation.md)
- [BaselineController 개발 기록](BaselineControllerDevelopmentLog.md)
- [PID 모델 학습 개발 기록](ModelTrainingDevelopmentLog.md)

## 문서 갱신 기준

| 변경 | 함께 갱신할 문서 |
| --- | --- |
| 기능 연결·새 작업·화면 입력·재개 형식 | GUI 규약, 체크리스트, 현재 상태가 바뀌면 루트 README |
| 장치·프로필·버튼·중지·해제·필터 | 차량 입력 규약/게임패드 운영 안내, 검증 기록 |
| 특징·정답·split·정규화·모델 형식 | 해당 학습 규약, 실험 검토, 필요 시 모델 표 |
| 모델·프리셋·시험 시간·채택 상태 | 모델과 설정, 실험 검토, 체크리스트 |
| 기록 스키마·보관·선별·경로 | 기록 규약, 자료 보관, 입력에 영향받는 학습 규약 |
| 실험과 테스트 | 날짜별 개발 기록, 결과 파일 연결, 공통 검증 색인 |
| 환경·의존성·설치 자산 | 환경 구성, 버전 고정 파일과 해당 장치 규약 |

현재 규약에는 최신 사용법을 반영하고 과거 결과·실패 원본은 날짜와 근거를 유지한다. 계획은 목표로, 구현은 코드 존재로, 검증은 명시한 조건의 증거로 구분한다. 경로·수치·선정 상태를 변경할 때 관련 manifest와 summary를 확인한다. 로컬 생성 결과는 소스 저장소에 없을 수 있으므로 [보관 기준](DataArchiveGuide.md)을 함께 전달한다.
