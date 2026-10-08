import unittest
from pathlib import Path
from .isolation import validate_configuration

class IsolationTests(unittest.TestCase):
    def test_allow_only_controller_and_hide_physical_pad(self):
        c=dict(controller=str(Path('controller.exe').resolve()),visibility_probe=str(Path('probe.exe').resolve()),game_executables=[str(Path('acc.exe').resolve())],hidden_device_paths=['HID\\PAD'])
        text='--cloak-on\n--inv-off\n--app-reg "'+c['controller']+'"\n--dev-hide "HID\\PAD"\n'
        validate_configuration(text,c)
        for bad in [text.replace('--cloak-on','--cloak-off'),text.replace('--inv-off','--inv-on'),text.replace('HID\\PAD','HID\\OTHER'),text+'--app-reg "'+c['game_executables'][0]+'"',text+'--app-reg "'+c['visibility_probe']+'"']:
            with self.subTest(bad=bad),self.assertRaises(ValueError):validate_configuration(bad,c)

if __name__=='__main__':unittest.main()
