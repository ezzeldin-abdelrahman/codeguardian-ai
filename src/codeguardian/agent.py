"""The CodeGuardian agent: a bounded loop where the LLM chooses which tool to call next.

Each turn the model either asks for one tool or returns its final findings:

    ask the model -> run the requested tool -> send the result back -> repeat
"""

import json
from pathlib import Path

from . import project as project_access
from .agent_tools import PROJECT_TOOLS, TOOLS, validate_project_arguments, validate_single_file_arguments
from .findings import parse_findings
from .prompts import build_messages
from .providers import add_usage, request_agent_turn
from .tools import inspect_code, inspect_file, list_files, run_bandit, run_ruff, search_code

MAX_STEPS = 5


def execute_project_tool(project, name, arguments):
    """Explicitly dispatch one validated request; file checks remain in the tools."""
    if name == 'list_files':
        return list_files(project)
    if name == 'inspect_file':
        return inspect_file(project, **arguments)
    if name == 'search_code':
        return search_code(project, **arguments)
    if name == 'run_bandit':
        return run_bandit(arguments['path'], project=project)
    return run_ruff(arguments['path'], project=project)


def conversation_size(messages, tools):
    return len(json.dumps({'messages': messages, 'tools': tools}))


def tools_allowed(step, max_steps, project, messages, tools):
    """Tools are switched off after the last step, or when the conversation is nearly full."""
    allowed = step <= max_steps
    if project is not None:
        used = conversation_size(messages, tools)
        if used + project_access.CONTEXT_RESERVE + project_access.MAX_CALL_CHARS + 500 >= project_access.MAX_CONTEXT_CHARS:
            allowed = False
            project["limits_reached"].append("Context limit reached; investigation stopped early.")
    if not allowed and project is not None and step > max_steps:
        project["limits_reached"].append("Investigation step limit reached.")
    return allowed


def ask_model(messages, tools, model, api_key, allow_tools, provider, usage):
    """Make one LLM request and add its tokens to the usage totals."""
    usage["llm_calls"] += 1
    try:
        turn = request_agent_turn(messages, tools, model, api_key, allow_tools, provider=provider)
    except RuntimeError:
        # The call was attempted but failed, so its token usage is unknown (not zero).
        add_usage(usage, None)
        raise
    add_usage(usage, turn.get("usage"))
    return turn


def execute_single_file_tool(path, name):
    if name == "inspect_code":
        return {"ok": True, "code": inspect_code(path)}
    if name == "run_bandit":
        return {"ok": True, "findings": run_bandit(path)}
    return {"ok": True, "findings": run_ruff(path)}


def limit_observation(project, observation, messages, tools):
    """Shrink a project tool result to fit the conversation budget, and note any limits."""
    remaining = (project_access.MAX_CONTEXT_CHARS - project_access.CONTEXT_RESERVE
                 - conversation_size(messages, tools) - 500)
    if remaining < 200:
        observation = {"ok": False, "error": "Context budget exhausted.", "truncated": True}
    else:
        observation = project_access.bound_observation(observation, min(project_access.MAX_OBSERVATION_CHARS, remaining))
    if observation.get("truncated"):
        project["limits_reached"].append("An observation was truncated or partial.")
    if not observation["ok"]:
        project["limits_reached"].append("A requested project tool failed or was refused.")
    project_access.record_locations(project, observation)
    return observation


def run_tool_call(call, call_count, path, project):
    """Check and run one requested tool. Errors become failed observations sent back to the model."""
    name = call["function"]["name"]
    arguments = None
    executed = False
    try:
        arguments = json.loads(call["function"]["arguments"])
        if call_count != 1:
            raise ValueError("Only one tool call per turn is allowed; none were executed.")
        # "executed" means the request passed the checks and the tool was started.
        if project is not None:
            validate_project_arguments(name, arguments)
            executed = True
            observation = execute_project_tool(project, name, arguments)
        else:
            validate_single_file_arguments(name, arguments)
            executed = True
            observation = execute_single_file_tool(path, name)
    except (ValueError, RuntimeError, OSError) as error:
        observation = {"ok": False, "error": str(error)}
    return arguments, executed, observation


def final_findings(content, project, change_context, report):
    """Check the model's final JSON and keep only the expected fields."""
    findings = parse_findings(content or "", require_evidence=change_context is not None)
    fields = ["category", "severity", "title", "line", "description", "recommendation"]
    if project is not None:
        # Drop only the findings that cite unseen code; keep the rest of the review.
        report["rejected_findings"] = []
        kept = []
        for item in findings:
            try:
                project_access.validate_locations(project, [item])
                kept.append(item)
            except ValueError as error:
                report["rejected_findings"].append({"title": item["title"], "reason": str(error)})
        findings = kept
        fields.append("file")
    if change_context is not None:
        fields.append("evidence")
    return [dict({key: item[key] for key in fields}, source="llm") for item in findings]


def coverage(project):
    return {"eligible_files": len(project["files"]), "inspected_files": sorted(project["inspected"]),
            "observed_files": sorted(project["observed"]), "search_results": project["search_results"],
            "deleted_files_observed": sorted(project.get("deleted_observed", set())),
            "limits_reached": list(dict.fromkeys(project["limits_reached"]))}


def check_target(path, original, project, change_context):
    """Return the project (created if needed), or raise ValueError if there is nothing to review."""
    if path.is_dir():
        if project is None:
            project = project_access.create_project(original)
        if not project["files"] and change_context is None:
            raise ValueError("The project contains no eligible Python files.")
    elif not path.is_file():
        raise ValueError("The selected source file or project does not exist.")
    return project


def run_agent(file_path, model, api_key, *, change_context=None, project=None, provider="openai"):
    """Run the bounded agent loop on one file or a small project, and return a report."""
    original = Path(file_path)
    path = original.resolve()
    report = {"status": "incomplete", "findings": [], "trace": [], "error": None,
              # Token counts are None when the provider does not report them.
              "usage": {"llm_calls": 0, "tool_calls": 0, "input_tokens": 0, "cached_input_tokens": 0,
                        "output_tokens": 0}}
    if change_context is not None:
        report["changes"] = change_context
    try:
        project = check_target(path, original, project, change_context)
    except (ValueError, RuntimeError, OSError) as error:
        report["error"] = str(error)
        return report
    if change_context is not None:
        project_access.record_change_locations(project, change_context)
    tools = PROJECT_TOOLS if project is not None else TOOLS
    max_steps = project_access.MAX_PROJECT_STEPS if project is not None else MAX_STEPS
    messages = build_messages(project, change_context, max_steps)

    try:
        # One extra step lets the model give its final answer after the last tool.
        for step in range(1, max_steps + 2):
            allow_tools = tools_allowed(step, max_steps, project, messages, tools)
            if not allow_tools:
                messages.append({"role": "user", "content":
                                 "Investigation limit reached. Return final JSON findings now; tools are disabled."})
            if project is not None and conversation_size(messages, tools) > project_access.MAX_CONTEXT_CHARS:
                raise ValueError("Conversation exceeds the context limit; cannot make another request.")

            turn = ask_model(messages, tools, model, api_key, allow_tools, provider, report["usage"])
            calls = turn["tool_calls"]
            if not calls:
                report["findings"] = final_findings(turn["content"], project, change_context, report)
                report["status"] = "complete"
                return report
            if not allow_tools:
                raise ValueError("The model requested tools after the investigation limit.")
            if project is not None and len(json.dumps(calls)) > project_access.MAX_CALL_CHARS:
                raise ValueError("Tool request exceeds the argument-size limit; no tool was executed.")

            messages.append({"role": "assistant", "content": None, "tool_calls": calls})
            for call in calls:
                arguments, executed, observation = run_tool_call(call, len(calls), path, project)
                if project is not None:
                    observation = limit_observation(project, observation, messages, tools)
                report["usage"]["tool_calls"] += executed
                report["trace"].append({"step": step, "call_id": call["id"], "tool": call["function"]["name"],
                                        "arguments": arguments if arguments is not None else call["function"]["arguments"],
                                        "executed": executed, "observation": observation})
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(observation)})
    except (ValueError, RuntimeError) as error:
        report["error"] = str(error)
    finally:
        if project is not None:
            report["coverage"] = coverage(project)
    return report
