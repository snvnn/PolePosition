"""Run from the repository: python -m TrainingController."""
import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from .coordinator import Coordinator
from .jobs import TASKS, command
from .results import recent_results, result_label, resume_checkpoint

ROOT = Path(__file__).resolve().parents[1]
PREPARATION_LABELS = {'config': '작업 설정', 'path': '경로 파일', 'model': '모델·기존 단계',
                      'driver': 'vJoy 장치 조회', 'calibration': '보정 파일·설정', 'hotkeys': 'F7/F8 등록'}
PREPARATION_STATES = {'checking': '확인 중', 'verified': '통과', 'failed': '실패',
                      'cancelled': '중단', 'unconfirmed': '결과 미확인'}
TASK_FIELDS = {
    '게임패드 연결 확인': {'seconds'},
    '실제 패드 응답 검사': {'config'},
    '가상 패드 응답 검사': {'config'},
    '게임패드 교사 기록': {'config','profile','mode','seconds'},
    '게임패드 모방학습': {'config','seconds'},
    '게임패드 모델 시험': {'config'},
    '게임패드 교정 주행': {'config'},
    '게임패드 교정 학습': {'config','seconds'},
    '수동 교사 모델 시험': {'prepared', 'profile', 'controller_config', 'config', 'seconds'},
    '모방학습': {'dataset', 'checkpoint', 'config', 'seconds', 'epochs'},
    '기록 수집': {'config', 'seconds'},
    '오프라인 평가': {'dataset', 'model', 'split', 'seconds'},
    '평가·주행': {'model', 'prepared', 'profile', 'controller_config', 'config', 'mode', 'model_seconds', 'seconds'},
    '강화학습': {'model', 'checkpoint', 'prepared', 'profile', 'controller_config', 'config', 'mode', 'seconds', 'previous_run'},
    '새 정책 비교 주행': {'model', 'prepared', 'profile', 'controller_config', 'config', 'seconds'},
    '강화학습 자료 준비': {'config', 'seconds'},
    '강화학습 사전학습': {'dataset', 'checkpoint', 'config', 'seconds', 'epochs'},
}
DEFAULT_CONFIGS = {'실제 패드 응답 검사':'GamepadIO/response-physical.toml','가상 패드 응답 검사':'GamepadIO/response-virtual.toml','강화학습': 'TrainingEnvironment/online.toml', '모방학습': 'ModelTrainer/config.toml', '기록 수집': 'DrivingRecorder/recorder.toml',
                   '게임패드 교사 기록': 'GamepadIO/teacher-hotlap.toml',
                   '게임패드 모방학습': 'TrainingEnvironment/gamepad-train.toml',
                   '게임패드 모델 시험': 'GamepadIO/trial.toml',
                   '게임패드 교정 주행': 'GamepadIO/intervention.toml',
                   '게임패드 교정 학습': 'TrainingEnvironment/correction-train.toml',
                   '평가·주행': 'ModelAndActuator/config.toml',
                   '강화학습 자료 준비': 'ModelTrainer/sources.toml',
                   '강화학습 사전학습': 'TrainingEnvironment/pretrain.toml',
                   '새 정책 비교 주행': 'ModelAndActuator/config.toml'}


class Controller:
    def __init__(self, root, monitoring=True):
        self.root = root
        self.coordinator = Coordinator(ROOT, monitoring=monitoring)
        self.job = self.coordinator.job
        self.closing = False
        self.results = []
        self.preparation_checks = {}
        root.title('폴포지션 학습 컨트롤러')
        root.geometry('900x840')
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(1, weight=1)
        self.status = tk.StringVar(value='대기')
        ttk.Label(frame, textvariable=self.status).grid(row=0, column=0, sticky='w', pady=8)
        self.connection = tk.StringVar(value='ACC 연결 확인 중' if monitoring else '')
        ttk.Label(frame, textvariable=self.connection, wraplength=620).grid(row=0, column=1, columnspan=2, sticky='w')
        self.inputs = []
        self.field_widgets = {}
        self.locked = False
        self.selected_task = TASKS[0]
        self.task_configs = {task: str(ROOT / path) for task, path in DEFAULT_CONFIGS.items()}
        self.task_profiles = {'게임패드 교사 기록': str(ROOT/'input_profiles/gamepad/profile.json')}
        self.task = tk.StringVar(value=TASKS[0])
        ttk.Label(frame, text='작업 선택').grid(row=1, column=0, sticky='w')
        task = ttk.Combobox(frame, textvariable=self.task, values=TASKS, state='readonly')
        task.grid(row=1, column=1, sticky='ew')
        task.bind('<<ComboboxSelected>>', lambda _: self.select_task())
        self.inputs.append(task)
        self.field_widgets['task'] = [task]
        self.dataset = self.path_row(frame, 2, '검증된 학습 데이터', 'dataset', directory=True)
        self.checkpoint = self.path_row(frame, 3, '이어갈 체크포인트', 'checkpoint', directory=False)
        self.config = self.path_row(frame, 4, '작업 설정 TOML', 'config', directory=False)
        self.config.set(str(ROOT / 'ModelTrainer/config.toml'))
        self.seconds = tk.StringVar(value='7200')
        self.model = self.path_row(frame, 5, '모델 / 평가할 학습 결과', 'model', directory=True)
        self.prepared = self.path_row(frame, 6, '검증된 경로 상태 폴더', 'prepared', directory=True)
        self.profile = self.path_row(frame, 7, '입력 보정 프로필', 'profile', directory=False)
        self.previous_run = self.path_row(frame, 8, '이전 통과 주행 결과', 'previous_run', directory=True)
        self.controller_config = self.path_row(frame, 9, '주행 제어 설정 TOML', 'controller_config', directory=False)
        self.controller_config.set(str(ROOT / 'BaselineController/config.toml'))
        ttk.Label(frame, text='전체 시간 제한 (초)').grid(row=10, column=0, sticky='w')
        limits = ttk.Frame(frame)
        limits.grid(row=10, column=1, columnspan=2, sticky='ew', pady=5)
        entry = ttk.Entry(limits, textvariable=self.seconds, width=12)
        entry.pack(side='left')
        self.inputs.append(entry)
        self.field_widgets['seconds'] = [entry]
        self.epochs = tk.StringVar()
        ttk.Label(limits, text='새 학습 상한(epoch, 빈칸=설정)').pack(side='left', padx=12)
        epoch_entry = ttk.Entry(limits, textvariable=self.epochs, width=8)
        epoch_entry.pack(side='left')
        self.inputs.append(epoch_entry)
        self.field_widgets['epochs'] = [epoch_entry]
        options = ttk.Frame(frame)
        options.grid(row=11, column=0, columnspan=3, sticky='w')
        self.mode = tk.StringVar(value='shadow')
        self.model_seconds = tk.StringVar(value='5')
        self.split = tk.StringVar(value='test')
        for name, label, variable, choices in [('mode', '주행 모드', self.mode, ('shadow', 'drive')),
                                         ('model_seconds', '기존 모델 단계(초)', self.model_seconds, ('5', '10', '15')),
                                         ('split', '평가 집합', self.split, ('validation', 'test'))]:
            ttk.Label(options, text=label).pack(side='left', padx=3)
            widget = ttk.Combobox(options, textvariable=variable, values=choices, width=10, state='readonly')
            widget.pack(side='left', padx=3)
            self.inputs.append(widget)
            self.field_widgets[name] = [widget]
        self.mode.trace_add('write', lambda *_: self.update_controls())
        buttons = ttk.Frame(frame)
        buttons.grid(row=12, column=0, columnspan=3, sticky='w', pady=12)
        for label, resume in [('준비 / 학습·평가 시작', False), ('이어서 학습', True)]:
            button = ttk.Button(buttons, text=label, command=lambda r=resume: self.start(r))
            button.pack(side='left', padx=4)
            self.inputs.append(button)
            self.field_widgets['resume' if resume else 'start'] = [button]
        self.stop_button = ttk.Button(buttons, text='중지', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=4)
        self.detail = tk.StringVar()
        ttk.Label(frame, textvariable=self.detail, wraplength=820).grid(row=13, column=0, columnspan=3, sticky='w')
        ttk.Label(frame, text='최근 결과').grid(row=14, column=0, sticky='w', pady=8)
        self.listbox = tk.Listbox(frame, height=7)
        self.listbox.grid(row=15, column=0, columnspan=3, sticky='nsew')
        self.listbox.bind('<<ListboxSelect>>', lambda _: self.update_result_actions())
        frame.rowconfigure(15, weight=1)
        footer = ttk.Frame(frame)
        footer.grid(row=16, column=0, columnspan=3, sticky='w', pady=8)
        self.folder_button = ttk.Button(footer, text='결과 폴더 열기', command=lambda: self.open_result(False))
        self.report_button = ttk.Button(footer, text='보고서 열기', command=lambda: self.open_result(True))
        self.checkpoint_button = ttk.Button(footer, text='체크포인트 선택', command=self.use_checkpoint)
        self.refresh_button = ttk.Button(footer, text='연결 다시 확인', command=self.coordinator.refresh)
        for button in (self.folder_button, self.report_button, self.checkpoint_button, self.refresh_button):
            button.pack(side='left')
        self.preparation = tk.StringVar()
        ttk.Label(frame, textvariable=self.preparation, wraplength=820).grid(row=17, column=0, columnspan=3, sticky='w')
        self.refresh_results()
        self.select_task()
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.poll_id = root.after(200, self.poll)
        root.bind('<Destroy>', self.on_destroy, add='+')

    def path_row(self, frame, row, label, name, directory):
        variable = tk.StringVar()
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', padx=(0, 12))
        entry = ttk.Entry(frame, textvariable=variable)
        entry.grid(row=row, column=1, sticky='ew', pady=5)
        def choose():
            path = (filedialog.askdirectory if directory or (name == 'checkpoint' and self.task.get() == '강화학습') else filedialog.askopenfilename)(parent=self.root, initialdir=ROOT)
            if path:
                variable.set(path)
        button = ttk.Button(frame, text='선택', command=choose)
        button.grid(row=row, column=2, padx=5)
        self.inputs.extend([entry, button])
        self.field_widgets[name] = [entry, button]
        return variable

    def lock(self, locked):
        self.locked = locked
        self.update_controls()
        self.stop_button.configure(state='normal' if locked else 'disabled')

    def update_controls(self):
        active = TASK_FIELDS[self.task.get()] | {'task'}
        active.add('start')
        if self.task.get() in ('모방학습', '강화학습 사전학습', '강화학습'):
            active.add('resume')
        if self.task.get() == '평가·주행' and self.mode.get() == 'drive':
            active.add('previous_run')
        for name, widgets in self.field_widgets.items():
            for widget in widgets:
                enabled = not self.locked and name in active
                widget.configure(state=('readonly' if isinstance(widget, ttk.Combobox) else 'normal') if enabled else 'disabled')
        label = {'모방학습': '학습 시작', '기록 수집': '준비 — F7 대기',
                 '오프라인 평가': '평가 시작', '평가·주행': '준비 — F7 대기', '강화학습': '준비 — F7 대기',
                 '강화학습 자료 준비': '자료 준비 시작', '강화학습 사전학습': '사전학습 시작',
                 '새 정책 비교 주행': '준비 — F7 대기'}
        self.field_widgets['start'][0].configure(text=label.get(self.task.get(), '주행 시험 준비'))
        self.update_result_actions()

    def select_task(self):
        self.preparation_checks.clear()
        self.preparation.set('')
        self.task_configs[self.selected_task] = self.config.get()
        self.task_profiles[self.selected_task] = self.profile.get()
        self.selected_task = self.task.get()
        choices = ('verify5', 'verify10', 'verify30', 'train', 'evaluate', 'evaluate_initial') if self.selected_task == '강화학습' else ('shadow', 'drive')
        if self.selected_task == '게임패드 교사 기록': choices=('0','1','2','3')
        self.field_widgets['mode'][0].configure(values=choices)
        if self.mode.get() not in choices:
            self.mode.set(choices[0])
        self.config.set(self.task_configs.get(self.selected_task, ''))
        self.profile.set(self.task_profiles.get(self.selected_task, ''))
        descriptions = {'모방학습': '새 학습은 설정과 입력한 epoch 상한 중 작은 값까지 실행합니다. 재개는 체크포인트의 학습 설정을 사용합니다.',
                        '게임패드 연결 확인': '실제 게임패드의 연결 번호와 원시 입력을 조회합니다. 가상 장치를 생성하지 않습니다.',
                        '실제 패드 응답 검사': '실제 패드만 연결하고 엔진 OFF(RPM 0)·정차·중립 N에서 스틱을 움직인 뒤 F7. 안내된 입력을 3초 유지하면 다음 단계로 진행합니다. 브레이크 유지 안내를 따르세요. F8 중지.',
                        '가상 패드 응답 검사': '실제 패드를 분리하고 ACC에서 엔진 OFF(RPM 0)·정차·중립 N으로 준비하세요. F7 이후 30초 동안 가상 패드 축을 검사하고 제동·해제합니다. F8 중지.',
                        '게임패드 교사 기록': '주행 모드 칸에서 실제 패드 번호를 선택하세요. 스틱을 움직여 선택 확인 후 F7 시작 / F8 기록 종료. 원시 입력과 ACC 관측을 함께 저장합니다.',
                        '게임패드 모방학습': '설정 파일의 새 시연 3회 중 앞 2회로 학습하고 마지막 1회로 검증합니다. 기존 휠 모델은 사용하지 않습니다.',
                        '게임패드 모델 시험': '실제 패드를 분리하고 ACC 게임패드 설정을 유지하세요. 설정 파일의 5→10→30초 진단 단계로 실행합니다. F7 시작 / F8 중지.',
                        '게임패드 교정 주행': '입력 분리 검증 후 실제 패드를 연결해 사용합니다. 왼쪽 스틱으로 장치 선택 확인 → 버튼을 놓고 F7. LB 유지: 조향 교정, RB 유지: 전체 인수, Back/F8: 비상 중지. 정상 종료 제동 중에도 LB/RB로 조향할 수 있습니다. 완전히 정차·해제될 때까지 세션을 재시작하지 마세요.',
                        '게임패드 교정 학습': '정상 정차한 교정 기록을 기존 교사 데이터와 함께 학습합니다. 조향만 개입한 구간은 조향만 학습하며, 기존 세 번째 교사 기록은 검증용으로 유지합니다. 주행 중 가중치는 바꾸지 않습니다.',
                        '수동 교사 모델 시험': '첫 랩 모방 모델 주행입니다. 현재 설정은 시간 제한 없음(전체 시간 0), 속도 상한·조향 폭·조향 변화율 제한 해제입니다. F7 시작 / F8 중지. 완주·무효 랩·경로 이탈·진행 정체·오류 시 정지합니다. 스로틀은 선택한 모델의 교사 입력 범위에 맞추며 가중치는 변경하지 않습니다.',
                        '오프라인 평가': '학습 결과 폴더와 데이터, 평가 집합을 선택하세요.',
                        '강화학습': 'verify5 → verify10 → verify30 순서로 직접 제어를 확인한 뒤 train을 선택합니다. 이전 통과 결과에는 직전 sac-session 폴더를 선택하세요. 매 회차 수동 준비 후 F7, F8은 학습까지 중단합니다. 재개 체크포인트는 폴더입니다. evaluate는 현재 정책, evaluate_initial은 초기 정책의 한 랩 평가이며 학습하지 않습니다.',
                        '강화학습 자료 준비': '기존 교사 자료의 집합 배정을 보존하고 조향 포함 학습 자료를 만듭니다. 설정에는 sources.toml을 선택하세요.',
                        '강화학습 사전학습': '자료 준비 결과 폴더를 선택하세요. 교사 요청값으로 초기 정책을 학습합니다. 실제 게임 주행은 시작하지 않습니다.',
                        '새 정책 비교 주행': '모델에는 사전학습 결과 폴더를 선택하세요. F7 이후 기존 제어기가 목표 10km/h로 최대 30초 주행하며 새 정책의 조향·가감속 판단을 기록합니다. F8 중지. 새 정책의 직접 제어와 학습 갱신은 다음 단계입니다.'}
        self.detail.set(descriptions.get(self.selected_task, '준비 후 ACC 전면에서 F7 시작 / F8 중지합니다.'))
        self.update_controls()

    def start(self, resume):
        try:
            fields = ('seconds', 'epochs', 'dataset', 'checkpoint', 'config', 'model', 'prepared', 'profile',
                      'previous_run', 'controller_config', 'mode', 'model_seconds', 'split')
            values = {name: getattr(self, name).get() for name in fields}
            self.coordinator.start(command(ROOT, self.task.get(), values, resume))
            self.preparation_checks.clear()
            self.preparation.set('')
            self.lock(True)
            self.status.set('준비 중')
        except (ValueError, OSError, RuntimeError) as error:
            self.status.set('시작 실패')
            self.detail.set(str(error))

    def stop(self):
        pending = self.coordinator.pending is not None
        self.coordinator.stop()
        self.status.set('준비 취소' if pending else '중지 처리 중 — 정상 종료 대기')
        if pending:
            self.lock(False)

    def add_result(self, path):
        self.refresh_results([path])

    def refresh_results(self, additional=()):
        selected = self.selected_result()
        self.results = recent_results(ROOT, [*self.results, *additional])
        self.listbox.delete(0, 'end')
        for path in self.results:
            self.listbox.insert('end', result_label(path))
        if selected in self.results:
            self.listbox.selection_set(self.results.index(selected))
        self.update_result_actions()

    def selected_result(self):
        selection = self.listbox.curselection()
        return self.results[selection[0]] if selection and selection[0] < len(self.results) else None

    def update_result_actions(self):
        selected = self.selected_result()
        idle = not self.locked and not self.closing
        for button, ready in ((self.folder_button, selected is not None and selected.is_dir()),
                              (self.report_button, selected is not None and (selected/'report.html').is_file()),
                              (self.checkpoint_button, selected is not None and resume_checkpoint(selected) is not None),
                              (self.refresh_button, True)):
            button.configure(state='normal' if idle and ready else 'disabled')

    def use_checkpoint(self):
        if self.locked or self.closing:
            return
        selected = self.selected_result()
        checkpoint = resume_checkpoint(selected) if selected else None
        if checkpoint is None:
            self.detail.set('선택한 결과에 이어갈 체크포인트가 없습니다.')
            self.update_result_actions()
            return
        from ModelTrainer.common import read
        try:
            kind = read(selected/'manifest.json').get('kind')
        except (OSError, ValueError):
            kind = None
        self.task.set('강화학습' if kind == 'sac_checkpoint' else '강화학습 사전학습' if kind == 'sac_pretraining' else '모방학습')
        self.select_task()
        if kind == 'sac_checkpoint': self.mode.set('train')
        self.checkpoint.set(str(checkpoint))
        self.detail.set('체크포인트를 선택했습니다. 이어서 학습을 누르면 기존 검증 후 새 실행 폴더에서 재개합니다.')

    def open_result(self, report):
        if self.locked or self.closing:
            return
        selected = self.listbox.curselection()
        if selected:
            path = self.results[selected[0]]
            if report:
                path = path / 'report.html'
            try:
                os.startfile(path)
            except OSError as error:
                self.detail.set(str(error))

    def poll(self):
        self.poll_id = None
        for source, event in self.coordinator.poll():
            if source == 'monitor':
                if event['event'] == 'finished':
                    self.connection.set('연결 진단 중지' if self.coordinator.busy else '연결 진단 종료: '+str(event.get('reason') or ''))
                else:
                    self.show_connection(event)
                continue
            if event['event'] == 'progress':
                if 'epoch' in event:
                    self.detail.set(f"epoch {event['epoch']} · 갱신 {event['updates']} · 손실 {event['loss']:.7f} · 경과 {event.get('elapsed_seconds', 0):.1f}초")
                else:
                    self.show_state(event)
            elif event['event'] == 'finished':
                self.finish_preparation(event['state'])
                self.status.set({'completed': '완료', 'stopped': '중단', 'failed': '실패', 'needs_attention': '사용자 확인 필요'}[event['state']])
                self.detail.set(event.get('reason') or '작업 종료')
                if event.get('result_dir'):
                    self.add_result(event['result_dir'])
                    if self.task.get() == '강화학습 자료 준비' and event['state'] == 'completed':
                        self.dataset.set(event['result_dir'])
                    if self.task.get() == '강화학습' and event['state'] == 'completed':
                        previous_stage = self.mode.get()
                        following = {'verify5':'verify10','verify10':'verify30','verify30':'train'}.get(previous_stage)
                        if following:
                            self.previous_run.set(event['result_dir'])
                            self.mode.set(following)
                            self.detail.set('검증 통과. 다음 단계 ' + following + ' 설정을 채웠습니다. 수동 준비 후 준비 버튼과 F7으로 시작하세요.')
                        elif previous_stage == 'train':
                            from ModelTrainer.common import read
                            checkpoint = read(Path(event['result_dir'])/'manifest.json').get('checkpoint')
                            if checkpoint:
                                self.checkpoint.set(checkpoint)
                                self.detail.set(('학습·저장 완료. ' if event.get('updates',0) else '경험 저장 완료 · 갱신 보류. ') + '체크포인트를 채웠습니다. 수동 준비 후 이어서 학습과 F7으로 다음 회차를 시작하세요.')
                self.lock(False)
            else:
                self.show_state(event)
        if self.coordinator.closed:
            self.root.destroy()
            return
        self.poll_id = self.root.after(200, self.poll)

    def on_destroy(self, event):
        if event.widget is self.root and self.poll_id is not None:
            self.root.after_cancel(self.poll_id)
            self.poll_id = None

    def show_state(self, event):
        if 'preparation' in event:
            check = event['preparation']
            if isinstance(check, dict) and check.get('check') in PREPARATION_LABELS and check.get('state') in PREPARATION_STATES:
                self.preparation_checks[check['check']] = check['state']
                self.show_preparation()
        if 'connection' in event:
            self.show_connection(event)
        phases = {'preparing': '입력·장치 검사 중', 'training': '학습 중', 'evaluating': '평가 중', 'waiting_f7': 'F7 대기',
                  'policy_shadow': '기존 제어기 주행 · 새 정책 판단 기록 중',
                  'recording': '기록 수집 중', 'device_response':'패드 입력 응답 검사', 'pid_start': 'PID 출발', 'model_control': '모델 제어',
                  'lap_evaluated': '한 랩 평가 기록 완료', 'manual_reset': '수동 출발 준비', 'resetting': '수동 출발 준비', 'validating': '경험 검증', 'learning': '정차 후 학습', 'saving': '정책 저장', 'saved': '저장 완료', 'stopping': '정지 처리', 'finished': '종료 처리'}
        phase = event.get('phase', event.get('state', ''))
        self.status.set(phases.get(phase, phase))
        controller_reason = event.get('controller_reason')
        if phase == 'pid_start' and controller_reason == 'waiting_forward_gear':
            self.status.set('전진 기어 대기 — ACC에서 1단 확인')
        identity = event.get('identity') or {}
        parts = [identity.get('car', ''), identity.get('track', '')]
        for key, label in [('connection', 'ACC'), ('fresh', '갱신'), ('tracking_ready', '경로'),
                           ('foreground', 'ACC 전면'), ('controller_reason', '제어 상태'),
                           ('vjoy', 'vJoy'), ('calibration', '보정'), ('speed_kmh', 'km/h'),
                           ('gamepad_slot','게임패드 번호'),('teacher_slot','실제 패드 번호'),('authority','제어권'),('correction_samples','교정 표본'),('isolation_ok','입력 분리'),('stick_selected','스틱 선택 확인'),('model_type','모델 종류'),('profile_match','설정 일치'),
                           ('episode', '회차'), ('updates', '갱신'), ('reward', '보상'), ('samples', '샘플'), ('total_samples', '전체 샘플'), ('elapsed_seconds', '경과 초'), ('reason', '상태')]:
            if key in event:
                value = event[key]
                parts.append(f'{label}: {value:.1f}' if isinstance(value, float) else f'{label}: {value}')
        self.detail.set(' · '.join(part for part in parts if part))

    def show_preparation(self):
        self.preparation.set('준비 검사 기록: ' + ' · '.join(
            f'{PREPARATION_LABELS[key]} {PREPARATION_STATES[value]}'
            for key, value in self.preparation_checks.items()))

    def finish_preparation(self, state):
        for key, value in self.preparation_checks.items():
            if value == 'checking':
                self.preparation_checks[key] = 'unconfirmed' if state == 'failed' else 'cancelled'
        if self.preparation_checks:
            self.show_preparation()

    def show_connection(self, event):
        identity = event.get('identity') or {}
        parts = ['ACC: '+str(event.get('connection','확인 중')), identity.get('car',''), identity.get('track','')]
        if 'fresh' in event:
            parts.append('공유 메모리 갱신' if event['fresh'] else '새 관측 대기')
        if event.get('age_ms') is not None:
            parts.append(f"관측 경과 {event['age_ms']:.0f}ms")
        self.connection.set(' · '.join(part for part in parts if part))

    def close(self):
        self.closing = True
        self.coordinator.close()
        if self.coordinator.closed:
            self.root.destroy()
        else:
            self.status.set('종료 처리 중')


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument('--preset', type=Path)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--resume', action='store_true', help='Prepare from the preset checkpoint')
    args = parser.parse_args()
    if args.resume and not (args.preset and args.prepare):
        parser.error('--resume requires --preset and --prepare')
    from .single_instance import SingleInstance
    try: instance=SingleInstance(ROOT)
    except RuntimeError as e:
        from tkinter import messagebox
        messagebox.showinfo('학습 컨트롤러',str(e));return
    root = tk.Tk()
    app = Controller(root)
    if args.preset:
        preset = json.loads(args.preset.read_text(encoding='utf-8-sig'))
        if preset.get('task') not in TASKS:
            raise ValueError('Unknown preset task')
        app.task.set(preset['task']); app.select_task()
        for name in TASK_FIELDS[preset['task']]:
            if name in preset: getattr(app, name).set(str(preset[name]))
        if args.prepare: root.after(100, lambda: app.start(args.resume))
    elif args.prepare:
        parser.error('--prepare requires a preset')
    try: root.mainloop()
    finally: instance.close()


if __name__ == '__main__':
    main()
