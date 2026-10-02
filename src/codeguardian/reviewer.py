import argparse
import json
from pathlib import Path
from dotenv import load_dotenv

if __package__:
    from .providers import get_settings, review_code
else:
    from providers import get_settings, review_code


def parse_findings(response_text):
    """Parse JSON and check the required fields before displaying any findings."""
    try:
        review = json.loads(response_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"The model returned invalid JSON: {error.msg} (response line {error.lineno}).") from None

    if not isinstance(review, dict) or not isinstance(review.get("findings"), list):
        raise ValueError("The review must be a JSON object containing a findings list.")

    required = ("category", "severity", "title", "line", "description", "recommendation")
    for number, finding in enumerate(review["findings"], start=1):
        if not isinstance(finding, dict):
            raise ValueError(f"Finding {number} must be an object.")
        for field in required:
            if field not in finding:
                raise ValueError(f"Finding {number} is missing '{field}'.")
        if finding["category"] not in ("bug", "security", "quality", "testing"):
            raise ValueError(f"Finding {number} has an invalid category.")
        if finding["severity"] not in ("low", "medium", "high", "critical"):
            raise ValueError(f"Finding {number} has an invalid severity.")
        for field in ("title", "description", "recommendation"):
            if not isinstance(finding[field], str) or not finding[field].strip():
                raise ValueError(f"Finding {number}: '{field}' must be a nonempty string.")
        line = finding["line"]
        # bool is a subclass of int in Python, so check the exact type here.
        if line is not None and (type(line) is not int or line < 1):
            raise ValueError(f"Finding {number}: 'line' must be a positive integer or null.")
    return review["findings"]


def print_findings(findings):
    """Display validated findings as a readable review."""
    if not findings:
        print("No findings reported.")
        return
    for number, finding in enumerate(findings, start=1):
        if number > 1:
            print()
        print(f"{number}. [{finding['severity'].upper()}] {finding['category'].upper()} — {finding['title']}")
        line = finding["line"] if finding["line"] is not None else "Not specified"
        print(f"   Line: {line}")
        print(f"   Description: {finding['description']}")
        print(f"   Recommendation: {finding['recommendation']}")


def main():
    parser = argparse.ArgumentParser(description="Ask an AI model to review a source file.")
    parser.add_argument("filename", help="Path to the file to read")
    parser.add_argument("--provider", help="Override AI_PROVIDER for this run")
    parser.add_argument("--model", help="Override the selected provider's default model")
    args = parser.parse_args()

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

    try:
        provider, model, api_key = get_settings(args.provider, args.model)
        review = review_code(contents, provider, model, api_key)
        findings = parse_findings(review)
    except (ValueError, RuntimeError) as error:
        parser.exit(status=1, message=f"Error: {error}\n")
    print_findings(findings)


if __name__ == "__main__":
    main()
