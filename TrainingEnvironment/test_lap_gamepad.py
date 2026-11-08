import unittest
from unittest.mock import patch
from .lap_gamepad import select

class LapSplitTests(unittest.TestCase):
    def test_invalid_and_duplicate_laps_rejected(self):
        lap={'all_samples_lap_valid':True}
        proof={'format_pass':True,'laps':[lap,{'all_samples_lap_valid':False}]}
        with patch('TrainingEnvironment.lap_gamepad.validate',return_value=proof):
            with self.assertRaises(ValueError):select({'laps':[{'recording':'same','lap_index':1,'split':'train'},{'recording':'same','lap_index':1,'split':'validation'}]})
            with self.assertRaises(ValueError):select({'laps':[{'recording':'same','lap_index':1,'split':'train'},{'recording':'same','lap_index':2,'split':'validation'}]})
    def test_holdout_is_separate_lap(self):
        proof={'format_pass':True,'laps':[{'all_samples_lap_valid':True},{'all_samples_lap_valid':True}]}
        with patch('TrainingEnvironment.lap_gamepad.validate',return_value=proof):
            r=select({'laps':[{'recording':'same','lap_index':1,'split':'train'},{'recording':'same','lap_index':2,'split':'validation'}]})
            self.assertEqual([x[2:] for x in r],[('train',1),('validation',2)])
