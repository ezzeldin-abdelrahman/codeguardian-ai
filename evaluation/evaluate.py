"""v0.8-v0.9: benchmark CodeGuardian's quality, speed, and cost on hand-labeled cases.

    python evaluation/evaluate.py --smoke                     # 2 cases, all modes: a cheap first check
    python evaluation/evaluate.py --modes all --models openai:gpt-6-luna,groq:openai/gpt-oss-120b \\
                                  --repeats 2 --budget 1.00
    python evaluation/evaluate.py --modes static_only         # free: no LLM calls
    python evaluation/evaluate.py --score-only                # rebuild the latest report, no API calls

Without --modes, it runs agentic and agentic_verified (the v0.8 before/after comparison).
The files work together like this:
    modes.py    runs one review mode on one case (only existing CodeGuardian code)
    scoring.py  matches findings to labels and calculates metrics and cost
    report.py   writes summary.md
Case code is only read and scanned, never executed.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import modes as mode_runner  # also makes the codeguardian package importable
from codeguardian import agent, project, verifier
from codeguardian.providers import get_settings
from report import write_report
from scoring import LINE_TOLERANCE, aggregate, load_pricing, load_rows, review_cost

HERE = Path(__file__).resolve().parent
# Used by --budget before the first real cost is known. Deliberately on the high side.
FIRST_CASE_ESTIMATE = 0.05
SMOKE_CASES = ["sql_vulnerable", "sql_safe"]


class Budget:
    """Approximate spending limit: it stops before a case whose expected cost would exceed the limit.

    A case's cost is only known after it runs, so one case can still go over the limit.
    """

    def __init__(self, limit):
        self.limit, self.spent, self.cases = limit, 0.0, 0

    def allows_next_case(self):
        if self.limit is None:
            return True
        expected = self.spent / self.cases if self.cases else FIRST_CASE_ESTIMATE
        return self.spent + expected <= self.limit

    def record(self, cost):
        if cost is not None:
            self.spent += cost
            self.cases += 1


def model_slug(model_key):
    return model_key.replace(":", "__").replace("/", "_")


def git_commit():
    def git(*arguments):
        return subprocess.run(["git", "-C", str(HERE.parent), *arguments], capture_output=True, text=True).stdout.strip()
    return git("rev-parse", "--short", "HEAD") + (" (with uncommitted changes)" if git("status", "--porcelain") else "")


def dataset_version():
    """A short hash of the labels and every case file, so results can be tied to the exact dataset."""
    digest = hashlib.sha256((HERE / "labels.json").read_bytes())
    for path in sorted((HERE / "cases").rglob("*.py")):
        digest.update(str(path.relative_to(HERE)).encode() + path.read_bytes())
    return digest.hexdigest()[:12]


def run_benchmark(case_ids, modes, model_keys, repeats, run_directory, budget, pause, settings_for, prices):
    """Run every (model, repeat, case) and save one raw JSON file per mode result."""
    for model_index, model_key in enumerate(model_keys):
        provider, model, api_key = settings_for(model_key)
        for repeat in range(1, repeats + 1):
            for case_id in case_ids:
                case = mode_runner.case_info(case_id)
                # Bandit/Ruff do not depend on the model, so static_only runs once per repeat.
                selected = [m for m in modes if m != "static_only" or model_index == 0]
                needs_llm = [m for m in selected if m in mode_runner.LLM_MODES
                             and not mode_runner.unsupported_reason(m, case)]
                skipped = needs_llm and not budget.allows_next_case()
                print(f"[{model_key} run {repeat}] {case_id}" + (" (LLM modes skipped: budget)" if skipped else ""),
                      flush=True)
                to_run = [m for m in selected if not (skipped and m in needs_llm)]
                results = mode_runner.run_modes(case, to_run, provider, model, api_key)
                for mode in needs_llm if skipped else []:
                    results[mode] = dict(mode_runner.new_result("skipped"), error="Budget limit reached.")
                if not skipped and needs_llm:
                    # The verified result already includes the agent's usage, so count the larger one only.
                    agent_result = results.get("agentic_verified") or results.get("agentic")
                    spent = [review_cost(r["usage"], prices.get(model_key))[0]
                             for r in (results.get("llm_only"), agent_result) if r]
                    budget.record(None if None in spent else sum(spent))
                for mode, result in results.items():
                    owner = "none" if mode == "static_only" else model_key
                    folder = run_directory / "raw" / model_slug(owner) / mode
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / f"{case_id}-run{repeat}.json").write_text(json.dumps(
                        {"model": owner, "mode": mode, "case": case_id, "repeat": repeat, "result": result},
                        indent=2, default=str))
                if pause and needs_llm and not skipped:
                    time.sleep(pause)


def parse_arguments():
    parser = argparse.ArgumentParser(description="Benchmark CodeGuardian on the labeled cases.")
    parser.add_argument("--modes", help="Comma-separated: " + ", ".join(mode_runner.MODES) + ", or all "
                        "(default: agentic,agentic_verified)")
    parser.add_argument("--models", help="Comma-separated provider:model, e.g. openai:gpt-6-luna "
                        "(default: the provider and model in .env)")
    parser.add_argument("--cases", help="Comma-separated case names (default: all)")
    parser.add_argument("--repeats", type=int, default=1, help="Run every case this many times")
    parser.add_argument("--smoke", action="store_true", help="Quick check: 2 cases, all modes, 1 repeat")
    parser.add_argument("--budget", type=float, help="Approximate maximum estimated spend in USD")
    parser.add_argument("--pause", type=float, default=0, help="Seconds to wait between cases (for free-tier rate limits)")
    parser.add_argument("--score-only", nargs="?", const="latest", metavar="RUN",
                        help="Rebuild the report of a saved run (default: the latest) without API calls")
    parser.add_argument("--results", type=Path, default=HERE / "results", help="Folder for benchmark runs")
    return parser, parser.parse_args()


def saved_run(parser, results, name):
    """The folder of a saved run: the newest one, or the one the user named."""
    runs = sorted(path for path in results.glob("*") if (path / "run_info.json").exists())
    if not runs:
        parser.error(f"No saved runs in {results}")
    return runs[-1] if name == "latest" else results / name


def choose_plan(parser, args, labels, prices):
    """Decide which modes, cases, repeats, and models to run, and check them before any API call."""
    if args.modes == "all" or (args.smoke and not args.modes):
        modes = list(mode_runner.MODES)
    else:
        modes = (args.modes or "agentic,agentic_verified").split(",")
    case_ids = SMOKE_CASES if args.smoke else (args.cases.split(",") if args.cases else list(labels))
    repeats = 1 if args.smoke else args.repeats
    unknown = [m for m in modes if m not in mode_runner.MODES] + [c for c in case_ids if c not in labels]
    if unknown:
        parser.error(f"Unknown mode or case: {', '.join(unknown)}")
    if repeats < 1:
        parser.error("--repeats must be at least 1")

    uses_llm = any(m in mode_runner.LLM_MODES for m in modes)
    if not uses_llm:
        return modes, case_ids, repeats, ["none"]
    try:
        if args.models:
            model_keys = args.models.split(",")
        else:
            provider, model, _ = get_settings()
            model_keys = [f"{provider}:{model}"]
        for key in model_keys:
            get_settings(*key.split(":", 1))   # checks that the key and model are configured
    except ValueError as error:
        parser.error(str(error))
    if args.budget is not None:
        test_usage = {"llm_calls": 1, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
        missing = [key for key in model_keys if review_cost(test_usage, prices.get(key))[0] is None]
        if missing:
            parser.error(f"--budget needs prices in evaluation/pricing.json for: {', '.join(missing)}")
    return modes, case_ids, repeats, model_keys


def make_run_info(args, modes, case_ids, repeats, model_keys, pricing):
    """Everything needed to reproduce this run later."""
    return {"date": datetime.now().isoformat(timespec="seconds"), "commit": git_commit(),
            "dataset_version": dataset_version(), "models": model_keys, "modes": modes,
            "cases": case_ids, "repeats": repeats, "budget": args.budget, "smoke": args.smoke,
            "pricing": {key: pricing[key] for key in ("currency", "unit", "pricing_date")},
            "settings": {"temperature": "provider default (not set by CodeGuardian)",
                         "openai_reasoning_effort": os.getenv("OPENAI_REASONING_EFFORT") or "model default",
                         "agent_max_steps_single_file": agent.MAX_STEPS,
                         "agent_max_steps_project": project.MAX_PROJECT_STEPS,
                         "max_verified_findings": verifier.MAX_VERIFIED_FINDINGS,
                         "line_tolerance": LINE_TOLERANCE, "python": sys.version.split()[0]}}


def settings_for(model_key):
    return (None, None, None) if model_key == "none" else get_settings(*model_key.split(":", 1))


def main():
    parser, args = parse_arguments()
    labels = json.loads((HERE / "labels.json").read_text())["cases"]
    pricing, prices = load_pricing(HERE / "pricing.json")

    if args.score_only:
        run_directory = saved_run(parser, args.results, args.score_only)
        run_info = json.loads((run_directory / "run_info.json").read_text())
        run_info["pricing"] = {key: pricing[key] for key in ("currency", "unit", "pricing_date")}
    else:
        load_dotenv(HERE.parent / ".env")
        modes, case_ids, repeats, model_keys = choose_plan(parser, args, labels, prices)
        run_directory = args.results / datetime.now().strftime("%Y%m%d-%H%M%S")
        run_directory.mkdir(parents=True)
        run_info = make_run_info(args, modes, case_ids, repeats, model_keys, pricing)
        (run_directory / "run_info.json").write_text(json.dumps(run_info, indent=2))
        run_benchmark(case_ids, modes, model_keys, repeats, run_directory, Budget(args.budget), args.pause,
                      settings_for, prices)

    summaries = aggregate(load_rows(run_directory, labels), prices)
    (run_directory / "results.json").write_text(json.dumps({"run": run_info, "summaries": summaries}, indent=2))
    write_report(summaries, run_info, run_directory / "summary.md")
    print((run_directory / "summary.md").read_text())
    print(f"Saved in {run_directory}")


if __name__ == "__main__":
    main()
