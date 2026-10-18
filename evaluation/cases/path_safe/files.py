from pathlib import Path

BASE = Path("/srv/uploads").resolve()


def read_user_file(name):
    path = (BASE / name).resolve()
    # Safe: after resolving "..", the path must still be inside BASE.
    if not path.is_relative_to(BASE):
        raise ValueError("Invalid file name")
    return path.read_text()
