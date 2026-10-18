from pathlib import Path

BASE = Path("/srv/uploads")


def read_welcome():
    return (BASE / "welcome.txt").read_text()


def read_user_file(name):
    # The name comes from the URL, for example "../../etc/passwd".
    return (BASE / name).read_text()
