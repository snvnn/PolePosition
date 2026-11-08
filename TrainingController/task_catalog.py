"""Single task catalog; importing it never imports a learner or opens a device."""
from dataclasses import dataclass
from importlib import import_module


@dataclass(frozen=True)
class TaskSpec:
    id: str
    label: str
    group: str
    fields: frozenset[str]
    builder: str
    default_config: str | None = None
    resumable: bool = False
    max_seconds: int = 7200
    allow_zero: bool = False
    worker_task: str | None = None
    handler: str | None = None
    continuous: bool = False
    start_label: str = '주행 시험 준비'
    description: str = '준비 후 ACC 전면에서 F7 시작 / F8 중지합니다.'

    @property
    def execution(self):
        return 'worker' if self.worker_task else 'native'

    def build(self, root, values, resume, seconds, path):
        from . import command_builders
        return getattr(command_builders, self.builder)(root, self.label, values, resume, seconds, path)

    def load_handler(self):
        if self.handler is None:
            raise ValueError('Task has a dedicated worker adapter: ' + self.id)
        module, name = self.handler.split(':')
        return getattr(import_module(module), name)


SPECS = (
    TaskSpec(
        id='gamepad-teacher',
        label='게임패드 교사 기록',
        group='게임패드 학습·주행',
        fields=frozenset(('config', 'mode', 'profile', 'seconds')),
        builder='teacher',
        default_config='GamepadIO/teacher-hotlap.toml',
        description='주행 모드 칸에서 실제 패드 번호를 선택하세요. 스틱을 움직여 선택 확인 후 F7 시작 / F8 기록 종료. 원시 입력과 ACC 관측을 함께 저장합니다.',
    ),
    TaskSpec(
        id='gamepad-imitation',
        label='게임패드 모방학습',
        group='게임패드 학습·주행',
        fields=frozenset(('config', 'seconds')),
        builder='gamepad_train',
        default_config='TrainingEnvironment/gamepad-train.toml',
        worker_task='gamepad-train',
        handler='TrainingEnvironment.gamepad:train',
        description='선택한 설정의 교사 기록으로 학습합니다. 학습·검증 분리는 설정에 따르며 기존 모델은 별도 보존합니다.',
    ),
    TaskSpec(
        id='gamepad-test',
        label='게임패드 모델 시험',
        group='게임패드 학습·주행',
        fields=frozenset(('config',)),
        builder='gamepad_drive',
        default_config='GamepadIO/trial.toml',
        description='실제 패드를 분리하고 ACC 게임패드 설정을 유지하세요. 설정 파일의 5→10→30초 진단 단계로 실행합니다. F7 시작 / F8 중지.',
    ),
    TaskSpec(
        id='gamepad-intervention',
        label='게임패드 교정 주행',
        group='게임패드 학습·주행',
        fields=frozenset(('config',)),
        builder='gamepad_drive',
        default_config='GamepadIO/intervention.toml',
        description='입력 분리 검증 후 실제 패드를 연결해 사용합니다. 왼쪽 스틱으로 장치 선택 확인 → 버튼을 놓고 F7. 준비 대기 중 A/X: 기어 올림/내림. LB 유지: 조향 교정, RB 한 번: 직접 운전 켜기/끄기, Back/F8: 비상 중지. 정상 종료 제동 중에도 직접 운전 토글이 켜져 있거나 LB를 누르면 조향할 수 있습니다. 완전히 정차·해제될 때까지 세션을 재시작하지 마세요.',
    ),
    TaskSpec(
        id='gamepad-correction',
        label='게임패드 교정 학습',
        group='게임패드 학습·주행',
        fields=frozenset(('config', 'seconds')),
        builder='gamepad_train',
        default_config='TrainingEnvironment/correction-train.toml',
        worker_task='gamepad-train',
        handler='TrainingEnvironment.gamepad:train',
        description='정상 정차한 교정 기록을 기존 교사 데이터와 함께 학습합니다. 조향만 개입한 구간은 조향만 학습하며, 기존 세 번째 교사 기록은 검증용으로 유지합니다. 주행 중 가중치는 바꾸지 않습니다.',
    ),
    TaskSpec(
        id='gamepad-probe',
        label='게임패드 연결 확인',
        group='장치 연결·진단',
        fields=frozenset(('seconds',)),
        builder='probe',
        worker_task='gamepad-probe',
        description='실제 게임패드의 연결 번호와 원시 입력을 조회합니다. 가상 장치를 생성하지 않습니다.',
    ),
    TaskSpec(
        id='physical-response',
        label='실제 패드 응답 검사',
        group='장치 연결·진단',
        fields=frozenset(('config',)),
        builder='response',
        default_config='GamepadIO/response-physical.toml',
        description='실제 패드만 연결하고 엔진 OFF(RPM 0)·정차·중립 N에서 스틱을 움직인 뒤 F7. 안내된 입력을 3초 유지하면 다음 단계로 진행합니다. 브레이크 유지 안내를 따르세요. F8 중지.',
    ),
    TaskSpec(
        id='virtual-response',
        label='가상 패드 응답 검사',
        group='장치 연결·진단',
        fields=frozenset(('config',)),
        builder='response',
        default_config='GamepadIO/response-virtual.toml',
        description='실제 패드를 분리하고 ACC에서 엔진 OFF(RPM 0)·정차·중립 N으로 준비하세요. F7 이후 30초 동안 가상 패드 축을 검사하고 제동·해제합니다. F8 중지.',
    ),
    TaskSpec(
        id='champion-cycle',
        label='최고 모델 학습 반복',
        group='강화학습',
        fields=frozenset(('config', 'seconds')),
        builder='champion_cycle',
        default_config='reviews/champion-cycle-20261108/loop-config.json',
        max_seconds=43200,
        worker_task='gamepad-champion-cycle',
        handler='TrainingEnvironment.champion_cycle:run',
        continuous=True,
        start_label='1시간 학습·선발 반복 시작',
        description='1시간 학습 후 완주 후보와 기존 최고 모델을 각각 5회 검증합니다. 통과한 모델에서 다음 학습을 이어갑니다. 설정 파일의 반복 횟수 또는 종료 시각까지 실행합니다. F8 또는 중지는 전체 반복을 종료합니다.',
    ),
    TaskSpec(
        id='gamepad-online',
        label='게임패드 회차 학습',
        group='강화학습',
        fields=frozenset(('checkpoint', 'config', 'seconds')),
        builder='gamepad_online',
        default_config='TrainingEnvironment/gamepad-online.toml',
        resumable=True,
        worker_task='gamepad-online',
        handler='TrainingEnvironment.gamepad_online:run',
        continuous=True,
        start_label='B에서 새 학습 준비',
        description='자동 설정은 ACC를 전면에 두면 출발 위치 복귀·가상 패드 검사·첫 출발까지 자동 진행합니다. 수동 설정은 스틱 선택 후 F7로 시작합니다. 무효화·정체 후 정차·해제를 확인하고 IQL 갱신·복귀·다음 회차를 이어갑니다. F8은 전체 중지입니다. 계속 학습할 때는 최근 실행 세션을 선택하세요.',
    ),
    TaskSpec(
        id='policy-comparison',
        label='정책 비교 평가',
        group='강화학습',
        fields=frozenset(('config', 'seconds')),
        builder='comparison',
        default_config='TrainingEnvironment/policy-comparison.json',
        worker_task='gamepad-policy-compare',
        handler='TrainingEnvironment.policy_comparison:run',
        continuous=True,
        start_label='고정 정책 비교 시작',
        description='설정에 고정된 모델을 자동 복귀하며 비교합니다. 학습 없이 완주·위빙·랩타임을 평가하고 최대 2시간 뒤 종료합니다. ACC를 전면에 유지하세요. F8은 전체 비교를 중지합니다.',
    ),
    TaskSpec(
        id='gamepad-evaluation',
        label='고정 정책 연속주행 평가',
        group='강화학습',
        fields=frozenset(('checkpoint', 'config', 'model', 'seconds')),
        builder='gamepad_online',
        default_config='TrainingEnvironment/gamepad-online.toml',
        resumable=True,
        worker_task='gamepad-online-evaluate',
        handler='TrainingEnvironment.gamepad_online:run',
        continuous=True,
        start_label='선택 모델 평가 준비',
        description='선택한 고정 모델로 유효 3랩을 3회 평가합니다. 각 회차 수동 준비 후 F7. 학습과 replay 추가는 없으며 정차·장치 해제까지 통과해야 합니다.',
    ),
    TaskSpec(
        id='prepare-v2',
        label='강화학습 자료 준비',
        group='강화학습',
        fields=frozenset(('config', 'seconds')),
        builder='offline',
        default_config='ModelTrainer/sources.toml',
        worker_task='prepare-v2',
        start_label='자료 준비 시작',
        description='기존 교사 자료의 집합 배정을 보존하고 조향 포함 학습 자료를 만듭니다. 설정에는 sources.toml을 선택하세요.',
    ),
    TaskSpec(
        id='pretrain-v2',
        label='강화학습 사전학습',
        group='강화학습',
        fields=frozenset(('checkpoint', 'config', 'dataset', 'epochs', 'seconds')),
        builder='offline',
        default_config='TrainingEnvironment/pretrain.toml',
        resumable=True,
        worker_task='pretrain-v2',
        handler='TrainingEnvironment.pretrain:train',
        start_label='사전학습 시작',
        description='자료 준비 결과 폴더를 선택하세요. 교사 요청값으로 초기 정책을 학습합니다. 실제 게임 주행은 시작하지 않습니다.',
    ),
    TaskSpec(
        id='v2-shadow',
        label='새 정책 비교 주행',
        group='강화학습',
        fields=frozenset(('config', 'controller_config', 'model', 'prepared', 'profile', 'seconds')),
        builder='shadow',
        default_config='ModelAndActuator/config.toml',
        start_label='준비 — F7 대기',
        description='모델에는 사전학습 결과 폴더를 선택하세요. F7 이후 기존 제어기가 목표 10km/h로 최대 30초 주행하며 새 정책의 조향·가감속 판단을 기록합니다. F8 중지. 새 정책의 직접 제어와 학습 갱신은 다음 단계입니다.',
    ),
    TaskSpec(
        id='online',
        label='강화학습',
        group='강화학습',
        fields=frozenset(('checkpoint', 'config', 'controller_config', 'mode', 'model', 'prepared', 'previous_run', 'profile', 'seconds')),
        builder='online',
        default_config='TrainingEnvironment/online.toml',
        resumable=True,
        worker_task='online',
        handler='TrainingEnvironment.online:run',
        start_label='준비 — F7 대기',
        description='verify5 → verify10 → verify30 순서로 직접 제어를 확인한 뒤 train을 선택합니다. 이전 통과 결과에는 직전 sac-session 폴더를 선택하세요. 매 회차 수동 준비 후 F7, F8은 학습까지 중단합니다. 재개 체크포인트는 폴더입니다. evaluate는 현재 정책, evaluate_initial은 초기 정책의 한 랩 평가이며 학습하지 않습니다.',
    ),
    TaskSpec(
        id='train',
        label='모방학습',
        group='이전 vJoy 작업',
        fields=frozenset(('checkpoint', 'config', 'dataset', 'epochs', 'seconds')),
        builder='offline',
        default_config='ModelTrainer/config.toml',
        resumable=True,
        worker_task='train',
        handler='ModelTrainer.training:train',
        start_label='학습 시작',
        description='새 학습은 설정과 입력한 epoch 상한 중 작은 값까지 실행합니다. 재개는 체크포인트의 학습 설정을 사용합니다.',
    ),
    TaskSpec(
        id='record',
        label='기록 수집',
        group='이전 vJoy 작업',
        fields=frozenset(('config', 'seconds')),
        builder='record',
        default_config='DrivingRecorder/recorder.toml',
        start_label='준비 — F7 대기',
        description='준비 후 ACC 전면에서 F7 시작 / F8 중지합니다.',
    ),
    TaskSpec(
        id='evaluate',
        label='오프라인 평가',
        group='이전 vJoy 작업',
        fields=frozenset(('dataset', 'model', 'seconds', 'split')),
        builder='offline',
        worker_task='evaluate',
        handler='Evaluator.evaluate:replay',
        start_label='평가 시작',
        description='학습 결과 폴더와 데이터, 평가 집합을 선택하세요.',
    ),
    TaskSpec(
        id='policy',
        label='평가·주행',
        group='이전 vJoy 작업',
        fields=frozenset(('config', 'controller_config', 'mode', 'model', 'model_seconds', 'prepared', 'profile', 'seconds')),
        builder='policy',
        default_config='ModelAndActuator/config.toml',
        start_label='준비 — F7 대기',
        description='준비 후 ACC 전면에서 F7 시작 / F8 중지합니다.',
    ),
    TaskSpec(
        id='demo',
        label='수동 교사 모델 시험',
        group='이전 vJoy 작업',
        fields=frozenset(('config', 'controller_config', 'prepared', 'profile', 'seconds')),
        builder='demo',
        allow_zero=True,
        description='첫 랩 모방 모델 주행입니다. 현재 설정은 시간 제한 없음(전체 시간 0), 속도 상한·조향 폭·조향 변화율 제한 해제입니다. F7 시작 / F8 중지. 완주·무효 랩·경로 이탈·진행 정체·오류 시 정지합니다. 스로틀은 선택한 모델의 교사 입력 범위에 맞추며 가중치는 변경하지 않습니다.',
    ),
)
BY_LABEL = {spec.label: spec for spec in SPECS}
BY_ID = {spec.id: spec for spec in SPECS}
WORKER_TASKS = {spec.worker_task: spec for spec in SPECS if spec.worker_task}
TASK_GROUPS = {}
for spec in SPECS:
    TASK_GROUPS.setdefault(spec.group, []).append(spec.label)
TASK_GROUPS = {group: tuple(labels) for group, labels in TASK_GROUPS.items()}
TASKS = tuple(BY_LABEL)
TASK_FIELDS = {spec.label: set(spec.fields) for spec in SPECS}
DEFAULT_CONFIGS = {spec.label: spec.default_config for spec in SPECS if spec.default_config}
