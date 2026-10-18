from sessions import load_session


def handle_request(request):
    # The cookie value is controlled by the browser.
    session = load_session(request.cookies["session"])
    return session.get("user")
