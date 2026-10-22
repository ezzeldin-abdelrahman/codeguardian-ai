"""v1.0: which findings are on lines a change (for example a pull request) actually added or edited.

A diff review may also report issues in related, unchanged code it looked at. Those are
kept, but marked on_changed_line = False, so old issues are never presented as new ones.
"""

from .tools import run_bandit, run_ruff


def added_lines(change_context):
    """{file: set of current line numbers that the change added or edited}."""
    lines = {}
    for change in change_context["changes"]:
        if change["status"] != "deleted":
            lines.setdefault(change["file"], set()).update(
                number for hunk in change["hunks"] for number in hunk["added_lines"])
    return lines


def mark_changed_lines(findings, change_context):
    """Add on_changed_line to every finding. A finding without a line cannot be on a changed line."""
    changed = added_lines(change_context)
    for finding in findings:
        finding["on_changed_line"] = finding.get("line") in changed.get(finding.get("file"), set())
    return findings


def static_review(project, change_context):
    """Free fallback without an LLM: Bandit and Ruff on changed files, keeping only changed lines."""
    findings, errors = [], []
    for path, lines in sorted(added_lines(change_context).items()):
        for analyzer in (run_bandit, run_ruff):
            try:
                results = analyzer(path, project=project)["findings"]
            except (ValueError, RuntimeError) as error:
                errors.append(f"{analyzer.__name__} on {path}: {error}")
                continue
            findings += [dict(item, file=path, on_changed_line=True) for item in results if item.get("line") in lines]
    return findings, errors
