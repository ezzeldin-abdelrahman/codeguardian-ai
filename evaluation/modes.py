"""v0.9: the four review modes compared by the benchmark. Each one only calls existing CodeGuardian code.

    static_only       Bandit + Ruff, no LLM.
    llm_only          one review_code() call per case (single-file cases only).
    agentic           run_agent(), the normal tool-using agent.
    agentic_verified  the same agent findings, then verify_findings().

agentic and agentic_verified share ONE agent run, so any difference between them
comes from verification alone, and the agent is paid for once.
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
from codeguardian.agent import run_agent  # noqa: E402
from codeguardian.findings import parse_findings  # noqa: E402
from codeguardian.git_changes import collect_changes  # noqa: E402
from codeguardian.project import create_project  # noqa: E402
from codeguardian.providers import USAGE_FIELDS, add_usage, review_code  # noqa: E402
from codeguardian.tools import run_bandit, run_ruff  # noqa: E402
from codeguardian.verifier import verify_findings  # noqa: E402

MODES = ("static_only", "llm_only", "agentic", "agentic_verified")
LLM_MODES = ("llm_only", "agentic", "agentic_verified")


def case_info(case_id):
    folder = HERE / "cases" / case_id
    diff = (folder / "before").is_dir()
    files = sorted(path.name for path in (folder / "after" if diff else folder).glob("*.py"))
    return {"id": case_id, "folder": folder, "diff": diff, "files": files}


def unsupported_reason(mode, case):
    """Return why a mode cannot review a case fairly, or None if it can."""
    if mode == "static_only" and case["diff"]:
        return "Bandit and Ruff cannot tell which code changed in a Git-diff case."
    if mode == "llm_only" and case["diff"]:
        return "A single LLM call receives one file, not a Git diff."
    if mode == "llm_only" and len(case["files"]) > 1:
        return "A single LLM call receives one file; this case needs several."
    return None


def new_result(status="complete"):
    return {"status": status, "error": None, "findings": [], "seconds": None,
            "usage": {"llm_calls": 0, "tool_calls": 0, **dict.fromkeys(USAGE_FIELDS, 0)}}


def run_static(case):
    result = new_result()
    started = time.perf_counter()
    for name in case["files"]:
        for finding in run_bandit(case["folder"] / name) + run_ruff(case["folder"] / name):
            result["findings"].append(dict(finding, file=name))
        result["usage"]["tool_calls"] += 2
    result["seconds"] = time.perf_counter() - started
    return result


def run_llm_only(case, provider, model, api_key):
    result = new_result()
    name = case["files"][0]
    usage = {}
    started = time.perf_counter()
    try:
        text = review_code((case["folder"] / name).read_text(encoding="utf-8"), provider, model, api_key, usage=usage)
        result["findings"] = [dict(finding, file=name) for finding in parse_findings(text)]
    except (ValueError, RuntimeError) as error:
        result.update(status="failed", error=str(error))
    result["seconds"] = time.perf_counter() - started
    # One call was attempted; tokens stay unavailable if it failed before a response arrived.
    result["usage"].update(llm_calls=1, **{key: usage.get(key) for key in USAGE_FIELDS})
    return result


def git(repository, *arguments):
    command = ["git", "-C", str(repository), "-c", "user.name=Eval", "-c", "user.email=eval@example.invalid"]
    subprocess.run([*command, *arguments], capture_output=True, check=True)


def build_diff_repository(case, target):
    """Commit before/ then after/, so the diff review sees exactly the case's change."""
    git(target, "init", "-q")
    for stage in ("before", "after"):
        for file in (case["folder"] / stage).glob("*.py"):
            (target / file.name).write_text(file.read_text())
        git(target, "add", "-A")
        git(target, "commit", "-qm", stage)


def run_agentic(case, provider, model, api_key, verify):
    """Return {"agentic": ..., "agentic_verified": ...} from one shared agent run."""
    with tempfile.TemporaryDirectory(prefix="codeguardian-bench-") as temporary:
        changes = None
        started = time.perf_counter()
        try:
            if case["diff"]:
                build_diff_repository(case, Path(temporary))
                project, changes = collect_changes(Path(temporary))
                target = Path(temporary)
            else:
                target = case["folder"]
                project = create_project(target)
            report = run_agent(target, model, api_key, change_context=changes, project=project, provider=provider)
        except (ValueError, RuntimeError, OSError) as error:
            report = {"status": "failed", "error": str(error), "findings": [], "trace": [], "usage": None}
        agent_seconds = time.perf_counter() - started

        agentic = new_result("complete" if report["status"] == "complete" else "failed")
        agentic.update(error=report.get("error"), findings=report["findings"], seconds=agent_seconds)
        if report.get("usage"):
            agentic["usage"] = dict(report["usage"])
        results = {"agentic": agentic}
        if not verify:
            return results

        verified = {**agentic, "usage": dict(agentic["usage"])}
        if agentic["status"] == "complete" and agentic["findings"]:
            started = time.perf_counter()
            verification = verify_findings(agentic["findings"], target, model, api_key, provider=provider,
                                           project=project, change_context=changes)
            verified["seconds"] = agent_seconds + time.perf_counter() - started
            verified["verification"] = verification
            # Agent and verification tokens are added once each, so nothing is counted twice.
            verified["usage"]["llm_calls"] += verification["llm_calls"]
            add_usage(verified["usage"], verification)
        results["agentic_verified"] = verified
        return results


def run_modes(case, modes, provider, model, api_key):
    """Run the requested modes on one case and return {mode: result}."""
    results = {}
    for mode in modes:
        reason = unsupported_reason(mode, case)
        if reason:
            results[mode] = dict(new_result("unsupported"), error=reason)
    try:
        if "static_only" in modes and "static_only" not in results:
            results["static_only"] = run_static(case)
    except RuntimeError as error:
        results["static_only"] = dict(new_result("failed"), error=str(error))
    if "llm_only" in modes and "llm_only" not in results:
        results["llm_only"] = run_llm_only(case, provider, model, api_key)
    if "agentic" in modes or "agentic_verified" in modes:
        shared = run_agentic(case, provider, model, api_key, verify="agentic_verified" in modes)
        results.update({mode: shared[mode] for mode in shared if mode in modes})
    return results
