"""Prepare bounded Git change context without executing reviewed code or Git filters."""

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

if __package__:
    from . import project as project_access
else:
    import project as project_access

MAX_CHANGED_FILES = 20
MAX_CHANGE_CHARS = 12_000
MAX_GIT_OUTPUT = 2_000_000


def run_git(directory, arguments, *, differences=False):
    """Run fixed Git operations; output goes to bounded-size-checked temporary files."""
    environment = {
        'PATH': os.defpath, 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0',
        'GIT_NO_LAZY_FETCH': '1', 'GIT_NO_REPLACE_OBJECTS': '1',
        'GIT_CEILING_DIRECTORIES': str(Path(directory).parent),
    }
    command = ['git', '--no-pager', '-c', 'core.fsmonitor=false',
               '-c', f'core.hooksPath={os.devnull}', '-c', 'protocol.allow=never',
               '-C', str(directory), *arguments]
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            result = subprocess.run(command, stdout=output, stderr=errors,
                                    env=environment, timeout=30, check=False)
            if output.tell() > MAX_GIT_OUTPUT or errors.tell() > MAX_GIT_OUTPUT:
                raise RuntimeError('Git output exceeded the collection limit; review a smaller repository.')
            if result.returncode not in ((0, 1) if differences else (0,)):
                # Do not forward arbitrary Git diagnostics/configuration to the model.
                raise RuntimeError(f'Git {arguments[0]} failed (exit {result.returncode}).')
            if result.returncode == 1 and errors.tell() and not output.tell():
                raise RuntimeError(f'Git {arguments[0]} failed before producing a comparison.')
            output.seek(0)
            return output.read()
    except FileNotFoundError:
        raise RuntimeError('Git is not installed or is unavailable on the system PATH.') from None
    except subprocess.TimeoutExpired:
        raise RuntimeError('Git exceeded the 30-second time limit.') from None


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


def missing_working_file(project, path):
    """Distinguish an actual deletion from an inaccessible or excluded current path."""
    parts = project_access.relative_parts(path)
    descriptor = project_access.open_root(project['root'])
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        return False
    except FileNotFoundError:
        return True
    except OSError:
        return False
    finally:
        os.close(descriptor)


def collect_changes(directory, base='HEAD'):
    """Return project access state and JSON-ready changes from base to current files."""
    project = project_access.create_project(directory)
    root = project['root']
    # External Git metadata is deliberately outside this first version's boundary.
    if (root / '.git').is_symlink() or not (root / '.git').is_dir():
        raise ValueError('Diff mode requires a repository root with a local .git directory (not a linked worktree).')
    top = run_git(root, ['rev-parse', '--show-toplevel']).decode('utf-8').strip()
    if Path(top).resolve() != root:
        raise ValueError('Select the Git repository root for diff mode.')
    try:
        commit = run_git(root, ['rev-parse', '--verify', '--end-of-options', base + '^{commit}']).decode().strip()
    except RuntimeError as error:
        raise ValueError(f'Cannot resolve base commit {base!r}. Check the reference and create an initial commit if needed. {error}') from None

    tree_records = run_git(root, ['ls-tree', '-r', '-l', '-z', commit]).split(b'\0')[:-1]
    index_records = run_git(root, ['ls-files', '--stage', '-z']).split(b'\0')[:-1]
    untracked_records = run_git(root, ['ls-files', '--others', '--exclude-standard', '-z']).split(b'\0')[:-1]
    if max(len(tree_records), len(index_records) + len(untracked_records)) > project_access.MAX_ENTRIES:
        raise ValueError('Git file inventory exceeds the 2,000-entry limit.')

    baseline = {}
    virtualenvs = set()
    for record in tree_records:
        metadata, raw_path = record.split(b'\t', 1)
        path = raw_path.decode('utf-8')
        mode, kind, object_id, size = metadata.split()
        baseline[path] = (mode, kind, object_id.decode(), size)
        if path.split('/')[-1] == 'pyvenv.cfg':
            virtualenvs.add(path.rpartition('/')[0])
    tracked = set()
    for record in index_records:
        metadata, raw_path = record.split(b'\t', 1)
        if metadata.split()[2] != b'0':
            raise ValueError('Resolve Git merge conflicts before starting a diff review.')
        tracked.add(raw_path.decode('utf-8'))
    untracked = {path.decode('utf-8') for path in untracked_records}
    # In diff mode, Git-ignored untracked files are also unavailable as related context.
    project['files'] = sorted(set(project['files']) & (tracked | untracked))
    candidates = []
    excluded = 0
    for path in sorted(set(baseline) | tracked | untracked):
        if not path.endswith('.py'):
            continue
        try:
            project_access.relative_parts(path)
            if any(not folder or path.startswith(folder + '/') for folder in virtualenvs):
                raise ValueError('Virtual environment file.')
        except ValueError:
            excluded += 1
            continue
        candidates.append(path)
    if len(candidates) > project_access.MAX_FILES:
        raise ValueError('Git review exceeds the 100-file collection limit.')

    context = {'base': commit, 'comparison': 'base commit to working tree, including untracked files',
               'changes': [], 'changed_count': 0, 'omitted_files': 0,
               'skipped': [], 'skipped_count': 0, 'excluded_count': excluded,
               'limitations': list(dict.fromkeys(project['limits_reached']))[:5]}
    project['snapshot_hashes'] = {}
    with tempfile.TemporaryDirectory(prefix='codeguardian-diff-') as temporary:
        workspace = Path(temporary)
        before, after = workspace / 'before', workspace / 'after'
        before.mkdir()
        after.mkdir()
        empty = workspace / 'empty'
        empty.write_bytes(b'')
        for path in candidates:
            try:
                old = None
                if path in baseline:
                    mode, kind, object_id, size = baseline[path]
                    if mode not in (b'100644', b'100755') or kind != b'blob':
                        raise ValueError('Baseline is not a regular file.')
                    if int(size) > project_access.MAX_FILE_BYTES:
                        raise ValueError('Baseline exceeds the source-size limit.')
                    old = run_git(root, ['cat-file', 'blob', object_id]).decode('utf-8')
                new = None
                if path in project['files']:
                    new = project_access.read_project_file(project, path)
                elif not missing_working_file(project, path):
                    raise ValueError('Current file is excluded, inaccessible, or exceeds discovery limits.')
                if any(text is not None and '\x00' in text for text in (old, new)):
                    raise ValueError('Binary source containing NUL bytes is not eligible.')
                if new is not None:
                    project['snapshot_hashes'][path] = hashlib.sha256(new.encode('utf-8')).hexdigest()
                # Write only after both versions passed checks; a skipped file is never a deletion.
                for destination, text in ((before / path, old), (after / path, new)):
                    if text is not None:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(text.encode('utf-8'))
            except (ValueError, RuntimeError, OSError, UnicodeError) as error:
                context['skipped'].append({'file': path, 'reason': str(error)})

        options = ['--no-index', '--no-ext-diff', '--no-textconv', '--no-color', '--find-renames']
        records = run_git(workspace, ['diff', *options, '--name-status', '-z', '--',
                                     'before', 'after'], differences=True).split(b'\0')[:-1]
        index = 0
        while index < len(records):
            status = records[index].decode()
            first = records[index + 1].decode('utf-8')
            index += 2
            second = first
            if status.startswith('R'):
                second = records[index].decode('utf-8')
                index += 1
            old_path = first.removeprefix('before/') if status[0] != 'A' else None
            new_path = second.removeprefix('after/') if status[0] != 'D' else None
            # A no-index modification uses its old-side path in name-status output.
            if status[0] == 'M':
                new_path = old_path
            for path in (old_path, new_path):
                if path is not None:
                    project_access.relative_parts(path)
                    if path not in candidates:
                        raise ValueError('Git returned an unexpected snapshot path.')
            context['changed_count'] += 1
            if len(context['changes']) >= MAX_CHANGED_FILES:
                continue
            old_file = before / old_path if old_path else empty
            new_file = after / new_path if new_path else empty
            patch = run_git(workspace, ['diff', *options, '--unified=3', '--',
                                       str(old_file), str(new_file)], differences=True).decode('utf-8')
            context['changes'].append({
                'status': {'A': 'added', 'M': 'modified', 'D': 'deleted', 'R': 'renamed'}[status[0]],
                'file': new_path or old_path, 'old_file': old_path,
                'untracked': new_path in untracked, 'hunks': parse_hunks(patch), 'omitted_hunks': 0,
            })

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
