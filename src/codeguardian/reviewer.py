"""CodeGuardian command line.

    python src/codeguardian/reviewer.py app.py                   # one-shot AI review + Bandit + Ruff
    python src/codeguardian/reviewer.py app.py --agent           # tool-using agent
    python src/codeguardian/reviewer.py . --mode diff --verify   # review the last commit, then verify
"""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

# When this file is run directly, make the codeguardian package importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codeguardian.agent import run_agent  # noqa: E402
from codeguardian.findings import parse_findings, print_findings  # noqa: E402
from codeguardian.git_changes import collect_changes  # noqa: E402
from codeguardian.project import create_project  # noqa: E402
from codeguardian.providers import get_settings, review_code  # noqa: E402
from codeguardian.tools import run_bandit, run_ruff  # noqa: E402
from codeguardian.verifier import verify_findings  # noqa: E402


def parse_arguments():
    parser = argparse.ArgumentParser(description="Review a source file with AI, Bandit, and Ruff.")
    parser.add_argument("filename", help="Source file, or project directory with --agent")
    parser.add_argument("--provider", help="Override AI_PROVIDER for this run")
    parser.add_argument("--model", help="Override the selected provider's default model")
    parser.add_argument("--agent", action="store_true", help="Use the bounded OpenAI/Groq agent")
    parser.add_argument("--mode", choices=("full", "diff"), default="full", help="Review full source or Git changes")
    parser.add_argument("--base", help="Base commit for --mode diff (default: HEAD~1, the last commit)")
    parser.add_argument("--trace-file", type=Path, help="Save agent actions and observations as JSON")
    parser.add_argument("--verify", action="store_true",
                        help="Check each agent finding against source evidence (extra API calls)")
    args = parser.parse_args()
    if args.base is not None and args.mode != "diff":
        parser.error("--base requires --mode diff")
    if args.mode == "diff":
        args.agent = True
    if args.trace_file and not args.agent:
        parser.error("--trace-file requires --agent")
    if args.verify and not args.agent:
        parser.error("--verify requires --agent or --mode diff")
    path = Path(args.filename)
    if args.mode == "diff" and not path.is_dir():
        parser.error("Diff mode requires a Git repository root directory")
    if path.is_dir() and not args.agent:
        parser.error("Project directories require --agent")
    if args.trace_file and args.trace_file.resolve() == path.resolve():
        parser.error("The trace file must not overwrite the source file")
    if path.is_dir() and args.trace_file and args.trace_file.suffix.lower() != ".json":
        parser.error("Project trace files must use the .json extension")
    return parser, args, path


def read_source(parser, path):
    """Read a single source file, or stop with a clear error."""
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        parser.exit(status=1, message=f"Error: file not found: {path}\n")
    except (OSError, UnicodeError) as error:
        parser.exit(status=1, message=f"Error: cannot read {path}: {error}\n")
    if not contents.strip():
        parser.exit(status=1, message="Error: the source file is empty.\n")
    return contents


def run_agent_review(args, path):
    """Collect Git changes if needed, run the agent, and optionally verify its findings."""
    project, changes = None, None
    if args.mode == "diff":
        project, changes = collect_changes(path, "HEAD~1" if args.base is None else args.base)
        if not changes["changed_count"]:
            # Nothing to review, so no LLM request is made.
            partial = bool(changes["skipped_count"] or changes["limitations"])
            return {"status": "incomplete" if partial else "complete", "findings": [], "trace": [],
                    "changes": changes,
                    "error": "No reviewable changes; some files could not be collected." if partial else None}
    provider, model, api_key = get_settings(args.provider, args.model)
    if provider not in ("openai", "groq"):
        raise ValueError("Agent mode supports OpenAI and Groq. Use --provider openai or groq.")
    if changes is None and path.is_dir():
        # Create the project here so verification can use the same file list.
        project = create_project(path)
    report = run_agent(path, model, api_key, change_context=changes, project=project, provider=provider)
    if args.verify and report["status"] == "complete" and report["findings"]:
        report["verification"] = verify_findings(report["findings"], path, model, api_key, provider=provider,
                                                 project=project, change_context=changes)
    return report


def print_changes(changes, status):
    print(f"Base: {changes['base']}")
    print(f"Changed Python files: {changes['changed_count']}; omitted: {changes['omitted_files']}; "
          f"skipped: {changes['skipped_count']}; excluded: {changes['excluded_count']}")
    for change in changes["changes"]:
        print(f"  {change['status']}: {change['file']}")
        if change["omitted_hunks"]:
            print(f"    Omitted patch hunks: {change['omitted_hunks']}")
    for skipped in changes["skipped"]:
        print(f"  Skipped {skipped['file']}: {skipped['reason']}")
    for limitation in changes["limitations"]:
        print(f"Limit: {limitation}")
    if not changes["changed_count"] and status == "complete":
        print("No eligible Python changes. No LLM request was made.")


def print_verification(verification):
    """Show one line per finding (F001 is finding 1 above) and the cost totals."""
    print("\n=== Evidence Verification ===")
    for item in verification["results"]:
        tools = f" [confirmed by {', '.join(item['corroborating_tools'])}]" if item["corroborating_tools"] else ""
        print(f"{item['finding_id']}: {item['verification_status'].upper()}{tools} - {item['reason']}")
    print(f"Verification calls: {verification['llm_calls']}; tokens in/out: "
          f"{verification['input_tokens'] if verification['input_tokens'] is not None else 'n/a'}/"
          f"{verification['output_tokens'] if verification['output_tokens'] is not None else 'n/a'}; "
          f"time: {verification['latency_ms'] / 1000:.1f}s")


def print_agent_report(report, mode):
    print("=== Git Diff Review ===" if mode == "diff" else "=== Agent Review ===")
    if "changes" in report:
        print_changes(report["changes"], report["status"])
    for event in report["trace"]:
        print(f"Step {event['step']}: {event['tool']} ({'ok' if event['observation']['ok'] else 'error'})")
        if not event["observation"]["ok"]:
            print(f"  {event['observation']['error']}")
    if report["status"] == "complete":
        print_findings(report["findings"])
        for rejected in report.get("rejected_findings", []):
            print(f"Removed finding '{rejected['title']}': {rejected['reason']}")
    else:
        print(f"Incomplete review: {report['error']}")
    if "verification" in report:
        print_verification(report["verification"])
    if "coverage" in report:
        coverage = report["coverage"]
        print(f"\nCoverage: {len(coverage['observed_files'])} files observed; "
              f"{coverage['eligible_files']} eligible files. This is a selective review.")
        for limitation in coverage["limits_reached"]:
            print(f"Limit: {limitation}")


def save_trace(parser, trace_file, report):
    try:
        # Exclusive creation ("x") prevents overwriting an existing file.
        with trace_file.open("x", encoding="utf-8") as output:
            json.dump(report, output, indent=2)
    except OSError as error:
        parser.exit(status=1, message=f"Could not save trace: {error}\n")


def run_simple_review(args, path, contents):
    """One AI request, then Bandit and Ruff. Returns True if every part worked."""
    sections = [("=== AI Review ===", None), ("\n=== Bandit Security Analysis ===", run_bandit),
                ("\n=== Ruff Code Quality Analysis ===", run_ruff)]
    succeeded = True
    for title, analyzer in sections:
        print(title)
        try:
            if analyzer is None:
                provider, model, api_key = get_settings(args.provider, args.model)
                findings = parse_findings(review_code(contents, provider, model, api_key))
                for finding in findings:
                    finding["source"] = "llm"
            else:
                findings = analyzer(path)
            print_findings(findings)
        except (ValueError, RuntimeError) as error:
            print(f"Error: {error}")
            succeeded = False
    return succeeded


def main():
    parser, args, path = parse_arguments()
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    contents = None if path.is_dir() else read_source(parser, path)

    if not args.agent:
        if not run_simple_review(args, path, contents):
            parser.exit(status=1)
        return

    try:
        report = run_agent_review(args, path)
    except (ValueError, RuntimeError, OSError, UnicodeError) as error:
        parser.exit(status=1, message=f"Error: {error}\n")
    print_agent_report(report, args.mode)
    if args.trace_file:
        save_trace(parser, args.trace_file, report)
    if report["status"] != "complete":
        parser.exit(status=1)


if __name__ == "__main__":
    main()
