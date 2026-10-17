"""Small-project boundaries and budgets. All paths sent to the model are relative."""

import json
import os
from pathlib import Path, PurePosixPath

MAX_PROJECT_STEPS = 10
MAX_FILES = 100
MAX_FILE_BYTES = 100_000
MAX_INSPECTED_FILES = 10
MAX_INSPECT_LINES = 100
MAX_SEARCH_RESULTS = 20
MAX_TOTAL_SEARCH_RESULTS = 60
MAX_OBSERVATION_CHARS = 8000
MAX_CONTEXT_CHARS = 40_000
CONTEXT_RESERVE = 2500
MAX_CALL_CHARS = 1500

EXCLUDED_DIRECTORIES = {
    'venv', 'env', 'virtualenv', '__pycache__', 'node_modules', 'site-packages',
    'build', 'dist', 'vendor', 'htmlcov', 'secrets', 'credentials',
}


def excluded_name(name):
    """Conservative filename policy, not a detector for secrets inside source code."""
    lower = name.lower()
    return (lower.startswith('.') or lower in EXCLUDED_DIRECTORIES
            or any(word in lower for word in ('secret', 'credential', 'password', 'private_key'))
            or lower in ('config_local.py', 'local_settings.py'))


def relative_parts(path):
    if not isinstance(path, str) or not path or '\\' in path or '\x00' in path:
        raise ValueError('Use a nonempty project-relative Python path with forward slashes.')
    parts = path.split('/')
    if PurePosixPath(path).is_absolute() or any(part in ('', '.', '..') for part in parts):
        raise ValueError('Absolute paths, empty components, and . or .. are not allowed.')
    if any(excluded_name(part) for part in parts) or not path.endswith('.py'):
        raise ValueError('This path is excluded; only eligible Python files can be read.')
    return parts


def create_project(directory):
    """List eligible Python files without reading their contents."""
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ValueError('The selected project directory does not exist.')
    if any(excluded_name(part) for part in root.parts[1:]):
        raise ValueError('The selected project root is inside an excluded directory.')
    project = {'root': root, 'files': [], 'limits_reached': [], 'inspected': set(),
               'search_results': 0, 'observed': {}}
    # os.walk does not follow directory symlinks by default.
    for folder, subfolders, filenames in os.walk(root):
        # Skip excluded folders and virtual environments (they contain pyvenv.cfg).
        subfolders[:] = sorted(name for name in subfolders if not excluded_name(name)
                               and not (Path(folder) / name / 'pyvenv.cfg').exists())
        for name in sorted(filenames):
            file = Path(folder) / name
            if excluded_name(name) or not name.endswith('.py') or file.is_symlink() or not file.is_file():
                continue
            if file.stat().st_size > MAX_FILE_BYTES:
                continue
            if len(project['files']) == MAX_FILES:
                project['limits_reached'].append('Eligible file limit reached; discovery is partial.')
                project['files'].sort()
                return project
            project['files'].append(file.relative_to(root).as_posix())
    project['files'].sort()
    return project


def read_project_file(project, path):
    """Read an eligible file, refusing anything that leads outside the project."""
    relative_parts(path)
    if path not in project['files']:
        raise ValueError('File is missing, excluded, too large, or outside the eligible-file list.')
    file = project['root'] / path
    try:
        if file.is_symlink() or not file.resolve().is_relative_to(project['root']) or not file.is_file():
            raise RuntimeError('Cannot read selected file: missing, not a regular file, or outside the project.')
        if file.stat().st_size > MAX_FILE_BYTES:
            raise RuntimeError('File exceeds the source-size limit.')
        return file.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        raise RuntimeError('Cannot read selected file: missing, unreadable, or non-UTF-8.') from None


def claim_file(project, path):
    """Inspection and analysis share a budget; repeated access counts once."""
    relative_parts(path)
    if path not in project['files']:
        raise ValueError('File is not in the eligible-file list.')
    if path not in project['inspected'] and len(project['inspected']) >= MAX_INSPECTED_FILES:
        raise ValueError('Distinct inspected/analyzed file limit reached.')
    project['inspected'].add(path)


def bound_observation(observation, limit=MAX_OBSERVATION_CHARS):
    """Remove whole entries, never slice JSON or pretend omitted output is complete."""
    if len(json.dumps(observation)) <= limit:
        return observation
    observation['truncated'] = True
    for field in ('lines', 'matches', 'findings', 'files', 'errors'):
        values = observation.get(field)
        if isinstance(values, list):
            while values and len(json.dumps(observation)) > limit:
                values.pop()
    if len(json.dumps(observation)) > limit:
        return {'ok': False, 'error': 'Observation exceeds the remaining context budget.', 'truncated': True}
    return observation


def record_locations(project, observation):
    """Only locations actually delivered to the model may support final citations."""
    if not observation.get('ok'):
        return
    path = observation.get('path')
    if path and 'lines' in observation and (observation['lines'] or not observation.get('truncated')):
        project['observed'].setdefault(path, set()).update(item['line'] for item in observation['lines'])
    for item in observation.get('matches', []) + observation.get('findings', []):
        path = item['file']
        lines = project['observed'].setdefault(path, set())
        if type(item.get('line')) is int:
            lines.add(item['line'])


def validate_locations(project, findings):
    for finding in findings:
        if 'file' not in finding:
            raise ValueError('Project findings must contain file (relative path or null).')
        path = finding['file']
        if path is None:
            if finding['line'] is not None:
                raise ValueError('A finding without a file cannot cite a line.')
            continue
        relative_parts(path)
        if path in project.get('deleted_observed', set()):
            if finding['line'] is not None:
                raise ValueError('Deleted-file findings must use line:null and cite baseline lines in evidence.')
            continue
        if path not in project['observed']:
            raise ValueError('A final finding cites a file that was not observed.')
        if finding['line'] is not None and finding['line'] not in project['observed'][path]:
            raise ValueError('A final finding cites a line that was not observed.')


def record_change_locations(project, context):
    """Register only patch locations delivered to the model, after context trimming."""
    project['deleted_observed'] = set()
    for change in context['changes']:
        if change['status'] == 'deleted':
            if change['hunks'] or not change['omitted_hunks']:
                project['deleted_observed'].add(change['file'])
        elif change['hunks'] or not change['omitted_hunks']:
            lines = project['observed'].setdefault(change['file'], set())
            for hunk in change['hunks']:
                lines.update(hunk['current_lines'])
