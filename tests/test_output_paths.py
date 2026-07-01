import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from utils import output_paths


class OutputPathTests(unittest.TestCase):
    def test_unique_run_directory_and_categories(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(output_paths, "RUNS_ROOT", Path(tmp) / "runs"), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(output_paths.RUN_ENV, None)
            fixed = dt.datetime(2026, 6, 19, 9, 0, 0).astimezone()
            first = output_paths.create_run_dir("v7-2", now=fixed)
            os.environ.pop(output_paths.RUN_ENV, None)
            second = output_paths.create_run_dir("v7-2", now=fixed)
            self.assertNotEqual(first, second)
            self.assertTrue((first / "reports").is_dir())
            self.assertTrue((first / "json").is_dir())
            self.assertTrue((first / "html").is_dir())

    def test_manifest_is_utf8_and_contains_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "2026-06-19_090000_test"
            run_dir.mkdir()
            for child in ("reports", "json", "html", "csv", "logs"):
                (run_dir / child).mkdir()
            path = output_paths.write_run_manifest({
                "phase": "test", "task_name": "路径测试",
                "decision_status": "FREEZE", "data_status": "WARNING",
                "model_status": "REFERENCE_ONLY", "output_files": ["html/test.html"],
                "blocked_reason": "test", "next_action": "review",
                "source_data_used": ["local"],
                "whether_root_directory_was_modified": "No",
            }, run_dir)
            text = path.read_text(encoding="utf-8")
            for field in ("run_id", "generated_at", "phase", "task_name", "decision_status", "data_status", "model_status", "output_files", "blocked_reason", "next_action", "git_commit_hash", "source_data_used", "whether_root_directory_was_modified"):
                self.assertIn(field, text)
