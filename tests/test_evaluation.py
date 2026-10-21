"""Evaluation scoring tests with hand-written predictions; no API calls."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluation"))
import scoring

LABEL = {"type": "sql_injection", "file": "users.py", "line": 6}


def finding(title="SQL injection in query", line=6, file="users.py", category="security"):
    return {"category": category, "severity": "high", "title": title, "file": file, "line": line,
            "description": "User input is placed in the query.", "recommendation": "Use parameters."}


def trace(findings, statuses=None):
    """A completed review; statuses default to supported for every finding."""
    statuses = statuses or ["supported"] * len(findings)
    return {"status": "complete", "findings": findings, "verification": {
        "results": [{"finding_id": f"F{n:03}", "verification_status": s} for n, s in enumerate(statuses, 1)],
        "input_tokens": 10, "output_tokens": 5}}


class EvaluationTests(unittest.TestCase):
    def score(self, findings, labels=(LABEL,), statuses=None):
        return scoring.score_case("case", trace(findings, statuses), list(labels))

    def test_finding_types_from_keywords(self):
        cases = {"SQL injection": "sql_injection", "Unsafe pickle loading": "unsafe_deserialization",
                 "Path traversal": "path_traversal", "Command injection via os.system": "command_injection",
                 "Weak MD5 password hashing": "insecure_crypto", "Hard-coded database password": "hardcoded_credentials",
                 "Missing docstring": "other"}
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(scoring.finding_type({"title": title, "description": ""}), expected)

    def test_perfect_prediction(self):
        result = self.score([finding()])
        self.assertEqual((result["before"]["tp"], result["before"]["fp"], result["before"]["fn"]), (1, 0, 0))
        self.assertEqual(scoring.metrics(1, 0, 0)["f1"], 1.0)

    def test_missed_vulnerability(self):
        result = self.score([])
        self.assertEqual(result["before"]["fn"], 1)
        self.assertEqual(result["before"]["missed"], [LABEL])

    def test_false_positive_on_safe_case(self):
        result = self.score([finding()], labels=[])
        self.assertEqual((result["before"]["tp"], result["before"]["fp"]), (0, 1))

    def test_duplicates_count_once(self):
        result = self.score([finding(), finding(line=7)])
        self.assertEqual((result["before"]["tp"], result["before"]["fp"]), (1, 1))
        self.assertEqual(result["before"]["matched"], ["F001"])  # the closer line wins

    def test_wrong_issue_type(self):
        result = self.score([finding(title="Hard-coded password")])
        self.assertEqual((result["before"]["tp"], result["before"]["fp"], result["before"]["fn"]), (0, 1, 1))

    def test_line_tolerance_and_wrong_file(self):
        self.assertEqual(self.score([finding(line=9)])["before"]["tp"], 1)    # 3 lines away: match
        self.assertEqual(self.score([finding(line=10)])["before"]["tp"], 0)   # 4 lines away: no match
        self.assertEqual(self.score([finding(line=None)])["before"]["tp"], 0)
        self.assertEqual(self.score([finding(file="other.py")])["before"]["tp"], 0)

    def test_quality_findings_are_not_scored(self):
        result = self.score([finding(title="Missing tests", category="testing")], labels=[])
        self.assertEqual(result["before"]["fp"], 0)

    def test_empty_predictions_and_empty_labels(self):
        self.assertEqual(scoring.metrics(0, 0, 0), {"tp": 0, "fp": 0, "fn": 0,
                                                     "precision": None, "recall": None, "f1": None})
        self.assertEqual(scoring.metrics(0, 0, 1)["precision"], None)
        self.assertEqual(scoring.metrics(0, 1, 1)["f1"], 0.0)

    def test_verification_rejecting_a_correct_finding(self):
        result = self.score([finding(), finding(title="SQL injection", line=40)], statuses=["uncertain", "unsupported"])
        self.assertEqual((result["before"]["tp"], result["before"]["fp"]), (1, 1))
        self.assertEqual((result["after"]["tp"], result["after"]["fp"], result["after"]["fn"]), (0, 0, 1))
        self.assertEqual(result["correct_rejected"], ["F001"])
        self.assertEqual(result["verification_counts"], {"supported": 0, "uncertain": 1, "unsupported": 1})

    def test_failed_and_incomplete_runs_are_not_clean(self):
        failed = [scoring.score_case("a", None, [LABEL]),
                  scoring.score_case("b", {"status": "incomplete", "error": "API error 429"}, [LABEL])]
        self.assertEqual([r["status"] for r in failed], ["failed", "failed"])
        self.assertEqual(failed[1]["error"], "API error 429")

    def test_every_label_points_to_a_real_line(self):
        labels = json.loads((ROOT / "evaluation" / "labels.json").read_text())["cases"]
        cases = ROOT / "evaluation" / "cases"
        self.assertEqual(set(labels), {path.name for path in cases.iterdir() if path.is_dir()})
        for case_id, case_labels in labels.items():
            folder = cases / case_id / "after" if (cases / case_id / "after").is_dir() else cases / case_id
            for label in case_labels:
                with self.subTest(case=case_id):
                    lines = (folder / label["file"]).read_text().splitlines()
                    self.assertLessEqual(label["line"], len(lines))
                    self.assertTrue(lines[label["line"] - 1].strip())


if __name__ == "__main__":
    unittest.main()
