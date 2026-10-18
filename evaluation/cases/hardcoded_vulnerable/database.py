import psycopg2

DB_PASSWORD = "Summer2024!"


def connect():
    return psycopg2.connect(host="db.internal", user="admin", password=DB_PASSWORD)
