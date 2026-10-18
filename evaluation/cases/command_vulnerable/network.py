import os


def ping(host):
    # The host comes from a web form.
    return os.system("ping -c 1 " + host)
