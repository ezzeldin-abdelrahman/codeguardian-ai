# CodeGuardian AI

Reads one source file, sends its text to Gemini or OpenAI, and prints an unverified AI review.
The source is not executed. API requests send the source to the selected provider and may cost money.

## Setup

Use Python 3.10 or newer. From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

If you do not already have `.env`, copy `.env.example` to `.env` and fill in the
selected provider's API key. Keep keys out of Git; `.env` is already ignored.
The example preserves Gemini as the default and the existing model choices.
Model availability depends on the provider and your account.

## Run

```bash
python src/codeguardian/reviewer.py path/to/nonempty_file.py
python src/codeguardian/reviewer.py path/to/nonempty_file.py --provider openai
python src/codeguardian/reviewer.py path/to/nonempty_file.py --provider gemini --model YOUR_MODEL_ID
```

`samples/vulnerable_app.py` is currently empty; add a small example before reviewing it.

## Configuration

- `AI_PROVIDER`: default provider (`gemini` or `openai`).
- `SUPPORTED_PROVIDERS`: comma-separated enabled providers; defaults to both.
- `GEMINI_MODEL` / `OPENAI_MODEL`: each provider's default model.
- `GEMINI_MODELS` / `OPENAI_MODELS`: optional comma-separated lists of allowed model IDs.
  If set, the selected model (including the default) must be in its provider's list.
  If omitted, the provider checks whether the requested model exists and is accessible.
- `GEMINI_API_KEY` / `OPENAI_API_KEY`: only the selected provider's key is required.

CLI options override configuration for that run only. Existing shell environment variables
take precedence over `.env`. The application loads `.env` from the repository root,
while relative source paths are resolved from your current working directory.

A provider is the service you contact; a model is the specific AI selected within it.
`get_settings()` selects and validates configuration. `review_code()` uses a simple
`if`/`elif` to call the appropriate SDK. Adding a name to configuration alone does
not implement a new provider. No provider classes or agent frameworks are used.
