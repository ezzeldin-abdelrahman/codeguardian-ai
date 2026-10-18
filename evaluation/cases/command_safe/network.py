import subprocess

ALLOWED_HOSTS = {"example.com", "localhost"}


def ping(host):
    if host not in ALLOWED_HOSTS:
        raise ValueError("Unknown host")
    # Safe: fixed allowlist, argument list, and no shell.
    return subprocess.run(["ping", "-c", "1", host], check=False).returncode
