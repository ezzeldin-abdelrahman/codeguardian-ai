"""Evidence verification tests. Every LLM response and analyzer result is mocked."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codeguardian import project, reviewer, verifier

SOURCE = '''import sqlite3

def find(name):
    query = f"SELECT * FROM users WHERE name = '{name}'"
    return sqlite3.connect("users.db").execute(query)

def add(a, b):
    return a + b
'''
USAGE = {'input_tokens': 100, 'output_tokens': 20, 'latency_ms': 500}
BANDIT_SQL = [{'source': 'bandit', 'category': 'security', 'severity': 'medium',
               'title': 'B608: hardcoded_sql_expressions', 'line': 4}]


def answer(status, lines=(), reason='Checked the supplied lines.'):
    content = json.dumps({'status': status, 'reason': reason, 'evidence_lines': list(lines)})
    return {'content': content, 'usage': USAGE}


def finding(file='database.py', line=4, category='security', title='SQL injection'):
    return {'category': category, 'severity': 'high', 'title': title, 'file': file, 'line': line,
            'description': 'User input is placed directly in an SQL query.', 'recommendation': 'Use parameters.'}


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        (self.root / 'database.py').write_text(SOURCE)
        self.project = project.create_project(self.root)

    def verify(self, findings, responses, bandit=(), ruff=(), **options):
        """Run the verifier with mocked model answers and mocked Bandit/Ruff results."""
        options.setdefault('project', self.project)
        with patch.object(verifier, 'request_verification', side_effect=responses) as request, \
             patch.object(verifier, 'run_bandit', return_value=list(bandit)), \
             patch.object(verifier, 'run_ruff', return_value=list(ruff)):
            outcome = verifier.verify_findings(findings, self.root, 'model', 'fake', **options)
        return outcome, request

    def test_correct_finding_supported_by_code_and_bandit(self):
        outcome, request = self.verify([finding()], [answer('supported', [4])], bandit=BANDIT_SQL)
        item = outcome['results'][0]
        self.assertEqual(item['finding_id'], 'F001')
        self.assertEqual(item['verification_status'], 'supported')
        self.assertEqual(item['corroborating_tools'], ['bandit'])
        self.assertTrue(item['independent_corroboration'])
        self.assertIn({'file': 'database.py', 'line_start': 4, 'line_end': 4, 'source': 'code'}, item['evidence'])
        self.assertIn({'file': 'database.py', 'line_start': 4, 'line_end': 4, 'source': 'bandit'}, item['evidence'])
        sent = json.loads(request.call_args.args[0][1]['content'])
        self.assertIn('4:     query = f"SELECT', sent['source_lines'])
        self.assertEqual(sent['analyzer_results'][0]['tool'], 'bandit')
        self.assertEqual((outcome['llm_calls'], outcome['input_tokens'], outcome['latency_ms']), (1, 100, 500))

    def test_false_finding_contradicted_without_tool_results(self):
        claim = finding(line=8, category='bug', title='Division by zero in add')
        outcome, _ = self.verify([claim], [answer('unsupported', [8], 'Line 8 adds; it never divides.')])
        item = outcome['results'][0]
        self.assertEqual(item['verification_status'], 'unsupported')
        self.assertEqual(item['corroborating_tools'], [])
        self.assertFalse(item['independent_corroboration'])

    def test_ambiguous_and_uncited_findings_are_uncertain(self):
        outcome, _ = self.verify([finding(), finding()], [answer('uncertain'), answer('supported', [99])])
        self.assertEqual([r['verification_status'] for r in outcome['results']], ['uncertain', 'uncertain'])
        self.assertIn('No cited line', outcome['results'][1]['reason'])
        self.assertEqual(outcome['results'][1]['evidence'], [])

    def test_missing_invalid_and_malformed_locations_make_no_model_call(self):
        cases = [(finding(file='missing.py'), 'unsupported'), (finding(line=999), 'unsupported'),
                 (finding(file=None, line=None), 'uncertain'), (finding(line=0), 'uncertain'),
                 ({'title': 'No fields'}, 'uncertain')]
        outcome, request = self.verify([case for case, _ in cases], [])
        self.assertEqual([r['verification_status'] for r in outcome['results']], [s for _, s in cases])
        self.assertIn('does not exist', outcome['results'][1]['reason'])
        request.assert_not_called()
        self.assertEqual(outcome['llm_calls'], 0)

    def test_file_access_restrictions_are_kept(self):
        with tempfile.TemporaryDirectory() as outside:
            (Path(outside) / 'outside.py').write_text('DO_NOT_READ = True\n')
            (self.root / 'linked.py').symlink_to(Path(outside) / 'outside.py')
            (self.root / 'secrets.py').write_text('DO_NOT_READ = True\n')
            current = project.create_project(self.root)
            claims = [finding(file=name, line=1) for name in ('linked.py', 'secrets.py', '../outside.py')]
            outcome, request = self.verify(claims, [], project=current)
        self.assertEqual({r['verification_status'] for r in outcome['results']}, {'unsupported'})
        request.assert_not_called()

    def test_tool_conflict_downgrades_unsupported_to_uncertain(self):
        outcome, _ = self.verify([finding()], [answer('unsupported', [4])], bandit=BANDIT_SQL)
        item = outcome['results'][0]
        self.assertEqual(item['verification_status'], 'uncertain')
        self.assertIn('Conflict: bandit', item['reason'])

    def test_tools_only_match_nearby_lines_and_matching_categories(self):
        far_away = [dict(BANDIT_SQL[0], line=20)]
        outcome, _ = self.verify([finding(), finding(category='testing')],
                                 [answer('supported', [4]), answer('supported', [4])], bandit=far_away)
        self.assertEqual([r['corroborating_tools'] for r in outcome['results']], [[], []])

    def test_model_failure_timeout_and_invalid_json_are_uncertain(self):
        responses = [RuntimeError('OpenAI verification request timed out.'),
                     {'content': 'not json', 'usage': USAGE},
                     {'content': json.dumps({'status': 'maybe', 'reason': 'x', 'evidence_lines': []}), 'usage': USAGE}]
        outcome, _ = self.verify([finding()] * 3, responses)
        self.assertEqual({r['verification_status'] for r in outcome['results']}, {'uncertain'})
        self.assertIn('timed out', outcome['results'][0]['reason'])
        self.assertEqual(outcome['llm_calls'], 3)

    def test_git_diff_changed_line_and_deleted_file(self):
        hunk = {'patch': '@@ -4 +4 @@\n-    query = "SELECT 1"\n+    query = f"SELECT"\n',
                'added_lines': [4], 'removed_lines': [4], 'current_lines': [4]}
        removed = {'patch': '@@ -1,2 +0,0 @@\n-import os\n-os.system(cmd)\n',
                   'added_lines': [], 'removed_lines': [1, 2], 'current_lines': []}
        changes = {'changes': [
            {'status': 'modified', 'file': 'database.py', 'old_file': 'database.py', 'hunks': [hunk]},
            {'status': 'deleted', 'file': 'old.py', 'old_file': 'old.py', 'hunks': [removed]}]}
        self.project['deleted_observed'] = {'old.py'}
        deleted = finding(file='old.py', line=None, title='Removed command runner')
        outcome, request = self.verify([finding(), deleted], [answer('supported', [4]), answer('supported', [2])],
                                       change_context=changes)
        first = json.loads(request.call_args_list[0].args[0][1]['content'])
        self.assertIn('+    query = f"SELECT"', first['diff_patch'])
        self.assertEqual(outcome['results'][1]['verification_status'], 'supported')
        self.assertEqual(outcome['results'][1]['evidence'],
                         [{'file': 'old.py', 'line_start': 2, 'line_end': 2, 'source': 'diff'}])

    def test_single_file_mode(self):
        claim = finding()
        del claim['file']
        with patch.object(verifier, 'request_verification', return_value=answer('supported', [4, 5])), \
             patch.object(verifier, 'run_bandit', return_value=[]), patch.object(verifier, 'run_ruff', return_value=[]):
            outcome = verifier.verify_findings([claim], self.root / 'database.py', 'model', 'fake')
        self.assertEqual(outcome['results'][0]['evidence'][0]['line_start'], 4)
        self.assertEqual(outcome['results'][0]['evidence'][0]['line_end'], 5)

    def test_call_limit_and_context_size_limit(self):
        (self.root / 'database.py').write_text(('x = "' + 'a' * 300 + '"\n') * 50)
        outcome, request = self.verify([finding(line=25)] * 12, [answer('uncertain')] * 10)
        self.assertEqual(request.call_count, verifier.MAX_VERIFIED_FINDINGS)
        self.assertIn('limit reached', outcome['results'][-1]['reason'])
        sent = json.loads(request.call_args.args[0][1]['content'])
        self.assertLessEqual(len(sent['source_lines']), verifier.MAX_EVIDENCE_CHARS)

    def test_original_findings_are_not_changed(self):
        claims = [finding()]
        before = json.dumps(claims)
        self.verify(claims, [answer('supported', [4])])
        self.assertEqual(json.dumps(claims), before)

    def test_cli_verify_requires_agent(self):
        with patch.object(sys, 'argv', ['reviewer.py', str(self.root / 'database.py'), '--verify']), \
             redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            reviewer.main()
        self.assertEqual(caught.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
