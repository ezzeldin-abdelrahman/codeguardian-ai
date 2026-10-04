import argparse
import json
from pathlib import Path
from dotenv import load_dotenv

if __package__:
    from .agent import run_agent
    from .findings import parse_findings, print_findings
    from .providers import get_settings, review_code
    from .tools import run_bandit, run_ruff
else:
    from agent import run_agent
    from findings import parse_findings, print_findings
    from providers import get_settings, review_code
    from tools import run_bandit, run_ruff


def main():
    parser = argparse.ArgumentParser(description="Review a source file with AI, Bandit, and Ruff.")
    parser.add_argument("filename", help="Path to the file to read")
    parser.add_argument("--provider", help="Override AI_PROVIDER for this run")
    parser.add_argument("--model", help="Override the selected provider's default model")
    parser.add_argument("--agent", action="store_true", help="Use the bounded Groq agent")
    parser.add_argument("--trace-file", type=Path, help="Save agent actions and observations as JSON")
    args = parser.parse_args()
    if args.trace_file and not args.agent:
        parser.error("--trace-file requires --agent")

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    path = Path(args.filename)

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
        try:
            provider, model, api_key = get_settings(args.provider or "groq", args.model)
            if provider != "groq":
                raise ValueError("Agent mode supports only Groq. Use --provider groq.")
            report = run_agent(path, model, api_key)
        except (ValueError, RuntimeError) as error:
            parser.exit(status=1, message=f"Error: {error}\n")
        print("=== Agent Review ===")
        for event in report["trace"]:
            print(f"Step {event['step']}: {event['tool']} "
                  f"({'ok' if event['observation']['ok'] else 'error'})")
            if not event["observation"]["ok"]:
                print(f"  {event['observation']['error']}")
        if report["status"] == "complete":
            print_findings(report["findings"])
        else:
            print(f"Incomplete review: {report['error']}")
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
