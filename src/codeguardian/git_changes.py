"""Collect the Python changes between a base commit and the current files, using git diff."""

import json
import re
import subprocess

if __package__:
    from . import project as project_access
else:
    import project as project_access

MAX_CHANGED_FILES = 20
MAX_CHANGE_CHARS = 12_000
# Do not run external diff programs or text converters configured in the repository.
DIFF_OPTIONS = ['--no-ext-diff', '--no-textconv', '--no-color', '--find-renames']
# T (type change, for example a file replaced by a symlink) is treated as a modification.
STATUS_NAMES = {'A': 'added', 'M': 'modified', 'T': 'modified', 'D': 'deleted', 'R': 'renamed'}


def run_git(directory, arguments):
    """Run one Git command (argument list, no shell) and return its output as text."""
    try:
        result = subprocess.run(['git', '-C', str(directory), *arguments],
                                capture_output=True, timeout=30, check=False)
    except FileNotFoundError:
        raise RuntimeError('Git is not installed or is unavailable on the system PATH.') from None
    except subprocess.TimeoutExpired:
        raise RuntimeError('Git exceeded the 30-second time limit.') from None
    if result.returncode != 0:
        raise RuntimeError(f'Git {arguments[0]} failed (exit {result.returncode}).')
    return result.stdout.decode('utf-8', errors='replace')


def parse_hunks(patch):
    """Track old and current line counters separately, including deletion-only hunks."""
    hunks = []
    current = None
    # Git counts LF-delimited lines, not every Unicode separator handled by splitlines().
    for line in patch.split('\n'):
        if not line:
            continue
        line += '\n'
        header = re.match(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
        if header:
            old_line, new_line = map(int, header.groups())
            current = {'patch': line, 'added_lines': [], 'removed_lines': [], 'current_lines': []}
            hunks.append(current)
        elif current is not None:
            current['patch'] += line
            if line.startswith('+'):
                current['added_lines'].append(new_line)
                current['current_lines'].append(new_line)
                new_line += 1
            elif line.startswith('-'):
                current['removed_lines'].append(old_line)
                old_line += 1
            elif line.startswith(' '):
                current['current_lines'].append(new_line)
                old_line += 1
                new_line += 1
            # The "no newline at end of file" marker advances neither counter.
    return hunks


def eligible(path):
    """Apply the same filename rules as project mode (for example, no secrets.py)."""
    try:
        project_access.relative_parts(path)
        return True
    except ValueError:
        return False


def collect_changes(directory, base='HEAD~1'):
    """Return project access state and JSON-ready changes from base to current files."""
    project = project_access.create_project(directory)
    root = project['root']
    if not (root / '.git').exists():
        raise ValueError('Diff mode requires a Git repository root directory.')
    try:
        commit = run_git(root, ['rev-parse', '--verify', '--end-of-options', base + '^{commit}']).strip()
    except RuntimeError as error:
        if 'failed' not in str(error):  # For example, Git is not installed.
            raise
        raise ValueError(f'Cannot resolve base commit {base!r}. The default base HEAD~1 needs at least '
                         'two commits; use --base HEAD to review only uncommitted changes.') from None
    if run_git(root, ['diff', '--name-only', '--diff-filter=U']).strip():
        raise ValueError('Resolve Git merge conflicts before starting a diff review.')

    # -z separates fields with NUL bytes so unusual filenames are parsed safely.
    fields = run_git(root, ['diff', *DIFF_OPTIONS, '--name-status', '-z', commit, '--', '*.py']).split('\0')[:-1]
    context = {'base': commit, 'comparison': 'base commit to current files (committed, staged, and unstaged)',
               'changes': [], 'changed_count': 0, 'omitted_files': 0,
               'skipped': [], 'skipped_count': 0, 'excluded_count': 0,
               'limitations': list(dict.fromkeys(project['limits_reached']))[:5]}
    index = 0
    while index < len(fields):
        status = fields[index][0]
        old_path = new_path = fields[index + 1]
        index += 2
        if status == 'R':
            new_path = fields[index]
            index += 1
        if status == 'A':
            old_path = None
        if status == 'D':
            new_path = None
        if status not in STATUS_NAMES or not all(eligible(path) for path in (old_path, new_path) if path):
            context['excluded_count'] += 1
            continue
        if new_path is not None and new_path not in project['files']:
            context['skipped'].append({'file': new_path,
                                       'reason': 'Current file is a symlink, excluded, too large, or unreadable.'})
            continue
        context['changed_count'] += 1
        if len(context['changes']) >= MAX_CHANGED_FILES:
            continue
        paths = [path for path in (old_path, new_path) if path]
        patch = run_git(root, ['diff', *DIFF_OPTIONS, '--unified=3', commit, '--', *dict.fromkeys(paths)])
        context['changes'].append({'status': STATUS_NAMES[status], 'file': new_path or old_path,
                                   'old_file': old_path, 'hunks': parse_hunks(patch), 'omitted_hunks': 0})

    context['skipped_count'] = len(context['skipped'])
    context['skipped'] = context['skipped'][:MAX_CHANGED_FILES]
    # Bound the exact serialized context, dropping whole hunks before file records.
    for change in reversed(context['changes']):
        while change['hunks'] and len(json.dumps(context)) > MAX_CHANGE_CHARS - 200:
            change['hunks'].pop()
            change['omitted_hunks'] += 1
    while context['changes'] and len(json.dumps(context)) > MAX_CHANGE_CHARS - 200:
        context['changes'].pop()
    while context['skipped'] and len(json.dumps(context)) > MAX_CHANGE_CHARS - 200:
        context['skipped'].pop()
    context['omitted_files'] = context['changed_count'] - len(context['changes'])
    if context['skipped_count'] or context['omitted_files'] or any(c['omitted_hunks'] for c in context['changes']):
        context['limitations'].append('Change coverage is partial; skipped/omitted code was not reviewed.')
    project['limits_reached'].extend(context['limitations'])
    return project, context
