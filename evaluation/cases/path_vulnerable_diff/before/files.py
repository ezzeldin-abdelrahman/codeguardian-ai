from pathlib import Path

BASE = Path("/srv/uploads")


def read_welcome():
    return (BASE / "welcome.txt").read_text()
