import base64
import pickle


def load_session(cookie):
    data = base64.b64decode(cookie)
    return pickle.loads(data)
