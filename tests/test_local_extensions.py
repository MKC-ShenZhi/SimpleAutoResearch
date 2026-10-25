from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from simple_ar.app.tool_application import ToolApplication
from simple_ar.cli.task_options import apply_task_config, validate_resume_options
from simple_ar.cli.parser import build_parser
from simple_ar.cli.start import native_creation_options
from simple_ar.core.capabilities import ArtifactStore
from simple_ar.tools.extensions import LocalExtension, freeze_extensions
from simple_ar.tools.specs import ToolCall, ToolResult


class LocalExtensionContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.package = Path(temporary.name) / 'package'
        shutil.copytree(Path(__file__).resolve().parents[1] / 'examples/local_extension', self.package)
        # Loaded modules intentionally remain registered in production; isolate test fixtures.
        self.modules_before = set(sys.modules)
        self.addCleanup(self.clean_modules)

    def clean_modules(self):
        for name in set(sys.modules) - self.modules_before:
            if name.startswith('_simple_ar_extension_'):
                sys.modules.pop(name, None)

    def owner(self):
        return LocalExtension(freeze_extensions([self.package])[0], Mock())

    def test_registered_module_annotations_reuse_and_owner_isolation(self):
        (self.package / 'notes.py').write_text('''from __future__ import annotations
from dataclasses import dataclass, KW_ONLY
from typing import get_type_hints
import importlib
import sys

@dataclass
class Note:
    _: KW_ONLY
    text: str

calls = 0
def count_words(arguments, services):
    global calls
    calls += 1
    assert importlib.import_module(__name__) is sys.modules[__name__]
    assert get_type_hints(Note)['text'] is str
    assert __spec__.name == __name__ and __package__ == ''
    return {'words': len(Note(text=arguments['path']).text.split()), 'calls': calls,
            'module': __name__}
''', encoding='utf-8')
        descriptor = self.package / 'extension.toml'
        contract = descriptor.read_text(encoding='utf-8').split('[[tools]]', 1)[1]
        with descriptor.open('a', encoding='utf-8') as stream:
            stream.write('\n[[tools]]' + contract.replace('name = "count_words"', 'name = "inspect"'))
        owner = self.owner()
        self.assertFalse(owner._modules)  # Freezing/catalog construction does not execute code.
        first = owner.call('local_notes__count_words', {'path': 'one two'}, identity='first')
        self.assertTrue(first.ok, first.error)
        second = owner.call('local_notes__count_words', {'path': 'three'}, identity='second')
        self.assertTrue(second.ok, second.error)
        self.assertEqual((first.data['words'], second.data['calls']), (2, 2))
        self.assertEqual(first.data['module'], second.data['module'])
        shared = owner.call('local_notes__inspect', {'path': 'five'}, identity='shared')
        self.assertTrue(shared.ok, shared.error)
        self.assertEqual((shared.data['module'], shared.data['calls']), (first.data['module'], 3))
        self.assertFalse(list(self.package.rglob('*.pyc')))
        other = self.owner().call('local_notes__count_words', {'path': 'four'}, identity='other')
        self.assertTrue(other.ok, other.error)
        self.assertEqual(other.data['calls'], 1)
        self.assertNotEqual(first.data['module'], other.data['module'])
        (self.package / 'notes.py').write_text('raise AssertionError("changed")', encoding='utf-8')
        changed = owner.call('local_notes__count_words', {'path': 'x'}, identity='changed')
        self.assertEqual(changed.status, 'error')
        self.assertIn('identity changed', changed.error)

    def test_failed_import_is_removed_and_relative_import_is_unsupported(self):
        for source in ('raise RuntimeError("import failed")', 'from .helper import value'):
            with self.subTest(source=source):
                (self.package / 'notes.py').write_text(source, encoding='utf-8')
                owner = self.owner()
                result = owner.call('local_notes__count_words', {'path': 'x'}, identity='failed')
                self.assertEqual(result.status, 'error')
                self.assertFalse(owner._modules)
                self.assertFalse(any(name.startswith(owner._module_prefix) for name in sys.modules))
        # A conventional package filename must not implicitly enable package imports.
        (self.package / 'notes.py').rename(self.package / '__init__.py')
        descriptor = self.package / 'extension.toml'
        descriptor.write_text(descriptor.read_text(encoding='utf-8').replace(
            'notes.py', '__init__.py'), encoding='utf-8')
        (self.package / 'helper.py').write_text('raise AssertionError("unchecked sibling")', encoding='utf-8')
        result = self.owner().call('local_notes__count_words', {'path': 'x'}, identity='package')
        self.assertEqual(result.status, 'error')
        self.assertIn('no known parent package', result.error)

    def test_standard_schema_local_references_and_no_external_retrieval(self):
        from simple_ar.tools.extensions import _validate_schema

        schema = {'type': 'object', '$defs': {'count': {'type': 'integer', 'minimum': 1}},
                  'properties': {'count': {'$ref': '#/$defs/count'}},
                  'required': ['count'], 'additionalProperties': False}
        _validate_schema(schema, {'count': 2}, check_value=True)
        for value in ({'count': 0}, {'count': True}, {'count': 2, 'extra': 1}, {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _validate_schema(schema, value, check_value=True)
        # Both network and local-file reference retrieval must fail without opening anything.
        with patch('urllib.request.urlopen', side_effect=AssertionError('network fetch')), \
                patch('pathlib.Path.open', side_effect=AssertionError('file fetch')):
            for ref in ('https://example.invalid/schema.json', 'file:///tmp/schema.json', '#/$defs/missing'):
                with self.subTest(ref=ref), self.assertRaisesRegex(ValueError, 'resolved locally'):
                    _validate_schema({'type': 'object', '$ref': ref}, {}, check_value=True)
        with self.assertRaises(ValueError):
            _validate_schema({'type': 'object', 'properties': []})

    def test_frozen_resources_and_contract_copies(self):
        descriptor = self.package / 'extension.toml'
        descriptor.write_text(descriptor.read_text(encoding='utf-8').replace(
            'resources = []', 'resources = ["companion.txt"]'), encoding='utf-8')
        companion = self.package / 'companion.txt'
        companion.write_text('original', encoding='utf-8')
        frozen = freeze_extensions([self.package])[0]
        owner = LocalExtension(frozen, Mock())
        frozen['tools'][0]['input_schema']['type'] = 'string'
        owner.specs()[0].input_schema['type'] = 'array'
        owner.verify()
        companion.write_text('edited', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            owner.verify()
        companion.unlink()
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            owner.verify()
        # Instructions-only packages retain the same freeze/verify contract.
        descriptor.write_text(descriptor.read_text(encoding='utf-8').split('[[tools]]')[0]
                              .replace('resources = ["companion.txt"]', 'resources = []'), encoding='utf-8')
        skill = self.owner()
        result = skill.call('local_notes__instructions', {}, identity='skill')
        self.assertTrue(result.ok, result.error)

    def test_host_identity_protocol_is_stable_bounded_and_historical(self):
        host = Mock(return_value=ToolResult(tool_name='read_file', data={'text': 'one two', 'has_more': False}))
        frozen = freeze_extensions([self.package])[0]
        parent = 'x' * 100
        modern = LocalExtension(frozen, host, host_protocol='nested_calls.v1')
        for _ in range(2):
            self.assertTrue(modern.call('local_notes__count_words', {'path': 'source0/note.txt'}, identity=parent).ok)
        identities = [call.args[2] for call in host.call_args_list]
        self.assertEqual(identities[0], identities[1])
        self.assertNotEqual(identities[0], parent)
        self.assertLessEqual(len(identities[0]), 101)
        host.reset_mock()
        legacy = LocalExtension(frozen, host, host_protocol=None)
        self.assertTrue(legacy.call('local_notes__count_words', {'path': 'source0/note.txt'}, identity=parent).ok)
        self.assertEqual(host.call_args.args[2], parent)
        with self.assertRaisesRegex(ValueError, 'host-call protocol'):
            LocalExtension(frozen, host, host_protocol='unknown')


class LocalExtensionIntegrationTests(unittest.TestCase):
    def test_declared_extension_completion_survives_supply_and_terminal_recovery(self):
        # The core never knows this domain/tool name or its delivery field.
        # One integrated check owns registration, completion and retained use.
        from simple_ar.tools.workspace import RetainedDelivery
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / 'package'
            package.mkdir()
            (package / 'extension.toml').write_text('''[extension]
id = "research_note"
version = "1"
description = "Deliver an explicit research note"
scope = "Caller-supplied note, no scientific certification"

[[tools]]
name = "finish_note"
description = "Return the caller note with its explicit completion"
implementation = "notes.py"
function = "finish_note"
permission_level = "plan"
completion = {field = "handoff", closing = true}
[tools.input_schema]
type = "object"
required = ["handoff"]
additionalProperties = false
[tools.input_schema.properties.handoff]
type = "object"
''', encoding='utf-8')
            (package / 'notes.py').write_text(
                'def finish_note(arguments, services):\n    return {"handoff": arguments["handoff"]}\n',
                encoding='utf-8')
            app = ToolApplication.create(root / 'task', 'Deliver the explicit note', model=None,
                sources={}, extensions=[package], tool_contracts='full', control_protocol=None)
            name = 'research_note__finish_note'
            frozen = app.state['config']['tool_completions'].copy()
            self.assertEqual(frozen[name]['field'], 'handoff')
            self.assertIn(name, [row['function']['name'] for row in app._closing_schemas()])
            app.supply(supply_id='keep-completion', reason='Retain original completion semantics',
                changes={'allow_execution': True})
            self.assertEqual(app.state['config']['tool_completions'], frozen)
            note = app.workspace.workspace / 'note.txt'
            note.write_text('Existing note; no new measurement performed', encoding='utf-8')
            delivery = {'summary': 'Explicit note retained; no new measurement performed',
                'artifacts': ['workspace/note.txt']}
            app.run(actions=[{'tool_name': name, 'arguments': {'handoff': delivery}}], progress=lambda _: None)
            self.assertEqual(app.session.manifest.status, 'completed')
            before = app.ledger.to_dict()
            loaded = ToolApplication.load(root / 'task')
            self.assertTrue(loaded._commit_terminal(lambda _: None))
            self.assertEqual(loaded.ledger.to_dict(), before)
            self.assertEqual(RetainedDelivery(root / 'task').reusable_materials(), {'artifacts': [note]})
            original = note.read_bytes()
            note.write_text('Changed note cannot inherit delivery acceptance', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'delivered files changed'):
                RetainedDelivery(root / 'task').reusable_materials()
            note.write_bytes(original)
            # Saved old sessions do not silently acquire extension completion.
            loaded.state['config'].pop('tool_completions')
            loaded.state['config'].pop('document_source_contract')
            loaded._save()
            loaded.supply(supply_id='legacy-config', reason='An unrelated supply preserves absent contracts',
                changes={'allow_network': True})
            self.assertNotIn('tool_completions', loaded.state['config'])
            self.assertNotIn('document_source_contract', loaded.state['config'])
            loaded.supply(supply_id='document-source', reason='Explicitly adopt new source handling for subsequent work',
                changes={'document_source_contract': 'document_source.v1'})
            self.assertEqual(loaded.state['config']['document_source_contract'], 'document_source.v1')
            self.assertFalse(loaded._terminal_intent({'function': {'name': name, 'arguments': '{}'}}))

    @unittest.skipUnless(shutil.which('rg'), 'Nested search uses the existing ripgrep tool')
    def test_nested_host_searches_have_distinct_recoverable_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / 'package'
            shutil.copytree(Path(__file__).resolve().parents[1] / 'examples/local_extension', package)
            (package / 'notes.py').write_text('''def count_words(arguments, services):
    observations = []
    for query in arguments['queries']:
        result = services['call_tool']('search_files', {'path': arguments['path'], 'query': query})
        if not result.ok:
            raise ValueError(result.error)
        observations.append(result.data)
    return {'observations': observations}
''', encoding='utf-8')
            with (package / 'extension.toml').open('a', encoding='utf-8') as stream:
                stream.write('\n[tools.input_schema.properties.queries]\ntype = "array"\nitems = {type = "string"}\n')
            project = root / 'project'
            project.mkdir()
            (project / 'observations.txt').write_text('accuracy = 0.91\nlatency = 0.42\n', encoding='utf-8')
            app = ToolApplication.create(root / 'task', 'Inspect two different properties', model=None,
                sources={'source0': project}, extensions=[package],
                process_limits={'process_invocations': 2, 'process_wall_seconds': 30}, control_protocol=None)
            arguments = {'path': 'source0', 'queries': ['accuracy', 'latency']}
            app.run(actions=[{'tool_name': 'local_notes__count_words', 'arguments': arguments}], progress=lambda _: None)
            observed = app.state['turns'][0]['tools'][0]
            result = app.session.store.read_json(observed['result'])
            self.assertEqual(result['status'], 'ok', result)
            first, second = result['data']['observations']
            self.assertIn('accuracy', first['text'])
            self.assertNotIn('latency', first['text'])
            self.assertIn('latency', second['text'])
            self.assertNotIn('accuracy', second['text'])
            self.assertNotEqual(first['receipt'], second['receipt'])
            ledger = app.ledger.to_dict()
            parent = Path(observed['result']).parent.name
            with patch('simple_ar.tools.workspace.run_process', side_effect=AssertionError('Do not replay a search')):
                replay = app.extensions[0].call('local_notes__count_words', arguments, identity=parent)
            self.assertTrue(replay.ok, replay.error)
            self.assertEqual(replay.data, result['data'])
            self.assertEqual(app.ledger.to_dict(), ledger)

    def test_extension_fields_do_not_inherit_professional_batch_controls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / 'package'
            shutil.copytree(Path(__file__).resolve().parents[1] / 'examples/local_extension', package)
            with (package / 'extension.toml').open('a', encoding='utf-8') as stream:
                stream.write('\n[tools.input_schema.properties.publication]\ntype = "string"\n'
                    '\n[tools.input_schema.properties.complete_work]\ntype = "boolean"\n')
            project = root / 'project'
            project.mkdir()
            (project / 'note.txt').write_text('one two three', encoding='utf-8')
            for professional in (False, True):
                with self.subTest(professional=professional):
                    app = ToolApplication.create(root / str(professional), 'Read two bibliographic entries', model=None,
                        sources={'source0': project}, extensions=[package], professional_inputs=professional, control_protocol=None)
                    app.run(actions=[{'tool_name': 'local_notes__count_words', 'arguments': {
                        'path': 'source0/note.txt', 'publication': title, 'complete_work': True}}
                        for title in ('First paper', 'Second paper')], progress=lambda _: None)
                    rows = [app.session.store.read_json(item['result']) for turn in app.state['turns'] for item in turn['tools']]
                    self.assertEqual([row['status'] for row in rows], ['ok', 'ok'], rows)
                    self.assertEqual([row['data']['words'] for row in rows], [3, 3])

    def test_local_package_consumer_recovery_and_identity(self):
        # Separate package/project/task roots: no repository edit or auto-discovery.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / 'outside-package'
            shutil.copytree(Path(__file__).resolve().parents[1] / 'examples/local_extension', package)
            project = root / 'project'
            project.mkdir()
            notes = project / 'notes.txt'
            notes.write_text('one two three\n', encoding='utf-8')
            config_path = project / 'task.toml'
            config_path.write_text('[task]\nextensions = ["../outside-package"]\n', encoding='utf-8')
            args = build_parser().parse_args(['start', '--task-config', str(config_path)])
            args._explicit_start_destinations = {'task_config'}
            apply_task_config(args)
            self.assertEqual(args.extensions, [package.resolve()])
            self.assertIn('extensions', args._native_start_destinations)
            self.assertEqual(native_creation_options(args)['extensions'], args.extensions)
            args.resume_task = root / 'saved'
            with self.assertRaisesRegex(ValueError, 'extensions'):
                validate_resume_options(args)

            fake = Mock()
            fake.effective_output_limit.return_value = None
            fake._settings = SimpleNamespace(api_mode='chat', context_input_tokens=120000)
            fake.connection_binding.return_value = {
                'profile': 'fixture', 'settings': {'model': 'fixture', 'api_mode': 'chat'}}
            selected = ['local_notes__instructions', 'local_notes__count_words']

            def message(name, arguments, identity):
                return {'role': 'assistant', 'content': '', 'reasoning_content': '', 'tool_calls': [
                    {'id': identity, 'type': 'function', 'function': {
                        'name': name, 'arguments': json.dumps(arguments)}}]}

            fake.chat_turn.side_effect = [
                message('load_tools', {'names': selected}, 'load'),
                message(selected[0], {}, 'skill'),
                message(selected[1], {'path': 'source0/notes.txt'}, 'count'),
                message('ask_user', {'question': 'What should follow this observed count?'}, 'question')]
            run_dir = root / 'saved'
            with patch('simple_ar.app.tool_application.LLMClient.from_env', return_value=fake):
                app = ToolApplication.create(run_dir, 'Inspect my note with the local notes skill',
                    model='fixture', sources={'source0': project}, professional_inputs=True,
                    extensions=args.extensions, max_turns=10, control_protocol=None)
                self.assertEqual(app.gateway.domain_owner('read_file'), app.workspace)
                self.assertIs(app.gateway.domain_owner(selected[1]), app.extensions[0])
                self.assertTrue(app.gateway.registry.request_visible(selected[1]))
                self.assertTrue(app.gateway.registry.request_visible('compose_document'))
                self.assertFalse(app.gateway.registry.request_visible('write_file'))
                self.assertFalse(app.gateway.registry.request_visible('execute_implementation'))
                self.assertIn(selected[1], app._specialist_catalog())
                self.assertNotIn(selected[1], {row['function']['name'] for row in app.schemas})
                # Resume after the result is durable but before controller commit.
                original = ArtifactStore.write_json

                def interrupt(store, path, data, **kwargs):
                    if (path == 'capability_result.json' and store.exists('native_result.json')
                            and store.read_json('native_result.json').get('tool_name') == selected[1]):
                        raise KeyboardInterrupt('extension result retained before commit')
                    return original(store, path, data, **kwargs)

                with patch.object(ArtifactStore, 'write_json', interrupt):
                    app.run(progress=lambda _: None)
                pending = app.state['pending']
                self.assertIsNotNone(pending, app.session.manifest.status_reason)
                result_path = f"attempts/{pending['attempt']}/native_result.json"
                observed = app.session.store.read_json(result_path)
                self.assertEqual(observed['status'], 'ok', observed)
                self.assertEqual(observed['data']['words'], 3)
                bytes_before = (run_dir / result_path).read_bytes()
                restored = ToolApplication.load(run_dir)
                with patch.object(LocalExtension, 'call', side_effect=AssertionError('must not replay')):
                    restored.run(progress=lambda _: None)
                self.assertIsNone(restored.state['pending'], restored.session.manifest.status_reason)
                self.assertEqual((run_dir / result_path).read_bytes(), bytes_before)
                self.assertEqual(fake.chat_turn.call_count, 4)
                calls = fake.chat_turn.call_args_list
                initial = json.loads(calls[0].args[0][1]['content'])
                self.assertIn(selected[0], initial['available_specialists'])
                self.assertNotIn('instructions grant no access', json.dumps(initial).lower())
                self.assertNotIn(selected[1], {row['function']['name'] for row in calls[0].kwargs['tools']})
                self.assertIn(selected[1], {row['function']['name'] for row in calls[1].kwargs['tools']})
                skill_result = restored.session.store.read_json(restored.state['turns'][1]['tools'][0]['result'])
                self.assertIn('These instructions grant no access', skill_result['data']['instructions'])
                self.assertIn('These instructions grant no access', json.dumps(calls[2].args[0]))

                # Declared policy and host mount checks still govern custom consumers.
                restored.gateway.policy.blocked_tools.add(selected[1])
                denied = restored.gateway.call(ToolCall(tool_name=selected[1],
                    arguments={'path': 'source0/notes.txt'}, trace_id='denied'))
                self.assertEqual(denied.status, 'blocked')
                restored.gateway.policy.blocked_tools.remove(selected[1])
                outside = root / 'not-mounted.txt'
                outside.write_text('private', encoding='utf-8')
                denied = restored.gateway.call(ToolCall(tool_name=selected[1],
                    arguments={'path': str(outside)}, trace_id='outside'))
                self.assertEqual(denied.status, 'error')
                self.assertEqual(restored._extension_host_call('run_process', {}, 'process').status, 'blocked')
                bad = restored.gateway.call(ToolCall(tool_name=selected[1],
                    arguments={'path': 1}, trace_id='invalid'))
                self.assertEqual(bad.status, 'error')
                trace = [json.loads(line) for line in (run_dir / 'tools/tool_trace.jsonl').read_text().splitlines()]
                self.assertTrue(any(row['tool'] == selected[1] and row['status'] == 'ok'
                    and row['trace_id'] == pending['attempt'] for row in trace))
                child = 'ext-' + hashlib.sha256(json.dumps([selected[1], pending['attempt']]).encode('utf-8')).hexdigest()[:32] + '-1'
                self.assertTrue(any(row['tool'] == 'read_file' and row['trace_id'] == child for row in trace))
                self.assertTrue(any(row['tool'] == selected[1] and row['status'] == 'blocked' for row in trace))
                attempts = restored.session.list_attempts()
                self.assertEqual(restored.session.manifest.budget.attempts, len(attempts))
                self.assertEqual(next(a for a in attempts if a.attempt_id == pending['attempt']).status, 'completed')

                with self.assertRaisesRegex(ValueError, 'collision'):
                    restored.gateway.registry.register_package(restored.extensions[0].specs(), restored.extensions[0])
                with self.assertRaisesRegex(ValueError, 'duplicate extension namespace'):
                    freeze_extensions([package, package])
                descriptor = package / 'extension.toml'
                original_descriptor = descriptor.read_bytes()
                descriptor.write_text(original_descriptor.decode().replace('"read_only"', '"network"'), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'read_only/plan'):
                    freeze_extensions([package])
                descriptor.write_bytes(original_descriptor)
                # Retained artifact ownership survives unavailable executable dependencies.
                from simple_ar.cli.start import session_materials
                restored.run(actions=[{'tool_name': 'write_file', 'arguments': {
                    'path': 'workspace/portable.txt', 'content': 'Retained portable output',
                    'delivery': {'summary': 'Portable result', 'artifacts': ['workspace/portable.txt']}}}],
                    progress=lambda _: None)
                portable = run_dir.resolve() / 'workspace/portable.txt'
                # Model a domain's declared group in the existing accepted receipt,
                # including both ownership and its original full output digest.
                receipt = restored.state['turns'][-1]['tools'][-1]['result']
                row = restored.session.store.read_json(receipt)
                row['artifacts'] = ['workspace/portable.txt']
                row['metadata']['reusable_materials'] = {'summary': ['workspace/portable.txt']}
                row['data']['artifact_sha256'] = {'workspace/portable.txt': row['data']['sha256']}
                restored.session.store.write_json(receipt, row)
                expected = {'artifacts': [portable], 'summary': [portable]}
                self.assertEqual(restored.reusable_materials(), expected)
                digest = row['data']['artifact_sha256']['workspace/portable.txt']
                row['data']['artifact_sha256']['workspace/portable.txt'] = '0' * 64
                restored.session.store.write_json(receipt, row)
                self.assertEqual(restored.reusable_materials(), {'artifacts': [portable]})
                row['data']['artifact_sha256']['workspace/portable.txt'] = digest
                restored.session.store.write_json(receipt, row)
                saved_actions = (run_dir / 'actions.json').read_bytes()
                for resource in ('SKILL.md', 'notes.py', 'extension.toml'):
                    with self.subTest(resource=resource):
                        file = package / resource
                        original_bytes = file.read_bytes()
                        file.write_bytes(original_bytes + b'\n# changed\n')
                        with self.assertRaisesRegex(ValueError, 'extension (identity changed|unavailable)'):
                            ToolApplication.load(run_dir)
                        self.assertEqual(session_materials(run_dir, include_project=False), expected)
                        self.assertEqual((run_dir / result_path).read_bytes(), bytes_before)
                        self.assertEqual((run_dir / 'actions.json').read_bytes(), saved_actions)
                        file.write_bytes(original_bytes)
                descriptor.rename(package / 'unavailable.toml')
                with self.assertRaisesRegex(ValueError, 'extension unavailable'):
                    ToolApplication.load(run_dir)
                self.assertEqual(session_materials(run_dir), expected)
                portable.write_text('Tampered replacement', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'delivered files changed'):
                    session_materials(run_dir, include_project=False)
                portable.write_text('Retained portable output', encoding='utf-8')
                (package / 'unavailable.toml').rename(descriptor)
                state = restored.session.store.read_json('actions.json')
                state['config']['project_python'] = str(root / 'missing-python')
                restored.session.store.write_json('actions.json', state)
                self.assertEqual(session_materials(run_dir, include_project=False), expected)
                with self.assertRaisesRegex(ValueError, 'Python interpreter is unavailable'):
                    ToolApplication.load(run_dir)
                self.assertEqual(json.loads((run_dir / result_path).read_text())['data']['words'], 3)


if __name__ == '__main__':
    unittest.main()
