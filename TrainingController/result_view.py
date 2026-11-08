import os
from .results import recent_results, result_label, resume_checkpoint



class ResultView:
    def add_result(self, path):
        self.refresh_results([path])

    def refresh_results(self, additional=()):
        selected = self.selected_result()
        self.results = recent_results(self.workspace, [*self.results, *additional])
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
                              (self.delete_button, selected is not None and selected.is_dir() and not self.coordinator.busy and not self.coordinator.background),
                              (self.refresh_button, True)):
            button.configure(state='normal' if idle and ready else 'disabled')

    def delete_selected_result(self):
        if self.locked or self.closing or self.coordinator.busy or self.coordinator.background:
            return
        selected = self.selected_result()
        if selected is None:
            return
        from tkinter import messagebox
        from .results import delete_result
        if not messagebox.askyesno('선택 로그 삭제',
                f'{selected.name}\n\n이 결과 폴더와 포함된 로그·보고서·모델 파일을 모두 삭제합니다. 복구할 수 없습니다.\n\n삭제할까요?', parent=self.root):
            return
        if self.coordinator.busy or self.coordinator.background or self.closing:
            return
        try:
            delete_result(self.workspace, selected)
            self.detail.set('삭제 완료: '+selected.name)
        except (OSError, ValueError) as error:
            self.detail.set('삭제 실패: '+str(error))
        self.refresh_results()

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
        online = False
        try:
            manifest = read(selected/'manifest.json')
            kind = manifest.get('kind')
            online = kind=='gamepad_online_session' or manifest.get('continuous_semantics')=='gamepad_continuous_iql_v1'
        except (OSError, ValueError):
            kind = None
        self.task.set('게임패드 회차 학습' if online else '강화학습' if kind == 'sac_checkpoint' else '강화학습 사전학습' if kind == 'sac_pretraining' else '모방학습')
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
