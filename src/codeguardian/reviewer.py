import argparse
from pathlib import Path
from dotenv import load_dotenv

if __package__:
    from .providers import get_settings, review_code
else:
    from providers import get_settings, review_code


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
    except (ValueError, RuntimeError) as error:
        parser.exit(status=1, message=f"Error: {error}\n")

    print(review)


if __name__ == "__main__":
    main()
