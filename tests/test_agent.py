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
            result = request_agent_turn([], agent.TOOLS, 'chosen', 'fake')
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
