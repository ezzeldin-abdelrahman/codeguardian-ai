# CodeGuardian AI

An AI code reviewer for Python. In its main mode, an LLM agent reviews the changes
in your last Git commit by deciding for itself which tools to use: reading code,
searching the project, and running the Bandit (security) and Ruff (code quality)
analyzers. The reviewed code is never executed. API requests send source code to the
selected provider and may cost money. All findings are AI-generated and unverified.

## How the agent works

The LLM is the reviewer. CodeGuardian gives it a small set of tools and runs a
bounded loop:

```
            ┌──────────────────────────────────────────────┐
            ▼                                              │
  Git changes ──► LLM decides ──► wants a tool? ──yes──► CodeGuardian checks the
  (or a file)     next step            │                 request, runs the tool,
                                       no                and returns the result
                                       ▼
                             final JSON findings ──► validated and printed
```

1. **Start:** the agent receives the task, for example the patches from `git diff`.
2. **Decide:** each turn, the model either asks for one tool or gives its final answer.
3. **Act:** CodeGuardian checks the request (known tool, valid arguments, allowed file)
   before running it. Errors are sent back to the model as results, not hidden.
4. **Observe:** the tool output goes back into the conversation, and the loop repeats.
5. **Finish:** the model returns findings as JSON. CodeGuardian checks every field, and
   in project/diff mode rejects findings that cite files or lines the agent never saw.

| Mode | Tools the agent can call |
| --- | --- |
| Single file (`--agent`) | `inspect_code`, `run_bandit`, `run_ruff` |
| Project or Git diff (`--mode diff`) | `list_files`, `inspect_file`, `search_code`, `run_bandit`, `run_ruff` |

**Safety limits:** at most 5 steps for one file or 10 for a project, one tool call per
turn, at most 10 files inspected, and a fixed conversation size. When a limit is
reached, tools are switched off and the model must give its final answer. Tool output
and source code are treated as data, never as instructions to the agent.

**Providers:** the agent uses OpenAI (the default) or Groq, because it relies on their
tool-calling API. The simpler one-shot review (no `--agent`) also supports Gemini,
OpenRouter, and Claude; it sends the whole file in a single request, then runs Bandit
and Ruff separately.

## Setup

Use Python 3.10 or newer. From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

If you do not already have `.env`, copy `.env.example` to `.env` and fill in the
selected provider's API key. Keep keys out of Git; `.env` is already ignored.
The example uses OpenAI as the default provider.
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
- `SUPPORTED_PROVIDERS`: comma-separated enabled providers; defaults to all five.
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
`..`, symlinks, and special files are rejected, and every read checks that the
resolved path is still inside the project folder. Hidden paths, Git internals,
virtual environments (including directories containing `pyvenv.cfg`),
dependency/build/cache directories, and names containing `secret`, `credential`,
`password`, or `private_key` are excluded. `config_local.py` and `local_settings.py`
are also excluded. This conservative policy may omit legitimate code; it is not a
general secret detector. Credentials embedded in ordinary eligible source can still
be sent to the provider. `.gitignore` is not interpreted.

These are simple, explainable checks, not an operating-system sandbox. CodeGuardian is
meant for reviewing your own local projects; review a stable checkout.

Project analyzer calls first check that the requested file is eligible, then run
Bandit or Ruff on it and map findings back to the relative path. Ruff never applies fixes.

separate manual check.

## v0.6: Git-aware review

```bash
# Existing full-project review (full is the default)
python3 src/codeguardian/reviewer.py . --agent --mode full

# Review the last commit (plus any uncommitted edits); diff mode implies --agent
python3 src/codeguardian/reviewer.py . --mode diff

# Review only uncommitted changes
python3 src/codeguardian/reviewer.py . --mode diff --base HEAD

# Optional trace: choose a new filename; existing files are not overwritten
python3 src/codeguardian/reviewer.py . --mode diff --trace-file diff-review.json
```

Existing single-file commands, provider selection, and full-project investigation
remain available. Diff review uses the existing OpenAI/Groq agent and model settings.
It requires Git and the repository root directory. Repositories with unresolved merge
conflicts, or without a valid base commit, receive an error.
No additional Python dependency is required.

### What is compared

The base defaults to `HEAD~1`, so a normal run reviews **the last commit**. The
comparison is against the current files on disk, so any staged or unstaged edits made
since then are included too. Use `--base HEAD` to review only uncommitted work, or any
other commit (for example `--base main`).

CodeGuardian runs `git diff <base> -- '*.py'` to list changed Python files and then
asks Git for each file's patch. New files must be added with `git add` before they
appear. The same filename exclusions as project mode apply (for example, a changed
`secrets.py` is counted as excluded and never sent). Files that are now symlinks or
too large are reported as skipped. Git commands use fixed argument lists (no shell),
a 30-second timeout, and `--no-ext-diff --no-textconv` so the repository cannot make
Git run external diff programs. Nothing is staged, committed, or modified.
Only review repositories you trust: Git still applies a repository's own configured
content filters when it reads files.

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

Initial change context is limited to 20 changed files and 12,000 serialized
characters, inside the existing 40,000-character conversation budget. Whole
hunks/records are omitted when necessary; counts and limitations are shown. All agent
step, tool-observation, and search limits remain active.

An empty eligible diff finishes without loading API settings or calling the LLM. If
collection failed or skipped files, the program reports incomplete coverage instead
of claiming a clean review. Optional traces include exactly the initial bounded change
context supplied to the model, tool calls/observations, and final findings, without
chain-of-thought. Traces may contain source code.

Tests use temporary Git repositories for staged/unstaged changes, renames,
deletions, bases, merge conflicts, unusual filenames, path restrictions,
line parsing, context caps, and mocked agent/CLI integration. Run them with the existing
`python -m unittest discover -s tests -v` command inside the virtual environment.

OpenAI agent requests use Chat Completions tool calling, preserving the existing
message loop. Groq-only request options are omitted for OpenAI. See the
[official OpenAI documentation](https://developers.openai.com/api/docs/guides/function-calling).

## v0.7: Evidence verification

```bash
# Opt-in: one extra API call per finding (at most 10), so it is off by default
python3 src/codeguardian/reviewer.py . --mode diff --verify
python3 src/codeguardian/reviewer.py . --agent --verify
```

After the agent finishes, `--verify` checks each finding separately. The original
findings are not changed; results are added to the report under `verification` and
printed in an "Evidence Verification" section (`F001` is finding 1, and so on). This
is a fixed pipeline in `verifier.py`, not a second agent: the verifier has no tools.

For each finding:

1. **Check the location (code).** The file must be an eligible project file (the same
   rules as `inspect_file`) and the line must exist. Otherwise the result is
   `unsupported` and no API call is made.
2. **Collect evidence (code).** The cited line ±10 lines (at most 40 lines and
   4,000 characters), plus the diff patch for that file in diff mode. Deleted files
   are checked against their patch only.
3. **Correlate tools (code).** Bandit (for `security` findings) and Ruff (for
   `quality`/`bug` findings) are run locally on the file; results within 3 lines count
   as corroboration.
4. **Ask the model (one call).** It receives only the evidence above and returns
   `{"status", "reason", "evidence_lines"}`. Source and tool output are untrusted data.
5. **Apply fixed rules (code).** The model's answer is not trusted on its own:

| Situation | Final status |
| --- | --- |
| Bad file/line location | `unsupported` |
| Malformed finding, project-wide finding, API error, timeout, or invalid JSON | `uncertain` |
| Model says `supported` but cites no line from the supplied source | `uncertain` |
| Model says `supported` for a finding with no line, and no tool agrees | `uncertain` |
| Model says `unsupported` but Bandit/Ruff flags the same area | `uncertain` (conflict) |
| Otherwise | the model's status |

Only lines that were actually supplied to the model can appear as `evidence`, so
locations are never invented. `independent_corroboration` is true only when Bandit or
Ruff agrees: a second LLM call agreeing with the first is not independent proof.
Each result records token usage and latency when the provider reports them, and the
terminal prints the totals. Findings beyond the first 10 are marked `uncertain`
("not verified: limit reached"). Findings already removed by the agent's location
check are not verified. Verification supports OpenAI and Groq, like the agent.

## Demo: v0.6 and v0.7 end to end

```bash
# One real review of a tiny two-commit repository (about a cent of API credit)
python examples/demo_diff_review.py

# The same check as an optional live test (skipped unless enabled)
CODEGUARDIAN_LIVE_TEST=1 python -m unittest tests.test_live_demo -v
```

The demo builds a temporary repository: the first commit has a safe `app.py` and a
`helper.py` with an old `eval` bug; the second commit adds an SQL injection to `app.py`.
It runs `--mode diff --verify` and checks the trace: the base is `HEAD~1`, only `app.py`
is reviewed, the SQL injection is found, the old `helper.py` bug is not reported, and
verification marks the finding `supported` with Bandit agreeing. LLM output varies, so
a single run can occasionally differ. A real run is saved in
[examples/sample-output.txt](examples/sample-output.txt) and
[examples/sample-trace.json](examples/sample-trace.json).
