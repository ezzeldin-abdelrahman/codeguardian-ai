import sqlite3


def find_user(username):
    connection = sqlite3.connect("users.db")
    # Safe: the value is passed as a query parameter, never joined into the SQL text.
    return connection.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchall()
