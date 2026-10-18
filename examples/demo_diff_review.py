"""End-to-end demo of v0.6 (Git diff review) and v0.7 (evidence verification).

Builds a tiny two-commit repository in a temporary folder, runs one real review,
and prints a pass/fail checklist. It uses the API key in .env and costs about a cent.

    python examples/demo_diff_review.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REVIEWER = Path(__file__).resolve().parents[1] / "src" / "codeguardian" / "reviewer.py"

# Commit 1: a safe app.py, plus helper.py with an old bug that the diff review must ignore.
HELPER = '''def calculate():
    # Pre-existing issue: evaluates raw user input.
    return eval(input("Expression: "))
'''
APP_BEFORE = '''import sqlite3


def get_user(user_id):
    connection = sqlite3.connect("users.db")
    return connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
'''
# Commit 2: adds an SQL injection (lines 9-12) that the review should find.
APP_AFTER = APP_BEFORE + '''

def find_user(name):
    connection = sqlite3.connect("users.db")
    query = f"SELECT * FROM users WHERE name = '{name}'"
    return connection.execute(query).fetchall()
'''


def git(repository, *arguments):
    command = ["git", "-C", str(repository), "-c", "user.name=Demo", "-c", "user.email=demo@example.invalid"]
    return subprocess.run([*command, *arguments], capture_output=True, text=True, check=True).stdout.strip()


def build_repository(repository):
    """Create the two commits and return the hash of the first (the expected base)."""
    git(repository, "init", "-q")
    (repository / "helper.py").write_text(HELPER)
    (repository / "app.py").write_text(APP_BEFORE)
    git(repository, "add", "-A")
    git(repository, "commit", "-qm", "Baseline")
    base = git(repository, "rev-parse", "HEAD")
    (repository / "app.py").write_text(APP_AFTER)
    git(repository, "commit", "-qam", "Add find_user")
    return base


def check_report(report, base):
    """Return (description, passed) pairs. Checks use the trace, not the LLM's wording."""
    findings = report.get("findings", [])
    results = report.get("verification", {}).get("results", [])
    sql = [number for number, item in enumerate(findings, 1)
           if item.get("file") == "app.py" and item.get("category") == "security" and 9 <= (item.get("line") or 0) <= 12]
    confirmed = [item for item in results if item["finding_id"] in {f"F{n:03}" for n in sql}
                 and item["verification_status"] == "supported" and "bandit" in item["corroborating_tools"]]
    return [
        ("v0.6: review completed", report.get("status") == "complete"),
        ("v0.6: base is the previous commit (HEAD~1)", report.get("changes", {}).get("base") == base),
        ("v0.6: only app.py is reported as changed",
         [change["file"] for change in report.get("changes", {}).get("changes", [])] == ["app.py"]),
        ("v0.6: SQL injection in the new code is found", bool(sql)),
        ("v0.6: old bug in unchanged helper.py is not reported",
         not any(item.get("file") == "helper.py" for item in findings)),
        ("v0.7: every finding has a verification result", len(results) == len(findings) and bool(results)),
        ("v0.7: SQL finding is SUPPORTED and confirmed by Bandit", bool(confirmed)),
    ]


def run_demo():
    """Build the repository, run one real review, and return (checks, printed output)."""
    with tempfile.TemporaryDirectory(prefix="codeguardian-demo-") as directory:
        repository = Path(directory) / "demo-repo"
        repository.mkdir()
        base = build_repository(repository)
        trace = Path(directory) / "trace.json"
        process = subprocess.run([sys.executable, str(REVIEWER), str(repository), "--mode", "diff",
                                  "--verify", "--trace-file", str(trace)], capture_output=True, text=True)
        output = process.stdout + process.stderr
        report = json.loads(trace.read_text()) if trace.exists() else {}
    return check_report(report, base), output


def main():
    checks, output = run_demo()
    print(output)
    print("=== Demo checklist ===")
    for description, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {description}")
    failed = sum(not passed for _, passed in checks)
    print("All checks passed." if not failed else f"{failed} check(s) failed. LLM output varies; see above.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
