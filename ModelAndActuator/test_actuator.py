import csv
import json
from pathlib import Path
import tempfile
import unittest
from .setup import backup, verify_backup, restore, digest, save_json
from .calibration import read_acc, validate_profile, validate_axis_bindings
from .report import read_rows, extrema, report, verify


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.acc = self.root / "acc"
        (self.acc / "Config").mkdir(parents=True)
        (self.acc / "Config/controls.json").write_bytes(b'{"carControllerMode":1}\n')
        (self.acc / "Config/assists.json").write_bytes('{"autoGear":1}'.encode("utf-16-le"))
        self.backups = self.root / "snapshots"

    def tearDown(self):
        self.temp.cleanup()

    def test_raw_backup_and_restore(self):
        saved = backup(self.acc, self.backups)
        original = (self.acc / "Config/assists.json").read_bytes()
        (self.acc / "Config/assists.json").write_bytes(b'{"autoGear":0}')
        outgoing = restore(saved, game_closed=True, destination=self.acc, parent=self.backups)
        self.assertEqual((self.acc / "Config/assists.json").read_bytes(), original)
        self.assertEqual((outgoing / "files/Config/assists.json").read_bytes(), b'{"autoGear":0}')

    def test_backup_tamper(self):
        saved = backup(self.acc, self.backups)
        (saved / "files/Config/controls.json").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            verify_backup(saved)

    def test_backup_unique_no_overwrite(self):
        a = backup(self.acc, self.backups)
        b = backup(self.acc, self.backups)
        self.assertNotEqual(a, b)
        self.assertEqual(verify_backup(a)["files"], verify_backup(b)["files"])

    def test_restore_requires_closed_confirmation(self):
        saved = backup(self.acc, self.backups)
        with self.assertRaises(ValueError):
            restore(saved, destination=self.acc)

    def test_acc_bomless_utf16(self):
        self.assertEqual(read_acc(self.acc / "Config/assists.json")["autoGear"], 1)

    def test_unverified_calibration_rejected(self):
        with self.assertRaises(ValueError):
            validate_profile({"schema_version": 1, "kind": "vjoy_calibration", "complete": True})

    def test_axis_assignment_missing_swapped_duplicate_rejected(self):
        axes = [{"axisCode": code, "axisIndex": index, "minRange": -1,
                 "maxRange": 1, "isCombined": False}
                for code, index in (("Steer", 0), ("Gas", 1), ("Brake", 2))]
        validate_axis_bindings({"raceCommandAxisList": axes})
        with self.assertRaises(ValueError):
            validate_axis_bindings({"raceCommandAxisList": []})
        with self.assertRaises(ValueError):
            validate_axis_bindings({"raceCommandAxisList": axes + [axes[0]]})
        axes[1]["axisIndex"] = 2
        with self.assertRaises(ValueError):
            validate_axis_bindings({"raceCommandAxisList": axes})

    def test_reversal_and_nonfinite_rows(self):
        path = self.root / "rows.csv"
        for body in ("now_ns,value\n2,1\n1,2\n", "now_ns,value\n1,nan\n"):
            path.write_text(body, encoding="utf-8")
            with self.assertRaises(ValueError):
                read_rows(path, ("now_ns", "value"))

    def test_extrema_preserved(self):
        points = [(i, float(i % 7)) for i in range(5000)]
        points[1555] = (1555, -1000.)
        points[3555] = (3555, 1000.)
        reduced = extrema(points)
        self.assertIn(points[1555], reduced)
        self.assertIn(points[3555], reduced)
        self.assertLess(len(reduced), len(points))

    def bundle(self):
        run = self.root / "run"
        run.mkdir()
        (run / "commands.csv").write_text("now_ns,applied_throttle,send_success,mode\n1,0,true,Stopping\n", encoding="utf-8")
        (run / "telemetry.csv").write_text("now_ns,sequence,throttle_observed,steering_raw\n1,1,0,0\n", encoding="utf-8")
        (run / "events.csv").write_text("now_ns,kind,detail\n1,stop,<test>\n", encoding="utf-8")
        save_json(run / "summary.json", {"written": {"commands": 1, "telemetry": 1, "events": 1}, "recording_valid": True})
        save_json(run / "manifest.json", {"kind": "actuator_run", "schema_version": 1, "mock": True,
            "input_sha256": {}, "output_sha256": {name: digest(run / name) for name in ("commands.csv", "telemetry.csv", "events.csv", "summary.json")}})
        return run

    def test_report_and_output_conflict(self):
        run = self.bundle()
        page = report(run).read_text(encoding="utf-8")
        self.assertIn("<svg", page)
        self.assertIn("&lt;test&gt;", page)
        self.assertIn("가상 축", page)
        with self.assertRaises(FileExistsError):
            report(run)

    def test_report_input_tamper(self):
        run = self.bundle()
        (run / "telemetry.csv").write_text("changed", encoding="utf-8")
        with self.assertRaises(ValueError):
            verify(run)

    def test_mismatched_counts(self):
        run = self.bundle()
        meta = json.loads((run / "manifest.json").read_text())
        meta["output_sha256"] = {}
        (run / "manifest.json").write_text(json.dumps(meta))
        summary = {"written": {"commands": 5, "telemetry": 1, "events": 1}}
        (run / "summary.json").write_text(json.dumps(summary))
        with self.assertRaises(ValueError):
            verify(run)

    def demo_bundle(self):
        run = self.bundle()
        (run/'telemetry.csv').write_text('now_ns,sequence,observation_ns,tracking_epoch,throttle_observed,steering_raw,position_x,position_z,speed_kmh,lateral_error_m,heading_error_rad,projection_distance_m,path_s_m\n1,1,1,1,0,0,0,0,0,0,0,0,0\n')
        (run/'controller.csv').write_text('now_ns,observation_sequence,observation_ns,tracking_epoch,target_speed_kmh,target_x,target_z,requested_curvature,steering_unclamped,reason,abort,complete\n2,1,1,1,1,8,0,0,0,tracking,false,false\n')
        track=self.root/'track';track.mkdir()
        (track/'reference.csv').write_text('position_x,position_z\n0,0\n10,0\n10,10\n')
        prep=self.root/'prepared';prep.mkdir()
        save_json(prep/'manifest.json',{'track_model_path':str(track),'input_sha256':{str(track/'reference.csv'):digest(track/'reference.csv')}})
        save_json(run/'summary-demo.json',{'written':{'commands':1,'telemetry':1,'events':1,'controller':1},'recording_valid':True})
        (run/'summary.json').write_bytes((run/'summary-demo.json').read_bytes())
        (run/'manifest.json').write_text(json.dumps({'kind':'actuator_run','schema_version':1,'scenario':'demo','mock':True,'prepared_path':str(prep),'input_sha256':{},'output_sha256':{}}))
        return run

    def test_demo_report_and_provenance(self):
        run=self.demo_bundle()
        page=report(run).read_text(encoding='utf-8')
        self.assertIn('데모 구간 궤적',page)
        self.assertIn('데모 목표·관측 속도',page)
        self.assertIn('경로 횡오차',page)

    def test_demo_missing_and_wrong_provenance(self):
        run=self.demo_bundle()
        p=run/'controller.csv'
        p.write_text(p.read_text().replace('2,1,1,1,1,','2,99,1,1,1,'))
        with self.assertRaisesRegex(ValueError,'provenance'):
            verify(run)
        p.unlink()
        with self.assertRaisesRegex(ValueError,'missing'):
            verify(run)

    def test_terrain_sign_grouping_and_partial_run(self):
        from .terrain import analyze
        rows=[{'sequence':str(i),'reference_grade_pct':str(g),'speed_kmh':str(v),'throttle_observed':'0.1','brake_observed':'0.2','position_y':str(i)} for i,(g,v) in enumerate([(5,8),(0,10),(-5,12)])]
        control=[{'observation_sequence':str(i),'reason':'tracking','target_speed_kmh':'10','lap_fraction':'0.3'} for i in range(3)]
        out=analyze(rows,control,{'lap_completed':False})
        self.assertEqual(out['groups']['uphill_gt_2pct']['speed_error_mean_kmh'],-2)
        self.assertEqual(out['groups']['downhill_lt_minus2pct']['speed_error_mean_kmh'],2)
        self.assertEqual(out['observed_height_range_m'],2)
        self.assertFalse(out['lap_completed'])

    def test_terrain_report_panels(self):
        run=self.demo_bundle()
        path=run/'telemetry.csv'
        with path.open(encoding='utf-8') as f:
            rows=list(csv.DictReader(f))
        for row in rows:
            row.update(position_y='2',reference_elevation_m='2',reference_grade_pct='3',accumulated_m='0',brake_observed='0')
        with path.open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
        page=report(run).read_text(encoding='utf-8')
        self.assertIn('고저차·속도 반응 분석',page)
        self.assertIn('진행 방향 경사',page)


if __name__ == "__main__":
    unittest.main()
