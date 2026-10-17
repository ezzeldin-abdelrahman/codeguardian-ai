"""Real temporary Git repositories; all model responses are mocked."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codeguardian import agent, git_changes, project, reviewer
from codeguardian.findings import parse_findings, print_findings

FINAL = {'content': '{"findings": []}', 'tool_calls': []}


@unittest.skipUnless(shutil.which('git', path=os.defpath), 'Git is required')
class GitChangesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.git('init')
        self.git('config', 'user.name', 'CodeGuardian Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.write('app.py', 'from helper import value\nprint(value)\n')
        self.write('helper.py', 'value = 1\n')
        self.commit()

    def git(self, *arguments):
        return git_changes.run_git(self.root, list(arguments))

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def commit(self):
        self.git('add', '--all')
        self.git('commit', '-m', 'Test snapshot')

    def collect(self, base='HEAD'):
        return git_changes.collect_changes(self.root, base)

    def test_staged_unstaged_and_combined_current_content(self):
        self.write('helper.py', 'value = 2\n')
        self.git('add', 'helper.py')
        self.write('helper.py', 'value = 3\n')
        self.write('app.py', 'print("unstaged")\n')
        self.write('staged.py', 'staged = True\n')
        self.git('add', 'staged.py')
        _, context = self.collect()
        changes = {c['file']: c for c in context['changes']}
        self.assertEqual(set(changes), {'helper.py', 'app.py', 'staged.py'})
        self.assertEqual(changes['staged.py']['status'], 'added')
        hunk = changes['helper.py']['hunks'][0]
        self.assertIn('-value = 1', hunk['patch'])
        self.assertIn('+value = 3', hunk['patch'])
        self.assertNotIn('value = 2', hunk['patch'])
        self.assertEqual(hunk['added_lines'], [1])
        self.assertEqual(hunk['removed_lines'], [1])

    def test_staged_edit_reversed_on_disk_has_no_net_change(self):
        self.write('helper.py', 'value = 2\n')
        self.git('add', 'helper.py')
        self.write('helper.py', 'value = 1\n')
        self.assertEqual(self.collect()[1]['changed_count'], 0)

    def test_untracked_files_need_git_add_and_secrets_are_excluded(self):
        self.write('new.py', 'new = True\n')
        self.assertEqual(self.collect()[1]['changed_count'], 0)
        self.write('secrets.py', 'sensitive = True\n')
        self.git('add', 'new.py', 'secrets.py')
        access, context = self.collect()
        self.assertEqual([c['file'] for c in context['changes']], ['new.py'])
        self.assertNotIn('secrets.py', access['files'])
        self.assertEqual(context['excluded_count'], 1)

    def test_deletions_and_renames(self):
        (self.root / 'app.py').unlink()
        self.git('mv', 'helper.py', 'renamed.py')
        changes = {c['file']: c for c in self.collect()[1]['changes']}
        self.assertEqual(changes['app.py']['status'], 'deleted')
        self.assertEqual(changes['app.py']['hunks'][0]['removed_lines'], [1, 2])
        self.assertEqual(changes['renamed.py']['status'], 'renamed')
        self.assertEqual(changes['renamed.py']['old_file'], 'helper.py')
        self.assertEqual(changes['renamed.py']['hunks'], [])

    def test_rename_with_content_changes(self):
        original = ''.join(f'value_{n} = {n}\n' for n in range(20))
        self.write('helper.py', original)
        self.commit()
        self.git('mv', 'helper.py', 'renamed.py')
        self.write('renamed.py', original.replace('value_10 = 10', 'value_10 = 99'))
        change = self.collect()[1]['changes'][0]
        self.assertEqual(change['status'], 'renamed')
        self.assertEqual(change['hunks'][0]['added_lines'], [11])

    def test_python_extension_boundary(self):
        self.git('mv', 'helper.py', 'helper.txt')
        self.assertEqual(self.collect()[1]['changes'][0]['status'], 'deleted')

    def test_base_reference_and_no_source_execution(self):
        self.write('helper.py', 'raise RuntimeError("never execute reviewed code")\n')
        self.commit()
        self.assertEqual(self.collect()[1]['changed_count'], 0)
        self.assertEqual(self.collect('HEAD~1')[1]['changed_count'], 1)

    def test_empty_and_non_python_changes(self):
        self.assertEqual(self.collect()[1]['changed_count'], 0)
        self.write('notes.txt', 'changed\n')
        self.assertEqual(self.collect()[1]['changed_count'], 0)

    def test_empty_python_addition_and_deletion(self):
        self.write('empty.py', '')
        self.git('add', 'empty.py')
        change = self.collect()[1]['changes'][0]
        self.assertEqual(change['status'], 'added')
        self.assertEqual(change['hunks'], [])
        self.commit()
        (self.root / 'empty.py').unlink()
        self.assertEqual(self.collect()[1]['changes'][0]['status'], 'deleted')

    def test_failed_comparison_is_not_an_empty_diff(self):
        with self.assertRaisesRegex(RuntimeError, 'failed'):
            git_changes.run_git(self.root, ['diff', '--no-index', '--', 'missing.py', 'absent.py'])

    def test_unusual_filenames_and_missing_final_newline(self):
        name = 'space tab\tnewline\n-name.py'
        self.write(name, 'x = 1')
        self.git('add', '--all')
        change = self.collect()[1]['changes'][0]
        self.assertEqual(change['file'], name)
        self.assertEqual(change['hunks'][0]['added_lines'], [1])
        self.assertIn('No newline', change['hunks'][0]['patch'])

    def test_invalid_base_and_subdirectory(self):
        for base in ('missing-reference', '--help', ''):
            with self.subTest(base=base), self.assertRaisesRegex(ValueError, 'base commit'):
                self.collect(base)
        child = self.root / 'child'
        child.mkdir()
        with self.assertRaisesRegex(ValueError, 'repository root'):
            git_changes.collect_changes(child)

    def test_non_repository_and_unborn_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'repository root'):
                git_changes.collect_changes(directory)
            git_changes.run_git(directory, ['init'])
            with self.assertRaisesRegex(ValueError, 'base commit'):
                git_changes.collect_changes(directory)

    def test_merge_conflict_is_reported(self):
        original_branch = self.git('symbolic-ref', '--short', 'HEAD').strip()
        self.git('branch', 'other')
        self.write('helper.py', 'value = 2\n')
        self.commit()
        self.git('checkout', 'other')
        self.write('helper.py', 'value = 3\n')
        self.commit()
        # A conflicting merge exits with an error, so call Git directly here.
        subprocess.run(['git', '-C', str(self.root), 'merge', '--no-edit', original_branch], capture_output=True)
        with self.assertRaisesRegex(ValueError, 'merge conflicts'):
            self.collect()

    def test_symlink_current_file_is_skipped_not_deleted(self):
        (self.root / 'helper.py').unlink()
        (self.root / 'helper.py').symlink_to(self.root / 'app.py')
        _, context = self.collect()
        self.assertEqual(context['changed_count'], 0)
        self.assertEqual(context['skipped_count'], 1)
        self.assertEqual(context['skipped'][0]['file'], 'helper.py')

    def test_symlinked_directory_content_is_not_sent(self):
        self.write('package/item.py', 'original = True\n')
        self.commit()
        with tempfile.TemporaryDirectory() as outside:
            (Path(outside) / 'item.py').write_text('DO_NOT_SEND_OUTSIDE\n')
            shutil.rmtree(self.root / 'package')
            (self.root / 'package').symlink_to(outside, target_is_directory=True)
            context = self.collect()[1]
        self.assertNotIn('DO_NOT_SEND_OUTSIDE', json.dumps(context))

    def test_deleted_secret_file_is_excluded(self):
        self.write('secrets.py', 'DO_NOT_SEND_SECRET\n')
        self.commit()
        (self.root / 'secrets.py').unlink()
        context = self.collect()[1]
        self.assertEqual(context['excluded_count'], 1)
        self.assertEqual(context['changed_count'], 0)
        self.assertNotIn('DO_NOT_SEND', json.dumps(context))

    def test_large_files_are_skipped(self):
        self.write('helper.py', 'x' * (project.MAX_FILE_BYTES + 1))
        context = self.collect()[1]
        self.assertEqual(context['changed_count'], 0)
        self.assertEqual(context['skipped_count'], 1)

    def test_context_and_changed_file_caps(self):
        for n in range(25):
            self.write(f'new{n:02}.py', ('x = "' + 'a'*200 + '"\n') * 40)
        self.git('add', '--all')
        access, context = self.collect()
        self.assertEqual(context['changed_count'], 25)
        self.assertEqual(len(context['changes']), git_changes.MAX_CHANGED_FILES)
        self.assertEqual(context['omitted_files'], 5)
        self.assertLessEqual(len(json.dumps(context)), git_changes.MAX_CHANGE_CHARS)
        self.assertTrue(any(c['omitted_hunks'] for c in context['changes']))
        project.record_change_locations(access, context)
        for change in context['changes']:
            if not change['hunks'] and change['omitted_hunks']:
                self.assertNotIn(change['file'], access['observed'])

    def test_git_missing_timeout_and_output_limit(self):
        for error, message in [(FileNotFoundError(), 'not installed'),
                               (subprocess.TimeoutExpired('git', 30), 'time limit')]:
            with patch.object(git_changes.subprocess, 'run', side_effect=error), \
                 self.assertRaisesRegex(RuntimeError, message):
                self.collect()

    def test_agent_can_inspect_unchanged_file_and_preserves_evidence(self):
        self.write('app.py', 'from helper import value\nprint(1 / value)\n')
        access, context = self.collect()
        finding = {'category': 'bug', 'severity': 'high', 'title': 'Potential division failure',
                   'file': 'helper.py', 'line': 1, 'description': 'Example cross-file finding.',
                   'recommendation': 'Check the divisor.',
                   'evidence': 'app.py:2 adds division using helper.py:1; illustrative unverified finding.'}
        turns = [{'content': None, 'tool_calls': [{'id': 'inspect', 'type': 'function', 'function': {
                    'name': 'inspect_file', 'arguments': '{"path":"helper.py"}'}}]},
                 {'tool_calls': [], 'content': json.dumps({'findings': [finding]})}]
        with patch.object(agent, 'request_agent_turn', side_effect=turns) as request:
            report = agent.run_agent(self.root, 'model', 'fake', change_context=context, project=access)
        self.assertEqual(report['status'], 'complete', report['error'])
        self.assertEqual(report['findings'][0]['evidence'], finding['evidence'])
        self.assertEqual(report['trace'][0]['tool'], 'inspect_file')
        self.assertEqual(report['changes'], context)
        self.assertEqual(json.loads(request.call_args.args[0][1]['content'])['git_changes'], context)

    def test_deleted_only_agent_and_location_rules(self):
        (self.root / 'app.py').unlink()
        (self.root / 'helper.py').unlink()
        access, context = self.collect()
        finding = {'category': 'bug', 'severity': 'high', 'title': 'Removed entry point',
                   'file': 'app.py', 'line': None, 'description': 'Entry point removed.',
                   'recommendation': 'Update callers.', 'evidence': 'Base app.py:1-2 was removed.'}
        turn = {'tool_calls': [], 'content': json.dumps({'findings': [finding]})}
        with patch.object(agent, 'request_agent_turn', return_value=turn):
            report = agent.run_agent(self.root, 'model', 'fake', change_context=context, project=access)
        self.assertEqual(report['status'], 'complete', report['error'])
        finding['line'] = 1
        with self.assertRaisesRegex(ValueError, 'Deleted-file'):
            project.validate_locations(access, [finding])

    def test_cli_empty_diff_needs_no_api_key_and_saves_trace(self):
        trace = self.root / 'trace.json'
        output = io.StringIO()
        with patch.object(sys, 'argv', ['reviewer.py', str(self.root), '--mode', 'diff', '--base', 'HEAD', '--trace-file', str(trace)]), \
             patch.object(reviewer, 'load_dotenv'), patch.object(reviewer, 'get_settings') as settings, \
             patch.object(reviewer, 'run_agent') as run, redirect_stdout(output):
            reviewer.main()
        settings.assert_not_called()
        run.assert_not_called()
        self.assertIn('No eligible Python changes', output.getvalue())
        self.assertEqual(json.loads(trace.read_text())['changes']['changed_count'], 0)

    def test_cli_diff_reviews_last_commit_by_default(self):
        self.write('helper.py', 'value = 2\n')
        self.commit()
        output = io.StringIO()
        with patch.object(sys, 'argv', ['reviewer.py', str(self.root), '--mode', 'diff']), \
             patch.object(reviewer, 'load_dotenv'), \
             patch.object(reviewer, 'get_settings', return_value=('groq', 'chosen', 'fake')), \
             patch.object(agent, 'request_agent_turn', return_value=FINAL), redirect_stdout(output):
            reviewer.main()
        self.assertIn('Git Diff Review', output.getvalue())
        self.assertIn('modified: helper.py', output.getvalue())

    def test_cli_invalid_options_and_skipped_only_is_not_clean(self):
        for args in ([str(self.root), '--base', 'HEAD'], [str(self.root / 'app.py'), '--mode', 'diff']):
            with patch.object(sys, 'argv', ['reviewer.py', *args]), redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit) as caught:
                reviewer.main()
            self.assertEqual(caught.exception.code, 2)
        (self.root / 'helper.py').unlink()
        (self.root / 'helper.py').symlink_to('app.py')
        with patch.object(sys, 'argv', ['reviewer.py', str(self.root), '--mode', 'diff', '--base', 'HEAD']), \
             patch.object(reviewer, 'load_dotenv'), redirect_stdout(io.StringIO()), \
             self.assertRaises(SystemExit) as caught:
            reviewer.main()
        self.assertEqual(caught.exception.code, 1)


class HunkAndEvidenceTests(unittest.TestCase):
    def test_unicode_separator_does_not_change_git_line_numbers(self):
        hunks = git_changes.parse_hunks('@@ -0,0 +1,2 @@\n+text = "a\u2028b"\n+pass\n')
        self.assertEqual(hunks[0]['added_lines'], [1, 2])

    def test_multiple_hunks_zero_ranges_and_headers_inside_code(self):
        hunks = git_changes.parse_hunks('--- old\n+++ new\n@@ -2,2 +2,2 @@\n-old\n+++new\n same\n'
                                       '@@ -10 +10,0 @@\n-last\n\\ No newline at end of file\n')
        self.assertEqual(hunks[0]['added_lines'], [2])
        self.assertEqual(hunks[0]['current_lines'], [2, 3])
        self.assertEqual(hunks[1]['removed_lines'], [10])
        self.assertEqual(hunks[1]['current_lines'], [])

    def test_evidence_required_only_for_diff_and_printed(self):
        finding = {'category': 'bug', 'severity': 'high', 'title': 'Problem', 'line': 1,
                   'description': 'Description', 'recommendation': 'Fix'}
        text = json.dumps({'findings': [finding]})
        self.assertEqual(len(parse_findings(text)), 1)
        with self.assertRaisesRegex(ValueError, 'evidence'):
            parse_findings(text, require_evidence=True)
        finding['evidence'] = 'Working-tree app.py:1 introduces the problem.'
        output = io.StringIO()
        with redirect_stdout(output):
            print_findings(parse_findings(json.dumps({'findings': [finding]}), require_evidence=True))
        self.assertIn('Evidence: Working-tree app.py:1', output.getvalue())
