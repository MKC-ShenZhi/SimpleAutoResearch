"""User outcomes through the existing ToolApplication loop.

Provider transport (and explicitly marked code generation) is controlled. Workspace edits, Python execution,
approval, accounting, inspection, publication and recovery use production code.
These are server/offline integration scenarios, not scientific or visual QA.
The ordinary path explicitly opts into operations.v1; the final scenario keeps
one genuinely saved default-legacy task without fabricating contract variants.
"""
from __future__ import annotations

from collections import deque
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
from simple_ar.tools.specs import ToolCall


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
        self.transport_errors = []
        self.review = lambda view: {'findings': [], 'limitations': []}
        self.send = self.enterContext(patch('simple_ar.integrations.llm._call_openai_sdk',
            side_effect=self._provider))
        self.app = None

    def _create(self, *, report=False, allow_execution=True, legacy=False):
        options = {'control_protocol': None if legacy else 'operations.v1'}
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

    def _provider(self, mode, request, **kwargs):
        # A single controlled seam for both the native model and independent review.
        # We do not choose responses using invocation labels or internal stages.
        try:
            self.assertEqual(mode, 'chat')
            if request.get('tools'):
                self.assertTrue(self.decisions, 'Unexpected model work after the supplied decisions')
                action = self.decisions.popleft()
                action = action() if callable(action) else action
                offered = {tool['function']['name'] for tool in request['tools']}
                self.assertIn(action['tool_name'], offered)
                message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': f'decision-{self.send.call_count}', 'type': 'function',
                    'function': {'name': action['tool_name'],
                        'arguments': json.dumps(action['arguments'])}}]}
                finish = 'tool_calls'
            else:
                view, _ = json.JSONDecoder().raw_decode(request['messages'][1]['content'])
                self.assertIn('findings', view['response_schema']['properties'])
                self.assertTrue(view['targets'])
                self.assertIn('evidence', view)
                ids = [row['subject'] for row in view['targets']]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertTrue(all(row['text_windows'] for row in view['targets']))
                for row in [*view['targets'], *view['evidence']]:
                    for window in row['text_windows']:
                        self.assertIsInstance(window['text'], str)
                self.inspection_views.append(view)
                response = self.review(view)
                message = {'role': 'assistant', 'content': json.dumps(response)}
                finish = 'stop'
            return {'choices': [{'message': message, 'finish_reason': finish}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 40, 'total_tokens': 140}}
        except AssertionError as exc:
            # Production code retains provider failures; surface fixture mistakes at the test boundary.
            self.transport_errors.append(str(exc))
            raise

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
        self._submit(_action('inspect_artifacts', goal='Check the claims against supplied observations',
            targets=targets, evidence=evidence if evidence is not None else [
                'measurements/observations.csv', 'measurements/conditions.txt']))
        return self._last('inspect_artifacts')

    def _assessment(self):
        result = self._ok('inspect_artifacts')
        assessment = result['metadata']['assessment']
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
                delivery=self._delivery('v1/report.md', 'v1/table.csv', 'v1/figure.png', 'analyze.py')),
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
        self.assertEqual(self._last('inspect_artifacts')['metadata']['assessment']['verdict'], 'passed')

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
        self.assertFalse(self.inspection_views[-1]['evidence'][0]['text_windows'])
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

    def test_inspection_transport_unknown_survives_reload_without_replay_or_free_usage(self):
        import httpx

        self._create()
        self._write('report.md', 'The sample mean is 2.0.\n')
        self.review = lambda view: (_ for _ in ()).throw(httpx.ReadTimeout('Response lost after dispatch'))
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
                self._create()
                delivery = {'summary':'Actual final output','artifacts':['final.txt']}
                if kind == 'write':
                    self._submit(_action('write_file',path='final.txt',content='finished\n',delivery=delivery))
                    self._ok('write_file')
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
        self.assertIn('lacks a complete independent inspection',self.app.session.manifest.status_reason)
        self._ok('write_file')  # Actual work is retained; its old review cannot certify new bytes.
        self._not_delivered()

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
