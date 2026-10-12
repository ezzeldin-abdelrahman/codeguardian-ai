"""Run local analysis tools and return CodeGuardian finding dictionaries."""

import json
import os
import tempfile
import subprocess
import sys
from pathlib import Path

if __package__:
    from . import project as project_access
else:
    import project as project_access


def inspect_code(file_path):
    """Read the selected file without executing it and preserve line numbers."""
    try:
        contents = Path(file_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise RuntimeError(f"Could not inspect source: {error}") from None
    return "\n".join(f"{number}: {line}" for number, line in enumerate(contents.splitlines(), 1))


def run_bandit(file_path, *, project=None, isolated=False):
    """Return security findings; raise RuntimeError if Bandit cannot scan the file."""
    if project is not None:
        project_access.claim_file(project, file_path)
        contents = project_access.read_project_file(project, file_path)
        with tempfile.TemporaryDirectory(prefix="codeguardian-") as directory:
            snapshot = Path(directory) / "source.py"
            snapshot.write_text(contents, encoding="utf-8")
            findings = run_bandit(snapshot, isolated=True)
        for finding in findings:
            finding["file"] = file_path
        return project_access.bound_observation(
            {"ok": True, "path": file_path, "findings": findings, "truncated": False})
    path = Path(file_path).resolve()
    if not path.is_file():
        raise RuntimeError(f"Bandit requires an existing source file: {path}")
    command = [sys.executable] + (["-I"] if isolated else []) + ["-m", "bandit", "-f", "json"]
    if isolated:
        command += ["--ini", os.devnull]
    command += ["--", str(path)]
    options = {"cwd": str(path.parent), "env": {"PATH": os.defpath, "HOME": str(path.parent)}} if isolated else {}
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, **options)
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


def run_ruff(file_path, *, project=None, isolated=False):
    """Return lint findings. Ruff findings use our initial quality/low mapping."""
    if project is not None:
        project_access.claim_file(project, file_path)
        contents = project_access.read_project_file(project, file_path)
        with tempfile.TemporaryDirectory(prefix="codeguardian-") as directory:
            snapshot = Path(directory) / "source.py"
            snapshot.write_text(contents, encoding="utf-8")
            findings = run_ruff(snapshot, isolated=True)
        for finding in findings:
            finding["file"] = file_path
        return project_access.bound_observation(
            {"ok": True, "path": file_path, "findings": findings, "truncated": False})
    path = Path(file_path).resolve()
    if not path.is_file():
        raise RuntimeError(f"Ruff requires an existing source file: {path}")
    command = [sys.executable] + (["-I"] if isolated else []) + [
        "-m", "ruff", "check", "--no-fix", "--no-fix-only", "--no-cache", "--output-format", "json"]
    if isolated:
        command += ["--isolated"]
    command += ["--", str(path)]
    options = {"cwd": str(path.parent), "env": {"PATH": os.defpath, "HOME": str(path.parent)}} if isolated else {}
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, **options)
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


def list_files(project):
    """List the bounded eligibility set; exclusions also apply to direct requests."""
    return project_access.bound_observation({"ok": True, "files": project["files"].copy(),
            "truncated": bool(project["limits_reached"]),
            "limits": project["limits_reached"].copy()})


def inspect_file(project, path, start_line=1, end_line=None):
    """Inspect an inclusive line range; return original 1-based line numbers."""
    if type(start_line) is not int or start_line < 1:
        raise ValueError("start_line must be a positive integer.")
    if end_line is not None and (type(end_line) is not int or end_line < start_line):
        raise ValueError("end_line must be an integer at least start_line.")
    project_access.claim_file(project, path)
    lines = project_access.read_project_file(project, path).splitlines()
    if start_line > len(lines) and not (not lines and start_line == 1):
        raise ValueError("start_line is beyond the end of this file.")
    requested_end = len(lines) if end_line is None else end_line
    if end_line is not None and end_line > len(lines):
        raise ValueError("end_line is beyond the end of this file.")
    stop = min(requested_end, start_line + project_access.MAX_INSPECT_LINES - 1)
    return project_access.bound_observation({"ok": True, "path": path, "total_lines": len(lines),
            "lines": [{"line": number, "text": lines[number - 1]}
                      for number in range(start_line, stop + 1)],
            "truncated": stop < requested_end})


def search_code(project, query):
    """Literal, case-sensitive search. Only bounded excerpts leave this function."""
    if not isinstance(query, str) or not query.strip() or len(query) > 200:
        raise ValueError("query must be nonempty text of at most 200 characters.")
    remaining = project_access.MAX_TOTAL_SEARCH_RESULTS - project["search_results"]
    if remaining <= 0:
        raise ValueError("Total search-result limit reached.")
    limit = min(project_access.MAX_SEARCH_RESULTS, remaining)
    matches = []
    errors = []
    for path in project["files"]:
        try:
            lines = project_access.read_project_file(project, path).splitlines()
        except (ValueError, RuntimeError) as error:
            errors.append({"file": path, "error": str(error)})
            continue
        for number, line in enumerate(lines, 1):
            index = line.find(query)
            if index < 0:
                continue
            if len(matches) == limit:
                project["search_results"] += len(matches)
                return project_access.bound_observation(
                    {"ok": True, "matches": matches, "errors": errors, "truncated": True})
            start = max(0, index - 80)
            excerpt = line[start:start + 300]
            matches.append({"file": path, "line": number, "text": excerpt,
                            "excerpt_truncated": start > 0 or start + 300 < len(line)})
    project["search_results"] += len(matches)
    return project_access.bound_observation({"ok": True, "matches": matches, "errors": errors,
            "truncated": bool(project["limits_reached"]) or bool(errors)})
