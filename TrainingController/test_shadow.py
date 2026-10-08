import csv
from pathlib import Path
import tempfile
import tkinter as tk
import unittest

from ModelAndActuator.report import read_rows
from .__main__ import Controller, ROOT
from .jobs import command


class ShadowTests(unittest.TestCase):
    def test_gui_routes_only_to_native_comparison_and_locks_drive_controls(self):
        root=tk.Tk(); root.withdraw()
        try:
            app=Controller(root,monitoring=False)
            app.task.set('새 정책 비교 주행'); app.select_task()
            for name in ('start','model','prepared','profile','seconds'):
                self.assertNotEqual(str(app.field_widgets[name][0]['state']),'disabled')
            for name in ('mode','model_seconds','previous_run','resume'):
                self.assertEqual(str(app.field_widgets[name][0]['state']),'disabled')
            app.lock(True)
            self.assertTrue(all(str(w['state'])=='disabled' for w in app.inputs))
            app.show_state(dict(phase='policy_shadow',samples=15))
            self.assertIn('새 정책',app.status.get())
            self.assertIn('15',app.detail.get())
        finally:
            root.update_idletasks()
            root.destroy()
        values=dict(seconds='120',model=str(ROOT/'reinforcement_runs/sac-pretraining-1791009589852996200-037e7448'),
                    prepared=str(ROOT/'state_runs/prepared-1790679300781499500-3e604d8c'),
                    profile=str(ROOT/'input_profiles/calibration-1790823244181544300-71cd97f2/calibration.json'),
                    config=str(ROOT/'ModelAndActuator/config.toml'),controller_config=str(ROOT/'BaselineController/config.toml'),
                    mode='drive',model_seconds='15')
        args=command(ROOT,'새 정책 비교 주행',values)
        self.assertIn('v2-shadow',args)
        self.assertNotIn('--mode',args)
        self.assertNotIn('--model-seconds',args)

    def test_report_distinguishes_v1_and_v2_feature_width(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'policy.csv'
            def write(version,width):
                with path.open('w',newline='',encoding='utf-8') as stream:
                    writer=csv.writer(stream)
                    writer.writerow(['now_ns','features_json','feature_schema_version'])
                    writer.writerow([1,str([0]*width),version])
            for version,width in [(1,19),(2,35)]:
                write(version,width); self.assertEqual(len(read_rows(path,('features_json',))),1)
            for version,width in [(1,35),(2,19),(3,35)]:
                write(version,width)
                with self.assertRaises(ValueError): read_rows(path,('features_json',))


if __name__=='__main__': unittest.main()
