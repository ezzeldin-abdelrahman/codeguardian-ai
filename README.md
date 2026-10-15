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

- `AI_PROVIDER`: default provider (defaults to `openai`; also supports `gemini`, `groq`, `openrouter`, and `claude`).
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

## v0.4: Tool-using agent (OpenAI or Groq)

The original sequential review remains the default. Use `--agent` to let one model
choose between `inspect_code`, `run_bandit`, and `run_ruff`:

```bash
.venv/bin/python src/codeguardian/reviewer.py samples/vulnerable_app.py --agent --provider groq --model openai/gpt-oss-120b
.venv/bin/python src/codeguardian/reviewer.py samples/vulnerable_app.py --agent --trace-file review-trace.json
```

Agent mode follows `--provider`, then `AI_PROVIDER`, defaulting to OpenAI when unset.
The supplied configuration uses `AI_PROVIDER=openai`. OpenAI uses `OPENAI_API_KEY`
and `OPENAI_MODEL`; `--provider groq` uses `GROQ_API_KEY` and `GROQ_MODEL` instead.
Existing provider/model allowlists apply. Other providers remain available for
sequential review but are not supported by the agent.

A tool request is a name and an empty argument object. Python binds all three
tools to the user-selected file and executes only those named functions. The
model receives the result as an observation before choosing its next action.
No shell commands or alternate paths can be supplied by the model. Source code
and observations are sent to the selected provider; the source is never executed.

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

Directory review uses the existing OpenAI/Groq agent and provider configuration. Single-file
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

## v0.6: Git-aware review

```bash
# Existing full-project review (full is the default)
python3 src/codeguardian/reviewer.py . --agent --mode full

# Changes from HEAD to current files; diff mode implies --agent
python3 src/codeguardian/reviewer.py . --mode diff

# Current files compared with an earlier commit
python3 src/codeguardian/reviewer.py . --mode diff --base HEAD~1

# Optional trace: choose a new filename; existing files are not overwritten
python3 src/codeguardian/reviewer.py . --mode diff --trace-file diff-review.json
```

Existing single-file commands, provider selection, and full-project investigation
remain available. Diff review uses the existing OpenAI/Groq agent and model settings.
It requires Git and a repository root with a local `.git` directory. Linked
worktrees, bare repositories, subdirectory scopes, unresolved merge conflicts,
and repositories without a valid base commit receive an error in this version.
No additional Python dependency is required.

### What is compared

The base defaults to `HEAD`. The comparison is against the **current files on disk**,
so staged and unstaged changes are included together. If an unstaged edit completely
reverses a staged edit, there is no net change to review. `--base HEAD~1` compares
that commit with current disk contents, not just with the latest commit.

Non-ignored untracked Python files are included and labelled `untracked`. Git-ignored
untracked files are unavailable to the diff agent, including as related context.
Tracked files remain eligible even if an ignore pattern matches them. The existing
security exclusions still apply to both versions. Excluded Python paths are counted;
inaccessible, oversized, binary, or unsupported files are reported as skipped.
An unreadable current file is never treated as a deletion.

Git reads baseline regular-file blobs; the existing protected reader reads current
Python files. Temporary `before` and `after` directories contain only approved source
copies. Git compares those snapshots in a neutral directory without external diff
helpers, text conversions, or repository content filters. Git subprocesses have fixed
argument lists, a controlled environment, disabled filesystem-monitor hooks and network
protocols, and a 30-second timeout. Output is captured to temporary files and rejected
if it exceeds 2 MB after the command; this bounds in-memory consumption, not temporary
disk usage during execution. Nothing is staged, committed, checked out, or modified
in the reviewed repository.

This compares source contents as stored/read; Git attribute normalization and executable
permission-only changes are not reproduced. Rename detection is heuristic and limited
to eligible Python snapshots. Unrecognized renames appear as addition/deletion; crossing
the `.py` boundary appears as addition or deletion within the Python review scope.

### Agent context and evidence

The existing agent receives changed-file statuses, old/new paths, patches, added/removed
line numbers, and coverage limitations in its initial user message. There is no new
agent or unrestricted Git/shell tool. It may use `list_files`, `inspect_file`, `search_code`,
Bandit, and Ruff to investigate changed files and related unchanged Python files.

Diff hunk headers initialize separate old/current line counters. A `+` line advances
the current counter; a `-` line advances the old counter; context advances both.
Only locations in delivered hunks or successful tool observations may be cited.
Deleted files may be discussed using baseline patches even when no current Python files
remain. Their finding uses the old path and `line: null`, with old line numbers explicitly
identified in evidence. Current files use current line numbers.

Diff findings retain `category`, `severity`, `title`, `file`, `line`, `description`,
`recommendation`, and `source`, and require a nonempty `evidence` string. Evidence should
cite observed code or tool output and explain its connection to a change. It is printed
in the terminal and retained in the trace. The agent is instructed to omit unrelated
pre-existing findings, including those found by Bandit/Ruff. Relevance and evidence
correctness are still model judgments: v0.6 adds neither semantic verification nor
deduplication. Patches, filenames, and tool outputs are untrusted data, not instructions.

### Bounds and coverage

Collection permits at most 100 candidate Python paths and 2,000 inventory entries per
baseline/current inventory. It retains the existing 100 KB per-file limit. Initial
change context is limited to 20 changed files and 12,000 serialized characters, inside
the existing 40,000-character conversation budget. Whole hunks/records are omitted when
necessary; counts and limitations are shown. Collection reads and patch observations
are separate from the existing 10 explicitly inspected/analyzed-file budget. All agent
step, tool-observation, and search limits remain active.

Safe current-file reads are fingerprinted during collection. Later tool reads refuse
those files if their bytes changed. This helps prevent mixed-version investigations;
it is not a transactional filesystem snapshot or an OS sandbox. Review a stable local
checkout. Historical content can contain embedded credentials just as current code can;
filename exclusions do not detect secrets embedded in otherwise eligible source.

An empty eligible diff finishes without loading API settings or calling the LLM. If
collection failed or skipped files, the program reports incomplete coverage instead
of claiming a clean review. Optional traces include exactly the initial bounded change
context supplied to the model, tool calls/observations, and final findings, without
chain-of-thought. Traces may contain source code.

Tests use temporary Git repositories for staged/unstaged/untracked changes, renames,
deletions, bases, merge conflicts, unusual filenames, path restrictions, helper isolation,
line parsing, context caps, and mocked agent/CLI integration. Run them with the existing
`python -m unittest discover -s tests -v` command inside the virtual environment.

OpenAI agent requests use Chat Completions tool calling, preserving the existing
message loop. Groq-only request options are omitted for OpenAI. See the
[official OpenAI documentation](https://developers.openai.com/api/docs/guides/function-calling).
