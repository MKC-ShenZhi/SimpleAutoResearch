"""Server-only behavior checks for selected work continuity and input supply."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from simple_ar.app.reuse import ReuseAccess, select_reuse
from simple_ar.app.tool_application import ToolApplication
from simple_ar.core.artifacts import read_json, write_json
from simple_ar.core.working_context import select_working_context
from simple_ar.tools.delivery_versions import DeliveryVersions
from simple_ar.tools.materials import retained_working_inputs, _recorded_copy_source


def action(name, **arguments):
    return {'tool_name': name, 'arguments': arguments}


class SelectedWorkContinuityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.app = ToolApplication.create(self.base / 'old', 'Original goal', model=None, sources={})
        self.file = self.app.workspace.workspace / 'selected.txt'
        self.file.write_bytes(b'first\nsecond\nthird\n')
        (self.app.workspace.workspace / 'unselected.txt').write_text('UNSELECTED_PRIVATE_CONTENT')
        result = self.app.run(actions=[
            action('read_file', path='workspace/selected.txt', offset_bytes=6, max_bytes=7, full_digest=True),
            action('read_file', path='workspace/unselected.txt', full_digest=True),
            action('deliver_result', summary='Original result', artifacts=['workspace/selected.txt'])],
            progress=lambda _: None)
        self.assertEqual(result['status'], 'completed')

    def fresh(self):
        selection = select_reuse(self.app.session.store.root, ['artifacts'], version=1)
        return ToolApplication.create(self.base / 'fresh', 'Different current goal', model=None,
            sources={}, creation_declarations={'reuse_origin': selection.origin})

    def test_selected_partial_original_observation_reaches_common_working_input(self):
        old = {name: (self.app.session.store.root / name).read_bytes()
            for name in ('actions.json', 'budget_ledger.json', 'session_manifest.json')}
        fresh = self.fresh()
        view = fresh._retained_working_inputs()
        self.assertEqual(len(view['observations']), 1)
        observation = view['observations'][0]
        self.assertEqual((observation['offset_bytes'], observation['next_offset_bytes']), (6, 13))
        self.assertEqual(observation['text'], 'second\n')
        self.assertIn('records/', observation['source']['record'])
        self.assertNotIn('UNSELECTED_PRIVATE_CONTENT', json.dumps(fresh.state))
        self.assertEqual(fresh.state['requests'], ['Different current goal'])
        self.assertFalse(fresh.ledger.entries)
        self.assertFalse((fresh.session.store.root / 'delivery.json').exists())
        self.assertEqual({name: (self.app.session.store.root / name).read_bytes() for name in old}, old)
        provider = fresh._material_observations(['retained_delivery'])
        self.assertEqual(provider[0]['data']['text'], 'second\n')
        ToolApplication.load(fresh.session.store.root)

    def test_current_registered_result_selection_uses_its_exact_retained_version(self):
        selection = select_reuse(self.app.session.store.root, ['artifacts'])
        self.assertEqual(selection.version, 1)
        self.assertFalse(selection.materials)

    def test_changed_original_source_uses_retained_bytes_but_changed_input_is_rejected(self):
        self.file.write_text('later work')
        fresh = self.fresh()
        self.assertEqual(fresh._retained_working_inputs()['observations'][0]['text'], 'second\n')
        copied = Path(fresh.state['config']['sources']['retained_delivery']) / 'workspace/selected.txt'
        copied.write_text('tampered input')
        with self.assertRaisesRegex(ValueError, 'Retained source snapshot changed'):
            ToolApplication.load(fresh.session.store.root)

    def test_changed_original_receipt_does_not_become_source_by_matching_file_hash(self):
        turn = self.app.state['turns'][0]
        path = self.app.session.store.root / turn['tools'][0]['result']
        value = read_json(path)
        value['data']['text'] = 'forged!'
        write_json(path, value)
        self.assertFalse(self.fresh()._retained_working_inputs()['observations'])

    def test_ui_grant_checks_handoff_receipts_before_creation(self):
        first = self.app.state['turns'][0]['tools'][0]['result']
        root = self.app.session.store.root
        def authorize(path):
            if path == root / first:
                raise ValueError('Receipt outside UI grant')
            return path
        with self.assertRaisesRegex(ValueError, 'outside UI grant'):
            select_reuse(root, ['artifacts'], version=1, access=ReuseAccess(authorize=authorize))
        self.assertFalse((self.base / 'fresh').exists())

    def test_interrupted_creation_input_is_not_rebuilt_from_the_old_task_on_load(self):
        fresh = self.fresh()
        copied = Path(fresh.state['config']['sources']['retained_delivery']) / 'workspace/selected.txt'
        copied.unlink()
        with self.assertRaisesRegex(ValueError, 'Retained source snapshot changed'):
            ToolApplication.load(fresh.session.store.root)
        self.assertFalse(copied.exists())

    def test_retained_byte_link_is_rejected_before_creating_a_new_task(self):
        owner = DeliveryVersions(self.app.session.store.root)
        row = owner.select(1)['files']['workspace/selected.txt']
        blob = self.app.session.store.root / row['blob']
        content = blob.read_bytes()
        outside = self.base / 'outside.txt'
        outside.write_bytes(content)
        blob.unlink()
        try:
            try:
                blob.symlink_to(outside)
            except OSError as error:
                self.skipTest(f'Symbolic links unavailable: {error}')
            with self.assertRaises(ValueError):
                self.fresh()
            self.assertFalse((self.base / 'fresh/session_manifest.json').exists())
        finally:
            if blob.is_symlink():
                blob.unlink()
            if not blob.exists():
                blob.write_bytes(content)

    def test_import_projection_uses_only_explicit_subjects_and_cited_understanding(self):
        fresh = self.fresh()
        config = deepcopy(fresh.state['config'])
        handoff = config['source_snapshots']['retained_delivery']['working_context']
        record = handoff['observations'][0]['source']['record']
        source = {'record': 'memory_response.json', 'sha256': '0' * 64}
        handoff['notes'] = [
            {'text': 'Revisable interpretation', 'records': [record], 'source': source},
            {'text': 'Unrelated private note', 'records': ['records/unrelated.json'], 'source': source}]
        view = retained_working_inputs(config, resolve=fresh.workspace.resolve_material_root)
        self.assertEqual([note['text'] for note in view['notes']], ['Revisable interpretation'])
        self.assertEqual(view['notes'][0]['records'], [view['observations'][0]['record']])
        self.assertFalse(retained_working_inputs(config, resolve=fresh.workspace.resolve_material_root,
            targets=['workspace'])['observations'])


class WorkingSupplyContinuityTests(unittest.TestCase):
    def test_archiving_a_receipt_does_not_remove_its_current_original_material(self):
        row = {'turn': 0, 'record': 'records/read.json', 'kind': 'source_text',
            'role': 'historical_observation', 'path': 'workspace/a.txt', 'sha256': 'a' * 64,
            'offset_bytes': 0, 'next_offset_bytes': 4, 'text': 'DATA'}
        common = dict(user_requests=['Continue'], system_text='Allowed work', base_context={},
            turn_blocks=[[], []], observations=[row], native_observations=[], schema_tokens=0,
            context_token_target=100000, estimate_tokens=len, material_policy='working',
            require_work_notes=True, fit_work_notes=True, sparse_handoffs=True,
            processed_turns=[0], archived_turns=[0],
            work_notes=[{'text': 'A revisable interpretation', 'records': [row['record']]}],
            version_resolver=lambda _: 'a' * 64)
        _, old = select_working_context(**common)
        _, current = select_working_context(**common, preserve_material_observations=True)
        self.assertFalse(old['selected_records'])
        self.assertEqual(current['selected_records'][0]['source']['record'], row['record'])

    def test_repeated_same_path_window_shares_body_but_equal_hash_elsewhere_does_not(self):
        rows = [{'record': f'records/{i}.json', 'kind': 'source_text',
            'role': 'historical_observation', 'path': path, 'sha256': 'a' * 64,
            'offset_bytes': 0, 'next_offset_bytes': 4, 'text': 'DATA'}
            for i, path in enumerate(['workspace/a', 'workspace/a', 'workspace/b'])]
        _, selected = select_working_context(user_requests=['Continue'], system_text='Allowed work',
            base_context={}, turn_blocks=[], observations=rows, native_observations=[],
            schema_tokens=0, context_token_target=100000, estimate_tokens=len,
            material_policy='working', require_work_notes=True, fit_work_notes=True,
            sparse_handoffs=True, processed_turns=[], archived_turns=[],
            version_resolver=lambda _: 'a' * 64, preserve_material_observations=True)
        self.assertEqual(len(selected['selected_records']), 2)
        self.assertEqual([row['status'] for row in selected['omitted_records']], ['duplicate'])

    def test_copy_reuse_consumes_only_existing_recorded_copy_edges(self):
        from types import SimpleNamespace
        source, target = Path('/source'), Path('/target')
        graph = {'root': 0, 'nodes': [
            {'id': 0, 'path': target, 'sha256': 'a', 'state': 'recorded',
                'edges': [{'kind': 'input', 'node': 1}]},
            {'id': 1, 'path': source, 'sha256': 'a', 'state': 'ordinary', 'edges': []}]}
        workspace = SimpleNamespace(artifact_sources=lambda *_args, **_kw: {'/target': {'lineage': graph}},
            resolve=Path)
        self.assertFalse(_recorded_copy_source(workspace, '/target', 'a', '/source'))
        graph['nodes'][0]['edges'][0]['kind'] = 'copy'
        self.assertTrue(_recorded_copy_source(workspace, '/target', 'a', '/source'))
        graph['nodes'][0]['state'] = 'unbound'
        self.assertFalse(_recorded_copy_source(workspace, '/target', 'a', '/source'))


class InspectionWorkingContinuityTests(unittest.TestCase):
    def test_independent_inspection_freezes_work_policy_and_resume_preserves_it(self):
        from simple_ar.tools.inspection import InspectionRequest, InspectionToolGateway
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            app = ToolApplication.create(root / 'task', 'Independent work', model=None, sources={})
            file = app.workspace.workspace / 'original.txt'
            file.write_text('An original observation', encoding='utf-8')

            def gateway(policy):
                return InspectionToolGateway(app.session.store.root, app.workspace,
                    lambda _: self.fail('Preparing or resuming inspection must not call a model'),
                    protocol='subjects.v3', execution_protocol='shared.v1',
                    native_supply_policy='turns.v1', context_checkpoint_policy='economic.v3',
                    work_method='goal.v2', work_lifecycle=policy,
                    user_requests=lambda: ['Independent work'], operation_specs=lambda: [],
                    operation_call=lambda *_: self.fail('Pending inspection must not execute a tool'))

            arguments = {'goal': 'Check the claim', 'targets': ['workspace/original.txt']}
            request = InspectionRequest.model_validate(arguments)
            owner = gateway('integrated.v1')
            prepared = owner.call('inspect_artifacts', arguments, identity='inspection')
            self.assertEqual(prepared.data, {'inspection_pending': 'inspection'})
            path = owner._path('inspection', 'input')
            saved = owner.store.read_json(path)
            self.assertEqual(saved['work_lifecycle'], 'integrated.v1')
            before = owner.store.resolve(path).read_bytes()
            resumed = gateway(None)
            self.assertEqual(resumed.call('inspect_artifacts', arguments, identity='inspection').data,
                {'inspection_pending': 'inspection'})
            self.assertEqual(resumed.store.resolve(path).read_bytes(), before)

            record = 'inspection-original-read'
            raw = file.read_bytes()
            observation = {'turn': 0, 'record': record, 'path': str(file),
                'sha256': hashlib.sha256(raw).hexdigest(), 'offset_bytes': 0,
                'next_offset_bytes': len(raw), 'size_bytes': len(raw), 'text': raw.decode(),
                'kind': 'source_text', 'role': 'historical_observation'}
            memory = {'notes': [{'text': 'A fallible interpretation', 'records': [record]}],
                'through_turn': 1, 'compacted_records': {record}, 'archived_records': {record}}
            journal = [{'turn': 0, 'record': record, 'result': {'tool_name': 'read_file'}}]
            history = [{}, {}, {'role': 'assistant', 'content': 'Earlier work', 'reasoning_content': ''},
                {'role': 'assistant', 'content': 'Continue independent work', 'reasoning_content': ''}]
            client = SimpleNamespace(_budget_ledger=None,
                _settings=SimpleNamespace(context_input_tokens=128000, thinking_mode=''))
            with patch.object(resumed, '_conversation_material', return_value=(memory, [observation], journal)):
                _, supply = resumed._working_messages(saved, request, history, [], client)
            self.assertTrue(any(row['source']['record'] == record for row in supply['selected_records']))

            # A historical started inspection remains historical even under a
            # freshly configured integrated gateway. Its immutable input wins.
            historical = deepcopy(saved)
            historical.pop('work_lifecycle')
            owner.store.write_json(path, historical)
            current = gateway('integrated.v1')
            frozen = owner.store.resolve(path).read_bytes()
            current.call('inspect_artifacts', arguments, identity='inspection')
            self.assertEqual(owner.store.resolve(path).read_bytes(), frozen)
            with patch.object(current, '_conversation_material', return_value=(memory, [observation], journal)):
                _, old_supply = current._working_messages(historical, request, history, [], client)
            self.assertFalse(any(row['source']['record'] == record for row in old_supply['selected_records']))


if __name__ == '__main__':
    unittest.main()
