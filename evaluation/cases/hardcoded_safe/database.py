import os

import psycopg2


def connect():
    # Safe: the password is read from the environment, not stored in the code.
    return psycopg2.connect(host="db.internal", user="admin", password=os.environ["DB_PASSWORD"])
