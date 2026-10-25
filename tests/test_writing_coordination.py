"""Representative legacy material boundaries, not Writer scheduling contracts.

New operations-loop delivery is covered in test_professional_work_loop. These
legacy consumers still protect retained source bytes, observation scope and
recovery. No continuation/adoption or fixed author/reviewer topology is required.
"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from simple_ar.core.budget import BudgetLedger
from simple_ar.core.capabilities import ArtifactStore
from simple_ar.tools.specs import ToolResult
from simple_ar.tools.workspace import FileWindow, WorkspaceToolGateway
from simple_ar.tools.writing import ComposeRequest, WritingToolGateway


class LegacyMaterialBoundaryTests(unittest.TestCase):
    def _real_workspace(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        ledger = BudgetLedger({}, storage_path=root / 'ledger.json')
        workspace = WorkspaceToolGateway(root, {'records': root / 'attempts'}, ledger)
        return root, workspace, WritingToolGateway(root, workspace, None, 'unused', 'task')

    def _document_work_fixture(self):
        root, workspace, writing = self._real_workspace()
        source = root / 'source'
        (source / 'code').mkdir(parents=True)
        (source / 'results').mkdir()
        script, table = source / 'code/calculate.py', source / 'results/record.txt'
        script.write_text('from pathlib import Path\nprint((Path(__file__).parent / "../results/record.txt").read_text())\n', encoding='utf-8')
        table.write_text('value=2.5\nscope=saved record; no new execution\n', encoding='utf-8')
        (source / 'unselected.txt').write_text('Not selected.', encoding='utf-8')
        workspace.mounts['source0'] = source
        writing.ledger = workspace.ledger
        writing.document_work = True
        client = Mock()
        writing.client_factory = lambda identity: client
        return root, workspace, writing, source, script, table, client, []

    def test_inspection_views_do_not_depend_on_attachment_order_or_reject_reader_formats(self):
        from simple_ar.report.review_evidence import SCOPED_DELIVERY_INSPECTION, delivery_target_pointer
        _, _, writing, source, script, _, _, _ = self._document_work_fixture()
        script.write_text('#' + 'x' * 249998 + '\n', encoding='utf-8')
        table = source / 'results' / 'small.csv'
        table.write_text('value\n' + '1\n' * 5000, encoding='utf-8')
        paths = [str(table), str(script)]
        first = writing._freeze_delivery_review(paths, 'ordered', text_contract=SCOPED_DELIVERY_INSPECTION)
        second = writing._freeze_delivery_review(paths[::-1], 'reversed', text_contract=SCOPED_DELIVERY_INSPECTION)
        self.assertEqual({row['path']: (row['sha256'], row['coverage']) for row in first['artifacts']},
            {row['path']: (row['sha256'], row['coverage']) for row in second['artifacts']})
        self.assertEqual({row['path']: row['coverage'] for row in first['artifacts']},
            {str(script): 'complete', str(table): 'table_profile'})
        records = source / 'results' / 'records.json'
        records.write_text(json.dumps([{'': i, ' ': None} for i in range(2000)]), encoding='utf-8')
        current = writing._freeze_delivery_review([str(records)], 'json-reader',
            text_contract=SCOPED_DELIVERY_INSPECTION)
        row, = current['artifacts']
        self.assertEqual(row['coverage'], 'table_profile')
        self.assertEqual(row['profile']['columns'], ['', ' '])
        self.assertEqual(delivery_target_pointer(row, 0), '/delivery_artifacts/0/profile')


    def test_document_focus_keeps_task_evidence_and_replay_keeps_original_scope(self):
        from types import SimpleNamespace
        root, workspace, writing, source, _, table, _, _ = self._document_work_fixture()
        focused = workspace.workspace / 'new-results'
        focused.mkdir()
        result_file = focused / 'summary.txt'
        result_file.write_text('A derived result; original conditions are in the supplied source.\n', encoding='utf-8')
        request = ComposeRequest(goal='Revise the result explanation against original conditions',
            material_roots=[str(focused)])
        calls = []
        def answer(system, prompt, **options):
            view = json.loads(prompt)
            calls.append(view)
            visible = {row[0] for inventory in view['inventories'] for row in inventory['files']}
            selected = [str(result_file), *([str(table)] if workspace.locator(str(table)) in visible else [])]
            inspected = {row['data']['absolute_path'] for row in view.get('observations', [])}
            unread = [path for path in selected if path not in inspected]
            if unread:
                return {'read_windows': [{'path': path} for path in unread]}
            return {'title': 'Result and original conditions', 'materials': selected, 'delivery_artifacts': []}
        client = SimpleNamespace(ask_json=Mock(side_effect=answer))
        for name, added, expected in (('new-scope', [str(source)], True), ('old-scope', [], False)):
            owner = ArtifactStore(root / 'tools/writing' / name)
            selected, _ = writing._select_materials(request, owner, name, client, '', None,
                document_work=True, task_evidence_roots=added)
            inputs = owner.read_json('selection-input.json')
            self.assertEqual(str(source) in [row['project_path'] for row in inputs['inventories']], expected)
            self.assertEqual(str(table) in [row.path for row in selected.materials], expected)
            original = owner.resolve('selection-input.json').read_bytes()
            before = client.ask_json.call_count
            writing._select_materials(request, owner, name, client, '', None,
                document_work=True, task_evidence_roots=[str(source)])
            self.assertEqual(client.ask_json.call_count, before)
            self.assertEqual(owner.resolve('selection-input.json').read_bytes(), original)
        inaccessible = root / 'not-granted'
        inaccessible.mkdir()
        before = client.ask_json.call_count
        with self.assertRaises(ValueError):
            writing._select_materials(request, ArtifactStore(root / 'tools/writing/refused'),
                'refused', client, '', None, document_work=True, task_evidence_roots=[str(inaccessible)])
        self.assertEqual(client.ask_json.call_count, before)


    def test_document_material_copies_enforce_scope_size_and_retained_bytes(self):
        root, workspace, writing, source, script, table, _, _ = self._document_work_fixture()
        proof = writing._document_material_copies([str(script), str(table)], identity='copy-unit')
        self.assertEqual(writing._document_material_copies([str(script), str(table)],
            identity='copy-unit', retained=proof), proof)
        with self.assertRaisesRegex(ValueError, 'distinct'):
            writing._document_material_copies([str(table), str(table)], identity='copy-duplicate')
        with self.assertRaises(ValueError):
            writing._document_material_copies([str(root / 'ledger.json')], identity='copy-private')
        oversized = source / 'large.dat'
        with oversized.open('wb') as handle:
            handle.truncate(17 * 1024 * 1024)
        with self.assertRaisesRegex(ValueError, '16 MiB'):
            writing._document_material_copies([str(oversized)], identity='copy-oversized')


    def _journal_material(self, root, workspace, raw, **window):
        sources = root / 'originals'
        sources.mkdir(exist_ok=True)
        workspace.mounts['source0'] = sources
        source = sources / 'values.txt'
        source.write_bytes(raw)
        request = {'path': 'source0/values.txt', 'full_digest': True, **window}
        result = workspace.call('read_file', request, identity='parent-read')
        self.assertEqual(result.status, 'ok', result)
        store = ArtifactStore(root / 'attempts' / 'parent-read')
        store.write_json('tool_result.json', result.model_dump(mode='json'))
        reference = {'record': 'records/parent-read/tool_result.json', 'request': request,
            'receipt_sha256': hashlib.sha256(store.resolve('tool_result.json').read_bytes()).hexdigest()}
        return source, result, reference


    def test_journal_content_reuses_relocated_windows_without_moving_source_authority(self):
        from simple_ar.tools.materials import (MaterialSelection, inspect_materials,
            material_inventories, material_observation_seed, selected_material_windows,
            validate_material_observation)
        for window in ({'offset_bytes': 7, 'max_bytes': 6},
                {'start_line': 2, 'max_lines': 1, 'max_bytes': 40}):
            with self.subTest(window=window):
                root, workspace, _ = self._real_workspace()
                raw = 'header\n测量\nunseen tail\n'.encode('utf-8')
                source, result, reference = self._journal_material(root, workspace, raw, **window)
                # Source-side role/provenance labels are not target facts.
                result.data.update(evidence_role='draft_claims_not_evidence',
                    producer={'invocation_id': 'old-producer'}, execution='old-authority')
                journal = ArtifactStore(root / 'attempts' / 'parent-read')
                journal.write_json('tool_result.json', result.model_dump(mode='json'))
                reference['receipt_sha256'] = hashlib.sha256(journal.resolve('tool_result.json').read_bytes()).hexdigest()
                target = workspace.workspace / 'project' / 'renamed.csv'
                target.parent.mkdir()
                target.write_bytes(raw)
                # The target is its own byte owner even when the historical
                # source is subsequently edited. No copy command is inspected.
                source.write_bytes(b'changed original')
                inventory = material_inventories(workspace, [str(target.parent)], identity='selection')
                before = workspace.ledger.to_dict()
                seed = material_observation_seed(workspace=workspace, inventories=inventory,
                    provider=lambda roots: [reference], roots=[str(target.parent)], context={})
                self.assertEqual(len(seed['observations']), 1)
                row, = seed['observations']
                self.assertEqual(row['data']['text'], result.data['text'])
                self.assertNotIn('unseen', row['data']['text'])
                self.assertEqual(row['request']['path'], str(target))
                self.assertEqual(row['data']['absolute_path'], str(target))
                self.assertEqual(row['content_reuse']['source'], reference)
                self.assertEqual(row['content_reuse']['target']['sha256'], result.data['sha256'])
                self.assertEqual(row['content_reuse']['target']['offset_bytes'], 7)
                for key in ('producer', 'evidence_role', 'execution', 'record'):
                    self.assertNotIn(key, row)
                    self.assertNotIn(key, row['data'])
                self.assertEqual(validate_material_observation(workspace, row, identity='resume'), row)
                self.assertEqual(workspace.ledger.to_dict(), before)
                owner = ArtifactStore(root / 'selection')
                client = Mock()
                client.ask_json.return_value = {'materials': [row['request']]}
                response = inspect_materials(workspace=workspace, context={}, instructions='Inspect',
                    inventories=inventory, owner=owner, identity='selection', client=client,
                    response_model=MaterialSelection, label='selection', final_fields=('materials',), seed=seed)
                self.assertTrue(client.ask_json.called)
                selected, = selected_material_windows(workspace, MaterialSelection.model_validate(response),
                    owner.read_json('selection-observations.json'), identity='resume')
                self.assertEqual(selected['request']['path'], str(target))
                self.assertEqual(selected['data']['text'], result.data['text'])
                self.assertEqual(selected['data']['sha256'], row['data']['sha256'])
                with self.assertRaisesRegex(ValueError, 'not inspected'):
                    selected_material_windows(workspace, MaterialSelection(materials=[
                        FileWindow(path=str(target), max_bytes=len(raw))]), [row], identity='tail')
                target.write_bytes(raw.replace(b'unseen', b'UNSEEN'))
                with self.assertRaises(ValueError):
                    validate_material_observation(workspace, row, identity='changed-target')
                target.write_bytes(raw)
                ArtifactStore(root / 'attempts' / 'parent-read').write_json('tool_result.json', {})
                with self.assertRaisesRegex(ValueError, 'receipt changed'):
                    validate_material_observation(workspace, row, identity='changed-receipt')


    def test_journal_content_rejects_different_lossy_unhashed_and_excluded_inputs(self):
        from simple_ar.tools.materials import material_inventories, material_observation_seed
        for mode in ('different-bytes', 'invalid-utf8', 'split-utf8', 'no-full-hash',
                'failed-read', 'changed-receipt', 'excluded-source', 'excluded-target',
                'not-in-inventory', 'outside-mount'):
            with self.subTest(mode=mode):
                root, workspace, _ = self._real_workspace()
                raw = b'head\nvalue=123\ntail\n'
                window = {'offset_bytes': 5, 'max_bytes': 9}
                if mode == 'invalid-utf8':
                    raw = b'head\nvalue=\xff23\ntail\n'
                elif mode == 'split-utf8':
                    raw = 'head\n测量\ntail\n'.encode('utf-8')
                    window = {'offset_bytes': 6, 'max_bytes': 4}
                source, result, reference = self._journal_material(root, workspace, raw, **window)
                target = workspace.workspace / 'copy.txt'
                target.write_bytes(raw if mode != 'different-bytes' else raw.replace(b'tail', b'TAIL'))
                if mode in ('no-full-hash', 'failed-read'):
                    payload = result.model_dump(mode='json')
                    if mode == 'no-full-hash':
                        payload['data']['sha256'] = None
                    else:
                        payload.update(status='error', error='Read failed')
                    record = ArtifactStore(root / 'attempts' / 'parent-read')
                    record.write_json('tool_result.json', payload)
                    reference['receipt_sha256'] = hashlib.sha256(record.resolve('tool_result.json').read_bytes()).hexdigest()
                elif mode == 'changed-receipt':
                    reference['receipt_sha256'] = '0' * 64
                inventory = material_inventories(workspace, [str(target)], identity='selection')
                if mode == 'not-in-inventory':
                    inventory[0]['candidate_files'] = {}
                elif mode == 'outside-mount':
                    inaccessible = root / 'private.txt'
                    inaccessible.write_bytes(raw)
                    inventory[0]['candidate_files'] = {str(inaccessible): len(raw)}
                excluded = ({str(source)} if mode == 'excluded-source' else
                    {str(target)} if mode == 'excluded-target' else set())
                seed = material_observation_seed(workspace=workspace, inventories=inventory,
                    provider=lambda roots: [reference], roots=[str(target)], context={}, excluded_paths=excluded)
                self.assertEqual(seed['observations'], [])


    def test_journal_reference_cross_mount_lookup_is_explicit_and_does_not_expand_inventory(self):
        from simple_ar.tools.materials import journal_material_references
        root, workspace, _ = self._real_workspace()
        _, _, reference = self._journal_material(root, workspace, b'values\n', max_bytes=7)
        store = ArtifactStore(root)
        store.write_json('reply.json', {'tool_calls': [{'id': 'read', 'function': {
            'name': 'read_file', 'arguments': json.dumps(reference['request'])}}]})
        turns = [{'reply': 'reply.json', 'tools': [{'id': 'read',
            'result': 'attempts/parent-read/tool_result.json'}]}]
        self.assertEqual(journal_material_references(workspace, store, turns, ['workspace']), [])
        self.assertEqual(journal_material_references(workspace, store, turns, ['workspace'],
            include_cross_path=True), [reference])


    def test_journal_content_keeps_focus_input_receipt_identity(self):
        from simple_ar.tools.materials import material_inventories, material_observation_seed
        root, workspace, _ = self._real_workspace()
        _, result, reference = self._journal_material(root, workspace, b'values\n', max_bytes=7)
        store = ArtifactStore(root / 'attempts' / 'parent-read')
        store.write_json('tool_result.json', ToolResult(tool_name='focus_work', data={'inputs': [
            {'request': reference['request'], 'data': result.data}]}).model_dump(mode='json'))
        reference.update(input_index=0, receipt_sha256=hashlib.sha256(store.resolve('tool_result.json').read_bytes()).hexdigest())
        target = workspace.workspace / 'copy.csv'
        target.write_bytes(b'values\n')
        inventory = material_inventories(workspace, [str(target)], identity='focus-copy')
        seed = material_observation_seed(workspace=workspace, inventories=inventory,
            provider=lambda roots: [reference], roots=[str(target)], context={})
        row, = seed['observations']
        self.assertEqual(row['content_reuse']['source']['input_index'], 0)
        invalid = {**reference, 'input_index': 1}
        rejected = material_observation_seed(workspace=workspace, inventories=inventory,
            provider=lambda roots: [invalid], roots=[str(target)], context={})
        self.assertEqual(rejected['observations'], [])
