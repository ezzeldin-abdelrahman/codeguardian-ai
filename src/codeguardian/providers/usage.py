"""Token usage and error messages shared by every provider call."""


USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens")


def read_usage(response):
    """Token counts from an OpenAI-compatible response; None means the provider did not report it."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return dict.fromkeys(USAGE_FIELDS)
    # Chat Completions says prompt/completion; the Responses API says input/output.
    input_tokens = getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None)
    details = getattr(usage, "prompt_tokens_details", None) or getattr(usage, "input_tokens_details", None)
    cached = getattr(details, "cached_tokens", None)
    numbers = [value if isinstance(value, int) else None for value in (input_tokens, cached, output_tokens)]
    return dict(zip(USAGE_FIELDS, numbers))


def add_usage(total, usage):
    """Add one call's tokens to a running total. Once a value is unavailable, the total stays None."""
    for key in USAGE_FIELDS:
        value = (usage or {}).get(key)
        total[key] = None if total.get(key, 0) is None or value is None else total.get(key, 0) + value
    return total


def api_error_message(error, api_key):
    """The provider's short error message, with the API key removed, so failures can be understood."""
    message = getattr(error, "message", "") or ""
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        nested = body.get("error") if isinstance(body.get("error"), dict) else {}
        message = body.get("message") or nested.get("message") or message
    if api_key:
        message = str(message).replace(api_key, "[REDACTED]")
    return str(message)[:300]
