"""Which provider, model, and API key to use, read from .env or the command line."""

import os

ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}
LABELS = {"openai": "OpenAI", "groq": "Groq", "openrouter": "OpenRouter"}
MISSING_SDK = "Missing SDK. Run: python -m pip install -r requirements.txt"


def get_settings(provider=None, model=None):
    """Choose a provider and model, then check their configuration."""
    provider = (provider if provider is not None else os.getenv("AI_PROVIDER", "openai")).strip().lower()
    implemented = ("gemini", "openai", "groq", "openrouter", "claude")
    enabled = [name.strip().lower() for name in os.getenv(
        "SUPPORTED_PROVIDERS", ",".join(implemented)
    ).split(",") if name.strip()]

    if any(name not in implemented for name in enabled):
        raise ValueError("SUPPORTED_PROVIDERS may contain only: " + ", ".join(implemented))
    if provider not in implemented or provider not in enabled:
        raise ValueError("Choose an enabled provider using AI_PROVIDER or --provider: "
                         + ", ".join(enabled))

    prefix = provider.upper()
    model = (model if model is not None else os.getenv(f"{prefix}_MODEL", "")).strip()
    if not model:
        raise ValueError(f"Set {prefix}_MODEL or supply --model.")

    # Optional allowlist: if omitted, any model ID can be sent to this provider.
    allowed = os.getenv(f"{prefix}_MODELS")
    if allowed is not None:
        models = [name.strip() for name in allowed.split(",") if name.strip()]
        if model not in models:
            raise ValueError(f"Model is not in {prefix}_MODELS. Allowed: {', '.join(models)}")

    if provider == "openrouter" and model != "openrouter/free" and not model.endswith(":free"):
        raise ValueError("OpenRouter requires openrouter/free or a model ID ending in :free.")

    api_key = os.getenv(f"{prefix}_API_KEY", "").strip()
    if not api_key:
        raise ValueError(f"Set {prefix}_API_KEY in .env or your environment.")
    return provider, model, api_key


def openai_reasoning_effort():
    """Optional OPENAI_REASONING_EFFORT in .env (for example none or low); None keeps the model's default."""
    return os.getenv("OPENAI_REASONING_EFFORT", "").strip().lower() or None
