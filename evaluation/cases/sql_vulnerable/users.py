import sqlite3


def find_user(username):
    connection = sqlite3.connect("users.db")
    query = f"SELECT * FROM users WHERE username = '{username}'"
    return connection.execute(query).fetchall()
