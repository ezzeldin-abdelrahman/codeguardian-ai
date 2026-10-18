"""Optional live test: makes real API calls, so it only runs when explicitly enabled.

    CODEGUARDIAN_LIVE_TEST=1 python -m unittest tests.test_live_demo -v
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import demo_diff_review


@unittest.skipUnless(os.getenv("CODEGUARDIAN_LIVE_TEST") == "1", "Set CODEGUARDIAN_LIVE_TEST=1 to call the real API")
class LiveDemoTest(unittest.TestCase):
    def test_diff_review_and_verification_end_to_end(self):
        checks, output = demo_diff_review.run_demo()
        for description, passed in checks:
            with self.subTest(check=description):
                self.assertTrue(passed, output)


class DemoChecklistTest(unittest.TestCase):
    """Free check of the checklist logic itself, using a hand-written report."""

    def test_checklist_passes_for_expected_report(self):
        report = {"status": "complete", "changes": {"base": "abc", "changes": [{"file": "app.py"}]},
                  "findings": [{"file": "app.py", "category": "security", "line": 11}],
                  "verification": {"results": [{"finding_id": "F001", "verification_status": "supported",
                                                "corroborating_tools": ["bandit"]}]}}
        self.assertTrue(all(passed for _, passed in demo_diff_review.check_report(report, "abc")))

    def test_checklist_fails_when_unchanged_file_is_reported(self):
        report = {"status": "complete", "findings": [{"file": "helper.py", "category": "security", "line": 3}]}
        self.assertFalse(all(passed for _, passed in demo_diff_review.check_report(report, "abc")))


if __name__ == "__main__":
    unittest.main()
