# CodeGuardian AI

CodeGuardian is an AI code reviewer for Python, built as a learning project to show how
an **agentic AI application** works in practice. An LLM agent reviews code changes by
deciding for itself which tools to use (reading code, searching the project, and running
the Bandit security scanner and the Ruff linter). A second, separate step checks each
finding against the actual source code, and a small labeled benchmark measures how well
the whole system works. In v1.0 it reviews every GitHub pull request automatically.

The reviewed code is never executed. API requests send source code to the selected
provider and may cost money. All findings are AI-generated suggestions, not proof.

**Contents:** [What it does](#what-it-does) · [How the agent works](#how-the-agent-works) ·
[Setup](#setup) · [Running it](#running-it) · [Pull request reviews](#pull-request-reviews-github-actions) ·
[Evaluation and results](#evaluation-and-benchmark-results) · [Security and limitations](#security-and-limitations) ·
[Demo](#demo) · [Project layout](#project-layout) · [Version history](docs/version-history.md)

## What it does

| Feature | Version | Command |
| --- | --- | --- |
| One-shot review: one LLM call, then Bandit and Ruff | v0.1-v0.3 | `reviewer.py app.py` |
| Tool-using agent on one file or a small project | v0.4-v0.5 | `reviewer.py app.py --agent` |
| Review only the changes of the last commit | v0.6 | `reviewer.py . --mode diff` |
| Evidence verification of every finding | v0.7 | add `--verify` |
| Accuracy evaluation on 12 hand-labeled cases | v0.8 | `evaluation/evaluate.py` |
| Quality, speed, and cost benchmark of four approaches | v0.9 | `evaluation/evaluate.py --modes all` |
| Pull request review in GitHub Actions, Markdown report | v1.0 | automatic, or `--merge-base main` |

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
                             final JSON findings ──► validated, optionally verified, reported
```

1. **Start:** the agent receives the task, for example the patches from `git diff`.
2. **Decide:** each turn, the model either asks for one tool or gives its final answer.
3. **Act:** CodeGuardian checks the request (known tool, valid arguments, allowed file)
   before running it. Errors are sent back to the model as results, not hidden.
4. **Observe:** the tool output goes back into the conversation, and the loop repeats.
5. **Finish:** the model returns findings as JSON. CodeGuardian checks every field and
   removes any finding that cites a file or line the agent never actually saw.
6. **Verify (optional):** for each finding, code collects the cited lines and nearby
   Bandit/Ruff results, one separate LLM call judges the claim, and fixed rules decide
   `supported`, `uncertain`, or `unsupported`. Only Bandit/Ruff agreement counts as
   independent confirmation; two LLM calls agreeing is not proof.

| Mode | Tools the agent can call |
| --- | --- |
| Single file (`--agent`) | `inspect_code`, `run_bandit`, `run_ruff` |
| Project or Git diff (`--mode diff`) | `list_files`, `inspect_file`, `search_code`, `run_bandit`, `run_ruff` |

**Safety limits:** at most 5 steps for one file or 10 for a project, one tool call per
turn, at most 10 files inspected, a fixed conversation size, and at most 10 verified
findings. When a limit is reached, tools are switched off and the model must answer.
Source code and tool output are treated as data, never as instructions.

**Providers:** the agent and verification use **OpenAI** (default, Responses API) or
**Groq** (Chat Completions). The one-shot review also supports **Gemini**,
**OpenRouter** (free models only), and **Claude**.

## Setup

Use Python 3.10 or newer. From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env        # then fill in the API key of the provider you use
```

`.env` is ignored by Git. Main settings:

- `AI_PROVIDER`: `openai` (default), `groq`, `gemini`, `openrouter`, or `claude`.
- `<PROVIDER>_MODEL` and `<PROVIDER>_API_KEY`, for example `OPENAI_MODEL=gpt-6-luna`.
- Optional: `<PROVIDER>_MODELS` (allowed model list), `SUPPORTED_PROVIDERS`, and
  `OPENAI_REASONING_EFFORT` (for example `none`; unset keeps the model's default).

Command-line options `--provider` and `--model` override `.env` for one run.
More configuration details are in the [version history](docs/version-history.md).

## Running it

```bash
# One-shot review of a file (AI review, then Bandit and Ruff)
python src/codeguardian/reviewer.py samples/vulnerable_app.py

# Tool-using agent on a file or a project folder, with evidence verification
python src/codeguardian/reviewer.py samples/vulnerable_app.py --agent --verify

# Review the last commit (plus uncommitted edits); --base HEAD reviews only uncommitted work
python src/codeguardian/reviewer.py . --mode diff --verify

# Review a branch like a pull request: compare with where it split off from main
python src/codeguardian/reviewer.py . --mode diff --merge-base main --verify \
    --report-md review.md --trace-file review.json --title "my-branch"

# The same comparison with Bandit and Ruff only, on changed lines (free, no API key)
python src/codeguardian/reviewer.py . --mode diff --merge-base main --no-llm --report-md review.md
```

**Git-aware review** compares a base commit with the files on disk, so line numbers
always refer to the current version. Added, modified, renamed, and deleted Python files
are handled; other file types are ignored. New files must be added with `git add`.
`--merge-base BRANCH` uses `git merge-base` to find where the current branch split off,
so changes made to `BRANCH` since then are not counted as the branch's changes.

Findings are marked **in changed code** or **related code (not new in this change)**:
the agent may report an issue in an unchanged file it inspected, but it is never shown
as newly introduced. The Markdown report (`--report-md`) shows the commit range, status,
counts by severity, each finding with file, line, evidence, and verification status,
and any skipped files, limits, or errors. `--trace-file` saves the full JSON report.

## Pull request reviews (GitHub Actions)

The workflow [.github/workflows/codeguardian.yml](.github/workflows/codeguardian.yml)
reviews every pull request that changes Python files (`opened`, `synchronize`,
`reopened`; draft PRs are skipped).

**Setup (once):** in the GitHub repository, open *Settings → Secrets and variables →
Actions*.

- **Secret** `OPENAI_API_KEY` (or `GROQ_API_KEY` if you use Groq).
- **Variables** (optional): `AI_PROVIDER` (default `openai`), `OPENAI_MODEL`
  (default `gpt-6-luna`), `GROQ_MODEL`, and `CODEGUARDIAN_VERIFY` (default `true`;
  set to `false` to skip verification).

**What happens:** the job checks out the PR's head commit with full history, installs
the requirements, and runs [.github/scripts/pr_review.py](.github/scripts/pr_review.py).
That script is the only GitHub-specific code: it reads the PR number and base commit,
decides whether a paid review is allowed, and runs the normal CLI with
`--mode diff --merge-base <PR base commit> --verify --report-md --trace-file`. The
report appears on the run's **Summary** page, and `report.md` and `report.json` are
uploaded as the `codeguardian-report` artifact. No comments are posted on the PR.

| Situation | Job | Summary shows |
| --- | --- | --- |
| Review completed (with or without findings) | passes | the findings report |
| PR from a fork, or no API key set | passes with a warning | "**Paid AI review SKIPPED**" and the reason, plus free Bandit/Ruff results on changed lines |
| API error, timeout, or incomplete review | fails | the error (API keys are removed from messages) |

Findings do not fail the job in v1.0; they are information for the people reviewing the PR.
Cost: with `gpt-6-luna`, a small PR costs well under one cent, including verification.

## Evaluation and benchmark results

**Method (v0.8-v0.9).** `evaluation/cases/` contains 12 small, hand-written cases: one
vulnerable and one safe example for SQL injection, command injection, hard-coded
credentials, unsafe deserialization (`pickle`), path traversal, and weak password hashing
(MD5). One case spans two files and one is a Git-diff case. The correct answers are in
`evaluation/labels.json`, written by hand and never changed to improve a score. A
finding counts as correct when its issue type and file match and its line is within
±3 lines; matching is one-to-one, so duplicates count as false positives. Four
approaches are compared with the same cases and rules:

- `static_only`: Bandit and Ruff, no LLM
- `llm_only`: one LLM call per file, no tools
- `agentic`: the tool-using agent
- `agentic_verified`: the same agent findings, after evidence verification

**Results** (run of 2026-10-20: 12 cases × 2 repeats; `gpt-6-luna` and Groq's free
`openai/gpt-oss-120b`; prices from `evaluation/pricing.json`):

| Approach | Model | Precision | Recall | F1 (range over repeats) | Completed |
| --- | --- | --- | --- | --- | --- |
| static_only | Bandit + Ruff | 56% | 100% | 71% (71-71%) | 22/22 |
| llm_only | gpt-6-luna | 29% | 100% | 45% (43-47%) | 18/20 |
| llm_only | gpt-oss-120b | 20% | 100% | 33% (32-33%) | 20/20 |
| agentic | gpt-6-luna | 48% | 91% | 62% (59-67%) | 23/24 |
| agentic | gpt-oss-120b | 33% | 100% | 50% (40-60%) | 13/24 |
| agentic_verified | gpt-6-luna | 44% | 73% | 55% (50-62%) | 23/24 |
| agentic_verified | gpt-oss-120b | 42% | 100% | 59% (50-67%) | 13/24 |

Measured cost: the 24 `agentic` reviews with `gpt-6-luna` used 57,789 input and 9,700
output tokens, about **$0.01 in total** ($0.0005 per review). Some totals are unknown
because failed calls report no token counts. Average review time: 0.2 s for Bandit and
Ruff, about 9 s for the `gpt-6-luna` agent, and about 12 s with verification.

**Second view** (clearly separate from the main score): many false positives were
security or bug comments outside the six benchmark types, such as "missing TLS on the
database connection". Leaving those out, precision becomes 83% for the `gpt-6-luna`
agent (F1 87%), 78% for `gpt-6-luna` one-shot (F1 88%), and 62% for Bandit and Ruff
(F1 77%). The main table keeps counting them, because the rules were fixed before the run.

**What the results show (and do not show):**

- The agent clearly beat a single LLM call with both models (F1 45% → 62% and 33% → 50%):
  reading the code with tools roughly halved the false alarms.
- Verification helped Groq (3 false alarms removed, no correct finding lost) but hurt
  `gpt-6-luna` (1 false alarm removed, 2 correct findings rejected). It is a safety
  filter with a cost, not a free improvement.
- On these simple, textbook cases, Bandit and Ruff had the best main F1 at zero cost.
- Groq's free tier was unreliable for the agent (11 of 24 reviews failed): the model
  sometimes tried to call a non-existent tool named "JSON", and three runs hit the free
  limit of 8,000 tokens per minute.
- 12 small cases are enough to show the method, not to prove one approach is better.

Reproduce or rescore it:

```bash
python evaluation/evaluate.py --smoke                     # 2 cases, all modes, about 1 cent
python evaluation/evaluate.py --modes all --models openai:gpt-6-luna,groq:openai/gpt-oss-120b \
    --repeats 2 --budget 0.50 --pause 20                  # the full run (results vary between runs)
python evaluation/evaluate.py --score-only                # rebuild the latest report, no API calls
```

## Security and limitations

- **No code execution:** reviewed code is only read, scanned by Bandit and Ruff (which
  do not run it), and sent to the LLM as untrusted data. Git runs with
  `--no-ext-diff --no-textconv`, so a repository cannot make Git run external programs.
- **File access:** only project-relative `.py` files can be read; symlinks, paths
  outside the project, hidden folders, virtual environments, and names containing
  `secret`, `credential`, `password`, or `private_key` are refused. This is a filename
  rule, not a secret detector: a key written inside ordinary code would still be sent.
- **Secrets:** API keys come from `.env` locally or GitHub secrets in CI. They are never
  printed, and provider error messages have the key replaced by `[REDACTED]`.
- **GitHub Actions:** the workflow uses `pull_request` (never `pull_request_target`),
  read-only permissions, `persist-credentials: false`, a 15-minute timeout, and cancels
  older runs of the same PR. Fork PRs never receive secrets (GitHub's rule) and the
  script also skips paid review for them.
- **Known limitation:** with `pull_request`, the workflow file and CodeGuardian's own
  code come from the PR branch, so someone who can push branches to this repository could
  change them to misuse the API key. Fork PRs cannot. Protect the key with repository
  settings: give write access only to trusted people and require review before merging.
  Repository content is sent to the LLM even though prompts treat it as data; a prompt
  injection could still change what the model reports.
- **AI limits:** findings can be wrong or missing; verification reduces but does not
  remove false alarms, and it can reject correct findings. Line numbers and evidence are
  checked against what the agent saw, but the meaning of a finding is not proven.
- **Scope:** Python only; small projects (limits on files, lines, and conversation size);
  Git diff mode needs the repository root and a base commit.

## Demo

```bash
# 1. Free: unit tests (no API calls)
python -m unittest discover -s tests

# 2. About one cent: build a two-commit repository, review only the last commit, verify findings
python examples/demo_diff_review.py

# 3. Pull request demo: on a new branch, add a SQL injection to a Python file, push, and
#    open a pull request. The "CodeGuardian review" check shows the report in its Summary.
```

A saved real run of step 2 is in [examples/sample-output.txt](examples/sample-output.txt)
and [examples/sample-trace.json](examples/sample-trace.json).

## Project layout

Every file has one job and is at most about 240 lines.

```
src/codeguardian/
  reviewer.py         command line: reads options and prints results
  agent.py            the agent loop: ask the model, run the tool it chose, repeat
  agent_tools.py      the tools the model may ask for, and the checks on each request
  prompts.py          the instructions the agent receives
  tools.py            Bandit, Ruff, reading files, and searching code
  project.py          which project files may be read, and the size limits
  git_changes.py      collects Git changes (last commit, any base, or a merge base)
  changed_lines.py    marks findings on changed lines; free Bandit/Ruff fallback
  verifier.py         evidence verification
  findings.py         checks and prints findings
  markdown_report.py  the Markdown review report
  providers/          everything that talks to an LLM
    settings.py         provider, model, and API key from .env
    review.py           the one-shot review (one request, no tools)
    agent_calls.py      one agent turn (OpenAI or Groq) and one verification request
    usage.py            token counts and short error messages
evaluation/           benchmark: evaluate.py, modes.py, scoring.py, report.py, cases, labels
.github/              the pull request workflow and its helper script
examples/             end-to-end demo script and a saved sample run
tests/                unit tests; no paid API calls
docs/                 version history (v0.1-v0.9 design notes)
```
