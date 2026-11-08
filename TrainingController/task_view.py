from pathlib import Path
import json
import tkinter as tk
from tkinter import filedialog, ttk
from .task_catalog import BY_LABEL, TASKS, TASK_GROUPS, TASK_FIELDS
from .jobs import request



class TaskView:
    def path_row(self, frame, row, label, name, directory):
        variable = tk.StringVar()
        caption=ttk.Label(frame, text=label)
        caption.grid(row=row, column=0, sticky='w', padx=(0, 12))
        entry = ttk.Entry(frame, textvariable=variable)
        entry.grid(row=row, column=1, sticky='ew', pady=5)
        def choose():
            path = (filedialog.askdirectory if directory or (name == 'checkpoint' and self.task.get() == '강화학습') else filedialog.askopenfilename)(parent=self.root, initialdir=self.workspace)
            if path:
                variable.set(path)
        button = ttk.Button(frame, text='선택', command=choose)
        button.grid(row=row, column=2, padx=5)
        self.inputs.extend([entry, button])
        self.field_widgets[name] = [entry, button]
        self.path_widgets[name]=[caption,entry,button]
        return variable

    def lock(self, locked):
        self.locked = locked
        self.update_controls()
        self.stop_button.configure(state='normal' if locked else 'disabled')

    def update_controls(self):
        active = TASK_FIELDS[self.task.get()] | {'task', 'task_group'}
        active.add('start')
        if BY_LABEL[self.task.get()].resumable:
            active.add('resume')
        if self.task.get() == '평가·주행' and self.mode.get() == 'drive':
            active.add('previous_run')
        for name, widgets in self.field_widgets.items():
            for widget in widgets:
                enabled = not self.locked and name in active
                widget.configure(state=('readonly' if isinstance(widget, ttk.Combobox) else 'normal') if enabled else 'disabled')
        for name, widgets in self.path_widgets.items():
            for widget in widgets:
                if name in active:widget.grid()
                else:widget.grid_remove()
        for name,widgets in self.option_groups.items():
            for widget in widgets:
                widget.pack_forget()
                if name in active:widget.pack(side='left',padx=3)
        for widget in (self.limits_label,self.limits_frame):
            if 'seconds' in active:widget.grid()
            else:widget.grid_remove()
        self.epoch_label.pack_forget()
        self.field_widgets['epochs'][0].pack_forget()
        if 'epochs' in active:
            self.epoch_label.pack(side='left',padx=12)
            self.field_widgets['epochs'][0].pack(side='left')
        if 'resume' in active:self.field_widgets['resume'][0].pack(side='left',padx=4)
        else:self.field_widgets['resume'][0].pack_forget()
        self.field_widgets['start'][0].configure(text=BY_LABEL[self.task.get()].start_label)
        self.field_widgets['resume'][0].configure(text='온라인 체크포인트에서 재개' if self.task.get()=='게임패드 회차 학습' else '선택 체크포인트 평가' if self.task.get()=='고정 정책 연속주행 평가' else '이어서 학습')
        self.update_result_actions()
        self.update_config_preview()

    def update_config_preview(self):
        import tomllib
        try:
            path=Path(self.config.get())
            if not path.is_absolute():path=self.workspace/path
            content=path.read_text(encoding='utf-8')
            c=json.loads(content) if path.suffix.lower()=='.json' else tomllib.loads(content)
            parts=[]
            if c.get('schema')=='gamepad_policy_comparison_v1':
                parts.append('5개 정책 × 4회 + 후보·대조 각 5회')
                parts.append('1회 최대 180초 · 전체 최대 '+str(c['session_seconds'])+'초')
                parts.append('시작 후 10초 내 ACC 전환 · F8 중지')
            if c.get('model'):parts.append('모델: '+Path(c['model']).name)
            if 'seconds' in c:parts.append('시험 시간: '+('제한 없음 · F8로 중지' if c['seconds'] == 0 else str(c['seconds'])+'초'))
            if 'steering_smoothing_ms' in c:parts.append('조향 LPF: '+('끔' if not c['steering_smoothing_ms'] else str(c['steering_smoothing_ms'])+'ms'))
            if 'epochs' in c:parts.append('학습: 최대 '+str(c['epochs'])+'회')
            self.configuration_summary.set('  |  '.join(parts) or '설정 파일: '+path.name)
        except (OSError,ValueError):
            self.configuration_summary.set('설정 파일을 선택해 주세요.')

    def select_task_group(self):
        tasks = TASK_GROUPS[self.task_group.get()]
        if self.task.get() not in tasks:
            self.task.set(tasks[0])
        self.select_task()

    def select_task(self):
        # Presets and result shortcuts can select a task outside the visible group.
        for group, tasks in TASK_GROUPS.items():
            if self.task.get() in tasks:
                self.task_group.set(group)
                self.field_widgets['task'][0].configure(values=tasks)
                break
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
        self.detail.set(BY_LABEL[self.selected_task].description)
        self.update_controls()

    def start(self, resume):
        try:
            fields = ('seconds', 'epochs', 'dataset', 'checkpoint', 'config', 'model', 'prepared', 'profile',
                      'previous_run', 'controller_config', 'mode', 'model_seconds', 'split')
            values = {name: getattr(self, name).get() for name in fields}
            self.coordinator.start(request(self.workspace, self.task.get(), values, resume))
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


def apply_preset(app, preset):
    if preset.get('task') not in TASKS:
        raise ValueError('Unknown preset task')
    app.task.set(preset['task'])
    app.select_task()
    for name in TASK_FIELDS[preset['task']]:
        if name in preset:
            getattr(app, name).set(str(preset[name]))
