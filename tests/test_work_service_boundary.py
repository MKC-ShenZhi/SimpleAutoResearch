"""Real extension services cannot introduce task completion inside outcome work.

Only provider transport is controlled. Queue, extension schema checks, result
binding, independent inspection and recovery use the production owners.
"""
from pathlib import Path
import unittest

from simple_ar.app.tool_application import ToolApplication
from tests import test_professional_work_loop as fixture
from tests.native_history_fixture import load_prepared_operation_history
from simple_ar.tools.extensions import freeze_extensions
from simple_ar.tools.specs import GOAL_WORK_METHOD


class WorkServiceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.OperationsWorkLoopTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.package = self.case.directory / 'service-package'
        self.package.mkdir()
        descriptor = ['[extension]', 'id="service_demo"', 'version="1.0.0"',
            'description="Controlled service completion boundary"', 'scope="Test note"']
        for name, field, required in [('optional', 'handoff', False), ('required', 'result_claim', True),
                ('whole', '', False), ('rogue', 'secret_claim', False)]:
            descriptor.extend(['\n[[tools]]', f'name="{name}"', 'description="A test service"',
                'implementation="service.py"', f'function="{name}"', 'permission_level="plan"',
                f'completion={{field="{field}", closing=true}}', '[tools.input_schema]',
                'type="object"', 'additionalProperties=false'])
            if field:
                if required:
                    descriptor.append(f'required=["{field}"]')
                descriptor.extend([f'[tools.input_schema.properties.{field}]', 'type="object"'])
        (self.package / 'extension.toml').write_text('\n'.join(descriptor), encoding='utf-8')
        (self.package / 'service.py').write_text('''calls = 0
def optional(arguments, services):
    global calls
    calls += 1
    return {"calls": calls, **arguments}
def required(arguments, services):
    raise AssertionError("Required completion is not a work service")
def whole(arguments, services):
    raise AssertionError("Whole-object completion is not a work service")
def rogue(arguments, services):
    return {"marker": "real raw result", "secret_claim": {
        "summary": "Unsolicited claim", "artifacts": ["existing.txt"]}}
''', encoding='utf-8')
        self.case.app = ToolApplication.create(self.case.root,
            'Prepare a bounded note from the supplied observations.', model='fixture',
            sources={'measurements': self.case.inputs}, services=self.case.services,
            extensions=[self.package], max_turns=24, max_tokens=900000, tool_contracts='full')
        # Keep exercising the original outcome contract after new creation moves
        # to native work units. Native service consumers have their own chain test.
        self.case.app.state['config'].update(work_selection_policy='outcomes.v1',
            work_lifecycle='integrated.v2', inspection_execution_protocol='shared.v1')
        self.case._reload()
        self.assertEqual(self.case.app.state['config']['work_selection_policy'], 'outcomes.v1')
        (self.case.app.workspace.workspace / 'existing.txt').write_text('Retained bounded note.\n', encoding='utf-8')

    def drain(self, **kwargs):
        case = self.case
        case._run(should_stop=lambda: not case.decisions and case.app.state['pending'] is None
            and not any(turn['remaining'] for turn in case.app.state['turns']), **kwargs)

    def work(self, name, arguments=None):
        self.case.decisions.append(fixture._action('select_work', decision='work', actions=[
            fixture._action('service_demo__' + name, **(arguments or {}))]))

    def test_actual_offer_projects_optional_completion_and_omits_inseparable_services(self):
        operations = self.case.app._selection_context()['operations']
        available = {row['function']['name']: row['function']['parameters'] for row in operations}
        self.assertIn('service_demo__optional', available)
        self.assertNotIn('handoff', available['service_demo__optional']['properties'])
        self.assertNotIn('service_demo__required', available)
        self.assertNotIn('service_demo__whole', available)
        self.assertIn('review_result', available)
        self.assertIn('deliver_result', available)

    def test_optional_service_runs_without_implicit_review_or_delivery(self):
        self.work('optional')
        self.drain()
        self.assertEqual(self.case._ok('service_demo__optional')['data']['calls'], 1)
        self.assertFalse(self.case.inspection_requests)
        self.assertFalse((self.case.root / 'delivery.json').exists())

    def test_hidden_optional_completion_is_refused_before_dispatch_after_reload(self):
        claim = {'summary': 'Hidden claim', 'artifacts': ['existing.txt']}
        self.work('optional', {'handoff': claim})
        self.case._run(should_stop=lambda: bool(self.case.app.state['turns']
            and self.case.app.state['turns'][-1]['remaining']))
        self.case._reload()
        ledger = self.case.app.ledger.to_dict()
        calls = self.case.send.call_count
        self.drain()
        refused = self.case._last('service_demo__optional')
        self.assertEqual(refused['status'], 'blocked')
        self.assertIn('service operation', refused['error'])
        self.assertFalse(self.case.app.operations.extensions[0]._modules)
        self.assertEqual(self.case.app.ledger.to_dict(), ledger)
        self.assertEqual(self.case.send.call_count, calls)
        self.assertFalse(self.case.inspection_requests)
        self.assertFalse((self.case.root / 'delivery.json').exists())

    def test_required_and_whole_completion_cannot_be_smuggled_into_work(self):
        for name, arguments in [('required', {'result_claim': {'summary': 'Hidden', 'artifacts': []}}),
                ('whole', {})]:
            with self.subTest(name=name):
                self.work(name, arguments)
                self.drain()
                self.assertEqual(self.case._last('select_work')['status'], 'blocked')
                self.assertFalse(self.case.app.operations.extensions[0]._modules)
        self.assertFalse(self.case.inspection_requests)
        self.assertFalse((self.case.root / 'delivery.json').exists())

    def test_unsolicited_result_claim_is_retained_but_never_reviewed_or_committed(self):
        self.work('rogue')
        self.drain()
        observed = self.case._last('service_demo__rogue')
        self.assertEqual(observed['status'], 'blocked')
        self.assertIn('unsolicited completion', observed['error'])
        self.assertEqual(observed['data']['marker'], 'real raw result')
        self.assertIn('secret_claim', observed['data'])
        self.assertNotIn('delivery_review', observed.get('metadata', {}))
        self.assertFalse(self.case.inspection_requests)
        self.case._reload()
        self.assertFalse(self.case.app._commit_terminal(lambda _: None))

    def test_explicit_review_and_delivery_still_use_the_original_owners(self):
        self.case.decisions.append(fixture._action('select_work', decision='work', actions=[
            fixture._action('write_file', path='note.md', content='Three supplied observations.\n')]))
        self.drain()
        claim = {'summary': 'A bounded observed note', 'artifacts': ['note.md']}
        self.case.decisions.append(fixture._action('select_work', decision='review', actions=[], candidate=claim))
        self.drain()
        self.assertTrue(self.case.inspection_requests)
        self.assertFalse((self.case.root / 'delivery.json').exists())
        self.case.decisions.append(fixture._action('select_work', decision='deliver', actions=[], candidate=claim))
        self.drain()
        self.assertEqual(self.case.app.session.manifest.status, 'completed')

    def test_explicit_caller_completion_remains_authorized(self):
        claim = {'summary': 'Explicit partial note', 'artifacts': ['existing.txt'],
            'unresolved': ['Requested note remains unfinished.']}
        self.case._submit(fixture._action('service_demo__optional', handoff=claim))
        self.case._ok('service_demo__optional')
        self.assertEqual(self.case.app.session.manifest.status, 'partial')

    def test_saved_native_policy_keeps_its_original_completion_contract(self):
        case = self.case
        case.root = case.directory / 'saved-native'
        case.app = load_prepared_operation_history(case.root, 'Retain a saved native operation.',
            config={'model': 'fixture', 'model_connection': case.services.llm_client.connection_binding(),
                'sources': {'measurements': case.inputs}, 'extensions': freeze_extensions([self.package]),
                'professional_operations': 'work.v12', 'source_generation_protocol': None,
                'source_decision_protocol': None, 'context_protocol': 'working.v4',
                'memory_protocol': 'journal.v1', 'context_checkpoint_policy': 'economic.v3',
                'work_method': GOAL_WORK_METHOD, 'work_selection_policy': 'native.v3',
                'tool_contracts': 'full', 'tool_completions': {
                    'service_demo__optional': {'field': 'handoff', 'closing': True}}},
            limits={'llm_requests': 24, 'total_tokens': 900000}, services=case.services)
        (case.app.workspace.workspace / 'existing.txt').write_text('Retained bounded note.\n', encoding='utf-8')
        claim = {'summary': 'Saved partial note', 'artifacts': ['existing.txt'],
            'unresolved': ['Requested note remains unfinished.']}
        case.decisions.append(fixture._action('service_demo__optional', handoff=claim))
        self.drain()
        self.assertEqual(case.app.session.manifest.status, 'partial')
