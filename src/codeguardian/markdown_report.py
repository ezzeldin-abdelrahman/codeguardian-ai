"""v1.0: write a review report as Markdown (for the GitHub job summary, or to read locally)."""

SEVERITIES = ("critical", "high", "medium", "low")


def cell(text):
    """Make text safe inside a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def verification_by_finding(report):
    """{finding number: verification status}; F001 is finding 1."""
    results = report.get("verification", {}).get("results", [])
    return {int(item["finding_id"][1:]): item["verification_status"] for item in results}


def header_lines(report, title, review_type):
    changes = report.get("changes")
    lines = [f"# CodeGuardian review: {title}", ""]
    if changes:
        compared = f" (merge base with `{changes['merge_base_with']}`)" if changes.get("merge_base_with") else ""
        lines.append(f"- Commits reviewed: `{changes['base'][:10]}..{changes.get('head', 'HEAD')[:10]}`{compared}")
        lines.append(f"- Changed Python files: {changes['changed_count']} "
                     f"(skipped {changes['skipped_count']}, excluded {changes['excluded_count']})")
    status = report["status"].upper() + (f": {report['error']}" if report.get("error") else "")
    lines += [f"- Review type: {review_type}", f"- Status: **{status}**", ""]
    return lines


def severity_table(findings):
    counts = {severity: sum(f["severity"] == severity for f in findings) for severity in SEVERITIES}
    return ["| " + " | ".join(s.capitalize() for s in SEVERITIES) + " |", "| --- | --- | --- | --- |",
            "| " + " | ".join(str(counts[s]) for s in SEVERITIES) + " |", ""]


def findings_section(heading, numbered, statuses, unverified):
    lines = [f"## {heading}", ""]
    if not numbered:
        return lines + ["None.", ""]
    lines += ["| # | Severity | Category | Location | Finding | Verification |", "| --- | --- | --- | --- | --- | --- |"]
    for number, f in numbered:
        location = f"`{f.get('file') or 'project-wide'}:{f['line'] if f['line'] is not None else '-'}`"
        lines.append(f"| F{number:03} | {f['severity']} | {f['category']} | {location} | {cell(f['title'])} | "
                     f"{statuses.get(number, unverified)} |")
    lines.append("")
    for number, f in numbered:
        lines.append(f"**F{number:03}. {cell(f['title'])}** ({f.get('source', 'llm')})")
        lines.append(f"- Description: {cell(f['description'])}")
        lines.append(f"- Recommendation: {cell(f['recommendation'])}")
        if f.get("evidence"):
            lines.append(f"- Evidence: {cell(f['evidence'])}")
        lines.append("")
    return lines


def limitations_section(report):
    items = []
    changes = report.get("changes") or {}
    items += [f"Skipped `{s['file']}`: {s['reason']}" for s in changes.get("skipped", [])]
    items += [f"Removed finding '{cell(r['title'])}': {r['reason']}" for r in report.get("rejected_findings", [])]
    items += list(dict.fromkeys(changes.get("limitations", []) + report.get("coverage", {}).get("limits_reached", [])))
    items += report.get("notes", [])
    return ["## Limitations and skipped items", ""] + ([f"- {item}" for item in items] or ["None."]) + [
        "", "_AI findings are unverified suggestions. Verification lowers, but does not remove, false alarms._", ""]


def write_markdown(report, title, path, review_type="AI agent"):
    findings = report["findings"]
    statuses = verification_by_finding(report)
    numbered = list(enumerate(findings, 1))
    # Without Git changes (a full review), every finding counts as part of the reviewed code.
    in_change = [(n, f) for n, f in numbered if f.get("on_changed_line", "changes" not in report)]
    related = [(n, f) for n, f in numbered if (n, f) not in in_change]
    lines = header_lines(report, title, review_type)
    lines += ["## Findings in changed code by severity", ""] + severity_table([f for _, f in in_change])
    # "-" when verification was not run at all; "not verified" when it ran but skipped a finding.
    unverified = "not verified" if "verification" in report else "-"
    lines += findings_section("Findings in changed code", in_change, statuses, unverified)
    lines += findings_section("Related code (not new in this change)", related, statuses, unverified)
    lines += limitations_section(report)
    path.write_text("\n".join(lines), encoding="utf-8")
