# CodeGuardian AI

Reads one source file, sends its text to Gemini, OpenAI, Groq, or OpenRouter, and prints an unverified AI review.
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

Use `samples/vulnerable_app.py` for a sample review.

## Configuration

- `AI_PROVIDER`: default provider (`gemini`, `openai`, `groq`, or `openrouter`).
- `SUPPORTED_PROVIDERS`: comma-separated enabled providers; defaults to all four.
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

## Groq and OpenRouter

Create keys in [Groq Console](https://console.groq.com/keys) and
[OpenRouter](https://openrouter.ai/settings/keys), then fill in the corresponding
placeholders in your local `.env`:

```dotenv
GROQ_API_KEY=your_groq_key
GROQ_MODEL=openai/gpt-oss-120b
OPENROUTER_API_KEY=your_openrouter_key
OPENROUTER_MODEL=openrouter/free
```

If `SUPPORTED_PROVIDERS` is set, include `groq,openrouter` in it.
Keep `AI_PROVIDER` unchanged to retain your current default.
Optional `GROQ_MODELS` and `OPENROUTER_MODELS` accept comma-separated allowlists,
just like the existing providers.

```bash
python src/codeguardian/reviewer.py samples/vulnerable_app.py --provider groq
python src/codeguardian/reviewer.py samples/vulnerable_app.py --provider openrouter
python src/codeguardian/reviewer.py samples/vulnerable_app.py --provider groq --model openai/gpt-oss-20b
```

Both integrations reuse the OpenAI Python SDK with their own server address and key;
they do not use your OpenAI account balance. Groq uses your Groq account's plan and
limits: remain on its free plan for free usage.
OpenRouter is restricted to `openrouter/free` or IDs ending in `:free`.
The free router can choose a different model each time. For repeatable comparisons,
choose a specific available `:free` model using `--model`. Appending `:free` works
only if that model has a free variant. No paid fallback is configured.

Requests use Chat Completions: a system message holds review instructions, and a user
message holds source text. The returned message content is the review. Existing OpenAI
requests continue to use the Responses API. No extra dependencies are needed.

References: [Groq compatibility](https://console.groq.com/docs/openai),
[OpenRouter free variants](https://openrouter.ai/docs/guides/routing/model-variants/free),
[free router](https://openrouter.ai/openrouter/free).

Run offline tests with `python -m unittest discover -s tests -v`.

## v0.2: Structured findings

The model is prompted to return only `{"findings": [...]}`. Each finding contains
`category` (bug/security/quality/testing), `severity` (low/medium/high/critical),
nonempty `title`, `description`, and `recommendation` strings, and `line`
(a positive integer or null). An empty findings list is valid.

The application numbers source lines before sending them, parses the response with
Python's built-in `json.loads()`, checks required fields and types, then prints a
readable review. Invalid JSON or invalid fields produce a clear error and exit
status 1. Markdown fences and surrounding prose are rejected, not repaired.

This is prompt-based JSON output, not a provider-enforced schema. Providers may
still return invalid responses. Validation checks structure, not whether a finding
is true or its line reference is correct. All four providers and existing CLI
options remain supported. No new dependencies are required.

## Claude provider

Install the dependencies, then set `CLAUDE_API_KEY` to your Anthropic API key and
`CLAUDE_MODEL` to an available model (example: `claude-sonnet-4-5`) in `.env`.
Include `claude` in `SUPPORTED_PROVIDERS` if that setting is present.
An optional `CLAUDE_MODELS` comma-separated allowlist works like other providers.

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python src/codeguardian/reviewer.py samples/vulnerable_app.py --provider claude
```

Claude uses the Anthropic Messages API with the same JSON review instructions.
The application passes `CLAUDE_API_KEY` explicitly to the SDK. It extracts text
blocks from the response and reports truncated output rather than parsing partial
JSON. The output limit is 4096 tokens. Your default provider is unchanged.
This integration requires Anthropic API access and any applicable API credits.
Reference: https://platform.claude.com/docs/en/api/python/messages/create

## v0.4: Tool-using agent (Groq only)

The original sequential review remains the default. Use `--agent` to let one model
choose between `inspect_code`, `run_bandit`, and `run_ruff`:

```bash
.venv/bin/python src/codeguardian/reviewer.py samples/vulnerable_app.py --agent --model openai/gpt-oss-120b
.venv/bin/python src/codeguardian/reviewer.py samples/vulnerable_app.py --agent --trace-file review-trace.json
```

Agent mode selects Groq unless a provider is explicitly supplied; other providers
are rejected in agent mode. It uses GROQ_API_KEY and GROQ_MODEL and honors the
existing provider/model allowlists. No default environment setting is changed.

A tool request is a name and an empty argument object. Python binds all three
tools to the user-selected file and executes only those named functions. The
model receives the result as an observation before choosing its next action.
No shell commands or alternate paths can be supplied by the model. Source code
and observations are sent to Groq; the source is never executed.

The loop allows five investigation turns and at most one finalization request.
Invalid requests and failed tools consume turns. Multiple calls in one turn are
rejected, and tools are disabled for finalization. Tool and API requests time out
after 60 seconds; API retries are disabled. An API failure or invalid final JSON
produces an incomplete review with exit status 1. Tool failures become observations
and remain visible in the terminal even if the agent later finishes successfully.

Final findings use the existing structure and source `llm`. Bandit/Ruff results
retain their original source labels in observations. There is no automatic
three-tool sequence, deduplication, or evidence verification.

The optional trace file records status, final findings, tool names, arguments,
call IDs, execution flags, and observations. It can include source text, so share
it only when appropriate. No raw SDK responses, reasoning fields, or API keys
are deliberately recorded. Groq requests set `include_reasoning=False`. Trace
files are created only if the path does not already exist, to prevent overwrites.

Implementation: `agent.py` controls the loop; `providers.request_agent_turn()`
makes one request; `tools.py` executes tools; `findings.py` validates and displays
findings. This is ordinary Python with no agent framework.

Tests mock model responses, so they do not use API credits. Run them with:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## v0.5: Context-aware project investigation

Pass a small Python project directory with `--agent`:

```bash
.venv/bin/python src/codeguardian/reviewer.py path/to/project --agent
.venv/bin/python src/codeguardian/reviewer.py path/to/project --agent --trace-file project-review.json
```

Directory review uses the existing Groq agent and provider configuration. Single-file
sequential review and single-file `--agent` behavior remain available. Directories
without `--agent` produce a usage error. No new dependency or framework is required.

The model can choose five tools in project mode:

| Tool | Arguments | Observation |
| --- | --- | --- |
| `list_files` | `{}` | Eligible relative `.py` paths |
| `inspect_file` | `path`, optional `start_line` and `end_line` | Original numbered lines; endpoints are inclusive |
| `search_code` | `query` | Case-sensitive literal matches with file, line, and excerpt |
| `run_bandit` | `path` | Normalized security findings |
| `run_ruff` | `path` | Normalized quality findings |

Python establishes the eligible-file list without reading source contents. The LLM
chooses what to inspect; it is not given every file automatically. It can discover
an import in one file, search for the imported symbol, and inspect its definition
in another. Empty files can be inspected. Invalid ranges, unavailable tools, denied
paths, or unreadable files become error observations instead of clean findings.

Project findings add `file` (relative path or null) to the existing finding structure.
A cited file and line must have appeared in a successful content-bearing observation
actually delivered to the model. A listing alone does not count as evidence. A
project-wide finding must use both `file: null` and `line: null`. Final findings
remain labeled `source: llm`; original tool sources remain in the trace. These
location checks do not verify the truth of a finding or establish complete coverage.

### Limits

The constants live in `project.py`:

- 10 investigation turns plus at most one finalization request (single-file mode retains 5).
- 100 eligible files, 2,000 directory entries examined, and 20 directory levels.
- 100,000 bytes per source file; oversized files are excluded or refused if they grow.
- 10 distinct files explicitly inspected or analyzed; repeat visits count once.
- 100 lines per inspection, 20 matches per search, 60 matches across a review.
- Search scans the bounded eligible-file list, separately from the explicit inspection budget.
- 8,000 serialized characters per observation and 40,000 per request conversation,
  including tool definitions, instructions, tool calls, and observations. This is
  a character limit, not exact token accounting. The existing model output cap also applies.
- Tool-request payloads are capped at 1,500 serialized characters in project mode.

Whole output entries are removed when necessary, before JSON serialization, with
`truncated: true`. Long search lines return at most 300 characters around the match.
Counters conservatively include matches found even if context trimming omits them.
The loop reserves context space for finalization and disables tools when it is nearly
full. Coverage metadata records observed files, inspected files, search counts, and
limits reached. A valid final report can still have partial coverage; the terminal
makes that visible. Incomplete reports retain their trace.

### Access boundaries

Only eligible project-relative regular `.py` files can be read. Absolute paths,
`..`, symlinks (including internal links), hard links, and special files are rejected.
Hidden paths, Git internals, virtual environments (including directories containing
`pyvenv.cfg`), dependency/build/cache directories, and names containing `secret`,
`credential`, `password`, or `private_key` are excluded. `config_local.py` and
`local_settings.py` are also excluded. This conservative policy may omit legitimate
code; it is not a general secret detector. Credentials embedded in ordinary eligible
source can still be sent to the provider. `.gitignore` is not interpreted in v0.5.

`project.py` uses POSIX directory descriptors and `O_NOFOLLOW` to open path components
without following symlinks. It checks the selected root's identity and file size at
read time. Unsupported platforms fail closed. macOS and Linux are the initial target
platforms. Review a stable local project: v0.5 does not provide a transactional snapshot
of a concurrently changing filesystem or an operating-system sandbox.

Project analyzer calls securely read the selected file, scan a temporary copy, and
map findings back to the original relative path. Python subprocesses run with `-I`,
a minimal environment, and a neutral working directory. Ruff uses `--isolated`;
Bandit uses an empty INI file. Project-local tool settings and plugins are not loaded
from the project. Installed tools and the application's Python environment are trusted.
This intentionally uses default analyzer rules in project mode; single-file mode
continues to honor its existing configuration. Ruff never applies fixes.

Source and tool output are untrusted data, never instructions. Python validates tool
names, argument keys, file access, and budgets even when the model requests otherwise.
There is no unrestricted shell tool and reviewed Python code is not executed.

The optional trace records bounded observations and final findings, not model reasoning.
It may contain source excerpts. Project trace paths must end in `.json`; existing files
are never overwritten. Trace output is a user-chosen destination, not a model-accessible tool.

### Testing

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Temporary-project tests cover discovery, exact line ranges, search, exclusions, missing
files, permissions, invalid encoding, traversal, symlink swaps, hard links, special
files, budgets, analyzer isolation, and unsupported citations. Agent tests simulate
related-file investigation and ensure saved observations match model-visible data.
The tests make no live model requests. Provider-specific live behavior remains a
separate manual check.
