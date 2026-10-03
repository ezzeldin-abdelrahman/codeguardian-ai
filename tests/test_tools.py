import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codeguardian.tools import run_bandit, run_ruff


class ToolTests(unittest.TestCase):
    def test_bandit_converts_findings_with_exit_code_one(self):
        report = {"errors": [], "results": [{
            "test_id": "B105", "test_name": "hardcoded_password_string",
            "issue_severity": "LOW", "line_number": 3,
            "issue_text": "Possible hardcoded password.",
        }]}
        result = subprocess.CompletedProcess([], 1, json.dumps(report), "")
        with patch("codeguardian.tools.subprocess.run", return_value=result):
            findings = run_bandit(__file__)
        self.assertEqual(findings, [{
            "source": "bandit", "category": "security", "severity": "low",
            "title": "B105: hardcoded_password_string", "line": 3,
            "description": "Possible hardcoded password.",
            "recommendation": "Review this security issue and replace the unsafe pattern.",
        }])

    def test_ruff_converts_findings_with_exit_code_one(self):
        report = [{"code": "F401", "message": "Unused import",
                   "location": {"row": 1}, "fix": {"message": "Remove unused import"}}]
        result = subprocess.CompletedProcess([], 1, json.dumps(report), "")
        with patch("codeguardian.tools.subprocess.run", return_value=result):
            findings = run_ruff(__file__)
        self.assertEqual(findings, [{
            "source": "ruff", "category": "quality", "severity": "low",
            "title": "F401: Unused import", "line": 1,
            "description": "Unused import", "recommendation": "Remove unused import",
        }])

    def test_clean_reports_return_empty_lists(self):
        for run, report in [(run_bandit, {"errors": [], "results": []}), (run_ruff, [])]:
            with self.subTest(tool=run.__name__):
                result = subprocess.CompletedProcess([], 0, json.dumps(report), "")
                with patch("codeguardian.tools.subprocess.run", return_value=result):
                    self.assertEqual(run(__file__), [])

    def test_missing_tools_have_clear_errors(self):
        for run, name in [(run_bandit, "bandit"), (run_ruff, "ruff")]:
            with self.subTest(tool=name):
                result = subprocess.CompletedProcess([], 1, "", f"No module named {name}")
                with patch("codeguardian.tools.subprocess.run", return_value=result):
                    with self.assertRaisesRegex(RuntimeError, "not installed"):
                        run(__file__)

    def test_failed_commands_and_invalid_json_raise_errors(self):
        for run in (run_bandit, run_ruff):
            for code, output, expected in [(2, "", "failed"), (0, "not JSON", "valid JSON")]:
                with self.subTest(tool=run.__name__, code=code):
                    result = subprocess.CompletedProcess([], code, output, "")
                    with patch("codeguardian.tools.subprocess.run", return_value=result):
                        with self.assertRaisesRegex(RuntimeError, expected):
                            run(__file__)

    def test_bandit_scan_errors_are_not_clean_results(self):
        report = {"errors": [{"reason": "syntax error"}], "results": []}
        result = subprocess.CompletedProcess([], 0, json.dumps(report), "")
        with patch("codeguardian.tools.subprocess.run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "could not complete the scan"):
                run_bandit(__file__)


if __name__ == "__main__":
    unittest.main()
