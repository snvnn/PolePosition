"""Qualification is limited to one setting change, never driving success."""
import tempfile
import unittest
from pathlib import Path

from ModelTrainer.common import save, digest, hashes
from .validation import qualify_gearing_only_change


class QualificationTests(unittest.TestCase):
    def test_only_gearing_change_preserves_transport_evidence(self):
        for encoding in ('utf-8', 'utf-16le', 'utf-16'):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                paths = {k: root / k for k in ('controls', 'dll', 'driver_path', 'assists')}
                for key, path in paths.items():
                    path.write_bytes(key.encode())
                manual = '{"autoGear": 0, "autoClutch": 1, "stabilityControl": 0}'
                paths['assists'].write_bytes(manual.encode(encoding))
                m = dict(semantics='gamepad_v1', passed=True, numeric_passed=True,
                         visual_response_confirmed=True, assists_sha256=digest(paths['assists']),
                         controls_sha256=digest(paths['controls']), dll_sha256=digest(paths['dll']),
                         driver_sha256=digest(paths['driver_path']),
                         evidence_sha256=hashes([paths['controls'], paths['dll']]))
                evidence = root / 'manual.json'
                profile = root / 'profile.json'
                output = root / 'qualified.json'
                save(evidence, m)
                save(profile, {k: str(v) for k, v in paths.items()})
                automatic = manual.replace('"autoGear": 0', '"autoGear": 1')
                paths['assists'].write_bytes(automatic.encode(encoding))
                result = qualify_gearing_only_change(evidence, profile, output)
                self.assertEqual(result['kind'], 'qualified_axis_transport')
                self.assertTrue(result['passed'])
                self.assertFalse(result['automatic_drivetrain_verified'])
                self.assertFalse(result['in_motion_equivalence_verified'])
                paths['assists'].write_bytes(automatic.replace('"stabilityControl": 0', '"stabilityControl": 1').encode(encoding))
                with self.assertRaisesRegex(ValueError, 'single verified autoGear'):
                    qualify_gearing_only_change(evidence, profile, output)
                paths['assists'].write_bytes(automatic.encode(encoding))
                paths['controls'].write_bytes(b'changed steering filter')
                with self.assertRaises(ValueError):
                    qualify_gearing_only_change(evidence, profile, output)


if __name__ == '__main__':
    unittest.main()
