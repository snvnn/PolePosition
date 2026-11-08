import copy
import json
from pathlib import Path
import tempfile
import unittest
from ModelTrainer.common import digest
from .lap_gamepad import normalized_controls,verified_controls_reference


class ControlsInventoryTests(unittest.TestCase):
    def setUp(self):
        self.device=json.loads((Path(__file__).resolve().parents[1]/'GamepadIO/tests/unbound-virtual-device.json').read_text())
        self.base=dict(carControllerMode=1,gamepadSettings=dict(gamma=3,gear='A'),
                       commandDevices=[dict(id='bound',axes=[1,2])])
        self.current=copy.deepcopy(self.base);self.current['commandDevices'].append(self.device)

    def test_registered_unbound_virtual_pad_and_order_are_equivalent(self):
        self.current['commandDevices'].reverse()
        self.assertEqual(normalized_controls(self.base),normalized_controls(self.current))

    def test_bindings_axis_parameters_and_unknown_fields_are_not_ignored(self):
        for key,value in [('raceCommandAxisList',[dict(axisCode='Steer')]),
                          ('raceCommandButtonList',[dict(actionType='GearUp')]),
                          ('uICommandButtonList',[dict(key='A')]),('steerScale',2),('unexpected',True)]:
            with self.subTest(key=key):
                changed=copy.deepcopy(self.current);changed['commandDevices'][1][key]=value
                self.assertNotEqual(normalized_controls(self.base),normalized_controls(changed))
        changed=copy.deepcopy(self.current);changed['gamepadSettings']['gamma']=2
        self.assertNotEqual(normalized_controls(self.base),normalized_controls(changed))
        changed=copy.deepcopy(self.current);changed['commandDevices'][0]['axes'].reverse()
        self.assertNotEqual(normalized_controls(self.base),normalized_controls(changed))

    def test_multiple_virtual_entries_rejected(self):
        self.current['commandDevices'].append(copy.deepcopy(self.device))
        with self.assertRaises(ValueError):normalized_controls(self.current)

    def test_reference_is_pinned_and_live_settings_are_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            reference=Path(tmp)/'reference.json';current=Path(tmp)/'controls.json'
            reference.write_text(json.dumps(self.base));current.write_text(json.dumps(self.current))
            profile=dict(controls=str(current),controls_reference=str(reference),input_sha256={str(current):digest(reference)})
            self.assertEqual(verified_controls_reference(profile),reference)
            changed=copy.deepcopy(self.current);changed['gamepadSettings']['gear']='X'
            current.write_text(json.dumps(changed))
            with self.assertRaises(ValueError):verified_controls_reference(profile)
            current.write_text(json.dumps(self.current));reference.write_text(json.dumps(self.current))
            with self.assertRaises(ValueError):verified_controls_reference(profile)

if __name__=='__main__':unittest.main()
