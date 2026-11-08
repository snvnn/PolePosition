from pathlib import Path
import json
import tkinter as tk
from tkinter import ttk
from .task_catalog import TASKS, TASK_GROUPS, DEFAULT_CONFIGS

ROOT = Path(__file__).resolve().parents[1]
from .coordinator import Coordinator
from .task_view import TaskView, apply_preset
from .result_view import ResultView

PREPARATION_LABELS = {'config': '작업 설정', 'path': '경로 파일', 'model': '모델·기존 단계', 'driver': 'vJoy 장치 조회', 'calibration': '보정 파일·설정', 'hotkeys': 'F7/F8 등록'}
PREPARATION_STATES = {'checking': '확인 중', 'verified': '통과', 'failed': '실패', 'cancelled': '중단', 'unconfirmed': '결과 미확인'}


class Controller(TaskView, ResultView):
    def __init__(self, root, monitoring=True, *, workspace=None):
        self.root = root
        self.workspace = Path(workspace) if workspace is not None else ROOT
        self.coordinator = Coordinator(self.workspace, monitoring=monitoring)
        self.job = self.coordinator.job
        self.closing = False
        self.results = []
        self.preparation_checks = {}
        root.title('폴포지션 학습 컨트롤러')
        from . import theme
        self.theme = theme.apply(root)
        root.geometry('1040x860')
        root.minsize(860,680)
        header=ttk.Frame(root,style='Shell.TFrame',padding=(22,16,22,12))
        header.pack(fill='x')
        ttk.Label(header,text='POLE POSITION',style='Title.TLabel').pack(anchor='w')
        ttk.Label(header,text='DRIVING LAB   /   수집 · 학습 · 주행 검증',style='Subtitle.TLabel').pack(anchor='w')
        ttk.Label(header,text='ACC에서 F7 시작  ·  F8 중지',style='Subtitle.TLabel').pack(anchor='e')
        self.path_widgets = {}
        self.option_groups = {}

        frame = ttk.Frame(root, padding=16)
        frame.pack(fill='both', expand=True, padx=18, pady=(0,18))
        frame.columnconfigure(1, weight=1)
        self.status = tk.StringVar(value='대기')
        ttk.Label(frame, textvariable=self.status,style='Status.TLabel').grid(row=0, column=0, sticky='w', pady=8)
        self.connection = tk.StringVar(value='ACC 연결 확인 중' if monitoring else '')
        ttk.Label(frame, textvariable=self.connection, wraplength=620).grid(row=0, column=1, columnspan=2, sticky='w')
        self.inputs = []
        self.field_widgets = {}
        self.locked = False
        self.selected_task = TASKS[0]
        self.task_configs = {task: str(self.workspace / path) for task, path in DEFAULT_CONFIGS.items()}
        self.task_profiles = {'게임패드 교사 기록': str(self.workspace/'input_profiles/gamepad/profile.json')}
        self.task = tk.StringVar(value=TASKS[0])
        ttk.Label(frame, text='01  작업 선택',style='Section.TLabel').grid(row=1, column=0, sticky='w')
        task_row = ttk.Frame(frame)
        task_row.grid(row=1, column=1, columnspan=2, sticky='ew')
        task_row.columnconfigure(1, weight=1)
        self.task_group = tk.StringVar(value=next(iter(TASK_GROUPS)))
        group = ttk.Combobox(task_row, textvariable=self.task_group, values=tuple(TASK_GROUPS),
                             state='readonly', width=20)
        group.grid(row=0, column=0, padx=(0, 8))
        group.bind('<<ComboboxSelected>>', lambda _: self.select_task_group())
        self.inputs.append(group)
        self.field_widgets['task_group'] = [group]
        task = ttk.Combobox(task_row, textvariable=self.task,
                            values=TASK_GROUPS[self.task_group.get()], state='readonly')
        task.grid(row=0, column=1, sticky='ew')
        task.bind('<<ComboboxSelected>>', lambda _: self.select_task())
        self.inputs.append(task)
        self.field_widgets['task'] = [task]
        self.dataset = self.path_row(frame, 2, '검증된 학습 데이터', 'dataset', directory=True)
        self.checkpoint = self.path_row(frame, 3, '이어갈 체크포인트', 'checkpoint', directory=False)
        self.config = self.path_row(frame, 4, '작업 설정 파일', 'config', directory=False)
        self.config.set(str(self.workspace / 'ModelTrainer/config.toml'))
        self.seconds = tk.StringVar(value='7200')
        self.model = self.path_row(frame, 5, '모델 / 평가할 학습 결과', 'model', directory=True)
        self.prepared = self.path_row(frame, 6, '검증된 경로 상태 폴더', 'prepared', directory=True)
        self.profile = self.path_row(frame, 7, '입력 보정 프로필', 'profile', directory=False)
        self.previous_run = self.path_row(frame, 8, '이전 통과 주행 결과', 'previous_run', directory=True)
        self.controller_config = self.path_row(frame, 9, '주행 제어 설정 TOML', 'controller_config', directory=False)
        self.controller_config.set(str(self.workspace / 'BaselineController/config.toml'))
        self.limits_label=ttk.Label(frame, text='전체 시간 제한 (초)')
        self.limits_label.grid(row=10, column=0, sticky='w')
        limits = ttk.Frame(frame)
        self.limits_frame=limits
        limits.grid(row=10, column=1, columnspan=2, sticky='ew', pady=5)
        entry = ttk.Entry(limits, textvariable=self.seconds, width=12)
        entry.pack(side='left')
        self.inputs.append(entry)
        self.field_widgets['seconds'] = [entry]
        self.epochs = tk.StringVar()
        self.epoch_label=ttk.Label(limits, text='학습 횟수 상한 (빈칸=설정)')
        self.epoch_label.pack(side='left', padx=12)
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
            option_label=ttk.Label(options, text=label)
            option_label.pack(side='left', padx=3)
            widget = ttk.Combobox(options, textvariable=variable, values=choices, width=10, state='readonly')
            widget.pack(side='left', padx=3)
            self.inputs.append(widget)
            self.field_widgets[name] = [widget]
            self.option_groups[name]=[option_label,widget]
        self.mode.trace_add('write', lambda *_: self.update_controls())
        buttons = ttk.Frame(frame)
        buttons.grid(row=12, column=0, columnspan=3, sticky='w', pady=12)
        for label, resume in [('준비 / 학습·평가 시작', False), ('이어서 학습', True)]:
            button = ttk.Button(buttons, text=label, style='TButton' if resume else 'Primary.TButton', command=lambda r=resume: self.start(r))
            button.pack(side='left', padx=4)
            self.inputs.append(button)
            self.field_widgets['resume' if resume else 'start'] = [button]
        self.stop_button = ttk.Button(buttons, text='중지', style='Danger.TButton', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=4)
        self.configuration_summary=tk.StringVar()
        self.config_preview=ttk.Label(frame,textvariable=self.configuration_summary,style='Muted.TLabel',wraplength=940)
        self.config_preview.grid(row=18,column=0,columnspan=3,sticky='ew',pady=(8,0))
        self.config.trace_add('write',lambda *_: self.update_config_preview())
        self.detail = tk.StringVar()
        ttk.Label(frame, textvariable=self.detail, wraplength=820).grid(row=13, column=0, columnspan=3, sticky='w')
        ttk.Label(frame, text='02  최근 결과',style='Section.TLabel').grid(row=14, column=0, sticky='w', pady=8)
        self.listbox = tk.Listbox(frame, height=6,background=theme.BG,foreground=theme.TEXT,selectbackground='#285267',selectforeground='#ffffff',highlightthickness=1,highlightbackground='#35465c',borderwidth=0,activestyle='none',exportselection=False,font=('Malgun Gothic',10))
        self.listbox.grid(row=15, column=0, columnspan=3, sticky='nsew')
        self.listbox.bind('<<ListboxSelect>>', lambda _: self.update_result_actions())
        frame.rowconfigure(15, weight=1)
        footer = ttk.Frame(frame)
        footer.grid(row=16, column=0, columnspan=3, sticky='w', pady=8)
        self.folder_button = ttk.Button(footer, text='결과 폴더 열기', command=lambda: self.open_result(False))
        self.report_button = ttk.Button(footer, text='보고서 열기', command=lambda: self.open_result(True))
        self.checkpoint_button = ttk.Button(footer, text='체크포인트 선택', command=self.use_checkpoint)
        self.delete_button = ttk.Button(footer, text='선택 로그 삭제', command=self.delete_selected_result)
        self.refresh_button = ttk.Button(footer, text='연결 다시 확인', command=self.coordinator.refresh)
        for button in (self.folder_button, self.report_button, self.checkpoint_button, self.delete_button, self.refresh_button):
            button.pack(side='left')
        self.preparation = tk.StringVar()
        ttk.Label(frame, textvariable=self.preparation, wraplength=820).grid(row=17, column=0, columnspan=3, sticky='w')
        self.refresh_results()
        self.config.set(self.task_configs.get(self.selected_task, ''))
        self.profile.set(self.task_profiles.get(self.selected_task, ''))
        self.select_task()
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.poll_id = root.after(200, self.poll)
        root.bind('<Destroy>', self.on_destroy, add='+')

    def poll(self):
        self.poll_id = None
        for source, event in self.coordinator.poll():
            self.job = self.coordinator.job
            if source == 'background':
                self.update_result_actions()
                if event.get('result_dir'):
                    self.add_result(event['result_dir'])
                if event.get('state') == 'failed' and not self.coordinator.busy:
                    self.detail.set('백그라운드 결과 처리 실패: '+str(event.get('reason') or '결과 폴더 확인 필요'))
                continue
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
                    if self.task.get()=='게임패드 회차 학습' and (Path(event['result_dir'])/'latest.json').is_file():
                        self.checkpoint.set(event['result_dir'])
                        if event['state']=='completed':
                            self.detail.set('마지막 검증 완료 정책을 선택했습니다. 수동 준비 후 이어서 학습으로 재개할 수 있습니다. 실주행 안정성은 별도 평가가 필요합니다.')
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
                  'critic_warmup':'B 행동 유지 · 연속주행 가치 보정', 'supervisor_ready':'F7/F8 관리 준비',
                  'next_f7':'회차 저장 완료',
                  'comparison_reset':'정책 비교 · 자동 출발 준비',
                  'comparison_focus':'정책 비교 준비 · ACC 창으로 전환해 주세요',
                  'comparison_saved':'정책 비교 · 시도 기록 완료',
                  'relocation_recovery':'미확인 재배치 기록 제외 · 차고 확인 후 복귀',
                  'terminal_reset_recovery':'종료 관측 초기화 회차 제외 · 같은 모델로 복귀',
                  'timing_gap_recovery':'짧은 관측 지연 회차 제외 · 정차 확인 후 복귀',
                  'auto_reset':'자동 복귀 · ACC 재시작 중',
                  'auto_reset_ready':'자동 복귀 확인 완료',
                  'auto_start':'다음 정책 자동 출발 준비',
                  'relocated_stopping':'재배치 후 새 정차 확인',
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
                           ('gamepad_slot','게임패드 번호'),('teacher_slot','실제 패드 번호'),('authority','제어권'),('correction_samples','교정 표본'),('isolation_ok','입력 분리'),('stick_selected','패드 선택 확인'),('model_type','모델 종류'),('profile_match','설정 일치'),
                           ('valid_laps','유효 연속 랩'),('progress_m','진행 m'),('end_reason','주행 종료'),('stop_kind','정차 방식'),
                           ('eligible','학습 가능'),('q_updates','Q/V 갱신'),('actor_updates','정책 갱신'),
                           ('policy_before','적용 전 정책'),('policy_after','적용 후 정책'),
                           ('cycle_round','학습 반복'),('cycle_phase','반복 단계'),('countdown','시작까지 초'),('comparison_model','비교 모델'),('episode', '회차'), ('updates', '갱신'), ('reward', '보상'), ('samples', '샘플'), ('total_samples', '전체 샘플'), ('elapsed_seconds', '경과 초'), ('reason', '상태')]:
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
        apply_preset(app, preset)
        if args.prepare: root.after(100, lambda: app.start(args.resume))
    elif args.prepare:
        parser.error('--prepare requires a preset')
    try: root.mainloop()
    finally: instance.close()
