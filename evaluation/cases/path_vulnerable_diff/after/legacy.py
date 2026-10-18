import hashlib


def old_checksum(data):
    # Pre-existing weak hash. It is not part of the change, so a diff review should not report it.
    return hashlib.md5(data).hexdigest()
