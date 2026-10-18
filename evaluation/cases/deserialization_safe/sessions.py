import base64
import json


def load_session(cookie):
    # Safe: JSON can only create plain data (dicts, lists, strings, numbers).
    data = base64.b64decode(cookie)
    return json.loads(data)
