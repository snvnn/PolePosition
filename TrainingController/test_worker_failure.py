"""Controller failure reporting preserves known artifacts, without recovery runs."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from .worker import main


class WorkerFailureTests(unittest.TestCase):
    def invoke(self, operation):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['worker', '--dataset', 'fixture']), \
             patch.object(sys, 'stdin', io.StringIO('')), \
             patch.object(sys, 'stdout', output), \
             patch('ModelTrainer.training.train', side_effect=operation) as train:
            status = main()
        self.assertEqual(status, 1)
        train.assert_called_once()
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events[-1]['state'], 'failed')
        return events[-1]

    def test_storage_failure_after_progress_keeps_known_result_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            def operation(*args, on_progress, **kwargs):
                on_progress(dict(epoch=1, updates=1, loss=.1, result_dir=directory))
                raise OSError('disk full')
            final = self.invoke(operation)
            self.assertEqual(final['result_dir'], directory)
            self.assertIn('disk full', final['reason'])

    def test_manifest_failure_after_return_keeps_result_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            final = self.invoke(lambda *args, **kwargs: Path(directory))
            self.assertEqual(final['result_dir'], directory)
            self.assertIn('FileNotFoundError', final['reason'])

    def test_failure_before_output_does_not_invent_a_folder(self):
        def operation(*args, **kwargs):
            raise ValueError('invalid source')
        final = self.invoke(operation)
        self.assertIsNone(final['result_dir'])


if __name__ == '__main__':
    unittest.main()
