"""How benchmark results are scored and priced. The same rules apply to every review mode.

Matching (v0.8): same issue type, same file, line within LINE_TOLERANCE, one-to-one.
Cost (v0.9): token counts x prices from pricing.json. Unknown is never reported as zero.
"""

import json
import math
import statistics
from collections import Counter

from modes import MODES

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
    return {"case": case_id, "status": "scored", "before": before, "after": after,
            "verification_counts": counts,
            # Correct before verification, but not kept after it.
            "correct_rejected": sorted(set(before["matched"]) - set(after["matched"]))}


def score_result(case_id, mode, result, labels):
    """Score one mode's review of one case with the v0.8 rules."""
    if result["status"] in ("unsupported", "skipped"):
        return {"status": result["status"]}
    scored = score_case(case_id, result, labels)
    if scored["status"] == "failed":
        return {"status": "failed"}
    # agentic_verified keeps only supported findings; the other modes keep every finding.
    stage = scored["after"] if mode == "agentic_verified" else scored["before"]
    return {"status": "scored", **stage,
            "correct_rejected": scored["correct_rejected"] if mode == "agentic_verified" else [],
            "verification_counts": scored["verification_counts"]}


def load_pricing(path):
    pricing = json.loads(path.read_text())
    return pricing, pricing.get("models", {})


def review_cost(usage, price):
    """Return (cost in USD or None, kind). Unknown is never reported as zero."""
    if usage["llm_calls"] == 0:
        return 0.0, "no API calls"
    if not price or price.get("input") is None or price.get("output") is None:
        return None, "unknown (no price)"
    if usage["input_tokens"] is None or usage["output_tokens"] is None:
        return None, "unknown (no token counts)"
    cached = usage["cached_input_tokens"]
    kind = "estimated"
    if cached is None or (cached and price.get("cached_input") is None):
        # Without a cached count or cached price, charge every input token at the full rate.
        cached, kind = 0, "upper bound"
    cached_rate = price.get("cached_input") or 0
    cost = ((usage["input_tokens"] - cached) * price["input"] + cached * cached_rate
            + usage["output_tokens"] * price["output"]) / 1_000_000
    return cost, kind


def combine_cost(costs):
    """Add (cost, kind) pairs. Any unknown makes the total unknown; any upper bound makes it an upper bound."""
    if not costs:
        return None, "no data"
    if any(cost is None for cost, _ in costs):
        return None, "unknown"
    kinds = {kind for _, kind in costs}
    kind = "upper bound" if "upper bound" in kinds else "estimated" if "estimated" in kinds else "no API calls"
    return sum(cost for cost, _ in costs), kind


def percentile(values, percent):
    """Nearest-rank percentile; with few runs, p95 is simply one of the slowest reviews."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percent / 100 * len(ordered)) - 1)]


def load_rows(run_directory, labels):
    rows = []
    for path in sorted((run_directory / "raw").rglob("*.json")):
        raw = json.loads(path.read_text())
        raw["score"] = score_result(raw["case"], raw["mode"], raw["result"], labels[raw["case"]])
        rows.append(raw)
    return rows


def total_counts(scores):
    return {key: sum(score[key] for score in scores) for key in ("tp", "fp", "fn")}


def f1_for_each_repeat(scored):
    """F1 of each repeat on its own, to show how much the results vary between runs."""
    values = []
    for repeat in sorted({row["repeat"] for row in scored}):
        f1 = metrics(**total_counts([row["score"] for row in scored if row["repeat"] == repeat]))["f1"]
        if f1 is not None:
            values.append(f1)
    return values


def latency(seconds):
    if not seconds:
        return {"average": None, "median": None, "p95": None, "measured_reviews": 0}
    return {"average": statistics.mean(seconds), "median": statistics.median(seconds),
            "p95": percentile(seconds, 95), "measured_reviews": len(seconds)}


def total_usage(rows):
    """Add up calls and tokens. A token count that any review did not report makes the total None."""
    usage = {"llm_calls": 0, "tool_calls": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    for row in rows:
        for key, value in row["result"]["usage"].items():
            usage[key] = None if usage[key] is None or value is None else usage[key] + value
    return usage


def things_to_check(scored):
    """False positives and missed issues, as short lines for a person to look at."""
    lines = []
    for row in scored:
        run = f"{row['case']} run {row['repeat']}"
        lines += [f"{run}: false positive {p['file']}:{p['line']} {p['type']}" for p in row["score"]["false_positives"]]
        lines += [f"{run}: missed {m['file']}:{m['line']} {m['type']}" for m in row["score"]["missed"]]
    return lines


def summarize_group(mode, model, group, prices):
    """One summary for one (mode, model): quality, speed, usage, cost, and reliability."""
    status_counts = Counter(row["score"]["status"] for row in group)
    scored = [row for row in group if row["score"]["status"] == "scored"]
    attempted = [row for row in group if row["score"]["status"] in ("scored", "failed")]
    quality = metrics(**total_counts([row["score"] for row in scored]))
    cost, cost_kind = combine_cost([review_cost(row["result"]["usage"], prices.get(model)) for row in attempted])
    errors = [row["result"]["error"] for row in group if row["score"]["status"] == "failed"]
    return {
        "mode": mode, "model": model, **quality,
        "f1_by_repeat": f1_for_each_repeat(scored),
        "false_positives_by_type": dict(Counter(p["type"] for row in scored for p in row["score"]["false_positives"])),
        "completed": status_counts["scored"], "failed": status_counts["failed"],
        "unsupported": status_counts["unsupported"], "skipped": status_counts["skipped"],
        "completion_rate": status_counts["scored"] / len(attempted) if attempted else None,
        "timeouts": sum("timed out" in (error or "") for error in errors), "errors": errors,
        "retries": 0,  # CodeGuardian makes no automatic retries.
        "latency": latency([row["result"]["seconds"] for row in scored if row["result"]["seconds"] is not None]),
        "usage": total_usage(attempted), "cost": cost, "cost_kind": cost_kind,
        "cost_per_completed_review": cost / len(scored) if cost is not None and scored else None,
        "cost_per_true_positive": cost / quality["tp"] if cost is not None and quality["tp"] else None,
        "correct_rejected": sum(len(row["score"]["correct_rejected"]) for row in scored),
        "verification_counts": {s: sum(row["score"]["verification_counts"][s] for row in scored)
                                for s in ("supported", "uncertain", "unsupported")},
        "not_compared": [{"case": row["case"], "repeat": row["repeat"], "status": row["score"]["status"],
                          "reason": row["result"]["error"]} for row in group if row["score"]["status"] != "scored"],
        "to_investigate": things_to_check(scored),
    }


def aggregate(rows, prices):
    """Group the saved reviews by (mode, model) and summarize each group, in a fixed order."""
    groups = {}
    for row in rows:
        groups.setdefault((row["mode"], row["model"]), []).append(row)
    order = {mode: index for index, mode in enumerate(MODES)}
    keys = sorted(groups, key=lambda key: (order[key[0]], key[1]))
    return [summarize_group(mode, model, groups[(mode, model)], prices) for mode, model in keys]
