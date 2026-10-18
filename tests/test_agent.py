import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codeguardian import agent, reviewer
from codeguardian.providers import request_agent_turn
from codeguardian.tools import inspect_code


def tool_turn(name='inspect_code', arguments='{}'):
    return {'content': None, 'tool_calls': [{'id': 'call_1', 'type': 'function',
            'function': {'name': name, 'arguments': arguments}}]}


FINAL = {'content': '{"findings": []}', 'tool_calls': []}


class AgentTests(unittest.TestCase):
    def test_openai_is_default_agent_endpoint_and_finalization_disables_tools(self):
        message = SimpleNamespace(tool_calls=[], content='{"findings": []}', reasoning='private')
        with patch('openai.OpenAI') as factory:
            client = factory.return_value.__enter__.return_value
            client.chat.completions.create.return_value = SimpleNamespace(choices=[
                SimpleNamespace(message=message, finish_reason='stop')])
            result = request_agent_turn([], agent.TOOLS, 'configured-model', 'fake', False)
            args = client.chat.completions.create.call_args.kwargs
        self.assertEqual(factory.call_args.kwargs['base_url'], 'https://api.openai.com/v1')
        self.assertEqual(args['model'], 'configured-model')
        self.assertEqual(args['tool_choice'], 'none')
        self.assertFalse(args['parallel_tool_calls'])
        self.assertNotIn('extra_body', args)
        self.assertNotIn('private', json.dumps(result))

    def test_observation_feedback_and_no_automatic_tools(self):
        with patch.object(agent, 'request_agent_turn', side_effect=[tool_turn(), FINAL]) as request, \
             patch.object(agent, 'inspect_code', return_value='1: x = 1'), \
             patch.object(agent, 'run_bandit') as bandit, patch.object(agent, 'run_ruff') as ruff:
            result = agent.run_agent(__file__, 'chosen', 'fake')
        self.assertEqual(result['status'], 'complete')
        self.assertTrue(result['trace'][0]['executed'])
        self.assertEqual(result['trace'][0]['observation']['code'], '1: x = 1')
        messages = request.call_args.args[0]
        self.assertEqual(messages[-1]['tool_call_id'], 'call_1')
        self.assertEqual(json.loads(messages[-1]['content'])['code'], '1: x = 1')
        bandit.assert_not_called()
        ruff.assert_not_called()

    def test_tool_failure_becomes_observation(self):
        with patch.object(agent, 'request_agent_turn', side_effect=[tool_turn('run_bandit'), FINAL]), \
             patch.object(agent, 'run_bandit', side_effect=RuntimeError('Not installed')):
            result = agent.run_agent(__file__, 'chosen', 'fake')
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['trace'][0]['observation'], {'ok': False, 'error': 'Not installed'})

    def test_invalid_requests_do_not_execute_tools(self):
        multiple = tool_turn()
        multiple['tool_calls'] += [dict(multiple['tool_calls'][0], id='call_2')]
        for turn in [tool_turn('shell'), tool_turn(arguments='{"path":"/etc/passwd"}'),
                     tool_turn(arguments='bad json'), tool_turn(arguments='[]'), multiple]:
            with self.subTest(turn=turn), \
                 patch.object(agent, 'request_agent_turn', side_effect=[turn, FINAL]), \
                 patch.object(agent, 'inspect_code') as inspect:
                result = agent.run_agent(__file__, 'chosen', 'fake')
                inspect.assert_not_called()
                self.assertTrue(all(not event['executed'] for event in result['trace']))
                self.assertTrue(all(not event['observation']['ok'] for event in result['trace']))

    def test_step_limit_and_finalization(self):
        with patch.object(agent, 'request_agent_turn', side_effect=[tool_turn('run_ruff')]*5 + [FINAL]) as request, \
             patch.object(agent, 'run_ruff', return_value=[]) as ruff:
            result = agent.run_agent(__file__, 'chosen', 'fake')
        self.assertEqual(request.call_count, 6)
        self.assertFalse(request.call_args.args[-1])
        self.assertEqual(ruff.call_count, 5)
        self.assertEqual(len(result['trace']), 5)
        self.assertEqual(result['status'], 'complete')

    def test_incomplete_preserves_trace(self):
        for ending in [RuntimeError('API unavailable'), {'tool_calls': [], 'content': 'bad json'}]:
            with patch.object(agent, 'request_agent_turn', side_effect=[tool_turn(), ending]), \
                 patch.object(agent, 'inspect_code', return_value='1: pass'):
                result = agent.run_agent(__file__, 'chosen', 'fake')
            self.assertEqual(result['status'], 'incomplete')
            self.assertEqual(len(result['trace']), 1)
            self.assertTrue(result['error'])

    def test_final_findings_are_normalized(self):
        finding = dict(category='bug', severity='high', title='Problem', line=1,
                       description='Description', recommendation='Fix it', source='bandit', extra='discard')
        with patch.object(agent, 'request_agent_turn', return_value={
                'tool_calls': [], 'content': json.dumps({'findings': [finding]})}):
            result = agent.run_agent(__file__, 'chosen', 'fake')
        self.assertEqual(result['findings'][0]['source'], 'llm')
        self.assertNotIn('extra', result['findings'][0])

    def test_provider_omits_reasoning_and_sets_limits(self):
        call = SimpleNamespace(id='id', type='function', function=SimpleNamespace(name='inspect_code', arguments='{}'))
        message = SimpleNamespace(tool_calls=[call], content='private commentary', reasoning='private reasoning')
        with patch('openai.OpenAI') as factory:
            client = factory.return_value.__enter__.return_value
            client.chat.completions.create.return_value = SimpleNamespace(choices=[
                SimpleNamespace(message=message, finish_reason='tool_calls')])
            result = request_agent_turn([], agent.TOOLS, 'chosen', 'fake', provider='groq')
            args = client.chat.completions.create.call_args.kwargs
        self.assertEqual(args['extra_body'], {'include_reasoning': False})
        self.assertFalse(args['parallel_tool_calls'])
        self.assertEqual(factory.call_args.kwargs['max_retries'], 0)
        self.assertNotIn('private', json.dumps(result))

    def test_inspect_code_and_cli_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'sample.py'
            source.write_text('x = 1\n\nprint(x)\n')
            self.assertEqual(inspect_code(source), '1: x = 1\n2: \n3: print(x)')
            trace = Path(directory)/'trace.json'
            report = {'status': 'incomplete', 'trace': [], 'findings': [], 'error': 'API unavailable'}
            with patch.object(sys, 'argv', ['reviewer.py', str(source), '--agent', '--trace-file', str(trace)]), \
                 patch.object(reviewer, 'load_dotenv'), \
                 patch.object(reviewer, 'get_settings', return_value=('groq', 'chosen', 'fake')), \
                 patch.object(reviewer, 'run_agent', return_value=report), redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    reviewer.main()
            self.assertEqual(json.loads(trace.read_text()), report)


class ProjectAgentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'app.py').write_text('from auth import validate_user\nvalidate_user("Ada")\n')
        (self.root / 'auth.py').write_text('def validate_user(name):\n    return True\n')

    def project_turn(self, name, arguments):
        return tool_turn(name, json.dumps(arguments))

    def final_finding(self, file='auth.py', line=2):
        return {'tool_calls': [], 'content': json.dumps({'findings': [{
            'category': 'bug', 'severity': 'high', 'title': 'Always accepts users',
            'file': file, 'line': line, 'description': 'Validation always returns True.',
            'recommendation': 'Check the supplied name.'}]})}

    def test_related_file_investigation_and_trace(self):
        responses = [self.project_turn('list_files', {}),
                     self.project_turn('inspect_file', {'path': 'app.py'}),
                     self.project_turn('search_code', {'query': 'validate_user'}),
                     self.project_turn('inspect_file', {'path': 'auth.py', 'start_line': 1, 'end_line': 2}),
                     self.final_finding()]
        with patch.object(agent, 'request_agent_turn', side_effect=responses), \
             patch.object(agent, 'run_bandit') as bandit, patch.object(agent, 'run_ruff') as ruff:
            report = agent.run_agent(self.root, 'chosen', 'fake')
        self.assertEqual(report['status'], 'complete', report['error'])
        self.assertEqual(report['findings'][0]['file'], 'auth.py')
        self.assertEqual(report['findings'][0]['line'], 2)
        self.assertEqual(report['findings'][0]['source'], 'llm')
        self.assertEqual([event['tool'] for event in report['trace']],
                         ['list_files', 'inspect_file', 'search_code', 'inspect_file'])
        self.assertEqual(report['coverage']['observed_files'], ['app.py', 'auth.py'])
        bandit.assert_not_called()
        ruff.assert_not_called()

    def test_findings_citing_unseen_code_are_removed(self):
        for file, line in [('auth.py', 2), ('app.py', 1), (None, 1)]:
            with self.subTest(file=file, line=line), patch.object(agent, 'request_agent_turn', side_effect=[
                    self.project_turn('inspect_file', {'path': 'auth.py', 'start_line': 1, 'end_line': 1}),
                    self.final_finding(file, line)]):
                report = agent.run_agent(self.root, 'chosen', 'fake')
            self.assertEqual(report['status'], 'complete')
            self.assertEqual(report['findings'], [])
            self.assertEqual(len(report['rejected_findings']), 1)

    def test_good_findings_are_kept_when_another_is_removed(self):
        good = json.loads(self.final_finding('auth.py', 1)['content'])['findings'][0]
        bad = dict(good, title='Unseen line', line=2)
        with patch.object(agent, 'request_agent_turn', side_effect=[
                self.project_turn('inspect_file', {'path': 'auth.py', 'start_line': 1, 'end_line': 1}),
                {'tool_calls': [], 'content': json.dumps({'findings': [good, bad]})}]):
            report = agent.run_agent(self.root, 'chosen', 'fake')
        self.assertEqual(report['status'], 'complete')
        self.assertEqual([item['line'] for item in report['findings']], [1])
        self.assertEqual(report['rejected_findings'][0]['title'], 'Unseen line')

    def test_invalid_finding_path_fails_the_review(self):
        with patch.object(agent, 'request_agent_turn', side_effect=[
                self.project_turn('inspect_file', {'path': 'auth.py', 'start_line': 1, 'end_line': 1}),
                self.final_finding('../outside.py', 1)]):
            report = agent.run_agent(self.root, 'chosen', 'fake')
        self.assertEqual(report['status'], 'incomplete')
        self.assertTrue(report['error'])

    def test_invalid_arguments_fail_as_observations(self):
        for name, arguments in [('inspect_file', {'path': '../outside.py'}),
                                ('inspect_file', {'path': 'missing.py'}),
                                ('inspect_file', {}), ('run_ruff', {'path': '/etc/passwd'}),
                                ('list_files', {'extra': 'argument'}), ('search_code', {'query': ''})]:
            with self.subTest(name=name, arguments=arguments), patch.object(agent, 'request_agent_turn', side_effect=[
                    self.project_turn(name, arguments), FINAL]):
                report = agent.run_agent(self.root, 'chosen', 'fake')
            self.assertEqual(report['status'], 'complete')
            self.assertFalse(report['trace'][0]['observation']['ok'])
            self.assertTrue(report['coverage']['limits_reached'])

    def test_project_analyzer_failure_and_line_evidence(self):
        finding = dict(source='ruff', file='auth.py', line=2, category='quality', severity='low',
                       title='Problem', description='Problem', recommendation='Fix')
        with patch.object(agent, 'request_agent_turn', side_effect=[
                self.project_turn('run_bandit', {'path': 'auth.py'}),
                self.project_turn('run_ruff', {'path': 'auth.py'}), self.final_finding()]), \
             patch.object(agent, 'run_bandit', side_effect=RuntimeError('Tool failed')), \
             patch.object(agent, 'run_ruff', return_value={'ok': True, 'path': 'auth.py', 'findings': [finding]}):
            report = agent.run_agent(self.root, 'chosen', 'fake')
        self.assertEqual(report['status'], 'complete')
        self.assertFalse(report['trace'][0]['observation']['ok'])
        self.assertEqual(report['coverage']['observed_files'], ['auth.py'])

    def test_project_step_limit(self):
        with patch.object(agent, 'request_agent_turn', side_effect=[
                self.project_turn('list_files', {})] * 10 + [FINAL]) as request:
            report = agent.run_agent(self.root, 'chosen', 'fake')
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(request.call_count, 11)
        self.assertFalse(request.call_args.args[-1])
        self.assertIn('Investigation step limit reached.', report['coverage']['limits_reached'])

    def test_context_limit_forces_early_finalization(self):
        from codeguardian import project
        (self.root / 'auth.py').write_text(('a' * 150 + '\n') * 100)
        requests = []
        def respond(messages, tools, model, api_key, allow_tools, *, provider="openai"):
            requests.append((json.loads(json.dumps(messages)), allow_tools))
            self.assertLessEqual(agent.conversation_size(messages, tools), project.MAX_CONTEXT_CHARS)
            return self.project_turn('inspect_file', {'path': 'auth.py'}) if allow_tools else FINAL
        with patch.object(agent, 'request_agent_turn', side_effect=respond):
            report = agent.run_agent(self.root, 'chosen', 'fake')
        self.assertEqual(report['status'], 'complete', report['error'])
        self.assertLess(len(requests), 11)
        self.assertFalse(requests[-1][1])
        self.assertTrue(any('Context limit' in item for item in report['coverage']['limits_reached']))
        for event in report['trace']:
            self.assertLessEqual(len(json.dumps(event['observation'])), project.MAX_OBSERVATION_CHARS)
        tool_messages = [message for message in requests[-1][0] if message['role'] == 'tool']
        self.assertEqual([json.loads(message['content']) for message in tool_messages],
                         [event['observation'] for event in report['trace']])

    def test_empty_project_and_oversized_call(self):
        (self.root / 'app.py').unlink()
        (self.root / 'auth.py').unlink()
        with patch.object(agent, 'request_agent_turn') as request:
            report = agent.run_agent(self.root, 'chosen', 'fake')
            request.assert_not_called()
        self.assertEqual(report['status'], 'incomplete')
        (self.root / 'app.py').write_text('pass')
        with patch.object(agent, 'request_agent_turn', return_value=self.project_turn('search_code', {'query': 'x'*2000})):
            report = agent.run_agent(self.root, 'chosen', 'fake')
        self.assertEqual(report['status'], 'incomplete')
        self.assertIn('argument-size limit', report['error'])

    def test_directory_cli_and_trace(self):
        from contextlib import redirect_stderr
        report = {'status': 'complete', 'findings': [], 'trace': [], 'error': None,
                  'coverage': {'eligible_files': 2, 'observed_files': [], 'limits_reached': ['Partial coverage']}}
        output = io.StringIO()
        trace = self.root / 'trace.json'
        with patch.object(sys, 'argv', ['reviewer.py', str(self.root), '--agent', '--trace-file', str(trace)]), \
             patch.object(reviewer, 'load_dotenv'), \
             patch.object(reviewer, 'get_settings', return_value=('groq', 'chosen', 'fake')), \
             patch.object(reviewer, 'run_agent', return_value=report) as run, redirect_stdout(output):
            reviewer.main()
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs['project']['files'], ['app.py', 'auth.py'])
        self.assertEqual(run.call_args.kwargs['provider'], 'groq')
        self.assertEqual(json.loads(trace.read_text()), report)
        self.assertIn('Partial coverage', output.getvalue())
        with patch.object(sys, 'argv', ['reviewer.py', str(self.root)]), \
             patch.object(reviewer, 'load_dotenv'), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                reviewer.main()
        self.assertEqual(caught.exception.code, 2)
