"""Small-project boundaries and budgets. All paths sent to the model are relative."""

import json
import os
import stat
from pathlib import Path, PurePosixPath

MAX_PROJECT_STEPS = 10
MAX_FILES = 100
MAX_ENTRIES = 2000
MAX_DEPTH = 20
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


def open_root(root):
    """Open each directory without following symlinks (POSIX, including macOS/Linux)."""
    if not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY'):
        raise RuntimeError('Secure project access requires POSIX O_NOFOLLOW support.')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open('/', flags)
    try:
        for part in Path(root).parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def create_project(directory):
    """Build a bounded eligibility list without reading source contents."""
    original = Path(directory).absolute()
    if original.is_symlink():
        raise ValueError('The selected project directory must not be a symlink.')
    root = original.resolve(strict=True)
    if any(excluded_name(part) for part in root.parts[1:]):
        raise ValueError('The selected project root is inside an excluded directory.')
    project = {'root': root, 'files': [], 'limits_reached': [], 'inspected': set(),
               'search_results': 0, 'observed': {}}
    descriptor = open_root(root)
    try:
        info = os.fstat(descriptor)
        project['root_identity'] = (info.st_dev, info.st_ino)
        # Walk through directory descriptors so a symlink swap cannot redirect traversal.
        pending = [('', 0)]
        entries_seen = 0
        while pending:
            relative, depth = pending.pop()
            current = os.dup(descriptor)
            try:
                for part in relative.split('/') if relative else []:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                    os.close(current)
                    current = child
                # Virtual environments can have arbitrary directory names.
                try:
                    os.stat('pyvenv.cfg', dir_fd=current, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    continue
                with os.scandir(current) as entries:
                    for entry in entries:
                        entries_seen += 1
                        if entries_seen > MAX_ENTRIES:
                            project['limits_reached'].append('Directory entry limit reached; discovery is partial.')
                            pending.clear()
                            break
                        if excluded_name(entry.name) or entry.is_symlink():
                            continue
                        name = f'{relative}/{entry.name}' if relative else entry.name
                        if entry.is_dir(follow_symlinks=False):
                            if depth < MAX_DEPTH:
                                pending.append((name, depth + 1))
                            elif 'Directory depth limit reached.' not in project['limits_reached']:
                                project['limits_reached'].append('Directory depth limit reached.')
                        elif name.endswith('.py') and entry.is_file(follow_symlinks=False):
                            info = entry.stat(follow_symlinks=False)
                            if info.st_size > MAX_FILE_BYTES or info.st_nlink != 1:
                                continue
                            if len(project['files']) == MAX_FILES:
                                project['limits_reached'].append('Eligible file limit reached; discovery is partial.')
                                pending.clear()
                                break
                            project['files'].append(name)
            except OSError:
                project['limits_reached'].append('A directory could not be read; discovery is partial.')
            finally:
                os.close(current)
        project['files'].sort()
    finally:
        os.close(descriptor)
    return project


def read_project_file(project, path):
    """Read at most 100 KB; reject symlinks at every component during opening."""
    parts = relative_parts(path)
    if path not in project['files']:
        raise ValueError('File is missing, excluded, too large, or outside the eligible-file list.')
    directory = None
    file_descriptor = None
    try:
        directory = open_root(project['root'])
        info = os.fstat(directory)
        if (info.st_dev, info.st_ino) != project['root_identity']:
            raise RuntimeError('The project directory changed; start a new review.')
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        file_descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(file_descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError('Only regular files without hard links are eligible.')
        if info.st_size > MAX_FILE_BYTES:
            raise RuntimeError('File exceeds the source-size limit.')
        with os.fdopen(file_descriptor, 'rb') as source:
            file_descriptor = None
            data = source.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise RuntimeError('File exceeds the source-size limit.')
        return data.decode('utf-8')
    except (OSError, UnicodeError):
        raise RuntimeError('Cannot read selected file: missing, unreadable, non-UTF-8, or symlink access denied.') from None
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if directory is not None:
            os.close(directory)


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
        if path not in project['observed']:
            raise ValueError('A final finding cites a file that was not observed.')
        if finding['line'] is not None and finding['line'] not in project['observed'][path]:
            raise ValueError('A final finding cites a line that was not observed.')
