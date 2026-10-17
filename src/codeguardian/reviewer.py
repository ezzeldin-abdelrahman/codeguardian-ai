import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

if __package__:
    from .agent import run_agent
    from .findings import parse_findings, print_findings
    from .git_changes import collect_changes
    from .providers import get_settings, review_code
    from .tools import run_bandit, run_ruff
else:
    from agent import run_agent
    from findings import parse_findings, print_findings
    from git_changes import collect_changes
    from providers import get_settings, review_code
    from tools import run_bandit, run_ruff


def main():
    parser = argparse.ArgumentParser(description="Review a source file with AI, Bandit, and Ruff.")
    parser.add_argument("filename", help="Source file, or project directory with --agent")
    parser.add_argument("--provider", help="Override AI_PROVIDER for this run")
    parser.add_argument("--model", help="Override the selected provider's default model")
    parser.add_argument("--agent", action="store_true", help="Use the bounded OpenAI/Groq agent")
    parser.add_argument("--mode", choices=("full", "diff"), default="full", help="Review full source or Git changes")
    parser.add_argument("--base", help="Base commit for --mode diff (default: HEAD~1, the last commit)")
    parser.add_argument("--trace-file", type=Path, help="Save agent actions and observations as JSON")
    args = parser.parse_args()
    if args.base is not None and args.mode != 'diff':
        parser.error('--base requires --mode diff')
    if args.mode == 'diff':
        args.agent = True
    if args.trace_file and not args.agent:
        parser.error("--trace-file requires --agent")

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    path = Path(args.filename)
    if args.mode == 'diff' and not path.is_dir():
        parser.error('Diff mode requires a Git repository root directory')

    if path.is_dir():
        if not args.agent:
            parser.error("Project directories require --agent")
    else:
        try:
            contents = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            parser.exit(status=1, message=f"Error: file not found: {path}\n")
        except (OSError, UnicodeError) as error:
            parser.exit(status=1, message=f"Error: cannot read {path}: {error}\n")
        if not contents.strip():
            parser.exit(status=1, message="Error: the source file is empty.\n")

    if args.agent:
        if args.trace_file and args.trace_file.resolve() == path.resolve():
            parser.error("The trace file must not overwrite the source file")
        if path.is_dir() and args.trace_file and args.trace_file.suffix.lower() != ".json":
            parser.error("Project trace files must use the .json extension")
        try:
            project = None
            changes = None
            if args.mode == 'diff':
                project, changes = collect_changes(path, 'HEAD~1' if args.base is None else args.base)
            if changes is not None and not changes['changed_count']:
                partial = bool(changes['skipped_count'] or changes['limitations'])
                report = {'status': 'incomplete' if partial else 'complete', 'findings': [],
                          'trace': [], 'changes': changes,
                          'error': 'No reviewable changes; some files could not be collected.' if partial else None}
            else:
                provider, model, api_key = get_settings(args.provider, args.model)
                if provider not in ("openai", "groq"):
                    raise ValueError("Agent mode supports OpenAI and Groq. Use --provider openai or groq.")
                if changes is None:
                    report = run_agent(path, model, api_key, provider=provider)
                else:
                    report = run_agent(path, model, api_key, change_context=changes, project=project, provider=provider)
        except (ValueError, RuntimeError, OSError, UnicodeError) as error:
            parser.exit(status=1, message=f"Error: {error}\n")
        print("=== Git Diff Review ===" if args.mode == 'diff' else "=== Agent Review ===")
        if 'changes' in report:
            changes = report['changes']
            print(f"Base: {changes['base']}")
            print(f"Changed Python files: {changes['changed_count']}; "
                  f"omitted: {changes['omitted_files']}; skipped: {changes['skipped_count']}; "
                  f"excluded: {changes['excluded_count']}")
            for change in changes['changes']:
                print(f"  {change['status']}: {change['file']}")
                if change['omitted_hunks']:
                    print(f"    Omitted patch hunks: {change['omitted_hunks']}")
            for skipped in changes['skipped']:
                print(f"  Skipped {skipped['file']}: {skipped['reason']}")
            for limitation in changes['limitations']:
                print(f"Limit: {limitation}")
            if not changes['changed_count'] and report['status'] == 'complete':
                print('No eligible Python changes. No LLM request was made.')
        for event in report["trace"]:
            print(f"Step {event['step']}: {event['tool']} "
                  f"({'ok' if event['observation']['ok'] else 'error'})")
            if not event["observation"]["ok"]:
                print(f"  {event['observation']['error']}")
        if report["status"] == "complete":
            print_findings(report["findings"])
            for rejected in report.get("rejected_findings", []):
                print(f"Removed finding '{rejected['title']}': {rejected['reason']}")
        else:
            print(f"Incomplete review: {report['error']}")
        if "coverage" in report:
            coverage = report["coverage"]
            print(f"\nCoverage: {len(coverage['observed_files'])} files observed; "
                  f"{coverage['eligible_files']} eligible files. This is a selective review.")
            for limitation in coverage["limits_reached"]:
                print(f"Limit: {limitation}")
        if args.trace_file:
            try:
                # Exclusive creation prevents overwriting an existing file.
                with args.trace_file.open("x", encoding="utf-8") as output:
                    json.dump(report, output, indent=2)
            except OSError as error:
                parser.exit(status=1, message=f"Could not save trace: {error}\n")
        if report["status"] != "complete":
            parser.exit(status=1)
        return

    failed = False
    print("=== AI Review ===")
    try:
        provider, model, api_key = get_settings(args.provider, args.model)
        review = review_code(contents, provider, model, api_key)
        findings = parse_findings(review)
        for finding in findings:
            finding["source"] = "llm"
        print_findings(findings)
    except (ValueError, RuntimeError) as error:
        print(f"Error: {error}")
        failed = True

    print("\n=== Bandit Security Analysis ===")
    try:
        print_findings(run_bandit(path))
    except RuntimeError as error:
        print(f"Error: {error}")
        failed = True

    print("\n=== Ruff Code Quality Analysis ===")
    try:
        print_findings(run_ruff(path))
    except RuntimeError as error:
        print(f"Error: {error}")
        failed = True

    if failed:
        parser.exit(status=1)


if __name__ == "__main__":
    main()
