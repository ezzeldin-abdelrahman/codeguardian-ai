import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Read and print a source file.")
    parser.add_argument("filename", help="Path to the file to read")
    args = parser.parse_args()

    path = Path(args.filename)

    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        parser.exit(status=1, message=f"Error: file not found: {path}\n")
    except (OSError, UnicodeError) as error:
        parser.exit(status=1, message=f"Error: cannot read {path}: {error}\n")

    print(contents, end="")


if __name__ == "__main__":
    main()
