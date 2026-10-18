import hashlib
import os


def hash_password(password):
    # Safe: random salt and a slow, standard password hash (PBKDF2-SHA256).
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return salt.hex() + ":" + digest.hex()
