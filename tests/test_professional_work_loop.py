"""User outcomes through the existing ToolApplication loop.

Provider transport (and explicitly marked code generation) is controlled. Workspace edits, Python execution,
approval, accounting, inspection, publication and recovery use production code.
These are server/offline integration scenarios, not scientific or visual QA.
The ordinary path explicitly opts into operations.v1; the final scenario keeps
one genuinely saved default-legacy task without fabricating contract variants.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import csv
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from simple_ar.app.tool_application import ToolApplication
from simple_ar.core.assessments import ArtifactAssessment
from simple_ar.integrations.llm import LLMClient, LLMSettings
from simple_ar.tools.specs import ToolCall, ToolResult


# A real, dependency-free calculation and raster chart, not prepared output bytes.
# The figure's pixels are computed from the same observations as the CSV table.
_PROGRAM = r'''import csv
import json
from pathlib import Path
import struct
import sys
import zlib

source, output = Path(sys.argv[1]), Path(sys.argv[2])
output.mkdir(parents=True, exist_ok=True)
with source.open(encoding='utf-8', newline='') as stream:
    rows = list(csv.DictReader(stream))
values = [float(row['value']) for row in rows]
mean = sum(values) / len(values)
with (output / 'table.csv').open('w', encoding='utf-8', newline='') as stream:
    writer = csv.writer(stream)
    writer.writerow(['sample', 'value'])
    writer.writerows((row['sample'], row['value']) for row in rows)
(output / 'summary.json').write_text(json.dumps({
    'count': len(values), 'mean': mean, 'scope': 'supplied observations only'
}) + '\n', encoding='utf-8')
width, height = 180, 100
pixels = []
for y in range(height):
    line = bytearray()
    for x in range(width):
        bar = next((i for i in range(len(values)) if 15 + 50*i <= x < 40 + 50*i), None)
        on_bar = bar is not None and height - 10 - 20*values[bar] <= y < height - 10
        line.extend((45, 90, 150) if on_bar else (255, 255, 255))
    pixels.append(b'\x00' + bytes(line))
def chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
png = b'\x89PNG\r\n\x1a\n'
png += chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
png += chunk(b'IDAT', zlib.compress(b''.join(pixels))) + chunk(b'IEND', b'')
(output / 'figure.png').write_bytes(png)
# This append exposes an accidental execution replay across reload or feedback.
with (output.parent / 'execution-count.txt').open('a', encoding='utf-8') as stream:
    stream.write('calculated\n')
print(json.dumps({'count': len(values), 'mean': mean}))
'''

_BODY = '''# Observed sample values

The sample mean is 2.0 across 3 supplied observations.

| Sample | Value |
| --- | ---: |
| a | 1 |
| b | 2 |
| c | 3 |

![Values of the three supplied observations](figure.png)

The chart displays recorded values; it is not an uncertainty estimate.
'''
_SCOPE = ('The sample mean is 2.0 across 3 supplied observations. '
          'No population inference is supported by these three observations.')


def _action(name, **arguments):
    return {'tool_name': name, 'arguments': arguments}


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assessments(result):
    metadata = result['metadata']
    return metadata['assessments'] if 'assessments' in metadata else [metadata['assessment']]


class OperationsWorkLoopTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = self.directory / 'task'
        self.inputs = self.directory / 'measurements'
        self.inputs.mkdir()
        (self.inputs / 'observations.csv').write_text('sample,value\na,1\nb,2\nc,3\n', encoding='utf-8')
        (self.inputs / 'conditions.txt').write_text(
            'Three supplied observations; no sampling design or population uncertainty is known.\n',
            encoding='utf-8')
        client = LLMClient(LLMSettings(api_key='offline-fixture', model='fixture',
            api_mode='chat', retry_attempts=1, max_output_tokens=2048,
            context_input_tokens=100000))
        self.services = SimpleNamespace(llm_client=client)
        self.decisions = deque()
        self.inspection_views = []
        self.inspection_requests = []
        self.inspection_messages = []
        self.memory_views = []
        self.transport_errors = []
        self.review = self._review_complete_targets
        self.inspection_partial_targets = set()
        self.send = self.enterContext(patch('simple_ar.integrations.llm._call_openai_sdk',
            side_effect=self._provider))
        self.app = None

    def _create(self, *, report=False, allow_execution=True, legacy=False, delivery_files=None):
        options = {'control_protocol': None if legacy else 'operations.v1'}
        if delivery_files is not None:
            options['delivery_files'] = delivery_files
        if report:
            options.update(creation_declarations={'with_report': True,
                    'material_roles': {'data_path': ['measurements']}})
        self.app = ToolApplication.create(self.root,
            'Explain the supplied observations with an editable report, table and figure; '
            'preserve originals and disclose the limits of the evidence.',
            model='fixture', sources={'measurements': self.inputs}, services=self.services,
            allow_execution=allow_execution, tool_contracts='full',
            max_turns=70, max_tokens=900000, max_attempts=180,
            process_limits={'process_invocations': 48, 'process_wall_seconds': 360}, **options)
        return self.app

    def _reload(self):
        self.app = ToolApplication.load(self.root, services=self.services)
        return self.app

    def _review_complete_targets(self, view):
        """Default text review reads missing bytes; custom callbacks own their reads."""
        reads = []
        for target in view['targets']:
            if (target['complete'] or not target.get('sha256') or not target['text_windows']
                    or target['path'] in self.inspection_partial_targets):
                continue
            rows = [row for row in [*view['targets'], *view['evidence']]
                if row.get('path') == target['path'] and row.get('sha256') == target['sha256']]
            cursor = 0
            for start, end in sorted(span for row in rows for span in row.get('observed_ranges', [])):
                if start > cursor:
                    break
                cursor = max(cursor, end)
            size = target['text_windows'][0]['size_bytes']
            if cursor < size:
                reads.append(_action('read_file', path=target['path'], offset_bytes=cursor,
                    max_bytes=min(64000, size-cursor)))
        offered = {tool['function']['name'] for tool in self.inspection_requests[-1]['tools']}
        if reads and 'read_file' in offered:
            return reads[:8]
        return {'findings': [], 'limitations': ['Target reading remains partial.'] if reads else []}

    def _provider(self, mode, request, **kwargs):
        # A single controlled seam for both the native model and independent review.
        # We do not choose responses using invocation labels or internal stages.
        try:
            self.assertEqual(mode, 'chat')
            offered = {tool['function']['name'] for tool in request.get('tools', [])}
            if 'record_inspection' in offered:
                self.assertLessEqual(offered, {
                    'read_file', 'list_directory', 'search_files', 'record_inspection'})
                # Native follow-ups retain the original view and append assistant/tool
                # messages; source observations arrive through tool replies.
                view, _ = json.JSONDecoder().raw_decode(request['messages'][1]['content'])
                self.assertTrue(view['targets'])
                self.assertIn('evidence', view)
                ids = [row['subject'] for row in view['targets']]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertTrue(all(row['text_windows'] for row in view['targets']))
                for row in [*view['targets'], *view['evidence']]:
                    for window in row['text_windows']:
                        self.assertIsInstance(window['text'], str)
                self.inspection_views.append(view)
                self.inspection_requests.append(deepcopy(request))
                response = self.review(view)
                terminal = next(tool['function'] for tool in request['tools']
                    if tool['function']['name']=='record_inspection')
                if (isinstance(response,dict) and 'findings' in response
                        and 'decisions' in terminal['parameters'].get('properties',{})):
                    # Fixtures describe the same opinions for both frozen contracts.
                    # Supply actual read IDs, never infer verified evidence from a verdict.
                    observed = list(view['evidence'])
                    for message in request['messages']:
                        if message['role']=='tool':
                            row = json.loads(message['content']).get('data',{})
                            if row.get('subject'):
                                observed.append(row)
                    decisions = []
                    for subject in dict.fromkeys([*ids,*(f['subject'] for f in response['findings'])]):
                        target = next((row for row in view['targets'] if row['subject']==subject),{})
                        target_text = [w['text'] for w in target.get('text_windows',[])]
                        decisions.append({'subject':subject,
                            'findings':[{k:v for k,v in f.items() if k!='subject'}
                                for f in response['findings'] if f['subject']==subject],
                            'evidence':list(dict.fromkeys(row['subject'] for row in observed
                                if row.get('text_windows') and row['subject']!=subject
                                and row.get('path')!=target.get('path')
                                and (not row.get('sha256') or row.get('sha256')!=target.get('sha256'))
                                and [w['text'] for w in row['text_windows']]!=target_text)),
                            'limitations':response['limitations']})
                    response = {'decisions':decisions}
                actions = ([_action('record_inspection', **response)]
                    if isinstance(response, dict) else response)
                message = {'role': 'assistant', 'content': None,
                    'reasoning_content': 'Inspect the recorded evidence and retain its limits.',
                    'tool_calls': [{
                        'id': f'inspection-{self.send.call_count}-{number}', 'type': 'function',
                        'function': {'name': action['tool_name'],
                            'arguments': json.dumps(action['arguments'])}}
                        for number, action in enumerate(actions)]}
                self.inspection_messages.append(deepcopy(message))
                finish = 'tool_calls'
            elif request.get('tools'):
                self.assertTrue(self.decisions, 'Unexpected model work after the supplied decisions')
                action = self.decisions.popleft()
                action = action() if callable(action) else action
                self.assertIn(action['tool_name'], offered)
                message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': f'decision-{self.send.call_count}', 'type': 'function',
                    'function': {'name': action['tool_name'],
                        'arguments': json.dumps(action['arguments'])}}]}
                finish = 'tool_calls'
            else:
                view, _ = json.JSONDecoder().raw_decode(request['messages'][1]['content'])
                if 'notes' in view.get('response_schema',{}).get('properties',{}):
                    self.memory_views.append(view)
                    message = {'role':'assistant','content':json.dumps({'notes':[{
                        'text':'Earlier observations are saved; continue the requested explanation and retain the source scope.',
                        'observations':[row['id'] for row in view['observations'][:4]]}]})}
                    return {'choices':[{'message':message,'finish_reason':'stop'}],
                        'usage':{'prompt_tokens':100,'completion_tokens':40,'total_tokens':140}}
                self.fail('Unexpected non-native provider request outside working memory')
            return {'choices': [{'message': message, 'finish_reason': finish}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 40, 'total_tokens': 140}}
        except AssertionError as exc:
            # Production code retains provider failures; surface fixture mistakes at the test boundary.
            self.transport_errors.append(str(exc))
            raise

    def _inspection_history(self):
        """Check provider-visible native chronology and return the latest tool replies."""
        messages = self.inspection_requests[-1]['messages']
        previous = self.inspection_requests[-2]['messages']
        self.assertEqual([row for row in messages if row['role']=='assistant'],
            [row for row in previous if row['role']=='assistant'] + [self.inspection_messages[-1]])
        self.assertEqual([row for row in messages if row['role']=='tool'][:
            sum(row['role']=='tool' for row in previous)],
            [row for row in previous if row['role']=='tool'])
        replies, pending = [], []
        for message in messages:
            if message['role'] == 'assistant' and message.get('tool_calls'):
                self.assertFalse(pending, 'An assistant turn preceded outstanding tool replies')
                self.assertEqual(message.get('reasoning_content'),
                    'Inspect the recorded evidence and retain its limits.')
                pending = [call['id'] for call in message['tool_calls']]
                replies = []
            elif message['role'] == 'tool':
                self.assertTrue(pending, 'Tool reply has no preceding native call')
                self.assertEqual(message['tool_call_id'], pending.pop(0))
                replies.append(json.loads(message['content']))
        self.assertFalse(pending, 'Provider follow-up lost a tool reply')
        return replies

    def _run(self, **kwargs):
        turns = self.app.state['turns']
        caller_batch = kwargs.get('actions') is not None or (turns
            and turns[-1].get('origin') == 'caller'
            and turns[-1].get('request_count') == len(self.app.state['requests']))
        if caller_batch:
            # Ordinary caller actions do not abandon a failed model decision
            # and therefore do not grant caller takeover. Use the public stop
            # boundary after this batch (including approval/reload) drains.
            kwargs.setdefault('should_stop', lambda: self.app.state['pending'] is None
                and not any(turn['remaining'] for turn in self.app.state['turns']))
        result = self.app.run(progress=lambda _: None, **kwargs)
        self.assertFalse(self.transport_errors, self.transport_errors)
        return result

    def _observations(self, name=None):
        values = [self.app.session.store.read_json(tool['result'])
            for turn in self.app.state['turns'] for tool in turn['tools']]
        return [row for row in values if name is None or row['tool_name'] == name]

    def _last(self, name):
        values = self._observations(name)
        self.assertTrue(values, self.app.result_snapshot())
        return values[-1]

    def _submit(self, *actions, **kwargs):
        result = self._run(actions=list(actions), **kwargs)
        return result

    def _ok(self, name):
        row = self._last(name)
        self.assertEqual(row['status'], 'ok', row)
        return row

    def _write(self, path, content):
        self._submit(_action('write_file', path=path, content=content))
        return self._ok('write_file')

    def _read(self, path):
        self._submit(_action('read_file', path=path, full_digest=True))
        return self._ok('read_file')['data']

    def _inspect(self, targets, evidence=None):
        # Explicit FileWindows intentionally preserve their caller-selected scope.
        # The default reviewer may finish string targets; custom reviews remain untouched.
        partial = {str(self.app.workspace.resolve(row['path'])) for row in targets if isinstance(row, dict)}
        with patch.object(self, 'inspection_partial_targets', partial):
            self._submit(_action('inspect_artifacts', goal='Check the claims against supplied observations',
                targets=targets, evidence=evidence if evidence is not None else [
                    'measurements/observations.csv', 'measurements/conditions.txt']))
        return self._last('inspect_artifacts')

    def _assessment(self):
        result = self._ok('inspect_artifacts')
        assessment = _assessments(result)[0]
        ArtifactAssessment.model_validate(assessment)
        for path, digest in assessment['subjects'].items():
            self.assertEqual(_digest(Path(path)), digest)
        for path, digest in assessment['evidence_sha256'].items():
            self.assertEqual(_digest(Path(path)), digest)
        self.assertTrue(any('text' in item.lower() and 'scientific' in item.lower()
            for item in assessment['limitations']))
        return assessment

    def _process_receipts(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in (self.root / 'tools/processes').glob('*/invocation.json')}

    def _versions(self, directory):
        return {path: path.read_bytes() for path in directory.rglob('*') if path.is_file()}

    def _delivery(self, manuscript, *artifacts):
        return {'summary': 'Observed values with explicit evidence limits',
            'artifacts': [manuscript, *artifacts],
            'report': {'status': 'canonical', 'manuscript': manuscript}}

    def _export(self, manuscript, *, delivery=None):
        arguments = {'markdown': manuscript, 'compile_pdf': False}
        if delivery is not None:
            arguments['delivery'] = delivery
        self._submit(_action('export_report', **arguments), approve=lambda call, token: True)
        return self._last('export_report')

    def _not_delivered(self):
        self.assertNotIn(self.app.session.manifest.status, {'completed', 'partial'})
        self.assertFalse((self.root / 'delivery.json').exists())

    def test_native_work_approval_reload_independent_review_export_and_feedback(self):
        if not shutil.which('pandoc'):
            self.skipTest('Real editable publication requires Pandoc on the server')
        compile_pdf = os.environ.get('SAR_TEST_EXPORT_INTEGRATION') == '1'
        if compile_pdf and not all(shutil.which(tool) for tool in ('pdflatex', 'bibtex')):
            self.skipTest('Requested PDF integration needs the real ACM TeX environment')
        self._create(report=True)

        def review_actual_text(view):
            targets = '\n'.join(window['text'] for row in view['targets']
                for window in row['text_windows'])
            evidence = '\n'.join(window['text'] for row in view['evidence']
                for window in row['text_windows'])
            self.assertIn('2.0', targets)
            self.assertIn('a,1', evidence)
            self.assertIn('b,2', evidence)
            self.assertIn('c,3', evidence)
            self.assertIn('no sampling design', evidence)
            return {'findings': [], 'limitations': []}

        self.review = review_actual_text
        original_inputs = self._versions(self.inputs)
        self.decisions.extend([
            _action('read_file', path='measurements/observations.csv'),
            _action('write_file', path='analyze.py', content=_PROGRAM),
            _action('run_process', argv=['python', 'workspace/analyze.py',
                'measurements/observations.csv', 'workspace/v1'],
                inputs=['analyze.py', 'measurements/observations.csv'],
                outputs=['v1/table.csv', 'v1/summary.json', 'v1/figure.png'], timeout_sec=10),
            _action('read_file', path='v1/summary.json'),
            _action('write_file', path='v1/report.md', content=_BODY),
            _action('inspect_artifacts', goal='Check reported numbers and the declared evidence scope',
                targets=['v1/report.md', 'v1/table.csv', 'v1/summary.json'],
                evidence=['measurements/observations.csv', 'measurements/conditions.txt']),
            _action('export_report', markdown='v1/report.md', compile_pdf=compile_pdf,
                delivery={**self._delivery('v1/report.md'),
                    'artifacts':['v1/table.csv','v1/figure.png','analyze.py']}),
        ])
        self._run()
        pending = self.app.state['pending']
        self.assertIsNotNone(pending)
        token = self.app.approval_token(pending['call'])
        self.assertFalse(self._process_receipts(), 'An unapproved command executed')
        self.assertFalse((self.root / 'workspace/v1/table.csv').exists())
        ledger_before = self.app.ledger.to_dict()
        self._reload()
        self.assertEqual(self.app.ledger.to_dict(), ledger_before)
        self.assertEqual(self.app.approval_token(self.app.state['pending']['call']), token)
        self._run(approved_execution=token, approve=lambda call, value: True)
        self.assertEqual(self.app.session.manifest.status, 'completed', self.app.result_snapshot())
        self.assertFalse(self.decisions)
        products = self.root / 'workspace/v1'
        with (products / 'table.csv').open(encoding='utf-8', newline='') as stream:
            self.assertEqual([float(row['value']) for row in csv.DictReader(stream)], [1, 2, 3])
        self.assertEqual(json.loads((products / 'summary.json').read_text())['mean'], 2.0)
        self.assertTrue((products / 'figure.png').read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertEqual(self._assessment()['verdict'], 'passed')
        first_export = self._ok('export_report')
        self.assertIn('workspace/v1/report.md',first_export['artifacts'])
        text_only = self._delivery('v1/report.md')
        text_only['report']['formats'] = ['markdown']
        self.assertEqual(self.app.operations.delivery_issues(self.app._operation_observations(),text_only),[])
        first_project = self.app.workspace.resolve(first_export['data']['project'])
        self.assertIn('sample mean', (first_project / 'source.md').read_text(encoding='utf-8'))
        if compile_pdf:
            self.assertTrue((first_project / 'main.pdf').read_bytes().startswith(b'%PDF'))
        retained = {**self._versions(products), **self._versions(first_project)}
        first_delivery = (self.root / 'delivery.json').read_bytes()
        processes = self._process_receipts()
        self._reload()
        self._run()
        self.assertEqual(self._process_receipts(), processes)
        self.assertEqual((self.root / 'delivery.json').read_bytes(), first_delivery)
        # A completed historical delivery cannot stay current after its
        # independent evidence changes, even when delivered output bytes match.
        condition = self.inputs/'conditions.txt'
        original_condition = condition.read_bytes()
        usage = self.app.ledger.to_dict()
        try:
            condition.write_text('Different evidence, not independently reviewed.\n',encoding='utf-8')
            self._run()
            self.assertEqual(self.app.session.manifest.status,'paused')
            self.assertIn('Retained terminal observation',self.app.session.manifest.status_reason)
            self.assertEqual((self.root/'delivery.json').read_bytes(),first_delivery)
        finally:
            condition.write_bytes(original_condition)
        self._reload()
        self._run()
        self.assertEqual(self.app.session.manifest.status,'completed')
        self.assertEqual(self.app.ledger.to_dict(),usage)
        self.assertEqual(self._process_receipts(),processes)
        self.decisions.extend([
            _action('copy_file', source='v1/report.md', source_sha256=_digest(products / 'report.md'),
                path='v2/report.md'),
            _action('read_file', path='v2/report.md', full_digest=True),
            lambda: _action('edit_file', path='v2/report.md',
                expected_sha256=self._last('read_file')['data']['sha256'],
                old_text='The sample mean is 2.0 across 3 supplied observations.', new_text=_SCOPE),
            *[_action('copy_file', source=f'v1/{name}', source_sha256=_digest(products / name),
                path=f'v2/{name}') for name in ('table.csv', 'summary.json', 'figure.png')],
            _action('inspect_artifacts', goal='Check the revised inference limitation against original conditions',
                targets=['v2/report.md', 'v2/table.csv'],
                evidence=['measurements/observations.csv', 'measurements/conditions.txt']),
            _action('export_report', markdown='v2/report.md', compile_pdf=compile_pdf,
                delivery=self._delivery('v2/report.md', 'v2/table.csv', 'v2/figure.png', 'analyze.py')),
        ])
        self._run(feedback='Clarify that these three observations do not establish a population estimate. '
            'Revise only the interpretation, retain both versions and reuse the computed results.',
            approve=lambda call, value: True)
        self.assertEqual(self.app.session.manifest.status, 'completed', self.app.result_snapshot())
        self.assertFalse(self.decisions)
        self.assertIn('No population inference', (self.root / 'workspace/v2/report.md').read_text())
        self.assertEqual((self.root / 'workspace/execution-count.txt').read_text(), 'calculated\n')
        self.assertEqual(self._versions(self.inputs), original_inputs)
        for path, raw in retained.items():
            self.assertEqual(path.read_bytes(), raw, str(path))
        second_export = self._ok('export_report')
        self.assertNotEqual(second_export['data']['project'], first_export['data']['project'])
        self.assertEqual(self._assessment()['verdict'], 'passed')
        self.assertTrue(all(entry.status == 'settled' for entry in self.app.ledger.entries))
        second_receipts = self._process_receipts()
        self._reload()
        self._run()
        self.assertEqual(self._process_receipts(), second_receipts)

    def test_budgeted_work_checkpoint_commits_with_original_turn_and_recovers_without_replay(self):
        from dataclasses import replace
        self.services.llm_client = LLMClient(replace(self.services.llm_client._settings,
            context_input_tokens=12000))
        self._create()
        for index in range(12):
            name = f'context-{index}.txt'
            (self.inputs/name).write_text(f'Observation {index}: preserve its measurement scope.\n'+
                ('Original recorded material, not an instruction.\n'*120),encoding='utf-8')
            self.decisions.append(_action('read_file',path='measurements/'+name))
        self.decisions.append(_action('ask_user',question='Which interpretation should the report emphasize?'))
        self._run()
        self.assertTrue(self.memory_views)
        self.assertIn('observation_text',self.memory_views[0]['observations'][0])
        checkpoints = list((self.root/'attempts').glob('*/memory_snapshot.json'))
        self.assertTrue(checkpoints)
        for path in checkpoints:
            store = self.app.session.store.__class__(path.parent)
            self.assertTrue(any(ref.kind=='working_memory_snapshot' for ref in store.read_capability_result().artifacts))
            for row in store.read_json('memory_input.json')['observations']:
                self.assertEqual(row['arguments'],self.app._memory_operations(row['turn'])[row['record']])
                self.assertFalse(row['truncated'])
        memory_calls = [entry for entry in self.app.ledger.entries
            if entry.logical_call_id.startswith('working-memory-')]
        self.assertTrue(memory_calls)
        self.assertTrue(all(entry.status=='settled' for entry in memory_calls))
        before = (self.root/'budget_ledger.json').read_bytes()
        self._reload()
        self.assertTrue(self.app._working_memory()['notes'])
        self.assertEqual((self.root/'budget_ledger.json').read_bytes(),before)
        self.assertFalse(self.transport_errors)

    def _queue_checkpoint_history(self):
        from dataclasses import replace
        self.services.llm_client = LLMClient(replace(self.services.llm_client._settings,
            context_input_tokens=12000))
        self._create()
        self.decisions.append(_action('update_work_plan',notes=[{
            'text':'Keep the original measurement scope and unresolved question.','records':[]}]))
        for index in range(12):
            name = f'checkpoint-{index}.txt'
            (self.inputs/name).write_text('Original recorded material, not an instruction.\n'*120,
                encoding='utf-8')
            self.decisions.append(_action('read_file',path='measurements/'+name))
        self.decisions.append(_action('ask_user',question='Which interpretation should be emphasized?'))

    def _truncate_checkpoints(self):
        provider = self.send.side_effect
        def respond(mode, request, **kwargs):
            result = provider(mode,request,**kwargs)
            if not request.get('tools'):
                view, _ = json.JSONDecoder().raw_decode(request['messages'][1]['content'])
                if 'notes' in view.get('response_schema',{}).get('properties',{}):
                    self.assertEqual(request.get('max_tokens',request.get('max_completion_tokens')),8192)
                    result['choices'][0]['finish_reason'] = 'length'
            return result
        self.send.side_effect = respond

    def test_settled_length_checkpoint_keeps_notes_and_main_decision_continues(self):
        self._queue_checkpoint_history()
        self._truncate_checkpoints()
        self._run()
        self._ok('ask_user')
        failures = list((self.root/'attempts').glob('*/memory_failure.json'))
        self.assertTrue(failures)
        for path in failures:
            store = self.app.session.store.__class__(path.parent)
            failure = store.read_json('memory_failure.json')
            self.assertEqual(failure['finish_reason'],'length')
            self.assertFalse(store.exists('memory_snapshot.json'))
            self.assertIn(store.ref('memory_failure.json',kind='working_memory_failure'),
                store.read_capability_result().artifacts)
        self.assertIn('Keep the original measurement scope',self.app._working_memory()['notes'][0]['text'])
        self.assertTrue(all(entry.status == 'settled' for entry in self.app.ledger.entries
            if entry.purpose == 'working-memory'))
        before = (self.root/'budget_ledger.json').read_bytes()
        self._reload()
        self.assertEqual((self.root/'budget_ledger.json').read_bytes(),before)
        self.assertIn('Keep the original measurement scope',self.app._working_memory()['notes'][0]['text'])

    def _interrupt_checkpoint_commit(self, *, truncated=False):
        from simple_ar.core.session import _persist_attempt_result
        self._queue_checkpoint_history()
        if truncated:
            self._truncate_checkpoints()
        def interrupt(store, result, facts):
            if (store.exists('memory_input.json') and store.exists('native_result.json')
                    and not store.exists('capability_result.json')):
                raise KeyboardInterrupt('Crash after native reply, before capability persistence')
            return _persist_attempt_result(store,result,facts)
        with patch('simple_ar.core.session._persist_attempt_result',side_effect=interrupt):
            self._run()
        pending = deepcopy(self.app.state['pending'])
        self.assertEqual(pending['kind'],'model')
        store = self.app.session.store.__class__(self.root/'attempts'/pending['attempt'])
        self.assertFalse(store.exists('capability_result.json'))
        before = (self.root/'budget_ledger.json').read_bytes()
        self._reload()
        calls = self.send.call_count
        self._run(should_stop=lambda:self.app.state['pending'] is None)
        self.assertEqual(self.send.call_count,calls)
        self.assertEqual((self.root/'budget_ledger.json').read_bytes(),before)
        refs = store.read_capability_result().artifacts
        path = 'memory_failure.json' if truncated else 'memory_snapshot.json'
        self.assertIn(path,[ref.path for ref in refs])
        self.assertIn('memory_input.json',[ref.path for ref in refs])
        self.assertIn('native_result.json',[ref.path for ref in refs])
        turn = self.app.state['turns'][-1]
        if truncated:
            self.assertIsNotNone(self.app._committed_memory_record(turn,path))
            self.assertIn('Keep the original measurement scope',self.app._working_memory()['notes'][0]['text'])
        else:
            self.assertIsNotNone(self.app._checkpoint_for_turn(turn))

    def test_cached_native_reply_recovers_checkpoint_without_provider_replay(self):
        self._interrupt_checkpoint_commit()

    def test_cached_native_reply_recovers_failed_checkpoint_scope_without_provider_replay(self):
        self._interrupt_checkpoint_commit(truncated=True)

    def test_cached_legacy_native_reply_recovers_without_operations_or_model_call(self):
        from simple_ar.core.session import _persist_attempt_result
        self._create(legacy=True)
        self.assertIsNone(self.app.operations)
        self.decisions.append(_action('ask_user',question='Which interpretation should be emphasized?'))
        def interrupt(store, result, facts):
            if store.exists('native_result.json') and not store.exists('capability_result.json'):
                raise KeyboardInterrupt('Crash before legacy capability persistence')
            return _persist_attempt_result(store,result,facts)
        with patch('simple_ar.core.session._persist_attempt_result',side_effect=interrupt):
            self._run()
        pending = deepcopy(self.app.state['pending'])
        before = (self.root/'budget_ledger.json').read_bytes()
        self._reload()
        calls = self.send.call_count
        self._run(should_stop=lambda:self.app.state['pending'] is None)
        self.assertEqual(self.send.call_count,calls)
        self.assertEqual((self.root/'budget_ledger.json').read_bytes(),before)
        self.assertIsNotNone(self.app._accepted_model_attempt(pending['attempt']))

    def test_working_model_boundary_keeps_real_observations_without_reopening_native_history(self):
        self._create()
        self.decisions.extend([_action('read_file',path='measurements/observations.csv'),
                               _action('ask_user',question='Which comparison should be emphasized?')])
        self._run()
        before = {p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        calls = self.send.call_count
        client = LLMClient(LLMSettings(api_key='fixture',model='thinking-fixture',
            thinking_mode='enabled',max_output_tokens=2048,context_input_tokens=100000))
        # These real, registered fixture responses have no provider reasoning.
        with self.assertRaisesRegex(ValueError,'Native history lacks reasoning_content'):
            self.app._messages(client=client)
        boundary = len(self.app.state['turns'])
        messages = self.app._messages(client=client,native_history_start_turn=boundary)
        self.assertFalse(any(row['role'] in {'assistant','tool'} for row in messages))
        context = json.loads(messages[1]['content'])
        self.assertEqual(context['history_archive']['native_history_start_turn'],boundary)
        retained = context['working_view']['observations']
        self.assertTrue(any('sample,value' in row['observation']['text'] for row in retained))
        self.assertEqual({p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()},before)
        self.assertEqual(self.send.call_count,calls)

    def test_working_understanding_survives_feedback_and_invalidates_changed_source(self):
        self._create()
        self._read('measurements/observations.csv')
        source_record = self.app.state['turns'][-1]['tools'][-1]['result']
        self._submit(_action('update_work_plan', notes=[{
            'text':'Three recorded observations; no population sampling design is supplied.',
            'records':['records/'+source_record.removeprefix('attempts/')]}]))
        self.assertEqual(self._ok('update_work_plan')['data']['notes'][0]['scope'],
            'model_note_not_verified_evidence')
        before = (self.root/'budget_ledger.json').read_bytes()
        self._reload()
        self.assertEqual((self.root/'budget_ledger.json').read_bytes(), before)
        self.assertTrue(self.app._working_memory()['notes'][0]['current'])
        self._submit(_action('update_work_plan', steps=[{'step':'Revise only the explanation',
            'status':'pending'}]), feedback='Keep the calculation; clarify interpretation.')
        self.assertTrue(self.app._working_memory()['notes'][0]['current'])
        self.assertIn('Three recorded', self.app._working_memory()['notes'][0]['text'])
        self._submit(_action('update_work_plan', notes=[{'text':'Invented source',
            'records':['records/not-an-observation/native_result.json']}]))
        self.assertNotEqual(self._last('update_work_plan')['status'],'ok')
        self.assertIn('Three recorded', self.app._working_memory()['notes'][0]['text'])
        (self.inputs/'observations.csv').write_text('sample,value\na,7\n',encoding='utf-8')
        self.assertFalse(self.app._working_memory()['notes'][0]['current'])
        self._submit(_action('update_work_plan', notes=[]))
        self.assertEqual(self.app._working_memory()['notes'],[])

    def test_required_defect_blocks_delivery_without_editing_then_same_bytes_can_be_reassessed(self):
        self._create()
        self._write('analysis.md', 'The sample mean is 9.0.\n')
        self._write('evidence-notes.txt', 'Only the three supplied observations were checked.\n')
        self.review = lambda view: {'findings': [{'subject': view['targets'][0]['subject'],
            'severity': 'required', 'message': 'The claim 9.0 contradicts the supplied values 1, 2, 3.'}],
            'limitations': []}
        self._inspect(['analysis.md'], ['evidence-notes.txt', 'measurements/observations.csv'])
        assessment = self._assessment()
        self.assertEqual(assessment['verdict'], 'needs_revision')
        original = (self.root / 'workspace/analysis.md').read_bytes()
        self._submit(_action('deliver_result', summary='All requested claims checked', artifacts=['analysis.md'],
            limitations=['Only the supplied observations are in scope.']))
        self._not_delivered()
        self.assertEqual((self.root / 'workspace/analysis.md').read_bytes(), original)
        self.assertFalse(self._process_receipts())
        self.assertEqual(len(self.inspection_views), 1)
        # Changing independent evidence cannot erase an existing required
        # concern on unchanged target bytes, even for a plain non-report claim.
        evidence = self._read('evidence-notes.txt')
        self._submit(_action('edit_file', path='evidence-notes.txt',
            expected_sha256=evidence['sha256'], old_text='were checked', new_text='still need checking'))
        self._ok('edit_file')
        self._reload()
        plain_delivery = _action('deliver_result', summary='All requested claims checked', artifacts=['analysis.md'])
        self._submit(plain_delivery)
        self._not_delivered()
        self.review = lambda view: {'findings': [], 'limitations': []}
        self._inspect([{'path': 'analysis.md', 'max_bytes': 8}], ['evidence-notes.txt'])
        self.assertEqual(self._assessment()['verdict'], 'incomplete')
        self._submit(plain_delivery)
        self._not_delivered()
        self.assertEqual((self.root / 'workspace/analysis.md').read_bytes(), original)
        # A semantic concern remains available to the model, without an automatic repair chain.
        # Fix using the native edit primitive, then let an independent reviewer make a
        # conservative required judgment followed by a complete same-byte reassessment.
        observed = self._read('analysis.md')
        self._submit(_action('edit_file', path='analysis.md', expected_sha256=observed['sha256'],
            old_text='9.0', new_text='2.0'))
        self._ok('edit_file')
        self.review = lambda view: {'findings': [{'subject': view['targets'][0]['subject'],
            'severity': 'required', 'message': 'Recheck that the supplied rows are the full stated sample.'}],
            'limitations': []}
        self._inspect(['analysis.md'])
        correct_bytes = (self.root / 'workspace/analysis.md').read_bytes()
        self.assertEqual(self._assessment()['verdict'], 'needs_revision')
        self.review = lambda view: {'findings': [], 'limitations': []}
        self._inspect(['analysis.md'])
        self.assertEqual(self._assessment()['verdict'], 'passed')
        self.assertEqual((self.root / 'workspace/analysis.md').read_bytes(), correct_bytes)
        self._submit(_action('deliver_result', summary='Observed sample mean checked', artifacts=['analysis.md']))
        self.assertEqual(self.app.session.manifest.status, 'completed', self.app.result_snapshot())
        self._reload()
        self.assertEqual(_assessments(self._last('inspect_artifacts'))[0]['verdict'], 'passed')

    def test_independent_subjects_keep_passes_and_defects_when_a_sibling_changes(self):
        from simple_ar.core.assessments import assessment_issues
        self._create()
        self.assertEqual(self.app.state['config']['inspection_protocol'],'subjects.v2')
        self._write('report.md','# Results\n\nThe observed mean is 2.0.\n')
        self._write('layout.py','font_size = 10\n')
        self._write('incorrect.txt','The observed mean is 9.0.\n')
        self.review = lambda view: {'decisions':[
            {'subject':'target-1','evidence':['evidence-1'],'findings':[],'limitations':[]},
            {'subject':'target-2','evidence':[],'findings':[],'limitations':[]},
            {'subject':'target-3','evidence':['evidence-1'],
             'findings':[{'severity':'required','message':'9.0 contradicts the observed mean 2.0.'}],
             'limitations':[]}]}
        result = self._inspect(['report.md','layout.py','incorrect.txt'])
        self.assertEqual([a['verdict'] for a in _assessments(result)],
            ['passed','passed','needs_revision'])
        before = deepcopy(result)
        layout = self._read('layout.py')
        self._submit(_action('edit_file',path='layout.py',expected_sha256=layout['sha256'],
            old_text='10',new_text='12'))
        self._reload()
        self.assertEqual(self._last('inspect_artifacts'),before)
        observations = self.app._operation_observations()
        self.assertEqual(assessment_issues(self.app.workspace,observations,['report.md'],
            require_pass=['report.md']),[])
        issues = assessment_issues(self.app.workspace,observations,['incorrect.txt'])
        self.assertTrue(any('9.0 contradicts' in text for text in issues),issues)
        context = self.app._work_context(['workspace'])
        self.assertIn('9.0 contradicts',json.dumps(context['professional_findings']))
        self.assertNotIn('sample,value',json.dumps(context['professional_findings']))
        self._submit(_action('deliver_result',summary='Incorrect result',artifacts=['incorrect.txt']))
        self._not_delivered()
        # A source change invalidates the dependent report without erasing its sibling's defect.
        original = (self.inputs/'observations.csv').read_bytes()
        (self.inputs/'observations.csv').write_text('sample,value\na,7\n')
        observations = self.app._operation_observations()
        issues = assessment_issues(self.app.workspace,observations,['report.md','incorrect.txt'],
            require_pass=['report.md'])
        self.assertTrue(any('lacks a complete' in text for text in issues),issues)
        self.assertTrue(any('9.0 contradicts' in text for text in issues),issues)
        (self.inputs/'observations.csv').write_bytes(original)
        self._submit(_action('deliver_result',**self._delivery('report.md')))
        self.assertEqual(self.app.session.manifest.status,'completed',self.app.result_snapshot())
        self.assertEqual(self._last('inspect_artifacts'),before)

    def test_subject_inspection_binds_cross_file_support_without_passing_partial_siblings(self):
        from simple_ar.core.assessments import assessment_issues
        self._create()
        self._write('report.md','# Results\n\nSee the calculation.\n')
        self._write('calculation.py','mean = (1 + 2 + 3) / 3\n')
        self.review = lambda view: {'decisions':[
            {'subject':'target-1','evidence':['target-2'],'findings':[],'limitations':[]},
            {'subject':'target-2','evidence':[],'findings':[],'limitations':['Only the supplied prefix was reviewed.']}]}
        result = self._inspect(['report.md',{'path':'calculation.py','max_bytes':8}],[])
        self.assertEqual([a['verdict'] for a in _assessments(result)],['passed','incomplete'])
        observations = self.app._operation_observations()
        self.assertEqual(assessment_issues(self.app.workspace,observations,['report.md'],require_pass=['report.md']),[])
        source = self._read('calculation.py')
        self._submit(_action('edit_file',path='calculation.py',expected_sha256=source['sha256'],
            old_text='1 + 2 + 3',new_text='7 + 8 + 9'))
        self._reload()
        issues = assessment_issues(self.app.workspace,self.app._operation_observations(),
            ['report.md'],require_pass=['report.md'])
        self.assertTrue(any('lacks a complete' in text for text in issues),issues)

        # Reject an unread source ID rather than silently dropping the dependency.
        self.review = lambda view: {'decisions':[
            {'subject':'target-1','evidence':['evidence-999'],'findings':[],'limitations':[]}]}
        result = self._inspect(['report.md'],[])
        self.assertEqual(result['status'],'error',result)
        self.assertTrue(all(a['verdict']=='incomplete' for a in _assessments(result)))
        self._submit(_action('deliver_result',**self._delivery('report.md')))
        self._not_delivered()

    def test_changed_bytes_and_partial_windows_cannot_inherit_a_passed_inspection(self):
        if not shutil.which('pandoc'):
            self.skipTest('Isolating stale review from publication requires real Pandoc')
        self._create(report=True)
        self._write('report.md', '# Measurements\n\nThe sample mean is 2.0.\n')
        self._inspect(['report.md'])
        self.assertEqual(self._assessment()['verdict'], 'passed')
        bound = self._last('inspect_artifacts')
        before = self._read('report.md')
        self._submit(_action('edit_file', path='report.md', expected_sha256=before['sha256'],
            old_text='2.0', new_text='9.0'))
        self._ok('edit_file')
        self._reload()
        exported = self._export('report.md')
        self.assertEqual(exported['status'], 'ok', exported)
        # The current bytes now have their own export. Only the independent
        # assessment is stale; missing publication cannot explain this refusal.
        delivery = self._delivery('report.md', *exported['artifacts'])
        self._submit(_action('deliver_result', **delivery))
        self._not_delivered()
        self.assertEqual(self._last('inspect_artifacts'), bound)
        self._inspect([{'path': 'report.md', 'max_bytes': 8}])
        self.assertEqual(self._assessment()['verdict'], 'incomplete')
        self._submit(_action('deliver_result', **delivery))
        self._not_delivered()
        self.assertFalse(self._observations('run_process'))
        self.assertEqual((self.root / 'workspace/report.md').read_text().count('9.0'), 1)

    def test_independent_evidence_version_and_current_manuscript_export_are_required(self):
        if not shutil.which('pandoc'):
            self.skipTest('Real cross-version publication requires Pandoc on the server')
        self._create(report=True)
        self._write('v1/report.md', '# Measurements\n\nThe sample mean is 2.0.\n')
        self._inspect(['v1/report.md'])
        self.assertEqual(self._assessment()['verdict'], 'passed')
        old = self._export('v1/report.md')
        self.assertEqual(old['status'], 'ok', old)
        retained = self._versions(self.app.workspace.resolve(old['data']['project']))
        self._write('v2/report.md', '# Measurements\n\nThe sample mean is 2.0; no population inference.\n')
        self._inspect(['v2/report.md'])
        self.assertEqual(self._assessment()['verdict'], 'passed')
        self._submit(_action('deliver_result', **self._delivery('v2/report.md', *old['artifacts'])))
        self._not_delivered()
        # A passed target judgment must also retain its independent evidence version.
        self._write('conditions.txt', 'Only these three observations are in scope.\n')
        self._inspect(['v2/report.md'], ['conditions.txt', 'measurements/observations.csv'])
        self.assertEqual(self._assessment()['verdict'], 'passed')
        current = self._export('v2/report.md')
        self.assertEqual(current['status'], 'ok', current)
        evidence = self._read('conditions.txt')
        self._submit(_action('edit_file', path='conditions.txt', expected_sha256=evidence['sha256'],
            old_text='Only these three observations', new_text='A different, unobserved dataset'))
        self._ok('edit_file')
        self._submit(_action('deliver_result', **self._delivery('v2/report.md', *current['artifacts'])))
        self._not_delivered()
        for path, raw in retained.items():
            self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(len(self._observations('run_process')), 0)

    def test_same_path_republication_selects_current_delivery_and_preserves_history(self):
        if not shutil.which('pandoc'):
            self.skipTest('Real replacement publication requires Pandoc on the server')
        compile_pdf = os.environ.get('SAR_TEST_EXPORT_INTEGRATION') == '1'
        if compile_pdf and not all(shutil.which(tool) for tool in ('pdflatex','bibtex')):
            self.skipTest('Requested PDF integration needs the real ACM TeX environment')
        self._create(report=True)
        original_inputs = self._versions(self.inputs)
        self._write('analyze.py',_PROGRAM)
        self._submit(_action('run_process',argv=['python','workspace/analyze.py',
            'measurements/observations.csv','workspace/paper'],
            inputs=['analyze.py','measurements/observations.csv'],
            outputs=['paper/table.csv','paper/summary.json','paper/figure.png'],timeout_sec=10),
            approve=lambda *_:True)
        self._ok('run_process')
        manuscript = 'paper/report.md'
        self._write('paper/references.bib',
            '@misc{P1, author={Fixture Author}, title={Supplied observations}, year={2026}}\n')
        self._write(manuscript,_BODY+'\nThe supplied observation record is cited as [@P1].\n')
        self._inspect([manuscript])
        self._ok('inspect_artifacts')
        self._submit(_action('export_report',markdown=manuscript,compile_pdf=compile_pdf),approve=lambda *_:True)
        old = self._ok('export_report')
        old_project = self.app.workspace.resolve(old['data']['project'])
        formats = ['pdf'] if compile_pdf else ['tex']
        old_outputs = [self.app.workspace.locator(str(old_project/'main.pdf'))] if compile_pdf else old['artifacts']
        old_delivery = self._delivery(manuscript,*old_outputs)
        old_delivery['report']['formats'] = formats
        self._submit(_action('deliver_result',**old_delivery))
        self._ok('deliver_result')
        self.assertEqual(self.app.session.manifest.status,'completed',self.app.result_snapshot())
        old_bytes = self._versions(old_project)
        old_records = {path:path.read_bytes() for path in (self.root/'attempts').glob('*/native_result.json')}
        old_usage = [entry.to_dict() for entry in self.app.ledger.entries]

        self._submit(_action('read_file',path=manuscript,full_digest=True),
            feedback='Clarify the inference limit in the same manuscript and publish the revised version.')
        version = self._ok('read_file')['data']['sha256']
        self._submit(_action('edit_file',path=manuscript,expected_sha256=version,
            old_text='The sample mean is 2.0 across 3 supplied observations.',new_text=_SCOPE))
        self._ok('edit_file')
        self._inspect([manuscript])
        self._ok('inspect_artifacts')
        self._reload()
        self._submit(_action('deliver_result',**old_delivery))
        self.assertIn('Publication authoring inputs changed',self._last('deliver_result')['error'])
        self.assertNotEqual(self.app.session.manifest.status,'completed')
        self._submit(_action('export_report',markdown=manuscript,compile_pdf=compile_pdf),approve=lambda *_:True)
        current = self._ok('export_report')
        current_project = self.app.workspace.resolve(current['data']['project'])
        self.assertNotEqual(current_project,old_project)
        current_outputs = ([self.app.workspace.locator(str(current_project/'main.pdf'))]
                           if compile_pdf else current['artifacts'])
        delivery = self._delivery(manuscript,*current_outputs)
        delivery['report']['formats'] = formats
        if compile_pdf:
            self.assertNotIn(self.app.workspace.locator(str(current_project/'main.tex')),delivery['artifacts'])
        observations = self.app._operation_observations()
        ledger = self.app.ledger.to_dict()
        calls = self.send.call_count

        def check(rows,artifacts):
            return self.app.publication.check_report_delivery(rows,None,artifacts,
                formats=formats,manuscript=manuscript)

        expected = {self.app.workspace.resolve(path) for path in current['data']['artifact_sha256']}
        for rows in (observations,list(reversed(observations))):
            self.assertEqual(check(rows,delivery['artifacts']),expected)
            # Even when an old project is also delivered, its stale source
            # binding cannot veto a genuinely current replacement candidate.
            self.assertEqual(check(rows,[*delivery['artifacts'],*old_outputs]),expected)
        old_source = old_project/'source.md'
        try:
            old_source.write_bytes(old_bytes[old_source]+b'\nHistorical project damaged.\n')
            self.assertEqual(check(observations,delivery['artifacts']),expected)
        finally:
            old_source.write_bytes(old_bytes[old_source])
        # PDF-only delivery still validates the full retained project binding;
        # it simply does not require every editable file in the delivery list.
        selected = current_project/'main.tex'
        selected_bytes = selected.read_bytes()
        try:
            selected.write_bytes(selected_bytes+b'\n% Changed after export.\n')
            with self.assertRaisesRegex(ValueError,'Publication project or owner evidence changed'):
                check(observations,delivery['artifacts'])
            self._submit(_action('deliver_result',**delivery))
            self.assertIn('Publication project or owner evidence changed',self._last('deliver_result')['error'])
            self.assertNotEqual(self.app.session.manifest.status,'completed')
        finally:
            selected.write_bytes(selected_bytes)
        self._reload()
        self._submit(_action('deliver_result',**delivery))
        self._ok('deliver_result')
        self.assertEqual(self.app.session.manifest.status,'completed',self.app.result_snapshot())
        self.assertEqual(self.app.ledger.to_dict(),ledger)
        self.assertEqual(self.send.call_count,calls)
        self.assertEqual([entry.to_dict() for entry in self.app.ledger.entries][:len(old_usage)],old_usage)
        for path,raw in {**old_bytes,**old_records}.items():
            self.assertEqual(path.read_bytes(),raw)
        self.assertEqual(self._versions(self.inputs),original_inputs)

    def test_progressive_inspection_keeps_bounded_context_and_cumulative_decoded_coverage(self):
        from dataclasses import replace
        from simple_ar.tools.workspace import FileWindow

        self.services.llm_client = LLMClient(replace(self.services.llm_client._settings,
            context_input_tokens=12000))
        self._create()
        text = 'A'*511+'\u6d4b\n'+('Observed record; scope is text only.\n'*800)
        self._write('progressive.md', text)
        target = self.root/'workspace/progressive.md'
        original = target.read_bytes()
        before = len(self.inspection_requests)

        def review(view):
            response = self._review_complete_targets(view)
            if isinstance(response, list):
                for action in response:
                    action['arguments']['max_bytes'] = min(8192, action['arguments']['max_bytes'])
            return response

        self.review = review
        result = self._inspect(['progressive.md'], [])
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(self._assessment()['coverage'], 'complete')
        self.assertEqual(self._assessment()['verdict'], 'passed')
        requests = self.inspection_requests[before:]
        self.assertGreater(len(requests), 3)
        self.assertLessEqual(len(requests), 7)
        first, _ = json.JSONDecoder().raw_decode(requests[0]['messages'][1]['content'])
        self.assertFalse(first['targets'][0]['complete'])
        self.assertEqual(first['targets'][0]['observed_ranges'], [[0, 511]])
        self.assertEqual(first['targets'][0]['text_windows'][0]['text'], 'A'*511)
        final, _ = json.JSONDecoder().raw_decode(requests[-1]['messages'][1]['content'])
        self.assertTrue(final['targets'][0]['complete'])
        self.assertGreater(final['working_view']['history']['archive_end_turn'], 0)

        identity = Path(result['data']['record']).parts[1]
        store = self.app.session.store
        saved = store.read_json(f'tools/inspection/{identity}/input.json')
        rows = [*saved['targets'], *saved['evidence']]
        self.assertGreater(len(rows), 3)
        self.assertTrue(all(not row['complete'] for row in rows))
        self.assertEqual({row['sha256'] for row in rows}, {_digest(target)})
        windows = sorted((window for row in rows for window in row['windows']),
            key=lambda window: window['offset_bytes'])
        cursor, decoded = 0, []
        for window in windows:
            self.assertEqual(window['offset_bytes'], cursor)
            self.assertNotIn('\ufffd', window['review_text'])
            decoded.append(window['review_text'])
            cursor = window['review_next_offset_bytes']
        self.assertEqual(cursor, len(original))
        self.assertEqual(''.join(decoded).encode('utf-8'), original)
        traces = [store.read_json(f'tools/inspection/{identity}/sources-{index}.json')
            for index in range(len(requests))]
        for trace in traces:
            projection = trace['working_input']['projection']
            self.assertEqual(projection['input_token_target'], 12000)
            self.assertLessEqual(projection['estimated_input_tokens'], 12000)
            self.assertTrue(trace['completed'])
        self.assertEqual(sum(len(trace['results']) for trace in traces), len(rows)-1)
        self.assertTrue(any(not row['text_windows'] and row['observed_ranges']
            for row in final['evidence']))
        owner = self.app.operations.inspection
        self.assertTrue(owner._complete_target(rows[0], rows))
        # A real tail starting after the split character leaves a decoded-byte gap.
        tail = owner._observe(FileWindow(path='progressive.md', offset_bytes=514, max_bytes=64000),
            'gap-tail', 'progressive-gap', [512000])
        self.assertTrue(tail['readable'], tail)
        self.assertFalse(owner._complete_target(rows[0], [rows[0], tail]))
        # Read the otherwise complementary tail from a changed file: it must not
        # complete the old preview or inherit that version's passed assessment.
        target.write_bytes(b'B'+original[1:])
        changed = owner._observe(FileWindow(path='progressive.md', offset_bytes=511, max_bytes=64000),
            'changed-tail', 'progressive-version', [512000])
        self.assertTrue(changed['readable'], changed)
        self.assertNotEqual(changed['sha256'], rows[0]['sha256'])
        self.assertFalse(owner._complete_target(rows[0], [rows[0], changed]))
        with self.assertRaisesRegex(ValueError, 'version changed'):
            owner._check_versions(rows, 'progressive-stale')
        self.assertFalse(owner.can_reconcile('inspect_artifacts', identity))
        target.write_bytes(original)
        calls, ledger = self.send.call_count, self.app.ledger.to_dict()
        records = self._versions(self.root/f'tools/inspection/{identity}')
        self._reload()
        cached = self.app.operations.inspection.call('inspect_artifacts', saved['arguments'], identity)
        self.assertEqual(cached.status, 'ok', cached)
        self.assertEqual(self.send.call_count, calls)
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        self.assertEqual(self._versions(self.root/f'tools/inspection/{identity}'), records)

    def test_inspector_locates_missing_sources_within_one_version_bound_operation(self):
        self._create(report=True)
        self._write('notes.md','The sample mean is 2.0 from three observations.\n')
        originals = self._versions(self.inputs)

        def review(view):
            rounds = len(self.inspection_views)
            if rounds==1:
                return [_action('list_directory', path='measurements')]
            replies = self._inspection_history()
            if rounds==2:
                self.assertEqual(len(replies), 1)
                self.assertIn('observations.csv', json.dumps(replies[0]))
                return [
                    _action('search_files', path='measurements', query='sample'),
                    _action('search_files', path='measurements', query='a,1'),
                    _action('read_file', path='measurements/observations.csv'),
                    _action('read_file', path=str(self.directory/'outside.txt'))]
            self.assertEqual(rounds,3)
            self.assertEqual(len(replies), 4)
            for reply, match in zip(replies[:2], ('sample,value', 'a,1')):
                self.assertEqual(reply['status'], 'ok', reply)
                self.assertIn('measurements/observations.csv:', reply['data']['text'])
                self.assertIn(match, reply['data']['text'])
                self.assertEqual(reply['data']['returncode'], 0)
            self.assertNotEqual(replies[0]['data']['receipt'], replies[1]['data']['receipt'])
            self.assertIn('a,1', json.dumps(replies[2]))
            self.assertNotEqual(replies[3]['status'], 'ok', replies[3])
            return {'findings':[],'limitations':['The inaccessible file was not read.']}

        (self.directory/'outside.txt').write_text('Not authorized for this task.\n')
        self.review = review
        result = self._inspect(['notes.md'],['measurements/conditions.txt'])
        raw_result = deepcopy(result)
        record = 'records/'+result['data']['record'].split('/inspection/',1)[0].removeprefix('attempts/')+'/native_result.json'
        presented = self.app._coordination_result(result,record)
        self.assertEqual(result,raw_result)
        self.assertEqual(_assessments(presented),_assessments(result))
        self.assertTrue(all('windows' not in row for row in presented['data']['evidence']))
        self.assertEqual(presented['data']['source_observations']['record'],record)
        self.assertIn('a,1',json.dumps(result))
        self.assertNotIn('a,1',json.dumps(presented))
        assessment = self._assessment()
        self.assertEqual(assessment['verdict'],'passed')
        self.assertEqual(assessment['evidence_sha256'][str(self.inputs/'observations.csv')],
                         _digest(self.inputs/'observations.csv'))
        self.assertNotIn(str(self.directory/'outside.txt'),assessment['evidence_sha256'])
        self.assertNotIn('Not authorized for this task',json.dumps(self.inspection_views))
        self.assertNotIn('Not authorized for this task',json.dumps(self.inspection_requests))
        self.assertEqual(self.send.call_count,3)
        identity = Path(result['data']['record']).parts[1]
        saved_records = self._versions(self.root/f'tools/inspection/{identity}')
        self.assertTrue(any(path.name=='sources-0.json' for path in saved_records))
        saved_input = self.app.session.store.read_json(f'tools/inspection/{identity}/input.json')
        source_ids = saved_input['source_invocations']
        self.assertEqual(len(source_ids), 5)
        self.assertEqual(len(set(source_ids)), len(source_ids))
        trace = self.app.session.store.read_json(f'tools/inspection/{identity}/sources-1.json')
        searches = trace['results'][:2]
        process_records = {}
        for source_id, search, query in zip(source_ids[1:3], searches, ('sample', 'a,1')):
            self.assertEqual(search['arguments']['query'], query)
            receipt = self.root/search['result']['data']['receipt']
            process_records.update(self._versions(receipt.parent))
            invocation = json.loads(receipt.read_text(encoding='utf-8'))
            self.assertEqual(invocation['invocation_id'], source_id)
            separator = invocation['argv'].index('--')
            self.assertEqual(invocation['argv'][separator+1], query)
            self.assertEqual(set(invocation['argv'][separator+2:]),
                             {str(self.inputs/name) for name in ('conditions.txt','observations.csv')})
        usage = self.app.operations.inspection._usage(identity)
        charges = [row for row in usage if row['logical_call_id'] in source_ids[1:3]]
        self.assertEqual({row['logical_call_id'] for row in charges}, set(source_ids[1:3]))
        self.assertEqual(len(charges), 2)
        self.assertTrue(all(row['status']=='settled' and row['actual']['process_invocations']==1
                            for row in charges))
        self.assertEqual(sum(row.get('actual', {}).get('llm_requests', 0) for row in usage), 3)
        ledger = self.app.ledger.to_dict()
        self._reload()
        with patch('simple_ar.tools.workspace.run_process') as execute:
            cached = self.app.operations.inspection.call('inspect_artifacts',{
                'goal':'Check the claims against supplied observations','targets':['notes.md'],
                'evidence':['measurements/conditions.txt']},identity)
            execute.assert_not_called()
        self.assertEqual(cached.status,'ok',cached.error)
        self.assertEqual(self.send.call_count,3)
        self.assertEqual(self._versions(self.root/f'tools/inspection/{identity}'), saved_records)
        self.assertEqual(self.app.operations.inspection._usage(identity), usage)
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        for path, raw in process_records.items():
            self.assertEqual(path.read_bytes(), raw)
        (self.inputs/'observations.csv').write_text('sample,value\na,9\n')
        self.assertFalse(self.app.operations.inspection.can_reconcile('inspect_artifacts',identity))
        for path,raw in originals.items():
            path.write_bytes(raw)
        self.assertEqual(self._versions(self.inputs),originals)

    def test_publication_consumes_actual_assets_not_unrelated_failed_producer_outputs(self):
        if not shutil.which('pandoc'):
            self.skipTest('Real publication requires Pandoc')
        self._create()
        self._write('analyze.py', _PROGRAM)
        self._submit(_action('run_process', argv=['python', 'workspace/analyze.py',
            'measurements/observations.csv', 'workspace/live'],
            inputs=['analyze.py', 'measurements/observations.csv'],
            outputs=['live/table.csv', 'live/summary.json', 'live/figure.png']),
            approve=lambda call, token: True)
        self._ok('run_process')
        self._write('live/report.md', _BODY)
        self._submit(_action('run_process', argv=['python', '-c',
            "from pathlib import Path; import shutil; p=Path('live/old output'); p.mkdir(); "
            "shutil.copyfile('live/figure.png',p/'unused.png'); shutil.copyfile('live/figure.png',p/'missing.png')"],
            inputs=['live/figure.png'], outputs=['live/old output/unused.png', 'live/old output/missing.png']),
            approve=lambda call, token: True)
        self._ok('run_process')
        self._submit(_action('write_file', path='live/old output/unused.png',
            content='Changed unrelated bytes',
            expected_sha256=_digest(self.root/'workspace/live/old output/unused.png')))
        self._ok('write_file')
        (self.root/'workspace/live/old output/missing.png').unlink()
        ledger_before = [row.to_dict() for row in self.app.ledger.entries]
        exported = self._export('live/report.md')
        self.assertEqual(exported['status'], 'ok', exported)
        manifest = exported['data']['manifest']
        self.assertEqual(set(manifest['consumed_inputs']), {
            'report.md', 'references.bib', 'figures/figures_manifest.json', 'figure.png'})
        project = exported['data']['project']
        self._inspect(['live/report.md'])
        delivery = self._delivery('live/report.md', project)
        delivery['report']['formats'] = ['tex']
        self._submit(_action('deliver_result', **delivery))
        self._ok('deliver_result')
        self.assertEqual(self.app.session.manifest.status, 'completed')
        self.assertEqual([row.to_dict() for row in self.app.ledger.entries][:len(ledger_before)], ledger_before)
        identity = Path(project).name
        self._reload()
        ledger = self.app.ledger.to_dict()
        (self.root/'workspace/live/unrelated.svg').write_text('<svg/>')
        cached = self.app.operations.publication.call('export_report',
            {'markdown':'live/report.md', 'compile_pdf':False}, identity=identity)
        self.assertEqual(cached.status, 'ok', cached.error)
        self.assertEqual(cached.data, exported['data'])
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        script = self.root/'workspace/analyze.py'
        script_bytes = script.read_bytes()
        script.write_bytes(script_bytes+b'\n# Changed producer input\n')
        stale = self.app.operations.publication.call('export_report',
            {'markdown':'live/report.md', 'compile_pdf':False}, identity=identity)
        self.assertEqual(stale.status, 'error')
        self.assertIn('producer inputs/outputs changed', stale.error)
        script.write_bytes(script_bytes)
        # Actual consumed bytes still invalidate the receipt; stale unrelated
        # producers are not an excuse to weaken source/figure provenance.
        (self.root/'workspace/live/figure.png').write_bytes(b'changed consumed image')
        rejected = self.app.operations.publication.call('export_report',
            {'markdown':'live/report.md', 'compile_pdf':False}, identity=identity)
        self.assertEqual(rejected.status, 'error')
        self.assertEqual(self.app.ledger.to_dict(), ledger)

    def test_inspection_finishes_within_shared_capacity_and_returns_to_ordinary_delivery(self):
        self._create()
        self._write('report.md', 'The sample mean is 2.0 across three supplied observations.\n')
        self.app.ledger.limits['llm_requests'] = 7
        originals = self._versions(self.inputs)
        old_entries = [entry.to_dict() for entry in self.app.ledger.entries]

        def review(view):
            offered = {row['function']['name'] for row in self.inspection_requests[-1]['tools']}
            if offered == {'record_inspection'}:
                self.assertEqual(len(self.inspection_requests), 5)
                self.assertEqual(self.app.ledger.remaining('llm_requests'), 0)
                return {'findings': [], 'limitations': ['Only supplied observations support this claim.']}
            return [_action('read_file', path='measurements/conditions.txt')]

        self.review = review
        self.decisions.extend([
            _action('inspect_artifacts', goal='Check the mean and disclose scope.', targets=['report.md'],
                evidence=['measurements/observations.csv']),
            _action('deliver_result', **self._delivery('report.md'))])
        self._run(should_stop=lambda: False)
        self.assertEqual(self.app.session.manifest.status, 'completed', self.app.result_snapshot())
        self.assertEqual(self.send.call_count, 7)
        self.assertEqual(self._assessment()['verdict'], 'passed')
        self.assertEqual(self.app.ledger.remaining('llm_requests'), 0)
        self.assertFalse(self.app.ledger.over_limit())
        self.assertEqual([entry.to_dict() for entry in self.app.ledger.entries][:len(old_entries)], old_entries)
        self.assertEqual(self._versions(self.inputs), originals)
        calls, ledger = self.send.call_count, self.app.ledger.to_dict()
        self._reload()
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        self.assertEqual(self.send.call_count, calls)

    def test_bounded_native_retry_keeps_unknown_charge_and_can_review_then_deliver(self):
        from dataclasses import replace
        from simple_ar.integrations.llm import LLMStreamError
        self.services.llm_client = LLMClient(replace(self.services.llm_client._settings,
            retry_attempts=2, retry_base_delay_sec=0))
        for stage in ('native', 'inspection'):
            with self.subTest(stage=stage):
                self.root = self.directory/stage
                self._create()
                self._write('report.md', 'The sample mean is 2.0 across three supplied observations.\n')
                self.decisions.extend([
                    _action('inspect_artifacts', goal='Check the claim', targets=['report.md'],
                        evidence=['measurements/observations.csv']),
                    _action('deliver_result', **self._delivery('report.md'))])
                failed, start = False, self.send.call_count
                def provider(mode, request, **kwargs):
                    nonlocal failed
                    inspecting = any(t['function']['name']=='record_inspection' for t in request.get('tools', []))
                    if not failed and inspecting == (stage=='inspection'):
                        failed = True
                        raise LLMStreamError('Controlled opened stream interrupted before complete tool arguments')
                    return self._provider(mode, request, **kwargs)
                self.send.side_effect = provider
                self._run(should_stop=lambda:False)
                self.assertEqual(self.app.session.manifest.status, 'completed', self.app.result_snapshot())
                lost, = [entry for entry in self.app.ledger.entries if entry.status=='unknown']
                self.assertEqual(lost.actual_source, 'reservation_bound')
                self.assertEqual(lost.actual, {'llm_requests':1})
                self.assertGreater(lost.reserved['total_tokens'], 2048)
                inspected = self._ok('inspect_artifacts')
                self.assertEqual(self._assessment()['verdict'], 'passed')
                identity = Path(inspected['data']['record']).parts[1]
                saved = self.app.session.store.read_json(f'tools/inspection/{identity}/input.json')
                ledger = self.app.ledger.to_dict()
                self._reload()
                cached = self.app.operations.inspection.call('inspect_artifacts',saved['arguments'],identity)
                self.assertEqual(cached.status,'ok',cached)
                self.assertEqual(self.app.ledger.to_dict(), ledger)
                self.assertEqual(self.send.call_count-start, 4)

    def test_inspection_low_capacity_still_requires_a_real_terminal_opinion(self):
        # Request pressure, token pressure, and unknown prior usage all use
        # the same owner; none may synthesize a successful assessment.
        for mode in ('one_request', 'tokens', 'unoffered_read', 'unknown', 'bounded_prior'):
            with self.subTest(mode=mode):
                self.root = self.directory/mode
                self._create()
                self._write('report.md', 'The sample mean is 2.0.\n')
                ledger = self.app.ledger
                if mode in {'one_request', 'unoffered_read'}:
                    ledger.limits['llm_requests'] = 2  # caller floor + final opinion
                if mode == 'tokens':
                    # Direct operation below has no caller floor; its current
                    # input fits once, not a gathering turn plus final opinion.
                    ledger.limits['total_tokens'] = 5000
                if mode in {'unknown','bounded_prior'}:
                    ledger.reserve('lost', {'llm_requests': 1, 'total_tokens': 100})
                    ledger.mark_unknown('lost', reason='Response unavailable',
                        known_actual={'llm_requests':1}, retain_reservation=mode=='bounded_prior')
                    if mode=='bounded_prior':
                        ledger.limits['llm_requests'] = 3  # charge + caller floor + opinion
                before = len(self.inspection_requests)

                def review(view):
                    self.assertEqual({row['function']['name'] for row in
                        self.inspection_requests[-1]['tools']}, {'record_inspection'})
                    if mode == 'unoffered_read':
                        return [_action('read_file', path='measurements/conditions.txt')]
                    return {'findings': [{'subject': 'target-1', 'severity': 'required',
                        'message': 'The necessary source comparison has not been established.'}],
                        'limitations': ['Source gathering ended at the available capacity.']}

                self.review = review
                if mode == 'tokens':
                    result = self.app.operations.inspection.call('inspect_artifacts', {
                        'goal': 'Check the claim', 'targets': ['report.md'], 'evidence': []}, 'limited')
                else:
                    result = ToolResult.model_validate(self._inspect(['report.md'], []))
                self.assertFalse(self.transport_errors, self.transport_errors)
                self.assertEqual(len(self.inspection_requests)-before, 0 if mode=='unknown' else 1)
                self.assertEqual(_assessments(result.model_dump(mode='json'))[0]['verdict'],
                    'needs_revision' if mode in {'one_request', 'tokens','bounded_prior'} else 'incomplete')
                self.assertFalse(self._process_receipts())
                if mode == 'unoffered_read':
                    identity = Path(result.data['record']).parts[1]
                    trace = self.app.session.store.read_json(f'tools/inspection/{identity}/sources-0.json')
                    self.assertEqual(trace['results'], [])
                if mode in {'unknown','bounded_prior'}:
                    self.assertEqual(ledger.entries[0].status, 'unknown')

    def test_inspector_rejected_native_batches_reply_without_executing_and_can_recover(self):
        final = _action('record_inspection', findings=[], limitations=[])
        source = _action('read_file', path='measurements/conditions.txt')
        cases = {
            'unoffered_write': [_action('write_file', path='intrusion.txt', content='unapproved')],
            'mixed_final': [source, final],
            'final_then_source': [final, source],
            'multiple_final': [final, final],
            'invalid_shape': [{'tool_name':'read_file', 'arguments':['measurements/conditions.txt']}],
            'too_many_sources': [source]*9,
        }
        for name, batch in cases.items():
            with self.subTest(batch=name):
                self.root = self.directory/name
                self._create(report=True)
                self._write('report.md', 'The sample mean is 2.0.\n')
                before = len(self.inspection_requests)
                originals = self._versions(self.inputs)

                def recover(view):
                    if len(self.inspection_requests)==before+1:
                        return batch
                    self.assertEqual(len(self.inspection_requests), before+2)
                    replies = self._inspection_history()
                    self.assertEqual(len(replies), len(batch))
                    self.assertTrue(all(reply['status']!='ok' for reply in replies), replies)
                    return {'findings':[], 'limitations':[]}

                self.review = recover
                workspace = self.app.operations.inspection.workspace
                with patch.object(workspace, 'call', wraps=workspace.call) as reads:
                    self._inspect(['report.md'], [])
                self.assertEqual(self._assessment()['verdict'], 'passed')
                self.assertEqual(len(self.inspection_requests)-before, 2)
                # Admission and version checks can read the target; no rejected
                # batch may reach a source read or a workspace write.
                self.assertFalse(any(call.args[0]=='write_file' or
                    (call.args[0]=='read_file' and
                     call.args[1].get('path')=='measurements/conditions.txt')
                    for call in reads.call_args_list))
                self.assertFalse((self.root/'workspace/intrusion.txt').exists())
                self.assertEqual(self._versions(self.inputs), originals)
                self.assertFalse(self._process_receipts())

    def test_incomplete_source_investigation_supersedes_prior_pass_and_retains_completed_reads(self):
        self._create(report=True)
        self._write('report.md','The sample mean is 2.0.\n')
        self._inspect(['report.md'])
        self.assertEqual(self._assessment()['verdict'],'passed')
        first_round = len(self.inspection_requests)
        def invalid_reads(view):
            if len(self.inspection_requests)>first_round+1:
                replies = self._inspection_history()
                self.assertEqual(len(replies), 2)
                self.assertNotEqual(replies[1]['status'], 'ok', replies[1])
            if len(self.inspection_requests)==first_round+7:
                self.assertEqual({tool['function']['name']
                    for tool in self.inspection_requests[-1]['tools']}, {'record_inspection'})
                # The real provider also requested a read after only the final
                # tool was offered. Exhaustion must not grant another read.
                return [_action('list_directory', path='measurements')]
            return [
                _action('read_file', path='measurements/observations.csv'),
                _action('read_file', path='measurements/conditions.txt', max_bytes=-1)]
        self.review = invalid_reads
        workspace = self.app.operations.inspection.workspace
        with patch.object(workspace, 'call', wraps=workspace.call) as reads:
            result = self._inspect(['report.md'],['measurements/conditions.txt'])
        self.assertFalse(any(call.args[0]=='list_directory' for call in reads.call_args_list))
        self.assertEqual(len(self.inspection_requests)-first_round, 7)
        self.assertEqual(result['status'],'error')
        self.assertEqual(_assessments(result)[0]['verdict'],'incomplete')
        identity = Path(result['data']['record']).parts[1]
        saved = self.app.session.store.read_json(f'tools/inspection/{identity}/input.json')
        self.assertTrue(any(row.get('path')==str(self.inputs/'observations.csv') for row in saved['evidence']))
        trace = self.app.session.store.read_json(f'tools/inspection/{identity}/sources-0.json')
        self.assertEqual(len(trace['results']),2)
        self.assertEqual(trace['results'][0]['tool'],'read_file')
        calls = self.send.call_count
        self._reload()
        self._submit(_action('deliver_result',**self._delivery('report.md')))
        self._not_delivered()
        self.assertIn('lacks a complete independent inspection',self._last('deliver_result')['error'])
        self.assertEqual(self.send.call_count,calls)
        self.review = lambda view: {'findings':[],'limitations':[]}
        self._inspect(['report.md'])
        self._submit(_action('deliver_result',**self._delivery('report.md')))
        self._ok('deliver_result')
        self.assertEqual(self.app.session.manifest.status,'completed')

    def test_text_inspection_preserves_scope_and_does_not_promote_a_copy_to_evidence(self):
        self._create(report=True)
        missing = self._inspect(['missing.md'])
        self.assertEqual(missing['status'], 'error', missing)
        self.assertNotIn('assessment', missing['metadata'])
        self.assertFalse(missing['artifacts'])
        self.send.assert_not_called()
        # A failed selection is an observation, not an invalid assessment that
        # poisons subsequent valid review or final delivery in this same task.
        self._write('notes.md', 'The sample mean is 2.0 from three observations.\n')
        limitation = 'Sampling design and uncertainty are not established by this text check.'
        self.review = lambda view: {'findings': [], 'limitations': [limitation]}
        self._inspect(['notes.md'], [])
        assessment = self._assessment()
        self.assertEqual(assessment['verdict'], 'passed')
        self.assertEqual(assessment['coverage'], 'complete')
        self.assertFalse(assessment['evidence_sha256'])
        self.assertIn(limitation, assessment['limitations'])
        self.assertTrue(any('independent' in item.lower() for item in assessment['limitations']))
        observed = self._read('notes.md')
        self._submit(_action('copy_file', source='notes.md', source_sha256=observed['sha256'],
            path='claimed-evidence.md'))
        self._ok('copy_file')
        self._inspect(['notes.md'], ['claimed-evidence.md'])
        assessment = self._assessment()
        self.assertEqual(assessment['verdict'], 'passed')
        self.assertEqual(assessment['coverage'], 'complete')
        self.assertFalse(assessment['evidence_sha256'])
        self.assertIn(limitation, assessment['limitations'])
        self.assertTrue(any('independent' in item.lower() for item in assessment['limitations']))
        # A copied target is visible for cross-file comparison, but cannot be
        # selected as independent support for itself or identical bytes.
        self.assertTrue(self.inspection_views[-1]['evidence'][0]['text_windows'])
        self.review = lambda view: {'findings': [], 'limitations': []}
        self._inspect(['notes.md'])
        assessment = self._assessment()
        self.assertEqual(assessment['verdict'], 'passed')
        self.assertEqual(assessment['coverage'], 'complete')
        self.assertTrue(self.inspection_views[-1]['evidence'][0]['text_windows'])
        # A window that covers the complete small file is a valid FileWindow,
        # while an arbitrary nonempty prefix is never equivalent coverage.
        self.review = lambda view: {'findings': [], 'limitations': [limitation]}
        self._inspect([{'path': 'notes.md', 'start_line': 1, 'max_lines': 10}], [])
        self.assertEqual(self._assessment()['verdict'], 'passed')
        self.assertFalse(self._process_receipts())
        pdf_delivery = self._delivery('notes.md')
        pdf_delivery['report']['formats'] = ['pdf']
        self._submit(_action('deliver_result', **pdf_delivery))
        self._not_delivered()
        self.assertFalse(self._observations('export_report'))
        # The caller declared a report, with no PDF/TeX format obligation.
        # Current complete text inspection supports a Markdown delivery while
        # its retained limitations still disclaim independent scientific evidence.
        self._submit(_action('deliver_result', **self._delivery('notes.md'), limitations=[limitation]))
        self.assertEqual(self.app.session.manifest.status, 'completed', self.app.result_snapshot())
        self.assertEqual(self.app.session.store.read_json('delivery.json')['limitations'],[limitation])
        export = next(row['function'] for row in self.app.schemas if row['function']['name']=='export_report')
        self.assertNotIn('candidate_result',export['parameters']['properties'])

    def test_request_boundary_keeps_real_nonterminal_work_without_forcing_a_delivery(self):
        self.app = ToolApplication.create(self.root,'Prepare a useful note; explain any remaining work.',
            model='fixture',services=self.services,sources={},max_turns=1,max_tokens=100000,
            max_attempts=10,tool_contracts='full')
        self.decisions.append(_action('write_file',path='note.md',content='Saved work, not a completed task.'))
        result=self._run()
        self.assertEqual(result['status'],'paused',result)
        self.assertEqual((self.root/'workspace/note.md').read_text(),'Saved work, not a completed task.')
        self.assertEqual(self._last('write_file')['status'],'ok')
        self.assertFalse(self.app.session.store.exists('delivery.json'))
        calls=self.send.call_count
        self._reload()
        self._run()
        self.assertEqual(self.send.call_count,calls)

    def test_complete_prose_gets_one_accounted_action_correction_without_implicit_delivery(self):
        provider = self._provider
        for count, legacy, historical in ((1,False,False),(2,False,False),(1,True,False),(1,False,True)):
            with self.subTest(text_replies=count,legacy=legacy,historical=historical):
                self.root = self.directory/('prose-'+str(count)+'-'+str(legacy)+'-'+str(historical))
                self._create(legacy=legacy)
                if historical:
                    self.app.ledger.reserve('old-charge', {'llm_requests':1,'total_tokens':100})
                    self.app.ledger.mark_unknown('old-charge', reason='Prior allowance response unavailable')
                    old = self.app.ledger.entries[-1].to_dict()
                    self.app.ledger.authorize_remaining({key:self.app.ledger.limits[key]
                        for key in ('llm_requests','total_tokens')},
                        authorization_id='later-allowance', reason='Explicitly continue without clearing old fees')
                texts = deque(['Claimed complete without a delivery action.']*count)
                if count==1 and not legacy:
                    self.decisions.append(_action('ask_user',question='Which comparison is intended?'))
                def respond(mode,request,**kwargs):
                    if request.get('tools') and texts:
                        return {'choices':[{'message':{'role':'assistant','content':texts.popleft()},
                            'finish_reason':'stop'}],
                            'usage':{'prompt_tokens':100,'completion_tokens':10,'total_tokens':110}}
                    self.assertEqual(request['messages'][-1]['role'],'system')
                    self.assertIn('did not execute an operation',request['messages'][-1]['content'])
                    self.assertEqual(request['messages'][-2]['role'],'assistant')
                    return provider(mode,request,**kwargs)
                self.send.side_effect = respond
                before = self.send.call_count
                self._run()
                self.assertEqual(self.send.call_count-before,2 if not legacy else 1)
                self.assertFalse(self.app.session.store.exists('delivery.json'))
                calls = [entry for entry in self.app.ledger.entries if entry.reserved.get('llm_requests')
                    and entry.actual_source!='user_authorization' and entry.reservation_id!='old-charge']
                self.assertEqual(len(calls),2 if not legacy else 1)
                self.assertTrue(all(entry.status=='settled' for entry in calls))
                self.assertIn('Claimed complete',self.app.session.store.read_json(
                    self.app.state['turns'][0]['reply'])['content'])
                if count==1 and not legacy:
                    self._ok('ask_user')
                else:
                    self.assertIn('Model returned prose',self.app.session.manifest.status_reason)
                self._reload()
                if historical:
                    self.assertEqual(self.app.ledger.entries[0].to_dict(),old)
                    self.assertTrue(self.app.ledger.unknown_dimensions())
                    self.assertFalse(self.app.ledger.unknown_dimensions(current_allowance_only=True))
                if count==2:
                    calls = self.send.call_count
                    self._run()
                    self.assertEqual(self.send.call_count,calls)

    def test_inspection_transport_unknown_survives_reload_without_replay_or_free_usage(self):
        import httpx

        self._create()
        self._write('report.md', 'The sample mean is 2.0.\n')
        def lose_followup(view):
            if len(self.inspection_views)==1:
                return [_action('list_directory', path='measurements')]
            self.assertEqual(len(self._inspection_history()), 1)
            raise httpx.ReadTimeout('Response lost after dispatch')
        self.review = lose_followup
        self._inspect(['report.md'])
        row = self._last('inspect_artifacts')
        self.assertNotEqual(row['status'], 'ok', row)
        unknown = [entry.to_dict() for entry in self.app.ledger.entries if entry.status == 'unknown']
        self.assertTrue(unknown, self.app.ledger.to_dict())
        calls = self.send.call_count
        ledger = self.app.ledger.to_dict()
        inspection_record = self.root / row['data']['record']
        self.assertTrue(inspection_record.is_file(), row)
        saved = {path: path.read_bytes()
            for directory in (self.root / 'tools/inspection', inspection_record.parent)
            for path in directory.rglob('*.json')}
        self.assertIn(inspection_record, saved)
        self._reload()
        self._run()
        self.assertEqual(self.send.call_count, calls)
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        # Exercise the real operation's same-identity recovery too. Merely
        # leaving an idle caller queue unchanged would not prove no replay.
        recovered = self.app.gateway.call(ToolCall(tool_name='inspect_artifacts',
            trace_id=unknown[0]['attempt_id'], arguments={
                'goal': 'Check the claims against supplied observations', 'targets': ['report.md'],
                'evidence': ['measurements/observations.csv', 'measurements/conditions.txt']}))
        self.assertFalse(recovered.ok, recovered)
        self.assertEqual(self.send.call_count, calls)
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        for path, raw in saved.items():
            self.assertEqual(path.read_bytes(), raw)
        self._not_delivered()
        self.assertTrue((self.root / 'workspace/report.md').is_file())

        # A source process can be unknown even when the parent model settled.
        self.root = self.directory/'unknown-source-task'
        self._create()
        self._write('report.md', 'The sample mean is 2.0.\n')
        self.review = lambda view: [_action('search_files', path='measurements', query='sample')]
        def unknown_source(spec, *, budget_ledger):
            saved_input = next((self.root/'tools/inspection').glob('*/input.json'))
            self.assertIn(spec.invocation_id, json.loads(saved_input.read_text())['source_invocations'])
            reservation = f'process-{spec.invocation_id}'
            budget_ledger.reserve(reservation,
                {'process_invocations':1, 'process_wall_seconds':spec.timeout_sec},
                logical_call_id=spec.invocation_id, purpose='local_process')
            budget_ledger.mark_unknown(reservation, reason='Controlled loss of source process supervision')
            raise OSError('Source process outcome unknown')
        before = self.send.call_count
        with patch('simple_ar.tools.workspace.run_process', side_effect=unknown_source) as execute:
            self._inspect(['report.md'])
            execute.assert_called_once()
        row = self._last('inspect_artifacts')
        self.assertNotEqual(row['status'], 'ok', row)
        self.assertEqual(self.send.call_count, before+1)
        identity = Path(row['data']['record']).parts[1]
        trace = self.app.session.store.read_json(f'tools/inspection/{identity}/sources-0.json')
        self.assertEqual(len(trace['results']), 1)
        self.assertIn('Source process outcome unknown', trace['results'][0]['result']['error'])
        self.assertTrue(any(entry['status']=='unknown' and entry['reserved'].get('process_invocations')==1
                            for entry in trace['usage']))
        ledger = self.app.ledger.to_dict()
        saved = self._versions(self.root/f'tools/inspection/{identity}')
        self._reload()
        with patch('simple_ar.tools.workspace.run_process') as execute:
            recovered = self.app.operations.inspection.call('inspect_artifacts', {
                'goal':'Check the claims against supplied observations', 'targets':['report.md'],
                'evidence':['measurements/observations.csv','measurements/conditions.txt']}, identity)
            self.assertFalse(recovered.ok, recovered)
            execute.assert_not_called()
        self.assertEqual(self.send.call_count, before+1)
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        self.assertEqual(self._versions(self.root/f'tools/inspection/{identity}'), saved)

    def test_frozen_code_contract_rejects_generic_bypass_and_unvalidated_success_but_keeps_partial_work(self):
        # Minimal exact contract used by the existing authority integration in
        # test_tool_agent_handoff; no implementation or validation is invented.
        (self.inputs / 'main.py').write_text('VALUE = 1\n', encoding='utf-8')
        (self.inputs / 'check.py').write_text('from main import VALUE\nassert VALUE == 2\n',
            encoding='utf-8')
        contract = {'project_path': 'project', 'goal': 'Repair the supplied source',
            'allowed_patterns': ['main.py'], 'validation_command': ['python', 'check.py']}
        self.app = ToolApplication.create(self.root, contract['goal'], model='fixture',
            services=self.services,
            control_protocol='operations.v1', sources={'project': self.inputs},
            code_task=contract, allow_execution=True, tool_contracts='full', max_attempts=30)
        # This unrelated pre-existing user note proves file presence alone
        # cannot satisfy the frozen implementation/validation obligation.
        note = self.app.workspace.workspace / 'note.md'
        note.write_text('The supplied source has not been repaired or validated.\n', encoding='utf-8')
        self.assertIsNotNone(self.app.gateway.registry.get('implement_code_task'))
        for action in (
            _action('write_file', path='escape.py', content='print("unrelated success")\n'),
            _action('run_process', argv=['python', '-c',
                "from pathlib import Path; Path('escape.txt').write_text('unrelated success')"]),
        ):
            with self.subTest(tool=action['tool_name']):
                with self.assertRaisesRegex(ValueError, 'unavailable'):
                    self._submit(action)
        self.assertFalse((self.app.workspace.workspace / 'escape.py').exists())
        self.assertFalse((self.app.workspace.workspace / 'escape.txt').exists())
        delivery = {'summary': 'Retained user note', 'artifacts': ['note.md']}
        self._submit(_action('deliver_result', **delivery))
        self._not_delivered()
        self.assertNotEqual(self._last('deliver_result')['status'], 'ok')
        unresolved = ['The frozen code repair and its validation remain unperformed.']
        self._submit(_action('deliver_result', **delivery, unresolved=unresolved))
        self.assertEqual(self.app.session.manifest.status, 'partial', self.app.result_snapshot())
        self.assertEqual(self.app.session.store.read_json('delivery.json')['unresolved'], unresolved)
        self.assertTrue(note.is_file())
        self.assertEqual((self.inputs / 'main.py').read_text(encoding='utf-8'), 'VALUE = 1\n')
        self.assertFalse(self._process_receipts())
        self.send.assert_not_called()

    def test_frozen_domain_obligations_survive_reload_and_partial_disclosure(self):
        table = {'path':'measurements/observations.csv',
                 'spec':{'value_columns':['value'],'observation_unit':'one supplied observation'}}
        for summary in (False,True):
            with self.subTest(required_summary=summary):
                self.root = self.directory / ('summary-required' if summary else 'table-required')
                self.app = ToolApplication.create(self.root,'Analyze the supplied observations',
                    model='fixture',services=self.services,sources={'measurements':self.inputs},
                    table_analysis=table,research_policy={'require_summary':summary},
                    max_attempts=40,tool_contracts='full')
                self._write('note.md','A note cannot replace the frozen analysis.\n')
                delivery = {'summary':'Supplied observations','artifacts':['note.md']}
                self._submit(_action('deliver_result',**delivery))
                self.assertIn('exact table analysis',self._last('deliver_result')['error'])
                self._not_delivered()
                self._submit(_action('analyze_table',**table))
                analysis = self._ok('analyze_table')
                original = {name: self.app.workspace.resolve(name).read_bytes()
                            for name in analysis['data']['artifact_sha256']}
                calls = self.send.call_count
                self._reload()
                self._submit(_action('deliver_result',**delivery))
                if summary:
                    self.assertIn('Required research summary',self._last('deliver_result')['error'])
                    self._not_delivered()
                    self._submit(_action('deliver_result',**delivery,
                        unresolved=['The required research summary remains unfinished.']))
                    self.assertEqual(self.app.session.manifest.status,'partial')
                else:
                    self.assertEqual(self.app.session.manifest.status,'completed')
                    self.app.supply(supply_id='replace-analysis',reason='Reconfirm the exact analysis for this request',
                        changes={'table_analysis':table},replace_existing=True)
                    self._reload()
                    self._submit(_action('deliver_result',**delivery))
                    self.assertIn('exact table analysis',self._last('deliver_result')['error'])
                    self.assertNotEqual(self.app.session.manifest.status,'completed')
                    self._submit(_action('analyze_table',**table))
                    current = self._ok('analyze_table')
                    self._submit(_action('deliver_result',**delivery))
                    self.assertEqual(self.app.session.manifest.status,'completed')
                    # A new request still needs the intact frozen result, even
                    # when delivering a different file after history restore.
                    artifact = self.app.workspace.resolve(next(iter(current['data']['artifact_sha256'])))
                    artifact.write_bytes(artifact.read_bytes()+b'\nchanged')
                    self._submit(_action('deliver_result',**delivery),feedback='Deliver the retained analysis again.')
                    self.assertNotEqual(self._last('deliver_result')['status'],'ok')
                    self.assertNotEqual(self.app.session.manifest.status,'completed')
                self.assertEqual(self.send.call_count,calls)
                if summary:
                    for name,raw in original.items():
                        self.assertEqual(self.app.workspace.resolve(name).read_bytes(),raw)

        from simple_ar.tools.writing import freeze_report_requirement
        self.root = self.directory/'report-required'
        requirement = freeze_report_requirement({'outline_strategy':'template','max_cited_sources':1})
        self.assertTrue(requirement['outline'])
        self.app = ToolApplication.create(self.root,'Report the supplied observations in the fixed outline',
            model='fixture',services=self.services,sources={'measurements':self.inputs},
            report_requirement=requirement,max_attempts=60,tool_contracts='full')
        self._write('report.md','# Observations\n\n## Unrequested heading\n\n'+_SCOPE+'\n')
        delivery = {'summary':'Supplied observations','artifacts':['report.md']}
        self._reload()
        self._submit(_action('deliver_result',**delivery))
        self.assertIn('explicit report claim',self._last('deliver_result')['error'])
        self._not_delivered()
        self._submit(_action('deliver_result',**delivery,
            unresolved=['The frozen formal report is not yet complete.']))
        self._ok('deliver_result')
        self.assertEqual(self.app.session.manifest.status,'partial')

        # Inspection does not replace explicit output-contract enforcement.
        self._submit(_action('inspect_artifacts',goal='Check the actual supplied observations',
            targets=['report.md'],evidence=['measurements/observations.csv']),
            feedback='Complete the frozen formal report.')
        self._ok('inspect_artifacts')
        canonical = self._delivery('report.md')
        self._submit(_action('deliver_result',**canonical))
        self.assertIn('fixed section structure',self._last('deliver_result')['error'])
        body = '# Observations\n\n'+'\n\n'.join(
            '## '+row['heading']+'\n\n'+_SCOPE for row in requirement['outline'])+'\n'
        for citations,error in (('[@P1; @P2]','max_cited_sources'),('[@P1] and again [@P1]',None)):
            version = self._read('report.md')['sha256']
            self._submit(_action('write_file',path='report.md',expected_sha256=version,content=body+citations+'\n'))
            self._ok('write_file')
            self._inspect(['report.md'])
            self._ok('inspect_artifacts')
            self.assertEqual(self._assessment()['coverage'], 'complete')
            self._reload()
            self._submit(_action('deliver_result',**canonical))
            if error:
                self.assertIn(error,self._last('deliver_result')['error'])
                self.assertNotEqual(self.app.session.manifest.status,'completed')
            else:
                self._ok('deliver_result')
                self.assertEqual(self.app.session.manifest.status,'completed',self.app.result_snapshot())

    def test_inline_producers_validate_actual_outputs_and_repair_stale_process_edges(self):
        for kind in ('write','process'):
            with self.subTest(producer=kind):
                self.root = self.directory / ('inline-'+kind)
                self._create(delivery_files=['final.txt','conditions.txt'] if kind=='write' else None)
                delivery = {'summary':'Actual final output','artifacts':['final.txt']}
                if kind == 'write':
                    self._submit(_action('write_file',path='final.txt',content='finished\n',delivery=delivery))
                    self._ok('write_file')
                    self._not_delivered()
                    self.assertIn('conditions.txt','; '.join(self.app._delivery_feedback()['issues']))
                    self._write('conditions.txt','Execution and interpretation are distinct.\n')
                    self._submit(_action('deliver_result',summary='Actual final output',
                        artifacts=['final.txt','conditions.txt']))
                    self._ok('deliver_result')
                else:
                    self._write('produce.py',
                        "from pathlib import Path\nPath('final.txt').write_text('old\\n')\n")
                    command = {'argv':['python','produce.py'],'inputs':['produce.py'],'outputs':['final.txt']}
                    self._submit(_action('run_process',**command),approve=lambda *_:True)
                    self._ok('run_process')
                    script = self._read('produce.py')
                    self._submit(_action('edit_file',path='produce.py',expected_sha256=script['sha256'],
                        old_text="'old\\n'",new_text="'finished\\n'"))
                    self._ok('edit_file')
                    # The previous producer is stale until this approved final
                    # run succeeds. Preflight must not reject its replacement.
                    self._submit(_action('run_process',**command,delivery=delivery),approve=lambda *_:True)
                    self._ok('run_process')
                self.assertEqual(self.app.session.manifest.status,'completed',self.app.result_snapshot())
                self.assertEqual((self.root/'workspace/final.txt').read_text(),'finished\n')
                bound = self.app.session.store.read_json('delivery.json')
                receipts = self._process_receipts()
                self._reload()
                self._run()
                self.assertEqual(self.app.session.store.read_json('delivery.json'),bound)
                self.assertEqual(self._process_receipts(),receipts)

        for program in ("print('no output')","raise SystemExit(2)"):
            with self.subTest(failed_producer=program):
                self.root = self.directory / ('missing-output' if program.startswith('print') else 'failed-output')
                self._create()
                self._submit(_action('run_process',argv=['python','-c',program],
                    delivery={'summary':'Unproduced output','artifacts':['missing.txt']}),approve=lambda *_:True)
                self.assertNotEqual(self._last('run_process')['status'],'ok')
                self._not_delivered()
                receipts = self._process_receipts()
                self._reload()
                self._run()
                self.assertEqual(self._process_receipts(),receipts)
        self.send.assert_not_called()
        self.root = self.directory/'inline-measurement-repair'
        self._create()
        measurement = {'command':['python','-c',
            "from pathlib import Path; print('score: 1' if Path('ready.txt').exists() else 'missing')"],
            'cwd':str(self.app.workspace.workspace),'timeout_sec':5,
            'result_schema':{'primary_metric':'score','required_metrics':['score']}}
        self._submit(_action('measure_experiment',**measurement),approve=lambda *_:True)
        failed = self._ok('measure_experiment')
        failed_record = self.app.workspace.resolve(failed['data']['result_ref'])
        failed_bytes = failed_record.read_bytes()
        canonical = json.loads(failed_bytes)
        # Process success does not establish required metric coverage. The
        # scientific owner's delivery check must reject the actual missing fact.
        self.assertNotIn('score',canonical['metrics'],canonical)
        self._write('ready.txt','The same measurement can now observe its required metric.\n')
        delivery = {'summary':'The corrected actual measurement','artifacts':['ready.txt']}
        self._submit(_action('deliver_result',**delivery))
        self.assertNotEqual(self._last('deliver_result')['status'],'ok')
        self.assertIn('missing/nonfinite required metrics: score',self._last('deliver_result')['error'])
        self._not_delivered()
        self._reload()
        self._submit(_action('measure_experiment',**measurement,delivery=delivery),approve=lambda *_:True)
        self._ok('measure_experiment')
        self.assertEqual(self.app.session.manifest.status,'completed',self.app.result_snapshot())
        self.assertEqual(failed_record.read_bytes(),failed_bytes)
        self.send.assert_not_called()
        self.root = self.directory/'inline-inspection-version'
        self._create()
        self._write('report.md','The sample mean is 2.0.\n')
        self._inspect(['report.md'])
        version = self._read('report.md')['sha256']
        self._submit(_action('write_file',path='report.md',expected_sha256=version,
            content='The sample mean is 9.0.\n',delivery=self._delivery('report.md')))
        self.assertIn('lacks a complete independent inspection','; '.join(self.app._delivery_feedback()['issues']))
        self._ok('write_file')  # Actual work is retained; its old review cannot certify new bytes.
        self._not_delivered()
        receipt = self.app.state['turns'][-1]['tools'][-1]['result']
        original = (self.root/receipt).read_bytes()
        self._reload()
        self._run()
        self.assertEqual((self.root/receipt).read_bytes(),original)
        self.assertNotIn('Retained terminal observation cannot be committed',self.app.session.manifest.status_reason)
        self._submit(_action('read_file',path='report.md'),feedback='Continue the original task, preserving completed work.')
        self.assertEqual(self.app._delivery_feedback()['request_count'],1)
        version = self._ok('read_file')['data']['sha256']
        self._submit(_action('write_file',path='report.md',expected_sha256=version,
            content='The sample mean is 2.0.\n'))
        self._ok('write_file')
        self._inspect(['report.md'],['measurements/observations.csv'])
        self._submit(_action('deliver_result',**self._delivery('report.md')))
        self._ok('deliver_result')
        self.assertEqual(self.app.session.manifest.status,'completed')

    def test_prepared_measurements_feed_exact_work_and_delivery_tracks_current_revision(self):
        from simple_ar.code_task.runtime.state import (
            code_task_paths,implementation_file_hashes,load_code_task_manifest)
        from simple_ar.code_task.execution.runner import run_code_task_benchmark

        (self.inputs/'main.py').write_text('VALUE = 1\n',encoding='utf-8')
        (self.inputs/'check.py').write_text('from main import VALUE\nassert VALUE >= 2\n',encoding='utf-8')
        (self.inputs/'evaluate.py').write_text('from main import VALUE\nprint("score: " + str(VALUE))\n',encoding='utf-8')
        contract = {'project_path':'project','goal':'Improve the candidate using its unchanged baseline',
            'allowed_patterns':['main.py'],'validation_command':['python','check.py'],'timeout_sec':5,
            'preparation':{'kind':'existing_project','workspace_mode':'copy'}}
        execution = {'command':['python','evaluate.py'],'cwd':str(self.inputs),'timeout_sec':5,
            'result_schema':{'primary_metric':'score','required_metrics':['score']},
            # These are unseeded deterministic synthetic evaluations. Explicit
            # shared conditions and unchanged evaluator bytes establish the
            # fixture comparison; code validation alone cannot establish it.
            'protocol':{'contract_id':'synthetic-value-comparison',
                'dataset_refs':[{'asset_id':'constant-value-fixture'}],
                'split_spec':{'name':'single fixed synthetic case'},
                'metric_specs':[{'name':'score','direction':'higher'}],
                'comparison_conditions':{'evaluation':'same deterministic evaluator','randomness':'none'},
                'protected_assets':[{'asset_id':'evaluator','path':'evaluate.py'}]}}
        conditions = {'conditions':[
            {'id':'baseline','role':'baseline','seed':None,'source':'preparation',
             'before_implementation':True,'execution':execution},
            {'id':'candidate','role':'candidate','seed':None,'source':'implementation','execution':execution}]}
        self.app = ToolApplication.create(self.root,contract['goal'],model='fixture',services=self.services,
            sources={'project':self.inputs},code_task=contract,execution_contract=conditions,
            allow_execution=True,tool_contracts='full',max_attempts=80,
            process_limits={'process_invocations':32,'process_wall_seconds':180})
        self._submit(_action('prepare_code_task'),approve=lambda *_:True)
        prepared = self._ok('prepare_code_task')
        prepared_ref = self.app.workspace.locator(str(self.root/prepared['artifacts'][0]))
        source = self.app.implementation.prepared_source(prepared_ref,identity='read-baseline')
        work = {'prepared_result':prepared_ref}
        with self.assertRaisesRegex(ValueError,'required prepared-source measurements'):
            self.app.implementation.approval_binding(work,identity='too-early')
        self._submit(_action('measure_experiment',**{**execution,'cwd':source['cwd']},
            condition_id='baseline',preparation_ref=prepared_ref),approve=lambda *_:True)
        self._ok('measure_experiment')
        self._reload()
        binding = self.app.implementation.approval_binding(work,identity='after-baseline')
        self.assertEqual(binding['measurement_inputs']['observations'][0]['metrics']['score'],1)

        value = 2
        def generate(*,context,request):
            # Substitute code generation only. Preparation, frozen approval,
            # actual CPU validator and all native/domain receipts remain real.
            brief = context.store.read_json('inputs/brief.json')['request_text']
            if 'conditions' in self.app.implementation.measurement:
                self.assertIn('Recorded prior measurement evidence',brief)
            candidate = code_task_paths(request.run_dir).workspace_dir
            (candidate/'main.py').write_text(f'VALUE = {value}\n',encoding='utf-8')
            validation = run_code_task_benchmark(request.run_dir,command=shlex.join(request.validation_command),
                skip_validation=True,timeout_sec=5,budget_ledger=self.app.ledger,
                session_id=self.app.session.manifest.session_id,attempt_id=request.run_dir.name)
            self.assertEqual(validation.status,'passed')
            report = context.store.write_text('code_task/validation.json',validation.report_path.read_text(encoding='utf-8'))
            context.store.write_json('implementation.json',{'schema_version':'research_implementation.v1',
                'status':'validated','next_action':'inspect',
                'native_binding':load_code_task_manifest(request.run_dir)['code_task']['native_binding'],
                'validation':{'command':list(request.validation_command),'status':validation.status,
                    'timeout_sec':5,'returncode':validation.returncode,'timed_out':validation.timed_out},
                'artifact_refs':{'validation_report':report.to_dict()},
                'artifact_hashes':{report.path:_digest(context.store.resolve(report))},
                'implementation_file_hashes':implementation_file_hashes(request.run_dir),
                'workspace_dir':str(candidate),'code_task_run_dir':str(request.run_dir)})

        with patch('simple_ar.research.implementation.run_implementation_capability',side_effect=generate):
            self._submit(_action('implement_code_task',**work),approve=lambda *_:True)
            first = self._ok('implement_code_task')
            first_ref = self.app.workspace.locator(str(self.root/first['artifacts'][0]))
            source = self.app.implementation.measurement_source(first_ref,identity='read-candidate')
            self._submit(_action('measure_experiment',**{**execution,'cwd':source['cwd']},
                condition_id='candidate',implementation_ref=first_ref),approve=lambda *_:True)
            self._ok('measure_experiment')
            refs = [row['data']['result_ref'] for row in self._observations('measure_experiment')]
            self._submit(_action('analyze_measurements',**self.app.scientific.analysis_arguments(refs)),approve=lambda *_:True)
            analysis = self._ok('analyze_measurements')
            self.assertEqual(analysis['data']['comparison_gaps'],[],analysis)
            self.assertEqual(analysis['data']['analysis_status'],'passed',analysis)
            prior_records = {path:path.read_bytes() for path in (self.root/'attempts').glob('*/native_result.json')}
            value = 3
            self._submit(_action('implement_code_task',prior_result=first_ref,
                instruction='Improve the same candidate further'),approve=lambda *_:True)
            second = self._ok('implement_code_task')
        delivery = {'summary':'Current validated candidate','artifacts':[second['data']['project_path']]}
        self._reload()
        self._submit(_action('deliver_result',**delivery))
        self.assertNotEqual(self._last('deliver_result')['status'],'ok')
        self.assertIn('candidate',self._last('deliver_result')['error'])
        self._not_delivered()
        second_ref = self.app.workspace.locator(str(self.root/second['artifacts'][0]))
        source = self.app.implementation.measurement_source(second_ref,identity='read-current')
        self._submit(_action('measure_experiment',**{**execution,'cwd':source['cwd']},
            condition_id='candidate',implementation_ref=second_ref),approve=lambda *_:True)
        self._ok('measure_experiment')
        # Selection and matching analysis belong to the validated scientific owner.
        refs = [row['data']['result_ref'] for row in self._observations('measure_experiment')]
        selected = self.app.scientific.report_evidence(refs,[second['artifacts'][0]])
        current = [self.app.workspace.locator(str(self.root/ref)) for ref in selected['measurement_refs']]
        self._submit(_action('analyze_measurements',**self.app.scientific.analysis_arguments(current)),approve=lambda *_:True)
        analysis = self._ok('analyze_measurements')
        self.assertEqual(analysis['data']['comparison_gaps'],[],analysis)
        self.assertEqual(analysis['data']['analysis_status'],'passed',analysis)
        receipts = self._process_receipts()
        self._reload()
        self._submit(_action('deliver_result',**delivery))
        self._ok('deliver_result')  # Surface the actual native error before the larger session snapshot.
        self.assertEqual(self.app.session.manifest.status,'completed',self.app.result_snapshot())
        self.assertEqual(self._process_receipts(),receipts)
        for path,raw in prior_records.items():
            self.assertEqual(path.read_bytes(),raw)
        self.assertEqual((self.inputs/'main.py').read_text(),'VALUE = 1\n')
        self.send.assert_not_called()

        # A flat frozen measurement has the same revision requirement. Its
        # actual final measurement can deliver inline through the common gate.
        self.root = self.directory/'flat-code-measurement'
        self.app = ToolApplication.create(self.root,contract['goal'],model='fixture',services=self.services,
            sources={'project':self.inputs},code_task=contract,execution_contract=execution,
            allow_execution=True,tool_contracts='full',max_attempts=50,
            process_limits={'process_invocations':16,'process_wall_seconds':90})
        value = 2
        with patch('simple_ar.research.implementation.run_implementation_capability',side_effect=generate):
            self._submit(_action('implement_code_task'),approve=lambda *_:True)
            first = self._ok('implement_code_task')
            first_ref = self.app.workspace.locator(str(self.root/first['artifacts'][0]))
            source = self.app.implementation.measurement_source(first_ref,identity='flat-first')
            self._submit(_action('measure_experiment',**{**execution,'cwd':source['cwd']},
                implementation_ref=first_ref),approve=lambda *_:True)
            self._ok('measure_experiment')
            value = 3
            self._submit(_action('implement_code_task',prior_result=first_ref),approve=lambda *_:True)
            second = self._ok('implement_code_task')
        delivery = {'summary':'Current measured candidate','artifacts':[second['data']['project_path']]}
        self._submit(_action('deliver_result',**delivery))
        self.assertIn('current implementation has no formal measurement',self._last('deliver_result')['error'])
        self._not_delivered()
        second_ref = self.app.workspace.locator(str(self.root/second['artifacts'][0]))
        source = self.app.implementation.measurement_source(second_ref,identity='flat-second')
        self._submit(_action('measure_experiment',**{**execution,'cwd':source['cwd']},
            implementation_ref=second_ref,delivery=delivery),approve=lambda *_:True)
        self._ok('measure_experiment')
        self.assertEqual(self.app.session.manifest.status,'completed',self.app.result_snapshot())
        self.send.assert_not_called()

        # Reused controls have no producing-source lineage. Preserve their
        # original physical identity and digest when feeding a new exact owner.
        self.root = self.directory/'independent-control'
        control = {'command':[sys.executable,'-c',"print('score: 1')"],'cwd':str(self.inputs),
            'timeout_sec':5,'result_schema':execution['result_schema'],
            'protocol':{'contract_id':'retained-control','dataset_refs':[{'asset_id':'conditions'}],
                'split_spec':{'name':'supplied'},'comparison_conditions':{'split':'fixed'},
                'metric_specs':[{'name':'score','direction':'higher'}],
                'protected_assets':[{'asset_id':'conditions','path':'conditions.txt'}]}}
        self.app = ToolApplication.create(self.root,'Measure the independent control',model='fixture',
            services=self.services,sources={'project':self.inputs},execution_contract=control,
            allow_execution=True,tool_contracts='full',max_attempts=12,
            process_limits={'process_invocations':2,'process_wall_seconds':10})
        self._submit(_action('measure_experiment',**control),approve=lambda *_:True)
        measured = self._ok('measure_experiment')
        previous_root = self.root
        original_record = self.app.workspace.resolve(measured['data']['result_ref'])
        imported = 'prior/'+original_record.relative_to(previous_root).as_posix()
        reused = {'conditions':[
            {'id':'baseline','role':'baseline','seed':None,'source':'declared',
             'reuse_ref':imported,'execution':control},
            {'id':'candidate','role':'candidate','seed':None,'source':'implementation','execution':execution}]}
        self.root = self.directory/'reused-control'
        self.app = ToolApplication.create(self.root,contract['goal'],model='fixture',services=self.services,
            sources={'project':self.inputs,'prior':previous_root},code_task=contract,execution_contract=reused,
            allow_execution=True,tool_contracts='full',max_attempts=20,
            process_limits={'process_invocations':8,'process_wall_seconds':45})
        self._reload()
        ledger = self.app.ledger.to_dict()
        binding = self.app.implementation.approval_binding({},identity='consume-control')
        evidence = binding['measurement_inputs']
        self.assertEqual(evidence['observations'][0]['metrics']['score'],1)
        canonical = json.loads(original_record.read_bytes())
        self.assertEqual(evidence['observations'][0]['measurement']['measurement_id'],
            canonical['measurement']['measurement_id'])
        self.assertEqual(evidence['observations'][0]['measurement_source']['origin'],'reused')
        self.assertEqual(self.app.ledger.to_dict(),ledger)
        frozen = next(row for row in self.app.scientific.execution_contract['conditions'] if row.get('reuse_ref'))
        snapshot = self.app.workspace.resolve(frozen['reuse_ref'])
        original = snapshot.read_bytes()
        try:
            snapshot.write_bytes(original+b'\n')
            with self.assertRaisesRegex(ValueError,'digest'):
                self.app.implementation.approval_binding({},identity='changed-control')
        finally:
            snapshot.write_bytes(original)
        self.assertEqual(original_record.read_bytes(),original)
        self.send.assert_not_called()

    def test_saved_default_legacy_approval_resumes_real_process_without_upgrade_or_replay(self):
        self._create(legacy=True)
        self.assertNotIn('control_protocol', self.app.state['config'])
        self._write('count.py', "from pathlib import Path\np = Path('count.txt')\n"
            "p.write_text(p.read_text() + 'ran\\n' if p.exists() else 'ran\\n')\n")
        self._submit(_action('run_process', argv=['python', 'count.py'], timeout_sec=5,
            inputs=['count.py'], outputs=['count.txt']))
        pending = self.app.state['pending']
        self.assertIsNotNone(pending, self.app.result_snapshot())
        token = self.app.approval_token(pending['call'])
        self.assertFalse(self._process_receipts())
        config = self.app.state['config'].copy()
        ledger = self.app.ledger.to_dict()
        self._reload()
        self.assertEqual(self.app.state['config'], config)
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        self._run(approved_execution=token)
        self._ok('run_process')
        self.assertEqual((self.root / 'workspace/count.txt').read_text(), 'ran\n')
        receipts = self._process_receipts()
        self.assertTrue(receipts)
        self._reload()
        self._run()
        self.assertEqual(self._process_receipts(), receipts)
        self.assertEqual((self.root / 'workspace/count.txt').read_text(), 'ran\n')
        self.assertNotIn('control_protocol', self.app.state['config'])
        self._submit(_action('deliver_result', summary='Actual execution retained', artifacts=['count.txt']))
        self.assertEqual(self.app.session.manifest.status, 'completed', self.app.result_snapshot())
        self.assertFalse(self.inspection_views, 'Legacy execution acquired a new inspection lifecycle')
