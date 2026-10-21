"""Requests made by the agent loop and by evidence verification (OpenAI or Groq only)."""

import time

from .settings import ENDPOINTS, LABELS, MISSING_SDK, openai_reasoning_effort
from .usage import api_error_message, read_usage


def request_verification(messages, model, api_key, *, provider="openai"):
    """Make one OpenAI or Groq request without tools; return its text, token usage, and latency."""
    if provider not in ("openai", "groq"):
        raise ValueError("Verification supports OpenAI and Groq. Use --provider openai or groq.")
    label, endpoint = LABELS[provider], ENDPOINTS[provider]
    try:
        from openai import OpenAI, APIError, APIStatusError, APIConnectionError, APITimeoutError
    except ImportError:
        raise RuntimeError(MISSING_SDK) from None
    started = time.perf_counter()
    try:
        with OpenAI(api_key=api_key, base_url=endpoint, timeout=60.0, max_retries=0) as client:
            effort = openai_reasoning_effort() if provider == "openai" else None
            response = client.chat.completions.create(model=model, messages=messages, max_completion_tokens=400,
                                                      **({"reasoning_effort": effort} if effort else {}))
    except APITimeoutError:
        raise RuntimeError(f"{label} verification request timed out.") from None
    except APIConnectionError:
        raise RuntimeError(f"Could not connect to {label}.") from None
    except APIStatusError as error:
        raise RuntimeError(f"{label} verification API error {error.status_code}: "
                           f"{api_error_message(error, api_key)}") from None
    except APIError:
        raise RuntimeError(f"{label} returned an unexpected verification response.") from None
    latency_ms = round((time.perf_counter() - started) * 1000)
    if not response.choices or not response.choices[0].message.content:
        raise RuntimeError(f"{label} returned no verification text.")
    # Not every provider reports token usage, so these may be None.
    return {"content": response.choices[0].message.content,
            "usage": dict(read_usage(response), latency_ms=latency_ms)}


def to_responses_input(messages):
    """Convert the agent's Chat Completions messages to Responses API input items."""
    items = []
    for message in messages:
        if message["role"] == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                          "output": message["content"]})
        elif message["role"] == "assistant" and message.get("tool_calls"):
            for call in message["tool_calls"]:
                items.append({"type": "function_call", "call_id": call["id"],
                              "name": call["function"]["name"], "arguments": call["function"]["arguments"]})
        else:
            items.append({"role": message["role"], "content": message["content"]})
    return items


def openai_turn(client, messages, tools, model, allow_tools):
    """One agent turn through OpenAI's Responses API. Returns (tool calls, final text, response)."""
    effort = openai_reasoning_effort()
    response = client.responses.create(
        model=model, input=to_responses_input(messages),
        # strict=False because some tool arguments (like end_line) are optional.
        tools=[{"type": "function", **tool["function"], "strict": False} for tool in tools],
        tool_choice="auto" if allow_tools else "none",
        parallel_tool_calls=False, max_output_tokens=4096, store=False,
        **({"reasoning": {"effort": effort}} if effort else {}),
    )
    if response.status != "completed":
        raise RuntimeError("OpenAI did not complete the agent response (possibly an output limit or refusal).")
    # Reasoning items are skipped, so private reasoning is never kept.
    calls = [{"id": item.call_id, "type": "function", "function": {"name": item.name, "arguments": item.arguments}}
             for item in response.output if item.type == "function_call"]
    return calls, response.output_text, response


def groq_turn(client, messages, tools, model, allow_tools):
    """One agent turn through Groq's Chat Completions API. Returns (tool calls, final text, response)."""
    response = client.chat.completions.create(
        model=model, messages=messages, tools=tools,
        tool_choice="auto" if allow_tools else "none",
        parallel_tool_calls=False, max_completion_tokens=4096,
        extra_body={"include_reasoning": False},
    )
    if not response.choices:
        raise RuntimeError("Groq returned no agent response.")
    choice = response.choices[0]
    if choice.finish_reason not in ("stop", "tool_calls"):
        raise RuntimeError("Groq did not complete the agent response (possibly an output limit or refusal).")
    calls = []
    for call in choice.message.tool_calls or []:
        if call.type != "function":
            raise RuntimeError("Groq returned an unsupported tool request type.")
        calls.append({"id": call.id, "type": "function",
                      "function": {"name": call.function.name, "arguments": call.function.arguments}})
    return calls, choice.message.content, response


def request_agent_turn(messages, tools, model, api_key, allow_tools=True, *, provider="openai"):
    """Make one OpenAI or Groq request; keep only tool decisions or final text.

    OpenAI uses the Responses API, because newer reasoning models only allow tools there.
    Groq uses Chat Completions. The agent's messages stay in one format either way.
    """
    if provider not in ("openai", "groq"):
        raise ValueError("Agent mode supports OpenAI and Groq. Use --provider openai or groq.")
    label, endpoint = LABELS[provider], ENDPOINTS[provider]
    try:
        from openai import OpenAI, APIError, APIStatusError, APIConnectionError, APITimeoutError
    except ImportError:
        raise RuntimeError(MISSING_SDK) from None
    make_turn = openai_turn if provider == "openai" else groq_turn
    try:
        with OpenAI(api_key=api_key, base_url=endpoint, timeout=60.0, max_retries=0) as client:
            calls, content, response = make_turn(client, messages, tools, model, allow_tools)
    except APITimeoutError:
        raise RuntimeError(f"{label} agent request timed out.") from None
    except APIConnectionError:
        raise RuntimeError(f"Could not connect to {label}.") from None
    except APIStatusError as error:
        # Keep only the short message; a raw response could contain reasoning or credentials.
        raise RuntimeError(f"{label} agent API error {error.status_code}: "
                           f"{api_error_message(error, api_key)}") from None
    except APIError:
        raise RuntimeError(f"{label} returned an unexpected agent response.") from None
    return {"tool_calls": calls, "content": None if calls else content, "usage": read_usage(response)}
