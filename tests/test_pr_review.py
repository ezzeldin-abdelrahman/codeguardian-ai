"""v1.0 pull request review tests: merge-base diffs, changed lines, reports, and the GitHub script.

Real temporary Git repositories and real (local, free) Bandit runs; no paid API calls.
"""

import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from codeguardian import reviewer  # noqa: E402
from codeguardian.changed_lines import mark_changed_lines, static_review  # noqa: E402
from codeguardian.git_changes import collect_changes  # noqa: E402
from codeguardian.markdown_report import write_markdown  # noqa: E402

spec = importlib.util.spec_from_file_location("pr_review", ROOT / ".github" / "scripts" / "pr_review.py")
pr_review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pr_review)

SAFE_APP = 'def greet(name):\n    return "Hello " + name\n'
SQL_APP = SAFE_APP + ('\n\ndef find(cursor, name):\n'
                      '    return cursor.execute(f"SELECT * FROM users WHERE name = \'{name}\'")\n')
OLD_BUG = 'def calculate(text):\n    return eval(text)\n'


@unittest.skipUnless(shutil.which("git"), "Git is required")
class PullRequestDiffTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.git("init", "-q", "-b", "main")
        self.write("app.py", SAFE_APP)
        self.write("helper.py", OLD_BUG)          # an old issue that a PR review must not call new
        self.commit("start")
        self.start = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "feature")

    def git(self, *arguments):
        command = ["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
        return subprocess.run([*command, *arguments], capture_output=True, text=True, check=True).stdout.strip()

    def write(self, name, text):
        (self.root / name).write_text(text)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def main_moves_on(self):
        """main gets a new commit after the branch started, as usually happens with real PRs."""
        self.git("checkout", "-q", "main")
        self.write("helper.py", OLD_BUG + "\nVERSION = 2\n")
        self.commit("main changes helper.py")
        self.git("checkout", "-q", "feature")

    def test_merge_base_reviews_only_the_branch_changes(self):
        self.write("app.py", SQL_APP)
        self.commit("feature adds find")
        self.main_moves_on()
        _, changes = collect_changes(self.root, merge_base_with="main")
        self.assertEqual(changes["base"], self.start)
        self.assertEqual(changes["head"], self.git("rev-parse", "HEAD"))
        self.assertEqual([c["file"] for c in changes["changes"]], ["app.py"])
        self.assertEqual(changes["changes"][0]["hunks"][0]["added_lines"], [3, 4, 5, 6])   # head line numbers
        # Comparing with the tip of main instead would wrongly include main's own change.
        _, wrong = collect_changes(self.root, "main")
        self.assertEqual(sorted(c["file"] for c in wrong["changes"]), ["app.py", "helper.py"])

    def test_added_modified_renamed_and_deleted_files(self):
        self.write("new.py", "x = 1\n")
        self.write("app.py", SQL_APP)
        self.git("mv", "helper.py", "tools.py")
        self.commit("feature changes files")
        self.write("new2.py", "y = 2\n")
        self.commit("add another")
        self.git("rm", "-q", "new2.py")
        self.commit("delete it again")
        statuses = {c["file"]: c["status"] for c in collect_changes(self.root, merge_base_with="main")[1]["changes"]}
        self.assertEqual(statuses, {"new.py": "added", "app.py": "modified", "tools.py": "renamed"})
        self.git("rm", "-q", "app.py")
        self.commit("delete app")
        statuses = {c["file"]: c["status"] for c in collect_changes(self.root, merge_base_with="main")[1]["changes"]}
        self.assertEqual(statuses["app.py"], "deleted")

    def test_empty_pr_and_unsupported_files(self):
        self.assertEqual(collect_changes(self.root, merge_base_with="main")[1]["changed_count"], 0)
        self.write("notes.txt", "only text\n")
        self.commit("text only")
        self.assertEqual(collect_changes(self.root, merge_base_with="main")[1]["changed_count"], 0)

    def test_unknown_branch_is_a_clear_error(self):
        with self.assertRaisesRegex(ValueError, "base commit"):
            collect_changes(self.root, merge_base_with="no-such-branch")

    def test_static_review_reports_only_changed_lines(self):
        self.write("app.py", SQL_APP)
        self.commit("feature adds find")
        project, changes = collect_changes(self.root, merge_base_with="main")
        findings, errors = static_review(project, changes)
        self.assertEqual(errors, [])
        self.assertTrue(any(f["title"].startswith("B608") and f["line"] == 6 for f in findings))
        self.assertFalse(any(f["file"] == "helper.py" for f in findings))   # the old eval() is not new
        self.assertTrue(all(f["on_changed_line"] for f in findings))

    def test_cli_no_llm_pr_review_needs_no_api_key_and_writes_reports(self):
        self.write("app.py", SQL_APP)
        self.commit("feature adds find")
        self.main_moves_on()
        report_md, trace = self.root.parent / "pr-report.md", self.root.parent / "pr-report.json"
        self.addCleanup(report_md.unlink, missing_ok=True)
        self.addCleanup(trace.unlink, missing_ok=True)
        argv = ["reviewer.py", str(self.root), "--mode", "diff", "--merge-base", "main", "--no-llm",
                "--report-md", str(report_md), "--trace-file", str(trace), "--title", "PR #7"]
        with patch.object(sys, "argv", argv), patch.object(reviewer, "load_dotenv"), \
             patch.object(reviewer, "get_settings") as settings, redirect_stdout(io.StringIO()):
            reviewer.main()
        settings.assert_not_called()
        text = report_md.read_text()
        self.assertIn("# CodeGuardian review: PR #7", text)
        self.assertIn(f"`{self.start[:10]}..", text)
        self.assertIn("B608", text)
        self.assertNotIn("helper.py", text)
        self.assertEqual(json.loads(trace.read_text())["changes"]["merge_base_with"], "main")


class ChangedLineAndReportTests(unittest.TestCase):
    CHANGES = {"base": "a" * 40, "head": "b" * 40, "merge_base_with": "main", "changed_count": 1,
               "skipped_count": 1, "excluded_count": 0, "limitations": [],
               "skipped": [{"file": "big.py", "reason": "too large"}],
               "changes": [{"status": "modified", "file": "app.py", "old_file": "app.py",
                            "hunks": [{"added_lines": [5, 6], "removed_lines": [5], "current_lines": [4, 5, 6]}]}]}

    @staticmethod
    def finding(file="app.py", line=5, title="SQL injection", severity="high"):
        return {"category": "security", "severity": severity, "title": title, "file": file, "line": line,
                "description": "Input reaches | the query.", "recommendation": "Use parameters.",
                "evidence": "app.py:5 adds an f-string query.", "source": "llm"}

    def test_findings_are_marked_by_changed_line(self):
        findings = [self.finding(), self.finding(line=4), self.finding(file="db.py", line=5), self.finding(line=None)]
        mark_changed_lines(findings, self.CHANGES)
        self.assertEqual([f["on_changed_line"] for f in findings], [True, False, False, False])

    def test_markdown_report(self):
        findings = mark_changed_lines([self.finding(), self.finding(file="db.py", line=2, title="Old issue",
                                                                    severity="low")], self.CHANGES)
        report = {"status": "complete", "error": None, "findings": findings, "changes": self.CHANGES,
                  "verification": {"results": [{"finding_id": "F001", "verification_status": "supported"}]},
                  "rejected_findings": [{"title": "Unseen", "reason": "cited a line that was not observed"}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.md"
            write_markdown(report, "PR #3", path, "AI agent with evidence verification")
            text = path.read_text()
        self.assertIn("# CodeGuardian review: PR #3", text)
        self.assertIn("`aaaaaaaaaa..bbbbbbbbbb` (merge base with `main`)", text)
        self.assertIn("| 0 | 1 | 0 | 0 |", text)                         # severity counts for changed code only
        self.assertIn("| F001 | high | security | `app.py:5` | SQL injection | supported |", text)
        self.assertIn("## Related code (not new in this change)", text)
        self.assertIn("| F002 | low | security | `db.py:2` | Old issue | not verified |", text)
        self.assertIn("Input reaches \\| the query.", text)              # table-safe text
        self.assertIn("Skipped `big.py`: too large", text)
        self.assertIn("Removed finding 'Unseen'", text)

    def test_markdown_report_shows_failure(self):
        report = {"status": "incomplete", "error": "OpenAI agent API error 429: quota", "findings": [],
                  "changes": self.CHANGES}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.md"
            write_markdown(report, "PR #4", path)
            self.assertIn("Status: **INCOMPLETE: OpenAI agent API error 429: quota**", path.read_text())


class GitHubScriptTests(unittest.TestCase):
    def event(self, fork=False):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        path = directory / "event.json"
        head = "someone/fork" if fork else "owner/repo"
        path.write_text(json.dumps({"pull_request": {"number": 12, "base": {"sha": "abc123", "repo": {
            "full_name": "owner/repo"}}, "head": {"repo": {"full_name": head}}}}))
        return directory, path

    def run_script(self, environment, fork=False, returncode=0, write_report=True, timeout=False):
        directory, event = self.event(fork)
        summary = directory / "summary.md"
        environment = dict(environment, GITHUB_EVENT_PATH=str(event), GITHUB_STEP_SUMMARY=str(summary))
        commands = []

        def fake_run(command, **options):
            commands.append(command)
            if timeout:
                raise subprocess.TimeoutExpired(command, 1)
            if write_report:
                Path(command[command.index("--report-md") + 1]).write_text("# CodeGuardian review: PR #12\n")
            return subprocess.CompletedProcess(command, returncode, "review output\n", "")

        output = io.StringIO()
        with redirect_stdout(output):
            code = pr_review.main(["--output", str(directory / "out")], environment, fake_run)
        return code, commands, summary.read_text(), output.getvalue()

    def test_paid_review_runs_with_key_and_verification(self):
        code, commands, summary, _ = self.run_script({"AI_PROVIDER": "openai", "OPENAI_API_KEY": "sk-test-secret"})
        self.assertEqual(code, 0)
        self.assertIn("--verify", commands[0])
        self.assertNotIn("--no-llm", commands[0])
        self.assertEqual(commands[0][commands[0].index("--merge-base") + 1], "abc123")
        self.assertIn("# CodeGuardian review: PR #12", summary)

    def test_missing_api_key_skips_paid_review_clearly(self):
        code, commands, summary, output = self.run_script({"AI_PROVIDER": "openai"})
        self.assertEqual(code, 0)
        self.assertIn("--no-llm", commands[0])
        self.assertIn("Paid AI review SKIPPED", summary)
        self.assertIn("::warning::", output)
        self.assertIn("OPENAI_API_KEY", summary)

    def test_fork_pr_never_uses_the_key(self):
        code, commands, summary, output = self.run_script({"OPENAI_API_KEY": "sk-test-secret"}, fork=True)
        self.assertEqual(code, 0)
        self.assertIn("--no-llm", commands[0])
        self.assertIn("fork", summary)
        self.assertNotIn("sk-test-secret", summary + output + " ".join(commands[0]))

    def test_verification_can_be_turned_off(self):
        _, commands, _, _ = self.run_script({"OPENAI_API_KEY": "k", "CODEGUARDIAN_VERIFY": "false"})
        self.assertNotIn("--verify", commands[0])

    def test_failed_review_fails_the_job(self):
        code, _, summary, output = self.run_script({"OPENAI_API_KEY": "k"}, returncode=1)
        self.assertEqual(code, 1)
        self.assertIn("::error::", output)
        code, _, summary, _ = self.run_script({"OPENAI_API_KEY": "k"}, returncode=1, write_report=False)
        self.assertEqual(code, 1)
        self.assertIn("FAILED before a report was written", summary)
        code, _, summary, output = self.run_script({"OPENAI_API_KEY": "k"}, timeout=True)
        self.assertEqual(code, 1)
        self.assertIn("timed out", output)

    def test_workflow_settings_are_safe(self):
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "codeguardian.yml").read_text())
        triggers = workflow.get("on") or workflow.get(True)   # YAML reads the key "on" as True
        self.assertEqual(set(triggers), {"pull_request"})
        self.assertEqual(triggers["pull_request"]["types"], ["opened", "synchronize", "reopened"])
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        job = workflow["jobs"]["review"]
        self.assertEqual(job["timeout-minutes"], 15)
        checkout = job["steps"][0]["with"]
        self.assertEqual((checkout["fetch-depth"], checkout["persist-credentials"]), (0, False))
        self.assertIn("head.sha", checkout["ref"])
        self.assertEqual(job["steps"][-1]["if"], "always()")


class BackwardCompatibilityTests(unittest.TestCase):
    def test_new_options_are_checked(self):
        for argv in (["app.py", "--merge-base", "main"], [".", "--mode", "diff", "--base", "HEAD", "--merge-base", "main"],
                     [".", "--mode", "diff", "--no-llm", "--verify"], ["app.py", "--report-md", "r.md"]):
            with self.subTest(argv=argv), patch.object(sys, "argv", ["reviewer.py", *argv]), \
                 redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                reviewer.main()
            self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
