"""v1.0: run CodeGuardian on a GitHub pull request. This is the only file that knows about GitHub.

The workflow calls it after checking out the PR head. It decides whether a paid AI review is
allowed, runs the normal reviewer CLI, and writes the report to the job summary.

    python .github/scripts/pr_review.py --output codeguardian-report
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REVIEWER = ROOT / "src" / "codeguardian" / "reviewer.py"
TIMEOUT_SECONDS = 12 * 60   # the workflow itself stops after 15 minutes


def read_pull_request(event_path):
    """The PR number, base commit, and whether it comes from a fork, from GitHub's event file."""
    pull_request = json.loads(Path(event_path).read_text())["pull_request"]
    return {"number": pull_request["number"], "base_sha": pull_request["base"]["sha"],
            "fork": pull_request["head"]["repo"]["full_name"] != pull_request["base"]["repo"]["full_name"]}


def paid_review_skip_reason(pull_request, environment):
    """Return why the paid AI review must be skipped, or None if it may run."""
    if pull_request["fork"]:
        # GitHub does not give secrets to fork PRs; we also refuse on purpose.
        return "this pull request comes from a fork, so API keys are not used"
    provider = environment.get("AI_PROVIDER", "openai").strip().lower() or "openai"
    if not environment.get(f"{provider.upper()}_API_KEY", "").strip():
        return f"the {provider.upper()}_API_KEY secret is not set for this repository"
    return None


def build_command(pull_request, output, skip_reason, verify):
    command = [sys.executable, str(REVIEWER), str(ROOT), "--mode", "diff", "--merge-base", pull_request["base_sha"],
               "--trace-file", str(output / "report.json"), "--report-md", str(output / "report.md"),
               "--title", f"PR #{pull_request['number']}"]
    if skip_reason:
        command.append("--no-llm")
    elif verify:
        command.append("--verify")
    return command


def write_summary(environment, text):
    """Add text to the GitHub job summary (or print it when run outside GitHub)."""
    summary = environment.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as file:
            file.write(text + "\n")
    else:
        print(text)


def main(arguments=None, environment=None, run=subprocess.run):
    parser = argparse.ArgumentParser(description="Review a GitHub pull request with CodeGuardian.")
    parser.add_argument("--output", type=Path, default=Path("codeguardian-report"))
    args = parser.parse_args(arguments)
    environment = dict(os.environ if environment is None else environment)
    args.output.mkdir(parents=True, exist_ok=True)
    for old in ("report.json", "report.md"):
        (args.output / old).unlink(missing_ok=True)   # never show a report from an earlier run

    pull_request = read_pull_request(environment["GITHUB_EVENT_PATH"])
    skip_reason = paid_review_skip_reason(pull_request, environment)
    verify = environment.get("CODEGUARDIAN_VERIFY", "true").strip().lower() != "false"
    if skip_reason:
        # "::warning::" shows a yellow warning on the workflow run page.
        print(f"::warning::CodeGuardian paid AI review SKIPPED: {skip_reason}. Running Bandit and Ruff only.")
        write_summary(environment, f"> **Paid AI review SKIPPED:** {skip_reason}. "
                                   "Only Bandit and Ruff were run, on changed lines.\n")
    try:
        result = run(build_command(pull_request, args.output, skip_reason, verify), cwd=ROOT,
                     capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
        print(result.stdout + result.stderr)   # error messages already have API keys removed
        exit_code = result.returncode
    except subprocess.TimeoutExpired:
        exit_code, result = 1, None
        print(f"::error::CodeGuardian review timed out after {TIMEOUT_SECONDS // 60} minutes.")

    report = args.output / "report.md"
    if report.exists():
        write_summary(environment, report.read_text(encoding="utf-8"))
    else:
        write_summary(environment, f"# CodeGuardian review: PR #{pull_request['number']}\n\n"
                                   "**The review FAILED before a report was written.** See the job log.")
    if exit_code != 0:
        print("::error::CodeGuardian review did not complete. See the job summary and log.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
