"""v0.8: measure CodeGuardian's accuracy on hand-labeled cases, before and after verification.

    python evaluation/evaluate.py                       # run all 12 cases (real API calls)
    python evaluation/evaluate.py --cases sql_vulnerable,sql_safe
    python evaluation/evaluate.py --score-only          # rescore saved results, no API calls

Each case is reviewed once with --verify. "Before" scores every candidate finding;
"after" keeps only findings that verification marked supported. Both use the same
agent run, so any difference comes from verification alone. Case code is only read
and scanned, never executed.
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REVIEWER = HERE.parent / "src" / "codeguardian" / "reviewer.py"
LINE_TOLERANCE = 3

# The first matching keyword decides the issue type, so more specific types come first.
TYPE_KEYWORDS = [
    ("sql_injection", ("sql",)),
    ("unsafe_deserialization", ("pickle", "deserializ", "yaml.load")),
    ("path_traversal", ("traversal", "path injection")),
    ("command_injection", ("command injection", "os.system", "shell", "subprocess")),
    ("insecure_crypto", ("md5", "sha1", "weak hash", "password hash", "cryptograph")),
    ("hardcoded_credentials", ("hardcoded", "hard-coded", "credential", "password", "secret")),
]


def finding_type(finding):
    """Map a finding's free text to one of the benchmark issue types (or "other")."""
    text = (finding.get("title", "") + " " + finding.get("description", "")).lower()
    for issue_type, keywords in TYPE_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return issue_type
    return "other"


def match(predictions, labels):
    """One-to-one matching: same type, same file, line within LINE_TOLERANCE.

    Each label takes the closest unused prediction. Leftover predictions are false
    positives, so duplicates of one issue count only once as correct.
    """
    used = set()
    missed = []
    for label in labels:
        candidates = [(abs(p["line"] - label["line"]), index) for index, p in enumerate(predictions)
                      if index not in used and p["type"] == label["type"] and p["file"] == label["file"]
                      and p["line"] is not None and abs(p["line"] - label["line"]) <= LINE_TOLERANCE]
        if candidates:
            used.add(min(candidates)[1])
        else:
            missed.append(label)
    false_positives = [p for index, p in enumerate(predictions) if index not in used]
    return {"tp": len(used), "fp": len(false_positives), "fn": len(missed),
            "matched": sorted(predictions[i]["id"] for i in used),
            "false_positives": false_positives, "missed": missed}


def metrics(tp, fp, fn):
    """Precision and recall are None ("n/a") when their denominator is zero."""
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    if precision is None or recall is None:
        f1 = None
    else:
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def score_case(case_id, trace, labels):
    """Score one saved review. A failed or incomplete review is never counted as clean."""
    if not trace or trace.get("status") != "complete":
        return {"case": case_id, "status": "failed",
                "error": (trace or {}).get("error") or "No review result was saved."}
    statuses = {item["finding_id"]: item["verification_status"]
                for item in trace.get("verification", {}).get("results", [])}
    predictions = []
    for number, finding in enumerate(trace["findings"], 1):
        # Only security and bug findings are scored; the benchmark does not label quality or testing.
        if finding.get("category") in ("security", "bug"):
            finding_id = f"F{number:03}"
            predictions.append({"id": finding_id, "type": finding_type(finding), "file": finding.get("file"),
                                "line": finding.get("line"), "title": finding.get("title"),
                                "verification": statuses.get(finding_id, "not verified")})
    before = match(predictions, labels)
    after = match([p for p in predictions if p["verification"] == "supported"], labels)
    counts = {status: list(statuses.values()).count(status) for status in ("supported", "uncertain", "unsupported")}
    verification = trace.get("verification", {})
    return {"case": case_id, "status": "scored", "before": before, "after": after,
            "verification_counts": counts,
            # Correct before verification, but not kept after it.
            "correct_rejected": sorted(set(before["matched"]) - set(after["matched"])),
            "verification_tokens": verification.get("input_tokens", 0) + verification.get("output_tokens", 0)}


def summarize(case_results):
    scored = [r for r in case_results if r["status"] == "scored"]
    totals = {}
    for stage in ("before", "after"):
        totals[stage] = metrics(*(sum(r[stage][key] for r in scored) for key in ("tp", "fp", "fn")))
    return {"cases_scored": len(scored), "failed": [r["case"] for r in case_results if r["status"] == "failed"],
            "totals": totals,
            "correct_rejected": sum(len(r["correct_rejected"]) for r in scored),
            "verification_counts": {s: sum(r["verification_counts"][s] for r in scored)
                                    for s in ("supported", "uncertain", "unsupported")},
            "verification_tokens": sum(r["verification_tokens"] for r in scored),
            "cases": case_results}


def git(repository, *arguments):
    command = ["git", "-C", str(repository), "-c", "user.name=Eval", "-c", "user.email=eval@example.invalid"]
    subprocess.run([*command, *arguments], capture_output=True, check=True)


def copy_files(source, destination):
    for file in source.glob("*.py"):
        (destination / file.name).write_text(file.read_text())


def run_case(case_id, raw_directory):
    """Run the real reviewer CLI on one case; save its trace and printed output."""
    case = HERE / "cases" / case_id
    trace = raw_directory / f"{case_id}.json"
    trace.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="codeguardian-eval-") as temporary:
        if (case / "before").is_dir():
            # Diff case: commit before/, then commit after/, and review the last commit.
            target = Path(temporary)
            git(target, "init", "-q")
            copy_files(case / "before", target)
            git(target, "add", "-A")
            git(target, "commit", "-qm", "before")
            copy_files(case / "after", target)
            git(target, "add", "-A")
            git(target, "commit", "-qm", "after")
            options = ["--mode", "diff"]
        else:
            target, options = case, ["--agent"]
        process = subprocess.run([sys.executable, str(REVIEWER), str(target), *options, "--verify",
                                  "--trace-file", str(trace)], capture_output=True, text=True, timeout=600)
    (raw_directory / f"{case_id}.log").write_text(process.stdout + process.stderr)


def percent(value):
    return "n/a" if value is None else f"{value:.0%}"


def write_summary(summary, path):
    lines = ["# CodeGuardian evaluation", "",
             f"Cases scored: {summary['cases_scored']}. Failed: {', '.join(summary['failed']) or 'none'}.", "",
             "| Stage | TP | FP | FN | Precision | Recall | F1 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for stage, label in (("before", "Before verification"), ("after", "After verification (supported only)")):
        t = summary["totals"][stage]
        lines.append(f"| {label} | {t['tp']} | {t['fp']} | {t['fn']} | "
                     f"{percent(t['precision'])} | {percent(t['recall'])} | {percent(t['f1'])} |")
    counts = summary["verification_counts"]
    lines += ["", f"Verification: {counts['supported']} supported, {counts['uncertain']} uncertain, "
              f"{counts['unsupported']} unsupported. Correct findings rejected by verification: "
              f"{summary['correct_rejected']}. Verification tokens: {summary['verification_tokens']}.",
              "", "## Per case", "", "| Case | Before TP/FP/FN | After TP/FP/FN |", "| --- | --- | --- |"]
    for r in summary["cases"]:
        if r["status"] == "failed":
            lines.append(f"| {r['case']} | failed: {r['error']} | failed |")
        else:
            b, a = r["before"], r["after"]
            lines.append(f"| {r['case']} | {b['tp']}/{b['fp']}/{b['fn']} | {a['tp']}/{a['fp']}/{a['fn']} |")
    lines += ["", "## To investigate (before verification)", ""]
    for r in summary["cases"]:
        if r["status"] != "scored":
            continue
        for p in r["before"]["false_positives"]:
            lines.append(f"- False positive in {r['case']}: {p['file']}:{p['line']} {p['type']} - "
                         f"{p['title']} (verification: {p['verification']})")
        for label in r["before"]["missed"]:
            lines.append(f"- Missed in {r['case']}: {label['file']}:{label['line']} {label['type']}")
    path.write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Evaluate CodeGuardian on the labeled benchmark.")
    parser.add_argument("--cases", help="Comma-separated case names (default: all)")
    parser.add_argument("--score-only", action="store_true", help="Rescore saved results without API calls")
    parser.add_argument("--results", type=Path, default=HERE / "results", help="Output folder")
    args = parser.parse_args()

    labels = json.loads((HERE / "labels.json").read_text())["cases"]
    case_ids = args.cases.split(",") if args.cases else list(labels)
    unknown = [case_id for case_id in case_ids if case_id not in labels]
    if unknown:
        parser.error(f"Unknown case(s): {', '.join(unknown)}")
    raw_directory = args.results / "raw"
    raw_directory.mkdir(parents=True, exist_ok=True)

    case_results = []
    for case_id in case_ids:
        if not args.score_only:
            print(f"Reviewing {case_id} ...", flush=True)
            run_case(case_id, raw_directory)
        trace_file = raw_directory / f"{case_id}.json"
        trace = json.loads(trace_file.read_text()) if trace_file.exists() else None
        case_results.append(score_case(case_id, trace, labels[case_id]))

    summary = summarize(case_results)
    (args.results / "results.json").write_text(json.dumps(summary, indent=2))
    write_summary(summary, args.results / "summary.md")
    print((args.results / "summary.md").read_text())


if __name__ == "__main__":
    main()
