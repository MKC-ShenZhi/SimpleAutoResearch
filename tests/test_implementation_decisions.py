"""Server-side offline checks of the consumed professional decision flow.

Provider transport is faked; project receipts and copies use the existing
owners, with an injected copy corruption for failure coverage. One historical
approval check runs a tiny process; the remaining checks need no process or
coordinator call.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from simple_ar.app.tool_application import ToolApplication
from simple_ar.integrations.llm import LLMClient, LLMSettings


class ImplementationDecisionTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.responses, self.prompts = [], []
        client = LLMClient(LLMSettings(api_key='offline-fixture', model='fixture',
            api_mode='chat', max_output_tokens=2048, retry_attempts=1, context_input_tokens=100000))
        self.services = SimpleNamespace(llm_client=client)
        self.send = self.enterContext(patch('simple_ar.integrations.llm._call_openai_sdk',
            side_effect=self._provider))
        self.coordinator = self.enterContext(patch.object(LLMClient, 'chat_turn',
            side_effect=AssertionError('Preparation must not call the coordinator')))

    def _provider(self, mode, request, **kwargs):
        self.assertEqual(mode, 'chat')
        self.assertFalse(request.get('tools'))
        self.assertIn('Implement one coherent, bounded code work unit', request['messages'][0]['content'])
        prompt, _ = json.JSONDecoder().raw_decode(request['messages'][1]['content'])
        self.prompts.append(prompt)
        self.assertTrue(self.responses, 'Unexpected extra model step: ' + request['messages'][-1]['content'][-2500:])
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        finish, content = response if isinstance(response, tuple) else ('stop', json.dumps(response))
        return {'choices': [{'message': {'role': 'assistant', 'content': content}, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}}

    def _create(self, name='task', sources=None, *, operations=False):
        return ToolApplication.create(self.base / name, 'Implement from the authorized original inputs',
            model='fixture', sources=sources or {}, services=self.services,
            professional_inputs=True, allow_execution=True, max_turns=12, max_tokens=600000,
            process_limits={'process_invocations': 3, 'process_wall_seconds': 30},
            control_protocol='operations.v1' if operations else None)

    @staticmethod
    def _observations(app):
        return [app.session.store.read_json(row['result'])
            for turn in app.state['turns'] for row in turn['tools']]

    def _prepare(self, app, arguments, preceding=None):
        before = sum(row['tool_name'] == 'prepare_implementation' for row in self._observations(app))
        with patch('simple_ar.tools.implementation.select_implementation_files',
                side_effect=AssertionError('New work must not preselect code files')), \
                patch('simple_ar.tools.materials.implementation_materials',
                    side_effect=AssertionError('New work must not finalize a material selection')):
            app.run(actions=[*(preceding or []), {'tool_name': 'prepare_implementation',
                'arguments': arguments}], progress=lambda _: None,
                should_stop=lambda: sum(row['tool_name'] == 'prepare_implementation'
                    for row in self._observations(app)) > before)
        self.coordinator.assert_not_called()
        return self._observations(app)[-1]

    @staticmethod
    def _candidate(code):
        return {'summary': 'Save a bounded computed result', 'unresolved': [],
            'files': [{'path': 'calculate.py', 'content': code}],
            'execution_plan': {'argv': ['python', 'calculate.py'], 'outputs': ['result.txt'], 'timeout_sec': 3}}

    def test_ordinary_project_snapshot_preserves_unedited_members(self):
        project = self.base / 'ordinary'
        project.mkdir()
        for index in range(32):
            (project / f'module_{index}.py').write_text(f'value = {index}\n', encoding='utf-8')
        (project / 'settings.toml').write_bytes(b'enabled = true\r\n')
        (project / 'payload.bin').write_bytes(b'\x00\xff' * 160000)
        expected = {path.name: path.read_bytes() for path in project.iterdir()}
        (project / '.env').write_bytes(b'not a project input')
        (project / '.venv').mkdir()
        (project / '.venv/pyvenv.cfg').write_text('home = /not/imported', encoding='utf-8')
        (project / 'build').mkdir()
        (project / 'build/cache').write_bytes(b'not a project source')
        app = self._create('ordinary-admission', sources={'code': project})
        proposal = {'summary': 'Change one module', 'unresolved': [],
            'files': [{'path': 'module_0.py', 'replacements': [
                {'old_text': 'value = 0', 'new_text': 'value = 100'}]}],
            'execution_plan': {'argv': ['python', 'module_0.py'], 'outputs': ['result.txt'], 'timeout_sec': 3}}
        for root, preceding in (('code', []), ('workspace/copied', [
                {'tool_name': 'copy_project', 'arguments': {'source': 'code', 'path': 'copied'}}])):
            with self.subTest(root=root):
                self.responses = [proposal]
                prepared = self._prepare(app, {'goal': 'Change one module and preserve the project',
                    'project_path': root, 'complete_work': True}, preceding=preceding)
                self.assertEqual(prepared['status'], 'ok', prepared)
                candidate = Path(prepared['data']['project_dir'])
                self.assertEqual({path.name for path in candidate.iterdir()}, set(expected))
                for name, raw in expected.items():
                    self.assertEqual((candidate / name).read_bytes(),
                        b'value = 100\n' if name == 'module_0.py' else raw)
                source = json.loads(Path(prepared['data']['source_binding']).read_text())
                self.assertEqual(source['project_snapshot']['contract'], 'ordinary_project.v1')
                self.assertEqual(set(source['original_files']), {'module_0.py'})
        self.assertEqual((project / 'module_0.py').read_bytes(), expected['module_0.py'])

    def test_project_snapshot_capacity_refused_before_generation(self):
        for name, sizes in (('member', [65 * 1024 * 1024]),
                            ('aggregate', [45 * 1024 * 1024] * 3),
                            ('members', [1] * 4097)):
            with self.subTest(name=name):
                project = self.base / name
                project.mkdir()
                for index, size in enumerate(sizes):
                    with (project / f'part-{index}').open('wb') as stream:
                        stream.truncate(size)
                app = self._create('capacity-' + name, sources={'code': project})
                result = self._prepare(app, {'goal': 'Preserve the complete project',
                    'project_path': 'code', 'complete_work': True})
                self.assertEqual(result['status'], 'error', result)
                self.assertFalse(app.ledger.entries)
                self.send.assert_not_called()

    def test_retained_project_execution_binding_exceeds_direct_input_contract(self):
        """Baseline counterexample: approved owner inventory is not a direct-input list."""
        import hashlib
        from simple_ar.tools.implementation_run import execution_binding, execution_input_versions, resolve_execution_snapshot
        from simple_ar.tools.workspace import validate_workspace_arguments

        app, previous, _ = self._registered_large_project('direct-input-capacity')
        self.responses = [{'summary': 'Keep members and propose execution', 'unresolved': [],
            'files': [], 'execution_plan': previous.data['execution_plan']}]
        prepared = self._prepare(app, {'goal': 'Run the existing project',
            'project_path': previous.data['project_dir'], 'complete_work': True})
        self.assertEqual(prepared['status'], 'ok', prepared)
        binding = execution_binding(app.implementation,
            {'prepared_result': prepared['data']['prepared_result']}, identity='capacity-probe')
        inputs = list(execution_input_versions(binding))
        self.assertGreater(len(inputs), 64)
        with self.assertRaisesRegex(ValueError, '64'):
            validate_workspace_arguments('run_process', {'argv': binding['argv'],
                'cwd': binding['cwd'], 'inputs': inputs, 'outputs': binding['outputs']})
        # Pure execution-owner preflight: use the existing approval record, not
        # a generated input list or a fabricated preparation registration.
        store, _ = app.session.store.new_attempt('capacity-probe', capability='native_tool')
        store.write_json('execution_input.json', {'token': 'offline-approved', 'binding': binding})
        reference = {'record': 'records/capacity-probe/execution_input.json',
            'sha256': hashlib.sha256(store.resolve('execution_input.json').read_bytes()).hexdigest()}
        process = {'argv': binding['argv'], 'cwd': binding['cwd'],
            'timeout_sec': binding['plan']['timeout_sec'], 'literal_argv': True,
            'outputs': binding['outputs'], 'input_snapshot': reference}
        self.assertEqual(resolve_execution_snapshot(app.workspace, reference,
            process=process, identity='capacity-probe'), execution_input_versions(binding))
        # Approval hooks may lack an invocation ID; they still bind the actual
        # command, cwd, timeout and normalized output set to this owner receipt.
        self.assertEqual(resolve_execution_snapshot(app.workspace, reference,
            process=process), execution_input_versions(binding))
        for change in ({'argv': [binding['argv'][0], '-c', 'print(99)']},
                       {'cwd': str(app.workspace.workspace)},
                       {'timeout_sec': binding['plan']['timeout_sec'] + 1},
                       {'outputs': [str(app.workspace.workspace / 'unapproved.txt')]},
                       {'inputs': [str(app.workspace.workspace / 'unapproved-input')]},
                       {'literal_argv': False}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                resolve_execution_snapshot(app.workspace, reference,
                    process={**process, **change}, identity='capacity-probe')
        with self.assertRaisesRegex(ValueError, 'different execution attempt'):
            resolve_execution_snapshot(app.workspace, reference, process=process, identity='foreign-execution')
        with self.assertRaises(ValueError):
            resolve_execution_snapshot(app.workspace, {**reference, 'sha256': '0' * 64})
        self.assertFalse((app.session.store.root / 'tools/processes').exists())

    def test_project_execution_snapshot_runs_and_rechecks_all_members(self):
        from simple_ar.tools.implementation_run import resolve_execution_snapshot

        app, previous, _ = self._registered_large_project('snapshot-execution', binary_bytes=40 * 1024 * 1024)
        plan = {'argv': ['python', '-c', "from pathlib import Path; p=Path('observations/result.txt'); "
            "p.parent.mkdir(exist_ok=True); p.write_text('7')"],
            'outputs': ['observations/result.txt'], 'timeout_sec': 3}
        self.responses = [{'files': [], 'summary': 'Execute the complete snapshot',
            'unresolved': [], 'execution_plan': plan}]
        prepared = self._prepare(app, {'goal': 'Run the complete project snapshot',
            'project_path': previous.data['project_dir'], 'complete_work': True})
        self.assertEqual(prepared['status'], 'ok', prepared)
        app.run(actions=[{'tool_name': 'execute_implementation', 'arguments': {
            'prepared_result': prepared['data']['prepared_result']}}], progress=lambda _: None)
        call = app.state['pending']['call']
        binding = app._owner_execution_binding(call)
        self.assertGreater(len(binding['source_sha256']), 64)
        self.assertGreater(sum(Path(path).stat().st_size for path in binding['source_sha256']), 64 * 1024 * 1024)
        token = app.approval_token(call)
        app.run(approved_execution=token, progress=lambda _: None,
            should_stop=lambda: any(row['tool_name'] == 'execute_implementation'
                for row in self._observations(app)))
        executed = self._observations(app)[-1]
        self.assertEqual(executed['tool_name'], 'execute_implementation', executed)
        self.assertEqual(executed['status'], 'ok', executed)
        self.assertEqual(executed['data']['status'], 'run_succeeded')
        receipt = json.loads((app.session.store.root / executed['data']['receipt']).read_text())
        physical = receipt['input_binding']['workspace_artifacts']
        reference = physical['input_snapshot']
        self.assertEqual(set(reference), {'record', 'sha256'})
        self.assertEqual(app.workspace.resolved_versions(resolve_execution_snapshot(app.workspace, reference)),
            app.workspace.resolved_versions(physical['inputs']))
        output = next(iter(executed['data']['output_sha256']))
        app.workspace.check_artifact_producers([output])
        original_ledger = app.ledger.to_dict()
        restored = ToolApplication.load(app.session.store.root, services=self.services)
        restored.workspace.check_artifact_producers([output])
        self.assertEqual(restored.ledger.to_dict(), original_ledger)
        # Neither candidate copies nor retained source members can be sampled
        # away merely because the compact command argument has only two keys.
        for path in (Path(prepared['data']['project_dir']) / 'config/options',
                     Path(previous.data['project_dir']) / 'support/settings-27.toml'):
            raw = path.read_bytes()
            path.write_bytes(b'changed after execution')
            with self.assertRaises(ValueError):
                resolve_execution_snapshot(restored.workspace, reference)
            path.write_bytes(raw)
        self.assertEqual(restored.ledger.to_dict(), original_ledger)

    def test_project_execution_snapshot_rejects_change_after_approval(self):
        app, previous, _ = self._registered_large_project('snapshot-stale')
        self.responses = [{'files': [], 'summary': 'Prepare a fresh execution', 'unresolved': [],
            'execution_plan': previous.data['execution_plan']}]
        prepared = self._prepare(app, {'goal': 'Run the complete project snapshot',
            'project_path': previous.data['project_dir'], 'complete_work': True})
        app.run(actions=[{'tool_name': 'execute_implementation', 'arguments': {
            'prepared_result': prepared['data']['prepared_result']}}], progress=lambda _: None)
        token = app.approval_token(app.state['pending']['call'])
        (Path(prepared['data']['project_dir']) / 'config/options').write_bytes(b'changed before launch')
        app.run(approved_execution=token, progress=lambda _: None,
            should_stop=lambda: any(row['tool_name'] == 'execute_implementation'
                for row in self._observations(app)))
        observed = self._observations(app)[-1]
        self.assertEqual(observed['tool_name'], 'execute_implementation', observed)
        self.assertNotEqual(observed['status'], 'ok', observed)
        self.assertFalse(observed['data'].get('executed', True))
        self.assertFalse((app.session.store.root / 'tools/processes').exists())

    def test_command_identity_uses_bound_members_and_native_literal_paths(self):
        import os
        from simple_ar.code_task.generation.run_plan import ImplementationRunPlan

        old, candidate = self.base / 'source', self.base / 'candidate'
        external = self.base / 'materials/reference'
        interpreter = self.base / 'environment/bin/interpreter'
        binding = {'argv_semantics': 'candidate_relative.v2',
            'original_files': {'entry': {'path': str(old / 'original entry')}},
            'preserved_files': {'inputs/data': {'path': str(self.base / 'materials/copied')}},
            'prior_candidate': {'project_root': str(old), 'inventory': {'file_hashes': {
                str(old / 'support/extensionless'): 'digest'}},
                'preserved_files': {'inputs/retained': {'path': str(self.base / 'materials/retained')}}}}
        paths = ['entry', 'inputs/data', 'inputs/retained', 'support/extensionless']
        stale = str(old / 'original entry')
        rejected = ([stale], ['runner', '--mode', 'compute', stale],
            ['runner', '--asset=' + str(old / 'support/extensionless')],
            ['runner', '--data=' + str(self.base / 'materials/copied')],
            ['runner', str(self.base / 'materials/retained')],
            ['runner', os.path.relpath(old / 'original entry', candidate)],
            ['runner', str(old / 'support/../support/extensionless')])
        allowed = ([str(interpreter), 'entry'], ['runner', '--asset=support/extensionless'],
            ['runner', '--data=inputs/data', str(external)],
            ['runner', str(candidate / 'entry')],
            ['runner', str(old / 'unselected')], ['runner', 'print("inline code")'])
        for argv in (*rejected, *allowed):
            with self.subTest(argv=argv):
                plan = ImplementationRunPlan(argv=list(argv), outputs=['result.txt'])
                if argv in rejected:
                    with self.assertRaisesRegex(ValueError, 'copied source member'):
                        plan.validate_command_identity(candidate_root=candidate,
                            source_binding=binding, candidate_paths=paths)
                else:
                    plan.validate_command_identity(candidate_root=candidate,
                        source_binding=binding, candidate_paths=paths)
        # Both historical contracts keep the same formerly admitted command.
        for semantics in (None, 'candidate_relative.v1'):
            historical = {**binding, 'argv_semantics': semantics}
            ImplementationRunPlan(argv=['runner', stale], outputs=['result.txt']).validate_command_identity(
                candidate_root=candidate, source_binding=historical, candidate_paths=paths)
        with self.assertRaisesRegex(ValueError, 'Unknown retained command'):
            ImplementationRunPlan(argv=['runner'], outputs=['result.txt']).validate_command_identity(
                candidate_root=candidate, source_binding={**binding, 'argv_semantics': 'unknown'},
                candidate_paths=paths)

    def test_project_materials_read_then_implement_with_unchanged_dependency(self):
        import hashlib
        from simple_ar.code_task.generation.selection import ImplementationSources
        from simple_ar.tools.implementation_run import prepared_unit

        project, materials = self.base / 'project', self.base / 'materials'
        project.mkdir()
        materials.mkdir()
        old = 'from helper import value\nprint(value())\n'
        helper = 'def value():\n    return 7\n'
        (project / 'calculate.py').write_text(old, encoding='utf-8')
        (project / 'helper.py').write_text(helper, encoding='utf-8')
        (materials / 'requirements.txt').write_text('Save the existing helper value to result.txt.\n', encoding='utf-8')
        raw = b'\xef\xbb\xbf' + json.dumps({'scientific_notes_not_timing': ['\u539f\u59cb\u8bb0\u5f55'] * 1600,
            'timing': [1, 2, 3]}, ensure_ascii=False, indent=2).replace('\n', '\r\n').encode('utf-8')
        data = materials / 'records.json'
        data.write_bytes(raw)
        fixed = "from helper import value\nfrom pathlib import Path\nPath('result.txt').write_text(str(value()))\n"
        rejected = {'summary': 'Invalid plan after valid source binding', 'unresolved': [],
            'preserve_files': ['helper.py', {'source': 'materials/records.json', 'path': 'inputs/records.json'}],
            'files': [{'path': 'calculate.py', 'replacements': [{'old_text': old, 'new_text': fixed}]}],
            'execution_plan': {'argv': ['python', 'calculate.py'], 'outputs': ['calculate.py']}}
        accepted = {'summary': 'Preserve the runtime helper and complete original data', 'unresolved': [],
            'preserve_files': {'materials/records.json': 'inputs/records.json'},
            'files': [{'path': 'calculate.py', 'replacements': [{'old_text': old, 'new_text': fixed}]},
                {'path': 'helper.py', 'replacements': []}],
            'execution_plan': {'argv': ['python', 'calculate.py'], 'outputs': ['result.txt'], 'timeout_sec': 3}}
        self.responses = [
            {'read_windows': [{'path': 'code/calculate.py'}, {'path': 'materials/requirements.txt'}]},
            rejected, rejected, accepted]
        app = self._create(sources={'code': project, 'materials': materials})
        arguments = {'goal': 'Save the existing helper value', 'project_path': 'code',
            'material_roots': ['materials'], 'complete_work': True}
        result = self._prepare(app, arguments)
        self.assertEqual(result['status'], 'ok', result)
        candidate = Path(result['data']['project_dir'])
        self.assertEqual((candidate / 'calculate.py').read_text(), fixed)
        self.assertEqual((candidate / 'helper.py').read_text(), helper)
        self.assertEqual((candidate / 'inputs/records.json').read_bytes(), raw)
        self.assertEqual(data.read_bytes(), raw)
        self.assertEqual((project / 'calculate.py').read_text(), old)
        self.assertFalse((candidate / 'result.txt').exists())
        self.assertFalse((candidate / 'requirements.txt').exists())
        store = Path(result['data']['source_binding']).parent
        binding = json.loads((store / 'request.json').read_text())
        self.assertEqual(binding['generation_method'], 'professional_decision.v1')
        self.assertEqual(binding['request']['max_repairs'], 1)
        self.assertEqual(set(binding['original_files']), {'calculate.py', 'helper.py'})
        preserved = {'inputs/records.json': {'path': str(data.resolve()),
            'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw)}}
        self.assertEqual(binding['preserved_files'], preserved)
        approved, _, _ = prepared_unit(app.implementation, result['data']['prepared_result'])
        self.assertEqual(approved['source_sha256'][str(data.resolve())], preserved['inputs/records.json']['sha256'])
        self.assertEqual(approved['source_sha256'][str(candidate / 'inputs/records.json')],
            preserved['inputs/records.json']['sha256'])
        self.assertIsNone(app.workspace.artifact_provenance([
            str(candidate / 'inputs/records.json')])[str(candidate / 'inputs/records.json')]['producer'])
        self.assertNotIn('scientific_notes_not_timing', json.dumps(self.prompts))
        self.assertTrue(any(row.get('text') == old for row in self.prompts[1]['source_windows']))
        self.assertEqual(self.send.call_count, 4)
        self.assertEqual(sorted(path.name for path in (store / 'decision-steps').iterdir()), ['1', '2', '3', '4'])
        self.assertEqual(json.loads((store / 'decision-steps/4/response.json').read_text()), accepted)
        # Neither rejected candidate may turn tentative bindings into facts
        # supplied to the next decision (including immutable preserve roles).
        for prompt in self.prompts[2:4]:
            self.assertEqual(prompt['original_files'], {})
            self.assertNotIn('preserved_files', prompt['investigation'])
        self.assertEqual(len(self.prompts[3]['validation_observations']), 2)
        self.assertFalse((store / 'selection-input.json').exists())
        self.assertFalse((store / 'material-selection').exists())
        self.assertIn('Complete registered', result['data']['workspace_scope'])
        ledger = app.ledger.to_dict()
        self.assertEqual(sum(row.actual.get('llm_requests', 0) for row in app.ledger.entries), 4)
        identity = store.parent.name
        restored = ToolApplication.load(app.session.store.root, services=self.services)
        request_path = store / 'request.json'
        frozen_request = request_path.read_bytes()
        recovery_arguments = json.loads(frozen_request)['request']
        self.assertEqual(recovery_arguments['work_contract'], 'shared.v2')
        self.assertEqual(recovery_arguments['candidate_admission'], 'python_candidate_admission.v1')
        # A direct gateway consumer must restore the exact saved owner request;
        # public arguments acquire their internal markers only in the app.
        omitted = restored.implementation.call('prepare_implementation', arguments, identity=identity)
        self.assertEqual(omitted.status, 'error', omitted)
        self.assertIn('Professional generation request changed', omitted.error)
        self.assertEqual(request_path.read_bytes(), frozen_request)
        self.assertEqual(restored.ledger.to_dict(), ledger)
        self.assertEqual(self.send.call_count, 4)
        replay = restored.implementation.call('prepare_implementation', recovery_arguments, identity=identity)
        self.assertEqual(replay.status, 'ok', replay)
        self.assertEqual(replay.data, result['data'])
        self.assertEqual(request_path.read_bytes(), frozen_request)
        self.assertEqual(restored.ledger.to_dict(), ledger)
        self.assertEqual(self.send.call_count, 4)

        # Inventory permission, literal destinations and immutable role are
        # enforced by the same owner that consumes actual provider responses.
        outside = app.workspace.workspace / 'not-selected.json'
        outside.write_bytes(raw)
        sources = ImplementationSources(workspace=app.workspace, project=candidate,
            roots=['materials'], windows=[], originals={}, identity='copy-policy',
            preserved_files=preserved)
        for mapping in (
                {'source': 'workspace/not-selected.json', 'path': 'inputs/extra.json'},
                {'source': 'materials/records.json', 'path': '../escape.json'},
                {'source': 'materials/records.json', 'path': 'inputs/extra.json', 'sha256': '0' * 64},
                {'source': 'materials/records.json', 'path': 'calculate.py'}):
            with self.subTest(preserve=mapping), self.assertRaises(ValueError):
                sources.bind_response({'files': [{'path': 'calculate.py', 'replacements': []}],
                    'preserve_files': [mapping]})
            self.assertEqual(sources.originals, {})
            self.assertEqual(sources.preserved_files, preserved)
        for mappings in ({'materials/records.json': '../escape.json'},
                         {'materials/records.json': 'calculate.py'},
                         {'materials/records.json': {'path': 'data.json'}},
                         {'materials/records.json': 'same.json', 'code/helper.py': 'same.json'}):
            with self.subTest(mappings=mappings), self.assertRaises(ValueError):
                sources.bind_response({'files': [{'path': 'calculate.py', 'replacements': []}],
                    'preserve_files': mappings})
            self.assertEqual(sources.originals, {})
            self.assertEqual(sources.preserved_files, preserved)
        with self.assertRaises(ValueError):
            sources.bind_response({'files': [{'path': 'inputs/records.json', 'content': '{"timing":[]}'}]})

        # Feedback carries byte dependencies automatically, not full editable
        # originals or a second model transcription of the dataset.
        self.responses = [{'summary': 'Keep the candidate and its inputs', 'unresolved': [], 'files': [],
            'execution_plan': result['data']['execution_plan']}]
        revised = restored.implementation.call('prepare_implementation', {
            'goal': arguments['goal'], 'project_path': str(candidate), 'complete_work': True},
            identity='copy-feedback')
        self.assertEqual(revised.status, 'ok', revised)
        self.assertEqual((Path(revised.data['project_dir']) / 'inputs/records.json').read_bytes(), raw)
        self.assertEqual((candidate / 'inputs/records.json').read_bytes(), raw)
        self.assertNotIn('inputs/records.json', self.prompts[-1]['original_files'])
        self.assertNotIn('scientific_notes_not_timing', json.dumps(self.prompts[-1]))
        self.assertEqual(self.send.call_count, 5)
        data.write_bytes(raw + b'\n')
        before = restored.ledger.to_dict()
        changed = restored.implementation.call('prepare_implementation',
            json.loads(request_path.read_bytes())['request'], identity=identity)
        self.assertEqual(changed.status, 'error')
        self.assertIn('Preserved input changed', changed.error)
        self.assertEqual(request_path.read_bytes(), frozen_request)
        self.assertEqual(restored.ledger.to_dict(), before)
        self.assertEqual(self.send.call_count, 5)

    def test_ordinary_inputs_survive_execution_and_feedback_without_transcription(self):
        from simple_ar.code_task.generation.selection import ImplementationSources, preserved_input_hashes
        from simple_ar.tools.implementation_run import prepared_unit

        materials = self.base / 'records'
        materials.mkdir()
        for index in range(32):
            (materials / f'{index}.json').write_bytes(json.dumps({'value': index}).encode() + b'\r\n')
        mappings = {f'inputs/{i}.json': f'data/{i}.json' for i in range(32)}
        code = ("import json\nfrom pathlib import Path\n"
            "total = sum(json.loads(p.read_text())['value'] for p in Path('data').glob('*.json'))\n"
            "Path('result.txt').write_text(str(total))\n")
        proposal = self._candidate(code)
        proposal['preserve_files'] = mappings
        self.responses = [{'read_windows': [{'path': 'inputs/0.json'}]}, proposal]
        app = self._create('independent-inputs', sources={'inputs': materials}, operations=True)
        prepared = self._prepare(app, {'goal': 'Sum the recorded values without rewriting inputs',
            'material_roots': ['inputs']})
        self.assertEqual(prepared['status'], 'ok', prepared)
        candidate = Path(prepared['data']['project_dir'])
        for source, destination in mappings.items():
            self.assertEqual((candidate / destination).read_bytes(), (materials / Path(source).name).read_bytes())
        source_binding = json.loads(Path(prepared['data']['source_binding']).read_text())
        self.assertEqual(source_binding['preserved_input_contract'], 'bounded_inputs.v2')
        self.assertEqual(source_binding['candidate_admission'], 'python_candidate_admission.v1')
        self.assertIsNotNone(self.prompts[0]['dependency_observation'])
        self.assertEqual(len(source_binding['preserved_files']), 32)
        self.assertEqual(sum('request' in row for row in source_binding['source_windows']), 1)
        # The same complete input inventory is not legal under a missing/old marker.
        with self.assertRaises(ValueError):
            preserved_input_hashes(app.workspace, source_binding['preserved_files'])
        legacy = ImplementationSources(workspace=app.workspace, project=None, roots=['inputs'],
            windows=[], originals={}, identity='historical-input-admission')
        with self.assertRaises(ValueError):
            legacy.bind_response(proposal)
        for contract in ('unknown', 'bounded_inputs.v3'):
            with self.assertRaises(ValueError):
                preserved_input_hashes(app.workspace, {}, contract=contract)

        app.run(actions=[{'tool_name': 'execute_implementation', 'arguments': {
            'prepared_result': prepared['data']['prepared_result']}}], progress=lambda _: None)
        token = app.approval_token(app.state['pending']['call'])
        app.run(approved_execution=token, progress=lambda _: None,
            should_stop=lambda: self._observations(app)[-1]['tool_name'] == 'execute_implementation')
        self.assertEqual(self._observations(app)[-1]['status'], 'ok')
        self.assertEqual((candidate / 'result.txt').read_text(), '496')
        ledger = app.ledger.to_dict()
        restored = ToolApplication.load(app.session.store.root, services=self.services)
        self.assertEqual(restored.ledger.to_dict(), ledger)
        prepared_unit(restored.implementation, prepared['data']['prepared_result'])
        self.responses = [{'summary': 'Add one to the computed sum; preserve all input bytes',
            'unresolved': [], 'files': [{'path': 'calculate.py', 'replacements': [{
                'old_text': 'str(total)', 'new_text': 'str(total + 1)'}]}],
            'execution_plan': prepared['data']['execution_plan']}]
        revised = self._prepare(restored, {'goal': 'Add one to the sum', 'project_path': str(candidate)})
        self.assertEqual(revised['status'], 'ok', revised)
        self.assertEqual(set(self.prompts[-1]['original_files']), {'calculate.py'})
        for destination in mappings.values():
            self.assertEqual((Path(revised['data']['project_dir']) / destination).read_bytes(),
                (candidate / destination).read_bytes())
        frozen = restored.ledger.to_dict()
        (materials / '31.json').write_text('{"value": 900}')
        with self.assertRaises(ValueError):
            prepared_unit(restored.implementation, revised['data']['prepared_result'])
        self.assertEqual(restored.ledger.to_dict(), frozen)
        self.coordinator.assert_not_called()

    def test_independent_input_capacity_is_atomic_and_keeps_edit_limit(self):
        from simple_ar.code_task.generation.selection import ImplementationSources

        material = self.base / 'bounded-inputs'
        material.mkdir()
        for index in range(65):
            (material / f'{index}.bin').write_bytes(b'\x00\xff')
        (material / 'large.bin').write_bytes(b'x' * 240000)
        app = self._create('input-capacity', sources={'inputs': material}, operations=True)
        source = ImplementationSources(workspace=app.workspace, project=None, roots=['inputs'],
            windows=[], originals={}, identity='capacity', input_contract='bounded_inputs.v2')
        code = self._candidate('print(7)\n')
        mappings = {f'inputs/{i}.bin': f'data/{i}.bin' for i in range(64)}
        originals, copied = source.bind_response({**code, 'preserve_files': mappings}, commit=False)
        self.assertEqual((len(originals), len(copied)), (0, 64))
        for invalid in (
                {**code, 'preserve_files': {**mappings, 'inputs/64.bin': 'data/64.bin'}},
                {**code, 'preserve_files': [{'source': 'inputs/large.bin', 'path': f'data/{i}.bin'}
                    for i in range(13)]},
                {**code, 'files': [{'path': f'module_{i}.py', 'content': 'value = 7\n'}
                    for i in range(13)]}):
            with self.assertRaises(ValueError):
                source.bind_response(invalid)
            self.assertEqual(source.originals, {})
            self.assertEqual(source.preserved_files, {})

    def _registered_subtree(self, name):
        materials = self.base / f'{name}-materials'
        materials.mkdir()
        (materials / 'notes.txt').write_text('Keep the calibration input unchanged.\n', encoding='utf-8')
        raw = b'\x00\xff\x01\r\n'
        (materials / 'calibration.bin').write_bytes(raw)
        app = self._create(name, sources={'materials': materials})
        self.responses = [{'summary': 'Complete source unit', 'unresolved': [],
            'files': [{'path': 'package/worker.py', 'content': 'value = 7\n'},
                {'path': 'support/helper.py', 'content': 'constant = 3\n'}],
            'preserve_files': {'materials/calibration.bin': 'inputs/calibration.bin'},
            'execution_plan': {'argv': ['python', 'package/worker.py'],
                'outputs': ['observations/result.txt'], 'timeout_sec': 3}}]
        result = self._prepare(app, {'goal': 'Prepare the complete calculation unit',
            'complete_work': True, 'source_windows': [{'path': 'materials/notes.txt', 'full_digest': True}],
            'material_roots': ['materials']})
        self.assertEqual(result['status'], 'ok', result)
        return app, result, raw

    def test_registered_subtree_feedback_preserves_unit_and_historical_evidence(self):
        from simple_ar.tools.implementation_run import execution_binding

        for complete in (True, False):
            with self.subTest(complete_work=complete):
                app, result, raw = self._registered_subtree(f'subtree-{complete}')
                candidate = Path(result['data']['project_dir'])
                # A retained runtime member is historical material, never a
                # source or fresh output of the next preparation.
                historical = candidate / 'observations/result.txt'
                historical.parent.mkdir()
                historical.write_text('historical observation\n', encoding='utf-8')
                record = app.workspace.resolve(result['data']['prepared_result'])
                proof = {path: path.read_bytes() for path in
                    (record, record.parent / 'request.json', record.parent.parent / 'native_result.json', historical)}
                self.responses = [{'summary': 'Change only selected source', 'unresolved': [],
                    'files': [{'path': 'package/worker.py',
                        'replacements': [{'old_text': 'value = 7', 'new_text': 'value = 8'}]},
                        {'path': 'package/extra.py', 'content': 'extra = 1\n'}],
                    'execution_plan': result['data']['execution_plan']}]
                identity = f'feedback-{complete}'
                revised = app.implementation.call('prepare_implementation', {
                    'goal': 'Revise the selected source subtree', 'project_path': str(candidate / 'package'),
                    'complete_work': complete, 'execution_requested': True}, identity=identity)
                self.assertEqual(revised.status, 'ok', revised)
                output = Path(revised.data['project_dir'])
                self.assertEqual((output / 'package/worker.py').read_text(), 'value = 8\n')
                self.assertEqual((output / 'support/helper.py').read_bytes(), (candidate / 'support/helper.py').read_bytes())
                self.assertEqual((output / 'inputs/calibration.bin').read_bytes(), raw)
                self.assertFalse((output / 'observations/result.txt').exists())
                prompt = self.prompts[-1]
                self.assertEqual(set(prompt['original_files']), {'package/worker.py'})
                self.assertIn('support/helper.py', prompt['investigation']['registered_project']['members'])
                self.assertTrue(any(row.get('text') == 'Keep the calibration input unchanged.\n'
                    for row in prompt['source_windows']))
                binding = json.loads((record.parents[2] / identity / 'implementation/request.json').read_text())
                self.assertEqual(binding['prior_candidate']['result_ref'], result['data']['prepared_result'])
                self.assertEqual(binding['prior_candidate']['editable_project'], str(candidate / 'package'))
                self.assertEqual({path: path.read_bytes() for path in proof}, proof)
                calls = self.send.call_count
                replay = app.implementation.call('prepare_implementation', binding['request'], identity=identity)
                self.assertEqual(replay.status, 'ok', replay)
                self.assertEqual(replay.data, revised.data)
                self.assertEqual(self.send.call_count, calls)
                # Registering this new preparation is required before it can
                # authorize any process; old success cannot supply that right.
                with self.assertRaises((ValueError, OSError)):
                    execution_binding(app.implementation, {'prepared_result': revised.data['prepared_result']},
                        identity='unregistered-run')
                self.assertEqual(app.workspace.artifact_provenance([str(output / 'package/worker.py')])[
                    str(output / 'package/worker.py')]['producer'], None)
                self.assertEqual((candidate / 'package/worker.py').read_text(), 'value = 7\n')
                self.assertFalse((candidate / 'package/extra.py').exists())

    def _registered_large_project(self, name, *, binary_bytes=600000):
        """Admit the original project through the public preparation consumer."""

        source = self.base / f'{name}-source'
        source.mkdir()
        (source / 'package').mkdir()
        (source / 'package/worker.py').write_bytes(b'value = 7\n')
        (source / 'support').mkdir()
        (source / 'support/helper.py').write_bytes(b'constant = 3\n')
        (source / 'assets').mkdir()
        with (source / 'assets/calibration.bin').open('wb') as stream:
            stream.truncate(binary_bytes)
        (source / 'assets/reference').write_bytes(b'unread original record\r\n' * 20000)
        (source / 'config').mkdir()
        (source / 'config/options').write_bytes(b'keep registered layout\r\n')
        for index in range(28):
            (source / f'support/settings-{index}.toml').write_bytes(f'index = {index}\r\n'.encode())
        app = self._create(name, sources={'code': source})
        inventory = app.workspace.bind_inventory(source.resolve(), identity='ordinary-admission')
        files = {Path(path).relative_to(source.resolve()).as_posix(): digest
            for path, digest in inventory['file_hashes'].items()}
        plan = {'argv': ['python', 'package/worker.py'],
            'outputs': ['observations/result.txt'], 'timeout_sec': 3}
        self.responses = [{'files': [], 'execution_plan': plan,
            'summary': 'Retain the complete project without edits', 'unresolved': []}]
        observed = self._prepare(app, {'goal': 'Retain the existing copied project',
            'project_path': 'code', 'complete_work': False})
        self.assertEqual(observed['status'], 'ok', observed)
        from simple_ar.tools.specs import ToolResult
        result = ToolResult.model_validate(observed)
        # Subsequent decision assertions measure feedback, not fixture admission;
        # the real seed call remains charged in the same ledger and journal.
        self.send.reset_mock()
        self.prompts.clear()
        return app, result, files

    def test_registered_project_copy_separates_complete_members_from_bounded_edits(self):
        import hashlib
        from simple_ar.code_task.generation.selection import ImplementationSources
        from simple_ar.code_task.generation.writer import _decode_implementation_response
        from simple_ar.code_task.workspace.copy import _materialize_selection
        from simple_ar.tools.implementation_run import candidate_source, execution_binding

        app, previous, files = self._registered_large_project('large-project')
        app.implementation.source_edit_contract = 'bound_content.v1'
        candidate = Path(previous.data['project_dir'])
        runtime = candidate / 'observations/result.txt'
        runtime.parent.mkdir()
        runtime.write_bytes(b'historical result\n')
        record = app.workspace.resolve(previous.data['prepared_result'])
        self.assertNotIn('source_edit_contract', json.loads((record.parent / 'request.json').read_bytes()))
        historical = {path: path.read_bytes() for path in
            (record, record.parent / 'request.json', record.parent.parent / 'native_result.json', runtime)}
        proposal = {'files': [{'path': 'package/worker.py', 'content': 'value = 8\n'}],
                'summary': 'Change the selected source', 'unresolved': [],
                'execution_plan': previous.data['execution_plan']}
        self.responses = [
            {'read_windows': [{'path': str(candidate / 'package/worker.py')}]},
            {**proposal, 'execution_plan': {**proposal['execution_plan'],
                'argv': ['python', str(candidate / 'package/worker.py')]}},
            {**proposal, 'execution_plan': {**proposal['execution_plan'],
                'argv': ['python', 'package/worker.py', '--asset=' + str(candidate / 'assets/reference')]}},
            proposal]
        with patch('simple_ar.code_task.workspace.copy._materialize_selection', wraps=_materialize_selection) as copier:
            prepared = self._prepare(app, {'goal': 'Change the selected source',
                'project_path': str(candidate / 'package'), 'complete_work': True})
        self.assertEqual(prepared['status'], 'ok', prepared)
        copier.assert_called_once()
        self.assertEqual(self.send.call_count, 4)
        self.assertIn('copied source member', json.dumps(self.prompts[2]['validation_observations']))
        self.assertIn('copied source member', json.dumps(self.prompts[3]['validation_observations']))
        output = Path(prepared['data']['project_dir'])
        self.assertGreater(len(prepared['data']['files']), 12)
        self.assertEqual((output / 'package/worker.py').read_bytes(), b'value = 8\n')
        for name in files.keys() - {'package/worker.py'}:
            self.assertEqual((output / name).read_bytes(), (candidate / name).read_bytes())
        self.assertFalse((output / 'observations/result.txt').exists())
        self.assertEqual({path: path.read_bytes() for path in historical}, historical)
        self.assertNotIn('unread original record', json.dumps(self.prompts))
        binding_path = Path(prepared['data']['source_binding'])
        frozen = binding_path.read_bytes()
        binding = json.loads(frozen)
        self.assertEqual(binding['generation_method'], 'professional_project.v1')
        self.assertEqual(binding['argv_semantics'], 'candidate_relative.v2')
        self.assertEqual(binding['source_edit_contract'], 'bound_content.v1')
        self.assertEqual(set(binding['original_files']), {'package/worker.py'})
        report = json.loads((binding_path.parent / 'candidate_validation.json').read_bytes())
        self.assertEqual(set(report['file_sha256']), set(files))
        self.assertIn('assets/calibration.bin', report['retained_uninspected_paths'])
        approval = execution_binding(app.implementation, {'prepared_result': prepared['data']['prepared_result']},
            identity='approve-large-project')
        self.assertEqual(approval['cwd'], str(output))
        self.assertEqual(approval['argv'][1:], ['package/worker.py'])
        self.assertEqual(approval['source_sha256'][str(output / 'assets/calibration.bin')],
            files['assets/calibration.bin'])
        self.assertEqual(approval['source_sha256'][str(candidate / 'assets/calibration.bin')],
            files['assets/calibration.bin'])
        # Inject a faulty retained proposal at the source boundary: approval
        # must apply the same identity contract even if generation was bypassed.
        current_record = app.workspace.resolve(prepared['data']['prepared_result'])
        payload = json.loads(current_record.read_text())
        payload['data']['execution_plan']['argv'] = ['python', str(candidate / 'package/worker.py')]
        with patch('simple_ar.tools.implementation_run.candidate_source', return_value=(output, payload, binding)):
            with self.assertRaisesRegex(ValueError, 'copied source member'):
                execution_binding(app.implementation, {'prepared_result': prepared['data']['prepared_result']},
                    identity='reject-old-command')
        self.assertFalse((app.session.store.root / 'tools/processes').exists())
        calls, ledger = self.send.call_count, app.ledger.to_dict()
        restored = ToolApplication.load(app.session.store.root, services=self.services)
        replay = restored.implementation.call('prepare_implementation', binding['request'], identity=binding_path.parents[1].name)
        self.assertEqual(replay.status, 'ok', replay)
        self.assertEqual(replay.data, prepared['data'])
        self.assertEqual(binding_path.read_bytes(), frozen)
        self.assertEqual(restored.ledger.to_dict(), ledger)
        self.assertEqual(self.send.call_count, calls)
        # Recovery keeps the producing contract even under a legacy gateway.
        self.assertEqual(json.loads(binding_path.read_bytes())['source_edit_contract'], 'bound_content.v1')
        original = binding['original_files']
        decode = dict(planned=None, original_files=original, legacy=False,
            artifact_dir=None, attempt=1, validate_destination=lambda _: None)
        for policy, row in (
                (None, proposal['files'][0]),
                ('bound_content.v1', {**proposal['files'][0], 'replacements': []}),
                ('unknown', proposal['files'][0])):
            with self.subTest(policy=policy, row=row), self.assertRaises(ValueError):
                _decode_implementation_response({'files': [row]}, edit_contract=policy, **decode)
        replacements, _ = _decode_implementation_response({'files': [
            {'path': 'package/worker.py', 'replacements': [
                {'old_text': 'value = 7', 'new_text': 'value = 8'}]}]},
            edit_contract='bound_content.v1', **decode)
        self.assertEqual(replacements['package/worker.py'], 'value = 8\n')
        # Unknown policies fail both accepted candidate and cached recovery,
        # without dispatch or silently adopting the gateway's current policy.
        binding_path.write_text(json.dumps({**binding, 'source_edit_contract': 'unknown'}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Unknown retained source edit contract'):
            candidate_source(restored.implementation, prepared['data']['prepared_result'])
        rejected = restored.implementation.call('prepare_implementation', binding['request'],
            identity=binding_path.parents[1].name)
        self.assertEqual(rejected.status, 'error', rejected)
        self.assertIn('Unknown retained source edit contract', rejected.error)
        binding_path.write_bytes(frozen)
        self.assertEqual(restored.ledger.to_dict(), ledger)
        self.assertEqual(self.send.call_count, calls)
        # A source owner cannot rebind stale supplied originals to newer bytes.
        sources = ImplementationSources(workspace=app.workspace, project=candidate,
            roots=[], windows=[], originals=dict(original), identity='stale-content',
            edit_contract='bound_content.v1')
        with self.assertRaisesRegex(ValueError, 'completely read'):
            unread = ImplementationSources(workspace=app.workspace, project=candidate,
                roots=[], windows=[], originals={}, identity='unread-content',
                edit_contract='bound_content.v1')
            unread.bind_response(proposal)
        (candidate / 'package/worker.py').write_bytes(b'value = 9\n')
        with self.assertRaisesRegex(ValueError, 'changed'):
            sources.bind_response(proposal)
        (candidate / 'package/worker.py').write_bytes(b'value = 7\n')
        (candidate / 'assets/calibration.bin').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Registered project inputs changed'):
            execution_binding(restored.implementation, {'prepared_result': prepared['data']['prepared_result']},
                identity='stale-project')
        self.assertEqual(hashlib.sha256((output / 'assets/calibration.bin').read_bytes()).hexdigest(),
            files['assets/calibration.bin'])

    def test_registered_project_plan_only_feedback_and_scope_rejections(self):
        from simple_ar.code_task.generation.selection import ImplementationSources

        app, previous, files = self._registered_large_project('large-plan')
        candidate = Path(previous.data['project_dir'])
        selected = candidate / 'package'
        retained = app.implementation._retained_candidate(selected, identity='large-scope', complete_project=True)
        sources = ImplementationSources(workspace=app.workspace, project=candidate, editable_project=selected,
            roots=[], windows=[], originals={}, identity='large-scope', retained=True, registered_project=retained)
        for response in (
                {'files': [{'path': 'support/helper.py', 'replacements': []}]},
                {'files': [{'path': 'outside/new.py', 'content': 'value = 1\n'}]},
                {'files': [{'path': 'package', 'content': 'cannot replace a directory'}]},
                {'files': [], 'execution_plan': {**previous.data['execution_plan'],
                    'outputs': ['assets/calibration.bin']}},
                {'files': [{'path': 'package/worker.py', 'content': 'value = 99\n'}],
                    'execution_plan': {**previous.data['execution_plan'], 'outputs': ['assets/calibration.bin']}}):
            # Source binding enforces scope/conflicts; whole-response decoding
            # below also rejects a new-content rewrite and retained output collision.
            from simple_ar.code_task.generation.writer import _decode_implementation_response
            with self.subTest(response=response), self.assertRaises(ValueError):
                originals, copies = sources.bind_response(response, commit=False)
                _decode_implementation_response(response, planned=None, original_files=originals,
                    legacy=False, artifact_dir=None, attempt=1, preserved_files=copies,
                    registered_paths=list(files), validate_destination=lambda name:
                        app.implementation._feedback_destination(app.workspace.workspace / 'scope-target', name, retained))
        self.assertEqual(sources.originals, {})
        for complete in (False, True):
            self.responses = [{'files': [], 'summary': 'Only update the execution plan', 'unresolved': [],
                'execution_plan': previous.data['execution_plan']}]
            result = app.implementation.call('prepare_implementation', {'goal': 'Plan-only correction',
                'project_path': str(selected), 'complete_work': complete,
                'execution_requested': True}, identity=f'large-plan-{complete}')
            self.assertEqual(result.status, 'ok', result)
            output = Path(result.data['project_dir'])
            self.assertEqual({row['path']: row['sha256'] for row in result.data['files']}, files)
            self.assertEqual((output / 'assets/reference').read_bytes(), (candidate / 'assets/reference').read_bytes())
        self.responses = [{'files': [{'path': f'package/new-{index}.py', 'content': f'value = {index}\n'}
            for index in range(12)], 'summary': 'A bounded additional edit unit', 'unresolved': []}]
        expanded = app.implementation.call('prepare_implementation', {'goal': 'Add the bounded source unit',
            'project_path': str(selected)}, identity='large-additions')
        self.assertEqual(expanded.status, 'ok', expanded)
        self.assertEqual(len(expanded.data['files']), len(files) + 12)
        with self.assertRaisesRegex(ValueError, '12 source files'):
            sources.bind_response({'files': [{'path': f'package/extra-{index}.py', 'content': 'value = 1\n'}
                for index in range(13)]}, commit=False)

    def test_registered_project_copy_failure_preserves_prior_and_cannot_replay(self):
        from simple_ar.code_task.workspace.copy import _materialize_selection

        app, previous, _ = self._registered_large_project('copy-failure')
        candidate = Path(previous.data['project_dir'])
        original = (candidate / 'assets/calibration.bin').read_bytes()
        self.responses = [{'files': [], 'summary': 'Retain the project', 'unresolved': [],
            'execution_plan': previous.data['execution_plan']}]

        def corrupt_copy(selection):
            report = _materialize_selection(selection)
            (selection.workspace / 'assets/calibration.bin').write_bytes(b'incomplete copy')
            return report

        arguments = {'goal': 'Retain the project', 'project_path': str(candidate)}
        with patch('simple_ar.code_task.workspace.copy._materialize_selection', side_effect=corrupt_copy):
            failed = app.implementation.call('prepare_implementation', arguments, identity='incomplete-copy')
        self.assertEqual(failed.status, 'error', failed)
        self.assertIn('copy omitted or changed members', failed.error)
        self.assertFalse((app.workspace.workspace / 'implementations/incomplete-copy').exists())
        self.assertEqual((candidate / 'assets/calibration.bin').read_bytes(), original)
        request = app.session.store.root / 'attempts/incomplete-copy/implementation/request.json'
        frozen, calls = request.read_bytes(), self.send.call_count
        replay = app.implementation.call('prepare_implementation', json.loads(frozen)['request'],
            identity='incomplete-copy')
        self.assertEqual(replay.status, 'error', replay)
        self.assertIn('Interrupted professional decision', replay.error)
        self.assertEqual(request.read_bytes(), frozen)
        self.assertEqual(self.send.call_count, calls)

    def test_subtree_admission_rejects_outside_edits_and_new_dependencies(self):
        from simple_ar.code_task.generation.selection import ImplementationSources

        app, result, _ = self._registered_subtree('scope')
        candidate = Path(result['data']['project_dir'])
        selected = candidate / 'package'
        retained = app.implementation._retained_candidate(selected, identity='scope-probe')
        sources = ImplementationSources(workspace=app.workspace, project=candidate,
            editable_project=selected, roots=['materials'], windows=[], originals=retained['original_files'],
            identity='scope-probe', retained=True, preserved_files=retained['preserved_files'])
        for response in (
                {'files': [{'path': 'support/helper.py', 'replacements': []}]},
                {'files': [{'path': 'elsewhere/new.py', 'content': 'value = 1\n'}]},
                {'files': [], 'preserve_files': {'materials/notes.txt': 'elsewhere/notes.txt'}}):
            with self.subTest(response=response), self.assertRaisesRegex(ValueError, 'selected editable subtree'):
                sources.bind_response(response, commit=False)
        with self.assertRaisesRegex(ValueError, 'selected editable subtree'):
            app.implementation._feedback_destination(app.workspace.workspace / 'new', 'elsewhere/new.py', retained)
        (candidate / 'support/helper.py').write_text('constant = 4\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Preserved input changed'):
            app.implementation._retained_candidate(selected, identity='changed-dependency')

    def test_subtree_reuse_requires_registered_source_membership(self):
        from simple_ar.code_task.generation.selection import ImplementationSources

        app, result, _ = self._registered_subtree('membership')
        candidate = Path(result['data']['project_dir'])
        root = app.implementation._retained_candidate(candidate, identity='whole-unit')
        self.assertEqual(set(root['original_files']), {'package/worker.py', 'support/helper.py'})
        self.assertNotIn('editable_project', root)
        foreign = app.workspace.workspace / 'unregistered' / candidate.name / 'package'
        foreign.mkdir(parents=True)
        (foreign / 'worker.py').write_bytes((candidate / 'package/worker.py').read_bytes())
        self.assertIsNone(app.implementation._retained_candidate(foreign, identity='foreign'))
        outputs = candidate / 'observations'
        outputs.mkdir()
        (outputs / 'result.txt').write_text('Historical output', encoding='utf-8')
        self.assertIsNone(app.implementation._retained_candidate(outputs, identity='runtime-only'))
        (candidate / 'unlisted.py').write_text('unlisted = True\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'unlisted members'):
            app.implementation._retained_candidate(candidate / 'package', identity='unlisted')
        # Whole registered-project reuse distinguishes accepted sources from
        # directory contents. Residual files stay available for investigation,
        # but cannot silently acquire edit/copy or successful-output authority.
        retained = app.implementation._retained_candidate(candidate / 'package',
            identity='registered-with-residuals', complete_project=True)
        sources = ImplementationSources(workspace=app.workspace, project=candidate,
            editable_project=candidate / 'package', roots=[], windows=[],
            originals=retained['original_files'], identity='residual-observation', retained=True,
            preserved_files=retained.get('preserved_files'), registered_project=retained, diagnostic_context=True)
        self.assertNotIn('unlisted.py', sources.project_files)
        self.assertNotIn('observations/result.txt', sources.registered_files)
        observed = sources.read([{'path': str(candidate / 'unlisted.py'), 'max_bytes': 1000}])
        self.assertIn('unlisted = True', observed['source_windows'][-1]['text'])
        with self.assertRaisesRegex(ValueError, 'selected editable subtree'):
            sources.bind_response({'files': [{'path': 'unlisted.py', 'content': 'changed = True\n'}]}, commit=False)
        self.responses = [{'files': [], 'summary': 'Retain registered sources only', 'unresolved': [],
            'execution_plan': result['data']['execution_plan']}]
        reused = app.implementation.call('prepare_implementation', {
            'goal': 'Retain registered sources only', 'project_path': str(candidate / 'package')},
            identity='registered-source-reuse')
        self.assertEqual(reused.status, 'ok', reused)
        copied = Path(reused.data['project_dir'])
        self.assertFalse((copied / 'unlisted.py').exists())
        self.assertFalse((copied / 'observations').exists())
        self.assertTrue((candidate / 'unlisted.py').exists())
        self.assertTrue((candidate / 'observations/result.txt').exists())
        (candidate / 'unlisted.py').write_text('changed_during_work = True\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'inventory changed|inputs changed'):
            sources.validate()

    def test_subtree_reuse_rejects_changed_accepted_receipt_without_investigation(self):
        app, result, _ = self._registered_subtree('receipt')
        candidate = Path(result['data']['project_dir'])
        record = app.workspace.resolve(result['data']['prepared_result'])
        payload = json.loads(record.read_text())
        payload['data']['summary'] = 'Changed after registration'
        record.write_text(json.dumps(payload), encoding='utf-8')
        calls = self.send.call_count
        rejected = app.implementation.call('prepare_implementation', {
            'goal': 'Continue selected sources', 'project_path': str(candidate / 'package'),
            'complete_work': True}, identity='changed-receipt')
        self.assertEqual(rejected.status, 'error', rejected)
        self.assertIn('accepted native observation', rejected.error)
        self.assertEqual(self.send.call_count, calls)

    def test_admitted_parent_window_can_implement_in_one_call_without_finalizing_selection(self):
        inputs = self.base / 'inputs'
        inputs.mkdir()
        text = 'The requested output is a text file derived by the script.\n'
        (inputs / 'notes.txt').write_text(text, encoding='utf-8')
        self.responses = [self._candidate("from pathlib import Path\nPath('result.txt').write_text('ready')\n")]
        app = self._create(sources={'inputs': inputs})
        result = self._prepare(app, {'goal': 'Implement the supplied instruction',
            'complete_work': True, 'material_roots': ['inputs']}, preceding=[
                {'tool_name': 'read_file', 'arguments': {'path': 'inputs/notes.txt', 'full_digest': True}}])
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(sum(row.get('text') == text for row in self.prompts[0]['source_windows']), 1)
        self.assertTrue(self.prompts[0]['investigation']['journal_coverage']['admitted'])

    def test_optional_inheritance_and_journal_share_capacity_beyond_previous_limit(self):
        from copy import deepcopy
        from simple_ar.code_task.generation.selection import ImplementationSources
        from simple_ar.tools.materials import read_material_window

        root = self.base / 'working-materials'
        root.mkdir()
        app = self._create(sources={'inputs': root})
        references = []
        for index in range(30):
            path = root / f'notes-{index}.txt'
            path.write_text(str(index % 10) * 4096, encoding='utf-8')
            references.append(read_material_window(app.workspace,
                {'path': str(path), 'max_bytes': 4096, 'full_digest': True}, identity='parent'))
        frozen = deepcopy(references)
        required = {'request': references[0]['request'], **references[0]['data'], 'role': 'current_request'}
        current_context = {'text': 'Current user goal and accepted feedback. ' * 80,
            'role': 'user_request_and_accepted_feedback_not_source_or_authority'}
        durable = [required, current_context]
        fresh = root / 'fresh.txt'
        fresh.write_text('z' * 64000, encoding='utf-8')
        with patch.object(app.workspace, 'call', wraps=app.workspace.call) as reads:
            sources = ImplementationSources(workspace=app.workspace, project=None, roots=['inputs'],
                windows=durable, originals={}, identity='bounded-reuse', diagnostic_context=True,
                inherited_observations=references * 20, material_observations=lambda roots: references * 20)
        self.assertEqual(references, frozen)
        self.assertGreater(sum(len(row['data']['text']) for row in references), 96000)
        self.assertLessEqual(sources.context()['observation_characters'], 32000)
        self.assertLessEqual(reads.call_count, 7)
        self.assertTrue(sources.seed_coverage['not_admitted'])
        reference_coverage = sources.seed_coverage['reference_coverage']
        self.assertEqual(reference_coverage['inherited_available'], 600)
        self.assertEqual(reference_coverage['journal_available'], 600)
        self.assertEqual(reference_coverage['inherited_omitted'], 536)
        self.assertEqual(reference_coverage['journal_omitted'], 536)
        self.assertLessEqual(sum(len(sources.seed_coverage[key])
            for key in ('admitted', 'not_admitted')), 128)
        self.assertTrue(any(row['reason'] == 'observation_capacity'
            for row in sources.seed_coverage['not_admitted']))
        refs = sources.context()['working_set']['required_refs']
        self.assertEqual(refs, [sources.windows[0]['observation_ref']])
        before = deepcopy(durable)
        sources.focus([])
        self.assertEqual(len(sources.windows), 2)
        self.assertEqual(durable, before)
        self.assertEqual(sources.windows[0]['text'], required['text'])
        self.assertIn(current_context, sources.windows)
        sources.read([{'path': str(fresh), 'max_bytes': 64000}])
        self.assertLessEqual(sources.context()['observation_characters'], 96000)
        for invalid in (['observation-999999'], [refs[0], refs[0]], [{'sha256': '0' * 64}], 'all'):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'owner-issued'):
                sources.focus(invalid)
        sources.validate()

    def test_required_windows_leave_only_remaining_capacity_for_optional_seed(self):
        from simple_ar.code_task.generation.selection import ImplementationSources
        from simple_ar.tools.materials import read_material_window

        root = self.base / 'required-materials'
        root.mkdir()
        path = root / 'required.txt'
        path.write_text('x' * 95792, encoding='utf-8')
        optional = root / 'optional.txt'
        optional.write_text('y' * 4096, encoding='utf-8')
        app = self._create(sources={'inputs': root})
        windows = []
        for offset, count in ((0, 48000), (48000, 47792)):
            row = read_material_window(app.workspace, {'path': str(path), 'offset_bytes': offset,
                'max_bytes': count, 'full_digest': True}, identity='required')
            windows.append({'request': row['request'], **row['data'], 'role': 'current_request'})
        reference = read_material_window(app.workspace, {'path': str(optional), 'full_digest': True}, identity='parent')
        with patch.object(app.workspace, 'call', wraps=app.workspace.call) as reads:
            sources = ImplementationSources(workspace=app.workspace, project=None, roots=['inputs'],
                windows=windows, originals={}, identity='required-capacity', diagnostic_context=True,
                material_observations=lambda roots: [reference])
        # Inventory discovery is required; optional text must not be reread
        # after required current windows have exhausted its shared allowance.
        self.assertFalse(any(call.args[0] == 'read_file' for call in reads.call_args_list))
        self.assertEqual(sources.context()['observation_characters'], 95792)
        sources.focus([])
        self.assertEqual(len(sources.windows), 2)
        self.assertEqual(len(windows), 2)
        self.assertEqual(sources.seed_coverage['not_admitted'][0]['reason'], 'observation_capacity')

    def test_whole_utf8_overlap_and_capacity_refusals_keep_actual_read_guards(self):
        from copy import deepcopy
        from simple_ar.code_task.generation.selection import ImplementationSources

        root = self.base / 'unicode-materials'
        root.mkdir()
        path = root / 'notes.txt'
        path.write_bytes(('\u6c49' * 40000).encode('utf-8'))
        other = root / 'other.txt'
        other.write_text('x' * 64000, encoding='utf-8')
        app = self._create(sources={'inputs': root})
        durable = []
        sources = ImplementationSources(workspace=app.workspace, project=None, roots=['inputs'],
            windows=durable, originals={}, identity='whole-utf8', diagnostic_context=True)
        sources.read([{'path': str(path), 'offset_bytes': 1, 'max_bytes': 64000}])
        first = deepcopy(durable[0])
        self.assertIn('\ufffd', first['text'])
        sources.read([{'path': str(path), 'offset_bytes': 30001, 'max_bytes': 64000}])
        self.assertEqual(len(sources.windows), 2, 'Partial UTF-8 overlap must not splice decoded text')
        self.assertEqual(durable[0], first)
        with self.assertRaisesRegex(ValueError, 'capacity'):
            sources.read([{'path': str(other), 'max_bytes': 64000}])
        self.assertEqual(len(durable), 3, 'A successful read refused presentation still owns guards')
        sources.focus([])
        self.assertEqual(sources.windows, [])
        self.assertEqual(durable[0], first)
        # Reversible complete containment replaces only presentation, and is
        # real progress even when the active row count stays one.
        sources.read([{'path': str(path), 'offset_bytes': 0, 'max_bytes': 300}])
        sources.read([{'path': str(path), 'offset_bytes': 0, 'max_bytes': 600}])
        self.assertEqual(len(sources.windows), 1)
        self.assertEqual(sources.windows[0]['text'], '\u6c49' * 200)
        self.assertEqual(durable[0], first)
        other.write_text('y' * 64000, encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'inputs changed'):
            sources.validate()

    def test_focus_feedback_persists_retired_guards_and_reuses_large_prior_on_demand(self):
        from simple_ar.tools.implementation_run import prepared_unit, execution_binding

        root = self.base / 'large-materials'
        root.mkdir()
        paths = [root / name for name in ('first.txt', 'second.txt')]
        for index, path in enumerate(paths):
            path.write_text(str(index) * 60000, encoding='utf-8')
        app = self._create(sources={'inputs': root})
        self.responses = [
            {'read_windows': [{'path': str(paths[0]), 'max_bytes': 60000}]},
            {'focus_observations': [], 'read_windows': [{'path': str(paths[1]), 'max_bytes': 60000}]},
            self._candidate("from pathlib import Path\nPath('result.txt').write_text('ready')\n")]
        arguments = {'goal': 'Save a bounded result', 'material_roots': ['inputs'],
            'complete_work': True, 'producer_completion': 'goal_findings.v1'}
        result = self._prepare(app, arguments)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(self.send.call_count, 3)
        record = app.workspace.resolve(result['data']['prepared_result'])
        binding_path = record.parent / 'request.json'
        original_binding = binding_path.read_bytes()
        binding = json.loads(original_binding)
        self.assertEqual(sum(len(row.get('text', '')) for row in binding['source_windows']), 120000)
        self.assertNotIn('0' * 60000, json.dumps(self.prompts[2]))
        self.assertIn('1' * 60000, json.dumps(self.prompts[2]))
        approved, _, _ = prepared_unit(app.implementation, result['data']['prepared_result'])
        for path in paths:
            self.assertIn(str(path), approved['source_sha256'])
            self.assertIn(str(path), approved['context_sha256'])
        # A later professional preparation does not replay 120000 characters.
        self.responses = [{'files': [], 'summary': 'Retain current source', 'unresolved': [],
            'execution_plan': result['data']['execution_plan']}]
        reused = app.implementation.call('prepare_implementation', {**arguments,
            'project_path': result['data']['project_dir']}, identity='large-feedback')
        self.assertEqual(reused.status, 'ok', reused)
        self.assertEqual(self.send.call_count, 4)
        self.assertLessEqual(self.prompts[-1]['investigation']['observation_characters'], 32000)
        self.assertEqual(binding_path.read_bytes(), original_binding)
        # Retired context is still checked at approval and cached recovery.
        paths[0].write_text('changed retired source', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'changed before execution'):
            execution_binding(app.implementation, {'prepared_result': result['data']['prepared_result']},
                identity='retired-approval')
        replay = app.implementation.call('prepare_implementation', binding['request'],
            identity=record.parent.parent.name)
        self.assertEqual(replay.status, 'error', replay)
        self.assertIn('source inputs changed', replay.error)
        self.assertEqual(self.send.call_count, 4)

    def test_invalid_optional_inheritance_is_reported_and_legacy_focus_is_refused(self):
        from simple_ar.code_task.generation.selection import ImplementationSources
        from simple_ar.tools.materials import read_material_window

        root = self.base / 'changed-materials'
        root.mkdir()
        path = root / 'notes.txt'
        path.write_text('previous bytes', encoding='utf-8')
        app = self._create(sources={'inputs': root})
        previous = read_material_window(app.workspace, {'path': str(path), 'full_digest': True}, identity='parent')
        path.write_text('current bytes!', encoding='utf-8')
        durable = []
        sources = ImplementationSources(workspace=app.workspace, project=None, roots=['inputs'],
            windows=durable, originals={}, identity='changed-parent', diagnostic_context=True,
            inherited_observations=[previous, {'request': {'path': str(path), 'max_bytes': 0}, 'data': {}}])
        self.assertEqual(sources.windows, [])
        self.assertEqual(len(sources.seed_coverage['not_admitted']), 2)
        self.assertEqual(durable[0]['text'], 'current bytes!')
        sources.validate()
        legacy = ImplementationSources(workspace=app.workspace, project=None, roots=['inputs'],
            windows=[], originals={}, identity='legacy')
        self.assertNotIn('working_set', legacy.context())
        with self.assertRaisesRegex(ValueError, 'historical preparation'):
            legacy.focus([])

    def test_professional_diagnoses_share_the_same_six_decisions(self):
        for name, count, succeeds in (('corrected', 2, True), ('bounded', 6, False),
                ('contract-corrected', 4, True), ('contract-bounded', 6, False),
                ('read-corrected', 4, True), ('read-bounded', 6, False), ('historical-contract', 2, False)):
            with self.subTest(name=name):
                start = self.send.call_count
                broken = self._candidate('def broken(:\n    pass\n')
                if 'contract' in name:
                    broken = {**self._candidate('value = 7\n'), 'preserve_files': {'unavailable': []}}
                elif name.startswith('read'):
                    broken = {'read_windows': [{'path': 'unavailable/input.txt'}]}
                self.responses = [broken] * (count - int(succeeds))
                if succeeds:
                    self.responses.append(self._candidate("from pathlib import Path\nPath('result.txt').write_text('ready')\n"))
                app = self._create(name)
                arguments = {'goal': 'Save a syntactically valid script', 'complete_work': True}
                if name == 'historical-contract':
                    arguments.update(complete_work=False, files=[{'path': 'calculate.py', 'purpose': 'Exact source-only request'}])
                result = self._prepare(app, arguments)
                self.assertEqual(result['status'], 'ok' if succeeds else 'error', result)
                self.assertEqual(self.send.call_count - start, count)
                if name == 'historical-contract':
                    self.assertTrue(all('investigation' not in prompt for prompt in self.prompts[start:]))
                    continue
                for index, prompt in enumerate(self.prompts[start:]):
                    self.assertEqual(len(prompt['validation_observations']), index)
                    self.assertEqual(prompt['investigation']['decisions_remaining'], 6 - index)
                store = app.session.store.root / Path(app.state['turns'][-1]['tools'][-1]['result']).parent / 'implementation'
                diagnosis = json.loads((store / 'decision-steps/1/validation.json').read_text())
                if name.startswith(('contract', 'read')):
                    self.assertEqual(diagnosis['kind'], 'professional_contract_failed')
                    self.assertEqual(diagnosis['operation'], 'read_windows' if name.startswith('read') else 'candidate')
                else:
                    self.assertEqual(diagnosis['kind'], 'python_syntax_failed')
                    self.assertEqual(diagnosis['filename'], 'calculate.py')
                last = json.loads((store / f'decision-steps/{count - int(succeeds)}/validation.json').read_text())
                self.assertEqual(last['stop_reason'], None if succeeds else 'decision_ceiling')
                self.assertEqual((store / 'result.json').exists(), succeeds)
                self.assertFalse((app.session.store.root / 'tools/processes').exists())

    def test_budget_authorization_resumes_only_undispatched_preparation(self):
        from simple_ar.core.budget import BudgetExceededError

        for paid in (False, True):
            with self.subTest(prior_physical_work=paid):
                start = self.send.call_count
                inputs = self.base / f'budget-inputs-{paid}'
                inputs.mkdir()
                (inputs / 'note.txt').write_text('Save a bounded text result.\n', encoding='utf-8')
                app = self._create(f'budget-{paid}', sources={'inputs': inputs})
                arguments = {'goal': 'Save a bounded text result', 'complete_work': True,
                    'material_roots': ['inputs']}
                self.responses = ([{'read_windows': [{'path': 'inputs/note.txt'}]}] if paid else [])
                reserve, count = app.ledger.reserve, 0

                def reserve_until_refused(*args, **kwargs):
                    nonlocal count
                    count += 1
                    if count == (2 if paid else 1):
                        raise BudgetExceededError('offline finite reservation refusal')
                    return reserve(*args, **kwargs)

                with patch.object(app.ledger, 'reserve', side_effect=reserve_until_refused):
                    failed = self._prepare(app, arguments)
                self.assertEqual(failed['status'], 'error', failed)
                self.assertEqual(failed['data']['owner_stop_reason'], 'shared_budget_exhausted')
                self.assertEqual(failed['data']['budget_refusal']['physical_activity'], paid)
                self.assertEqual(self.send.call_count - start, int(paid))
                app.run(progress=lambda _: None)
                stop_path = app.session.store.resolve(app.state['turns'][-1]['reply'])
                original_stop = stop_path.read_bytes()
                source_path = app.session.store.resolve(json.loads(original_stop)['source_result'])
                original_source = source_path.read_bytes()
                request_path = source_path.parent / 'implementation/request.json'
                original_request = request_path.read_bytes()
                self.responses = ([] if paid else [self._candidate(
                    "from pathlib import Path\nPath('result.txt').write_text('ready')\n")])
                app = ToolApplication.load(app.session.store.root, services=self.services)
                grant = {'resource_allowances': {'total_tokens': 600000},
                    'authorization_id': 'explicit-budget-resume',
                    'authorization_reason': 'Resume the original owner under its saved contract'}
                status = app.run(progress=lambda _: None, **grant)
                self.assertEqual(status['status'], 'paused', status)
                if paid:
                    self.assertIsNone(app.state['pending'])
                    stopped = app.session.store.read_json(app.state['turns'][-1]['reply'])['owner_stop']
                    self.assertEqual(stopped['reason'], 'preparation_restart_requires_action')
                    self.assertEqual(stopped['requested_action'], {'tool_name': 'prepare_implementation',
                        'arguments': json.loads(original_request)['request']})
                else:
                    pending = app.state['pending']
                    self.assertEqual(pending['call']['function']['name'], 'execute_implementation')
                    prepared = self._observations(app)[-1]
                    new_request = app.workspace.resolve(prepared['data']['prepared_result']).parent / 'request.json'
                    self.assertNotEqual(new_request, request_path)
                    self.assertEqual(json.loads(new_request.read_bytes())['request'],
                        json.loads(original_request)['request'])
                self.assertEqual(stop_path.read_bytes(), original_stop)
                self.assertEqual(source_path.read_bytes(), original_source)
                self.assertEqual(request_path.read_bytes(), original_request)
                ledger, turns = app.ledger.to_dict(), len(app.state['turns'])
                app.run(progress=lambda _: None, **grant)  # Same authorization never reopens/refills work.
                self.assertEqual(app.ledger.to_dict(), ledger)
                self.assertEqual(len(app.state['turns']), turns)
                self.assertEqual(self.send.call_count - start, 1)
                self.assertFalse((app.session.store.root / 'tools/processes').exists())
                self.assertTrue(all(row.status in {'settled', 'released'} for row in app.ledger.entries))
                self.coordinator.assert_not_called()

    def test_incomplete_transport_and_truncation_are_not_replayed(self):
        from contextlib import nullcontext
        for name, response in (('truncated', ('length', '{"files":[')),
                               ('transport', RuntimeError('offline interrupted transport fixture')),
                               ('unknown', self._candidate('value = 7\n'))):
            with self.subTest(name=name):
                start = self.send.call_count
                self.responses = [response]
                app = self._create(name)
                arguments = {'goal': 'Retain failed generation without replay', 'complete_work': True}
                guard = (patch('simple_ar.code_task.generation.writer.response_accounting_known', return_value=False)
                    if name == 'unknown' else nullcontext())
                with guard:
                    result = self._prepare(app, arguments)
                self.assertEqual(result['status'], 'error', result)
                identity = Path(app.state['turns'][-1]['tools'][-1]['result']).parent.name
                ledger = app.ledger.to_dict()
                restored = ToolApplication.load(app.session.store.root, services=self.services)
                request_path = app.session.store.root / 'attempts' / identity / 'implementation/request.json'
                frozen_request = request_path.read_bytes()
                repeated = restored.implementation.call('prepare_implementation',
                    json.loads(frozen_request)['request'], identity=identity)
                self.assertEqual(repeated.status, 'error')
                self.assertIn('no blind replay', repeated.error)
                self.assertEqual(request_path.read_bytes(), frozen_request)
                self.assertEqual(restored.ledger.to_dict(), ledger)
                self.assertEqual(self.send.call_count - start, 1)

    def test_professional_plan_uses_literal_candidate_paths_through_physical_execution(self):
        material = self.base / 'input'
        material.mkdir()
        (material / 'note.txt').write_text('Authorized source, not an executable', encoding='utf-8')
        for prefix, semantics in (('workspace', 'candidate_relative.v2'), ('materials', 'candidate_relative.v1')):
            with self.subTest(prefix=prefix):
                path = prefix + '/nested runner.py'
                self.responses = [{'summary': 'Create a nested executable', 'unresolved': [],
                    'files': [{'path': path, 'content': "from pathlib import Path\nPath('result.txt').write_text('7')\n"}],
                    'execution_plan': {'argv': ['python', path], 'outputs': ['result.txt'], 'timeout_sec': 3}}]
                app = self._create('literal-' + prefix, sources={'materials': material})
                result = self._prepare(app, {'goal': 'Save a value from a nested entry point', 'complete_work': True})
                self.assertEqual(result['status'], 'ok', result)
                source = json.loads(Path(result['data']['source_binding']).read_text())
                self.assertEqual(source['argv_semantics'], 'candidate_relative.v2')
                if semantics == 'candidate_relative.v1':
                    # Reconstruct a historical literal-argv preparation before
                    # approval, while the other existing case exercises v2.
                    source['argv_semantics'] = semantics
                    Path(result['data']['source_binding']).write_text(json.dumps(source), encoding='utf-8')
                app.run(actions=[{'tool_name': 'execute_implementation', 'arguments': {
                    'prepared_result': result['data']['prepared_result']}}], progress=lambda _: None)
                binding = app._owner_execution_binding(app.state['pending']['call'])
                self.assertEqual(binding['argv_semantics'], semantics)
                self.assertEqual(binding['argv'][1], path)
                self.assertEqual(Path(binding['cwd']), Path(result['data']['project_dir']))
                token = app.approval_token(app.state['pending']['call'])
                with patch.object(app.workspace, 'call', wraps=app.workspace.call) as calls:
                    app.run(approved_execution=token, progress=lambda _: None,
                        should_stop=lambda: any(row['tool_name'] == 'execute_implementation'
                            for row in self._observations(app)))
                executed = self._observations(app)[-1]
                self.assertEqual(executed['status'], 'ok', executed)
                self.assertEqual(executed['data']['status'], 'run_succeeded')
                self.assertEqual((Path(binding['cwd']) / 'result.txt').read_text(), '7')
                physical = [row for row in calls.call_args_list if row.args[0] == 'run_process']
                self.assertEqual(len(physical), 1)
                self.assertTrue(physical[0].args[1]['literal_argv'])
                ledger = app.ledger.to_dict()
                restored = ToolApplication.load(app.session.store.root, services=self.services)
                self.assertEqual(restored.ledger.to_dict(), ledger)
                self.assertEqual(self._observations(restored)[-1], executed)
        self.coordinator.assert_not_called()

    def test_explicit_code_only_owner_keeps_unmarked_generation_on_reload(self):
        import hashlib
        from simple_ar.core.capabilities import ArtifactStore
        from simple_ar.tools.implementation_run import execution_binding
        self.responses = [self._candidate("from pathlib import Path\nPath('result.txt').write_text('7')\n")]
        app = self._create()
        # Freeze a historical task before preparation: this fixture directly
        # replays unmarked owner arguments and reconstructs pre-literal-argv
        # approvals below. It must not first create a new completion contract.
        app.state['config'].pop('producer_completion')
        app._save()
        app = ToolApplication.load(app.session.store.root, services=self.services)
        arguments = {'goal': 'Source only', 'complete_work': False,
            'files': [{'path': 'calculate.py', 'purpose': 'A code-only entry'}]}
        result = self._prepare(app, arguments)
        self.assertEqual(result['status'], 'ok', result)
        store = Path(result['data']['source_binding']).parent
        binding = json.loads((store / 'request.json').read_text())
        self.assertNotEqual(binding.get('generation_method'), 'professional_decision.v1')
        self.assertFalse((store / 'decision-steps').exists())
        self.assertNotIn('investigation', self.prompts[0])
        restored = ToolApplication.load(app.session.store.root, services=self.services)
        repeated = restored.implementation.call('prepare_implementation', arguments, identity=store.parent.name)
        self.assertEqual(repeated.data, result['data'])
        self.assertEqual(self.send.call_count, 1)
        # Reconstruct a pre-literal-argv preparation, not merely a newly
        # generated request using the current semantics. Its old approval and
        # physical process contract must remain unchanged on reload.
        binding.pop('argv_semantics')
        (store / 'request.json').write_text(json.dumps(binding), encoding='utf-8')
        restored = ToolApplication.load(app.session.store.root, services=self.services)
        repeated = restored.implementation.call('prepare_implementation', arguments, identity=store.parent.name)
        self.assertEqual(repeated.data, result['data'])
        self.assertEqual(self.send.call_count, 1)
        run_arguments = {'prepared_result': result['data']['prepared_result']}
        restored.run(actions=[{'tool_name': 'execute_implementation', 'arguments': run_arguments}],
            progress=lambda _: None)
        pending = restored.state['pending']
        self.assertEqual(pending['call']['function']['name'], 'execute_implementation')
        identity = pending['attempt']
        legacy = execution_binding(restored.implementation, run_arguments, identity=identity)
        legacy.pop('process_artifacts')
        # Reconstruct the original exact approval shape, then reload before the
        # owner has written execution_input.
        approval = {'binding': legacy, 'token': hashlib.sha256(json.dumps({
            'call': pending['call'], 'inputs': legacy}, sort_keys=True).encode()).hexdigest()}
        pending['execution_approval'] = approval
        restored._save()
        restored = ToolApplication.load(restored.session.store.root, services=self.services)
        execution = ArtifactStore(restored.session.store.root / 'attempts' / identity)
        self.assertFalse(execution.exists('execution_input.json'))
        self.assertFalse(restored.workspace.has_started(identity))
        # The current dispatcher must recompute the old exact binding/token;
        # no approval or owner method is mocked for this compatibility check.
        self.assertEqual(restored._owner_execution_binding(restored.state['pending']['call']), legacy)
        self.assertEqual(restored.approval_token(restored.state['pending']['call']), approval['token'])
        with patch.object(restored.workspace, 'call', wraps=restored.workspace.call) as calls:
            restored.run(approved_execution=approval['token'], progress=lambda _: None,
                should_stop=lambda: any(row['tool_name'] == 'execute_implementation'
                    for row in self._observations(restored)))
        executed = self._observations(restored)[-1]
        self.assertEqual(executed['tool_name'], 'execute_implementation')
        physical = [call for call in calls.call_args_list if call.args[0] == 'run_process']
        self.assertEqual(len(physical), 1)
        self.assertEqual(set(physical[0].args[1]), {'argv', 'cwd', 'timeout_sec'})
        self.assertEqual(executed['status'], 'ok', executed)
        self.assertEqual(executed['data']['status'], 'run_succeeded')
        self.assertEqual(execution.read_json('execution_input.json'), approval)
        self.assertEqual(execution.read_json('candidate_execution.json')['approval'], approval)
        receipt = restored.session.store.read_json(executed['data']['receipt'])
        self.assertNotIn('input_binding', receipt)
        self.assertNotIn('output_binding', receipt)
        output = str(Path(result['data']['project_dir']) / 'result.txt')
        self.assertEqual(Path(output).read_text(), '7')
        self.assertIsNone(restored.workspace.artifact_provenance([output])[output]['producer'])
        self.assertIsNone(restored.state['pending'])
        self.assertEqual(restored.state['turns'][-1]['remaining'], [])
        accepted_ref = restored.state['turns'][-1]['tools'][-1]['result']
        self.assertEqual(restored.session.store.read_json(accepted_ref), executed)
        self.assertEqual(execution.read_json('native_result.json'), executed)
        self.assertEqual(execution.read_json('capability_result.json')['status'], 'completed')
        ledger, raw = restored.ledger.to_dict(), execution.resolve('candidate_execution.json').read_bytes()
        history = json.loads(json.dumps(restored.state['turns']))
        restored = ToolApplication.load(restored.session.store.root, services=self.services)
        self.assertEqual(restored.state['turns'], history)
        self.assertEqual(self._observations(restored)[-1], executed)
        # Stop at the consumed-action boundary: loading accepted history must
        # neither invent another action nor rerun the physical attempt.
        with patch.object(restored.workspace, 'call',
                side_effect=AssertionError('Accepted history must not rerun a workspace tool')):
            restored.run(progress=lambda _: None, should_stop=lambda: any(
                row['tool_name'] == 'execute_implementation' for row in self._observations(restored)))
        self.assertEqual(self._observations(restored)[-1], executed)
        self.assertEqual(restored.state['turns'], history)
        self.assertIsNone(restored.state['pending'])
        self.assertEqual(restored.ledger.to_dict(), ledger)
        self.assertEqual(execution.resolve('candidate_execution.json').read_bytes(), raw)
        self.assertEqual(self.send.call_count, 1)
        self.coordinator.assert_not_called()

    def test_independent_review_uses_explicit_revision_receipt(self):
        import hashlib
        from simple_ar.core.capabilities import ArtifactStore
        from simple_ar.report.review_evidence import DELIVERY_REVIEW_CONTRACT, delivery_review_result
        from simple_ar.report.schema import ReportSectionReview
        from simple_ar.report.writing import _retained_response
        from simple_ar.report.writing import validate_document_inspection

        store = ArtifactStore(self.base / 'writing')
        snapshot = {'contract': DELIVERY_REVIEW_CONTRACT, 'artifacts': []}
        response = {'section_reviews': [{'section_id': 'document', 'verdict': 'pass', 'findings': []}]}
        request = {'response_record_contract': 'atomic_complete.v1', 'prompt': json.dumps({
            'task': 'review_document_coherence', 'delivery_review_contract': DELIVERY_REVIEW_CONTRACT,
            'delivery_artifacts': [], 'sections': [{'section_id': 'document', 'heading': 'Title',
                'markdown': 'Revised body.'}]})}
        # Both receipts are complete. An invalid explicit revision proof must
        # not silently fall back to the otherwise matching initial inspection.
        for location in ('writer/review', 'writer/review/revision-2/inspection/format_correction'):
            receipt = ArtifactStore(store.root / location)
            _retained_response(receipt, 'request.json', request, lambda: response)
        source = {}
        for name in ('request', 'response'):
            path = receipt.resolve(f'{name}.json')
            source[f'{name}_path'] = path.relative_to(store.root).as_posix()
            source[f'{name}_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        data = {'delivery_review': delivery_review_result(snapshot,
            [ReportSectionReview.model_validate(response['section_reviews'][0])])}
        review = {'inspection_source': source}
        validate_document_inspection(store, {'delivery_review': snapshot}, data, review_result=review)
        source['response_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'changed bytes'):
            validate_document_inspection(store, {'delivery_review': snapshot}, data, review_result=review)
        # Even matching file hashes cannot elevate a broken atomic completion.
        receipt.write_json('response.json', {'response': response,
            'request_sha256': '0' * 64, 'response_sha256': '0' * 64})
        source['response_sha256'] = hashlib.sha256(receipt.resolve('response.json').read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'complete bound record'):
            validate_document_inspection(store, {'delivery_review': snapshot}, data, review_result=review)


if __name__ == '__main__':
    unittest.main()
