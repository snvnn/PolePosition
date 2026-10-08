import os
from pathlib import Path
import tempfile
import unittest

from .results import recent_results, result_label, resume_checkpoint


class ResultTests(unittest.TestCase):
    def test_all_task_types_are_sorted_together_and_duplicates_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runs = [root/name/'run' for name in ('training_runs', 'recordings', 'evaluation_runs')]
            for index, path in enumerate(runs):
                path.mkdir(parents=True)
                os.utime(path, (100 + index, 100 + index))
            self.assertEqual(recent_results(root, [runs[0], root/'deleted']), list(reversed(runs)))
            self.assertEqual(recent_results(root, limit=1), [runs[-1]])
            self.assertIn('평가', result_label(runs[-1]))

    def test_missing_parent_and_missing_checkpoint_are_not_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(recent_results(root), [])
            self.assertIsNone(resume_checkpoint(root))
            (root/'last.pt').write_bytes(b'not loaded here')
            self.assertEqual(resume_checkpoint(root), root/'last.pt')


if __name__ == '__main__':
    unittest.main()
