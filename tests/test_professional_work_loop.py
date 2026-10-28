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
from tests.native_history_fixture import load_native_history
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
        self.document_views = []
        self.document_decision = None
        self.transport_errors = []
        self.review = self._review_complete_targets
        self.inspection_partial_targets = set()
        self.send = self.enterContext(patch('simple_ar.integrations.llm._call_openai_sdk',
            side_effect=self._provider))
        self.app = None

    def _create(self, *, report=False, allow_execution=True, delivery_files=None, context_protocol='auto', goal=None,
                saved_scope=False):
        if saved_scope:
            from tests.native_history_fixture import load_prepared_operation_history
            self.app = load_prepared_operation_history(self.root, goal or 'Continue the saved professional work.',
                config={'model':'fixture', 'sources':{'measurements':self.inputs},
                    'model_connection':self.services.llm_client.connection_binding(),
                    'professional_operations':'work.v4', 'source_generation_protocol':None,
                    'source_decision_protocol':None, 'context_protocol':'working.v4',
                    'delivery_review_protocol':'task.v1', 'publication_assets':'referenced.v2',
                    'allow_execution':allow_execution, 'tool_contracts':'full', 'max_turns':70},
                limits={'llm_requests':70, 'total_tokens':900000,
                    'process_invocations':48, 'process_wall_seconds':360},
                max_attempts=180, services=self.services)
            return self.app
        options = {'control_protocol': 'operations.v1', 'context_protocol': context_protocol}
        if delivery_files is not None:
            options['delivery_files'] = delivery_files
        if report:
            options.update(creation_declarations={'with_report': True,
                    'material_roles': {'data_path': ['measurements']}})
        self.app = ToolApplication.create(self.root,
            goal or ('Explain the supplied observations with an editable report, table and figure; '
            'preserve originals and disclose the limits of the evidence.'),
            model='fixture', sources={'measurements': self.inputs}, services=self.services,
            allow_execution=allow_execution, tool_contracts='full',
            max_turns=70, max_tokens=900000, max_attempts=180,
            process_limits={'process_invocations': 48, 'process_wall_seconds': 360}, **options)
        return self.app

    def _load_history(self):
        """Exercise the saved native controller through its wire-format loader."""
        self.app = load_native_history(self.root,
            'Explain the supplied observations with an editable report, table and figure; '
            'preserve originals and disclose the limits of the evidence.',
            config={'model':'fixture', 'sources':{'measurements':self.inputs},
                'allow_execution':True, 'tool_contracts':'full', 'max_turns':70,
                'model_connection':self.services.llm_client.connection_binding()},
            limits={'llm_requests':70, 'total_tokens':900000,
                'process_invocations':48, 'process_wall_seconds':360, 'source_operations':210},
            max_attempts=180, services=self.services)
        return self.app

    def test_creation_rejects_historical_controllers_before_persisting_or_resolving_models(self):
        for name, options in (
            ('old-control', {'control_protocol':None}),
            ('professional-owner', {'professional_inputs':True}),
            ('old-result-controller', {'investigate_results':False}),
            ('writer-controller', {'report_protocol':'canonical_report.v1'}),
        ):
            with self.subTest(contract=name), patch(
                    'simple_ar.app.tool_application.LLMClient.from_env') as resolve:
                target = self.directory/name
                with self.assertRaisesRegex(ValueError, 'load'):
                    ToolApplication.create(target, 'Use the ordinary action loop',
                        model='fixture', sources={}, **options)
                resolve.assert_not_called()
                self.assertFalse(target.exists())
        self._create()
        self.assertEqual(self.app.state['config']['control_protocol'],'operations.v1')
        self.assertIsNone(self.app.writing)
        self.assertEqual(self.app.state['config']['professional_operations'], 'work.v5')
        self.assertIsNone(self.app.implementation)
        self.assertIsNone(self.app.operations.document)
        self.assertIsNone(self.app.operations.methods)
        self.assertFalse({'professional_work','result_investigation','document_source_contract',
            'source_generation_protocol','source_decision_protocol'}
            & self.app.state['config'].keys())

    def _reload(self):
        self.app = ToolApplication.load(self.root, services=self.services)
        return self.app

    def test_nonreader_text_does_not_acquire_original_read_provenance(self):
        from simple_ar.core.working_context import select_working_context
        self._create()
        path = self.inputs/'conditions.txt'
        produced = ToolResult(tool_name='analyze_table', data={
            'text':'Generated interpretation, not the file bytes.',
            'absolute_path':str(path), 'sha256':_digest(path)})
        owner = self.app.operations.registry.domain_gateways()['analysis']
        with patch.object(owner, 'call', return_value=produced):
            self._submit(_action('analyze_table', path='measurements/observations.csv',
                spec={'value_columns':['value'], 'observation_unit':'one recorded observation'}))
        with patch('simple_ar.core.working_context.select_working_context', wraps=select_working_context) as select:
            self.app._messages()
        rows = select.call_args.kwargs['observations']
        self.assertFalse(any(row.get('role')=='historical_observation' for row in rows))
        self.assertTrue(any(row.get('kind')=='operation_result'
            and 'Generated interpretation' in row['text'] for row in rows))
        self.assertEqual(path.read_text(), 'Three supplied observations; no sampling design or population uncertainty is known.\n')
        self.send.assert_not_called()

    def test_shared_work_returns_actual_source_and_document_on_the_native_loop(self):
        self._create(saved_scope=True, goal='Modify a working copy of the project, validate it with the supplied command, '
            'and retain a Markdown account; preserve the original project.')
        (self.inputs / 'main.py').write_text('VALUE = 1\n', encoding='utf-8')
        (self.inputs / 'check.py').write_text(
            "from main import VALUE\nfrom pathlib import Path\n"
            "assert VALUE == 3, 'expected requested value'\nPath('result.txt').write_text(str(VALUE))\n",
            encoding='utf-8')
        originals = self._versions(self.inputs)
        command = _action('run_process', argv=['python', 'check.py'], cwd='workspace/working',
            inputs=['working/main.py', 'working/check.py'], outputs=['working/result.txt'], timeout_sec=5)
        def edit(value):
            return _action('edit_file', path='working/main.py', expected_sha256=self._last('read_file')['data']['sha256'],
                replacements=[{'old_text': 'VALUE = ' + str(value - 1), 'new_text': 'VALUE = ' + str(value)}])
        def diagnosed_repair():
            call = self.send.call_args
            request = call.kwargs.get('request') or call.args[1]
            self.assertIn('expected requested value', json.dumps(request['messages']))
            return edit(3)
        def enter_source_scope():
            call = self.send.call_args
            request = call.kwargs.get('request') or call.args[1]
            view = json.loads(request['messages'][1]['content'])
            self.assertEqual(view['professional_work']['goal'], 'Revise and validate the actual working project')
            self.assertIn('no sampling design', json.dumps(view['working_view']['observations']))
            self.assertIn('earlier execution diagnosis', json.dumps(view['working_view']['observations']))
            return _action('copy_project', source='measurements', path='working')
        self.decisions.extend([
            _action('read_file', path='measurements/conditions.txt', full_digest=True),
            _action('run_process', argv=['python', '-c',
                "import sys; print('earlier execution diagnosis', file=sys.stderr); sys.exit(1)"],
                cwd='workspace', inputs=['measurements/conditions.txt'], timeout_sec=5),
            _action('begin_professional_work', kind='source', goal='Revise and validate the actual working project',
                material_roots=['measurements'], project_path='measurements'),
            enter_source_scope,
            _action('read_file', path='working/main.py'), lambda: edit(2), command,
            _action('read_file', path='working/main.py'),
            _action('read_file', path='working/check.py'),
            _action('read_file', path='working/main.py'), diagnosed_repair, command,
            _action('finish_professional_work', candidate='working/main.py'),
            _action('begin_professional_work', kind='document', goal='Retain an editable account of the actual working files',
                material_roots=['workspace/working'], project_path='workspace/working'),
            _action('write_file', path='working/note.md', content='Working files retained.\n'),
            _action('read_file', path='working/note.md'),
            lambda: _action('edit_file', path='working/note.md',
                expected_sha256=self._last('read_file')['data']['sha256'],
                replacements=[{'old_text': 'retained.', 'new_text': 'retained with execution receipts.'}]),
            _action('finish_professional_work', candidate='working/note.md'),
            _action('deliver_result', summary='Retained actual working files and receipts.',
                artifacts=['working/main.py', 'working/check.py', 'working/result.txt', 'working/note.md']),
        ])
        with patch('simple_ar.code_task.generation.writer.write_implementation_unit',
                side_effect=AssertionError('No historical source owner in work.v4')), \
                patch('simple_ar.tools.document_work.DocumentWorkToolGateway.call',
                    side_effect=AssertionError('No historical document owner in work.v4')):
            self._run()
            self.assertIsNotNone(self.app.state['pending'])
            self.assertFalse(self._process_receipts())
            self.assertEqual(self.app._coordinator_purpose(), 'task')
            for index in range(3):
                token = self.app.approval_token(self.app.state['pending']['call'])
                self._reload()
                self.assertEqual(self.app._coordinator_purpose(), 'task' if index == 0 else 'code')
                self._run(approved_execution=token)
        self.assertEqual(self.app.session.manifest.status, 'completed', self.app.result_snapshot())
        processes = self._observations('run_process')
        self.assertEqual([row['data']['returncode'] for row in processes], [1, 1, 0])
        self.assertEqual(self._versions(self.inputs), originals)
        self.assertEqual((self.root / 'workspace/working/result.txt').read_text(), '3')
        self.assertIn('execution receipts', (self.root / 'workspace/working/note.md').read_text())
        self.assertEqual(self.app._coordinator_purpose(), 'task')
        returned = self._observations('finish_professional_work')
        self.assertEqual([row['data']['kind'] for row in returned], ['source', 'document'])
        self.assertTrue(all(row['data']['outcome'] == 'candidate' for row in returned))
        self.assertTrue(all(row['data']['artifact_sha256'] for row in returned))
        self.assertFalse(self.document_views)
        self.assertFalse(any(self._observations(name) for name in ('work_document', 'prepare_implementation')))
        self.assertFalse((self.root / 'workspace/implementation-drafts').exists())

    def test_work_scope_restores_and_unavailable_roots_can_return_partial_work(self):
        self._create(saved_scope=True)
        project = self.root / 'workspace/optional'
        project.mkdir()
        self._submit(_action('begin_professional_work', kind='source', goal='Inspect the optional working project',
            material_roots=['workspace/optional'], project_path='workspace/optional'))
        self.assertTrue(self._ok('begin_professional_work')['data']['accepted'])
        self.assertFalse(self._observations('finish_professional_work'))
        self._reload()
        self.assertEqual(self.app._coordinator_purpose(), 'code')
        project.rename(project.with_name('moved'))
        view = json.loads(self.app._messages()[1]['content'])['professional_work']
        self.assertEqual(view['status'], 'active')
        self.assertTrue(view['unavailable_locations'])
        feedback = 'New request: explain the available original observations directly.'
        self._submit(_action('read_file', path='measurements/conditions.txt'), feedback=feedback)
        self._reload()
        self.assertEqual(self.app._coordinator_purpose(), 'code')
        messages = self.app._messages()
        self.assertIn(feedback, json.dumps(messages))
        self._submit(_action('finish_professional_work', gaps=['The working project is unavailable.']))
        self.assertEqual(self._ok('finish_professional_work')['data']['outcome'], 'partial')
        self.assertEqual(self.app._coordinator_purpose(), 'task')
        self.send.assert_not_called()

    def test_source_work_from_empty_workspace_and_route_preflight(self):
        self._create(saved_scope=True, goal='Create a command-line implementation that sums supplied numbers.')
        original_text_client = self.app._text_client
        def routed(client, purpose, *, native=True):
            if purpose == 'writing' and native:
                raise ValueError('Native tools require the saved Chat protocol; no protocol fallback')
            return original_text_client(client, purpose, native=native)
        with patch.object(ToolApplication, '_text_client', side_effect=routed):
            self._submit(_action('begin_professional_work', kind='document', goal='Draft a note.'))
        self.assertEqual(self._last('begin_professional_work')['status'], 'blocked')
        self.assertIn('Chat protocol', self._last('begin_professional_work')['error'])
        self.assertEqual(self.app._coordinator_purpose(), 'task')
        self._submit(_action('begin_professional_work', kind='source', goal='Create and validate the summation implementation.'))
        self.assertTrue(self._ok('begin_professional_work')['data']['accepted'])
        self._submit(_action('finish_professional_work', candidate='fresh/main.py'))
        self.assertEqual(self._last('finish_professional_work')['status'], 'blocked')
        self.assertEqual(self.app._coordinator_purpose(), 'code')
        source = 'import sys\nprint(sum(map(int, sys.argv[1:])))\n'
        self._write('fresh/main.py', source)
        self._submit(_action('run_process', argv=['python', 'fresh/main.py', '1', '2', '3'],
            cwd='workspace', inputs=['fresh/main.py'], timeout_sec=5))
        token = self.app.approval_token(self.app.state['pending']['call'])
        self._reload()
        self._run(approved_execution=token)
        self.assertIn('6', self._ok('run_process')['data']['stdout'])
        self._submit(_action('finish_professional_work', candidate='fresh/main.py'))
        result = self._ok('finish_professional_work')['data']
        self.assertEqual(result['outcome'], 'candidate')
        self.assertEqual((self.root/'workspace/fresh/main.py').read_text(), source)
        self.assertEqual(self.app._coordinator_purpose(), 'task')
        self.send.assert_not_called()

    def test_revision_keeps_original_bytes_after_undeclared_process_side_effect(self):
        self._create(saved_scope=True, goal='Revise the existing manuscript and retain the original.')
        original = 'Original manuscript.\n'
        self._write('draft.md', original)
        with patch('simple_ar.tools.professional_work.write_atomically', side_effect=KeyboardInterrupt):
            self._submit(_action('begin_professional_work', kind='document',
                goal='Revise the manuscript.', prior_draft='draft.md'))
        self.assertEqual(self.app.session.manifest.status, 'paused')
        self.assertIn('KeyboardInterrupt', self.app.session.manifest.status_reason)
        pending = self.app.state['pending']['attempt']
        attempt = self.root/'attempts'/pending
        self.assertTrue((attempt/'professional_begin.json').is_file())
        self.assertFalse((attempt/'professional_receipt.json').exists())
        before = self.app.ledger.to_dict()
        self._reload()
        self._run()
        self.assertEqual(self.app.ledger.to_dict(), before)
        prior = self._ok('begin_professional_work')['data']['prior_draft']
        snapshot = self.app.workspace.resolve(prior['snapshot'])
        self.assertEqual(snapshot.read_text(encoding='utf-8'), original)
        command = _action('run_process', argv=['python', '-c',
            "from pathlib import Path; Path('draft.md').write_text('changed')"], cwd='workspace', timeout_sec=5)
        self._submit(command)
        token = self.app.approval_token(self.app.state['pending']['call'])
        self._reload()
        self._run(approved_execution=token)
        self.assertEqual((self.root/'workspace/draft.md').read_text(), 'changed')
        self.assertEqual(snapshot.read_text(encoding='utf-8'), original)
        self._submit(_action('finish_professional_work', gaps=['The revision needs the original restored.']))
        returned = self._ok('finish_professional_work')['data']
        self.assertEqual(returned['outcome'], 'partial')
        self.assertFalse(returned['prior_current'])
        self.assertEqual(snapshot.read_text(encoding='utf-8'), original)
        receipts = self._process_receipts()
        self._reload()
        self.assertEqual(self._process_receipts(), receipts)

    def test_professional_candidate_uses_the_producers_bound_version(self):
        for replaced in (False, True):
            with self.subTest(replaced=replaced):
                self.root = self.directory / ('replaced' if replaced else 'intact')
                self._create(saved_scope=True)
                replacement = self._write('old-note.md', 'An unrelated old note.\n')
                self._submit(_action('begin_professional_work', kind='source',
                    goal='Compute the supplied observations and return the actual analysis.'))
                self._submit(_action('analyze_table', path='measurements/observations.csv',
                    spec={'value_columns':['value'], 'observation_unit':'one supplied observation'}))
                produced = self._ok('analyze_table')['data']
                candidate = produced['report']
                if replaced:
                    self._submit(_action('copy_file', path=candidate, source='old-note.md',
                        source_sha256=replacement['data']['sha256'],
                        expected_sha256=produced['artifact_sha256'][candidate]))
                    self._ok('copy_file')
                self._submit(_action('finish_professional_work', candidate=candidate))
                result = self._last('finish_professional_work')
                self.assertEqual(result['status'], 'blocked' if replaced else 'ok', result)
                if replaced:
                    self.assertIn('actual source or document work', result['error'])
        self.send.assert_not_called()

    def test_saved_work_v3_keeps_its_original_intent_contract(self):
        from tests.native_history_fixture import load_prepared_operation_history
        self.app = load_prepared_operation_history(self.root, 'Inspect the supplied project.',
            config={'model':'fixture', 'sources':{'measurements':self.inputs},
                'professional_operations':'work.v3', 'source_generation_protocol':None,
                'source_decision_protocol':None, 'context_protocol':'working.v3',
                'tool_contracts':'full', 'max_turns':30},
            limits={'llm_requests':30, 'total_tokens':300000}, max_attempts=70, services=self.services)
        offered = {row['function']['name'] for row in self.app.schemas}
        self.assertIn('select_work_method', offered)
        self.assertNotIn('begin_professional_work', offered)
        self._submit(_action('select_work_method', method='source', goal='Inspect the source.',
            material_roots=['measurements']))
        self._reload()
        self.assertEqual(self.app._coordinator_purpose(), 'code')
        self.assertEqual(json.loads(self.app._messages()[1]['content'])['professional_method']['status'], 'current')
        self.send.assert_not_called()

    def test_default_discovery_loads_optional_tools_and_preserves_frozen_owners(self):
        self.app = ToolApplication.create(self.root, 'Inspect the supplied table and retain a note.',
            model='fixture', sources={'measurements':self.inputs}, services=self.services,
            allow_execution=True, max_turns=12, max_tokens=200000, max_attempts=30)
        self.assertEqual(self.app.state['config']['tool_contracts'], 'on_demand')
        core = {row['function']['name'] for row in self.app.schemas}
        self.assertTrue({'read_file','search_files','write_file','edit_file','run_process',
            'deliver_result','load_tools','read_history','update_work_plan','export_report'} <= core)
        self.assertFalse({'select_work_method','prepare_implementation','execute_implementation','work_document',
            'begin_professional_work','finish_professional_work'} & core)
        optional = {'inspect_table','analyze_table','measure_experiment','analyze_measurements',
            'inspect_artifacts','find_source_passages'}
        self.assertFalse(optional & core)
        view = json.loads(self.app._messages()[1]['content'])
        self.assertTrue(optional <= view['available_specialists'].keys())
        self.assertNotIn('search_sources', view['available_specialists'])
        historical, _ = self.app.operations.presentation(set(), 'outcomes')
        self.assertTrue(optional <= historical)

        selected = {'inspect_table','inspect_artifacts','export_report'}
        self._submit(_action('load_tools', names=sorted(selected)))
        self._ok('load_tools')
        self._reload()
        self.assertEqual({row['function']['name'] for row in self.app.schemas}, core | selected)
        view = json.loads(self.app._messages()[1]['content'])
        self.assertFalse(selected & view['available_specialists'].keys())
        self.assertIn('analyze_table', view['available_specialists'])

        # Discovery cannot grant network access, nor replace the last valid selection on failure.
        self._submit(_action('load_tools', names=['search_sources']))
        rejected = self._last('load_tools')
        self.assertEqual(rejected['status'], 'blocked', rejected)
        self.assertIn('unauthorized', rejected['error'])
        self._reload()
        self.assertEqual({row['function']['name'] for row in self.app.schemas}, core | selected)
        self.send.assert_not_called()
        self._submit(_action('load_tools', names=['inspect_table']))
        self._reload()
        self.assertEqual({row['function']['name'] for row in self.app.schemas}, core | {'inspect_table'})
        self.decisions.extend([
            _action('inspect_table', path='measurements/observations.csv'),
            _action('write_file', path='note.md', content='The supplied table has been inspected.\n',
                delivery={'summary':'Retained table inspection.', 'artifacts':['note.md']}),
        ])
        self._run(should_stop=lambda:False)
        self.assertEqual(self._ok('inspect_table')['data']['row_count'], 3)
        self.assertEqual(self.app.session.manifest.status, 'completed', self.app.result_snapshot())
        self.assertFalse(self._process_receipts())

        table = {'path':'measurements/observations.csv',
            'spec':{'value_columns':['value'],'observation_unit':'one supplied observation'}}
        execution = {'command':[sys.executable,'-c',"print('score: 1')"],
            'cwd':str(self.inputs), 'timeout_sec':5,
            'result_schema':{'primary_metric':'score','required_metrics':['score']}}
        (self.inputs/'main.py').write_text('VALUE = 1\n', encoding='utf-8')
        (self.inputs/'check.py').write_text('from main import VALUE\nassert VALUE == 2\n', encoding='utf-8')
        code = {'project_path':'measurements', 'goal':'Repair the supplied source',
            'allowed_patterns':['main.py'], 'validation_command':['python','check.py']}
        for name, contracts, required in (
            ('table', {'table_analysis':table}, {'inspect_table','analyze_table'}),
            ('execution', {'execution_contract':execution}, {'measure_experiment','analyze_measurements'}),
            ('code', {'code_task':code}, {'prepare_code_task','implement_code_task'}),
        ):
            with self.subTest(frozen_owner=name):
                self.root = self.directory / ('discovery-' + name)
                self.app = ToolApplication.create(self.root, 'Perform the frozen work.',
                    model='fixture', sources={'measurements':self.inputs}, services=self.services,
                    allow_execution=True, max_attempts=30, **contracts)
                self._reload()
                offered = {row['function']['name'] for row in self.app.schemas}
                view = json.loads(self.app._messages()[1]['content'])
                self.assertTrue(required <= offered)
                self.assertFalse(required & view['available_specialists'].keys())
                self.assertNotIn('inspect_artifacts', offered)
                self.assertIn('export_report', offered)
                if name == 'code':
                    self.assertFalse({'write_file','edit_file','run_process'} & offered)
                else:
                    self.assertTrue({'write_file','edit_file','run_process'} <= offered)
                # The frozen table owner remains usable without any optional loading.
                if name == 'table':
                    self._submit(_action('analyze_table', **table))
                    self._ok('analyze_table')

    def test_chinese_markdown_feedback_delivers_without_a_report_or_publication_contract(self):
        self.app = ToolApplication.create(self.root,
            '把已有观察整理成简短的中文 Markdown 说明，保留适用范围即可。',
            model='fixture', sources={'measurements':self.inputs}, services=self.services,
            allow_execution=False, max_turns=10, max_tokens=200000, tool_contracts='full')
        first = '# 结果说明\n\n这些是已提供的观察，尚不支持总体推断。\n'
        self.decisions.extend([
            _action('write_file',path='说明.md',content=first),
            _action('deliver_result',summary='已整理中文说明。',artifacts=['说明.md']),
        ])
        self._run()
        self.assertEqual(self.app.session.manifest.status,'completed',self.app.result_snapshot())
        delivery = self.app.session.store.read_json('delivery.json')
        self.assertIsNone(delivery.get('report'))
        old_delivery = (self.root/'delivery.json').read_bytes()
        ledger = self.app.ledger.to_dict()['entries']
        self._reload()
        revised = first + '\n后续需要扩大样本并确认采样方式。\n'
        self.decisions.extend([
            _action('read_file',path='说明.md'),
            _action('write_file',path='修订说明.md',content=revised),
            _action('deliver_result',summary='已补充后续建议。',artifacts=['修订说明.md']),
        ])
        self._run(feedback='保留原说明，另存一个版本，加一句后续建议。仍然用中文 Markdown。')
        self.assertEqual(self.app.session.manifest.status,'completed',self.app.result_snapshot())
        self.assertEqual((self.root/'workspace/说明.md').read_text(encoding='utf-8'),first)
        self.assertEqual((self.root/'workspace/修订说明.md').read_text(encoding='utf-8'),revised)
        self.assertNotEqual((self.root/'delivery.json').read_bytes(),old_delivery)
        self.assertEqual(self.app.ledger.to_dict()['entries'][:len(ledger)],ledger)
        self.assertFalse(self._process_receipts())
        self.assertFalse(self._observations('inspect_artifacts'))
        self.assertFalse(self._observations('export_report'))

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
                branches = terminal['parameters'].get('properties', {}).get(
                    'decisions', {}).get('items', {}).get('anyOf', [])
                if (isinstance(response, dict) and branches
                        and all('conclusion' in branch.get('required', []) for branch in branches)):
                    response = {**response, 'decisions':[
                        {'conclusion':'complete', **decision} for decision in response.get('decisions', [])]}
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
                if 'manuscript_markdown' in view.get('response_schema', {}).get('properties', {}):
                    self.assertTrue(callable(self.document_decision), 'Unexpected document work')
                    self.document_views.append(deepcopy(view))
                    return {'choices': [{'message': {'role': 'assistant',
                        'content': json.dumps(self.document_decision(view))}, 'finish_reason': 'stop'}],
                        'usage': {'prompt_tokens': 100, 'completion_tokens': 40, 'total_tokens': 140}}
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
        result = next(row for row in reversed(self._observations())
            if 'assessments' in row.get('metadata', {}) or 'assessment' in row.get('metadata', {}))
        self.assertEqual(result['status'], 'ok', result)
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
            evidence = json.dumps(self.inspection_requests[-1]['messages'])
            missing = []
            if not all(text in evidence for text in ('a,1', 'b,2', 'c,3')):
                missing.append(_action('read_file', path='measurements/observations.csv'))
            if 'no sampling design' not in evidence:
                missing.append(_action('read_file', path='measurements/conditions.txt'))
            if missing:
                return missing
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
        self.assertTrue(self.app.operations.delivery_issues(self.app._operation_observations(),text_only))
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
            _action('export_report', markdown='v2/report.md', compile_pdf=compile_pdf,
                delivery=self._delivery('v2/report.md', 'v2/table.csv', 'v2/figure.png', 'analyze.py')),
        ])
        self._run(feedback='Clarify that these three observations do not establish a population estimate. '
            'Revise only the interpretation, retain both versions and reuse the computed results.',
            approve=lambda call, value: True)
        self.assertEqual(self.app.session.manifest.status, 'completed', self.app.result_snapshot())
        self.assertFalse(self.decisions)
        self.assertFalse(self._observations('begin_professional_work'))
        self.assertFalse(self._observations('finish_professional_work'))
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
        self._create(context_protocol='working.v1')
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
        self._create(context_protocol='working.v1')
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
        self._load_history()
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

    def test_interrupted_publication_recovery_never_starts_a_delivery_review(self):
        for tool, reviewed in (('export_report', False), ('export_report', True), ('write_file', True)):
            with self.subTest(tool=tool, reviewed=reviewed):
                self.root = self.directory / (tool + ('-reviewed' if reviewed else '-unreviewed'))
                self._create()
                self._write('report.md', '# Observations\n\nThree supplied values; no population inference.\n')
                bind = self.app._bind_delivery
                def interrupt(call, payload, store, **kwargs):
                    if reviewed:
                        bind(call, payload, store, **kwargs)
                    raise KeyboardInterrupt('Crash after durable publication, before native observation')
                with patch.object(self.app, '_bind_delivery', side_effect=interrupt):
                    arguments = ({'markdown':'report.md', 'compile_pdf':False} if tool == 'export_report'
                        else {'path':'report.md', 'content':'# Observations\n\nThree supplied values; no population inference.\n',
                              'expected_sha256':_digest(self.root/'workspace/report.md')})
                    self._submit(_action(tool, **arguments, delivery=self._delivery('report.md')),
                        approve=lambda call, token: True)
                pending = deepcopy(self.app.state['pending'])
                self.assertEqual(pending['kind'], 'tool')
                self.assertEqual(self.app.gateway.can_reconcile(ToolCall(
                    tool_name=tool, trace_id=pending['attempt'])), tool == 'export_report')
                retained = self.root/'attempts'/pending['attempt']/'native_result.json'
                self.assertEqual(retained.exists(), reviewed)
                if reviewed:
                    raw = json.loads(retained.read_text())
                    self.assertEqual(raw['status'], 'ok')
                    self.assertNotIn('delivery_review', raw['metadata'])
                    self.assertEqual(raw['metadata']['delivery_hashes'],
                        self.app.workspace.artifact_hashes(raw['metadata']['delivery_hashes']))
                ledger, calls = self.app.ledger.to_dict(), self.send.call_count
                exports = self._versions(self.root/'tools/publication')
                self._reload()
                with patch.object(self.app.gateway, 'call', wraps=self.app.gateway.call) as dispatch:
                    self._run(should_stop=lambda: self.app.state['pending'] is None)
                if reviewed:
                    dispatch.assert_not_called()
                self.assertEqual(self.send.call_count, calls)
                self.assertEqual(self.app.ledger.to_dict(), ledger)
                self.assertEqual(self._versions(self.root/'tools/publication'), exports)
                result = self._ok(tool)
                if reviewed:
                    self.assertIn('delivery_review', result['metadata'])
                    self.assertEqual(self.app.session.manifest.status, 'completed')
                else:
                    self.assertNotIn('delivery_review', result['metadata'])
                    self._not_delivered()

    def test_working_model_boundary_keeps_real_observations_without_reopening_native_history(self):
        self._create(context_protocol='working.v1')
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

    def test_working_v3_retains_closed_operations_without_notes_after_native_eviction(self):
        from dataclasses import replace
        self.services.llm_client = LLMClient(replace(self.services.llm_client._settings,
            context_input_tokens=20000))
        self._create(context_protocol='working.v3', goal='Retain a checked working artifact and inspect the supplied files.')
        self.assertEqual(self.app.state['config']['context_protocol'], 'working.v3')
        command = ['python', '-c', "from pathlib import Path; "
            "Path('retained.txt').write_text('checked'); print('closed execution outcome')"]
        self.decisions.append(_action('run_process', argv=command, cwd='workspace',
            inputs=[], outputs=['retained.txt'], timeout_sec=5))
        for i in range(8):
            self.decisions.extend([
                _action('write_file', path=f'working-{i}.txt',
                    content=f'Authored working file {i}.\n' + 'Recorded payload.\n' * 1200),
                _action('read_file', path=f'working-{i}.txt', full_digest=True),
            ])
        self.decisions.append(_action('ask_user', question='Which retained file should be explained?'))
        self._run(approve=lambda call, token: True)
        self._ok('ask_user')
        self.assertEqual(self._ok('run_process')['data']['returncode'], 0)
        self.assertFalse(self.app._working_memory()['notes'])
        self.assertFalse(self.memory_views)
        self.assertFalse(self._observations('select_work_method'))
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        self._reload()
        messages = self.app._messages()
        context = json.loads(messages[1]['content'])
        self.assertGreater(context['working_context']['recent_start_turn'], 0)
        overview = context['working_view']['operation_overview']
        groups = overview['entries']
        if isinstance(groups, dict):
            table = groups
            groups = [dict(zip(table['columns'], row)) for row in table['rows']]
            groups = [{**group, 'observations': [
                dict(zip(table['observation_columns'][schema], values))
                for schema, values in group['observations']]} for group in groups]
        entries = [entry for group in groups for entry in group['observations']]
        execution = next(entry for entry in entries if entry['tool'] == 'run_process')
        self.assertEqual(execution['arguments']['argv'], command)
        self.assertEqual(execution['observed']['returncode'], 0)
        self.assertIn('closed execution outcome', json.dumps(execution))
        self.assertIn('retained.txt', json.dumps(execution))
        self.assertTrue(any(entry['tool'] == 'write_file' for entry in entries))
        self.assertFalse(any('closed execution outcome' in json.dumps(message)
                             for message in messages[2:]))
        self.assertLessEqual(context['working_context']['estimated_input_tokens'], 20000)
        self.assertEqual(self.app._working_memory()['notes'], [])
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}, before)
        self.assertFalse(self.transport_errors)

    def test_working_understanding_survives_feedback_and_invalidates_changed_source(self):
        from dataclasses import replace
        self.services.llm_client = LLMClient(replace(self.services.llm_client._settings,
            context_input_tokens=12000))
        self._create(context_protocol='working.v2')
        self.assertEqual(self.app.state['config']['context_protocol'], 'working.v2')
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
        # The same work owner maintains understanding; rotation must not buy
        # another author or turn its notes into a claim of covered history.
        for index in range(8):
            name = f'context-{index}.txt'
            (self.inputs/name).write_text('Unrelated original observation.\n'*180, encoding='utf-8')
            self.decisions.append(_action('read_file', path='measurements/'+name))
        self.decisions.append(_action('ask_user', question='Which interpretation should be emphasized?'))
        self._run()
        self.assertFalse(self.memory_views)
        self.assertFalse(list((self.root/'attempts').glob('*/memory_input.json')))
        self.assertFalse(any(entry.purpose.startswith('working-memory') for entry in self.app.ledger.entries))
        self.assertEqual(self.app._working_memory()['through_turn'], 0)
        self.assertIn('Three recorded', self.app._working_memory()['notes'][0]['text'])
        self._submit(_action('update_work_plan', steps=[{'step':'Revise only the explanation',
            'status':'pending'}]), feedback='Keep the calculation; clarify interpretation.')
        self.assertTrue(self.app._working_memory()['notes'][0]['current'])
        self.assertIn('Three recorded', self.app._working_memory()['notes'][0]['text'])
        professional = self.app._work_context(['measurements'])
        self.assertEqual(professional['working_notes'], self.app._working_memory()['notes'])
        self._submit(_action('update_work_plan', notes=[{'text':'Invented source',
            'records':['records/not-an-observation/native_result.json']}]))
        self.assertNotEqual(self._last('update_work_plan')['status'],'ok')
        self.assertIn('Three recorded', self.app._working_memory()['notes'][0]['text'])
        (self.inputs/'observations.csv').write_text('sample,value\na,7\n',encoding='utf-8')
        self.assertFalse(self.app._working_memory()['notes'][0]['current'])
        self.assertFalse(self.app._work_context(['measurements'])['working_notes'][0]['current'])
        self._submit(_action('update_work_plan', notes=[]))
        self.assertEqual(self.app._working_memory()['notes'],[])

    def test_report_delivery_owns_full_review_instead_of_promoting_a_local_consistency_pass(self):
        self._create()
        self._write('report.md', '# Observations\n\nThe sample mean is 9.0.\n')
        self._write('table.csv', 'mean\n9.0\n')
        original_inputs = self._versions(self.inputs)

        def inspect(view):
            if 'proposed_delivery' not in view:
                return {'findings': [], 'limitations': ['Local text/table agreement only.']}
            for original in self.app.state['requests']:
                self.assertIn({'role': 'user', 'content': original}, self.inspection_requests[-1]['messages'])
            self.assertIn('entire current report delivery', view['goal'])
            inventory = view['proposed_delivery']['artifacts']
            self.assertEqual(set(map(Path, inventory)), {
                self.root/'workspace/report.md', self.root/'workspace/table.csv'})
            if not any(row.get('path', '').endswith('observations.csv') for row in view['evidence']):
                return [_action('read_file', path='measurements/observations.csv')]
            # This is a controlled inspector response; the real reader and scope
            # owner must have supplied the independent original, not just table 9.
            self.assertIn('a,1', json.dumps(self.inspection_requests[-1]['messages']))
            target = view['targets'][0]
            text = ''.join(row['text'] for row in target['text_windows'])
            return {'findings': [{'subject': target['subject'], 'severity': 'required',
                'message': 'The mean 9.0 contradicts the original observations.'}]
                if 'mean is 9.0' in text else [], 'limitations': []}

        self.review = inspect
        self._submit(_action('inspect_artifacts', goal='Check local table/text agreement',
            targets=['report.md'], evidence=['table.csv']))
        self.assertEqual(self._assessment()['verdict'], 'passed')
        self._submit(_action('deliver_result', **self._delivery('report.md', 'table.csv')))
        self._not_delivered()
        first = self._last('deliver_result')
        proof = first['metadata']['delivery_review']
        self.assertEqual(proof['result']['metadata']['assessments'][0]['verdict'], 'needs_revision',
            {'error': proof.get('error'), 'inspection_error': proof['result'].get('error')})
        references = self.app._material_observations([str(self.inputs)])
        parent_record = self.app.state['turns'][-1]['tools'][0]['result'].replace('attempts/', 'records/', 1)
        self.assertTrue(any(row['record'] == parent_record for row in references))
        self.assertIn('contradicts', ' '.join(self.app._delivery_feedback()['issues']))
        context = self.app._work_context(['workspace'])
        feedback = context['professional_findings']
        joint = next(row for row in feedback if row.get('delivery_context'))
        self.assertEqual(joint['delivery_context'], proof['binding'])
        self.assertIn('contradicts', json.dumps(joint['result']['metadata']))
        self.assertTrue(joint['subjects'][0]['current_matches_review'])
        self.assertNotIn('windows', joint['result']['data']['targets'][0])
        source_feedback = self.app._work_context(['workspace/table.csv'])['professional_findings']
        routed = next(row for row in source_feedback if row.get('delivery_context'))
        self.assertEqual(routed['subjects'][0]['review_role'], 'delivery_inventory_member')
        self.assertEqual(set(routed['result']['metadata']['assessments'][0]['subjects']),
            {str(self.app.workspace.resolve('workspace/report.md'))})
        calls = self.send.call_count
        ledger = (self.root/'budget_ledger.json').read_bytes()
        self._reload()
        self.app._bind_delivery(self.app.session.store.read_json(self.app.state['turns'][-1]['reply'])
            ['tool_calls'][0], deepcopy(first), self.app.session.store.__class__(
                self.root/'attempts'/Path(self.app.state['turns'][-1]['tools'][0]['result']).parent.name))
        self.assertEqual(self.send.call_count, calls)
        self.assertEqual((self.root/'budget_ledger.json').read_bytes(), ledger)
        for path, content in [('report.md', '# Observations\n\nThe sample mean is 2.0.\n'),
                              ('table.csv', 'mean\n2.0\n')]:
            self._submit(_action('write_file', path=path, content=content,
                expected_sha256=_digest(self.root/'workspace'/path)))
            self._ok('write_file')
        before_revision_context = (self.root/'budget_ledger.json').read_bytes()
        requests_before_revision_context = self.send.call_count
        historical = self.app._work_context(['workspace'])['professional_findings']
        joint = next(row for row in historical if row.get('delivery_context'))
        self.assertFalse(joint['subjects'][0]['current_matches_review'])
        self.assertIn('contradicts', json.dumps(joint['result']['metadata']))
        self.assertEqual((self.root/'budget_ledger.json').read_bytes(), before_revision_context)
        self.assertEqual(self.send.call_count, requests_before_revision_context)
        self._submit(_action('deliver_result', **self._delivery('report.md', 'table.csv')))
        self.assertEqual(self.app.session.manifest.status, 'completed', self.app.result_snapshot())
        self.assertEqual(self._versions(self.inputs), original_inputs)
        self.assertFalse(self._process_receipts())

    def test_document_owner_uses_original_reads_across_feedback_and_recovers_without_new_calls(self):
        # Frozen work.v2 keeps its real nested document protocol through load;
        # work.v4 creation exercises native professional prose authoring separately.
        from tests.native_history_fixture import load_prepared_operation_history
        self.app = load_prepared_operation_history(self.root,
            'Explain the supplied observations in an editable Markdown note, preserving the originals.',
            config={'model': 'fixture', 'sources': {'measurements': self.inputs},
                'professional_operations': 'work.v2', 'source_generation_protocol': None,
                'source_decision_protocol': None, 'context_protocol': 'working.v2',
                'delivery_review_protocol': 'task.v1', 'tool_contracts': 'full', 'max_turns': 30},
            limits={'llm_requests': 30, 'total_tokens': 300000}, max_attempts=70, services=self.services)
        originals = self._versions(self.inputs)

        def author(view):
            observed = view['observations']
            if not observed:
                return {'action': 'investigate', 'read_windows': [
                    {'path': 'measurements/observations.csv'}, {'path': 'measurements/conditions.txt'}]}
            context = view['work_context']
            prior = view['prior_draft']
            body = '# Recorded values\n\nThe observed mean is 2.0.\n'
            if prior:
                self.assertIn('Clarify the scope', context['user_requests'][-1])
                self.assertTrue(any(row.get('delivery_context') for row in context['professional_findings']))
                body += '\nThis describes the supplied observations only; no population inference is supported.\n'
            return {'action': 'finish', 'manuscript_markdown': body,
                'selected_observation_ids': [row['id'] for row in observed]}

        self.document_decision = author
        arguments = {'goal': 'Explain the actual recorded values', 'material_roots': ['measurements']}
        self._submit(_action('work_document', **arguments))
        initial = self._ok('work_document')
        self.assertEqual(len(self.document_views), 2)
        first_record = self.app.state['turns'][-1]['tools'][-1]['result']
        references = self.app._material_observations(['measurements'])
        self.assertTrue(any(row['record'].endswith(first_record.removeprefix('attempts/')) for row in references))
        manuscript = initial['data']['manuscript']
        initial_bytes = self.app.workspace.resolve(manuscript).read_bytes()
        self._submit(_action('deliver_result', **self._delivery(manuscript)))
        self.assertEqual(self.app.session.manifest.status, 'completed', self.app.result_snapshot())
        self._reload()
        self._submit(_action('work_document', goal='Clarify the scope while preserving the result',
            prior_draft=manuscript, prior_sha256=initial['data']['sha256']),
            feedback='Clarify the scope: explain why these observations do not establish a population claim.')
        revision = self._ok('work_document')
        self.assertEqual(len(self.document_views), 3)  # Reused actual reads, no new investigation request.
        self.assertEqual(self.app.workspace.resolve(manuscript).read_bytes(), initial_bytes)
        self.assertEqual(self._versions(self.inputs), originals)
        self.assertIn('no population inference', revision['data']['markdown'])
        self._submit(_action('deliver_result', **self._delivery(revision['data']['manuscript'])))
        self.assertEqual(self.app.session.manifest.status, 'completed', self.app.result_snapshot())
        ledger, calls = (self.root/'budget_ledger.json').read_bytes(), self.send.call_count
        identity = Path(first_record).parent.name
        replay = self.app.operations.document.call('work_document', arguments, identity=identity)
        self.assertEqual(replay.model_dump(mode='json'), initial)
        self.assertEqual(self.send.call_count, calls)
        self.assertEqual((self.root/'budget_ledger.json').read_bytes(), ledger)

    def test_unavailable_review_remains_a_diagnostic_without_poisoning_professional_work(self):
        from simple_ar.integrations.llm import LLMStreamError
        self._create()
        self._write('report.md', '# Observations\n\nThe recorded mean is 2.0.\n')

        def interrupted(mode, request, **kwargs):
            if any(row['function']['name'] == 'record_inspection' for row in request.get('tools', [])):
                raise LLMStreamError('Controlled independent review stream interrupted')
            return self._provider(mode, request, **kwargs)

        self.send.side_effect = interrupted
        self._submit(_action('deliver_result', **self._delivery('report.md')))
        self._not_delivered()
        before, calls = (self.root/'budget_ledger.json').read_bytes(), self.send.call_count
        self._reload()
        context = self.app._work_context(['workspace/report.md'])
        diagnostic, = [row for row in context['professional_findings'] if row.get('delivery_context')]
        self.assertFalse(diagnostic['opinion_available'])
        self.assertEqual(diagnostic['result']['metadata'], {})
        self.assertIn('unavailable', diagnostic['result']['error'])
        self.assertEqual(self.app._work_context(['measurements'])['review_observations'], [])
        self.assertEqual(self.send.call_count, calls)
        self.assertEqual((self.root/'budget_ledger.json').read_bytes(), before)
        self.assertTrue(self.app.ledger.unknown_dimensions())

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
        self.assertEqual(self.app.state['config']['inspection_protocol'],'subjects.v3')
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
        self._submit(_action('deliver_result',summary='Checked artifact',artifacts=['report.md']))
        self.assertEqual(self.app.session.manifest.status,'completed',self.app.result_snapshot())
        self.assertEqual(self._last('inspect_artifacts'),before)

    def test_subject_inspection_binds_cross_file_support_without_passing_partial_siblings(self):
        from simple_ar.core.assessments import assessment_issues
        self._create()
        self._write('report.md','# Results\n\nSee the calculation.\n')
        self._write('calculation.py','mean = (1 + 2 + 3) / 3\n')
        self._write('support.txt','The calculation uses three supplied values.\n')
        self._write('copy.md','# Results\n\nSee the calculation.\n')
        self._write('blank.txt',' \n\t')
        (self.root/'workspace/unreadable.txt').write_bytes(b'\xffinvalid UTF-8')
        def review(view):
            terminal = next(tool['function']['parameters']
                for tool in self.inspection_requests[-1]['tools']
                if tool['function']['name']=='record_inspection')
            branches = {row['properties']['subject']['const']: row
                for row in terminal['properties']['decisions']['items']['anyOf']}
            # Eligibility is per target: another target and a copy of its bytes
            # can support a sibling, but never that target itself. Blank and
            # unreadable observations cannot support either decision.
            extra = ['evidence-6'] if len(self.inspection_views)>1 else []
            for subject, references in {
                    'target-1':['target-2','evidence-2',*extra],
                    'target-2':['target-1','evidence-2','evidence-3',*extra]}.items():
                branch = branches[subject]
                self.assertEqual(branch['properties']['evidence']['items']['enum'], references)
                self.assertIn('conclusion', branch['required'])
                self.assertIn('chunk_id', branch['properties']['evidence']['description'])
            self.assertNotIn('evidence-1', [row['subject'] for row in view['evidence']])
            if len(self.inspection_views)==1:
                self.assertEqual([row['subject'] for row in view['evidence']],
                    ['evidence-2','evidence-3','evidence-4','evidence-5'])
                return [_action('read_file',path='support.txt')]
            self.assertEqual([row['subject'] for row in view['evidence']],
                ['evidence-2','evidence-3','evidence-4','evidence-5','evidence-6'])
            return {'decisions':[
                {'subject':'target-1','evidence':['target-2','evidence-6'],'findings':[],'limitations':[]},
                {'subject':'target-2','evidence':['target-1'],'findings':[],
                 'limitations':['Only the supplied prefix was reviewed.']}]}
        self.review = review
        result = self._inspect([{'path':'report.md','max_bytes':12000},
            {'path':'calculation.py','max_bytes':8}],
            [{'path':'./report.md','max_bytes':12000},'measurements/conditions.txt',
                'copy.md','blank.txt','unreadable.txt'])
        self.assertEqual([a['verdict'] for a in _assessments(result)],['passed','incomplete'])
        self.assertEqual([row['subject'] for row in result['data']['evidence']],
            ['evidence-2','evidence-3','evidence-4','evidence-5','evidence-6'])
        self.assertEqual(_assessments(result)[1]['evidence_sha256'],
            {str(self.root/'workspace/report.md'):_digest(self.root/'workspace/report.md')})
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
        added = self.app.ledger.entries[len(ledger['entries']):]
        self.assertTrue(added)  # The new bundle receives its actual whole-delivery review.
        self.assertTrue(all(row.purpose.startswith('inspect-artifacts-') for row in added))
        reviewed, reviewed_calls = self.app.ledger.to_dict(), self.send.call_count
        self._reload()
        self._run()
        self.assertEqual(self.app.ledger.to_dict(),reviewed)
        self.assertEqual(self.send.call_count,reviewed_calls)
        self.assertEqual([entry.to_dict() for entry in self.app.ledger.entries][:len(old_usage)],old_usage)
        for path,raw in {**old_bytes,**old_records}.items():
            self.assertEqual(path.read_bytes(),raw)
        self.assertEqual(self._versions(self.inputs),original_inputs)

    def test_inspection_freezes_original_requests_separately_from_narrow_goal_and_cached_replay(self):
        self._create()
        self._write('note.md', 'Only this selected text is reviewed.\n'*12)
        feedback = '  保留原文，只检查所选文字。\n其他工作稍后处理。  '
        goal = 'Check only the selected text for internal consistency'
        expected = [*self.app.state['requests'], feedback]

        def review(view):
            self.assertEqual(view['goal'], goal)
            self.assertIn('narrow check does not certify',view['user_request_scope'])
            messages = self.inspection_requests[-1]['messages']
            supplied = [message['content'] for message in messages if message['role']=='user']
            for text in expected:
                self.assertEqual(supplied.count(text),1)
            self.assertNotIn(goal,supplied)  # Coordinator scope is labelled in the view.
            self.assertEqual(len(view['targets']),1)
            self.assertTrue(view['targets'][0]['complete'])
            return {'decisions':[{'subject':'target-1','conclusion':'complete',
                'findings':[],'evidence':[],'limitations':[]}]}

        self.review = review
        self._submit(_action('inspect_artifacts',goal=goal,targets=['note.md'],evidence=[]),feedback=feedback)
        result = self._ok('inspect_artifacts')
        identity = Path(result['data']['record']).parts[1]
        saved = self.app.session.store.read_json(f'tools/inspection/{identity}/input.json')
        self.assertEqual(saved['user_requests'],expected)
        self.assertEqual(saved['arguments']['goal'],goal)
        self.assertEqual(_assessments(result)[0]['verdict'],'passed')
        records = self._versions(self.root/f'tools/inspection/{identity}')
        self._submit(_action('ask_user',question='Retain work for the next selected check'),
            feedback='A later user request must not rewrite the earlier inspection input.')
        calls, ledger = self.send.call_count,self.app.ledger.to_dict()
        self._reload()
        with patch.object(self.app.operations.inspection,'user_requests',
                side_effect=AssertionError('Cached inspection must not refreeze current requests')):
            cached = self.app.operations.inspection.call('inspect_artifacts',saved['arguments'],identity)
        self.assertEqual(cached.status,'ok',cached.error)
        self.assertEqual(cached.metadata['assessments'],_assessments(result))
        self.assertEqual(self.send.call_count,calls)
        self.assertEqual(self.app.ledger.to_dict(),ledger)
        self.assertEqual(self._versions(self.root/f'tools/inspection/{identity}'),records)

    def test_progressive_inspection_primes_small_targets_for_one_final_response(self):
        self._create()
        texts = {
            'records.csv': 'record,category\n' + ''.join(f'{index},group-{index % 3}\n' for index in range(90)),
            'review-notes.md': '# Recorded scope\n\n' + 'Only supplied records are described.\n'*35,
        }
        for path, text in texts.items():
            self._write(path, text)

        def review(view):
            self.assertEqual(view['evidence'], [])
            self.assertEqual(len(view['targets']), len(texts))
            for target in view['targets']:
                text = texts[Path(target['path']).name]
                self.assertTrue(target['complete'])
                self.assertEqual(''.join(w['text'] for w in target['text_windows']), text)
            # Complete reading does not choose the reviewer's conclusion.
            return {'decisions':[{'subject':target['subject'], 'conclusion':'incomplete',
                'findings':[], 'evidence':[], 'limitations':['Further interpretation is needed.']}
                for target in view['targets']]}

        self.review = review
        result = self._inspect(list(texts), [])
        self.assertFalse(self.transport_errors, self.transport_errors)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(len(self.inspection_requests), 1)
        self.assertEqual([call['function']['name'] for call in
            self.inspection_messages[0]['tool_calls']], ['record_inspection'])
        assessments = _assessments(result)
        self.assertEqual(len(assessments), len(texts))
        self.assertTrue(all(row['coverage']=='partial' and row['verdict']=='incomplete'
            and not row['evidence_sha256'] for row in assessments))

    def test_progressive_inspection_large_target_under_tight_capacity_still_allows_paging(self):
        from dataclasses import replace

        self.services.llm_client = LLMClient(replace(self.services.llm_client._settings,
            context_input_tokens=9000))
        self._create()
        text = 'record,description\n' + ''.join(
            f'{index},Authorized record with limited interpretation.\n' for index in range(3500))
        self._write('large-records.csv', text)

        def review(view):
            target = view['targets'][0]
            self.assertFalse(target['complete'])
            if len(self.inspection_requests) == 1:
                self.assertIn('read_file', {tool['function']['name']
                    for tool in self.inspection_requests[-1]['tools']})
                cursor = max(end for _, end in target['observed_ranges'])
                return [_action('read_file', path=target['path'], offset_bytes=cursor, max_bytes=1024)]
            return {'findings':[], 'limitations':['Only the supplied pages were reviewed.']}

        self.review = review
        result = self._inspect(['large-records.csv'], [])
        self.assertFalse(self.transport_errors, self.transport_errors)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(len(self.inspection_requests), 2)
        assessment = _assessments(result)[0]
        self.assertEqual(assessment['coverage'], 'partial')
        self.assertEqual(assessment['verdict'], 'incomplete')
        identity = Path(result['data']['record']).parts[1]
        for index in range(2):
            trace = self.app.session.store.read_json(f'tools/inspection/{identity}/sources-{index}.json')
            self.assertLessEqual(trace['working_input']['projection']['estimated_input_tokens'], 9000)
            if index == 0:
                self.assertEqual(len(trace['results']), 1)
                self.assertEqual(trace['results'][0]['result']['status'], 'ok')
                self.assertTrue(trace['results'][0]['result']['data']['text_windows'])

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
            visible = view['targets'][0]
            if visible['complete']:
                self.assertEqual(''.join(w['text'] for w in visible['text_windows']), text)
                self.assertEqual(view['evidence'], [])
            # Explicit caller scope stays bounded initially; this reviewer then
            # deliberately requests the rest through the normal read tool.
            with patch.object(self, 'inspection_partial_targets', set()):
                response = self._review_complete_targets(view)
            if isinstance(response, list):
                for action in response:
                    action['arguments']['max_bytes'] = min(8192, action['arguments']['max_bytes'])
            return response

        self.review = review
        result = self._inspect([{'path':'progressive.md', 'max_bytes':512}], [])
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
        self.assertEqual(''.join(w['text'] for w in final['targets'][0]['text_windows']), text)

        identity = Path(result['data']['record']).parts[1]
        store = self.app.session.store
        saved = store.read_json(f'tools/inspection/{identity}/input.json')
        rows = [*saved['targets'], *saved['evidence']]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['complete'])
        preview = deepcopy(rows[0])
        preview['windows'] = preview['windows'][:1]
        preview['complete'] = False
        self.assertTrue(preview['windows'][0]['text'].endswith('\ufffd'))
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
        self.assertEqual(sum(len(trace['results']) for trace in traces), len(windows)-1)
        self.assertTrue(all(result['result']['data']['subject']=='target-1'
            for trace in traces for result in trace['results']))
        projected_reads = [json.loads(message['content']) for trace in traces
            for message in trace['working_input']['messages'] if message['role'] == 'tool']
        self.assertTrue(projected_reads)
        for result in projected_reads:
            self.assertNotIn('text_windows', result['data'])
            self.assertEqual(result['data']['text_window_owner'], 'target-1')
        self.assertTrue(all(result['result']['data']['text_windows']
            for trace in traces for result in trace['results']))
        self.assertEqual(final['evidence'], [])
        owner = self.app.operations.inspection
        self.assertTrue(owner._complete_target(rows[0], rows))
        # A real tail starting after the split character leaves a decoded-byte gap.
        tail = owner._observe(FileWindow(path='progressive.md', offset_bytes=514, max_bytes=64000),
            'gap-tail', 'progressive-gap', [512000])
        self.assertTrue(tail['readable'], tail)
        self.assertFalse(owner._complete_target(preview, [preview, tail]))
        self._write('same-bytes.txt', text)
        copy = owner._observe(FileWindow(path='same-bytes.txt', max_bytes=64000),
            'copy', 'progressive-copy', [512000])
        self.assertEqual(copy['sha256'], preview['sha256'])
        self.assertIsNone(owner._target_observation({'targets':[preview]}, copy))
        self.assertFalse(owner._complete_target(preview, [preview, copy]))
        for filename, raw in (('bad-text.txt', b'valid prefix\xff'),
                              ('control-text.txt', b'valid prefix\x00'),
                              ('disguised.bin', b'plain text')):
            with self.subTest(filename=filename):
                self._write(filename, 'placeholder')
                (self.root/'workspace'/filename).write_bytes(raw)
                invalid = owner._observe(filename, 'invalid', 'progressive-invalid', [512000])
                self.assertFalse(invalid['readable'])
                self.assertFalse(owner._complete_target(invalid, [invalid]))
                self.assertIsNone(owner._target_observation({'targets':[invalid]}, invalid))
        unread = owner._observe('absent.txt', 'unread', 'progressive-unread', [512000])
        self.assertFalse(owner._complete_target(unread, [unread]))
        # Read the otherwise complementary tail from a changed file: it must not
        # complete the old preview or inherit that version's passed assessment.
        target.write_bytes(b'B'+original[1:])
        changed = owner._observe(FileWindow(path='progressive.md', offset_bytes=511, max_bytes=64000),
            'changed-tail', 'progressive-version', [512000])
        self.assertTrue(changed['readable'], changed)
        self.assertNotEqual(changed['sha256'], rows[0]['sha256'])
        self.assertFalse(owner._complete_target(preview, [preview, changed]))
        self.assertIsNone(owner._target_observation({'targets':[preview]}, changed))
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

    def test_progressive_historical_material_roles_and_conclusions_survive_cached_load(self):
        self._create()
        text = 'Earlier retained observation.\n'*40
        self._write('retained.txt', text)
        owner = self.app.operations.inspection
        # Freeze the earlier allocation of supplementary target reads to evidence.
        # Loading it must verify the original records, not migrate material roles.
        with patch.object(owner, 'user_requests', None), patch.object(owner, '_prime_targets', return_value=None), patch.object(
                owner, '_target_observation', return_value=None):
            result = self._inspect(['retained.txt'], ['retained.txt'])
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(_assessments(result)[0]['verdict'], 'passed')
        identity = Path(result['data']['record']).parts[1]
        saved = self.app.session.store.read_json(f'tools/inspection/{identity}/input.json')
        self.assertNotIn('user_requests',saved)
        self.assertFalse(saved['targets'][0]['complete'])
        self.assertGreater(len(saved['evidence']), 1)
        expected = self.app.session.store.read_json(f'tools/inspection/{identity}/result.json')
        records = self._versions(self.root/f'tools/inspection/{identity}')
        calls, ledger = self.send.call_count, self.app.ledger.to_dict()
        self._reload()
        cached = self.app.operations.inspection.call('inspect_artifacts', saved['arguments'], identity)
        self.assertEqual(cached.model_dump(mode='json'), expected)
        self.assertEqual(cached.metadata['assessments'], _assessments(result))
        self.assertEqual(self.send.call_count, calls)
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        self.assertEqual(self._versions(self.root/f'tools/inspection/{identity}'), records)

    def test_inspection_reuses_retired_evidence_in_the_common_working_view(self):
        from dataclasses import replace

        self.services.llm_client = LLMClient(replace(self.services.llm_client._settings,
            context_input_tokens=9000))
        self._create()
        self._write('notes.md', 'The observed sample mean is 2.0.\n')
        self._write('basis.txt', 'Observed sample values are 1, 2, 3; their mean is 2.0.\n')
        for index in range(2):
            self._write(f'context-{index}.txt', f'Other authorized context {index}.\n'+'Unrelated. '*1600)

        def review(view):
            number = len(self.inspection_views)
            terminal = next(tool['function']['parameters']
                for tool in self.inspection_requests[-1]['tools']
                if tool['function']['name']=='record_inspection')
            evidence = terminal['properties']['decisions']['items']['anyOf'][0]['properties']['evidence']
            if number == 1:
                self.assertEqual(evidence['maxItems'], 0)
                self.assertNotIn('enum', evidence['items'])
            else:
                self.assertEqual(evidence['items']['enum'],
                    [f'evidence-{index}' for index in range(1,number)])
            if number == 1:
                return [_action('read_file', path='basis.txt', max_bytes=1024)]
            if number < 4:
                return [_action('read_file', path=f'context-{number-2}.txt', max_bytes=16000)]
            self.assertGreater(view['working_view']['history']['archive_end_turn'], 0)
            supplied = [row['observation'] for row in view['working_view']['observations']]
            basis, = [row for row in supplied if row['subject']=='evidence-1']
            self.assertEqual(basis['text'], 'Observed sample values are 1, 2, 3; their mean is 2.0.\n')
            self.assertEqual(basis['sha256'], _digest(self.root/'workspace/basis.txt'))
            self.assertEqual(basis['next_offset_bytes'],len(basis['text'].encode()))
            return {'decisions':[{'subject':'target-1','evidence':['evidence-1'],
                'findings':[],'limitations':[]}]}

        self.review = review
        result = self._inspect([{'path':'notes.md','max_bytes':1024}], [])
        self.assertEqual(result['status'], 'ok', result.get('error'))
        self.assertEqual(self._assessment()['verdict'], 'passed')
        self.assertEqual(self._assessment()['evidence_sha256'],
            {str(self.root/'workspace/basis.txt'):_digest(self.root/'workspace/basis.txt')})
        for request in self.inspection_requests:
            self.assertLessEqual(request['messages'][1]['content'].count('working_view'),1)
        identity = Path(result['data']['record']).parts[1]
        retained = self._versions(self.root/f'tools/inspection/{identity}')
        calls, ledger = self.send.call_count, self.app.ledger.to_dict()
        self._reload()
        cached = self.app.operations.inspection.call('inspect_artifacts',
            {'goal':'Check the claims against supplied observations',
             'targets':[{'path':'notes.md','max_bytes':1024}], 'evidence':[]}, identity)
        self.assertEqual(cached.status,'ok',cached.error)
        self.assertEqual(self.send.call_count,calls)
        self.assertEqual(self.app.ledger.to_dict(),ledger)
        self.assertEqual(self._versions(self.root/f'tools/inspection/{identity}'),retained)

    def test_inspection_raw_sources_continue_across_failed_opinions_and_later_work(self):
        from simple_ar.tools.materials import validate_material_observation

        self._create()
        self._write('draft.md', 'Explain the original measured conditions.\n')
        self._write('revision.md', 'Explain the revised result against original conditions.\n')
        basis = 'Original measured value and its experimental conditions.\n' * 36
        lines = 'Header\nOther header\nFirst measured condition\nSecond measured condition\nTail\n'
        self._write('basis.txt', basis)
        self._write('lines.txt', lines)
        initial = len(self.inspection_requests)

        def unfinished(view):
            if len(self.inspection_requests) == initial + 1:
                return [_action('read_file', path='basis.txt', max_bytes=8000),
                        _action('read_file', path='lines.txt', start_line=3, max_lines=2)]
            return [_action('record_inspection', decisions=[{
                'subject': 'target-1', 'findings': [], 'evidence': ['not-an-observed-ID'],
                'limitations': [], 'conclusion': 'complete'}])]

        self.review = unfinished
        failed = self._inspect(['draft.md'], ['basis.txt'])
        self.assertEqual(failed['status'], 'error', failed)
        self.assertEqual(len(self.inspection_requests)-initial, 3)
        failed_identity = Path(failed['data']['record']).parts[1]
        failed_records = self._versions(self.root/f'tools/inspection/{failed_identity}')
        old_receipts = {self.root/item['result']: (self.root/item['result']).read_bytes()
            for turn in self.app.state['turns'] for item in turn['tools']}
        ledger = self.app.ledger.to_dict()
        references = self.app._material_observations(['basis.txt', 'lines.txt'])
        observed = [validate_material_observation(self.app.workspace, row, identity='reuse-check')
                    for row in references]
        self.assertTrue(any(row['data']['text'] == basis for row in observed))
        self.assertTrue(any(row['request'].get('start_line') == 3 and
            row['data']['text'] == 'First measured condition\nSecond measured condition\n' for row in observed))
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        self.assertTrue(all(row['record'].endswith('native_result.json') for row in references))

        before = len(self.inspection_requests)
        def revised(view):
            messages = self.inspection_requests[-1]['messages']
            supplied = json.dumps(messages, ensure_ascii=False)
            self.assertNotIn('not-an-observed-ID', supplied)
            self.assertEqual(view['targets'][0]['text_windows'][0]['text'],
                             'Explain the revised result against original conditions.\n')
            self.assertEqual([row['observation']['text']
                for row in view['working_view']['observations']].count(basis), 1)
            sources = [row['observation'] for row in view['working_view']['observations']]
            line_text = 'First measured condition\nSecond measured condition\n'
            self.assertEqual([row['text'] for row in sources].count(line_text), 1)
            return {'decisions': [{'subject': 'target-1', 'findings': [], 'limitations': [],
                'evidence': list(dict.fromkeys(row['subject'] for row in sources
                    if row['text'] in (basis, line_text)))}]}
        self.review = revised
        self._reload()
        result = self._inspect(['revision.md'], [])
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(len(self.inspection_requests)-before, 1,
                         'Previously observed supporting bytes need no new model read requests')
        self.assertEqual((self.root/'workspace/basis.txt').read_text(), basis)
        identity = Path(result['data']['record']).parts[1]
        saved = self.app.session.store.read_json(f'tools/inspection/{identity}/input.json')
        self.assertFalse(any(w.get('source_reference') for row in saved['targets'] for w in row['windows']))
        self.assertTrue(any(w.get('source_reference') for row in saved['evidence'] for w in row['windows']))
        self.assertEqual(saved['arguments']['evidence'], [])
        self.assertEqual(_assessments(result)[0]['evidence_sha256'], {
            str(self.root/'workspace'/name): _digest(self.root/'workspace'/name)
            for name in ('basis.txt', 'lines.txt')})
        # Newly available reads belong to a new inspection, not to an already
        # started identity. Reopening this successful empty-evidence call must
        # return its frozen result without reseeding or spending again.
        completed_records = self._versions(self.root/f'tools/inspection/{identity}')
        self._read('measurements/conditions.txt')
        calls, current_ledger = self.send.call_count, self.app.ledger.to_dict()
        completed = self.app.operations.inspection.call('inspect_artifacts', saved['arguments'], identity)
        self.assertEqual(completed.model_dump(mode='json'), result)
        self.assertEqual(self._versions(self.root/f'tools/inspection/{identity}'), completed_records)
        cached = self.app.operations.inspection.call('inspect_artifacts',
            {'goal': 'Check the claims against supplied observations',
             'targets': ['draft.md'], 'evidence': ['basis.txt']}, failed_identity)
        self.assertFalse(cached.ok, cached)
        self.assertEqual(self.send.call_count, calls)
        self.assertEqual(self.app.ledger.to_dict(), current_ledger)
        self.assertEqual(current_ledger['entries'][:len(ledger['entries'])], ledger['entries'])
        self.assertEqual(self._versions(self.root/f'tools/inspection/{failed_identity}'), failed_records)
        for path, raw in old_receipts.items():
            self.assertEqual(path.read_bytes(), raw)

        # Changed sources retain their old receipts, but not their old bodies in
        # the next professional view. No verdict or stale content is inherited.
        self._submit(_action('write_file', path='basis.txt',
            content='New measured condition.\n' * 70,
            expected_sha256=_digest(self.root/'workspace/basis.txt')))
        self._ok('write_file')
        def changed(view):
            self.assertNotIn(basis, [row['observation']['text']
                for row in view['working_view']['observations']])
            return {'findings': [], 'limitations': []}
        self.review = changed
        self._inspect(['revision.md'], [])

    def test_delivery_review_with_empty_evidence_reuses_journal_sources_and_reads_current_target(self):
        self._create()
        old_body = 'The sample mean is 2.0 across three supplied observations.\n'
        current_body = old_body + 'These observations do not support population inference.\n'
        self._write('report.md', old_body)
        source = self._read('measurements/observations.csv')
        original_receipts = {self.root/item['result']: (self.root/item['result']).read_bytes()
            for turn in self.app.state['turns'] for item in turn['tools']}
        self._inspect(['report.md'], [])
        prior = self._ok('inspect_artifacts')
        prior_identity = Path(prior['data']['record']).parts[1]
        prior_records = self._versions(self.root/f'tools/inspection/{prior_identity}')
        self._submit(_action('write_file', path='report.md', content=current_body,
            expected_sha256=_digest(self.root/'workspace/report.md')))
        self._ok('write_file')
        # An unread attachment is in the delivery inventory, but is not thereby
        # a source observation or a separately passed inspection target.
        self._write('method.txt', 'Unreviewed method attachment.\n')
        self._reload()
        before = len(self.inspection_requests)
        old_entries = [entry.to_dict() for entry in self.app.ledger.entries]

        def review(view):
            self.assertEqual(len(view['targets']), 1)
            target, = view['targets']
            self.assertTrue(target['complete'])
            self.assertEqual(''.join(w['text'] for w in target['text_windows']), current_body)
            sources = [row['observation'] for row in view['working_view']['observations']]
            observed, = [row for row in sources if row['path'] == source['absolute_path']]
            self.assertEqual(observed['text'], source['text'])
            self.assertEqual(observed['sha256'], source['sha256'])
            self.assertNotIn(target['path'], [row['path'] for row in sources])
            self.assertNotIn('Unreviewed method attachment.\n', [row['text'] for row in sources])
            return {'decisions': [{'subject': target['subject'], 'evidence': [observed['subject']],
                'findings': [], 'limitations': ['The method attachment was not independently checked.']}]}

        self.review = review
        self.decisions.append(_action('deliver_result', **self._delivery('report.md', 'method.txt')))
        self._run(should_stop=lambda: False)
        self.assertEqual(self.app.session.manifest.status, 'completed', self.app.result_snapshot())
        reviewed = self._ok('deliver_result')['metadata']['delivery_review']['result']
        self.assertEqual(len(self.inspection_requests)-before, 1,
            'Ordinary whole-delivery review should receive already-read support on its first request')
        self.assertEqual(_assessments(reviewed)[0]['subjects'],
            {str(self.root/'workspace/report.md'): _digest(self.root/'workspace/report.md')})
        self.assertEqual(_assessments(reviewed)[0]['evidence_sha256'],
            {source['absolute_path']: source['sha256']})
        self.assertEqual(len(_assessments(reviewed)), 1)
        identity = Path(reviewed['data']['record']).parts[1]
        saved = self.app.session.store.read_json(f'tools/inspection/{identity}/input.json')
        self.assertEqual(saved['arguments']['evidence'], [])
        self.assertEqual(saved['arguments']['targets'], [str(self.root/'workspace/report.md')])
        self.assertFalse(any(w.get('source_reference') for row in saved['targets'] for w in row['windows']))
        self.assertTrue(any(w.get('source_reference') for row in saved['evidence'] for w in row['windows']))
        self.assertEqual(self._versions(self.root/f'tools/inspection/{prior_identity}'), prior_records)
        self.assertEqual([entry.to_dict() for entry in self.app.ledger.entries][:len(old_entries)], old_entries)
        for path, raw in original_receipts.items():
            self.assertEqual(path.read_bytes(), raw)

    def test_empty_evidence_reuse_respects_current_access_versions_and_whole_window_capacity(self):
        from dataclasses import replace

        # Isolate seed's 96000-character whole-window allowance from the
        # separate model-view token budget. This fixture has no paid transport.
        self.services.llm_client = LLMClient(replace(self.services.llm_client._settings,
            context_input_tokens=500000))
        self._create()
        self._write('report.md', 'Check only available supporting observations.\n')
        stale = 'Historical source bytes must not reappear.\n'
        self._write('stale.txt', stale)
        self._read('stale.txt')
        revoked = self._read('measurements/conditions.txt')['text']
        self._submit(_action('write_file', path='stale.txt', content='Current unread replacement.\n',
            expected_sha256=_digest(self.root/'workspace/stale.txt')))
        self._ok('write_file')
        # Two original windows exceed the common optional 96000-character
        # allowance together. Retain whole receipts; never invent a clipped read.
        bodies = ['First retained source. '+('a '*31000), 'Second retained source. '+('b '*31000)]
        for index, body in enumerate(bodies):
            self._write(f'large-{index}.txt', body)
            self._submit(_action('read_file', path=f'large-{index}.txt', max_bytes=64000, full_digest=True))
            self.assertEqual(self._ok('read_file')['data']['text'], body)
        private = self.root/'workspace/.env'
        outside = self.directory/'outside.txt'
        private.write_text('Private bytes are not supporting observations.\n', encoding='utf-8')
        outside.write_text('Outside bytes are not supporting observations.\n', encoding='utf-8')
        for path in (str(private), str(outside)):
            self._submit(_action('read_file', path=path, full_digest=True))
            self.assertNotEqual(self._last('read_file')['status'], 'ok')
        retained = {self.root/item['result']: (self.root/item['result']).read_bytes()
            for turn in self.app.state['turns'] for item in turn['tools']}
        entries = [entry.to_dict() for entry in self.app.ledger.entries]

        def review(view):
            sources = [row['observation'] for row in view['working_view']['observations']]
            texts = [row['text'] for row in sources]
            for forbidden in (stale, revoked, private.read_text(), outside.read_text(),
                              'Current unread replacement.\n'):
                self.assertNotIn(forbidden, texts)
            admitted = [text for text in texts if text in bodies]
            self.assertEqual(len(admitted), 1, 'Only one whole original window fits optional capacity')
            self.assertLessEqual(sum(len(text) for text in texts), 96000)
            self.assertTrue(all(row['text'] in bodies for row in sources))
            return {'decisions': [{'subject': 'target-1', 'conclusion': 'incomplete',
                'evidence': list(dict.fromkeys(row['subject'] for row in sources)), 'findings': [],
                'limitations': ['Revoked, stale and omitted sources cannot complete this investigation.']}]}

        self.review = review
        # Remove an existing source grant without editing its original receipts.
        # Revoke the external grant, not the internal journal needed to verify
        # every retained source. Removing records too correctly admits nothing.
        with patch.dict(self.app.workspace.mounts, {name: root for name, root
                in self.app.workspace.mounts.items() if name != 'measurements'}, clear=True):
            result = self._inspect(['report.md'], [])
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(_assessments(result)[0]['verdict'], 'incomplete')
        identity = Path(result['data']['record']).parts[1]
        saved = self.app.session.store.read_json(f'tools/inspection/{identity}/input.json')
        seeded = [window['text'] for row in saved['evidence'] for window in row['windows']
            if window.get('source_reference')]
        self.assertEqual(len(seeded), 1, 'Seed itself admits only one whole original window')
        self.assertIn(seeded[0], bodies)
        self.assertEqual([entry.to_dict() for entry in self.app.ledger.entries][:len(entries)], entries)
        for path, raw in retained.items():
            self.assertEqual(path.read_bytes(), raw)

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
        self._write('reports/report.md', _BODY.replace('(figure.png)', '(../live/figure.png)'))
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
        arguments = {'markdown': 'reports/report.md', 'compile_pdf': False}
        approved = self.app.operations.publication.approval_binding(arguments, identity='proof')
        self.assertEqual(approved['image_binding_version'], 2)
        self._write('reports/unrelated.png', 'Unrelated workspace bytes')
        self.assertEqual(self.app.operations.publication.approval_binding(arguments, identity='proof'), approved)
        exported = self._export('reports/report.md')
        self.assertEqual(exported['status'], 'ok', exported)
        manifest = exported['data']['manifest']
        self.assertEqual(set(manifest['consumed_inputs']), {
            'report.md', 'references.bib', 'figures/figures_manifest.json', '../live/figure.png'})
        self.assertFalse((self.root/'workspace/reports/figure.png').exists())
        project = exported['data']['project']
        self._inspect(['reports/report.md'])
        delivery = self._delivery('reports/report.md', project)
        delivery['report']['formats'] = ['tex']
        self._submit(_action('deliver_result', **delivery))
        self._ok('deliver_result')
        self.assertEqual(self.app.session.manifest.status, 'completed')
        self.assertEqual([row.to_dict() for row in self.app.ledger.entries][:len(ledger_before)], ledger_before)
        identity = Path(project).name
        self._reload()
        self.assertEqual(self.app.state['config']['publication_assets'], 'referenced.v2')
        ledger = self.app.ledger.to_dict()
        (self.root/'workspace/live/unrelated.svg').write_text('<svg/>')
        with patch('simple_ar.tools.publication.export_acm_report') as renderer:
            cached = self.app.operations.publication.call('export_report', arguments, identity=identity)
            renderer.assert_not_called()
        self.assertEqual(cached.status, 'ok', cached.error)
        self.assertEqual(cached.data, exported['data'])
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        script = self.root/'workspace/analyze.py'
        script_bytes = script.read_bytes()
        script.write_bytes(script_bytes+b'\n# Changed producer input\n')
        stale = self.app.operations.publication.call('export_report', arguments, identity=identity)
        self.assertEqual(stale.status, 'error')
        self.assertIn('producer inputs/outputs changed', stale.error)
        script.write_bytes(script_bytes)
        # Actual consumed bytes still invalidate the receipt; stale unrelated
        # producers are not an excuse to weaken source/figure provenance.
        (self.root/'workspace/live/figure.png').write_bytes(b'changed consumed image')
        rejected = self.app.operations.publication.call('export_report', arguments, identity=identity)
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
        self.decisions.append(_action('deliver_result', **self._delivery('report.md')))
        self._run(should_stop=lambda: False)
        self.assertEqual(self.app.session.manifest.status, 'completed', self.app.result_snapshot())
        self.assertEqual(self.send.call_count, 6)
        self.assertEqual(self._assessment()['verdict'], 'passed')
        self.assertEqual(self.app.ledger.remaining('llm_requests'), 1)
        self.assertFalse(self.app.ledger.over_limit())
        self.assertEqual([entry.to_dict() for entry in self.app.ledger.entries][:len(old_entries)], old_entries)
        self.assertEqual(self._versions(self.inputs), originals)
        calls, ledger = self.send.call_count, self.app.ledger.to_dict()
        self._reload()
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        self.assertEqual(self.send.call_count, calls)

    def test_terminal_delivery_review_preserves_one_native_partial_delivery_decision(self):
        self.app = ToolApplication.create(self.root, 'Explain the supplied observations.',
            model='fixture', sources={'measurements':self.inputs}, services=self.services,
            tool_contracts='full', max_turns=4, max_tokens=500000, max_attempts=20)
        self._write('report.md', 'The sample mean is 2.0 across three supplied observations.\n')
        limits = dict(self.app.ledger.limits)

        def review(view):
            offered = {row['function']['name'] for row in self.inspection_requests[-1]['tools']}
            if offered != {'record_inspection'}:
                self.assertEqual(len(self.inspection_requests), 1)
                return [_action('read_file', path='measurements/conditions.txt')]
            self.assertEqual(len(self.inspection_requests), 2)
            self.assertEqual(self.app.ledger.remaining('llm_requests'), 0)
            return {'decisions':[{'subject':target['subject'], 'conclusion':'incomplete',
                'findings':[], 'evidence':[],
                'limitations':['The necessary interpretation remains unfinished.']}
                for target in view['targets']]}

        self.review = review
        self.decisions.append(_action('deliver_result', **self._delivery('report.md')))
        self._run(should_stop=lambda:len(self.inspection_requests) == 2
            and self.app.state['pending'] is None)
        self._not_delivered()
        reviewed = self._ok('deliver_result')['metadata']['delivery_review']['result']
        self.assertEqual(_assessments(reviewed)[0]['verdict'], 'incomplete')
        self.assertEqual(self.send.call_count, 3)
        self.assertEqual(self.app.ledger.remaining('llm_requests'), 1)
        charged = [entry.to_dict() for entry in self.app.ledger.entries]
        ledger = self.app.ledger.to_dict()
        self._reload()
        self.assertEqual(self.app.ledger.to_dict(), ledger)

        partial = self._delivery('report.md')
        partial['report']['status'] = 'candidate'
        partial['unresolved'] = ['The independent review and necessary interpretation remain unfinished.']
        self.decisions.append(_action('deliver_result', **partial))
        self._run(should_stop=lambda:False)
        self.assertEqual(self.app.session.manifest.status, 'partial', self.app.result_snapshot())
        self.assertEqual(self.app.session.store.read_json('delivery.json')['unresolved'], partial['unresolved'])
        self.assertEqual(self.send.call_count, 4)
        self.assertEqual(len(self.inspection_requests), 2)
        self.assertEqual(self.app.ledger.limits, limits)
        entries = [entry.to_dict() for entry in self.app.ledger.entries]
        self.assertEqual(entries[:len(charged)], charged)
        paid = [entry for entry in entries if entry.get('actual', {}).get('llm_requests')]
        self.assertEqual(len(paid), 4)
        self.assertTrue(all(entry['status'] == 'settled' for entry in paid))
        self.assertEqual(sum(entry['actual']['llm_requests'] for entry in paid), 4)
        self.assertEqual(sum(entry['actual']['total_tokens'] for entry in paid), 560)
        self.assertEqual(self.app.ledger.remaining('llm_requests'), 0)
        self.assertFalse(self.app.ledger.unknown_dimensions())
        self.assertFalse(self.app.ledger.over_limit())

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
                self.decisions.append(_action('deliver_result', **self._delivery('report.md')))
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
                inspected = self._ok('deliver_result')['metadata']['delivery_review']['result']
                self.assertEqual(self._assessment()['verdict'], 'passed')
                identity = Path(inspected['data']['record']).parts[1]
                saved = self.app.session.store.read_json(f'tools/inspection/{identity}/input.json')
                ledger = self.app.ledger.to_dict()
                self._reload()
                cached = self.app.operations.inspection.call('inspect_artifacts',saved['arguments'],identity,
                    delivery_context=saved['delivery_context'])
                self.assertEqual(cached.status,'ok',cached)
                self.assertEqual(self.app.ledger.to_dict(), ledger)
                self.assertEqual(self.send.call_count-start, 3)

    def test_inspection_low_capacity_still_requires_a_real_terminal_opinion(self):
        # Request pressure, token pressure, and unknown prior usage all use
        # the same owner; none may synthesize a successful assessment.
        for mode in ('one_request', 'tokens', 'unfinished', 'unoffered_read', 'invalid_final', 'unknown', 'bounded_prior'):
            with self.subTest(mode=mode):
                self.root = self.directory/mode
                self._create()
                self._write('report.md', 'The sample mean is 2.0.\n')
                ledger = self.app.ledger
                if mode in {'one_request', 'unfinished', 'unoffered_read', 'invalid_final'}:
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
                    if mode == 'invalid_final':
                        return {'decisions':[]}
                    if mode == 'unfinished':
                        self.assertNotIn('For necessary claims', self.inspection_requests[-1]['messages'][-1]['content'])
                        return {'decisions':[{'subject':'target-1','conclusion':'incomplete',
                            'findings':[],'evidence':[],
                            'limitations':['Necessary source investigation remains unfinished at this boundary.']}]}
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
                if mode == 'invalid_final':
                    self.assertIn('final response invalid', result.error)
                    self.assertIn('exactly one decision per target', result.error)
                if mode == 'unoffered_read':
                    identity = Path(result.data['record']).parts[1]
                    trace = self.app.session.store.read_json(f'tools/inspection/{identity}/sources-0.json')
                    self.assertEqual(trace['results'], [])
                if mode in {'unknown','bounded_prior'}:
                    self.assertEqual(ledger.entries[0].status, 'unknown')

    def test_fresh_inspection_binds_only_the_unique_omitted_subject_with_explicit_peers(self):
        self._create()
        for index in range(1, 4):
            self._write(f'target-{index}.md', f'Observed claim {index}.\n')
        finding = {'severity':'required', 'message':'The population claim lacks a sampling basis.'}
        decisions = [
            {'subject':'target-3', 'conclusion':'complete', 'findings':[], 'evidence':[], 'limitations':[]},
            {'conclusion':'complete', 'findings':[finding], 'evidence':[], 'limitations':[]},
            {'subject':'target-1', 'conclusion':'complete', 'findings':[], 'evidence':[], 'limitations':[]}]
        original = deepcopy(decisions)
        self.review = lambda _: [_action('record_inspection', decisions=decisions)]
        result = self._inspect([f'target-{index}.md' for index in range(1, 4)], [])
        self.assertEqual(result['status'], 'ok', result)
        assessments = _assessments(result)
        self.assertEqual([row['verdict'] for row in assessments], ['passed', 'needs_revision', 'passed'])
        self.assertEqual(assessments[1]['findings'][0]['message'], finding['message'])
        self.assertEqual(decisions, original)
        identity = Path(result['data']['record']).parts[1]
        store = self.app.session.store
        trace = store.read_json(f'tools/inspection/{identity}/sources-0.json')
        self.assertEqual(json.loads(trace['response']['tool_calls'][0]['function']['arguments']),
                         {'decisions':original})
        self.assertEqual(trace['owner_bindings'], [{'decision_index':1, 'subject':'target-2'}])
        saved = store.read_json(f'tools/inspection/{identity}/input.json')
        # Historical validation stays strict even when the fresh path could bind it.
        owner = self.app.operations.inspection
        with self.assertRaises(ValueError):
            owner._subject_decisions(saved, {'decisions':original})
        historical, error = owner._subject_assessments(saved, {'response':{'decisions':original}}, identity, '')
        self.assertTrue(error)
        self.assertTrue(all(row['verdict']=='incomplete' and not row['findings'] for row in historical))
        records = self._versions(self.root/f'tools/inspection/{identity}')
        calls, ledger = self.send.call_count, self.app.ledger.to_dict()
        self._reload()
        cached = self.app.operations.inspection.call('inspect_artifacts', saved['arguments'], identity)
        self.assertEqual(cached.metadata['assessments'], assessments)
        self.assertEqual(self.send.call_count, calls)
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        self.assertEqual(self._versions(self.root/f'tools/inspection/{identity}'), records)

    def test_fresh_inspection_rejects_ambiguous_missing_and_explicit_invalid_subjects(self):
        for mode in ('ambiguous', 'wrong', 'blank', 'null', 'duplicate', 'short'):
            with self.subTest(mode=mode):
                self.root = self.directory/mode
                self._create()
                for index in range(1, 4):
                    self._write(f'target-{index}.md', f'Observed claim {index}.\n')
                decisions = [{'conclusion':'complete', 'findings':[{
                    'severity':'required', 'message':'Malformed opinion must not be adopted.'}],
                    'evidence':[], 'limitations':[]} for _ in range(3)]
                decisions[0]['subject'] = 'target-1'
                if mode != 'ambiguous':
                    decisions[1]['subject'] = {'wrong':'target-999', 'blank':'', 'null':None,
                        'duplicate':'target-1', 'short':'target-2'}[mode]
                if mode == 'short':
                    decisions.pop()
                self.review = lambda _: [_action('record_inspection', decisions=decisions)]
                before = len(self.inspection_requests)
                result = self._inspect([f'target-{index}.md' for index in range(1, 4)], [])
                self.assertEqual(result['status'], 'error', result)
                self.assertEqual(len(self.inspection_requests)-before, 2)
                self.assertTrue(all(row['verdict']=='incomplete' and not row['findings']
                    for row in _assessments(result)))

    def test_binary_evidence_diagnostics_allow_one_fresh_correction_with_omitted_subject(self):
        self._create()
        self._write('report.md', 'A population inference from three observations.\n')
        evidence = ['basis-1.txt', 'first.png', 'basis-2.txt', 'second.png']
        for index, filename in enumerate(evidence):
            self._write(filename, f'Observation {index}: no sampling design.\n')
        before = len(self.inspection_requests)
        finding = {'severity':'required', 'message':'The population inference lacks a sampling design.'}
        def review(_):
            if len(self.inspection_requests) == before+1:
                return [_action('record_inspection', decisions=[{
                    'subject':'target-1', 'conclusion':'complete', 'findings':[finding],
                    'evidence':[f'evidence-{index}' for index in (1, 2, 3, 4)], 'limitations':[]}])]
            error = self._inspection_history()[0]['error']
            diagnostics = json.loads(error[error.index('{'):])
            subject, = diagnostics['subjects']
            self.assertEqual(subject['subject'], 'target-1')
            self.assertEqual(subject['invalid']['ids'], ['evidence-2', 'evidence-4'])
            self.assertEqual(subject['eligible']['ids'], ['evidence-1', 'evidence-3'])
            return [_action('record_inspection', decisions=[{'conclusion':'complete',
                'findings':[finding], 'evidence':['evidence-1', 'evidence-3'], 'limitations':[]}])]
        self.review = review
        result = self._inspect(['report.md'], evidence)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(len(self.inspection_requests)-before, 2)
        assessment, = _assessments(result)
        self.assertEqual(assessment['verdict'], 'needs_revision')
        self.assertEqual(assessment['findings'][0]['message'], finding['message'])
        self.assertEqual(set(assessment['evidence_sha256']),
                         {str(self.root/f'workspace/basis-{index}.txt') for index in (1, 2)})
        identity = Path(result['data']['record']).parts[1]
        trace = self.app.session.store.read_json(f'tools/inspection/{identity}/sources-0.json')
        self.assertEqual(trace['results'][0]['result']['status'], 'blocked')
        self.assertNotIn('owner_bindings', trace)
        self.assertEqual(json.loads(trace['response']['tool_calls'][0]['function']['arguments'])
            ['decisions'][0]['evidence'], [f'evidence-{index}' for index in (1, 2, 3, 4)])

    def test_exhausted_final_retains_only_unadopted_diagnostic_and_authoritative_trace(self):
        self._create()
        self._write('report.md', 'A measured result.\n')
        self._write('figure.png', 'Not eligible text evidence.\n')
        message = 'An aggregate claim may hide cohort differences.'
        decisions = [{'subject':'target-1', 'conclusion':'complete',
            'findings':[{'severity':'required', 'message':message}],
            'evidence':['evidence-1'], 'limitations':[]}]
        self.review = lambda _: [_action('record_inspection', decisions=decisions)]
        before = len(self.inspection_requests)
        result = self._inspect(['report.md'], ['figure.png'])
        self.assertEqual(result['status'], 'error', result)
        self.assertEqual(len(self.inspection_requests)-before, 2)
        self.assertTrue(all(row['verdict']=='incomplete' and not row['findings']
            for row in _assessments(result)))
        diagnostic = result['data']['rejected_opinion']
        self.assertIn('not an assessment, fact or pass', diagnostic['scope'])
        self.assertIn('Requires valid reinspection', diagnostic['scope'])
        self.assertEqual(diagnostic['candidate_excerpt'],
                         [{'subject':'target-1', 'candidate_messages':[message]}])
        self.assertIn('evidence-1', diagnostic['validation_error'])
        self.assertTrue(diagnostic['record'].endswith('/sources-1.json'))
        trace = self.app.session.store.read_json(diagnostic['record'])
        self.assertEqual(trace['results'][0]['call_id'], diagnostic['call_id'])
        self.assertEqual(json.loads(trace['response']['tool_calls'][0]['function']['arguments']),
                         {'decisions':decisions})
        self.assertNotIn('rejected_opinion', result['metadata'])
        identity = Path(result['data']['record']).parts[1]
        saved = self.app.session.store.read_json(f'tools/inspection/{identity}/input.json')
        records = self._versions(self.root/f'tools/inspection/{identity}')
        calls, ledger = self.send.call_count, self.app.ledger.to_dict()
        self._reload()
        cached = self.app.operations.inspection.call('inspect_artifacts', saved['arguments'], identity)
        self.assertEqual(cached.status, 'blocked', cached)
        self.assertEqual(self.send.call_count, calls)
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        self.assertEqual(self._versions(self.root/f'tools/inspection/{identity}'), records)

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
            # Raw native calls bypass the fixture's convenience conclusion.
            'missing_conclusion': [_action('record_inspection', decisions=[{
                'subject':'target-1','findings':[],'evidence':[],'limitations':[]}])],
        }
        for name, reference in {
                'unread_reference':'evidence-999', 'self_reference':'target-1',
                'chunk_reference':'evidence-1:0', 'path_reference':'basis.txt',
                'verbose_reference':'evidence-1 (basis.txt, lines 1-2)',
                'repeated_reference':'evidence-1:0'}.items():
            cases[name] = [_action('record_inspection', decisions=[{
                'subject':'target-1','conclusion':'complete','findings':[],
                'evidence':[reference],'limitations':[]}])]
        for name, batch in cases.items():
            with self.subTest(batch=name):
                self.root = self.directory/name
                self._create(report=True)
                self._write('report.md', 'The sample mean is 2.0.\n')
                self._write('basis.txt', 'The supplied sample values are 1, 2, 3.\n')
                evidence = ['basis.txt'] if name.endswith('_reference') else []
                if name=='repeated_reference':
                    self.review = lambda _: {'findings':[{'subject':'target-1','severity':'required',
                        'message':'The claim needs correction against the original observations.'}], 'limitations':[]}
                    self._inspect(['report.md'], evidence)
                before = len(self.inspection_requests)
                originals = self._versions(self.inputs)
                candidate = (self.root/'workspace/report.md').read_bytes()

                def recover(view):
                    if len(self.inspection_requests)==before+1:
                        if evidence:
                            terminal = next(tool['function']['parameters']
                                for tool in self.inspection_requests[-1]['tools']
                                if tool['function']['name']=='record_inspection')
                            branch = terminal['properties']['decisions']['items']['anyOf'][0]
                            self.assertEqual(branch['properties']['evidence']['items']['enum'], ['evidence-1'])
                        return batch
                    self.assertEqual(len(self.inspection_requests), before+2)
                    replies = self._inspection_history()
                    self.assertEqual(len(replies), len(batch))
                    self.assertTrue(all(reply['status']!='ok' for reply in replies), replies)
                    if name=='missing_conclusion':
                        self.assertIn('conclusion', replies[0]['error'])
                        self.assertIn('Field required', replies[0]['error'])
                    if evidence:
                        self.assertIn('bare observation subject ID', replies[0]['error'])
                    if name=='repeated_reference':
                        return batch
                    return {'findings':[], 'limitations':[]}

                self.review = recover
                workspace = self.app.operations.inspection.workspace
                with patch.object(workspace, 'call', wraps=workspace.call) as reads:
                    result = self._inspect(['report.md'], evidence)
                if name=='repeated_reference':
                    from simple_ar.core.assessments import assessment_issues

                    self.assertEqual(result['status'], 'error', result)
                    self.assertIn('one corrective response exhausted', result['error'])
                    self.assertEqual(_assessments(result)[0]['verdict'], 'incomplete')
                    self.assertEqual((self.root/'workspace/report.md').read_bytes(), candidate)
                    self.assertTrue(assessment_issues(self.app.workspace, self._observations(), ['report.md']))
                    identity = Path(result['data']['record']).parts[1]
                    saved = self.app.session.store.read_json(f'tools/inspection/{identity}/input.json')
                    self.assertEqual([row['subject'] for row in saved['evidence']], ['evidence-1'])
                    self.assertTrue(saved['evidence'][0]['windows'])
                    self.assertTrue(saved['prior_required'])
                    self.assertEqual(len(saved['source_rounds']), 2)
                    for number in range(2):
                        trace = self.app.session.store.read_json(f'tools/inspection/{identity}/sources-{number}.json')
                        self.assertEqual(json.loads(trace['response']['tool_calls'][0]['function']['arguments']),
                            batch[0]['arguments'])
                        self.assertEqual(trace['results'][0]['result']['status'], 'blocked')
                        self.assertIn('bare observation subject ID', trace['results'][0]['result']['error'])
                    receipts = self._versions(self.root/f'tools/inspection/{identity}')
                    expected = self.app.session.store.read_json(f'tools/inspection/{identity}/result.json')
                    calls, ledger = self.send.call_count, self.app.ledger.to_dict()
                    self._reload()
                    cached = self.app.operations.inspection.call('inspect_artifacts', saved['arguments'], identity)
                    # An invalid final has no completed professional opinion.
                    # Recovery must refuse replay, not reinterpret it as a pass.
                    self.assertEqual(cached.status, 'blocked', cached.error)
                    self.assertIn('No completed inspection response', cached.error)
                    self.assertEqual(self.app.session.store.read_json(
                        f'tools/inspection/{identity}/result.json'), expected)
                    self.assertEqual(self.send.call_count, calls)
                    self.assertEqual(self.app.ledger.to_dict(), ledger)
                    self.assertEqual(self._versions(self.root/f'tools/inspection/{identity}'), receipts)
                else:
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

    def test_review_investigates_beyond_six_rounds_and_binds_its_own_conclusion(self):
        self._create()
        self._write('notes.md', 'A narrow observation requiring source verification.\n')
        initial = len(self.inspection_requests)
        before = self._versions(self.inputs)
        def investigate(view):
            self.assertNotIn('source_rounds_remaining', view)
            if len(self.inspection_requests)-initial <= 8:
                return [_action('read_file', path='measurements/conditions.txt')]
            return {'decisions':[{'subject':'target-1', 'conclusion':'incomplete',
                'findings':[], 'evidence':[], 'limitations':['The necessary comparison is still unresolved.']}]}
        self.review = investigate
        result = self._inspect(['notes.md'])
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(len(self.inspection_requests)-initial, 9)
        self.assertEqual(self._assessment()['verdict'], 'incomplete')
        self.assertEqual(self._assessment()['coverage'], 'partial')
        self.assertFalse(self._assessment()['findings'])
        self.assertEqual(self._versions(self.inputs), before)
        self.assertFalse(self.app.ledger.over_limit())
        identity = Path(result['data']['record']).parts[1]
        saved = self.app.session.store.read_json(f'tools/inspection/{identity}/input.json')
        ledger, calls = self.app.ledger.to_dict(), self.send.call_count
        receipts = self._versions(self.root/f'tools/inspection/{identity}')
        self._reload()
        cached = self.app.operations.inspection.call('inspect_artifacts', saved['arguments'], identity)
        self.assertTrue(cached.ok, cached)
        self.assertEqual(cached.metadata['assessments'], result['metadata']['assessments'])
        self.assertEqual(self.send.call_count, calls)
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        self.assertEqual(self._versions(self.root/f'tools/inspection/{identity}'), receipts)

    def test_unfinished_review_revokes_pass_but_preserves_existing_required_findings(self):
        from simple_ar.core.assessments import assessment_issues
        self._create()
        self._write('notes.md', 'The sample mean needs verification.\n')
        self._inspect(['notes.md'])
        self.assertEqual(self._assessment()['verdict'], 'passed')
        unfinished = {'decisions':[{'subject':'target-1', 'conclusion':'incomplete',
            'findings':[], 'evidence':[], 'limitations':['The original measurements need further review.']}]}
        self.review = lambda _: unfinished
        self._inspect(['notes.md'])
        self.assertTrue(assessment_issues(self.app.workspace, self._observations(),
            ['notes.md'], require_pass=['notes.md']))
        self.review = lambda view: {'findings':[{'subject':'target-1', 'severity':'required',
            'message':'The stated claim is not supported by the observed sample.'}], 'limitations':[]}
        self._inspect(['notes.md'])
        self.review = lambda _: unfinished
        self._inspect(['notes.md'])
        self.assertEqual(self._assessment()['verdict'], 'incomplete')
        self.assertTrue(assessment_issues(self.app.workspace, self._observations(), ['notes.md']))
        self.review = lambda _: {'findings':[], 'limitations':[]}
        self._inspect(['notes.md'])
        self.assertFalse(assessment_issues(self.app.workspace, self._observations(), ['notes.md']))

    def test_failed_legacy_complete_read_cannot_erase_a_required_opinion(self):
        from simple_ar.core.assessments import assessment_issues
        from simple_ar.tools.inspection import InspectionToolGateway
        self._create()
        self._write('notes.md', 'A claim requiring correction.\n')
        gateway = InspectionToolGateway(self.root, self.app.workspace,
            self.app.operations.inspection.client_factory)
        args = {'goal':'Check the claim', 'targets':['notes.md']}
        self.review = lambda _: {'findings':[{'subject':'target-1', 'severity':'required',
            'message':'The claim contradicts the observed source.'}], 'limitations':[]}
        negative = gateway.call('inspect_artifacts', args, 'legacy-negative').model_dump(mode='json')
        self.assertEqual(negative['metadata']['assessment']['verdict'], 'needs_revision')
        def fail(_):
            raise ValueError('Controlled failure after complete target read')
        self.review = fail
        failed = gateway.call('inspect_artifacts', args, 'legacy-failed').model_dump(mode='json')
        self.assertEqual(failed['status'], 'error')
        self.assertEqual(failed['metadata']['assessment']['coverage'], 'complete')
        self.assertEqual(failed['metadata']['assessment']['verdict'], 'incomplete')
        self.assertTrue(assessment_issues(self.app.workspace, [negative, failed], ['notes.md']))
        self.assertTrue(assessment_issues(self.app.workspace, [negative, failed],
            ['notes.md'], require_pass=['notes.md']))
        ledger, calls = self.app.ledger.to_dict(), self.send.call_count
        records = self._versions(self.root/'tools/inspection')
        gateway.call('inspect_artifacts', args, 'legacy-failed')
        self.assertEqual(self.send.call_count, calls)
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        self.assertEqual(self._versions(self.root/'tools/inspection'), records)

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
        # A full same-file initial evidence window belongs to the partial target;
        # its body stays visible without supplying a second evidence identity.
        self._inspect([{'path':'notes.md','max_bytes':8}],
            [{'path':'./notes.md','max_bytes':12000}])
        self.assertEqual(self._assessment()['verdict'], 'passed')
        view = self.inspection_views[-1]
        self.assertEqual(view['goal'], 'Check the claims against supplied observations')
        target, = view['targets']
        self.assertEqual(target['path'], str(self.root/'workspace/notes.md'))
        self.assertNotIn(target['path'], [row['path'] for row in view['evidence']])
        # Optional journal sources may now be present, but neither this target
        # nor a copy of its bytes may become independent support for itself.
        terminal = next(tool['function']['parameters']
            for tool in self.inspection_requests[-1]['tools']
            if tool['function']['name'] == 'record_inspection')
        branch, = terminal['properties']['decisions']['items']['anyOf']
        eligible = branch['properties']['evidence']['items'].get('enum', [])
        self.assertNotIn(target['subject'], eligible)
        for row in view['evidence']:
            if row.get('sha256') == target['sha256']:
                self.assertNotIn(row['subject'], eligible)
        assessment = self._assessment()
        for name in ('notes.md', 'claimed-evidence.md'):
            self.assertNotIn(str(self.root/'workspace'/name), assessment['evidence_sha256'])
        self.assertIn(limitation, assessment['limitations'])
        self.assertIn((self.root/'workspace/notes.md').read_text(),
            [window['text'] for window in view['targets'][0]['text_windows']])
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

    def test_unreadable_text_targets_reject_admission_without_poisoning_prior_review(self):
        from simple_ar.core.assessments import assessment_issues

        self._create(report=True)
        self._write('notes.md', 'The sample mean is 2.0 from three observations.\n')
        self._inspect(['notes.md'])
        self.assertEqual(self._assessment()['verdict'], 'passed')
        for name, content in (('figure.png', b'\x89PNG\r\n\x1a\n'),
                              ('unreadable.txt', b'\xffinvalid UTF-8'),
                              ('control.txt', b'not\x00text')):
            with self.subTest(target=name):
                (self.root/'workspace'/name).write_bytes(content)
                calls = self.send.call_count
                ledger = deepcopy(self.app.ledger.to_dict())
                result = self._inspect(['notes.md', name], [])
                self.assertEqual(result['status'], 'error', result)
                self.assertIn('requires readable targets', result['error'])
                self.assertFalse(result['artifacts'])
                self.assertFalse(result['metadata'])
                self.assertEqual(self.send.call_count, calls)
                self.assertEqual(self.app.ledger.to_dict(), ledger)
                observation = [row for row in self.app._operation_observations()
                    if row['tool_name'] == 'inspect_artifacts'][-1]
                identity = Path(observation['record']).parent.name
                self.assertFalse(self.app.operations.inspection.has_started(identity))
                self._reload()
                self.assertEqual(assessment_issues(self.app.workspace,
                    self.app._operation_observations(), ['notes.md', name],
                    require_pass=['notes.md']), [])
        self._submit(_action('deliver_result', **self._delivery('notes.md')))
        self.assertEqual(self.app.session.manifest.status, 'completed', self.app.result_snapshot())

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
                self._load_history() if legacy else self._create()
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

    def test_direct_source_edit_uses_current_producer_and_preserves_consumed_old_proof(self):
        self._create()
        originals = self._versions(self.inputs)
        raw = str(self.inputs/'observations.csv')
        self._read('measurements/observations.csv')
        code = ("from pathlib import Path\nimport sys\n"
            "raw = Path(sys.argv[1]).read_text(encoding='utf-8')\n"
            "Path('final.txt').write_text('old:' + raw, encoding='utf-8')\n"
            "Path('retained.txt').write_text('old:' + raw, encoding='utf-8')\n")
        self._submit(_action('write_file',path='produce.py',content=code))
        self._ok('write_file')
        candidate = self.app.workspace.workspace
        source, final, retained = (str(candidate/name) for name in ('produce.py','final.txt','retained.txt'))
        self._submit(_action('run_process',argv=['python',source,raw],
            inputs=[source,'measurements/observations.csv'],outputs=[final,retained],timeout_sec=5),
            approve=lambda *_:True)
        executed = self._ok('run_process')['data']
        execution_record = self.root/executed['receipt']
        old_records = self._versions(execution_record.parent)
        old_receipts = self._process_receipts()
        old_usage = [row.to_dict() for row in self.app.ledger.entries]
        retained_bytes = Path(retained).read_bytes()

        # This current output really consumes an old generated output. Its own
        # fresh input/output hashes cannot replace the ancestor's source proof.
        self._submit(_action('run_process',argv=['python','-c',
            "from pathlib import Path; import sys; "
            "Path('derived.txt').write_bytes(Path(sys.argv[1]).read_bytes())",retained],
            inputs=[retained],outputs=['derived.txt'],timeout_sec=5),approve=lambda *_:True)
        self._ok('run_process')
        self.app.workspace.check_artifact_producers(['derived.txt'])
        revised = code.replace("'old:'","'current:'").split("Path('retained.txt')")[0]
        observed = self._read(source)
        self._submit(_action('edit_file',path=source,expected_sha256=observed['sha256'],
            old_text=code,new_text=revised))
        self._ok('edit_file')
        for artifact in (final,'derived.txt'):
            with self.assertRaises(ValueError):
                self.app.workspace.check_artifact_producers([artifact])
            self._submit(_action('deliver_result',summary='Unregenerated output',artifacts=[artifact]))
            self.assertNotEqual(self._last('deliver_result')['status'],'ok')
            self._not_delivered()

        self._submit(_action('run_process',argv=['python',source,raw],cwd=str(candidate),
            inputs=[source,'measurements/observations.csv'],outputs=[final],timeout_sec=5),
            approve=lambda *_:True)
        self._ok('run_process')
        self.assertEqual(Path(final).read_text(encoding='utf-8'),
            'current:'+(self.inputs/'observations.csv').read_text(encoding='utf-8'))
        producer = self.app.workspace.artifact_provenance([final])[final]['producer']
        self.assertNotEqual(producer['invocation_id'],execution_record.parent.name)
        self.assertEqual(self.app.workspace.resolved_versions(producer['inputs']),
            {Path(source).resolve():_digest(Path(source)),Path(raw).resolve():_digest(Path(raw))})
        with self.assertRaises(ValueError):
            self.app.workspace.check_artifact_producers(['derived.txt'])
        self._submit(_action('deliver_result',summary='Still consumes the old result',artifacts=['derived.txt']))
        self.assertNotEqual(self._last('deliver_result')['status'],'ok')
        self._not_delivered()
        self._submit(_action('deliver_result',summary='Current source and freshly computed result',
            artifacts=[source,final]))
        self._ok('deliver_result')
        self.assertEqual(self.app.session.manifest.status,'completed',self.app.result_snapshot())
        self.assertEqual(self.app.session.store.read_json('delivery.json')['unresolved'],[])
        self.assertEqual(Path(retained).read_bytes(),retained_bytes)
        for path, content in old_records.items():
            self.assertEqual(path.read_bytes(),content)
        for path, content in old_receipts.items():
            self.assertEqual((self.root/path).read_bytes(),content)
        self.assertEqual([row.to_dict() for row in self.app.ledger.entries][:len(old_usage)],old_usage)
        self.assertEqual(self._versions(self.inputs),originals)
        bound = self.app.session.store.read_json('delivery.json')
        receipts, usage = self._process_receipts(),self.app.ledger.to_dict()
        self._reload()
        self._run()
        self.assertEqual(self.app.session.store.read_json('delivery.json'),bound)
        self.assertEqual(self._process_receipts(),receipts)
        self.assertEqual(self.app.ledger.to_dict(),usage)
        self.send.assert_not_called()

    def test_inline_producers_validate_actual_outputs_and_repair_stale_process_edges(self):
        self._create(delivery_files=['final.txt','conditions.txt'])
        delivery = {'summary':'Actual final output','artifacts':['final.txt']}
        self._submit(_action('write_file',path='final.txt',content='finished\n',delivery=delivery))
        self._ok('write_file')
        self._not_delivered()
        self.assertIn('conditions.txt','; '.join(self.app._delivery_feedback()['issues']))
        self._write('conditions.txt','Execution and interpretation are distinct.\n')
        self._submit(_action('deliver_result',summary='Actual final output',
            artifacts=['final.txt','conditions.txt']))
        self._ok('deliver_result')
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
        self.review = lambda view: {'findings': [
            {'subject': row['subject'], 'severity': 'required',
             'message': '9.0 contradicts the observed mean 2.0.'}
            for row in view['targets'] if any('9.0' in window['text'] for window in row['text_windows'])],
            'limitations': []}
        self._write('report.md','The sample mean is 2.0.\n')
        self._inspect(['report.md'])
        version = self._read('report.md')['sha256']
        self._submit(_action('write_file',path='report.md',expected_sha256=version,
            content='The sample mean is 9.0.\n',delivery=self._delivery('report.md')))
        self.assertIn('9.0 contradicts','; '.join(self.app._delivery_feedback()['issues']))
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

    def test_delivery_checks_actual_production_before_paid_content_review(self):
        self._create()
        self._write('report.md', '# Observations\n\nThree supplied observations.\n')
        calls = self.send.call_count
        delivery = self._delivery('report.md')
        delivery['report']['formats'] = ['markdown', 'pdf']
        self._submit(_action('export_report', markdown='report.md', compile_pdf=False,
            delivery=delivery), approve=lambda *_: True)
        produced = self._ok('export_report')
        self.assertTrue(self.app.workspace.resolve(produced['data']['project']).is_dir())
        self.assertIn('Delivery review not started', produced['metadata']['delivery_review_error'])
        self.assertIn('publication', produced['metadata']['delivery_review_error'].lower())
        self.assertEqual(self.send.call_count, calls)
        self.assertFalse((self.root/'tools/inspection').exists())
        self._not_delivered()
        ledger = self.app.ledger.to_dict()
        self._reload()
        self._run()
        self.assertEqual(self.send.call_count, calls)
        self.assertEqual(self.app.ledger.to_dict(), ledger)

        # An inline writer can complete production while its proposed result
        # still consumes a stale executed output. Reject before review here too.
        self.root = self.directory/'stale-output-admission'
        self._create()
        self._write('input.txt', 'first\n')
        self._submit(_action('run_process', argv=['python', '-c',
            "from pathlib import Path; Path('value.txt').write_text(Path('input.txt').read_text())"],
            inputs=['input.txt'], outputs=['value.txt']), approve=lambda *_: True)
        self._ok('run_process')
        self._submit(_action('write_file', path='input.txt', content='changed\n',
            expected_sha256=_digest(self.root/'workspace/input.txt')))
        calls = self.send.call_count
        self._submit(_action('write_file', path='report.md', content='# Current report\n',
            delivery=self._delivery('report.md', 'value.txt')))
        produced = self._ok('write_file')
        self.assertEqual((self.root/'workspace/report.md').read_text(), '# Current report\n')
        self.assertIn('producer inputs/outputs changed', produced['metadata']['delivery_review_error'])
        self.assertEqual(self.send.call_count, calls)
        self.assertFalse((self.root/'tools/inspection').exists())
        self._not_delivered()

        # A successful analysis is also a producer. The admission consumer
        # receives its retained observation before checking scientific coverage.
        self.root = self.directory/'analysis-delivery-admission'
        execution = {'command':[sys.executable,'-c',"print('score: 1')"],
            'cwd':str(self.inputs), 'timeout_sec':5,
            'result_schema':{'primary_metric':'score','required_metrics':['score']}}
        self.app = ToolApplication.create(self.root, 'Measure and explain the supplied control',
            model='fixture', services=self.services, sources={'measurements':self.inputs},
            execution_contract=execution, allow_execution=True, tool_contracts='full',
            max_attempts=30, process_limits={'process_invocations':4,'process_wall_seconds':20})
        self._submit(_action('measure_experiment', **execution), approve=lambda *_:True)
        measured = self._ok('measure_experiment')
        self._write('report.md', '# Control\n\nThe recorded score is 1.\n')
        with patch.object(self.app.scientific, 'evidence_requirement',
                wraps=self.app.scientific.evidence_requirement) as check:
            self._submit(_action('analyze_measurements',
                result_refs=[measured['data']['result_ref']],
                delivery=self._delivery('workspace/report.md')))
        produced = self._ok('analyze_measurements')
        self.assertNotIn('delivery_review_error', produced['metadata'])
        self.assertTrue(check.call_args_list)
        for call in check.call_args_list:
            self.assertTrue(call.args[1])
            for record in call.args[1]:
                self.assertTrue((self.root/record).is_file())
        self.assertEqual(self.app.session.manifest.status, 'completed', self.app.result_snapshot())

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
        self._load_history()
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
