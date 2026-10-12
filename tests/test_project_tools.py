import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codeguardian import project, tools
from codeguardian.findings import parse_findings


class ProjectToolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        (self.root / 'app.py').write_text('from auth import validate_user\n\nvalidate_user("Ada")\n')
        (self.root / 'auth.py').write_text('def validate_user(name):\n    return bool(name)\n')
        self.project = project.create_project(self.root)

    def test_listing_inspection_and_search(self):
        self.assertEqual(tools.list_files(self.project)['files'], ['app.py', 'auth.py'])
        observation = tools.inspect_file(self.project, 'app.py', 2, 3)
        self.assertEqual(observation['lines'], [
            {'line': 2, 'text': ''}, {'line': 3, 'text': 'validate_user("Ada")'}])
        matches = tools.search_code(self.project, 'validate_user')['matches']
        self.assertEqual([(item['file'], item['line']) for item in matches],
                         [('app.py', 1), ('app.py', 3), ('auth.py', 1)])
        self.assertEqual(tools.search_code(self.project, 'VALIDATE_USER')['matches'], [])

    def test_excluded_names_and_arbitrary_virtualenv(self):
        for name in ['.git', '.venv', 'venv', 'node_modules', '__pycache__', 'build', 'secrets']:
            directory = self.root / name
            directory.mkdir()
            (directory / 'hidden.py').write_text('TOKEN = "private"')
        (self.root / 'special_environment').mkdir()
        (self.root / 'special_environment' / 'pyvenv.cfg').write_text('')
        (self.root / 'special_environment' / 'hidden.py').write_text('pass')
        for name in ['.env', '.env.py', 'secrets.py', 'credentials.py', 'private_key.py', 'notes.txt']:
            (self.root / name).write_text('private')
        current = project.create_project(self.root)
        self.assertEqual(current['files'], ['app.py', 'auth.py'])
        for path in ['secrets.py', '.git/hidden.py', 'special_environment/hidden.py', 'notes.txt']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                tools.inspect_file(current, path)

    def test_invalid_and_missing_paths(self):
        for path in ['../outside.py', '/etc/passwd', str(self.root / 'app.py'),
                     'a/../../app.py', './app.py', 'a\\app.py', '', 'missing.py', None, 1]:
            with self.subTest(path=path), self.assertRaises((ValueError, RuntimeError)):
                tools.inspect_file(self.project, path)
        (self.root / 'app.py').unlink()
        with self.assertRaisesRegex(RuntimeError, 'missing'):
            tools.inspect_file(self.project, 'app.py')

    def test_symlinks_and_swaps_are_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / 'outside.py'
            external.write_text('DO_NOT_READ = True')
            (self.root / 'external.py').symlink_to(external)
            (self.root / 'internal.py').symlink_to(self.root / 'auth.py')
            (self.root / 'linked_dir').symlink_to(outside, target_is_directory=True)
            current = project.create_project(self.root)
            self.assertEqual(current['files'], ['app.py', 'auth.py'])
            for name in ['external.py', 'internal.py', 'linked_dir/outside.py']:
                with self.assertRaises(ValueError):
                    tools.inspect_file(current, name)
            # Swap a previously eligible regular file for a symlink after discovery.
            (self.root / 'app.py').unlink()
            (self.root / 'app.py').symlink_to(external)
            with self.assertRaises(RuntimeError):
                tools.inspect_file(current, 'app.py')
            result = tools.search_code(current, 'DO_NOT_READ')
            self.assertEqual(result['matches'], [])
            self.assertTrue(result['errors'])

    def test_directory_symlink_swap_and_root_symlink(self):
        package = self.root / 'package'
        package.mkdir()
        (package / 'a.py').write_text('pass')
        current = project.create_project(self.root)
        package.rename(self.root / 'moved')
        package.symlink_to(self.root / 'moved', target_is_directory=True)
        with self.assertRaises(RuntimeError):
            tools.inspect_file(current, 'package/a.py')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            project.create_project(package)

    def test_hard_links_and_non_regular_files(self):
        os.link(self.root / 'auth.py', self.root / 'linked.py')
        os.mkfifo(self.root / 'pipe.py')
        current = project.create_project(self.root)
        self.assertEqual(current['files'], ['app.py'])
        (self.root / 'app.py').unlink()
        os.mkfifo(self.root / 'app.py')
        with self.assertRaises(RuntimeError):
            tools.inspect_file(current, 'app.py')

    def test_permissions_and_invalid_encoding(self):
        with patch('codeguardian.project.os.open', side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(RuntimeError, 'unreadable'):
                tools.inspect_file(self.project, 'app.py')
        (self.root / 'app.py').write_bytes(b'\xff')
        with self.assertRaisesRegex(RuntimeError, 'non-UTF-8'):
            tools.inspect_file(self.project, 'app.py')

    def test_line_file_search_and_discovery_limits(self):
        for start, end in [(0, None), (True, None), (1, False), (3, 2), (1, 99), (99, None)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                tools.inspect_file(self.project, 'app.py', start, end)
        with patch.object(project, 'MAX_INSPECTED_FILES', 1):
            tools.inspect_file(self.project, 'app.py')
            tools.inspect_file(self.project, 'app.py')
            with self.assertRaisesRegex(ValueError, 'file limit'):
                tools.inspect_file(self.project, 'auth.py')
        (self.root / 'app.py').write_text('match\n' * 150)
        observation = tools.inspect_file(self.project, 'app.py')
        self.assertEqual(len(observation['lines']), 100)
        self.assertTrue(observation['truncated'])
        for _ in range(3):
            self.assertEqual(len(tools.search_code(self.project, 'match')['matches']), 20)
        with self.assertRaisesRegex(ValueError, 'search-result limit'):
            tools.search_code(self.project, 'match')
        with patch.object(project, 'MAX_FILES', 1):
            limited = project.create_project(self.root)
            self.assertEqual(len(limited['files']), 1)
            self.assertTrue(limited['limits_reached'])
        with patch.object(project, 'MAX_ENTRIES', 1):
            self.assertTrue(project.create_project(self.root)['limits_reached'])
        (self.root / 'large.py').write_text('x' * (project.MAX_FILE_BYTES + 1))
        self.assertNotIn('large.py', project.create_project(self.root)['files'])
        (self.root / 'app.py').write_text('x' * (project.MAX_FILE_BYTES + 1))
        with self.assertRaisesRegex(RuntimeError, 'size limit'):
            tools.inspect_file(self.project, 'app.py')

    def test_observation_limit_and_locations(self):
        (self.root / 'app.py').write_text('x' * 9000 + '\n')
        result = tools.inspect_file(self.project, 'app.py')
        self.assertLessEqual(len(json.dumps(result)), project.MAX_OBSERVATION_CHARS)
        self.assertTrue(result['truncated'])
        project.record_locations(self.project, result)
        self.assertNotIn('app.py', self.project['observed'])
        result = tools.inspect_file(self.project, 'auth.py', 1, 1)
        project.record_locations(self.project, result)
        project.validate_locations(self.project, [{'file': 'auth.py', 'line': 1}])
        for finding in [{'file': 'auth.py', 'line': 2}, {'file': 'app.py', 'line': None},
                        {'file': None, 'line': 1}, {'line': None}]:
            with self.assertRaises(ValueError):
                project.validate_locations(self.project, [finding])

    def test_analyzers_use_isolated_snapshots(self):
        bandit_report = {'errors': [], 'results': [{
            'issue_severity': 'LOW', 'test_id': 'B101', 'test_name': 'assert_used',
            'line_number': 1, 'issue_text': 'Use of assert detected.'}]}
        ruff_report = [{'code': 'F401', 'message': 'Unused import', 'location': {'row': 1}, 'fix': None}]
        for run, tool_name, output in [(tools.run_bandit, 'bandit', bandit_report),
                                       (tools.run_ruff, 'ruff', ruff_report)]:
            def fake_run(command, **options):
                self.assertIn('-I', command)
                self.assertIn('--ini' if tool_name == 'bandit' else '--isolated', command)
                snapshot = Path(command[-1])
                self.assertNotEqual(snapshot.parent, self.root)
                self.assertEqual(snapshot.read_text(), (self.root / 'app.py').read_text())
                self.assertEqual(options['cwd'], str(snapshot.parent))
                self.assertNotIn('PYTHONPATH', options['env'])
                return subprocess.CompletedProcess(command, 1, json.dumps(output), '')
            with self.subTest(tool=tool_name), patch('codeguardian.tools.subprocess.run', side_effect=fake_run):
                result = run('app.py', project=self.project)
            self.assertEqual(result['findings'][0]['file'], 'app.py')
            parse_findings(json.dumps({'findings': result['findings']}))
        with patch('codeguardian.tools.subprocess.run') as execute:
            with self.assertRaises(ValueError):
                tools.run_bandit('../outside.py', project=self.project)
            execute.assert_not_called()


    def test_empty_file_and_optional_finding_location(self):
        (self.root / 'empty.py').write_text('')
        current = project.create_project(self.root)
        result = tools.inspect_file(current, 'empty.py')
        self.assertEqual(result['lines'], [])
        self.assertFalse(result['truncated'])
        finding = dict(category='testing', severity='low', title='Add tests', line=None,
                       description='Add coverage', recommendation='Write a test')
        for file in ['auth.py', None]:
            result = parse_findings(json.dumps({'findings': [dict(finding, file=file)]}))
            self.assertEqual(result[0]['file'], file)
        for file in [True, 4, [], '', '../auth.py', '/auth.py']:
            with self.subTest(file=file), self.assertRaises(ValueError):
                parse_findings(json.dumps({'findings': [dict(finding, file=file)]}))
