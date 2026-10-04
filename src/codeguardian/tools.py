"""Run local analysis tools and return CodeGuardian finding dictionaries."""

import json
import subprocess
import sys
from pathlib import Path


def inspect_code(file_path):
    """Read the selected file without executing it and preserve line numbers."""
    try:
        contents = Path(file_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise RuntimeError(f"Could not inspect source: {error}") from None
    return "\n".join(f"{number}: {line}" for number, line in enumerate(contents.splitlines(), 1))


def run_bandit(file_path):
    """Return security findings; raise RuntimeError if Bandit cannot scan the file."""
    path = Path(file_path).resolve()
    if not path.is_file():
        raise RuntimeError(f"Bandit requires an existing source file: {path}")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "bandit", "-f", "json", "--", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Bandit timed out after 60 seconds.") from None
    except OSError as error:
        raise RuntimeError(f"Could not start Bandit: {error}") from None

    if "No module named bandit" in result.stderr:
        raise RuntimeError("Bandit is not installed in this Python environment. Run: python -m pip install bandit")
    # Exit 1 can mean findings exist, so do not use check=True.
    if result.returncode not in (0, 1):
        raise RuntimeError(f"Bandit failed (exit {result.returncode}): {result.stderr.strip()}")
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("Bandit did not return valid JSON. " + result.stderr.strip()) from None
    if not isinstance(report, dict) or not isinstance(report.get("results"), list):
        raise RuntimeError("Bandit returned an unexpected JSON structure.")
    # Bandit can report skipped/unreadable files or syntax errors in its JSON.
    if report.get("errors"):
        raise RuntimeError(f"Bandit could not complete the scan: {report['errors']}")

    findings = []
    try:
        for issue in report["results"]:
            severity = issue["issue_severity"].lower()
            if severity not in ("low", "medium", "high"):
                raise ValueError("Unknown Bandit severity")
            recommendation = "Review this security issue and replace the unsafe pattern."
            if issue.get("more_info"):
                recommendation += f" See: {issue['more_info']}"
            findings.append({
                "source": "bandit",
                "category": "security",
                "severity": severity,
                "title": f"{issue['test_id']}: {issue['test_name']}",
                "line": issue.get("line_number"),
                "description": issue["issue_text"],
                "recommendation": recommendation,
            })
    except (KeyError, TypeError, AttributeError, ValueError):
        raise RuntimeError("Bandit returned an unexpected finding structure.") from None
    return findings


def run_ruff(file_path):
    """Return lint findings. Ruff findings use our initial quality/low mapping."""
    path = Path(file_path).resolve()
    if not path.is_file():
        raise RuntimeError(f"Ruff requires an existing source file: {path}")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--no-fix", "--no-fix-only",
             "--no-cache", "--output-format", "json", "--", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Ruff timed out after 60 seconds.") from None
    except OSError as error:
        raise RuntimeError(f"Could not start Ruff: {error}") from None

    if "No module named ruff" in result.stderr:
        raise RuntimeError("Ruff is not installed in this Python environment. Run: python -m pip install ruff")
    if result.returncode not in (0, 1):
        raise RuntimeError(f"Ruff failed (exit {result.returncode}): {result.stderr.strip()}")
    try:
        issues = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("Ruff did not return valid JSON. " + result.stderr.strip()) from None
    if not isinstance(issues, list):
        raise RuntimeError("Ruff returned an unexpected JSON structure.")

    findings = []
    try:
        for issue in issues:
            # E902 indicates a file-reading failure, not a code-quality finding.
            if issue.get("code") == "E902":
                raise RuntimeError(f"Ruff could not read the file: {issue['message']}")
            fix = issue.get("fix") or {}
            findings.append({
                "source": "ruff",
                "category": "quality",
                "severity": "low",
                "title": f"{issue.get('code') or 'syntax'}: {issue['message']}",
                "line": issue["location"].get("row"),
                "description": issue["message"],
                "recommendation": fix.get("message") or "Review this diagnostic and update the code accordingly.",
            })
    except (KeyError, TypeError, AttributeError):
        raise RuntimeError("Ruff returned an unexpected finding structure.") from None
    return findings
