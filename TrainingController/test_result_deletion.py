import tempfile
import unittest
from pathlib import Path

from .results import delete_result, recent_results


class ResultDeletionTests(unittest.TestCase):
    def test_deletes_only_selected_result_and_refreshes_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root/'actuator_runs'/'first'
            second = root/'actuator_runs'/'second'
            first.mkdir(parents=True); second.mkdir()
            (first/'samples.jsonl').write_text('sample')
            delete_result(root, first)
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())
            self.assertEqual(recent_results(root, [first]), [second.resolve()])

    def test_rejects_parent_and_outside_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'workspace'; root.mkdir()
            parent = root/'actuator_runs'; parent.mkdir()
            outside = Path(tmp)/'outside'; outside.mkdir()
            for target in (root, parent, outside):
                with self.subTest(target=target), self.assertRaises(ValueError):
                    delete_result(root, target)
                self.assertTrue(target.exists())
