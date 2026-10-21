"""Write summary.md: two comparison tables, the effect of verification, and what is missing."""


def percent(value):
    return "n/a" if value is None else f"{value:.0%}"


def seconds(value):
    return "n/a" if value is None else f"{value:.1f}"


def money(value, kind=""):
    if value is None:
        return "unknown"
    return f"${value:.4f}" + (" (upper bound)" if kind == "upper bound" else "")


def tokens(usage):
    if usage["llm_calls"] == 0:
        return "none (no LLM)"
    if usage["input_tokens"] is None or usage["output_tokens"] is None:
        return "unavailable"
    cached = "n/a" if usage["cached_input_tokens"] is None else usage["cached_input_tokens"]
    return f"{usage['input_tokens']} ({cached} cached) / {usage['output_tokens']}"


LIMITATIONS = [
    "- Modes see different things (rules only, one file, or tools); unsupported cases are left out, "
    "so modes are not always scored on the same cases.",
    "- 12 small cases show the method; they cannot prove one approach is better. "
    "LLM output varies, so compare F1 ranges, not single numbers.",
    "- The budget limit is approximate, and Groq's free tier is not billed.",
]


def header(run_info):
    settings, pricing = run_info["settings"], run_info["pricing"]
    return ["# CodeGuardian benchmark", "",
            f"{run_info['date']} | commit {run_info['commit']} | dataset {run_info['dataset_version']} | "
            f"{len(run_info['cases'])} cases x {run_info['repeats']} repeat(s) | temperature: {settings['temperature']} "
            f"| OpenAI reasoning effort: {settings.get('openai_reasoning_effort', 'model default')}",
            f"Prices: {pricing['currency']} {pricing['unit']}, dated {pricing['pricing_date'] or 'not set'}. "
            "Costs are estimates from token counts, not bills."]


def quality_table(summaries):
    lines = ["## Quality and reliability", "",
             "| Mode | Model | TP | FP | FN | Precision | Recall | F1 (range over repeats) | Completed | Failed |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in summaries:
        f1 = percent(s["f1"])
        if len(s["f1_by_repeat"]) > 1:
            f1 += f" ({min(s['f1_by_repeat']):.0%}-{max(s['f1_by_repeat']):.0%})"
        lines.append(f"| {s['mode']} | {s['model']} | {s['tp']} | {s['fp']} | {s['fn']} | {percent(s['precision'])} | "
                     f"{percent(s['recall'])} | {f1} | {s['completed']} ({percent(s['completion_rate'])}) | "
                     f"{s['failed']} ({s['timeouts']} timeouts) |")
    return lines


def cost_table(summaries):
    lines = ["## Speed and cost", "",
             "| Mode | Model | Seconds avg / median / p95 | LLM calls | Tool calls | Tokens in (cached) / out "
             "| Cost | Per review | Per TP |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in summaries:
        time, kind = s["latency"], s["cost_kind"]
        per_tp = money(s["cost_per_true_positive"], kind) if s["tp"] else "n/a (no TP)"
        lines.append(f"| {s['mode']} | {s['model']} | {seconds(time['average'])} / {seconds(time['median'])} / "
                     f"{seconds(time['p95'])} | {s['usage']['llm_calls']} | {s['usage']['tool_calls']} | "
                     f"{tokens(s['usage'])} | {money(s['cost'], kind)} | "
                     f"{money(s['cost_per_completed_review'], kind)} | {per_tp} |")
    return lines


def false_positive_line(summaries):
    parts = []
    for s in summaries:
        counts = ", ".join(f"{name} {count}" for name, count in sorted(s["false_positives_by_type"].items()))
        parts.append(f"{s['mode']}/{s['model']}: {counts or 'none'}")
    return ["False positives by category: " + "; ".join(parts) + "."]


def verification_section(summaries, models):
    """Compare agentic and agentic_verified, which use the same agent findings."""
    lines = ["## Effect of verification (same agent findings)", ""]
    by_key = {(s["mode"], s["model"]): s for s in summaries}
    for model in models:
        before, after = by_key.get(("agentic", model)), by_key.get(("agentic_verified", model))
        if before and after:
            extra = None if None in (after["cost"], before["cost"]) else after["cost"] - before["cost"]
            lines.append(f"- {model}: TP {before['tp']} -> {after['tp']}, FP {before['fp']} -> {after['fp']}, "
                         f"correct findings rejected {after['correct_rejected']}, extra cost {money(extra)}.")
    if len(lines) == 2:
        lines.append("Not measured in this run: it needs both agentic and agentic_verified.")
    return lines


def missing_section(summaries):
    lines = ["## Missing data and findings to check", ""]
    for s in summaries:
        name = f"{s['mode']}/{s['model']}"
        lines += [f"- {name}, {item['case']} run {item['repeat']}: {item['status']} ({item['reason']})"
                  for item in s["not_compared"]]
        if s["cost"] is None and s["usage"]["llm_calls"]:
            lines.append(f"- {name}: cost unknown (missing price or token counts).")
        lines += [f"- {name}, {item}" for item in s["to_investigate"]]
    return lines


def write_report(summaries, run_info, path):
    sections = [header(run_info), quality_table(summaries), cost_table(summaries), false_positive_line(summaries),
                verification_section(summaries, run_info["models"]), missing_section(summaries),
                ["## Limitations", ""] + LIMITATIONS,
                ["## Discussion", "", "_To be written by hand after reading the results above._"]]
    path.write_text("\n\n".join("\n".join(section) for section in sections) + "\n")
