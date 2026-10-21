"""The tools the agent may ask for, and the checks run on every request before a tool starts.

The model sees TOOLS (single file) or PROJECT_TOOLS (project or Git diff). Each entry
describes one tool and its arguments in the JSON format that tool-calling APIs expect.
"""


TOOLS = [
    {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }}
    for name, description in (
        ("inspect_code", "Read the selected Python file with line numbers, without executing it."),
        ("run_bandit", "Check the selected file for security issues using Bandit."),
        ("run_ruff", "Check the selected file for code quality issues using Ruff."),
    )
]


PROJECT_TOOLS = [
    {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False},
    }}
    for name, description, properties, required in (
        ("list_files", "Discover eligible Python paths relative to the selected project.", {}, []),
        ("inspect_file", "Read a Python file or inclusive line range; long ranges are capped.",
         {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1},
          "end_line": {"type": "integer", "minimum": 1}}, ["path"]),
        ("search_code", "Search literal case-sensitive text across eligible Python files.",
         {"query": {"type": "string"}}, ["query"]),
        ("run_bandit", "Analyze one eligible Python file for security issues.",
         {"path": {"type": "string"}}, ["path"]),
        ("run_ruff", "Analyze one eligible Python file for quality issues.",
         {"path": {"type": "string"}}, ["path"]),
    )
]


def validate_project_arguments(name, arguments):
    """Check the model's request before dispatching any Python tool."""
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be a JSON object.")
    definitions = {tool['function']['name']: tool['function']['parameters'] for tool in PROJECT_TOOLS}
    if name not in definitions:
        raise ValueError("Unknown project tool.")
    definition = definitions[name]
    if set(arguments) - set(definition['properties']) or not set(definition['required']) <= set(arguments):
        raise ValueError("Missing required arguments or unexpected tool arguments.")


def validate_single_file_arguments(name, arguments):
    if arguments != {}:
        raise ValueError("Tool arguments must be an empty object: {}.")
    if name not in ("inspect_code", "run_bandit", "run_ruff"):
        raise ValueError("Unknown tool. Use inspect_code, run_bandit, or run_ruff.")
