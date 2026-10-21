"""v0.9 benchmark tests: modes, usage, cost, budget, aggregation, and reports. No API calls."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluation"))
sys.path.insert(0, str(ROOT / "src"))
import evaluate
import modes
import scoring
from report import write_report
from codeguardian.providers import add_usage, read_usage

PRICE = {"input": 1.0, "cached_input": 0.1, "output": 4.0}  # USD per 1,000,000 tokens
LABEL = {"type": "sql_injection", "file": "users.py", "line": 6}
SQL = {"category": "security", "severity": "high", "title": "SQL injection", "file": "users.py", "line": 6,
       "description": "Query built from input.", "recommendation": "Use parameters."}


def usage(calls=1, tools=0, tokens_in=1000, cached=0, tokens_out=100):
    return {"llm_calls": calls, "tool_calls": tools, "input_tokens": tokens_in,
            "cached_input_tokens": cached, "output_tokens": tokens_out}


def result(findings=(), status="complete", seconds=1.0, use=None, statuses=None, error=None):
    data = {"status": status, "error": error, "findings": list(findings), "seconds": seconds,
            "usage": use or usage()}
    if statuses is not None:
        data["verification"] = {"results": [{"finding_id": f"F{n:03}", "verification_status": s}
                                            for n, s in enumerate(statuses, 1)]}
    return data


def row(mode, data, case="sql_vulnerable", repeat=1, model="openai:m", labels=(LABEL,)):
    return {"mode": mode, "model": model, "case": case, "repeat": repeat, "result": data,
            "score": scoring.score_result(case, mode, data, list(labels))}


class UsageTests(unittest.TestCase):
    def test_read_usage_for_both_openai_styles_and_missing_usage(self):
        chat = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=50, completion_tokens=5,
                                                     prompt_tokens_details=SimpleNamespace(cached_tokens=20)))
        responses = SimpleNamespace(usage=SimpleNamespace(input_tokens=40, output_tokens=4,
                                                          input_tokens_details=None))
        self.assertEqual(read_usage(chat), {"input_tokens": 50, "cached_input_tokens": 20, "output_tokens": 5})
        self.assertEqual(read_usage(responses), {"input_tokens": 40, "cached_input_tokens": None, "output_tokens": 4})
        self.assertEqual(read_usage(SimpleNamespace()), dict.fromkeys(("input_tokens", "cached_input_tokens",
                                                                      "output_tokens")))

    def test_unavailable_tokens_stay_unavailable_when_added(self):
        total = add_usage({"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
                          {"input_tokens": 10, "cached_input_tokens": None, "output_tokens": 2})
        add_usage(total, {"input_tokens": 5, "cached_input_tokens": 3, "output_tokens": 1})
        self.assertEqual(total, {"input_tokens": 15, "cached_input_tokens": None, "output_tokens": 3})


class CostTests(unittest.TestCase):
    def test_cost_uses_prices_per_million_tokens(self):
        cost, kind = scoring.review_cost(usage(tokens_in=1_000_000, tokens_out=1_000_000), PRICE)
        self.assertEqual((cost, kind), (5.0, "estimated"))

    def test_cached_tokens_use_their_own_rate_and_are_not_counted_twice(self):
        cost, _ = scoring.review_cost(usage(tokens_in=1_000_000, cached=400_000, tokens_out=0), PRICE)
        self.assertAlmostEqual(cost, 0.6 * 1.0 + 0.4 * 0.1)

    def test_missing_cached_count_or_price_gives_an_upper_bound(self):
        self.assertEqual(scoring.review_cost(usage(cached=None), PRICE)[1], "upper bound")
        no_cached_price = dict(PRICE, cached_input=None)
        cost, kind = scoring.review_cost(usage(tokens_in=1_000_000, cached=500_000, tokens_out=0), no_cached_price)
        self.assertEqual((cost, kind), (1.0, "upper bound"))

    def test_unknown_cost_is_never_zero(self):
        self.assertEqual(scoring.review_cost(usage(), None), (None, "unknown (no price)"))
        self.assertEqual(scoring.review_cost(usage(), dict(PRICE, output=None))[0], None)
        self.assertEqual(scoring.review_cost(usage(tokens_in=None), PRICE), (None, "unknown (no token counts)"))
        self.assertEqual(scoring.review_cost(usage(calls=0), None), (0.0, "no API calls"))
        self.assertEqual(scoring.combine_cost([(1.0, "estimated"), (None, "unknown")]), (None, "unknown"))
        self.assertEqual(scoring.combine_cost([(1.0, "estimated"), (0.5, "upper bound")]), (1.5, "upper bound"))


class BudgetTests(unittest.TestCase):
    def test_budget_stops_before_the_expected_cost_exceeds_the_limit(self):
        budget = evaluate.Budget(0.10)
        self.assertTrue(budget.allows_next_case())   # first case uses FIRST_CASE_ESTIMATE (0.05)
        budget.record(0.04)
        self.assertTrue(budget.allows_next_case())   # 0.04 + 0.04 <= 0.10
        budget.record(0.04)
        self.assertFalse(budget.allows_next_case())  # 0.08 + 0.04 > 0.10
        budget.record(None)                          # unknown costs are not counted as spending
        self.assertEqual(budget.cases, 2)
        self.assertTrue(evaluate.Budget(None).allows_next_case())


class ModeTests(unittest.TestCase):
    def test_unsupported_cases_per_mode(self):
        diff, multi, single = (modes.case_info(name) for name in
                               ("path_vulnerable_diff", "deserialization_vulnerable", "sql_vulnerable"))
        self.assertIsNotNone(modes.unsupported_reason("static_only", diff))
        self.assertIsNotNone(modes.unsupported_reason("llm_only", diff))
        self.assertIsNotNone(modes.unsupported_reason("llm_only", multi))
        self.assertIsNone(modes.unsupported_reason("static_only", multi))
        for mode in modes.MODES:
            self.assertIsNone(modes.unsupported_reason(mode, single))
        self.assertIsNone(modes.unsupported_reason("agentic", diff))

    def test_selected_modes_run_and_share_one_agent_run(self):
        agent_report = {"status": "complete", "error": None, "findings": [SQL], "trace": [],
                        "usage": usage(calls=3, tools=2)}
        verification = {"results": [{"finding_id": "F001", "verification_status": "supported"}],
                        "llm_calls": 1, "input_tokens": 200, "cached_input_tokens": 0, "output_tokens": 20}
        with patch.object(modes, "run_agent", return_value=agent_report) as agent, \
             patch.object(modes, "verify_findings", return_value=verification), \
             patch.object(modes, "review_code", return_value=json.dumps({"findings": []})) as single, \
             patch.object(modes, "run_bandit", return_value=[]), patch.object(modes, "run_ruff", return_value=[]):
            results = modes.run_modes(modes.case_info("sql_vulnerable"), list(modes.MODES), "openai", "m", "key")
        self.assertEqual(set(results), set(modes.MODES))
        agent.assert_called_once()
        single.assert_called_once()
        self.assertEqual(results["agentic"]["usage"]["llm_calls"], 3)
        self.assertEqual(results["agentic_verified"]["usage"]["llm_calls"], 4)
        self.assertEqual(results["agentic_verified"]["usage"]["input_tokens"], 1200)
        self.assertEqual(results["agentic"]["findings"], results["agentic_verified"]["findings"])
        self.assertEqual(results["static_only"]["usage"]["llm_calls"], 0)

    def test_only_requested_modes_run(self):
        with patch.object(modes, "run_agent") as agent, patch.object(modes, "review_code") as single:
            results = modes.run_modes(modes.case_info("path_vulnerable_diff"), ["llm_only"], "openai", "m", "key")
        self.assertEqual(results["llm_only"]["status"], "unsupported")
        agent.assert_not_called()
        single.assert_not_called()

    def test_failed_agent_call_counts_as_attempted_with_unknown_tokens(self):
        from codeguardian import agent
        with patch.object(agent, "request_agent_turn", side_effect=RuntimeError("OpenAI agent API error 400.")):
            data = modes.run_modes(modes.case_info("sql_safe"), ["agentic"], "openai", "m", "key")["agentic"]
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["usage"]["llm_calls"], 1)
        self.assertIsNone(data["usage"]["input_tokens"])
        self.assertEqual(scoring.review_cost(data["usage"], PRICE), (None, "unknown (no token counts)"))

    def test_llm_failure_is_recorded_not_clean(self):
        with patch.object(modes, "review_code", side_effect=RuntimeError("OpenAI request timed out.")):
            data = modes.run_modes(modes.case_info("sql_safe"), ["llm_only"], "openai", "m", "key")["llm_only"]
        self.assertEqual(data["status"], "failed")
        self.assertIsNone(data["usage"]["input_tokens"])


class AggregationTests(unittest.TestCase):
    def test_repeats_duplicates_failures_and_unsupported(self):
        rows = [row("agentic", result([SQL, dict(SQL, line=7)]), repeat=1),         # 1 TP + duplicate FP
                row("agentic", result([SQL], seconds=3.0), repeat=2),               # 1 TP
                row("agentic", result(status="failed", error="Groq timed out"), repeat=3),
                row("agentic", result(status="unsupported", error="n/a"), case="sql_safe", labels=())]
        summary = scoring.aggregate(rows, {"openai:m": PRICE})[0]
        self.assertEqual((summary["tp"], summary["fp"], summary["fn"]), (2, 1, 0))
        self.assertEqual(summary["f1_by_repeat"], [2 / 3, 1.0])
        self.assertEqual((summary["completed"], summary["failed"], summary["unsupported"]), (2, 1, 1))
        self.assertEqual((summary["completion_rate"], summary["timeouts"]), (2 / 3, 1))
        self.assertEqual(summary["latency"]["median"], 2.0)
        self.assertEqual(summary["usage"]["llm_calls"], 3)   # the failed review's call is counted
        self.assertEqual(summary["false_positives_by_type"], {"sql_injection": 1})

    def test_verification_rejecting_a_correct_finding(self):
        data = result([SQL], statuses=["uncertain"])
        summaries = scoring.aggregate([row("agentic", data), row("agentic_verified", data)], {})
        before, after = summaries
        self.assertEqual((before["tp"], after["tp"], after["fn"]), (1, 0, 1))
        self.assertEqual(after["correct_rejected"], 1)
        self.assertEqual(after["verification_counts"]["uncertain"], 1)
        self.assertIsNone(after["cost"])   # no price: unknown, not zero

    def test_cost_per_review_and_per_true_positive(self):
        rows = [row("llm_only", result([SQL], use=usage(tokens_in=1_000_000, tokens_out=0))),
                row("llm_only", result([], use=usage(tokens_in=1_000_000, tokens_out=0)), case="sql_safe", labels=())]
        summary = scoring.aggregate(rows, {"openai:m": PRICE})[0]
        self.assertEqual((summary["cost"], summary["cost_per_completed_review"], summary["cost_per_true_positive"]),
                         (2.0, 1.0, 2.0))
        none_found = scoring.aggregate([rows[1]], {"openai:m": PRICE})[0]
        self.assertIsNone(none_found["cost_per_true_positive"])

    def test_percentile_nearest_rank(self):
        self.assertEqual(scoring.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95), 10)
        self.assertEqual(scoring.percentile([4.0], 95), 4.0)


class RunAndReportTests(unittest.TestCase):
    def run_fake_benchmark(self, budget, model_keys=("openai:m",), tokens_in=10_000):
        calls = []

        def fake_run_modes(case, selected, provider, model, api_key):
            calls.append((case["id"], tuple(selected)))
            return {mode: result([SQL] if case["id"] == "sql_vulnerable" else [],
                                 use=usage(calls=0) if mode == "static_only" else usage(tokens_in=tokens_in))
                    for mode in selected}

        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(directory))
        with patch.object(modes, "run_modes", side_effect=fake_run_modes):
            evaluate.run_benchmark(["sql_vulnerable", "sql_safe", "command_safe"], ["static_only", "llm_only"],
                                   list(model_keys), 1, directory, budget, 0,
                                   lambda key: ("openai", "m", "key"), {key: PRICE for key in model_keys})
        return calls, directory

    def test_budget_skips_llm_modes_but_still_runs_free_static_analysis(self):
        # The first case is expected to cost $0.05 and really costs $0.0304, so a $0.06 budget allows it.
        # The second case is then expected to cost $0.0304 too: 0.0608 > 0.06, so its LLM modes are skipped.
        calls, directory = self.run_fake_benchmark(evaluate.Budget(0.06), tokens_in=30_000)
        self.assertEqual(calls[0], ("sql_vulnerable", ("static_only", "llm_only")))
        self.assertEqual(calls[1], ("sql_safe", ("static_only",)))
        skipped = json.loads((directory / "raw" / "openai__m" / "llm_only" / "sql_safe-run1.json").read_text())
        self.assertEqual(skipped["result"]["status"], "skipped")

    def test_static_analysis_runs_once_for_several_models(self):
        calls, directory = self.run_fake_benchmark(evaluate.Budget(None), model_keys=("openai:m", "groq:g"))
        static_runs = [selected for _, selected in calls if "static_only" in selected]
        self.assertEqual(len(static_runs), 3)
        self.assertTrue((directory / "raw" / "none" / "static_only").is_dir())
        self.assertTrue((directory / "raw" / "groq__g" / "llm_only").is_dir())

    def test_report_generation(self):
        _, directory = self.run_fake_benchmark(evaluate.Budget(None))
        labels = json.loads((ROOT / "evaluation" / "labels.json").read_text())["cases"]
        summaries = scoring.aggregate(scoring.load_rows(directory, labels), {"openai:m": PRICE})
        run_info = {"date": "2026-10-20", "commit": "abc", "dataset_version": "123", "models": ["openai:m"],
                    "modes": ["static_only", "llm_only"], "cases": ["sql_vulnerable", "sql_safe", "command_safe"],
                    "repeats": 1, "pricing": {"currency": "USD", "unit": "per 1,000,000 tokens",
                                              "pricing_date": "2026-10-20"},
                    "settings": {"temperature": "provider default"}}
        write_report(summaries, run_info, directory / "summary.md")
        text = (directory / "summary.md").read_text()
        self.assertIn("| llm_only | openai:m | 1 | 0 | 0 | 100% | 100% | 100% | 3 (100%) | 0 (0 timeouts) |", text)
        self.assertIn("| static_only | none |", text)
        # 3 cases x (10,000 input x $1 + 100 output x $4) per million tokens
        self.assertIn("| $0.0312 | $0.0104 |", text)
        self.assertIn("## Discussion", text)
        self.assertIn("## Limitations", text)


if __name__ == "__main__":
    unittest.main()
