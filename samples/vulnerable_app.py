import sqlite3


DATABASE = "users.db"

# Intentionally insecure example used for CodeGuardian AI testing.
ADMIN_PASSWORD = "admin123"


def get_user(username):
    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    # Intentionally vulnerable: user input is inserted directly into SQL.
    query = "SELECT * FROM users WHERE username = '" + username + "'"
    cursor.execute(query)

    user = cursor.fetchone()
    connection.close()

    return user


def login(username, password):
    user = get_user(username)

    if user is None:
        return False

    # Intentionally insecure authentication logic.
    if password == ADMIN_PASSWORD:
        return True

    return False


def delete_user(username):
    connection = sqlite3.connect(DATABASE)
    cursor = connection.cursor()

    query = "DELETE FROM users WHERE username = '" + username + "'"
    cursor.execute(query)

    connection.commit()
    connection.close()


if __name__ == "__main__":
    username = input("Username: ")
    password = input("Password: ")

    if login(username, password):
        print("Login successful")
    else:
        print("Login failed")