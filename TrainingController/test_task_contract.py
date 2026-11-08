"""Command behavior captured before the responsibility split, without launching jobs."""
import json
from pathlib import Path
import tempfile
import unittest

from .jobs import command
from .task_fixtures import command_inputs, command_outcome


class TaskContractTests(unittest.TestCase):
    def test_commands_and_rejections_match_pre_refactor_contract(self):
        cases = json.loads(Path(__file__).with_name('task_contract.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for case in cases:
                with self.subTest(task=case['task'], overrides=case['values'], resume=case['resume']):
                    values = command_inputs(root, case['task']) | case['values']
                    self.assertEqual(command_outcome(command, root, case['task'], values, case['resume']),
                                     case['expected'])
