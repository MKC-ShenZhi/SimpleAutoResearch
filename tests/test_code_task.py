from __future__ import annotations

import contextlib
import io
import hashlib
import os
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from simple_ar.core.artifacts import read_json, read_jsonl, read_text, write_json, write_text
from simple_ar.cli.main import main
from simple_ar.code_task import (
    PatchValidationError,
    analyze_code_task_failure,
    apply_patch_edits,
    build_code_task_context_pack,
    build_code_task_repo_map,
    create_code_task_batch,
    execute_code_task,
    generate_code_task_work_plan,
    generate_patch_plan,
    initialize_code_task,
    locate_code_task_context,
    probe_code_task_environment,
    propose_patch_edits,
    propose_repair_edits,
    record_plan_decision,
    run_code_task_baseline,
    run_code_task_benchmark,
    validate_code_task,
)
from simple_ar.code_task.runtime.config import CodeTaskConfigError, load_code_task_init_options
from simple_ar.code_task.editing.actions import apply_repair_actions
from simple_ar.code_task.generation.dependencies import build_dependency_advice
from simple_ar.code_task.generation.file_specs import infer_file_kind
from simple_ar.code_task.analysis.resource_static import analyze_resource_risks, resource_review_findings
from simple_ar.code_task.generation.generated_project_repair import (
    repair_generated_project_from_review,
    repair_generated_project_from_run_failure,
)
from simple_ar.code_task.generation.review import GREENFIELD_REVIEW_CONTRACT_VERSION, review_generated_project
from simple_ar.code_task.orchestration.execute import (
    _update_generated_repair_artifacts,
    _attempt_greenfield_run_repair,
)
from simple_ar.code_task.editing.patching import _write_text_atomically
from simple_ar.code_task.editing.work_plan import _normalize_work_items
from simple_ar.code_task.runtime.state import code_task_paths, implementation_file_hashes
from simple_ar.code_task.workspace.modes import (
    WorkspaceSpec, WorkspaceModeError, create_workspace, workspace_source_binding,
    _source_venv_python,
)
from simple_ar.code_task.analysis.index import build_codebase_index
from simple_ar.integrations.llm import LLMError, LLMClient, LLMSettings
from simple_ar.core.budget import BudgetLedger
from tests.code_task_fixtures import (
    TOY_PREDICT_SOURCE,
    TOY_PREDICT_WITH_PRIZE,
    keyword_code_responses,
    write_toy_project as _write_toy_project,
)


TEST_ROOT = Path(__file__).resolve().parents[1] / ".tmp_tests"
TOY_BENCHMARK_COMMAND = "python -m unittest discover -s tests"


def _invoke_generated_repair(phase, kwargs, diagnostics=None):
    diagnostics = diagnostics or {'status': 'failed', 'findings': [{'summary': 'Actual blocking diagnosis.'}]}
    if phase == 'review':
        return repair_generated_project_from_review(review_report=diagnostics, **kwargs)
    return repair_generated_project_from_run_failure(failure_analysis=diagnostics,
        stderr_text="STDERR: AttributeError: score\nSTDOUT: raw record 37", **kwargs)


class _RelatedRepairClient:
    """Fixture for public shared-selection/related-candidate behavior."""

    def __init__(self, changes):
        self.changes, self.selected = changes, list(changes)
        self.requests, self.unresolved = [], []
        self.extra_file = self.generation_error = self.before_generation_response = None
        self._budget_ledger = BudgetLedger({'llm_requests': 2})

    def ask_json(self, _system, prompt, *, label='', max_output_tokens=None):
        request = json.loads(prompt)
        self.requests.append((label, request))
        identity = f'fixture-{len(self.requests)}'
        self._budget_ledger.reserve(identity, {'llm_requests': 1})
        if label == 'implementation-selection':
            self._budget_ledger.settle(identity, {'llm_requests': 1})
            return {'files': [{'path': path, 'purpose': f'Repair {path} from actual diagnostics.'}
                for path in self.selected]}
        if self.generation_error:
            self._budget_ledger.mark_unknown(identity, reason='Interrupted response')
            raise self.generation_error
        rows = []
        for spec in request['planned_files']:
            path = spec['path']
            if path in request['original_files']:
                rows.append({'path': path, 'replacements': [
                    {'old_text': old, 'new_text': new} for old, new in self.changes.get(path, [])]})
            else:
                rows.append({'path': path, 'content': 'VALUE = 2\n'})
        if self.extra_file:
            rows.append(self.extra_file)
        if self.before_generation_response:
            self.before_generation_response()
        self._budget_ledger.settle(identity, {'llm_requests': 1})
        return {'summary': 'Related producer/consumer repair.', 'unresolved': self.unresolved, 'files': rows}


class CodeTaskTests(unittest.TestCase):
    @contextlib.contextmanager
    def _orphan_validator(self, *, after_archive=False, returncode=0, watchdog=False, preparation=False):
        from simple_ar.code_task.runtime.state import load_code_task_manifest, save_code_task_manifest
        from simple_ar.code_task.execution.runner import _safe_env
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / 'source'
            project.mkdir()
            write_text(project / 'main.py', 'VALUE = 1\n')
            write_text(project / 'check.py', f"import sys\nprint('score: 0.75')\nsys.exit({returncode})\n")
            task = root / 'task.md'
            write_text(task, 'Fix VALUE, preserving the independent checker')
            initialized = initialize_code_task(run_dir=root / 'run', code_root=project, task_file=task,
                env_mode='external', python_executable=sys.executable,
                edit_scope_allowed_patterns=('main.py',), edit_scope_protected_patterns=('check.py',))
            run = initialized.run_dir
            manifest = load_code_task_manifest(run)
            manifest['code_task']['native_binding'] = {'token': 'original-approved-contract', 'binding': {
                'contract': {'validation_command': ['python', 'check.py'], 'timeout_sec': 5},
                'python_executable': sys.executable, 'environment': _safe_env(initialized.workspace_dir)}}
            if preparation:
                store_root = root / 'attempts/native-tool-001'
                manifest['code_task']['native_binding']['binding']['environment_preparations'] = {
                    manifest['workspace']['selected_mode']: {
                        'store_root': str(store_root), 'cwd': str(initialized.workspace_dir),
                        'command': ['python', 'check.py'], 'argv': [sys.executable, 'check.py'],
                        'result_schema': {}, 'protocol': None, 'interpreter': sys.executable,
                        'environment_preparation': {'profile': {'mode': 'current', 'timeout_sec': 5},
                            'steps': [[sys.executable, '-c', 'pass']]}}}
                # Boundary fixture only: the shared physical-proof owner is stubbed by its caller.
                write_json(store_root / 'environment_setup.json', {'steps': [
                    {'process_record': {'session_id': 'same-session'}}]})
                write_json(store_root / 'environment_observation.json', {'boundary_fixture': True})
            save_code_task_manifest(run, manifest)
            ledger = BudgetLedger({'process_invocations': 2 if preparation else 1,
                'process_wall_seconds': 10 if preparation else 5}, storage_path=root / 'ledger.json')
            if preparation:
                ledger.reserve('process-' + 'a' * 32, {'process_invocations': 1, 'process_wall_seconds': 5},
                    session_id='same-session', attempt_id='native-tool-001', logical_call_id='a' * 32,
                    purpose='local_process')
                ledger.settle('process-' + 'a' * 32, {'process_invocations': 1, 'process_wall_seconds': 0.01},
                    actual_source='executor_wall_clock')
            report = initialized.task_dir / 'run/patched/execution_report.json'

            def interrupt_latest(path, value):
                if path == report:
                    raise RuntimeError('stopped before latest validator report')
                write_json(path, value)

            target = 'simple_ar.code_task.execution.runner.' + ('write_json' if after_archive else '_write_execution_result')
            failure = interrupt_latest if after_archive else RuntimeError('stopped after process settlement')
            def rejected_output(owner, stream, text):
                if text.strip():
                    owner.triggered = {'detail': 'Recorded watchdog rejection'}

            guard = patch('simple_ar.code_task.execution.runner._OutputWatchdog.observe', autospec=True,
                          side_effect=rejected_output) if watchdog else contextlib.nullcontext()
            with guard, patch(target, side_effect=failure), self.assertRaisesRegex(RuntimeError, 'stopped'):
                run_code_task_benchmark(run, command='python check.py', timeout_sec=5, skip_validation=True,
                    budget_ledger=ledger, session_id='same-session', attempt_id='native-tool-001')
            self.assertFalse(report.exists())
            invocation = next((initialized.task_dir / 'run/patched/process').glob('*/invocation.json'))
            yield root, initialized, ledger, invocation

    def test_orphan_validator_completes_domain_result_without_process_or_model_replay(self):
        from simple_ar.code_task.orchestration.execute import implementation_resume_allowed
        from simple_ar.research.implementation import ImplementationRequest, run_implementation_capability
        from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
        for after_archive in (False, True):
            proof = patch('simple_ar.research.project_environment.verify_environment_preparation',
                return_value=({'a' * 32}, [])) if after_archive else contextlib.nullcontext()
            with self.subTest(after_archive=after_archive), proof, self._orphan_validator(
                    after_archive=after_archive, preparation=after_archive) as (root, initialized, ledger, invocation):
                run = initialized.run_dir
                before, original = ledger.to_dict(), invocation.read_bytes()
                self.assertTrue(implementation_resume_allowed(run, ledger=ledger, attempt_id='native-tool-001'))
                store = ArtifactStore(root / 'attempts/native-tool-001' if after_archive else run / 'domain-attempt')
                brief = store.write_json('brief.json', {'objective': 'Fix VALUE', 'request_text': 'Fix VALUE'}, kind='research_brief')
                context = CapabilityContext(store, AttemptManifest('native-tool-001'), inputs=(brief,))
                client = Mock()
                with patch('simple_ar.core.process.subprocess.Popen') as launch, \
                        patch('simple_ar.research.implementation.implement_code_task', return_value=SimpleNamespace(
                            stop_reason='stop_point', next_action='Inspect', steps=())):
                    result = run_implementation_capability(context=context, request=ImplementationRequest(
                        run, initialized.workspace_dir, 'Original approval', client,
                        validation_command=('python', 'check.py'), validation_timeout_sec=5,
                        budget_ledger=ledger, session_id='same-session', attempt_id='native-tool-001'))
                    self.assertEqual(result.status, 'completed')
                    launch.assert_not_called()
                    client.ask_json.assert_not_called()
                domain = store.read_json('implementation.json')
                self.assertEqual(domain['status'], 'validated')
                report = read_json(initialized.task_dir / 'run/patched/execution_report.json')
                self.assertEqual(report['process']['invocation_id'], read_json(invocation)['invocation_id'])
                self.assertEqual(report['metric_values'], {'score': 0.75})
                self.assertEqual(ledger.to_dict(), before)
                self.assertEqual(invocation.read_bytes(), original)
                histories = list((initialized.task_dir / 'run/patched/attempts').glob('*/execution_report.json'))
                self.assertEqual(len(histories), 1)

    def test_orphan_validator_rejects_changed_or_unknown_execution_bindings(self):
        from simple_ar.code_task.orchestration.verification import verify_implementation
        from simple_ar.code_task.execution.runner import CodeTaskRunError
        from simple_ar.code_task.orchestration.execute import implementation_resume_allowed
        with self._orphan_validator() as (root, initialized, ledger, invocation):
            original = read_json(invocation)
            mutations = {
                'argv': lambda r: r.update(argv=[sys.executable, 'main.py']),
                'cwd': lambda r: r.update(cwd=str(root)),
                'env': lambda r: r['environment'].update(PYTHONPATH='other-source'),
                'hash': lambda r: r['input_binding']['implementation_file_hashes'].update({'main.py': 'changed'}),
                'approval': lambda r: r['input_binding']['native_binding'].update(token='other-approval'),
                'attempt': lambda r: r.update(attempt_id='other-attempt'),
                'session': lambda r: r.update(session_id='other-session'),
                'pending': lambda r: r.update(status='running'),
                'unbound_legacy': lambda r: r.pop('input_binding'),
            }
            with patch('simple_ar.core.process.subprocess.Popen') as launch:
                for label, mutate in mutations.items():
                    changed = json.loads(json.dumps(original))
                    mutate(changed)
                    write_json(invocation, changed)
                    with self.subTest(label=label):
                        self.assertFalse(implementation_resume_allowed(initialized.run_dir, ledger=ledger, attempt_id='native-tool-001'))
                        with self.assertRaises(CodeTaskRunError):
                            verify_implementation(initialized.run_dir, command=('python', 'check.py'),
                                timeout_sec=5, approval_note='Original approval', llm_client=Mock(),
                                asset_guard=lambda: {'status': 'unchanged'}, budget_ledger=ledger,
                                session_id='same-session', attempt_id='native-tool-001')
                write_json(invocation, original)
                for field, value in (('status', 'unknown'), ('logical_call_id', 'other-invocation'),
                                     ('attempt_id', 'other-attempt'), ('session_id', 'other-session'),
                                     ('actual', {'process_invocations': 1, 'process_wall_seconds': 999})):
                    data = ledger.to_dict()
                    data['entries'][0][field] = value
                    path = root / 'altered-ledger.json'
                    write_json(path, data)
                    changed_ledger = BudgetLedger.load(path)
                    with self.subTest(ledger_field=field):
                        self.assertFalse(implementation_resume_allowed(initialized.run_dir,
                            ledger=changed_ledger, attempt_id='native-tool-001'))
                        with self.assertRaises(CodeTaskRunError):
                            verify_implementation(initialized.run_dir, command=('python', 'check.py'), timeout_sec=5,
                                approval_note='Original approval', llm_client=Mock(),
                                asset_guard=lambda: {'status': 'unchanged'}, budget_ledger=changed_ledger,
                                session_id='same-session', attempt_id='native-tool-001')
                (initialized.workspace_dir / 'main.py').write_text('VALUE = 2\n')
                self.assertFalse(implementation_resume_allowed(initialized.run_dir, ledger=ledger, attempt_id='native-tool-001'))
                launch.assert_not_called()

        from simple_ar.code_task.execution.runner import recover_implementation_process
        from simple_ar.code_task.runtime.state import native_preparation_processes
        with patch('simple_ar.research.project_environment.verify_environment_preparation',
                return_value=({'a' * 32}, [])) as proof, self._orphan_validator(preparation=True) as (
                    root, initialized, ledger, invocation), patch('simple_ar.core.process.subprocess.Popen') as launch:
            run = initialized.run_dir
            manifest = read_json(run / 'manifest.json')
            view = manifest['code_task']['native_binding']['binding']['environment_preparations'][
                manifest['workspace']['selected_mode']]
            before = ledger.to_dict()
            self.assertEqual(native_preparation_processes(run, ledger=ledger,
                session_id='', attempt_id='native-tool-001'), {'a' * 32})
            self.assertEqual(proof.call_args.kwargs['session_id'], 'same-session')
            self.assertEqual(proof.call_args.args[0].root, Path(view['store_root']))
            for failure in ('unknown', 'duplicate', 'omitted'):
                with self.subTest(preparation=failure):
                    proof.side_effect = ValueError('Unresolved preparation: ' + failure)
                    self.assertFalse(implementation_resume_allowed(run, ledger=ledger, attempt_id='native-tool-001'))
                    with self.assertRaises(CodeTaskRunError):
                        recover_implementation_process(run, budget_ledger=ledger,
                            session_id='same-session', attempt_id='native-tool-001')
            proof.side_effect = None
            proof.return_value = (set(), [])  # No proven IDs: a settled preparation is not a validator.
            with self.assertRaises(CodeTaskRunError):
                recover_implementation_process(run, budget_ledger=ledger,
                    session_id='same-session', attempt_id='native-tool-001')
            self.assertFalse(implementation_resume_allowed(run, ledger=ledger, attempt_id='native-tool-001'))
            proof.return_value = ({'a' * 32}, [])
            changed = json.loads(json.dumps(manifest))
            changed['code_task']['native_binding']['binding']['environment_preparations'][
                changed['workspace']['selected_mode']]['store_root'] = str(root / 'attempts/other-attempt')
            write_json(run / 'manifest.json', changed)
            proof.reset_mock()
            with self.assertRaises(CodeTaskRunError):
                recover_implementation_process(run, budget_ledger=ledger,
                    session_id='same-session', attempt_id='native-tool-001')
            proof.assert_not_called()
            write_json(run / 'manifest.json', manifest)
            duplicate = ledger.to_dict()
            duplicate['entries'].append({**duplicate['entries'][0], 'reservation_id': 'duplicate-preparation'})
            write_json(root / 'duplicate-ledger.json', duplicate)
            with self.assertRaises(CodeTaskRunError):
                recover_implementation_process(run, budget_ledger=BudgetLedger.load(root / 'duplicate-ledger.json'),
                    session_id='same-session', attempt_id='native-tool-001')
            inherited = json.loads(json.dumps(manifest))
            inherited['code_task']['native_binding']['binding']['environment_origin'] = {'record': 'prior'}
            write_json(run / 'manifest.json', inherited)
            proof.reset_mock()
            self.assertEqual(native_preparation_processes(run, ledger=ledger,
                session_id='same-session', attempt_id='native-tool-001'), set())
            proof.assert_not_called()
            self.assertEqual(ledger.to_dict(), before)
            launch.assert_not_called()

    def test_orphan_failed_validator_remains_failed_without_rerun(self):
        from simple_ar.code_task.orchestration.verification import verify_implementation
        with self._orphan_validator(returncode=1) as (_, initialized, ledger, invocation):
            before = ledger.to_dict()
            with patch('simple_ar.core.process.subprocess.Popen') as launch, \
                    patch('simple_ar.code_task.orchestration.verification.propose_repair_edits') as repair:
                result = verify_implementation(initialized.run_dir, command=('python', 'check.py'), timeout_sec=5,
                    approval_note='Original approval', llm_client=Mock(), max_repairs=0,
                    asset_guard=lambda: {'status': 'unchanged'}, budget_ledger=ledger,
                    session_id='same-session', attempt_id='native-tool-001')
                self.assertEqual(result.validation.status, 'failed')
                launch.assert_not_called()
                repair.assert_not_called()
            self.assertEqual(ledger.to_dict(), before)

    def test_orphan_validator_retains_watchdog_rejection(self):
        from simple_ar.code_task.orchestration.verification import verify_implementation
        with self._orphan_validator(watchdog=True) as (_, initialized, ledger, invocation):
            self.assertEqual(read_json(invocation)['input_binding']['runtime_watchdog'],
                             {'detail': 'Recorded watchdog rejection'})
            with patch('simple_ar.core.process.subprocess.Popen') as launch:
                result = verify_implementation(initialized.run_dir, command=('python', 'check.py'), timeout_sec=5,
                    approval_note='Original approval', llm_client=Mock(), max_repairs=0,
                    asset_guard=lambda: {'status': 'unchanged'}, budget_ledger=ledger,
                    session_id='same-session', attempt_id='native-tool-001')
                self.assertEqual(result.validation.status, 'failed')
                self.assertEqual(result.validation.returncode, 124)
                launch.assert_not_called()

    def test_bug_validation_repair_keeps_failed_and_repaired_evidence_distinct(self) -> None:
        from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
        from simple_ar.research.implementation import ImplementationRequest, run_implementation_capability

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, task, run_dir = root / "project", root / "task.md", root / "code-task"
            _write_toy_project(project)
            write_text(project / "tests" / "test_prize.py", (
                "import unittest\n"
                "print('stdout diagnostic: prize classification')\n"
                "from spam_model import predict\n"
                "class PrizeTest(unittest.TestCase):\n"
                "    def test_prize(self):\n"
                "        self.assertEqual(predict('prize for you'), 'spam')\n"
            ))
            write_text(task, "Repair prize-message classification without changing tests.")
            # A successfully revised analysis is portable even without a plot;
            # publication must not impose the legacy figure.png output shape.
            write_text(project / "analysis.py", "# Retained analysis source\n")
            write_text(project / "README.md", "Retained analysis project.")
            write_text(project / "tests/verify_delivery.py", "# Retained delivery checker\n")
            write_text(project / "outputs/report.md", "Recorded classification observations.")
            write_json(project / "outputs/results.json", {"observations": [1]})
            initialize_code_task(run_dir=run_dir, code_root=project, task_file=task)
            workspace = code_task_paths(run_dir).workspace_dir
            inputs = ArtifactStore(root / "inputs")
            brief = inputs.write_json("brief.json", {
                "request_text": "Fix prize-message classification.",
                "objective": "The prize test must pass.",
                "hard_constraints": ["Do not edit tests."],
            }, kind="research_brief")
            store = ArtifactStore(root / "attempt")
            context = CapabilityContext(
                store=store, attempt=AttemptManifest("implement-001"),
                inputs=(brief,), input_store=inputs,
            )

            class Client(_FakeCodeTaskClient):
                def ask_json(self, system: str, user: str, *, label: str = "") -> dict[str, object]:
                    if label == "code-task-repair":
                        # A stderr failure summary must not hide diagnostics
                        # emitted by a runner to stdout.
                        self_outer.assertIn("stdout diagnostic: prize classification", user)
                        self_outer.assertIn("AssertionError", user)
                        return {"summary": "Correct the near-miss keyword", "edits": [{
                            "path": "spam_model.py", "old": "('win', 'prizex')",
                            "new": "('win', 'prize')", "reason": "The failed test uses prize.",
                        }], "validation": ["Run the existing tests"], "risks": []}
                    result = super().ask_json(system, user, label=label)
                    if label == "code-task-propose-edits":
                        edit = result["edits"][0]
                        edit["new"] = edit["new"].replace("('win', 'prize')", "('win', 'prizex')")
                    return result

            self_outer = self
            client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat"))
            with patch.object(LLMClient, "ask_json", side_effect=Client().ask_json):
                result = run_implementation_capability(context=context, request=ImplementationRequest(
                    run_dir, workspace, "Authorize isolated bug fix", client,
                    validation_command=(sys.executable, "-m", "unittest", "discover", "-s", "tests"),
                    validation_timeout_sec=30, max_repairs=1,
                ))
            self.assertEqual(result.status, "completed", result.diagnostics)
            payload = read_json(root / "attempt" / "implementation.json")
            self.assertEqual(payload["validation"]["status"], "passed")
            self.assertEqual([row["outcome"] for row in payload["validation_repairs"]], ["passed"])
            failed = Path(payload["validation_repairs"][0]["failed_validation_report"])
            self.assertEqual(read_json(failed)["status"], "failed")
            self.assertEqual(read_json(run_dir / "code_task" / "run" / "patched" / "execution_report.json")["status"], "passed")
            refs = payload["artifact_refs"]
            delivery = read_json(store.root / refs["code_analysis"]["path"])
            self.assertEqual(delivery["figures"], [])
            self.assertIn("analysis.py", delivery["files"])
            self.assertEqual(delivery["results"], {"observations": [1]})
            self.assertNotEqual(refs["edit_proposal"]["path"], refs["repair_proposal"]["path"])
            self.assertIn("post_repair", refs["review"]["path"])
            self.assertEqual(read_json(store.root / refs["validation_failure_1"]["path"])["status"], "failed")
            self.assertIn("prize", (workspace / "spam_model.py").read_text(encoding="utf-8"))

    def test_implementation_authorization_preserves_rejections_and_dry_run(self):
        from simple_ar.code_task.orchestration.execute import implement_code_task, CodeTaskExecuteResult
        for reason in ("patch_apply_failed", "edit_budget_rejected", "patch_rollback_failed", "no_edits_proposed", "plan_rejected"):
            for corrections in (0, 1):
                first = CodeTaskExecuteResult(Path("run"), ("failed",), reason, "Inspect", Path("summary.md"))
                second = CodeTaskExecuteResult(Path("run"), ("still rejected",), reason, "Inspect", Path("summary.md"))
                messages = []
                with self.subTest(reason=reason, corrections=corrections), patch(
                    "simple_ar.code_task.orchestration.execute.execute_code_task", side_effect=[first, second],
                ) as call:
                    result = implement_code_task(Path("run"), approval_note="approved",
                        proposal_corrections=corrections, message_callback=messages.append,
                        edit_budget_overrides={"max_new_chars": 32000})
                repeated = corrections and reason in {"patch_apply_failed", "edit_budget_rejected"}
                self.assertEqual(call.call_count, 2 if repeated else 1)
                self.assertEqual(result.steps, ("failed", "still rejected") if repeated else ("failed",))
                self.assertEqual(bool(messages), bool(repeated))
                for recorded in call.call_args_list:
                    self.assertEqual(recorded.kwargs["edit_budget_overrides"], {"max_new_chars": 32000})
                if repeated:
                    self.assertIn("unchanged limits", messages[0])
        with patch("simple_ar.code_task.orchestration.execute.execute_code_task", side_effect=LLMError("Unknown use")) as call:
            with self.assertRaisesRegex(LLMError, "Unknown use"):
                implement_code_task(Path("run"), approval_note="approved", proposal_corrections=1)
            call.assert_called_once()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_toy_project(root / "project")
            write_text(root / "task.md", "Improve the existing classifier.")
            run_dir = root / "run"
            initialize_code_task(run_dir=run_dir, code_root=root / "project", task_file=root / "task.md")
            execute_code_task(run_dir, to_step="plan", baseline_policy="skip", use_llm=False)
            before = read_json(run_dir / "manifest.json")["plan"]
            result = implement_code_task(run_dir, approval_note="Isolated implementation only", use_llm=False, dry_run=True)
            self.assertEqual(result.stop_reason, "dry_run")
            self.assertEqual(read_json(run_dir / "manifest.json")["plan"], before)
            for decision, expected in (("reject", "plan_rejected"), ("revise", "plan_revision_requested")):
                record_plan_decision(run_dir, decision=decision)
                before = read_json(run_dir / "manifest.json")["plan"]
                result = implement_code_task(run_dir, approval_note="Isolated implementation only", use_llm=False)
                self.assertEqual(result.stop_reason, expected)
                self.assertEqual(read_json(run_dir / "manifest.json")["plan"], before)
            self.assertFalse((run_dir / "code_task" / "meta" / "applied_edits.json").exists())

    def test_native_verification_recovers_saved_validator_without_running_it_again(self):
        from simple_ar.code_task.orchestration.verification import verify_implementation
        from simple_ar.code_task.runtime.state import load_code_task_manifest, save_code_task_manifest, implementation_file_hashes
        import os
        import shlex
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / 'source'
            project.mkdir()
            (project / 'main.py').write_text('VALUE = 1\n')
            task = root / 'task.md'
            task.write_text('Fix VALUE')
            initialized = initialize_code_task(run_dir=root / 'run', code_root=project, task_file=task)
            manifest = load_code_task_manifest(initialized.run_dir)
            manifest['code_task']['native_binding'] = {'token': 'approved-original-contract'}
            save_code_task_manifest(initialized.run_dir, manifest)
            command = (sys.executable, 'check.py')
            command_text = subprocess.list2cmdline(command) if os.name == 'nt' else shlex.join(command)
            report = initialized.task_dir / 'run/patched/execution_report.json'
            write_json(report, {'native_binding': manifest['code_task']['native_binding'],
                'implementation_file_hashes': implementation_file_hashes(initialized.run_dir),
                'command_text': command_text, 'timeout_sec': 20, 'status': 'passed',
                'metric_values': {}, 'returncode': 0, 'timed_out': False})
            with patch('simple_ar.code_task.orchestration.verification.run_code_task_benchmark') as run, \
                    patch('simple_ar.code_task.orchestration.verification.propose_repair_edits') as repair:
                result = verify_implementation(initialized.run_dir, command=command,
                    approval_note='Original approved validator', llm_client=Mock(), timeout_sec=20,
                    max_repairs=1, asset_guard=lambda: {'status': 'unchanged'})
                self.assertEqual(result.validation.status, 'passed')
                run.assert_not_called()
                repair.assert_not_called()
                (initialized.workspace_dir / 'main.py').write_text('VALUE = 9\n')
                with self.assertRaisesRegex(ValueError, 'different candidate'):
                    verify_implementation(initialized.run_dir, command=command,
                        approval_note='Original approved validator', llm_client=Mock(), timeout_sec=20,
                        max_repairs=1, asset_guard=lambda: {'status': 'unchanged'})
                run.assert_not_called()

    def test_technical_verification_stops_before_repair_when_protected_assets_change(self):
        from simple_ar.code_task.orchestration.verification import verify_implementation, apply_verified_repair
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            validation = SimpleNamespace(status="failed")
            ledger = BudgetLedger({"process_invocations": 2})
            before = ledger.to_dict()
            for status, limits in (("changed", None), ("unchanged", {"process_invocations": 0}),
                                   ("unchanged", {"process_invocations": 2, "process_wall_seconds": 1})):
                # With no remaining process allowance, even an intact failed
                # check must not buy a repair that cannot be verified.
                budget = ledger if limits is None else BudgetLedger(limits)
                with self.subTest(status=status, limits=limits), patch(
                    "simple_ar.code_task.orchestration.verification.run_code_task_benchmark", return_value=validation,
                ) as run, patch("simple_ar.code_task.orchestration.verification.propose_repair_edits") as propose:
                    result = verify_implementation(root, command=(sys.executable, "check.py"),
                        approval_note="Authorized technical validation", llm_client=Mock(), max_repairs=1,
                        budget_ledger=budget, asset_guard=lambda: {"status": status})
                self.assertIs(result.validation, validation)
                self.assertEqual(result.repairs, ())
                propose.assert_not_called()
                run.assert_called_once()
                self.assertIs(run.call_args.kwargs["budget_ledger"], budget)
            self.assertEqual(ledger.to_dict(), before)
            with patch("simple_ar.code_task.orchestration.verification.apply_patch_edits") as apply:
                with self.assertRaisesRegex(ValueError, "approval"):
                    apply_verified_repair(root, Mock(), approval_note="", llm_client=Mock())
                apply.assert_not_called()

    def test_repair_accounting_keeps_review_and_run_facts_out_of_implementation(self):
        from simple_ar.code_task.orchestration.execute import (
            _greenfield_repair_available, _record_greenfield_repair_result,
        )

        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            implementation = {"status": "review_failed", "review_status": "failed"}
            write_json(root / "manifest.json", {"workflow": "code_task", "implementation": implementation})
            for phase, status in (("review", "failed"), ("run", "patched")):
                self.assertTrue(_greenfield_repair_available(root, 1, phase=phase))
                _record_greenfield_repair_result(root, phase=phase, repair={"status": status, "changed_files": ["model.py"]})
                self.assertFalse(_greenfield_repair_available(root, 1, phase=phase))
            manifest = read_json(root / "manifest.json")
            self.assertEqual(manifest["implementation"], implementation)
            self.assertEqual(manifest["repair"]["review_repair_count"], 1)
            self.assertEqual(manifest["repair"]["run_repair_count"], 1)
            self.assertEqual(manifest["repair"]["latest_review_repair"], "code_task/meta/review_repair.json")
            self.assertNotIn("effective_status", manifest["repair"])

    def test_execute_keeps_shared_budget_and_local_usage_through_edits_and_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, task, run_dir = root / "project", root / "task.md", root / "run"
            _write_toy_project(project)
            task.write_text("Improve spam prediction for prize messages.", encoding="utf-8")
            initialize_code_task(run_dir=run_dir, code_root=project, task_file=task,
                                 benchmark_command="python -m unittest discover -s tests")
            ledger = BudgetLedger({"llm_requests": 12, "total_tokens": 50000,
                                   "process_invocations": 1, "process_wall_seconds": 5})
            observed = []
            client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat", max_output_tokens=2048),
                               budget_ledger=ledger, budget_session_id="research", budget_attempt_id="implement-1",
                               usage_callback=observed.append)
            fake = _FakeCodeTaskClient()
            responses = [{
                "choices": [{"message": {"content": json.dumps(fake.ask_json("", "", label=label))}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
            } for label in ("code-task-work-plan", "code-task-plan", "code-task-propose-edits")]
            responses += [{
                "choices": [{"message": {"content": '{"findings": []}'}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
            } for _ in range(8)]
            with patch("simple_ar.integrations.llm._call_openai_sdk", side_effect=responses), patch.object(
                LLMClient, "from_env", side_effect=AssertionError("Injected execution must not reload credentials"),
            ):
                first = execute_code_task(run_dir, llm_client=client, baseline_policy="skip", timeout_sec=5)
                self.assertEqual(first.stop_reason, "approval_required")
                record_plan_decision(run_dir, decision="approve", note="Test authorization", reviewer="test")
                proposal = execute_code_task(run_dir, llm_client=client, baseline_policy="skip", timeout_sec=5)
                self.assertEqual(proposal.stop_reason, "proposal_review_required")
                result = execute_code_task(
                    run_dir, llm_client=client, baseline_policy="skip", timeout_sec=5,
                    to_step="run", apply_proposed_edits=True,
                    budget_ledger=ledger, session_id="research", attempt_id="implement-1",
                )
            self.assertEqual(result.stop_reason, "completed")
            self.assertGreater(len(observed), 3)  # Planning, edits and actual layered reviews.
            self.assertEqual(ledger.remaining("llm_requests"), 12 - len(observed))
            self.assertEqual(ledger.remaining("process_invocations"), 0)
            self.assertTrue(all(entry.attempt_id == "implement-1" for entry in ledger.entries))
            usage = read_jsonl(run_dir / "code_task/meta/llm_usage.jsonl")
            self.assertEqual(len(usage), len(observed))
            self.assertEqual(sum(row["label"] == "code-task-work-plan" for row in usage), 0)
            self.assertEqual(sum(row["label"] == "code-task-plan" for row in usage), 1)
            self.assertTrue(any(row["stage"] == "code_task.review" for row in usage))
            self.assertEqual(read_json(run_dir / "code_task/run/patched/execution_report.json")["status"], "passed")

    def test_atomic_text_write_retries_transient_destination_lock(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            target = Path(tmp) / "result.txt"
            target.write_text("old\n", encoding="utf-8")
            from simple_ar.core.artifacts import os
            original_replace = os.replace
            attempts = 0

            def flaky_replace(source: Path, destination: Path) -> None:
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError("transient destination lock")
                return original_replace(source, destination)

            with patch.object(os, "replace", new=flaky_replace):
                _write_text_atomically(target, "new\n")

            self.assertEqual(attempts, 3)
            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(list(target.parent.glob('.*.tmp')), [])

    def test_related_patch_write_failure_restores_unit_and_keeps_diagnostics(self) -> None:
        for failure in ('write', 'interrupt', 'restore'):
            with self.subTest(failure=failure), _temporary_root() as root:
                _, _, run_dir = _initialize_toy_task(root, '# Task\n\nApply a related patch.\n')
                _approve_offline_patch_plan(run_dir)
                workspace = code_task_paths(run_dir).workspace_dir
                original = b'VALUE = 1\r\n'
                (workspace / 'first.py').write_bytes(original)
                write_text(workspace / 'last.py', 'VALUE = 1\n')
                proposal = root / 'related.json'
                write_json(proposal, {'edits': [
                    {'path': 'first.py', 'old': 'VALUE = 1', 'new': 'VALUE = 2'},
                    {'path': 'new.py', 'old': '', 'new': 'NEW = 1\n'},
                    {'path': 'last.py', 'old': 'VALUE = 1', 'new': 'VALUE = 2'}]})

                def write_or_fail(path, text):
                    if path.name == 'last.py':
                        raise (KeyboardInterrupt('interrupted unit') if failure == 'interrupt'
                            else OSError('failed unit write'))
                    _write_text_atomically(path, text)

                def restore_or_fail(path, data):
                    if path.name == 'first.py':
                        raise OSError('restore destination locked')
                    _write_text_atomically(path, data)

                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch('simple_ar.code_task.editing.patching._write_text_atomically',
                        side_effect=write_or_fail))
                    if failure == 'restore':
                        stack.enter_context(patch('simple_ar.code_task.editing.snapshots.write_atomically',
                            side_effect=restore_or_fail))
                    with self.assertRaises(KeyboardInterrupt if failure == 'interrupt' else OSError) as caught:
                        apply_patch_edits(run_dir, edits_file=proposal)
                self.assertFalse((run_dir / 'code_task/meta/applied_edits.json').exists())
                if failure == 'restore':
                    self.assertIn('failed unit write', str(caught.exception))
                    self.assertIn('restore destination locked', str(caught.exception.__notes__))
                    self.assertIn('byte check failed', str(caught.exception.__notes__))
                else:
                    self.assertEqual((workspace / 'first.py').read_bytes(), original)
                self.assertFalse((workspace / 'new.py').exists())
                self.assertEqual(read_text(workspace / 'last.py'), 'VALUE = 1\n')
                snapshots = list((workspace.parent / 'meta/file_snapshots').glob('*/snapshot.json'))
                self.assertEqual(len(snapshots), 1)
                self.assertEqual(len(read_json(snapshots[0])['files']), 3)

    def test_python_file_in_runtime_named_directory_remains_source(self) -> None:
        self.assertEqual(infer_file_kind("artifacts/artifacts_io.py"), "source")
        self.assertEqual(
            infer_file_kind("report/report_generator.py", "output_placeholder"),
            "source",
        )
        self.assertEqual(infer_file_kind("artifacts/results.json", "output_placeholder"), "output_placeholder")

    def test_repair_add_file_can_populate_empty_placeholder(self) -> None:
        with _temporary_root() as root:
            target = root / "artifacts" / "artifacts_io.py"
            target.parent.mkdir(parents=True)
            target.touch()

            result = apply_repair_actions(
                root,
                [{"action": "add_file", "path": "artifacts/artifacts_io.py", "content": "def save():\n    return 1\n"}],
            )

            self.assertEqual(result["status"], "patched")
            self.assertIn("def save", read_text(target))

    def test_repair_rejects_path_below_existing_file(self) -> None:
        with _temporary_root() as root:
            write_text(root / "artifacts_io.py", "VALUE = 1\n")

            result = apply_repair_actions(
                root,
                [
                    {
                        "action": "add_file",
                        "path": "artifacts_io.py/submission_writer.py",
                        "content": "VALUE = 2\n",
                    }
                ],
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["rejected_actions"][0]["reason"], "parent_path_is_file")
            for unsafe in ('/artifacts_io.py', 'C:/artifacts_io.py',
                           'C:artifacts_io.py', '../artifacts_io.py'):
                with self.subTest(path=unsafe):
                    rejected = apply_repair_actions(root, [
                        {'action': 'rewrite_file', 'path': unsafe, 'content': 'VALUE = 99\n'}])
                    self.assertEqual(rejected['status'], 'failed')
                    self.assertEqual(rejected['rejected_actions'][0]['reason'], 'missing_or_unsafe_path')
                    self.assertEqual(read_text(root/'artifacts_io.py'), 'VALUE = 1\n')

    def test_greenfield_review_does_not_infer_hidden_labels_from_helper_names(self) -> None:
        with _temporary_root() as root:
            write_text(root / "main.py", "from strategies import select_qbc\n")
            write_text(
                root / "strategies.py",
                (
                    "from sklearn.linear_model import LogisticRegression\n"
                    "def _as_arrays(pool_state):\n"
                    "    return pool_state.X_train, pool_state.y_train\n"
                    "def select_qbc(pool_state, seed):\n"
                    "    X, y = _as_arrays(pool_state)\n"
                    "    model = LogisticRegression(max_iter=1000)\n"
                    "    model.fit(X, y)\n"
                    "    return 0\n"
                ),
            )

            report = _generated_review(root,
                [
                    {"path": "main.py", "line_count": 1, "mode": "llm"},
                    {"path": "strategies.py", "line_count": 8, "mode": "llm"},
                ],
                result_schema={"primary_metric": "score", "required_metrics": []},
                resource_plan={
                    "max_files": 10,
                    "max_generated_lines": 200,
                },

                contract={
                    "task": "Run a pool-based active learning query strategy benchmark with an unlabeled pool and label budget.",
                    "success_criteria": [],
                },
            )

            categories = {item.get("category") for item in report.get("findings", [])}
            self.assertNotIn("hidden_label_acquisition_leakage", categories)
            self.assertFalse(any(item.get("severity") == "blocking" for item in report["findings"]))

    def test_greenfield_review_blocks_return_contract_mismatch(self) -> None:
        with _temporary_root() as root:
            write_text(
                root / "producer.py",
                (
                    "from __future__ import annotations\n\n"
                    "def run_all_experiments(config):\n"
                    "    bundle = {'seed_summaries': [], 'required_metrics': {}}\n"
                    "    return bundle\n"
                ),
            )
            write_text(
                root / "metrics.py",
                (
                    "from __future__ import annotations\n\n"
                    "from typing import Sequence\n\n"
                    "class SeedSummary:\n"
                    "    pass\n\n"
                    "def aggregate_seed_summaries(seed_summaries: Sequence[SeedSummary]):\n"
                    "    return list(seed_summaries)\n"
                ),
            )
            write_text(
                root / "main.py",
                (
                    "from __future__ import annotations\n\n"
                    "from producer import run_all_experiments\n"
                    "from metrics import aggregate_seed_summaries\n\n"
                    "def main():\n"
                    "    seed_summaries = run_all_experiments({})\n"
                    "    return aggregate_seed_summaries(seed_summaries)\n"
                ),
            )

            report = _generated_review(root,
                [
                    {"path": "producer.py", "mode": "llm"},
                    {"path": "metrics.py", "mode": "llm"},
                    {"path": "main.py", "mode": "llm"},
                ],
                result_schema={"primary_metric": "accuracy", "required_metrics": ["accuracy"]},
                resource_plan={"max_files": 10, "max_generated_lines": 300},
                contract={"task": "Run an experiment and aggregate seed summaries."},
            )

            categories = {item.get("category") for item in report.get("findings", [])}
            self.assertIn("return_contract_mismatch", categories)

    def test_resource_static_reports_observations_without_model_specific_policy(self) -> None:
        with _temporary_root() as root:
            write_text(
                root / "model_loop.py",
                (
                    "from sklearn.linear_model import LogisticRegression\n"
                    "def run(X, y):\n"
                    "    for seed in range(5):\n"
                    "        model = LogisticRegression(max_iter=1000)\n"
                    "        model.fit(X, y)\n"
                    "    return model\n"
                ),
            )

            analysis = analyze_resource_risks(root)
            self.assertEqual(analysis["nested_fit_call_count"], 1)
            self.assertNotIn("risk_score", analysis)
            findings = resource_review_findings(root)
            self.assertEqual([item["severity"] for item in findings], ["warning"])
            before = analysis
            source = read_text(root / "model_loop.py")
            write_text(root / "model_loop.py", source.replace("LogisticRegression", "AnotherEstimator") + "# StandardScaler budget cap\n")
            self.assertEqual(analyze_resource_risks(root), before)
            self.assertTrue(any(item.get("category") == "resource_fit_loop_risk" for item in findings))

    def test_repair_action_rewrite_function_preserves_method_indentation(self) -> None:
        with _temporary_root() as root:
            module = root / "worker.py"
            write_text(
                module,
                (
                    "class Worker:\n"
                    "    def run(self):\n"
                    "        return 1\n"
                ),
            )

            result = apply_repair_actions(
                root,
                [
                    {
                        "action": "rewrite_function",
                        "path": "worker.py",
                        "function_name": "run",
                        "new_source": "def run(self):\n    return 2\n",
                        "rationale": "Fix method body.",
                    }
                ],
            )

            self.assertEqual(result["status"], "patched")
            self.assertIn("    def run(self):\n        return 2", read_text(module))

    def test_dependency_advice_scans_environment_beyond_static_hints(self) -> None:
        from simple_ar.code_task.generation.dependencies import render_dependency_advice_markdown
        from simple_ar.code_task.generation.writer import _dependency_advice_for_prompt
        distributions = [SimpleNamespace(metadata={'Name': 'Different-Library'}, version='1.2',
            read_text=lambda name: 'unrelated_import\n', files=[]),
            SimpleNamespace(metadata={'Name': 'Another.Distribution'}, version='3.4',
                read_text=lambda name: None, files=[Path('actual_namespace/__init__.py')])]
        with patch('importlib.metadata.distributions', return_value=distributions) as scan, \
                patch('importlib.util.find_spec', side_effect=AssertionError('Metadata observation must not import code')) as imports:
            base = build_dependency_advice('Use unrelated_import. Investigate actual_namespace, not Different-Library-extra.')
            self.assertEqual(base['selection_policy'], 'observed_metadata_and_explicit_names')
            self.assertEqual(base['environment_package_count'], 2)
            self.assertEqual({row['package'] for row in base['packages']}, {'Different-Library', 'Another.Distribution'})
            self.assertTrue(all(row['importable'] is None for row in base['packages']))
            self.assertEqual(base['missing_recommended'], [])
            self.assertEqual(base['install_command'], '')
            # A domain or resource word does not choose packages, algorithms or fallback datasets.
            ordinary = build_dependency_advice('Classification, baseline, neural GPU, plots and statistics.')
            self.assertEqual(ordinary['packages'], [])
            self.assertEqual(build_dependency_advice('Different-Library-extra')['packages'], [])
            scan.reset_mock()
            other = str(Path(sys.executable).absolute().parent / 'separate-task-env/python')
            foreign = build_dependency_advice('Use unrelated_import.', python_executable=other)
            scan.assert_not_called()
            imports.assert_not_called()
            self.assertEqual(foreign['environment_scope'], 'target_not_inspected')
            self.assertIsNone(foreign['environment_package_count'])
            self.assertEqual(foreign['packages'], [])
            self.assertFalse(foreign['target_matches_inspecting_interpreter'])
            self.assertEqual(_dependency_advice_for_prompt(foreign)['target_python_executable'], other)
        legacy = {'schema_version': 'code_task_dependency_advice.v1', 'packages': [{'package': 'saved-library',
            'import_name': 'saved_import', 'status': 'missing', 'role': 'Original recorded advice'}],
            'installed_packages': ['old-default-library'], 'notes': ['Original limitation'],
            'install_command': 'pip install saved-library'}
        markdown = render_dependency_advice_markdown(legacy)
        self.assertIn('saved-library', markdown)
        self.assertIn('Original limitation', markdown)
        self.assertIn('not authorization', markdown)
        self.assertIn('saved_import', markdown)
        self.assertIn('Recorded installed distributions: old-default-library', markdown)
        self.assertNotIn('Explicitly named installed distributions', markdown)

    def test_generated_repair_shared_unit_preserves_diagnostics_and_obligations(self) -> None:
        for phase in ('review', 'run'):
            with self.subTest(phase=phase), _temporary_root() as root:
                project = root / 'generated_project'
                originals = {'src/producer.py': "def produce():\n    return {'score': 1}\n",
                    'src/report.py': "def render(summary):\n    return summary.score\n",
                    'app.py': '# Preserve orchestration.\n'}
                for path, text in originals.items():
                    write_text(project / path, text)
                original_bytes = {path: (project / path).read_bytes() for path in originals}
                client = _RelatedRepairClient({'src/producer.py': [('score', 'accuracy')],
                    'src/report.py': [('summary.score', "summary['accuracy']")], 'app.py': []})
                diagnostics = {'status': 'failed', 'findings': [{'summary': 'Producer/report mismatch.'}],
                    'failure_graph_data': {'primary_signal': 'attribute mismatch',
                        'traceback_files': ['src/report.py'], 'artifact_scan': {'expected_path': 'artifacts/results.json'}}}
                kwargs = dict(project_dir=project, output_path=root / f'{phase}_repair.json',
                    code_artifacts={'generated_files': [{'path': 'src/producer.py', 'mode': 'fallback'}]},
                    architecture_plan={'files': [{'path': 'src/report.py', 'purpose': 'Report consumer',
                        'dependencies': ['src/producer.py'], 'public_api': ['render(summary)']}]},
                    result_schema={'required_metrics': ['accuracy']}, contract={'objective': 'Measure accuracy.'},
                    dependency_advice={'notes': ['No install authorized.']},
                    previous_repair_context='Prior app-only repair failed.', client=client, attempt_id='round-1')
                result = _invoke_generated_repair(phase, kwargs, diagnostics)
                self.assertEqual(result['status'], 'patched', result)
                self.assertEqual(set(result['changed_files']), {'src/producer.py', 'src/report.py'})
                self.assertEqual(read_text(project / 'app.py'), originals['app.py'])
                self.assertEqual(len(client.requests), 2)
                self.assertEqual(client._budget_ledger.remaining('llm_requests'), 0)
                selection, generation = [request for _, request in client.requests]
                self.assertIn('visible_project_paths', selection)
                self.assertEqual(set(generation['original_files']), set(originals))
                for path, text in originals.items():
                    self.assertEqual(generation['original_files'][path]['text'], original_bytes[path].decode('utf-8'))
                binding = generation['source_windows'][0]
                for key in ('architecture_plan', 'result_schema', 'contract', 'dependency_advice', 'previous_repair_context'):
                    self.assertEqual(binding[key], kwargs[key])
                self.assertEqual(binding['diagnostics']['review_report' if phase == 'review' else 'failure_analysis'], diagnostics)
                dependencies = generation['source_windows'][-1]['files']
                self.assertEqual({row['path'] for row in dependencies}, set(originals))
                self.assertTrue(all(set(row) == {'path', 'actual_dependency_apis'} for row in dependencies))
                self.assertIn('produce()', str(dependencies))
                self.assertFalse(generation['execution']['performed'])
                self.assertEqual(result['snapshot']['captured_count'], 2)
                self.assertTrue(all(row['mode'] == f'llm_{phase}_repair_actions'
                    for row in result['regenerated_files']))
                for row in result['regenerated_files']:
                    actions = row['edit_application']['applied_actions']
                    self.assertEqual(len(actions), 1)
                    self.assertEqual(actions[0]['before_public_api'], actions[0]['after_public_api'])
                if phase == 'run':
                    self.assertIn('raw record 37', binding['diagnostics']['runtime_output'])
                    plan = read_json(Path(result['repair_plan']))
                    self.assertEqual(plan['schema_version'], 'code_task_repair_plan.v1')
                    self.assertIn('STDERR', plan['observed_error'])
                    self.assertEqual({row['path'] for row in plan['target_files']}, set(originals))
                    atomic = read_json(Path(result['atomic_patch_set']))
                    self.assertEqual(atomic['status'], 'patched')
                    self.assertEqual(set(atomic['changed_files']), set(result['changed_files']))

    def test_generated_repair_receipt_binds_completed_project_and_inputs(self) -> None:
        with _temporary_root() as root:
            project = root / 'project'
            write_text(project / 'module.py', 'VALUE = 1\n')
            client = _RelatedRepairClient({'module.py': [('VALUE = 1', 'VALUE = 2')]})
            kwargs = dict(project_dir=project, output_path=root / 'run_repair.json', client=client, attempt_id='round-1')
            result = _invoke_generated_repair('run', kwargs)
            self.assertEqual(result['status'], 'patched', result)
            receipt = root / 'repair_attempts/run/round-1/result.json'
            saved, ledger = receipt.read_bytes(), client._budget_ledger.to_dict()
            self.assertEqual(_invoke_generated_repair('run', kwargs), result)
            for content in ('VALUE = 3\n', 'VALUE = 1\n'):
                write_text(project / 'module.py', content)
                rejected = _invoke_generated_repair('run', kwargs)
                self.assertEqual(rejected['status'], 'failed')
                self.assertEqual(rejected['changed_files'], [])
                self.assertIn('Project bytes/inventory changed', str(rejected['unresolved_errors']))
                self.assertEqual(read_text(project / 'module.py'), content)
            write_text(project / 'module.py', 'VALUE = 2\n')
            rejected = _invoke_generated_repair('run', kwargs, {'status': 'different failure'})
            self.assertEqual(rejected['status'], 'failed')
            self.assertIn('inputs changed', str(rejected['unresolved_errors']))
            write_text(project / 'additional.py', 'EXTRA = 1\n')
            self.assertEqual(_invoke_generated_repair('run', kwargs)['status'], 'failed')
            self.assertEqual(len(client.requests), 2)
            self.assertEqual(client._budget_ledger.to_dict(), ledger)
            self.assertEqual(receipt.read_bytes(), saved)
            completed = read_json(receipt)
            completed['completed'] = False
            write_json(receipt, completed)
            rejected = _invoke_generated_repair('run', kwargs)
            self.assertEqual(rejected['status'], 'failed')
            self.assertIn('completed consumer result', str(rejected['unresolved_errors']))
            self.assertEqual(len(client.requests), 2)

    def test_generated_repair_candidate_rejection_is_atomic_across_files(self) -> None:
        for phase in ('review', 'run'):
            for failure in ('api', 'syntax', 'unresolved', 'extra_path', 'ambiguous', 'traceback'):
                with self.subTest(phase=phase, failure=failure), _temporary_root() as root:
                    project = root / 'project'
                    write_text(project / 'first.py', 'VALUE = 1\n')
                    second_path = 'main.py' if failure == 'traceback' else 'second.py'
                    write_text(project / second_path, 'def measure(x):\n    return x + 1\n')
                    before = {path: path.read_bytes() for path in project.glob('*.py')}
                    changes = [('return x + 1', 'return x + 2')]
                    if failure in ('api', 'syntax'):
                        changes = [('def measure(x):', 'def measure(x, required):' if failure == 'api' else 'def measure(:')]
                    if failure == 'ambiguous':
                        changes = [('absent original span', 'replacement')]
                    if failure == 'traceback':
                        changes = [('return x + 1',
                            'try:\n        return x + 1\n    except Exception:\n        return 0')]
                    client = _RelatedRepairClient({'first.py': [('VALUE = 1', 'VALUE = 2')], second_path: changes})
                    # Review may add a file before a later candidate is rejected.
                    # Whole-unit rollback must remove it as well as restore edits.
                    if phase == 'review':
                        client.selected.insert(1, 'new.py')
                    client.unresolved = ['Missing scientific condition.'] if failure == 'unresolved' else []
                    client.extra_file = {'path': 'outside.py', 'content': 'VALUE = 9\n'} if failure == 'extra_path' else None
                    result = _invoke_generated_repair(phase,
                        dict(project_dir=project, output_path=root / f'{phase}_repair.json', client=client))
                    self.assertEqual(result['status'], 'failed', result)
                    self.assertEqual(result['changed_files'], [])
                    self.assertEqual(result.get('regenerated_files', []), [])
                    for path, raw in before.items():
                        self.assertEqual(path.read_bytes(), raw)
                    self.assertFalse((project / 'outside.py').exists())
                    self.assertFalse((project / 'new.py').exists())
                    self.assertEqual(len(client.requests), 2)
                    if failure == 'api':
                        self.assertIn('public_api_signature_would_change', str(result['unresolved_errors']))
                    if failure == 'traceback':
                        self.assertIn('broad_exception_without_traceback', str(result['unresolved_errors']))
                    if failure in ('api', 'traceback'):
                        self.assertEqual(result['snapshot']['captured_count'], 3 if phase == 'review' else 2)
                        self.assertEqual(result['snapshot']['restored_count'], result['snapshot']['captured_count'])
                    if phase == 'run':
                        self.assertEqual(read_json(Path(result['atomic_patch_set']))['status'], 'failed')

    def test_generated_repair_scope_and_source_races_do_not_publish(self) -> None:
        for failure in ('new_runtime_file', 'private', 'traversal', 'non_python', 'over_scope', 'source_race'):
            with self.subTest(failure=failure), _temporary_root() as root:
                project = root / 'project'
                write_text(project / 'module.py', 'VALUE = 1\n')
                client = _RelatedRepairClient({'module.py': [('VALUE = 1', 'VALUE = 2')]})
                if failure == 'over_scope':
                    client.selected = [f'module{i}.py' for i in range(9)]
                    for path in client.selected:
                        write_text(project / path, 'VALUE = 1\n')
                elif failure == 'source_race':
                    client.before_generation_response = lambda: write_text(project / 'module.py', 'USER_CHANGE = 7\n')
                else:
                    client.selected = [{'new_runtime_file': 'new.py', 'private': '.codex/private.py',
                        'traversal': '../outside.py', 'non_python': 'config.json'}[failure]]
                result = _invoke_generated_repair('run',
                    dict(project_dir=project, output_path=root / 'run_repair.json', client=client))
                self.assertEqual(result['status'], 'failed', result)
                self.assertEqual(result['changed_files'], [])
                self.assertEqual(len(client.requests), 2 if failure == 'source_race' else 1)
                self.assertEqual(read_text(project / 'module.py'),
                    'USER_CHANGE = 7\n' if failure == 'source_race' else 'VALUE = 1\n')
                self.assertFalse((root / 'outside.py').exists())

    def test_generated_repair_interrupted_generation_and_application_are_not_replayed(self) -> None:
        from simple_ar.integrations.llm import LLMStreamError
        for interruption in ('unknown_generation', 'candidate_application', 'application_after_write'):
            with self.subTest(interruption=interruption), _temporary_root() as root:
                project = root / 'project'
                write_text(project / 'module.py', 'VALUE = 1\n')
                client = _RelatedRepairClient({'module.py': [('VALUE = 1', 'VALUE = 2')]})
                if interruption == 'application_after_write':
                    write_text(project / 'second.py', 'VALUE = 1\n')
                    client = _RelatedRepairClient({'module.py': [('VALUE = 1', 'VALUE = 2')],
                        'second.py': [('VALUE = 1', 'VALUE = 2')]})
                kwargs = dict(project_dir=project, output_path=root / 'run_repair.json', client=client, attempt_id='round-1')
                if interruption == 'unknown_generation':
                    client.generation_error = LLMStreamError('Incomplete generation response')
                    self.assertEqual(_invoke_generated_repair('run', kwargs)['status'], 'failed')
                    self.assertTrue(client._budget_ledger.unknown_dimensions())
                    # Interrupted before completed consumer receipt persistence.
                    (root / 'repair_attempts/run/round-1/result.json').unlink()
                else:
                    def interrupt_application(project, actions, **options):
                        if interruption == 'application_after_write' and actions[0]['path'] == 'module.py':
                            return apply_repair_actions(project, actions, **options)
                        raise (KeyboardInterrupt('application interrupted')
                            if interruption == 'application_after_write' else RuntimeError('application interrupted'))
                    with patch('simple_ar.code_task.generation.generated_project_repair.apply_repair_actions',
                               side_effect=interrupt_application):
                        with self.assertRaisesRegex(KeyboardInterrupt if interruption == 'application_after_write'
                                else RuntimeError, 'application interrupted'):
                            _invoke_generated_repair('run', kwargs)
                    self.assertTrue((root / 'repair_attempts/run/round-1/candidate.json').is_file())
                    self.assertIn('application interrupted',
                        read_text(root / 'repair_attempts/run/round-1/application-error.json'))
                before = client._budget_ledger.to_dict()
                resumed = _invoke_generated_repair('run', kwargs)
                self.assertEqual(resumed['status'], 'failed')
                self.assertIn('no request or edit replayed', str(resumed['unresolved_errors']))
                self.assertIn('generation incomplete' if interruption == 'unknown_generation' else 'candidate saved',
                    str(resumed['unresolved_errors']))
                self.assertEqual(len(client.requests), 2)
                self.assertEqual(client._budget_ledger.to_dict(), before)
                self.assertEqual(read_text(project / 'module.py'), 'VALUE = 1\n')
                if interruption == 'application_after_write':
                    self.assertEqual(read_text(project / 'second.py'), 'VALUE = 1\n')

    def test_generated_repair_restore_failure_retains_original_rejection(self) -> None:
        with _temporary_root() as root:
            project = root / 'project'
            write_text(project / 'first.py', 'VALUE = 1\n')
            write_text(project / 'second.py', 'def measure(x):\n    return x\n')
            client = _RelatedRepairClient({'first.py': [('VALUE = 1', 'VALUE = 2')],
                'second.py': [('def measure(x):', 'def measure(x, required):')]})
            with patch('simple_ar.code_task.editing.snapshots.FileSnapshotSet.restore',
                       return_value=[{'path': 'first.py', 'status': 'failed', 'reason': 'snapshot_file_missing'}]):
                result = _invoke_generated_repair('run',
                    dict(project_dir=project, output_path=root / 'run_repair.json', client=client))
            self.assertEqual(result['status'], 'failed')
            self.assertIn('public_api_signature_would_change', str(result['unresolved_errors']))
            self.assertIn('snapshot_file_missing', str(result['unresolved_errors']))
            self.assertIn('restoration incomplete', str(result['notes']))
            self.assertTrue(Path(result['snapshot']['manifest']).is_file())

    def test_generated_repair_saved_legacy_results_are_history_without_redispatch(self) -> None:
        for phase in ('review', 'run'):
            for structured in (False, True):
                with self.subTest(phase=phase, structured=structured), _temporary_root() as root:
                    project = root / 'project'
                    write_text(project / 'module.py', 'USER_CHANGE = 8\n')
                    response = ({'actions': [{'action': 'rewrite_file', 'path': 'module.py', 'content': 'VALUE = 2\n'}]}
                        if structured else {'content': 'VALUE = 2\n'})
                    saved = {'schema_version': f'greenfield_{phase}_repair.v1', 'status': 'patched',
                        'changed_files': ['module.py'], 'regenerated_files': [{'path': 'module.py', 'response': response}],
                        'notes': [], 'unresolved_errors': []}
                    output = root / f'{phase}_repair.json'
                    write_json(output, saved)
                    client = Mock()
                    client.ask_json.side_effect = AssertionError('Historical response must not be regenerated')
                    result = _invoke_generated_repair(phase, dict(project_dir=project, output_path=output, client=client))
                    self.assertEqual(result['status'], 'skipped')
                    self.assertEqual(result['historical_status'], 'patched')
                    self.assertFalse(result['current_project_verified'])
                    self.assertEqual(result['historical_result'], saved)
                    self.assertEqual(read_json(output), saved)
                    self.assertEqual(read_text(project / 'module.py'), 'USER_CHANGE = 8\n')
                    client.ask_json.assert_not_called()
                    following = _RelatedRepairClient({'module.py': [('USER_CHANGE = 8', 'USER_CHANGE = 9')]})
                    revised = _invoke_generated_repair(phase, dict(project_dir=project,
                        output_path=output, client=following, attempt_id='round-2'))
                    self.assertEqual(revised['status'], 'patched', revised)
                    self.assertEqual(len(following.requests), 2)
                    self.assertEqual(read_json(root / f'repair_attempts/{phase}/round-2/previous-unbound-summary.json'), saved)
                    self.assertEqual(read_text(project / 'module.py'), 'USER_CHANGE = 9\n')

    def test_generated_repair_does_not_hide_internal_errors(self) -> None:
        with _temporary_root() as root:
            project = root / 'project'
            write_text(project / 'module.py', 'VALUE = 1\n')
            kwargs = dict(project_dir=project, output_path=root / 'review_repair.json')
            client = Mock()
            client.ask_json.side_effect = LLMError('provider unavailable')
            result = _invoke_generated_repair('review', dict(kwargs, client=client, attempt_id='provider'))
            self.assertEqual(result['status'], 'failed')
            self.assertIn('provider unavailable', str(result['unresolved_errors']))
            client.ask_json.side_effect = TypeError('invalid internal state')
            with self.assertRaisesRegex(TypeError, 'invalid internal state'):
                _invoke_generated_repair('review', dict(kwargs, client=client, attempt_id='internal'))
            client.ask_json.reset_mock()
            with patch('simple_ar.code_task.review_pipeline.build_review_index',
                       side_effect=RuntimeError('index construction failed')):
                with self.assertRaisesRegex(RuntimeError, 'index construction failed'):
                    _invoke_generated_repair('review', dict(kwargs, client=client, attempt_id='index'))
            client.ask_json.assert_not_called()
            self.assertEqual(read_text(project / 'module.py'), 'VALUE = 1\n')

    def test_generated_repair_inventory_uses_actual_files_and_explicit_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta"
            project = Path(tmp) / "project"
            project.mkdir()
            (project / "model.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
            (project / "README.md").write_text("Original description.\n", encoding="utf-8")
            (project / "review.md").write_text("Requested deliverable.\n", encoding="utf-8")
            write_json(meta / "code_artifacts.json", {
                "generated_files": [
                    {"path": "model.py", "mode": "fallback", "line_count": 999},
                    {"path": "README.md", "mode": "fallback", "line_count": 999},
                    {"path": "review.md", "mode": "llm", "line_count": 999},
                ],
            })
            repair = {
                "status": "failed",
                "changed_files": ["model.py", "README.md"],
                "unresolved_errors": ["another file: provider error"],
                "regenerated_files": [{
                    "path": "model.py", "mode": "llm_review_repair",
                    "line_count": 42, "summary": "Repaired model.", "public_api": [],
                }],
            }
            _update_generated_repair_artifacts(meta, project, repair)
            artifacts = read_json(meta / "code_artifacts.json")
            rows = {row["path"]: row for row in artifacts["generated_files"]}
            self.assertEqual(rows["model.py"]["mode"], "llm_review_repair")
            self.assertEqual(rows["model.py"]["line_count"], 2)
            self.assertEqual(rows["README.md"]["mode"], "fallback")
            self.assertNotIn("summary", rows["README.md"])
            self.assertIn("review.md", rows)
            self.assertEqual(artifacts["total_lines"], 4)

    def test_review_repair_without_model_preserves_source_and_missing_deliverables(self) -> None:
        cases = (
            ("fallback_resource", {
                "generated_experiment/resources.py": "from __future__ import annotations\n\n# Reserved generated module.\n",
            }, [{"category": "mixed_generation_fallback",
                 "summary": "Core file generated_experiment/resources.py fell back."}]),
            ("structural_defects", {
                "__init__.py": "def broken(:\n    pass\n",
                "helpers.py": "def _normalize(x):\n    return x\n",
                "consumer.py": "from helpers import normalize\n",
            }, [
                {"category": "missing_local_api", "summary": "Missing helpers.normalize."},
                {"category": "syntax_error", "summary": "__init__.py does not compile."},
                {"category": "missing_entrypoint", "summary": "Missing main.py."},
                {"category": "missing_required_artifact", "summary": "Missing README and config."},
            ]),
        )
        for name, files, findings in cases:
            with self.subTest(case=name), _temporary_root() as root:
                project = root / "project"
                for path, content in files.items():
                    write_text(project / path, content)
                before = _project_bytes(project)
                repair = repair_generated_project_from_review(project_dir=project,
                    review_report={"status": "failed", "findings": [
                        dict(row, severity="blocking") for row in findings]},
                    code_artifacts={"generated_files": [
                        {"path": path, "mode": "fallback", "line_count": len(content.splitlines())}
                        for path, content in files.items()]},
                    output_path=root / "repair.json", client=None)
                self.assertEqual(repair["status"], "skipped")
                self.assertEqual(repair["review_status"], "failed")
                self.assertEqual(repair["changed_files"], [])
                self.assertEqual(_project_bytes(project), before)


    def test_init_copies_workspace_and_indexes_python_ast(self) -> None:
        with _temporary_root() as root:
            code_root = root / "toy_project"
            task_file = root / "task.md"
            _write_toy_project(code_root)
            write_text(task_file, "# Task\n\nImprove the spam classifier without changing the API.\n")

            run_dir = root / "runs" / "code-task-run"
            binding = workspace_source_binding(WorkspaceSpec(
                code_root, run_dir / "code_task", "copy", max_file_bytes=10_000,
            ))
            result = initialize_code_task(
                run_dir=run_dir,
                code_root=code_root,
                task_file=task_file,
                benchmark_command="python -m unittest discover -s tests",
                max_file_bytes=10_000,
            )

            workspace = result.workspace_dir
            self.assertTrue((workspace / "spam_model.py").is_file())
            self.assertTrue((workspace / "tests" / "test_spam_model.py").is_file())
            self.assertFalse((workspace / ".env").exists())
            self.assertFalse((workspace / ".git" / "config").exists())
            self.assertEqual(
                read_text(code_root / "spam_model.py"),
                read_text(workspace / "spam_model.py"),
            )

            self.assertEqual(binding["source_snapshots"][result.workspace.selected_mode],
                             implementation_file_hashes(run_dir))
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["workflow"], "code_task")
            self.assertEqual(manifest["layout"]["workspace"], "code_task/workspace")
            self.assertEqual(manifest["benchmark"]["executed"], False)
            self.assertEqual(manifest["environment"]["policy"]["mode"], "current")
            self.assertEqual(manifest["environment"]["policy"]["python_executable"], sys.executable)
            self.assertEqual(manifest["workspace"]["mode"], "copy")
            self.assertEqual(manifest["workspace"]["workspace_dir"], "code_task/workspace")
            self.assertEqual(manifest["edit_scope"]["mode"], "source_only_default")
            self.assertIn("tests/**", manifest["edit_scope"]["protected_patterns"])
            self.assertGreaterEqual(manifest["copy"]["skipped_count"], 2)
            task_contract = read_json(run_dir / "code_task" / "meta" / "task_contract.json")
            self.assertEqual(task_contract["schema_version"], "code_task_contract.v4")
            self.assertEqual(task_contract["task_kind"], "existing_project")
            self.assertEqual(task_contract["contract_id"], "code-task-existing_project")
            self.assertIn("version_hash", task_contract)
            self.assertEqual(task_contract["metric_contract"]["primary_metric"], "score")
            coverage = read_json(run_dir / "code_task" / "meta" / "task_contract_coverage.json")
            self.assertEqual(coverage["contract_id"], task_contract["contract_id"])

            index = read_json(result.codebase_index_path)
            self.assertEqual(index["project"]["python_file_count"], 2)
            self.assertEqual(index["project"]["test_file_count"], 1)
            spam_model = _indexed_file(index, "spam_model.py")
            self.assertIn("source", spam_model["role_tags"])
            self.assertEqual(spam_model["python"]["syntax_ok"], True)
            self.assertIn("math", spam_model["python"]["imports"])
            self.assertEqual(
                [item["name"] for item in spam_model["python"]["classes"]],
                ["SpamModel"],
            )
            self.assertEqual(
                [item["name"] for item in spam_model["python"]["functions"]],
                ["predict"],
            )
            self.assertEqual(spam_model["python"]["has_main_guard"], True)

            self.assertTrue(result.repo_map_path.is_file())
            self.assertTrue(result.repo_map_summary_path.is_file())
            repo_map = read_json(result.repo_map_path)
            self.assertEqual(repo_map["schema_version"], 1)
            self.assertEqual(repo_map["project"]["file_count"], 3)
            self.assertEqual(repo_map["project"]["python_file_count"], 2)
            self.assertEqual(repo_map["project"]["test_file_count"], 1)
            self.assertGreaterEqual(repo_map["project"]["symbol_count"], 4)
            mapped_files = {item["path"]: item for item in repo_map["files"]}
            self.assertEqual(mapped_files["spam_model.py"]["access_role"], "editable")
            self.assertEqual(
                mapped_files["tests/test_spam_model.py"]["access_role"],
                "read_only_evidence",
            )
            symbols = {item["qualified_name"] for item in repo_map["symbols"]}
            self.assertIn("SpamModel", symbols)
            self.assertIn("SpamModel.score", symbols)
            self.assertIn("predict", symbols)
            self.assertIn("SpamModelTests.test_predicts_spam_keyword", symbols)
            entrypoint_paths = {item["path"] for item in repo_map["entrypoints"]}
            self.assertIn("spam_model.py", entrypoint_paths)
            self.assertEqual(repo_map["tests"][0]["path"], "tests/test_spam_model.py")
            self.assertEqual(repo_map["configs"][0]["path"], "pyproject.toml")
            repo_summary = read_text(result.repo_map_summary_path)
            self.assertIn("# Repo Map Summary", repo_summary)
            self.assertIn("## Prompt Budget", repo_summary)

    def test_greenfield_init_uses_empty_workspace_without_code_root(self) -> None:
        with _temporary_root() as root:
            task_file = root / "task.md"
            write_text(task_file, "# Task\n\nCreate a small runnable Python experiment.\n")
            config = root / "greenfield.toml"
            write_text(
                config,
                """
[code_task]
kind = "greenfield"
task_file = "task.md"
name = "greenfield-smoke"

[benchmark]
command = "python generated_project/main.py"
primary_metric = "accuracy"
""".strip(),
            )

            options = load_code_task_init_options(config_path=str(config))
            self.assertEqual(options.kind, "greenfield")
            self.assertIsNone(options.code_root)
            self.assertEqual(options.workspace_mode, "empty")

            run_dir = root / "runs" / "greenfield-run"
            result = initialize_code_task(
                run_dir=run_dir,
                code_root=None,
                task_file=task_file,
                kind=options.kind,
                benchmark_command=options.benchmark_command,
                workspace_mode=options.workspace_mode,
                primary_metric=options.primary_metric,
            )

            self.assertEqual(result.kind, "greenfield")
            self.assertTrue(result.workspace_dir.is_dir())
            self.assertEqual(list(result.workspace_dir.iterdir()), [])
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["code_task"]["kind"], "greenfield")
            self.assertEqual(manifest["workspace"]["mode"], "empty")
            self.assertEqual(manifest["source"]["code_root"], "")
            self.assertFalse((run_dir / "code_task" / "meta" / "task_contract.json").exists())

    def test_persisted_repair_counters_do_not_silently_grant_new_attempts(self) -> None:
        from simple_ar.code_task.orchestration.execute import _greenfield_repair_available

        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            task_file = root / "task.md"
            write_text(task_file, "Generate a small CPU experiment.")
            run_dir = root / "run"
            initialize_code_task(run_dir=run_dir, code_root=None, task_file=task_file,
                                 kind="greenfield", workspace_mode="empty")
            manifest_path = run_dir / "manifest.json"
            manifest = read_json(manifest_path)
            for value in (0, 1, -1, "invalid", None, True):
                with self.subTest(value=value):
                    manifest["repair"] = {"run_repair_count": value}
                    write_json(manifest_path, manifest)
                    before = manifest_path.read_bytes()
                    if type(value) is int and value >= 0:
                        self.assertEqual(_greenfield_repair_available(run_dir, 1, phase="run"), value == 0)
                    else:
                        with self.assertRaisesRegex(ValueError, "persisted repair counter"):
                            _greenfield_repair_available(run_dir, 1, phase="run")
                    self.assertEqual(manifest_path.read_bytes(), before)

    def test_greenfield_method_migration_retains_history_connection_and_budget(self) -> None:
        for original_mode in ("tool_agent", "compact", "unknown"):
            with self.subTest(original_mode=original_mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                planning = root / "planning"
                stage = "interfaces" if original_mode == "tool_agent" else "compact"
                write_json(planning / stage / "attempt-001.json", {"original": True})
                if original_mode != "unknown":
                    write_json(root / "implementation_plan.json", {"planning_mode": original_mode,
                        "models": {"planner": "profile:saved"}})
                before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
                ledger = BudgetLedger({"llm_requests": 3, "total_tokens": 1000})
                ledger.reserve("prior", {"llm_requests": 1, "total_tokens": 100})
                ledger.mark_unknown("prior", reason="original incomplete stream")
                budget_before = ledger.to_dict()
                client = Mock()
                client._budget_ledger = ledger
                client.ask_json.side_effect = LLMError("provider unavailable")
                messages = []
                kwargs = dict(contract={"objective": "Keep evidence"}, result_schema={},
                    resource_plan={}, domain_profile={}, client=client, planning_mode="tool_agent",
                    planning_review_rounds=2, planning_dir=planning, retry_attempts=3,
                    planner_selector="profile:saved", message_callback=messages.append)
                with self.assertRaisesRegex(LLMError, "provider unavailable"):
                    _architecture_plan(**kwargs, allow_fallback=True)
                client.ask_json.assert_called_once()
                receipt = read_json(planning / "method_migration.json")
                self.assertEqual(receipt["from_method"], original_mode)
                self.assertEqual(receipt["planner_selector"], "profile:saved")
                self.assertFalse(receipt["old_workflow_success"])
                self.assertEqual(ledger.to_dict(), budget_before)
                self.assertTrue(any("deprecated" in m for m in messages))
                with self.assertRaisesRegex(ValueError, "Planner selector conflicts"):
                    _architecture_plan(**{**kwargs, "planner_selector": "profile:other"})
                client.ask_json.assert_called_once()
                self.assertEqual({p: p.read_bytes() for p in before}, before)
                retained_receipt = (planning / "method_migration.json").read_bytes()
                # A later explicit attempt uses the same ledger and retains the
                # failed attempt, rather than regenerating retired stages.
                client.ask_json.side_effect = None
                client.ask_json.return_value = {"files": [{"path": "main.py"}]}
                plan, mode = _architecture_plan(**kwargs)
                self.assertEqual(mode, "compact")
                self.assertEqual(plan, read_json(planning / "architecture_unit/attempt-002/final_plan.json"))
                self.assertTrue((planning / "architecture_unit/attempt-001/agent_steps.jsonl").is_file())
                self.assertEqual((planning / "method_migration.json").read_bytes(), retained_receipt)
                self.assertEqual({p: p.read_bytes() for p in before}, before)
                self.assertEqual(ledger.to_dict(), budget_before)


    def test_architecture_alias_preserves_scope_and_explicit_empty_plan_fallback(self) -> None:
        for paths in ([], ["main.py"], ["main.py", "custom_model.py"]):
            plans = []
            for mode in ("compact", "tool_agent"):
                with self.subTest(paths=paths, mode=mode):
                    client = Mock()
                    client.ask_json.return_value = {
                        "architecture_summary": "A bounded experiment",
                        "files": [{"path": path, "public_api": ["main(argv=None)"] if path == "main.py" else []}
                                  for path in [*paths, "../escape.py"]],
                        "risks": ["Generalization outside the measured domain is unknown."],
                        "status": "pass", "findings": [],
                    }
                    if not paths:
                        with self.assertRaisesRegex(LLMError, "no usable file specifications"):
                            _architecture_plan(client=client, planning_mode=mode)
                        client.ask_json.assert_called_once()
                        plan, source = _architecture_plan(client=client, planning_mode=mode, allow_fallback=True)
                        self.assertEqual(source, "fallback")
                        self.assertTrue(plan["files"])
                        self.assertEqual(client.ask_json.call_count, 2)
                    else:
                        plan, source = _architecture_plan(client=client, planning_mode=mode)
                        client.ask_json.assert_called_once()
                        self.assertEqual(source, "compact")
                        self.assertEqual([row["path"] for row in plan["files"]], paths)
                        self.assertTrue(plan["files"][0]["entrypoint"])
                        self.assertTrue(any("outside the measured domain" in risk for risk in plan["risks"]))
                        plans.append(plan["files"])
            if paths:
                self.assertEqual(plans[0], plans[1])


    def test_plan_factory_owns_file_boundary_for_both_producers(self) -> None:
        rows = [
            {"path": "alternate.py", "entrypoint": True, "dependencies": ["discarded.py", "../secret"]},
            {"path": "generated_project/alternate.py", "contract_obligations": ["O1"]},
            {"path": "discarded.py"},
            *[{"path": path} for path in ("../escape.py", "/absolute.py", "C:/secret.py")],
        ]
        before = json.dumps(rows, sort_keys=True)
        for mode in ("compact", "tool_agent"):
            for limit in (1, 2, 3):
                with self.subTest(mode=mode, limit=limit):
                    client = Mock()
                    client.ask_json.return_value = {"files": rows, "status": "pass"}
                    kwargs = dict(contract={}, result_schema={}, resource_plan={"max_files": limit},
                        domain_profile={}, client=client, planning_mode=mode, planning_review_rounds=0)
                    if limit < 3:
                        with self.assertRaisesRegex(LLMError, 'requires 3 files but max_files'):
                            _architecture_plan(**kwargs)
                        self.assertEqual(json.dumps(rows, sort_keys=True), before)
                        continue
                    plan, source = _architecture_plan(**kwargs)
                    self.assertEqual(source, "compact")
                    self.assertEqual([row["path"] for row in plan["files"]],
                        ["main.py", "alternate.py", "discarded.py"])
                    self.assertTrue(plan["files"][0]["entrypoint"])
                    self.assertEqual(plan["files"][1]["dependencies"], ['discarded.py'])
                    self.assertEqual(plan["files"][1]["contract_obligations"], ["O1"])
                    self.assertEqual(json.dumps(rows, sort_keys=True), before)
        with self.assertRaisesRegex(LLMError, 'max_files is 1'):
            _architecture_plan(resource_plan={"max_files": 1}, allow_fallback=True)
        offline, source = _architecture_plan(resource_plan={"max_files": 4}, allow_fallback=True)
        self.assertEqual(source, "fallback")
        self.assertEqual(len(offline['files']), 4)
        self.assertEqual(offline['files'][0]['path'], 'main.py')

    def test_complete_architecture_reaches_generation_and_snapshot_without_reconstruction(self) -> None:
        from simple_ar.code_task.generation.greenfield import generate_greenfield_code_task
        from simple_ar.code_task.generation.task_contract import (
            build_greenfield_task_contract, save_task_contract,
        )

        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            task = root / "task.md"
            write_text(task, "Generate a bounded CPU project printing score.")
            source_run, restored_run = root / "source", root / "restored"
            for run in (source_run, restored_run):
                _initialize_greenfield(run_dir=run, task_file=task, primary_metric="score")
            contract = build_greenfield_task_contract(read_text(task),
                benchmark_command="python generated_project/main.py", max_files=3,
                max_generated_lines=1600, result_schema={"primary_metric": "score"},
                extra_contract={"implementation_obligations": [{
                    "id": "o1", "requirement": "Preserve records for reporting.",
                    "owner_files": ["records.py"],
                }]})
            for run in (source_run, restored_run):
                save_task_contract(run / "code_task/meta", contract)
            interfaces = {
                "shared_schemas": [{"name": "Record", "fields": ["score"],
                    "producer": "records.py", "consumers": ["main.py"]}],
                "module_apis": [{"module": "records.py", "public_api": ["records()"]}],
            }
            file_rows = [
                {"path": "generated_project/main.py", "dependencies": ["records.py"]},
                {"path": "records.py", "public_api": ["records()"], "contract_obligations": ["o1"]},
                {"path": "results", "kind": "runtime_dir"},
            ]
            requests = []

            def respond(_system, prompt, *, label, **_kwargs):
                if label.startswith("greenfield-unit-"):
                    context = json.loads(prompt)
                    requests.append(context)
                    return {"files": [{"path": row["path"], "content":
                        "print('score: 0.5')\n" if row["path"] == "main.py"
                        else "def records():\n    return []\n"}
                        for row in context["planned_files"]]}
                return {
                    "greenfield-architecture": {"objective": "Preserve records.",
                        "architecture_summary": "Shared records.", "interfaces": interfaces,
                        "files": file_rows, "risks": ["Coverage remains bounded."]},
                }[label]

            client = Mock()
            client.ask_json.side_effect = respond
            with patch("simple_ar.code_task.generation.greenfield._llm_client", return_value=client), patch(
                "simple_ar.code_task.generation.greenfield.review_generated_project",
                return_value={"status": "passed", "findings": []},
            ) as reviewer:
                generate_greenfield_code_task(source_run, max_files=3, planning_review_rounds=0,
                                             planner_model="profile:saved")
            meta = source_run / "code_task/meta"
            plan = read_json(meta / "architecture_plan.json")
            resources = read_json(meta / 'implementation_plan.json')['resource_plan']
            self.assertEqual(resources['max_files'], 3)
            self.assertEqual(resources['execution_budget']['policy'], 'explicit_execution_contract_and_shared_ledger')
            self.assertNotIn('max_model_fit_operations_hint', resources['execution_budget'])
            self.assertNotIn('max_runtime_sec_hint', resources['execution_budget'])
            self.assertNotIn('allow_gpu', resources)
            self.assertIn('gpu_observed', resources)
            self.assertEqual(plan, read_json(meta / "planning/architecture_unit/attempt-001/final_plan.json"))
            self.assertEqual(read_json(meta / "planning/method_migration.json")["planner_selector"], "profile:saved")
            self.assertEqual(requests[0]["architecture"], plan)
            self.assertEqual(reviewer.call_args.kwargs["architecture_plan"], plan)
            self.assertEqual(plan["interface_registry"]["shared_schemas"], interfaces["shared_schemas"])
            self.assertEqual(plan["interface_registry"]["module_apis"], interfaces["module_apis"])
            self.assertEqual(plan["implementation_contract"]["owner_index"]["o1"], ["records.py"])
            self.assertTrue(any(row["id"] == "o1" for row in plan["implementation_contract"]["obligations"]))
            self.assertTrue(any(row["id"] == "o1" for row in requests[0]["obligations"]["records.py"]))
            self.assertEqual(plan["files"][2]["kind"], "runtime_dir")
            self.assertIn("Coverage remains bounded.", plan["risks"])
            self.assertEqual([row["path"] for row in read_json(meta / "file_plan.json")["files"]],
                [row["path"] for row in plan["files"]])
            generated_bytes = {p: p.read_bytes() for p in source_run.rglob("*") if p.is_file()}
            with patch("simple_ar.code_task.generation.greenfield._llm_client",
                       side_effect=AssertionError("Completed projects must not select a connection")):
                generate_greenfield_code_task(source_run, planning_mode="tool_agent", planning_review_rounds=2)
            self.assertEqual({p: p.read_bytes() for p in generated_bytes}, generated_bytes)

            # An old saved plan is imported as-is, including absent modern fields.
            legacy = {key: value for key, value in plan.items()
                if key not in {"implementation_contract", "planning", "planning_status", "result_schema"}}
            legacy["historical_extension"] = {"keep": ["original shape"]}
            write_json(meta / "architecture_plan.json", legacy)
            old_implementation = read_json(meta / "implementation_plan.json")
            old_implementation.update(planning_mode="tool_agent", architecture_mode="tool_agent", planning_review_rounds=2)
            write_json(meta / "implementation_plan.json", old_implementation)
            source_bytes = {path: path.read_bytes() for path in meta.rglob("*") if path.is_file()}
            requests.clear()
            client.reset_mock()
            with patch("simple_ar.code_task.generation.greenfield._llm_client", return_value=client), patch(
                "simple_ar.code_task.generation.greenfield.review_generated_project",
                return_value={"status": "passed", "findings": []},
            ), patch("simple_ar.code_task.generation.architecture.normalize_architecture_plan",
                side_effect=AssertionError("Saved plans must not be canonicalized")):
                generate_greenfield_code_task(restored_run, max_files=3,
                    planning_snapshot_from=source_run)
            self.assertEqual(read_json(restored_run / "code_task/meta/architecture_plan.json"), legacy)
            self.assertEqual(read_json(restored_run / "code_task/meta/implementation_plan.json")["planning_mode"], "tool_agent")
            self.assertFalse((restored_run / "code_task/meta/planning/method_migration.json").exists())
            self.assertEqual(requests[0]["architecture"], legacy)
            self.assertTrue(all(call.kwargs["label"].startswith("greenfield-unit-")
                for call in client.ask_json.call_args_list))
            self.assertEqual({path: path.read_bytes() for path in meta.rglob("*") if path.is_file()}, source_bytes)

    def test_offline_greenfield_requires_implemented_measurement_before_run_and_recovery(self) -> None:
        with _temporary_root() as root:
            task_file = root / "task.md"
            write_text(
                task_file,
                "# Task\n\nGenerate a deterministic project that prints accuracy and macro_f1 metrics.\n",
            )
            run_dir = root / "runs" / "greenfield-run"
            _initialize_greenfield(run_dir=run_dir, task_file=task_file, primary_metric="accuracy",
                metric_directions={"accuracy": "higher_is_better", "macro_f1": "higher_is_better"},
            )

            result = execute_code_task(
                run_dir,
                use_llm=False,
                to_step="work-plan",
                timeout_sec=30,
                max_files=8,
            )

            project = run_dir / "code_task/workspace/generated_project"
            self.assertTrue((project / "main.py").is_file())
            self.assertTrue((run_dir / "code_task/meta/architecture_plan.json").is_file())
            self.assertTrue((run_dir / "code_task/meta/file_plan.json").is_file())
            self.assertEqual(validate_code_task(run_dir).status, "passed")
            config = read_json(project / "config.json")
            self.assertIn("accuracy", config["required_metrics"])
            self.assertIn("macro_f1", config["required_metrics"])
            # A standalone process cannot turn an unimplemented template into evidence.
            external = subprocess.run([sys.executable, str(project / "main.py")],
                cwd=project.parent, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(external.returncode, 0)
            self.assertIn("RuntimeError", external.stderr)
            self.assertIn("no implemented measurement", external.stderr.lower())
            self.assertNotRegex(external.stdout, r"(?m)^\s*(?:accuracy|macro_f1)\s*:\s*[-+]?\d")
            blocked = execute_code_task(run_dir, use_llm=False, to_step="run",
                timeout_sec=30, max_files=8, repair_rounds=0)
            self.assertIn(blocked.stop_reason, ("review_failed", "benchmark_failed"))
            metrics_path = run_dir / "code_task/run/patched/metrics.json"
            if metrics_path.exists():
                self.assertEqual(read_json(metrics_path), {})
            # This fixture implements a measurement; success below belongs to that code.
            write_text(project / "generated_experiment/runner.py",
                "def run_experiment():\n"
                "    truth = [1, 0, 1, 0]\n    predicted = [1, 0, 0, 0]\n"
                "    accuracy = sum(a == b for a, b in zip(truth, predicted)) / len(truth)\n"
                "    tp = sum(a == b == 1 for a, b in zip(truth, predicted))\n"
                "    fp = sum(a == 0 and b == 1 for a, b in zip(truth, predicted))\n"
                "    fn = sum(a == 1 and b == 0 for a, b in zip(truth, predicted))\n"
                "    positive_f1 = 2 * tp / (2 * tp + fp + fn)\n"
                "    tn = sum(a == b == 0 for a, b in zip(truth, predicted))\n"
                "    negative_f1 = 2 * tn / (2 * tn + fp + fn)\n"
                "    return {'accuracy': accuracy, 'macro_f1': (positive_f1 + negative_f1) / 2}\n")
            review_path = run_dir / "code_task/meta/review_report.json"
            review = read_json(review_path)
            review["metadata"]["review_contract_version"] = GREENFIELD_REVIEW_CONTRACT_VERSION - 1
            write_json(review_path, review)
            result = execute_code_task(run_dir, use_llm=False, to_step="run",
                timeout_sec=30, max_files=8)
            self.assertEqual(result.stop_reason, "completed")
            self.assertTrue((run_dir / "code_task" / "meta" / "resource_probe.json").is_file())
            self.assertTrue((run_dir / "code_task" / "meta" / "resource_decision.json").is_file())
            advice = read_json(run_dir / "code_task" / "meta" / "dependency_advice.json")
            self.assertEqual(advice["schema_version"], "code_task_dependency_advice.v1")
            self.assertEqual(advice["policy"], "advice_only_no_auto_install")
            task_contract = read_json(run_dir / "code_task" / "meta" / "task_contract.json")
            self.assertEqual(task_contract["schema_version"], "code_task_contract.v4")
            self.assertEqual(task_contract["task_kind"], "greenfield")
            self.assertEqual(task_contract["metric_contract"]["primary_metric"], "accuracy")
            self.assertIn("macro_f1", task_contract["metric_contract"]["required_metrics"])
            self.assertIn("version_hash", task_contract)
            coverage = read_json(run_dir / "code_task" / "meta" / "task_contract_coverage.json")
            self.assertEqual(coverage["version_hash"], task_contract["version_hash"])
            analyzer_registry = read_json(run_dir / "code_task" / "meta" / "analyzer_registry.json")
            self.assertEqual(analyzer_registry["schema_version"], "code_task_analyzer_registry.v1")
            self.assertTrue(any(row["id"] == "local_api_contract" for row in analyzer_registry["analyzers"]))
            validation = read_json(run_dir / "code_task" / "meta" / "validation_report.json")
            self.assertEqual(validation["status"], "passed")
            metrics = read_json(run_dir / "code_task" / "run" / "patched" / "metrics.json")
            self.assertIn("accuracy", metrics)
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["implementation"]["status"], "generated")
            self.assertEqual(manifest["patch"]["mode"], "greenfield_generated")

            # Refresh retired review rules without regenerating or rerunning the experiment.
            review_path = run_dir / "code_task/meta/review_report.json"
            prior_review = read_json(review_path)
            prior_review["metadata"]["review_contract_version"] = GREENFIELD_REVIEW_CONTRACT_VERSION - 1
            write_json(review_path, prior_review)
            metrics_path = run_dir / "code_task/run/patched/metrics.json"
            measured_bytes = metrics_path.read_bytes()
            with patch("simple_ar.code_task.orchestration.execute.run_code_task_benchmark") as benchmark:
                refreshed = execute_code_task(run_dir, use_llm=False, to_step="review", max_files=8)
            benchmark.assert_not_called()
            self.assertEqual(read_json(review_path)["metadata"]["review_contract_version"], GREENFIELD_REVIEW_CONTRACT_VERSION)
            self.assertTrue(any(step.step == "review" and step.detail.startswith("refreshed status")
                                for step in refreshed.steps))
            self.assertEqual(metrics_path.read_bytes(), measured_bytes)
            historical = [path for path in (review_path.parent / "review_history").glob("*.json")
                          if read_json(path) == prior_review]
            self.assertEqual(len(historical), 1)
            self.assertEqual(read_json(historical[0]), prior_review)
            current_review = read_json(review_path)
            write_json(historical[0], {**prior_review, "status": "tampered"})
            write_json(review_path, prior_review)
            with patch("simple_ar.code_task.orchestration.execute.run_code_task_benchmark") as benchmark:
                with self.assertRaisesRegex(ValueError, "historical review changed"):
                    execute_code_task(run_dir, use_llm=False, to_step="review", max_files=8)
            benchmark.assert_not_called()
            self.assertEqual(read_json(review_path), prior_review)
            self.assertEqual(metrics_path.read_bytes(), measured_bytes)
            write_json(historical[0], prior_review)
            write_json(review_path, current_review)

            # Exercise the same runner after an actual subprocess failure.
            # The repair boundary restores this fixture; it does not fake a metric.
            entrypoint = run_dir / "code_task/workspace/generated_project/main.py"
            original_source = entrypoint.read_text(encoding="utf-8")
            write_text(entrypoint, original_source + "\nraise RuntimeError('fixture run failure')\n")

            def restore_fixture(*args, **kwargs):
                write_text(entrypoint, original_source)
                return True

            with patch(
                "simple_ar.code_task.orchestration.execute._attempt_greenfield_run_repair",
                side_effect=restore_fixture,
            ), patch(
                "simple_ar.code_task.orchestration.execute.run_code_task_benchmark",
                wraps=run_code_task_benchmark,
            ) as runner:
                repaired = execute_code_task(run_dir, use_llm=False, to_step="run",
                                             timeout_sec=30, max_files=8)
            self.assertEqual(repaired.stop_reason, "completed")
            self.assertEqual(runner.call_count, 2)
            self.assertEqual([step.detail for step in repaired.steps if step.step == "run"],
                             ["status failed", "status passed"])
            self.assertEqual(read_json(run_dir / "code_task/run/patched/metrics.json"), metrics)

    def test_greenfield_review_failure_without_model_does_not_erase_package_code(self) -> None:
        with _temporary_root() as root:
            task_file = root / "task.md"
            write_text(
                task_file,
                "# Task\n\nGenerate a deterministic project that prints accuracy and macro_f1 metrics.\n",
            )
            run_dir = root / "runs" / "greenfield-review-repair"
            _initialize_greenfield(run_dir=run_dir, task_file=task_file, primary_metric="accuracy",
                metric_directions={"accuracy": "higher_is_better", "macro_f1": "higher_is_better"},
            )

            first = execute_code_task(
                run_dir,
                use_llm=False,
                to_step="work-plan",
                timeout_sec=30,
                max_files=8,
            )
            self.assertEqual(first.stop_reason, "stop_point")
            init_file = (
                run_dir
                / "code_task"
                / "workspace"
                / "generated_project"
                / "generated_experiment"
                / "__init__.py"
            )
            write_text(init_file, '__"""generated_experiment package."""\n')
            write_json(
                run_dir / "code_task" / "meta" / "review_report.json",
                {
                    "schema_version": "review_report.v1",
                    "status": "failed",
                    "findings": [
                        {
                            "severity": "blocking",
                            "category": "python_compile_failed",
                            "summary": "generated_experiment/__init__.py does not compile.",
                        }
                    ],
                    "summary": {"blocking_count": 1, "error_count": 1, "warning_count": 0},
                },
            )

            result = execute_code_task(
                run_dir,
                use_llm=False,
                to_step="run",
                timeout_sec=30,
                max_files=8,
                repair_rounds=1,
            )

            self.assertNotEqual(result.stop_reason, "completed")
            repair = read_json(run_dir / "code_task" / "meta" / "review_repair.json")
            self.assertEqual(repair["status"], "skipped")
            self.assertEqual(read_text(init_file), '__"""generated_experiment package."""\n')
            self.assertFalse((run_dir / "code_task" / "run" / "patched" / "metrics.json").exists())


    def _assert_greenfield_artifact_guard(
        self, root: Path, run_name: str, script: str, expected_reason: str,
    ) -> tuple[Path, dict]:
        """Exercise the common score benchmark while preserving each failure input."""
        task_file = root / "task.md"
        write_text(
            task_file,
            "# Task\n\nWrite artifacts/results.json and artifacts/report.md with measured rows.\n",
        )
        run_dir = root / "runs" / run_name
        _initialize_greenfield(run_dir=run_dir, task_file=task_file, primary_metric="score",
            metric_directions={"score": "higher"},
        )
        project_dir = run_dir / "code_task" / "workspace" / "generated_project"
        project_dir.mkdir(parents=True)
        write_text(project_dir / "main.py", script)

        result = run_code_task_benchmark(run_dir, timeout_sec=30, skip_validation=True)

        self.assertEqual(result.status, "failed")
        report = read_json(result.report_path)
        self.assertEqual(report["quality_guard"]["reason"], expected_reason)
        return run_dir, report

    def test_greenfield_benchmark_rejects_empty_zero_evidence(self) -> None:
        with _temporary_root() as root:
            task_file = root / "task.md"
            write_text(task_file, "# Task\n\nRun a greenfield experiment with condition-level evidence.\n")
            run_dir = root / "runs" / "empty-greenfield-evidence"
            _initialize_greenfield(run_dir=run_dir, task_file=task_file, primary_metric="test_accuracy",
                metric_directions={
                    "test_accuracy": "higher",
                    "macro_f1": "higher",
                    "accuracy_std": "lower",
                    "runtime_sec": "resource",
                },
            )
            project_dir = run_dir / "code_task" / "workspace" / "generated_project"
            project_dir.mkdir(parents=True)
            write_text(
                project_dir / "main.py",
                (
                    "from pathlib import Path\n"
                    "import json\n\n"
                    "artifacts = Path('generated_project') / 'artifacts'\n"
                    "artifacts.mkdir(parents=True, exist_ok=True)\n"
                    "(artifacts / 'results.json').write_text(json.dumps({\n"
                    "    'condition_summaries': {},\n"
                    "    'dataset_comparisons': {},\n"
                    "    'raw_records': [{'condition': {'name': 'baseline'}, 'records': []}],\n"
                    "    'global_metrics': {'test_accuracy': 0.0, 'macro_f1': 0.0, 'accuracy_std': 0.0, 'runtime_sec': 0.0},\n"
                    "}), encoding='utf-8')\n"
                    "print('test_accuracy: 0.0')\n"
                    "print('macro_f1: 0.0')\n"
                    "print('accuracy_std: 0.0')\n"
                    "print('runtime_sec: 0.0')\n"
                ),
            )

            result = run_code_task_benchmark(
                run_dir,
                timeout_sec=30,
                skip_validation=True,
                run_label="patched",
            )

            self.assertEqual(result.status, "failed")
            self.assertIn("Generated benchmark quality guard failed", read_text(result.stderr_path))
            report = read_json(result.report_path)
            self.assertEqual(report["quality_guard"]["reason"], "empty_greenfield_evidence")
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["status"], "benchmark_failed")

    def test_greenfield_benchmark_rejects_zero_exit_without_required_results(self) -> None:
        with _temporary_root() as root:
            run_dir, report = self._assert_greenfield_artifact_guard(
                root, "missing-results",
                "print('ERROR: experiment failed but was swallowed')\n",
                "missing_results_artifact",
            )

    def test_greenfield_benchmark_rejects_missing_required_report(self) -> None:
        with _temporary_root() as root:
            run_dir, report = self._assert_greenfield_artifact_guard(
                root, "missing-report",
                (
                    "from pathlib import Path\n"
                    "artifacts = Path('generated_project') / 'artifacts'\n"
                    "artifacts.mkdir(parents=True, exist_ok=True)\n"
                    "(artifacts / 'results.json').write_text("
                    "'{\"raw_records\": [{\"score\": 1.0}], \"global_metrics\": {\"score\": 1.0}}'"
                    ")\n"
                    "print('score: 1.0')\n"
                ),
                "missing_required_artifact",
            )
            self.assertIn("report.md", report["quality_guard"]["message"])

    def test_greenfield_benchmark_reports_artifact_path_mismatch(self) -> None:
        with _temporary_root() as root:
            run_dir, report = self._assert_greenfield_artifact_guard(
                root, "artifact-path-mismatch",
                (
                    "from pathlib import Path\n"
                    "root_artifacts = Path('artifacts')\n"
                    "root_artifacts.mkdir(parents=True, exist_ok=True)\n"
                    "(root_artifacts / 'results.json').write_text('{\"raw_records\": [{\"score\": 1.0}], \"global_metrics\": {\"score\": 1.0}}')\n"
                    "(root_artifacts / 'report.md').write_text('# Report\\n')\n"
                    "expected = Path('generated_project') / 'artifacts'\n"
                    "expected.mkdir(parents=True, exist_ok=True)\n"
                    "(expected / 'results.json').write_text('')\n"
                    "print('score: 1.0')\n"
                ),
                "artifact_path_mismatch",
            )
            scan = read_json(run_dir / report["artifact_scan"])
            self.assertEqual(scan["status"], "warning")
            self.assertTrue(
                any(item.get("code") == "artifact_path_mismatch" for item in scan["findings"])
            )
            analysis = analyze_code_task_failure(run_dir)
            self.assertIn("artifact_path_mismatch", read_text(analysis.analysis_path))

    def test_greenfield_artifact_scan_ignores_downstream_submission_signals(self) -> None:
        with _temporary_root() as root:
            task_file = root / "task.md"
            write_text(
                task_file,
                (
                    "# Task\n\n"
                    "- Execution produces a machine-readable metrics artifact.\n"
                    "- Write `generated_project/artifacts/results.json` with measured rows.\n"
                    "- Write `generated_project/artifacts/report.md` with the final report.\n"
                    "- A downstream adapter will convert this run into `submission/results/metrics.json` "
                    "and `submission/README.md`.\n"
                ),
            )
            run_dir = root / "runs" / "downstream-signals"
            _initialize_greenfield(run_dir=run_dir, task_file=task_file, primary_metric="score",
                metric_directions={"score": "higher"},
            )
            project_dir = run_dir / "code_task" / "workspace" / "generated_project"
            project_dir.mkdir(parents=True)
            write_text(
                project_dir / "main.py",
                (
                    "from pathlib import Path\n"
                    "import json\n"
                    "artifacts = Path('generated_project/artifacts')\n"
                    "artifacts.mkdir(parents=True, exist_ok=True)\n"
                    "(artifacts / 'results.json').write_text(json.dumps({'raw_records': [{'score': 1.0}], 'global_metrics': {'score': 1.0}}))\n"
                    "(artifacts / 'report.md').write_text('# Report\\n')\n"
                    "submission = Path('submission/results')\n"
                    "submission.mkdir(parents=True, exist_ok=True)\n"
                    "(submission / 'metrics.json').write_text('{}')\n"
                    "print('score: 1.0')\n"
                ),
            )

            result = run_code_task_benchmark(run_dir, timeout_sec=30, skip_validation=True)

            self.assertEqual(result.status, "passed")
            report = read_json(result.report_path)
            self.assertNotIn("quality_guard", report)
            scan = read_json(run_dir / report["artifact_scan"])
            scanned_paths = {row["expected_path"] for row in scan["artifacts"]}
            self.assertIn("generated_project/artifacts/results.json", scanned_paths)
            self.assertIn("generated_project/artifacts/report.md", scanned_paths)
            self.assertNotIn("generated_project/submission/results/metrics.json", scanned_paths)

    def test_greenfield_review_flags_stdlib_shadow_and_nested_artifact_path(self) -> None:
        with _temporary_root() as root:
            project_dir = root / "generated_project"
            project_dir.mkdir(parents=True)
            write_text(project_dir / "main.py", "from __future__ import annotations\n")
            write_text(project_dir / "types.py", "from __future__ import annotations\n\nclass Row: pass\n")
            write_text(
                project_dir / "writer.py",
                (
                    "from __future__ import annotations\n"
                    "from pathlib import Path\n\n"
                    "CANONICAL_RESULTS = Path('artifacts/results.json')\n\n"
                    "def write_artifacts(results_dir):\n"
                    "    base = Path(results_dir)\n"
                    "    (base / CANONICAL_RESULTS).parent.mkdir(parents=True, exist_ok=True)\n"
                    "    (base / CANONICAL_RESULTS).write_text('{}')\n"
                ),
            )

            report = _generated_review(project_dir,
                [
                    {"path": "main.py", "mode": "llm"},
                    {"path": "types.py", "mode": "llm"},
                    {"path": "writer.py", "mode": "llm"},
                ],
                result_schema={"required_metrics": ["accuracy"]},
                resource_plan={"max_files": 10, "max_generated_lines": 500},
                contract={"objective": "Write artifacts/results.json with measured accuracy."},
            )

            categories = {finding["category"] for finding in report["findings"]}
            self.assertIn("stdlib_module_shadow", categories)
            self.assertIn("nested_artifact_path_risk", categories)

    def test_runtime_repair_without_model_preserves_presets_apis_and_artifact_paths(self) -> None:
        with _temporary_root() as root:
            project = root / "project"
            files = {
                "types.py": "class ConditionSpec:\n    pass\n",
                "main.py": "from types import ConditionSpec\nLABEL = 'types'\n",
                "io/__init__.py": "",
                "writer.py": 'from pathlib import Path\nCANONICAL_RESULTS = Path("artifacts/results.json")\ndef write(results_dir):\n    base = Path(results_dir)\n    return base / CANONICAL_RESULTS\n',
                "generated_experiment/runner.py": "def run_experiment(preset='smoke'):\n    return {'score': 1.0}\n",
            }
            for path, content in files.items():
                write_text(project / path, content)
            write_json(project / "config.json", {
                "objective": "Greenfield run repair test.",
                "presets": {"{preset_name}": {"conditions": ["baseline", "candidate"], "max_items": 32}},
            })
            before = _project_bytes(project)
            failures = [
                f"ImportError: cannot import name 'ConditionSpec' from 'types' ({project / 'types.py'})",
                "ModuleNotFoundError: No module named 'io.artifacts'; 'io' is not a package",
                "ERROR: artifacts/results.json was not written",
                "raise KeyError(f\"Unknown preset '{preset_name}'. Available presets: {available}\")\n"
                "KeyError: \"Unknown preset 'smoke'. Available presets: {preset_name}\"\n",
                "TypeError: run_experiment() got an unexpected keyword argument 'data_source'",
            ]
            for index, stderr in enumerate(failures):
                with self.subTest(stderr=stderr):
                    repair = repair_generated_project_from_run_failure(
                        project_dir=project, failure_analysis={"status": "needs_repair"},
                        stderr_text=stderr, output_path=root / f"repair-{index}.json", client=None)
                    self.assertEqual(repair["status"], "skipped")
                    self.assertEqual(repair["failure_status"], "needs_repair")
                    self.assertEqual(repair["changed_files"], [])
                    self.assertEqual(_project_bytes(project), before)


    def test_greenfield_execute_can_use_fake_agent_backend(self) -> None:
        with _temporary_root() as root:
            task_file = root / "task.md"
            write_text(
                task_file,
                "# Task\n\nUse an external handoff backend to generate a runnable metric project.\n",
            )
            run_dir = root / "runs" / "greenfield-agent-run"
            _initialize_greenfield(run_dir=run_dir, task_file=task_file, primary_metric="accuracy")

            result = execute_code_task(
                run_dir,
                use_llm=False,
                to_step="run",
                timeout_sec=30,
                implementation_provider="fake",
                implementation_agent_mode="handoff",
            )

            self.assertEqual(result.stop_reason, "completed")
            self.assertTrue((run_dir / "agent_handoff" / "code-task-greenfield-fake").is_dir())
            self.assertTrue((run_dir / "agent_outputs" / "code-task-greenfield-fake" / "ingestion.json").is_file())
            self.assertTrue((run_dir / "code_task" / "meta" / "dependency_advice.md").is_file())
            backend = read_json(run_dir / "code_task" / "meta" / "code_backend.json")
            self.assertEqual(backend["backend"], "greenfield_agent")
            self.assertEqual(backend["provider"], "fake")
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["implementation"]["provider"], "fake")
            self.assertEqual(manifest["implementation"]["agent_mode"], "handoff")
            metrics = read_json(run_dir / "code_task" / "run" / "patched" / "metrics.json")
            self.assertIn("accuracy", metrics)

    def test_configured_edit_scope_limits_editable_repo_map_and_apply(self) -> None:
        with _temporary_root() as root:
            code_root = root / "toy_project"
            task_file = root / "task.md"
            config = root / "code_task.toml"
            _write_toy_project(code_root)
            write_text(task_file, "# Task\n\nImprove the model implementation only.\n")
            config.write_text(
                f"""
[code_task]
code_root = "{code_root.as_posix()}"
task_file = "{task_file.as_posix()}"

[edit_scope]
allowed_patterns = ["spam_model.py"]
protected_patterns = ["pyproject.toml"]
""".strip(),
                encoding="utf-8",
            )
            options = load_code_task_init_options(config_path=str(config))

            run_dir = root / "runs" / "scoped-code-task"
            initialize_code_task(
                run_dir=run_dir,
                code_root=Path(options.code_root),
                task_file=Path(options.task_file or ""),
                benchmark_command="python -m unittest discover -s tests",
                edit_scope_allowed_patterns=options.edit_scope_allowed_patterns,
                edit_scope_protected_patterns=options.edit_scope_protected_patterns,
            )

            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["edit_scope"]["allowed_patterns"], ["spam_model.py"])
            self.assertIn("pyproject.toml", manifest["edit_scope"]["protected_patterns"])
            repo_map = read_json(run_dir / "code_task" / "meta" / "repo_map.json")
            files = {row["path"]: row for row in repo_map["files"]}
            self.assertEqual(files["spam_model.py"]["access_role"], "editable")
            self.assertEqual(files["pyproject.toml"]["access_role"], "read_only_evidence")

            write_text(run_dir / "code_task" / "patch_plan.md", "# Patch Plan\n\n- Keep edits in scope.\n")
            record_plan_decision(run_dir, decision="approve", note="scope test")
            edits_path = root / "bad_scope_patch.json"
            write_json(
                edits_path,
                {
                    "schema_version": 1,
                    "edits": [
                        {
                            "path": "pyproject.toml",
                            "old": "[project]\nname = \"toy-project\"\nversion = \"0.1.0\"\n",
                            "new": "[project]\nname = \"toy-project\"\nversion = \"0.2.0\"\n",
                            "reason": "This file is intentionally outside the edit scope.",
                        }
                    ],
                },
            )
            with self.assertRaises(PatchValidationError) as caught:
                apply_patch_edits(run_dir, edits_file=edits_path)
            self.assertIn("path is not editable by the edit scope", str(caught.exception))

    def test_init_can_create_git_worktree_workspace(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git executable is not available")
        with _temporary_root() as root:
            code_root = root / "git_project"
            task_file = root / "task.md"
            _write_toy_project(code_root)
            shutil.rmtree(code_root / ".git")
            write_text(task_file, "# Task\n\nImprove the spam classifier.\n")
            _git(code_root, "init")
            _git(code_root, "config", "user.email", "test@example.com")
            _git(code_root, "config", "user.name", "SimpleAR Test")
            _git(code_root, "config", "core.autocrlf", "false")
            _git(code_root, "add", ".")
            _git(code_root, "commit", "-m", "initial")

            run_dir = root / "runs" / "git-worktree-run"
            binding = workspace_source_binding(WorkspaceSpec(
                code_root, run_dir / "code_task", "git_worktree", max_file_bytes=10_000,
            ))
            result = initialize_code_task(
                run_dir=run_dir,
                code_root=code_root,
                task_file=task_file,
                benchmark_command="python -m unittest discover -s tests",
                workspace_mode="git_worktree",
                max_file_bytes=10_000,
            )

            self.assertTrue((result.workspace_dir / "spam_model.py").is_file())
            self.assertTrue((result.workspace_dir / ".git").exists())
            self.assertEqual(binding["source_snapshots"][result.workspace.selected_mode],
                             implementation_file_hashes(run_dir))
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["workspace"]["mode"], "git_worktree")
            self.assertEqual(manifest["workspace"]["workspace_dir"], "code_task/workspace")
            self.assertEqual(manifest["workspace"]["project_root"], "code_task/workspace")
            self.assertEqual(manifest["copy"]["files_copied"], 0)
            self.assertTrue(manifest["workspace"]["git"]["origin_commit"])
            self.assertEqual(manifest["workspace"]["environment_mapping"]["mode"], "git_worktree")
            index = read_json(result.codebase_index_path)
            self.assertEqual(index["project"]["python_file_count"], 2)
            self.assertNotIn(".env", {item["path"] for item in index["files"]})

    def test_auto_workspace_prefers_git_worktree_for_git_project(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git executable is not available")
        with _temporary_root() as root:
            code_root = root / "git_project"
            task_file = root / "task.md"
            _write_toy_project(code_root)
            shutil.rmtree(code_root / ".git")
            write_text(task_file, "# Task\n\nImprove the spam classifier.\n")
            _git(code_root, "init")
            _git(code_root, "config", "user.email", "test@example.com")
            _git(code_root, "config", "user.name", "SimpleAR Test")
            _git(code_root, "config", "core.autocrlf", "false")
            _git(code_root, "add", ".")
            _git(code_root, "commit", "-m", "initial")

            run_dir = root / "runs" / "auto-worktree-run"
            binding = workspace_source_binding(WorkspaceSpec(
                code_root, run_dir / "code_task", "auto", max_file_bytes=10_000,
            ))
            result = initialize_code_task(
                run_dir=run_dir,
                code_root=code_root,
                task_file=task_file,
                workspace_mode="auto",
                max_file_bytes=10_000,
            )

            self.assertEqual(result.workspace.mode, "git_worktree")
            self.assertEqual(result.workspace.requested_mode, "auto")
            self.assertEqual(result.workspace.selected_mode, "git_worktree")
            self.assertTrue((result.workspace.workspace_dir / ".git").exists())
            self.assertEqual(binding["source_snapshots"][result.workspace.selected_mode],
                             implementation_file_hashes(run_dir))
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["workspace"]["requested_mode"], "auto")
            self.assertEqual(manifest["workspace"]["selected_mode"], "git_worktree")
            self.assertEqual(manifest["workspace"]["fallback_reason"], "")

            self.assertEqual(set(binding["source_snapshots"]), {"copy", "git_worktree"})
            write_text(code_root / "uncommitted.py", "VALUE = 7\n")
            dirty_binding = workspace_source_binding(WorkspaceSpec(
                code_root, root / "runs/dirty-auto/code_task", "auto",
            ))
            self.assertEqual(set(dirty_binding["source_snapshots"]), {"copy"})
            dirty = initialize_code_task(
                run_dir=root / "runs" / "dirty-auto", code_root=code_root,
                task_file=task_file, workspace_mode="auto",
            )
            self.assertEqual(dirty_binding["source_snapshots"]["copy"],
                             implementation_file_hashes(dirty.run_dir))
            self.assertEqual(dirty.workspace.mode, "copy")
            self.assertIn("uncommitted", dirty.workspace.fallback_reason)
            self.assertEqual((dirty.workspace_dir / "uncommitted.py").read_text(), "VALUE = 7\n")

    def test_git_worktree_supports_project_subdirectory(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git executable is not available")
        with _temporary_root() as root:
            repo = root / "repo"
            code_root = repo / "package"
            code_root.mkdir(parents=True)
            write_text(code_root / "module.py", "VALUE = 1\n")
            write_text(repo / "root_only.py", "ROOT_VALUE = 1\n")
            task_file = root / "task.md"
            write_text(task_file, "# Task\n\nChange VALUE.\n")
            _git(repo, "init")
            _git(repo, "config", "user.email", "test@example.com")
            _git(repo, "config", "user.name", "SimpleAR Test")
            _git(repo, "config", "core.autocrlf", "false")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "initial")

            run_dir = root / "runs" / "subdir-worktree-run"
            binding = workspace_source_binding(WorkspaceSpec(
                code_root, run_dir / "code_task", "git_worktree",
            ))
            result = initialize_code_task(
                run_dir=run_dir,
                code_root=code_root,
                task_file=task_file,
                workspace_mode="git_worktree",
            )

            self.assertEqual(result.workspace_dir, result.workspace.project_root)
            self.assertTrue((result.workspace_dir / "module.py").is_file())
            self.assertFalse((result.workspace_dir / ".git").exists())
            self.assertTrue((result.workspace.workspace_dir / ".git").exists())
            self.assertEqual(binding["source_snapshots"]["git_worktree"],
                             implementation_file_hashes(run_dir))
            manifest = read_json(root / "runs" / "subdir-worktree-run" / "manifest.json")
            self.assertEqual(manifest["workspace"]["mode"], "git_worktree")
            self.assertEqual(manifest["workspace"]["workspace_dir"], "code_task/workspace")
            self.assertEqual(manifest["workspace"]["project_root"], "code_task/workspace/package")
            self.assertEqual(manifest["workspace"]["project_relative_path"], "package")
            self.assertIn("subdirectory", " ".join(manifest["workspace"]["warnings"]))
            index = read_json(result.codebase_index_path)
            self.assertEqual(index["project"]["file_count"], 1)
            proposal = run_dir / "code_task" / "meta" / "escape_proposal.json"
            write_json(
                proposal,
                {
                    "edits": [
                        {
                            "path": "../root_only.py",
                            "old": "ROOT_VALUE = 1",
                            "new": "ROOT_VALUE = 2",
                            "reason": "This must not escape the package project root.",
                        }
                    ]
                },
            )
            with self.assertRaises(PatchValidationError):
                apply_patch_edits(run_dir, edits_file=proposal, allow_unapproved_plan=True)
            self.assertEqual(read_text(repo / "root_only.py"), "ROOT_VALUE = 1\n")

    def test_auto_workspace_falls_back_to_copy_for_non_git_project(self) -> None:
        with _temporary_root() as root:
            code_root = root / "plain_project"
            task_file = root / "task.md"
            _write_toy_project(code_root)
            write_text(task_file, "# Task\n\nImprove the plain project.\n")

            run_dir = root / "runs" / "auto-copy-run"
            result = initialize_code_task(
                run_dir=run_dir,
                code_root=code_root,
                task_file=task_file,
                workspace_mode="auto",
                max_file_bytes=10_000,
            )

            self.assertEqual(result.workspace.mode, "copy")
            self.assertEqual(result.workspace.requested_mode, "auto")
            self.assertEqual(result.workspace.selected_mode, "copy")
            self.assertTrue((result.workspace_dir / "spam_model.py").is_file())
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["workspace"]["requested_mode"], "auto")
            self.assertEqual(manifest["workspace"]["selected_mode"], "copy")
            self.assertTrue(manifest["workspace"]["fallback_reason"])
            self.assertTrue(manifest["workspace"]["user_next_steps"])

    def test_init_can_create_sparse_copy_workspace(self) -> None:
        with _temporary_root() as root:
            code_root = root / "sparse_project"
            task_file = root / "task.md"
            write_text(code_root / "src" / "pkg" / "model.py", "def predict():\n    return 1\n")
            write_text(code_root / "tests" / "test_model.py", "def test_predict():\n    assert True\n")
            write_text(code_root / "benchmark.py", "print('accuracy: 1.0')\n")
            write_text(code_root / "pyproject.toml", "[project]\nname = 'sparse-project'\n")
            write_text(code_root / "data" / "dataset.csv", "id,label\n1,spam\n")
            write_text(code_root / "models" / "weights.bin", "not really weights\n")
            write_text(code_root / ".env", "TOKEN=secret\n")
            write_text(task_file, "# Task\n\nImprove sparse project.\n")

            run_dir = root / "runs" / "sparse-run"
            binding = workspace_source_binding(WorkspaceSpec(
                code_root, run_dir / "code_task", "sparse_copy", include=("src/**", "tests/**", "benchmark.py", "pyproject.toml"),
                exclude=("models/**",),
            ))
            result = initialize_code_task(
                run_dir=run_dir,
                code_root=code_root,
                task_file=task_file,
                benchmark_command="python benchmark.py",
                workspace_mode="sparse_copy",
                workspace_include=(" src\\**/ ", "tests/**", "benchmark.py", "pyproject.toml", "src/**", " "),
                workspace_exclude=(" models\\**/ ", "models/**", " "),
            )

            workspace = result.workspace_dir
            self.assertTrue((workspace / "src" / "pkg" / "model.py").is_file())
            self.assertTrue((workspace / "tests" / "test_model.py").is_file())
            self.assertTrue((workspace / "benchmark.py").is_file())
            self.assertTrue((workspace / "pyproject.toml").is_file())
            self.assertFalse((workspace / "data" / "dataset.csv").exists())
            self.assertFalse((workspace / "models" / "weights.bin").exists())
            self.assertFalse((workspace / ".env").exists())

            self.assertEqual(binding["source_snapshots"][result.workspace.selected_mode],
                             implementation_file_hashes(run_dir))
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["workspace"]["mode"], "sparse_copy")
            self.assertEqual(
                manifest["workspace"]["patterns"]["include"],
                ["src/**", "tests/**", "benchmark.py", "pyproject.toml"],
            )
            self.assertIn("models/**", manifest["workspace"]["patterns"]["exclude"])
            skipped_reasons = {item["reason"] for item in manifest["copy"]["skipped"]}
            self.assertIn("sparse_excluded_dir", skipped_reasons)

    def test_code_task_init_cli_prints_summary(self) -> None:
        with _temporary_root() as root:
            code_root = root / "toy_project"
            task_file = root / "task.md"
            output_root = root / "runs"
            _write_toy_project(code_root)
            write_text(task_file, "# Task\n\nImprove tests.\n")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main(
                    [
                        "code-task",
                        "init",
                        "--code-root",
                        str(code_root),
                        "--task-file",
                        str(task_file),
                        "--output-root",
                        str(output_root),
                        "--name",
                        "demo-code-task",
                    ]
                )

            output = stdout.getvalue()
            self.assertIn("Code task run:", output)
            self.assertIn("Workspace:", output)
            self.assertIn("Indexed:", output)
            self.assertIn("Repo map:", output)
            run_dir = next(output_root.iterdir())
            self.assertTrue((run_dir / "code_task" / "workspace" / "spam_model.py").is_file())
            self.assertTrue((run_dir / "code_task" / "meta" / "codebase_index.json").is_file())
            self.assertTrue((run_dir / "code_task" / "meta" / "repo_map.json").is_file())

            status_stdout = io.StringIO()
            with contextlib.redirect_stdout(status_stdout):
                main(["status", str(run_dir)])

            status_text = status_stdout.getvalue()
            self.assertIn("Workflow: code_task", status_text)
            self.assertIn("python files: 2", status_text)

    def test_code_task_map_rebuilds_repo_map_from_workspace(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nMap this project.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )
            write_text(
                run_dir / "code_task" / "workspace" / "feature.py",
                "def improve_feature(value):\n    return value + 1\n",
            )

            result = build_code_task_repo_map(run_dir)

            self.assertTrue(result.refreshed_index)
            repo_map = read_json(result.repo_map_path)
            mapped_files = {item["path"] for item in repo_map["files"]}
            self.assertIn("feature.py", mapped_files)
            symbols = {item["qualified_name"] for item in repo_map["symbols"]}
            self.assertIn("improve_feature", symbols)
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["layout"]["repo_map"], "code_task/meta/repo_map.json")
            self.assertEqual(
                manifest["codebase"]["repo_map"]["summary"],
                "code_task/meta/repo_map_summary.md",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main(["code-task", "map", str(run_dir), "--no-refresh-index"])
            output = stdout.getvalue()
            self.assertIn("Repo map:", output)
            self.assertIn("Index refreshed: False", output)

    def test_code_task_locate_writes_ranked_targets_and_evidence(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nImprove spam keyword prediction.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )

            result = locate_code_task_context(
                run_dir,
                query="improve spam keyword predict behavior",
                top_k=4,
            )

            self.assertTrue(result.results_path.is_file())
            self.assertTrue(result.summary_path.is_file())
            self.assertEqual(result.editable_targets[0]["path"], "spam_model.py")
            evidence_paths = {row["path"] for row in result.read_only_evidence}
            self.assertIn("tests/test_spam_model.py", evidence_paths)
            locate_data = read_json(result.results_path)
            self.assertEqual(locate_data["schema_version"], 1)
            self.assertIn("spam", locate_data["query_terms"])
            summary = read_text(result.summary_path)
            self.assertIn("# Locate Results", summary)
            self.assertIn("spam_model.py", summary)
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["layout"]["locate_results"], "code_task/meta/locate_results.json")
            self.assertEqual(manifest["locate"]["status"], "completed")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main(
                    [
                        "code-task",
                        "locate",
                        str(run_dir),
                        "--query",
                        "spam keyword",
                        "--top-k",
                        "3",
                    ]
                )
            output = stdout.getvalue()
            self.assertIn("Locate results:", output)
            self.assertIn("Editable targets:", output)

    def test_code_task_context_pack_writes_prompt_and_snippets(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nImprove spam keyword prediction.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )

            result = build_code_task_context_pack(
                run_dir,
                query="improve spam keyword predict behavior",
                top_k=4,
                max_files=3,
                max_source_chars_per_file=600,
                max_total_chars=1200,
            )

            self.assertTrue(result.context_pack_path.is_file())
            self.assertTrue(result.prompt_context_path.is_file())
            self.assertTrue(result.snippets_path.is_file())
            self.assertIn("spam_model.py", result.selected_files)
            snippets = read_jsonl(result.snippets_path)
            snippet_paths = {row["path"] for row in snippets}
            self.assertIn("spam_model.py", snippet_paths)
            self.assertIn("tests/test_spam_model.py", snippet_paths)
            prompt_context = read_text(result.prompt_context_path)
            self.assertIn("# Code Task Context Pack", prompt_context)
            self.assertIn("## Editable Targets", prompt_context)
            self.assertIn("## Read-Only Evidence", prompt_context)
            context_pack = read_json(result.context_pack_path)
            self.assertEqual(context_pack["schema_version"], 1)
            self.assertLessEqual(context_pack["budget"]["used_chars"], 1200)
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["layout"]["context_packs"], "code_task/context_packs")
            self.assertEqual(manifest["context_pack"]["status"], "completed")

    def test_context_pack_preserves_required_config_over_similar_reports(self) -> None:
        with _temporary_root() as root:
            code_root = root / "toy_project"
            _write_toy_project(code_root)
            write_text(code_root / "config" / "active.toml", "seed = 0\n")
            write_text(code_root / "config" / "old" / "report.json", '{"seed": 0}\n')
            task_file = root / "task.md"
            write_text(task_file, "Inspect spam_model.py using the accepted execution config.\n")
            run_dir = root / "runs" / "code-task-run"
            initialize_code_task(
                run_dir=run_dir,
                code_root=code_root,
                task_file=task_file,
                benchmark_command="python -m unittest discover -s tests",
                edit_scope_allowed_patterns=("spam_model.py",),
                edit_scope_protected_patterns=("config/active.toml",),
            )
            manifest = read_json(run_dir / "manifest.json")
            manifest["context_requirements"] = {"read_only_paths": ["config/active.toml"]}
            write_json(run_dir / "manifest.json", manifest)

            result = build_code_task_context_pack(
                run_dir,
                top_k=4,
                max_files=2,
                max_source_chars_per_file=600,
                max_total_chars=1200,
            )

            self.assertIn("spam_model.py", result.selected_files)
            self.assertIn("config/active.toml", result.selected_files)
            config = next(
                row for row in read_jsonl(result.snippets_path)
                if row["path"] == "config/active.toml"
            )
            self.assertEqual(config["access_role"], "read_only_evidence")
            self.assertIn("required read-only context", config["reasons"])
            self.assertEqual(
                read_json(result.context_pack_path)["required_read_only_paths"],
                ["config/active.toml"],
            )

    def test_explicit_task_path_matching_has_path_boundaries(self) -> None:
        from simple_ar.code_task.analysis.locate import _mentions_workspace_path

        self.assertTrue(_mentions_workspace_path("Use `config/active.toml`.", "config/active.toml"))
        self.assertFalse(_mentions_workspace_path("Use config/active.toml.bak.", "config/active.toml"))
        self.assertFalse(_mentions_workspace_path("Use other/config/active.toml.", "config/active.toml"))

    def test_dependency_api_probe_only_accepts_imported_public_symbols(self) -> None:
        from simple_ar.code_task.analysis.dependency_api import inspect_dependency_api

        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            with patch("simple_ar.code_task.analysis.dependency_api.subprocess.run") as run:
                run.return_value = SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps([{
                        "module": "example_embeddings", "status": "observed",
                        "signatures": {"compute_bins": "(X, n_bins=48)"},
                    }]),
                )
                result = inspect_dependency_api(
                    [
                        {"module": "not_imported", "symbols": ["danger"]},
                        {"module": "example_embeddings", "symbols": ["compute_bins", "__dict__"]},
                    ],
                    {"files": [{"path": "model.py", "python": {"imports": ["example_embeddings"]}}]},
                    ["model.py"],
                    python_executable=sys.executable,
                    workspace_dir=Path(tmp),
                )
                self.assertEqual(result["status"], "observed")
                self.assertEqual(result["request"], [
                    {"module": "example_embeddings", "symbols": ["compute_bins"]},
                ])
                self.assertEqual(run.call_args.args[0][1], "-I")
                self.assertEqual(run.call_args.kwargs["timeout"], 12)

    def test_cached_context_rebuilds_when_protocol_adds_required_evidence(self) -> None:
        from simple_ar.code_task.orchestration.execute import _ensure_context_pack_for_current_batch

        with _temporary_root() as root:
            code_root = root / "project"
            _write_toy_project(code_root)
            write_text(code_root / "config" / "active.toml", "seed = 0\n")
            write_text(root / "task.md", "Edit spam_model.py only.")
            run_dir = root / "run"
            initialize_code_task(
                run_dir=run_dir, code_root=code_root, task_file=root / "task.md",
                benchmark_command="python -m unittest discover -s tests",
                edit_scope_allowed_patterns=("spam_model.py",),
                edit_scope_protected_patterns=("config/active.toml",),
            )
            original = build_code_task_context_pack(run_dir, max_files=2)
            self.assertNotIn("config/active.toml", original.selected_files)
            manifest = read_json(run_dir / "manifest.json")
            manifest["context_requirements"] = {"read_only_paths": ["config/active.toml"]}
            write_json(run_dir / "manifest.json", manifest)

            _ensure_context_pack_for_current_batch(
                run_dir, max_files=2, max_source_chars_per_file=600,
                message_callback=None,
            )

            latest = read_json(run_dir / "manifest.json")["context_pack"]["latest"]
            self.assertNotEqual(latest, "code_task/context_packs/context-001/context_pack.json")
            self.assertIn("config/active.toml", [
                row["path"] for row in read_json(run_dir / latest)["selected_files"]
            ])

    def test_edit_proposal_can_request_bounded_dependency_evidence(self) -> None:
        with _temporary_root() as root:
            code_root = root / "project"
            _write_toy_project(code_root)
            write_text(code_root / "spam_model.py", "import example_embeddings\n" + read_text(code_root / "spam_model.py"))
            write_text(root / "task.md", "Improve spam prediction using example_embeddings.")
            run_dir = root / "run"
            initialize_code_task(
                run_dir=run_dir, code_root=code_root, task_file=root / "task.md",
                benchmark_command="python -m unittest discover -s tests",
            )
            _approve_offline_patch_plan(run_dir)
            client = Mock()
            client.ask_json.side_effect = [
                {"edits": [], "context_request": {"dependency_symbols": [
                    {"module": "example_embeddings", "symbols": ["compute_bins"]},
                ]}},
                {"edits": [], "summary": "Observed interface does not fit the approved design."},
            ]
            observed = {"status": "observed", "interfaces": [{
                "module": "example_embeddings", "status": "observed",
                "signatures": {"compute_bins": "(X, n_bins=48)"},
            }]}
            with patch("simple_ar.code_task.editing.patching.LLMClient.for_task", return_value=client), patch(
                "simple_ar.code_task.editing.patching.inspect_dependency_api", return_value=observed,
            ) as probe:
                propose_patch_edits(run_dir, use_llm=True)
            self.assertEqual(client.ask_json.call_count, 2)
            self.assertEqual(probe.call_count, 1)
            self.assertIn("compute_bins", client.ask_json.call_args.args[1])
            self.assertEqual(read_json(run_dir / "code_task/meta/dependency_api.json"), observed)

    def test_edit_proposal_can_read_source_after_dependency_evidence(self) -> None:
        with _temporary_root() as root:
            code_root = root / "project"
            _write_toy_project(code_root)
            original = read_text(code_root / "spam_model.py")
            write_text(code_root / "config" / "calibration.py", "# calibration padding\n" * 50 + "FACTOR = 2\n")
            write_text(code_root / "spam_model.py", "import example_embeddings\n" + "# padding\n" * 12 + original)
            write_text(root / "task.md", "Improve spam prediction using example_embeddings.")
            run_dir = root / "run"
            initialize_code_task(
                run_dir=run_dir, code_root=code_root, task_file=root / "task.md",
                benchmark_command="python -m unittest discover -s tests",
            )
            _approve_offline_patch_plan(run_dir)
            client = Mock()
            client.ask_json.side_effect = [
                {"edits": [], "context_request": {"dependency_symbols": [
                    {"module": "example_embeddings", "symbols": ["compute_bins"]},
                ]}},
                {"edits": [], "summary": "Need the remainder of the selected source."},
                {"edits": [{"path": "spam_model.py",
                    "old": "return 'spam' if 'win' in text.lower() else 'ham'",
                    "new": "return 'spam' if 'prize' in text.lower() else 'ham'",
                    "reason": "Draft local change; calibration remains unread."}],
                 "context_request": {"files": ["config/calibration.py"],
                    "query": "FACTOR", "reason": "The newly read implementation depends on calibration."}},
                {"edits": [{
                    "path": "spam_model.py",
                    "old": "return 'spam' if 'win' in text.lower() else 'ham'",
                    "new": "return 'spam' if 'prize' in text.lower() else 'ham'",
                    "reason": "Use the requested keyword.",
                }]},
            ]
            observed = {"status": "observed", "interfaces": [{
                "module": "example_embeddings", "status": "observed",
                "signatures": {"compute_bins": "(X, n_bins=48)"},
            }]}
            with patch("simple_ar.code_task.editing.patching.LLMClient.for_task", return_value=client), patch(
                "simple_ar.code_task.editing.patching.inspect_dependency_api", return_value=observed,
            ):
                result = propose_patch_edits(run_dir, use_llm=True, max_source_chars_per_file=180)
            self.assertEqual(result.edit_count, 1)
            self.assertEqual(client.ask_json.call_count, 4)
            self.assertIn("def predict", client.ask_json.call_args_list[2].args[1])
            self.assertIn("FACTOR = 2", client.ask_json.call_args_list[3].args[1])
            self.assertIn("Unapplied draft edits", client.ask_json.call_args_list[3].args[1])
            self.assertIn("Draft local change; calibration remains unread.", client.ask_json.call_args_list[3].args[1])
            followup = read_json(run_dir / "code_task/meta/edit_context_followup.json")
            self.assertEqual(len(followup["rounds"]), 3)
            self.assertEqual(followup["rounds"][0]["snippets"], [])
            self.assertTrue(followup["rounds"][1]["snippets"])
            self.assertEqual(followup["rounds"][2]["snippets"][0]["path"], "config/calibration.py")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main(
                    [
                        "code-task",
                        "context",
                        str(run_dir),
                        "--query",
                        "spam keyword",
                        "--max-files",
                        "2",
                    ]
                )
            output = stdout.getvalue()
            self.assertIn("Context pack:", output)
            self.assertIn("Selected files:", output)

    def test_work_plan_offline_writes_batchable_items_and_manifest(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nImprove spam keyword prediction without changing tests.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )
            build_code_task_context_pack(
                run_dir,
                query="improve spam keyword prediction",
                top_k=4,
                max_files=3,
            )

            result = generate_code_task_work_plan(run_dir, use_llm=False)

            self.assertEqual(result.mode, "offline")
            self.assertTrue(result.pending_approval)
            self.assertEqual(result.item_count, 1)
            self.assertTrue(result.work_plan_path.is_file())
            self.assertTrue(result.work_plan_markdown_path.is_file())
            plan = read_json(result.work_plan_path)
            self.assertEqual(plan["schema_version"], 1)
            self.assertEqual(plan["items"][0]["id"], "W1")
            self.assertIn("spam_model.py", plan["items"][0]["target_files"])
            self.assertNotIn("tests/test_spam_model.py", plan["items"][0]["target_files"])
            self.assertIn("tests/test_spam_model.py", plan["items"][0]["read_only_evidence"])
            markdown = read_text(result.work_plan_markdown_path)
            self.assertIn("# Work Plan", markdown)
            self.assertIn("## Work Items", markdown)
            self.assertIn("W1", markdown)

            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["status"], "work_planned")
            self.assertEqual(manifest["layout"]["work_plan"], "code_task/work_plan.json")
            self.assertEqual(manifest["work_plan"]["status"], "pending_approval")
            self.assertEqual(manifest["work_plan"]["item_count"], 1)

    def test_work_plan_creation_targets_reach_batch_without_fabricated_sources(self) -> None:
        with _temporary_root() as root:
            _, _, run_dir = _initialize_toy_task(root, "Create the required adapter and a planned helper.")
            _approve_offline_patch_plan(run_dir)
            paths = code_task_paths(run_dir)
            manifest = read_json(paths.manifest_path)
            manifest['code_task']['native_binding'] = {'binding': {'contract': {
                'new_files': ['src/adapter.py']}}}
            manifest['edit_scope']['allowed_patterns'] = ['spam_model.py', 'src/*.py']
            write_json(paths.manifest_path, manifest)
            write_text(paths.task_dir / 'patch_plan.md', '# Patch Plan\n\n## New Files\n\n'
                       '- `src/patch_adapter.py`: Accepted model-selected creation.\n\n## Validation\nCheck source.\n')
            original_index = read_json(paths.meta_dir / 'codebase_index.json')
            data = {'new_files': [{'path': 'src/helper.py', 'reason': 'Model-selected helper'},
                                  {'path': '../escape.py'}, {'path': 'tests/new_test.py'}],
                'items': [{'id': 'W1', 'target_files': ['src/adapter.py', 'src/patch_adapter.py', 'src/helper.py', 'spam_model.py',
                                                     'src/undeclared.py', 'tests/new_test.py'],
                    'read_only_evidence': ['src/adapter.py', 'tests/test_spam_model.py'],
                    'context_request': {'files': ['src/helper.py', 'spam_model.py']}}]}
            with patch('simple_ar.code_task.editing.work_plan._generate_plan_data', return_value=(data, 'llm')):
                result = generate_code_task_work_plan(run_dir, use_llm=False)
            plan = read_json(result.work_plan_path)
            self.assertEqual(plan['new_files'], ['src/adapter.py', 'src/patch_adapter.py', 'src/helper.py'])
            self.assertEqual(plan['items'][0]['target_files'],
                             ['src/adapter.py', 'src/patch_adapter.py', 'src/helper.py', 'spam_model.py'])
            self.assertEqual(plan['items'][0]['read_only_evidence'], ['tests/test_spam_model.py'])
            self.assertEqual(plan['items'][0]['context_request']['files'], ['spam_model.py'])
            self.assertNotIn('src/adapter.py', plan['selected_files'])
            self.assertEqual(read_json(paths.meta_dir / 'codebase_index.json'), original_index)
            self.assertFalse((paths.workspace_dir / 'src/adapter.py').exists())
            batch = create_code_task_batch(run_dir, work_item_id='W1')
            self.assertEqual(read_json(batch.batch_state_path)['work_item']['target_files'],
                             plan['items'][0]['target_files'])
            record_plan_decision(run_dir, decision='approve')
            client = _FakeRepairClient({'edits': [
                {'path': name, 'old': '', 'new': 'VALUE = 1\n', 'reason': 'Create actual source'}
                for name in ('src/adapter.py', 'src/patch_adapter.py', 'src/helper.py', 'src/undeclared.py')]})
            with patch('simple_ar.code_task.editing.patching.LLMClient.from_env', return_value=client):
                proposed = propose_patch_edits(run_dir, use_llm=True)
            self.assertEqual([row['path'] for row in read_json(proposed.proposal_path)['edits']],
                             ['src/adapter.py', 'src/patch_adapter.py', 'src/helper.py'])
            apply_patch_edits(run_dir, edits_file=proposed.proposal_path, allow_large_edits=True)
            self.assertEqual(read_text(paths.workspace_dir / 'src/adapter.py'), 'VALUE = 1\n')
            self.assertFalse((paths.workspace_dir / 'src/undeclared.py').exists())

    def test_work_plan_creation_fallback_keeps_scope_and_real_context(self) -> None:
        from simple_ar.code_task.editing.work_plan import _normalize_work_plan_data, _work_plan_user_prompt
        index = {'files': [{'path': 'main.py', 'kind': 'python'}]}
        context = {'new_files': ['src/adapter.py', 'src/private.py', '../escape.py', 'other.py',
                                'src/../escape.py', 'src//alias.py', ' src/spaced.py',
                                'src/*.py', 'src/ADAPTER.py', 'src\\backslash.py']}
        plan = _normalize_work_plan_data({}, index, selected_files=['main.py'], task_text='Create an adapter',
            benchmark_command='', mode='offline', run_context=context, context_pack=None,
            allowed_patterns=('main.py', 'src/*.py'), protected_patterns=('src/private.py',))
        self.assertEqual(plan['new_files'], ['src/adapter.py'])
        self.assertEqual(plan['items'][0]['target_files'], ['src/adapter.py', 'main.py'])
        self.assertEqual(plan['items'][0]['context_request']['files'], ['main.py'])
        self.assertEqual(plan['selected_files'], ['main.py'])
        prompt = _work_plan_user_prompt(task_text='Create adapter', index=index, snippets=[],
            benchmark_command='', run_context=context, memory_context='',
            allowed_patterns=('main.py', 'src/*.py'), protected_patterns=('src/private.py',))
        self.assertIn('not observed source', prompt)
        self.assertEqual(index['files'], [{'path': 'main.py', 'kind': 'python'}])

    def test_work_plan_and_batch_cli_create_attempt_state(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nImprove spam classifier accuracy.\n", init_cli=True,
            )

            work_plan_stdout = io.StringIO()
            with contextlib.redirect_stdout(work_plan_stdout):
                main(["code-task", "work-plan", str(run_dir), "--no-llm"])

            self.assertIn("Work plan:", work_plan_stdout.getvalue())
            self.assertTrue((run_dir / "code_task" / "work_plan.json").is_file())

            batch_stdout = io.StringIO()
            with contextlib.redirect_stdout(batch_stdout):
                main(["code-task", "batch", str(run_dir), "--work-item", "W1"])

            output = batch_stdout.getvalue()
            self.assertIn("Attempt: attempt-001", output)
            self.assertIn("Batch: batch-001", output)
            attempt_state = read_json(
                run_dir / "code_task" / "attempts" / "attempt-001" / "attempt_state.json"
            )
            batch_state = read_json(
                run_dir
                / "code_task"
                / "attempts"
                / "attempt-001"
                / "batches"
                / "batch-001"
                / "batch_state.json"
            )
            self.assertEqual(attempt_state["state"], "batching")
            self.assertEqual(batch_state["state"], "created")
            self.assertEqual(batch_state["work_item_id"], "W1")
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["status"], "batch_created")
            self.assertEqual(manifest["attempts"]["active"], "attempt-001")
            self.assertIn("latest_batch", manifest["attempts"])
            self.assertNotIn("items", manifest["attempts"])

    def test_work_plan_budget_tracks_cohesive_file_scope(self) -> None:
        items = _normalize_work_items(
            [
                {
                    "id": "W1",
                    "objective": "Change implementation and its configuration together.",
                    "target_files": ["features.py", "model.py", "config.json"],
                    "budget_profile": "normal",
                }
            ],
            {"features.py", "model.py", "config.json"},
            selected_files=["features.py", "model.py", "config.json"],
            allowed_patterns=(),
            protected_patterns=(),
        )

        self.assertEqual(items[0]["budget_profile"], "normal")
        self.assertFalse(items[0]["requires_budget_override"])
        paths = ["a.py", "b.py", "c.py", "d.py", "e.py"]
        oversized = _normalize_work_items(
            [{"id": "W1", "target_files": paths, "budget_profile": "normal"}],
            set(paths), selected_files=paths, allowed_patterns=(), protected_patterns=(),
        )
        self.assertEqual(oversized[0]["budget_profile"], "large")
        self.assertTrue(oversized[0]["requires_budget_override"])
        self.assertIn("more files than the normal", oversized[0]["suggested_budget_override"])

    def test_create_code_task_batch_reuses_existing_item_batch(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nImprove spam classifier accuracy.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )
            generate_code_task_work_plan(run_dir, use_llm=False)

            first = create_code_task_batch(run_dir, work_item_id="W1")
            second = create_code_task_batch(run_dir, work_item_id="W1")
            forced = create_code_task_batch(run_dir, work_item_id="W1", force=True)

            self.assertEqual(first.batch_id, "batch-001")
            self.assertEqual(second.batch_id, "batch-001")
            self.assertEqual(forced.batch_id, "batch-002")
            self.assertTrue(forced.batch_state_path.is_file())

    def test_execute_selects_first_implementation_work_item(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nImprove spam prediction.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )
            _write_analysis_first_work_plan(run_dir)

            result = execute_code_task(run_dir, use_llm=False, to_step="batch", timeout_sec=10)

            self.assertEqual(result.stop_reason, "stop_point")
            batch_state = read_json(
                run_dir
                / "code_task"
                / "attempts"
                / "attempt-001"
                / "batches"
                / "batch-001"
                / "batch_state.json"
            )
            self.assertEqual(batch_state["work_item_id"], "W2")
            self.assertIn("Implement", batch_state["work_item"]["objective"])

    def test_create_code_task_batch_respects_serial_merge_choice(self) -> None:
        cases = (
            (True, "Implement a coupled feature, scorer, and config change.",
             ["W1", "W2", "W3"], "merged_dependent_chain",
             ["spam_model.py", "extra.py", "pyproject.toml"], "large"),
            (False, "Implement a coupled feature safely.",
             ["W1"], "single_work_item", ["spam_model.py"], "normal"),
        )
        for merge, task, source_ids, scope, targets, budget in cases:
            with self.subTest(merge_dependent_chain=merge), _temporary_root() as root:
                # extra.py must exist before initialization copies the source.
                code_root = root / "toy_project"
                _write_toy_project(code_root)
                write_text(code_root / "extra.py", "VALUE = 1\n")
                task_file = root / "task.md"
                write_text(task_file, f"# Task\n\n{task}\n")
                run_dir = root / "runs" / "code-task-run"
                initialize_code_task(
                    run_dir=run_dir, code_root=code_root, task_file=task_file,
                    benchmark_command=TOY_BENCHMARK_COMMAND,
                )
                _write_dependent_work_plan(run_dir)

                # The merged case also protects the public default.
                options = {} if merge else {"merge_dependent_chain": False}
                result = create_code_task_batch(run_dir, work_item_id="W1", **options)

                batch_state = read_json(result.batch_state_path)
                self.assertEqual(batch_state["work_item_id"], "W1")
                work_item = batch_state["work_item"]
                self.assertEqual(work_item["source_work_item_ids"], source_ids)
                self.assertEqual(work_item["execution_scope"], scope)
                self.assertEqual(work_item["target_files"], targets)
                self.assertEqual(work_item["budget_profile"], budget)
                self.assertEqual(work_item["requires_budget_override"], merge)

    def test_record_plan_decision_updates_work_plan_approval(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nApprove both plans.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )
            generate_code_task_work_plan(run_dir, use_llm=False)
            generate_patch_plan(run_dir, use_llm=False)

            record_plan_decision(run_dir, decision="approve", note="ready")

            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["plan"]["status"], "approved")
            self.assertEqual(manifest["work_plan"]["status"], "ready")
            self.assertEqual(manifest["work_plan"]["approval"]["status"], "approved")

    def test_probe_code_task_environment_writes_report_and_manifest(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nProbe this project.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )

            result = probe_code_task_environment(run_dir)

            self.assertTrue(result.report_path.is_file())
            self.assertIn(result.status, {"ok", "warning"})
            report = read_json(result.report_path)
            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(report["project"]["dependency_files"], ["pyproject.toml"])
            self.assertEqual(report["project"]["test_dirs"], ["tests"])
            self.assertTrue(report["tools"]["python"]["available"])
            self.assertEqual(report["execution_policy"]["mode"], "current")
            self.assertIn("available", report["gpu"])

            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["layout"]["environment_report"], "code_task/meta/environment_report.json")
            self.assertEqual(manifest["environment"]["report"], "code_task/meta/environment_report.json")
            self.assertEqual(manifest["status"], "environment_probed")
            summary = read_text(run_dir / "code_task" / "summary.md")
            self.assertIn("## Environment", summary)
            self.assertIn("pyproject.toml", summary)

    def test_code_task_probe_cli_prints_environment_summary(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nProbe from CLI.\n", init_cli=True,
            )

            probe_stdout = io.StringIO()
            with contextlib.redirect_stdout(probe_stdout):
                main(["code-task", "probe", str(run_dir)])

            output = probe_stdout.getvalue()
            self.assertIn("Environment report:", output)
            self.assertIn("Status:", output)
            status_stdout = io.StringIO()
            with contextlib.redirect_stdout(status_stdout):
                main(["status", str(run_dir)])
            self.assertIn("Environment:", status_stdout.getvalue())

    def test_patch_plan_offline_writes_reviewable_plan_and_updates_manifest(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nImprove spam keyword handling and keep the public predict API stable.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )

            result = generate_patch_plan(run_dir, use_llm=False)

            self.assertEqual(result.mode, "offline")
            self.assertTrue(result.pending_approval)
            self.assertTrue((run_dir / "code_task" / "patch_plan.md").is_file())
            plan_text = read_text(run_dir / "code_task" / "patch_plan.md")
            self.assertIn("# Patch Plan", plan_text)
            self.assertIn("## Files To Modify", plan_text)
            self.assertIn("spam_model.py", plan_text)
            self.assertIn("python -m unittest discover -s tests", plan_text)

            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["status"], "planned")
            self.assertEqual(manifest["plan"]["status"], "pending_approval")
            self.assertEqual(manifest["layout"]["patch_plan"], "code_task/patch_plan.md")

    def test_plan_and_propose_use_latest_context_pack_when_available(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nImprove spam keyword prediction without editing tests.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )
            context = build_code_task_context_pack(
                run_dir,
                query="improve spam keyword prediction",
                top_k=4,
                max_files=3,
            )

            plan = generate_patch_plan(run_dir, use_llm=False)

            self.assertIn("spam_model.py", plan.selected_files)
            plan_text = read_text(plan.patch_plan_path)
            self.assertIn("Context pack:", plan_text)
            self.assertIn("code_task/context_packs/context-001/context_pack.json", plan_text)
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(
                manifest["plan"]["context_pack"]["path"],
                "code_task/context_packs/context-001/context_pack.json",
            )
            self.assertEqual(
                manifest["plan"]["context_pack"]["prompt_context"],
                "code_task/context_packs/context-001/prompt_context.md",
            )

            record_plan_decision(run_dir, decision="approve")
            proposal = propose_patch_edits(run_dir, use_llm=False)

            self.assertEqual(proposal.edit_count, 0)
            proposal_data = read_json(proposal.proposal_path)
            self.assertEqual(
                proposal_data["context_pack"]["path"],
                "code_task/context_packs/context-001/context_pack.json",
            )
            self.assertEqual(proposal_data["selected_files"], ["spam_model.py"])
            self.assertIn("tests/test_spam_model.py", proposal_data["read_only_context"])
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(
                manifest["patch"]["context_pack"]["path"],
                "code_task/context_packs/context-001/context_pack.json",
            )
            self.assertTrue(context.context_pack_path.is_file())

    def test_failed_llm_edit_is_not_an_offline_proposal_and_can_retry(self) -> None:
        from simple_ar.integrations.llm import LLMResponseError
        with _temporary_root() as root:
            code_root = root / "project"
            _write_toy_project(code_root)
            write_text(root / "task.md", "Improve spam prediction.")
            run_dir = root / "run"
            initialize_code_task(run_dir=run_dir, code_root=code_root,
                                 task_file=root / "task.md",
                                 benchmark_command="python -m unittest discover -s tests")
            _approve_offline_patch_plan(run_dir)
            with patch("simple_ar.code_task.editing.patching.LLMClient.for_task"), patch(
                "simple_ar.code_task.editing.patching._ask_llm_for_edits",
                side_effect=LLMError("provider 524; prior consumption is unknown"),
            ):
                with self.assertRaisesRegex(LLMError, "provider 524; prior consumption is unknown"):
                    propose_patch_edits(run_dir, use_llm=True)
            self.assertFalse((run_dir / "code_task/meta/proposed_edits.json").exists())
            with patch("simple_ar.code_task.editing.patching.LLMClient.for_task") as factory:
                factory.return_value.ask_json.side_effect = [
                    LLMResponseError("invalid JSON", raw_response='{"edits": [broken first draft'),
                    LLMResponseError("invalid JSON", raw_response='{"edits": [broken corrected draft'),
                ]
                with self.assertRaisesRegex(LLMError, "invalid JSON"):
                    propose_patch_edits(run_dir, use_llm=True)
                self.assertEqual(factory.return_value.ask_json.call_count, 2)
                requests = factory.return_value.ask_json.call_args_list
                self.assertEqual(requests[1].kwargs['label'], 'code-task-propose-edits-format')
                self.assertIn('broken first draft', requests[1].args[1])
                rejected = list((run_dir / 'code_task/meta').glob('edit_response_invalid_*.txt'))
                self.assertEqual(len(rejected), 2)
                self.assertEqual({path.read_text() for path in rejected},
                                 {'{"edits": [broken first draft', '{"edits": [broken corrected draft'})
                self.assertFalse((run_dir / 'code_task/meta/proposed_edits.json').exists())
            with patch("simple_ar.code_task.editing.patching.LLMClient.for_task") as factory:
                factory.return_value.ask_json.side_effect = [
                    LLMResponseError('invalid JSON', raw_response='incomplete proposal'),
                    _FakeCodeTaskClient().ask_json('', '', label='code-task-propose-edits'),
                ]
                recovered = propose_patch_edits(run_dir, use_llm=True)
                self.assertEqual(factory.return_value.ask_json.call_count, 2)
            self.assertEqual(recovered.mode, "llm")
            self.assertEqual(recovered.edit_count, 1)

    def test_requested_symbol_context_replaces_clipped_prefix_with_bounded_window(self) -> None:
        from simple_ar.code_task.editing.patching import _requested_source_context
        from simple_ar.code_task.analysis.index import build_codebase_index
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            body = "# padding\n" * 100 + "def end_task():\n    return 'checkpoint'\n"
            write_text(root / "lifecycle.py", body)
            index = build_codebase_index(root)
            request = {"files": [], "symbols": ["end_task"], "query": "lifecycle"}
            result = _requested_source_context(root, index, request,
                supplied=[{"path": "lifecycle.py", "text": body[:200]}], max_files=1, max_chars=200)
            self.assertEqual(len(result), 1)
            self.assertIn("def end_task", result[0]["text"])
            self.assertLessEqual(len(result[0]["text"]), 200)
            self.assertEqual(result[0]["access_role"], "read_only")
            self.assertGreater(result[0]["source_offset"], 0)

            # Type declarations are Python source too. A generic prose hit
            # must not replace the specifically requested declaration.
            for suffix in (".py", ".pyi", ".md"):
                with self.subTest(suffix=suffix):
                    path = f"public_api{suffix}"
                    source = "# complete description\n" * 200 + "class Series:\n    def preview(self, count: int) -> list[int]: ...\n"
                    write_text(root / path, source)
                    declaration = _requested_source_context(root, build_codebase_index(root),
                        {"files": [path], "symbols": ["Series"], "query": "complete declaration"},
                        supplied=[], max_files=1, max_chars=160, max_total_chars=160)
                    self.assertEqual(len(declaration), 1)
                    self.assertIn("class Series", declaration[0]["text"])
                    self.assertEqual(source[declaration[0]["source_offset"]:][:len(declaration[0]["text"])], declaration[0]["text"])

            # A middle observation leaves a small header and a large unseen
            # definition body. The header must not consume the only window,
            # even when a constructor call precedes the requested definition.
            source = ("def factory():\n    return Settings()\n\n"
                      "class Settings:\n    field = 1\n"
                      + "    # author configuration\n" * 12
                      + "    def record(self):\n        return {'result': self.field}\n")
            write_text(root / "settings.py", source)
            index = build_codebase_index(root)
            begin = source.index("class Settings")
            middle = source.index("    field")
            stop = middle + len("    field = 1\n")
            supplied = [{"path": "settings.py", "text": source[middle:stop],
                         "source_offset": middle}]
            for symbols in (["Settings"], []):
                with self.subTest(symbols=symbols):
                    result = _requested_source_context(
                        root, index, {"files": ["settings.py"], "symbols": symbols,
                                      "query": "Settings record"},
                        supplied=supplied, max_files=1, max_chars=len(source),
                        max_total_chars=len(source),
                    )
                    self.assertEqual(len(result), 1)
                    window = result[0]
                    self.assertEqual(window["source_offset"], stop)
                    self.assertIn("def record", window["text"])
                    self.assertEqual(window["text"], source[stop:])
                    self.assertLessEqual(len(window["text"]), len(source))

            # A seen declaration plus a later overlapping observation must
            # still expose the unobserved body, without returning seen bytes.
            supplied.extend([
                {"path": "settings.py", "text": source[begin:middle], "source_offset": begin},
                {"path": "settings.py", "text": source[middle - 3:stop], "source_offset": middle - 3},
            ])
            result = _requested_source_context(
                root, index, {"files": ["settings.py"], "symbols": ["Settings"]},
                supplied=supplied, max_files=1, max_chars=80, max_total_chars=80,
            )
            self.assertEqual(result[0]["source_offset"], stop)
            self.assertEqual(result[0]["text"], source[stop:stop + 80])
            self.assertEqual(result[0]["start_line"], source.count("\n", 0, stop) + 1)

            from simple_ar.code_task.analysis.context import _named_definition_offset, clip_source_snippet
            for declaration, name in (
                ("class Options:\n    field: int = 0\n", "Options"),
                ("def adapt():\n    return 1\n", "adapt"),
                ("async def adapt_async():\n    return 1\n", "adapt_async"),
            ):
                with self.subTest(declaration=declaration):
                    prefix = "from dataclasses import dataclass\n\n"
                    decorators = "@dataclass\n@registered(\n    enabled=True,\n)\n"
                    text = prefix + decorators + declaration
                    write_text(root / "decorated.py", text)
                    declaration_offset = len(prefix + decorators)
                    declaration_line = text.count("\n", 0, declaration_offset) + 1
                    anchor = _named_definition_offset(text, "decorated.py", name, [{
                        "path": "decorated.py", "name": name, "qualified_name": name,
                        "line_start": declaration_line}])
                    self.assertEqual(anchor, len(prefix))
                    initial = clip_source_snippet({"path": "decorated.py", "text": text[anchor:],
                        "source_offset": anchor, "source_chars": len(text), "start_line": 3}, max_chars=64)
                    self.assertTrue(initial["text"].startswith("@dataclass\n@registered("))
                    self.assertLessEqual(len(initial["text"]), 64)
                    old = {"path": "decorated.py", "text": text[declaration_offset:],
                           "source_offset": declaration_offset}
                    result = _requested_source_context(root, build_codebase_index(root),
                        {"files": ["decorated.py"], "symbols": [name]}, supplied=[old],
                        max_files=1, max_chars=64, max_total_chars=64)
                    self.assertEqual(len(result), 1)
                    self.assertEqual(result[0]["source_offset"], len(prefix))
                    self.assertEqual(result[0]["text"], decorators)
                    self.assertEqual(result[0]["start_line"], 3)
                    self.assertLessEqual(len(result[0]["text"]), 64)
                    self.assertEqual(_requested_source_context(root, build_codebase_index(root),
                        {"files": ["decorated.py"], "symbols": [name]}, supplied=[old, *result],
                        max_files=1, max_chars=64, max_total_chars=64), [])

    def test_truncated_editable_file_can_be_read_again_without_expanding_scope(self) -> None:
        with _temporary_root() as root:
            code_root = root / "project"
            _write_toy_project(code_root)
            source = read_text(code_root / "spam_model.py")
            write_text(code_root / "spam_model.py", ("# context padding\n" * 35) + source)
            write_text(root / "task.md", "Change the spam keyword within the existing model.")
            run_dir = root / "run"
            initialize_code_task(
                run_dir=run_dir, code_root=code_root, task_file=root / "task.md",
                benchmark_command="python -m unittest discover -s tests",
            )
            _approve_offline_patch_plan(run_dir)
            client = Mock()
            client.ask_json.side_effect = [
                {"edits": [], "summary": "The required behavior is beyond the visible prefix; its interface is missing.",
                 "validation": ["Source excerpt is truncated."],
                 "context_request": {"files": ["spam_model.py:tail"]},
                 "implementation_feedback": {"kind": "source_gap", "reason": "Need the rest of the file."}},
                {"edits": [], "context_request": {
                    "files": ["spam_model.py"], "query": "predict", "symbols": ["predict"]}},
                {"edits": [{"path": "spam_model.py",
                            "old": "return 'spam' if 'win' in text.lower() else 'ham'",
                            "new": "return 'spam' if 'prize' in text.lower() else 'ham'",
                            "reason": "Change the requested keyword."}]},
            ]
            with patch("simple_ar.code_task.editing.patching.LLMClient.for_task", return_value=client):
                result = propose_patch_edits(
                    run_dir, use_llm=True, max_source_chars_per_file=180,
                )
            self.assertEqual(result.edit_count, 1)
            self.assertEqual(client.ask_json.call_count, 3)
            self.assertIn("partial source", client.ask_json.call_args_list[1].args[1])
            followup = read_json(run_dir / "code_task/meta/edit_context_followup.json")
            self.assertEqual(followup["request"]["files"], ["spam_model.py"])
            self.assertGreater(followup["snippets"][0]["source_offset"], 0)
            self.assertIn("def predict", client.ask_json.call_args_list[2].args[1])
            self.assertIn("### spam_model.py (editable", client.ask_json.call_args_list[2].args[1])
            self.assertIn("'win'", read_text(run_dir / "code_task/workspace/spam_model.py"))

    def test_truncated_editable_file_gets_bounded_fallback_when_model_cannot_request_source(self) -> None:
        with _temporary_root() as root:
            code_root = root / "project"
            _write_toy_project(code_root)
            source = read_text(code_root / "spam_model.py")
            write_text(code_root / "spam_model.py", ("# context padding\n" * 35) + source)
            write_text(root / "task.md", "Change the spam keyword within the existing model.")
            run_dir = root / "run"
            initialize_code_task(
                run_dir=run_dir, code_root=code_root, task_file=root / "task.md",
                benchmark_command="python -m unittest discover -s tests",
            )
            _approve_offline_patch_plan(run_dir)
            client = Mock()
            client.ask_json.side_effect = [
                {"edits": [], "summary": "Cannot find the function in the displayed source."},
                {"edits": [], "context_request": {"files": ["spam_model.py:tail"]}},
                {"edits": [{"path": "spam_model.py",
                            "old": "return 'spam' if 'win' in text.lower() else 'ham'",
                            "new": "return 'spam' if 'prize' in text.lower() else 'ham'",
                            "reason": "Change the requested keyword."}]},
            ]
            with patch("simple_ar.code_task.editing.patching.LLMClient.for_task", return_value=client):
                result = propose_patch_edits(run_dir, use_llm=True, max_source_chars_per_file=180)
            self.assertEqual(result.edit_count, 1)
            self.assertEqual(client.ask_json.call_count, 3)
            followup = read_json(run_dir / "code_task/meta/edit_context_followup.json")
            self.assertEqual(followup["request"]["files"], ["spam_model.py"])
            self.assertEqual(followup["request"]["query"], "")
            self.assertTrue(all(item["path"] == "spam_model.py" for item in followup["snippets"]))
            self.assertLessEqual(len(followup["snippets"]), 2)
            self.assertTrue(all(item["source_offset"] > 0 for item in followup["snippets"]))
            self.assertIn("### spam_model.py (editable", client.ask_json.call_args_list[2].args[1])
            self.assertIn("'win'", read_text(run_dir / "code_task/workspace/spam_model.py"))

    def test_bounded_fallback_reads_two_unseen_windows_within_budget(self) -> None:
        from simple_ar.code_task.analysis.index import build_codebase_index
        from simple_ar.code_task.analysis.source_context import requested_source_context
        with _temporary_root() as root:
            source = ("# source padding\n" * 100) + "def target():\n    return 1\n"
            write_text(root / "model.py", source)
            index = build_codebase_index(root)
            initial = {"path": "model.py", "text": source[:180], "source_offset": 0}
            result = requested_source_context(
                root, index, {"files": ["model.py"]}, supplied=[initial],
                max_files=2, max_chars=180, max_total_chars=360, max_windows_per_file=2,
            )
            self.assertEqual(len(result), 2)
            self.assertEqual([item["source_offset"] for item in result], [180, 360])
            self.assertEqual(sum(len(item["text"]) for item in result), 360)
            self.assertEqual(read_text(root / "model.py"), source)

    def test_missing_context_is_read_once_without_expanding_edit_scope(self) -> None:
        with _temporary_root() as root:
            code_root = root / "project"
            _write_toy_project(code_root)
            write_text(code_root / "tests/lifecycle.py", "def end_task():\n    pass\n")
            write_text(root / "task.md", "Improve spam prediction.")
            run_dir = root / "run"
            initialize_code_task(run_dir=run_dir, code_root=code_root,
                                 task_file=root / "task.md", benchmark_command="python -m unittest")
            _approve_offline_patch_plan(run_dir)
            initial = {"edits": [], "summary": "Need lifecycle context",
                       "context_request": {"files": ["tests/lifecycle.py", "../../.env"]}}
            client = Mock()
            client.ask_json.return_value = {
                "edits": [{"path": "tests/lifecycle.py", "old": "pass", "new": "return 1"}],
                "validation": ["Algorithm still unspecified"],
            }
            with patch("simple_ar.code_task.editing.patching.LLMClient.for_task", return_value=client), patch(
                "simple_ar.code_task.editing.patching._ask_llm_for_edits", return_value=initial,
            ):
                result = propose_patch_edits(run_dir, use_llm=True, max_files=1)
            client.ask_json.assert_called_once()
            prompt = client.ask_json.call_args.args[1]
            self.assertIn("def end_task()", prompt)
            self.assertEqual(result.edit_count, 0)
            evidence = read_json(run_dir / "code_task/meta/edit_context_followup.json")
            self.assertEqual(evidence["request"]["files"], ["tests/lifecycle.py"])
            self.assertEqual(read_text(run_dir / "code_task/workspace/tests/lifecycle.py"),
                             "def end_task():\n    pass\n")
            # A valid edit can follow the same read-only request; no source is
            # changed until the separate apply boundary runs.
            client.reset_mock()
            client.ask_json.return_value = {
                "edits": [{"path": "spam_model.py", "old": "'win'", "new": "'prize'"}],
            }
            with patch("simple_ar.code_task.editing.patching.LLMClient.for_task", return_value=client), patch(
                "simple_ar.code_task.editing.patching._ask_llm_for_edits", return_value=initial,
            ):
                result = propose_patch_edits(run_dir, use_llm=True, max_files=1, force=True)
            self.assertEqual(result.edit_count, 1)
            client.ask_json.assert_called_once()
            # Another context request is retained as a blocker, not recursively
            # executed until API budget is exhausted.
            client.reset_mock()
            client.ask_json.return_value = initial
            with patch("simple_ar.code_task.editing.patching.LLMClient.for_task", return_value=client), patch(
                "simple_ar.code_task.editing.patching._ask_llm_for_edits", return_value=initial,
            ):
                result = propose_patch_edits(run_dir, use_llm=True, max_files=1, force=True)
            self.assertEqual(result.edit_count, 0)
            client.ask_json.assert_called_once()

            # A full navigation pack cannot block later evidence or act as an
            # edit authorization list. The source scope still protects tests.
            workspace = run_dir / "code_task/workspace"
            write_text(workspace / "spam_model.py", "# initial navigation\n" * 80 + read_text(workspace / "spam_model.py"))
            write_text(workspace / "integration.py", "def normalize(value):\n    return value\n")
            pack = build_code_task_context_pack(run_dir, query="spam prediction", refresh_map=True,
                preferred_paths=("spam_model.py",), max_files=1,
                max_source_chars_per_file=600, max_total_chars=600)
            original_pack = read_text(pack.snippets_path)
            self.assertEqual(read_json(pack.context_pack_path)["budget"]["used_chars"], 600)
            client.reset_mock()
            client.ask_json.return_value = {"edits": [{"path": "integration.py",
                "old": "return value", "new": "return str(value)", "reason": "Normalize the boundary."}]}
            with patch("simple_ar.code_task.editing.patching.LLMClient.for_task", return_value=client), patch(
                "simple_ar.code_task.editing.patching._ask_llm_for_edits", return_value={
                    "edits": [], "context_request": {"files": ["integration.py", "tests/lifecycle.py"]}},
            ):
                result = propose_patch_edits(run_dir, use_llm=True, max_files=2, force=True)
            self.assertEqual(result.edit_count, 1)
            client.ask_json.assert_called_once()
            prompt = client.ask_json.call_args.args[1]
            self.assertIn("def normalize(value)", prompt)
            self.assertIn("def end_task()", prompt)
            evidence = read_json(run_dir / "code_task/meta/edit_context_followup.json")
            window = evidence["prompt_window"]
            self.assertEqual(window["max_chars"], 600)
            self.assertLessEqual(window["used_chars"], 600)
            self.assertEqual({row["path"]: row["access_role"] for row in evidence["snippets"]},
                {"integration.py": "editable", "tests/lifecycle.py": "read_only"})
            self.assertIn("integration.py", result.selected_files)
            self.assertEqual(read_text(pack.snippets_path), original_pack)
            self.assertEqual(read_text(workspace / "integration.py"), "def normalize(value):\n    return value\n")
            self.assertEqual(read_text(workspace / "tests/lifecycle.py"), "def end_task():\n    pass\n")

    def test_propose_edits_restricts_llm_to_current_batch_targets(self) -> None:
        with _temporary_root() as root:
            code_root = root / "toy_project"
            task_file = root / "task.md"
            _write_toy_project(code_root)
            source = read_text(code_root / "spam_model.py")
            write_text(code_root / "spam_model.py", "# unrelated module documentation\n" * 80 + source)
            write_text(code_root / "extra.py", "VALUE = 1\n")
            write_text(task_file, "# Task\n\nImprove spam prediction only.\n")
            run_dir = root / "runs" / "code-task-run"
            initialize_code_task(
                run_dir=run_dir,
                code_root=code_root,
                task_file=task_file,
                benchmark_command="python -m unittest discover -s tests",
            )
            generate_code_task_work_plan(run_dir, use_llm=False)
            work_plan_path = run_dir / "code_task" / "work_plan.json"
            work_plan = read_json(work_plan_path)
            work_plan["items"][0]["target_files"] = ["spam_model.py"]
            work_plan["items"][0]["budget_profile"] = "normal"
            write_json(work_plan_path, work_plan)
            create_code_task_batch(run_dir, work_item_id="W1")
            _approve_offline_patch_plan(run_dir)
            located = build_code_task_context_pack(
                run_dir, query="predict", max_files=3,
                max_source_chars_per_file=500,
            )
            self.assertIn("spam_model.py", located.selected_files)
            fake_client = _FakeRepairClient(
                {
                    "summary": "Try one valid batch edit and one unrelated edit.",
                    "edits": [
                        {
                            "path": "spam_model.py",
                            "old": TOY_PREDICT_SOURCE,
                            "new": TOY_PREDICT_WITH_PRIZE,
                            "reason": "Improve the selected batch target.",
                        },
                        {
                            "path": "extra.py",
                            "old": "VALUE = 1\n",
                            "new": "VALUE = 2\n",
                            "reason": "This file is outside the current batch.",
                        },
                        {"path": "src/new_helper.py", "old": "", "new": "VALUE = 1\n",
                         "reason": "An absent file does not bypass batch scope."},
                    ],
                    "validation": ["Run unit tests."],
                    "risks": [],
                }
            )

            with patch.object(fake_client, "ask_json", wraps=fake_client.ask_json) as call, \
                 patch("simple_ar.code_task.editing.patching.LLMClient.from_env", return_value=fake_client):
                proposal = propose_patch_edits(run_dir, use_llm=True, max_source_chars_per_file=500)
            call.assert_called_once()
            self.assertIn("def predict(text):", call.call_args.args[1])
            self.assertFalse((run_dir / "code_task/meta/edit_context_followup.json").exists())

            self.assertEqual(proposal.edit_count, 1)
            data = read_json(proposal.proposal_path)
            self.assertEqual(data["editor"]["backend"], "controlled_patch")
            self.assertEqual([edit["path"] for edit in data["edits"]], ["spam_model.py"])
            self.assertIn(
                "Dropped edit outside current batch target files: extra.py",
                data["warnings"],
            )
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["patch"]["editor_backend"], "controlled_patch")
            self.assertEqual(manifest["patch"]["editor"]["backend"], "controlled_patch")
            batch_state = read_json(
                run_dir
                / "code_task"
                / "attempts"
                / "attempt-001"
                / "batches"
                / "batch-001"
                / "batch_state.json"
            )
            self.assertEqual(batch_state["state"], "proposal_ready")
            self.assertEqual(batch_state["editor"]["backend"], "controlled_patch")
            self.assertEqual(
                batch_state["artifacts"]["proposed_edits"],
                "code_task/attempts/attempt-001/batches/batch-001/proposed_edits.json",
            )
            batch_proposal = read_json(
                run_dir
                / "code_task"
                / "attempts"
                / "attempt-001"
                / "batches"
                / "batch-001"
                / "proposed_edits.json"
            )
            self.assertEqual(batch_proposal["editor"]["backend"], "controlled_patch")
            protected_test = run_dir / "code_task/workspace/tests/test_spam_model.py"
            before = protected_test.read_bytes()
            applied = apply_patch_edits(run_dir)
            self.assertEqual(list(applied.changed_files), ["spam_model.py"])
            checked = run_code_task_benchmark(run_dir, timeout_sec=10)
            self.assertEqual(checked.status, "passed")
            self.assertEqual(protected_test.read_bytes(), before)

    def test_propose_edits_budget_requires_large_approval(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nMake a deliberately large local implementation.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )
            generate_code_task_work_plan(run_dir, use_llm=False)
            create_code_task_batch(run_dir, work_item_id="W1")
            _approve_offline_patch_plan(run_dir)
            large_new = "def predict(text):\n" + "    lowered = text.lower()\n" * 260 + "    return 'spam'\n"
            fake_client = _FakeRepairClient(
                {
                    "summary": "Large but localized function replacement.",
                    "edits": [
                        {
                            "path": "spam_model.py",
                            "old": TOY_PREDICT_SOURCE,
                            "new": large_new,
                            "reason": "Large local implementation.",
                        }
                    ],
                    "validation": ["Run unit tests."],
                    "risks": ["Large edit."],
                }
            )

            with patch("simple_ar.code_task.editing.patching.LLMClient.from_env", return_value=fake_client):
                blocked = propose_patch_edits(run_dir, use_llm=True)

            blocked_data = read_json(blocked.proposal_path)
            self.assertEqual(blocked.edit_count, 0)
            self.assertEqual(blocked_data["budget"]["status"], "large_requires_approval")
            self.assertTrue(blocked_data["budget"]["requires_approval"])
            self.assertIn("Proposal exceeds the selected edit budget", blocked_data["warnings"][0])

            with patch("simple_ar.code_task.editing.patching.LLMClient.from_env", return_value=fake_client):
                approved = propose_patch_edits(
                    run_dir,
                    use_llm=True,
                    force=True,
                    allow_large_edits=True,
                )

            approved_data = read_json(approved.proposal_path)
            self.assertEqual(approved.edit_count, 1)
            self.assertEqual(approved_data["budget"]["status"], "large_approved")
            self.assertTrue(approved_data["budget"]["approved"])



    def test_repair_consumes_external_measurement_without_a_legacy_run(self) -> None:
        from simple_ar.code_task.execution.repair import RepairEvidence
        from simple_ar.research.experiment import ExperimentRequest, run_experiment
        from simple_ar.experiment.execution.backend import RunRequest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, task, run_dir = root / "project", root / "task.md", root / "run"
            _write_toy_project(project)
            (project / "spam_model.py").write_text("def predict(text):\n    return missing_name\n", encoding="utf-8")
            (project / "evaluate.py").write_text("from spam_model import predict\nprint(predict('prize'))\n", encoding="utf-8")
            task.write_text("Repair spam_model.py without changing evaluation.", encoding="utf-8")
            initialize_code_task(run_dir=run_dir, code_root=project, task_file=task,
                                 benchmark_command="python evaluate.py")
            paths = code_task_paths(run_dir)
            result = run_experiment(ExperimentRequest(RunRequest([sys.executable, "evaluate.py"], paths.workspace_dir, 5)))
            measured = result.to_dict()
            self.assertNotEqual(measured["returncode"], 0)
            evidence = RepairEvidence("attempts/experiment-1/results.json", measured, result.run.stderr)
            response = {"choices": [{"message": {"content": json.dumps({
                "summary": "Remove undefined variable", "edits": [{"path": "spam_model.py",
                "old": "return missing_name", "new": "return 'ham'", "reason": "NameError"}],
                "validation": ["Run evaluation separately"], "risks": ["Prediction quality not established"],
            })}}], "usage": {"prompt_tokens": 40, "completion_tokens": 30, "total_tokens": 70}}
            client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat"))
            with patch("simple_ar.integrations.llm._call_openai_sdk", return_value=response) as transport, patch(
                "simple_ar.code_task.execution.repair.analyze_code_task_failure",
                side_effect=AssertionError("Do not rediscover a legacy run"),
            ):
                proposal = propose_repair_edits(run_dir, llm_client=client, failure_evidence=evidence)
            self.assertEqual(proposal.edit_count, 1)
            self.assertIn("NameError", json.dumps(transport.call_args.args[1]))
            saved = read_json(proposal.repair_dir / "failure_evidence.json")
            self.assertEqual(saved["execution_report"], measured)
            self.assertEqual(saved["source"], evidence.source)
            from simple_ar.integrations.llm import LLMError
            before_failure = read_json(run_dir / "manifest.json")
            with patch.object(LLMClient, "ask_json", side_effect=LLMError("provider unavailable")):
                with self.assertRaisesRegex(LLMError, "provider unavailable"):
                    propose_repair_edits(run_dir, llm_client=client, failure_evidence=evidence)
            after_failure = read_json(run_dir / "manifest.json")
            self.assertEqual(after_failure["repair"], before_failure["repair"])
            self.assertEqual(after_failure["status"], before_failure["status"])
            self.assertEqual(len(list(paths.repairs_dir.glob("*/proposed_edits.json"))), 1)
            self.assertIn("missing_name", (paths.workspace_dir / "spam_model.py").read_text())
            self.assertFalse((paths.run_artifact_dir / "patched/execution_report.json").exists())
            with self.assertRaisesRegex(ValueError, "valid low-scoring"):
                RepairEvidence("result", {**measured, "execution_status": "passed", "metrics": {"accuracy": 0}}, "No gain")
            timed_out = RepairEvidence("timed-out-result", {**measured, "execution_status": "timed_out"}, "Time limit reached")
            self.assertEqual(timed_out.execution_report["execution_status"], "timed_out")



    def test_patch_plan_includes_baseline_and_environment_context(self) -> None:
        with _temporary_root() as root:
            code_root = root / "metric_project"
            task_file = root / "task.md"
            _write_metric_project(code_root, value="0.50")
            write_text(task_file, "# Task\n\nImprove the printed accuracy metric.\n")
            run_dir = root / "runs" / "code-task-run"
            initialize_code_task(
                run_dir=run_dir,
                code_root=code_root,
                task_file=task_file,
                benchmark_command="python benchmark.py",
            )
            probe_code_task_environment(run_dir)
            run_code_task_baseline(run_dir, timeout_sec=10)

            result = generate_patch_plan(run_dir, use_llm=False)

            self.assertEqual(result.mode, "offline")
            plan_text = read_text(run_dir / "code_task" / "patch_plan.md")
            self.assertIn("## Run Context", plan_text)
            self.assertIn("Baseline metrics", plan_text)
            self.assertIn("`accuracy`=0.5", plan_text)
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["plan"]["context"]["baseline_status"], "passed")
            self.assertEqual(manifest["plan"]["context"]["baseline_metrics"]["accuracy"], 0.5)

    def test_code_task_plan_and_decide_cli(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nImprove spam classifier accuracy.\n", init_cli=True,
            )

            plan_stdout = io.StringIO()
            with contextlib.redirect_stdout(plan_stdout):
                main(["code-task", "plan", str(run_dir), "--no-llm"])

            self.assertIn("Patch plan:", plan_stdout.getvalue())
            self.assertTrue((run_dir / "code_task" / "patch_plan.md").is_file())

            decide_stdout = io.StringIO()
            with contextlib.redirect_stdout(decide_stdout):
                main(
                    [
                        "code-task",
                        "decide-plan",
                        str(run_dir),
                        "--decision",
                        "approve",
                        "--note",
                        "Looks small enough.",
                    ]
                )

            self.assertIn("Decision: approve", decide_stdout.getvalue())
            decisions = read_jsonl(run_dir / "code_task" / "meta" / "hitl_decisions.jsonl")
            self.assertEqual(decisions[-1]["decision"], "approve")
            self.assertEqual(decisions[-1]["note"], "Looks small enough.")
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["status"], "plan_approved")
            self.assertEqual(manifest["plan"]["status"], "approved")

            status_stdout = io.StringIO()
            with contextlib.redirect_stdout(status_stdout):
                main(["status", str(run_dir)])
            self.assertIn("Plan:", status_stdout.getvalue())
            self.assertIn("status: approved", status_stdout.getvalue())

    def test_apply_edits_requires_approved_plan_and_then_patches_workspace(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nAlso detect prize as spam.\n",
                edit_scope_allowed_patterns=("spam_model.py", "src/**"),
            )
            generate_patch_plan(run_dir, use_llm=False)
            proposal_path = _write_valid_edit_proposal(run_dir)

            with self.assertRaises(PermissionError):
                apply_patch_edits(run_dir, edits_file=proposal_path)

            record_plan_decision(run_dir, decision="approve", note="Small targeted edit.")
            result = apply_patch_edits(run_dir, edits_file=proposal_path)

            workspace_model = run_dir / "code_task" / "workspace" / "spam_model.py"
            self.assertIn("'prize'", read_text(workspace_model))
            self.assertNotIn("'prize'", read_text(code_root / "spam_model.py"))
            self.assertEqual(result.changed_files, ("spam_model.py",))
            self.assertTrue((run_dir / "code_task" / "patch.diff").is_file())
            self.assertTrue((run_dir / "code_task" / "meta" / "applied_edits.json").is_file())
            self.assertFalse((run_dir / "code_task" / "meta" / "pre_patch_manifest.json").exists())
            self.assertFalse((run_dir / "code_task" / "meta" / "post_patch_manifest.json").exists())
            applied = read_json(run_dir / "code_task" / "meta" / "applied_edits.json")
            self.assertEqual(applied["changed_files"], ["spam_model.py"])
            self.assertEqual(applied["editor"]["backend"], "controlled_patch")
            self.assertEqual(applied["editor"]["source"], "legacy_or_manual_proposal")
            self.assertTrue(applied["edits"][0]["old_sha256"])
            self.assertTrue(applied["edits"][0]["new_sha256"])
            self.assertEqual(applied["snapshot"]["captured_count"], 1)
            snapshot_path = Path(applied["snapshot"]["manifest"])
            self.assertTrue(snapshot_path.is_file())
            snapshot = read_json(snapshot_path)
            self.assertEqual(snapshot["files"][0]["path"], "spam_model.py")
            self.assertEqual(snapshot["files"][0]["kind"], "file")
            diff_text = read_text(run_dir / "code_task" / "patch.diff")
            self.assertIn("+    lowered = text.lower()", diff_text)
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["status"], "patched")
            self.assertEqual(manifest["patch"]["status"], "applied")
            self.assertEqual(manifest["patch"]["editor_backend"], "controlled_patch")
            self.assertEqual(manifest["patch"]["editor"]["backend"], "controlled_patch")
            self.assertNotIn("pre_patch_manifest", manifest["patch"])
            self.assertNotIn("post_patch_manifest", manifest["patch"])

            # Creation uses the same approval, scope, snapshot and index path,
            # rather than requiring generated programs to write their own modules.
            fake = _FakeRepairClient({"edits": [
                {"path": "src/helpers.py", "old": "", "new": "VALUE = 1\n", "reason": "Add a helper."},
                {"path": "tests/new_test.py", "old": "", "new": "pass\n", "reason": "Forbidden."},
                {"path": "../outside.py", "old": "", "new": "pass\n", "reason": "Forbidden."},
            ]})
            with patch("simple_ar.code_task.editing.patching.LLMClient.from_env", return_value=fake):
                proposed = propose_patch_edits(run_dir, use_llm=True, force=True)
            self.assertEqual(proposed.edit_count, 1)
            proposal_path = proposed.proposal_path
            created = apply_patch_edits(run_dir, edits_file=proposal_path)
            self.assertEqual(created.changed_files, ("src/helpers.py",))
            helper = run_dir / "code_task/workspace/src/helpers.py"
            self.assertEqual(read_text(helper), "VALUE = 1\n")
            applied = read_json(run_dir / "code_task/meta/applied_edits.json")
            self.assertIsNone(applied["edits"][0]["old_sha256"])
            captured = read_json(Path(applied["snapshot"]["manifest"]))
            self.assertFalse(captured["files"][0]["existed"])
            with self.assertRaises(PatchValidationError):
                apply_patch_edits(run_dir, edits_file=proposal_path)
            self.assertEqual(read_text(helper), "VALUE = 1\n")

    def test_apply_large_edits_records_apply_time_approval(self) -> None:
        from simple_ar.code_task.editing.patching import EditBudgetApprovalRequired
        from simple_ar.code_task.orchestration.execute import implement_code_task
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nApply a reviewed large proposal.\n",
            )
            _approve_offline_patch_plan(run_dir)
            proposal_path = _write_valid_edit_proposal(run_dir)
            proposal = read_json(proposal_path)
            proposal["budget"] = {
                "status": "accepted",
                "profile": "large",
                "requires_approval": True,
                "approved": False,
            }
            write_json(proposal_path, proposal)

            with self.assertRaises(EditBudgetApprovalRequired):
                apply_patch_edits(run_dir, edits_file=proposal_path)

            pending = implement_code_task(run_dir, approval_note="Review this isolated patch", use_llm=False)
            self.assertEqual(pending.stop_reason, "large_edit_approval_required")
            self.assertFalse((run_dir / "code_task" / "meta" / "applied_edits.json").exists())

            apply_patch_edits(run_dir, edits_file=proposal_path, allow_large_edits=True)

            applied = read_json(run_dir / "code_task" / "meta" / "applied_edits.json")
            self.assertTrue(applied["budget"]["approved"])
            self.assertEqual(applied["budget"]["approval_source"], "apply_edits_allow_large_edits")
            manifest = read_json(run_dir / "manifest.json")
            self.assertTrue(manifest["patch"]["budget"]["approved"])
            self.assertEqual(
                manifest["patch"]["budget"]["approval_source"],
                "apply_edits_allow_large_edits",
            )

    def test_apply_repair_proposal_records_latest_applied_proposal(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nApply a reviewed repair proposal.\n",
            )
            _approve_offline_patch_plan(run_dir)
            repair_dir = run_dir / "code_task" / "repairs" / "repair-001"
            repair_dir.mkdir(parents=True)
            repair_proposal = _write_valid_edit_proposal(run_dir, path=repair_dir / "proposed_edits.json")
            manifest = read_json(run_dir / "manifest.json")
            manifest["repair"] = {
                "status": "repair_proposed",
                "repair_count": 1,
                "latest_proposed_edits": "code_task/repairs/repair-001/proposed_edits.json",
            }
            write_json(run_dir / "manifest.json", manifest)

            apply_patch_edits(run_dir, edits_file=repair_proposal)

            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["patch"]["latest_applied_proposal"], "code_task/repairs/repair-001/proposed_edits.json")
            self.assertEqual(manifest["repair"]["status"], "repair_applied")
            self.assertEqual(
                manifest["repair"]["latest_applied_proposal"],
                "code_task/repairs/repair-001/proposed_edits.json",
            )
            applied = read_json(run_dir / "code_task" / "meta" / "applied_edits.json")
            self.assertEqual(applied["proposal"], "code_task/repairs/repair-001/proposed_edits.json")

    def test_apply_edits_allows_multiple_ordered_edits_in_one_file(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nApply two edits in the same file.\n",
            )
            _approve_offline_patch_plan(run_dir)
            proposal_path = run_dir / "code_task" / "meta" / "proposed_edits.json"
            write_json(
                proposal_path,
                {
                    "edits": [
                        {
                            "path": "spam_model.py",
                            "old": "import math\n\n\n",
                            "new": "import math\n\nSPAM_KEYWORDS = ('win', 'prize')\n\n",
                            "reason": "Add shared keyword configuration.",
                        },
                        {
                            "path": "spam_model.py",
                            "old": TOY_PREDICT_SOURCE,
                            "new": (
                                "def predict(text):\n"
                                "    lowered = text.lower()\n"
                                "    return 'spam' if any(keyword in lowered for keyword in SPAM_KEYWORDS) else 'ham'\n"
                            ),
                            "reason": "Use the shared keyword configuration.",
                        },
                    ]
                },
            )

            result = apply_patch_edits(run_dir, edits_file=proposal_path)

            self.assertEqual(result.changed_files, ("spam_model.py",))
            text = read_text(run_dir / "code_task" / "workspace" / "spam_model.py")
            self.assertIn("SPAM_KEYWORDS", text)
            self.assertIn("any(keyword in lowered", text)
            applied = read_json(run_dir / "code_task" / "meta" / "applied_edits.json")
            self.assertEqual(applied["edit_count"], 2)
            self.assertEqual(applied["changed_files"], ["spam_model.py"])
            diff_text = read_text(run_dir / "code_task" / "patch.diff")
            self.assertEqual(diff_text.count("--- a/spam_model.py"), 1)

    def test_apply_edits_rejects_path_traversal_without_modifying_workspace(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nTry an unsafe edit.\n",
            )
            _approve_offline_patch_plan(run_dir)
            proposal_path = root / "bad_edits.json"
            write_json(
                proposal_path,
                {
                    "edits": [
                        {
                            "path": "../outside.py",
                            "old": "x",
                            "new": "y",
                            "reason": "unsafe",
                        }
                    ]
                },
            )
            before = read_text(run_dir / "code_task" / "workspace" / "spam_model.py")

            with self.assertRaises(PatchValidationError):
                apply_patch_edits(run_dir, edits_file=proposal_path)

            after = read_text(run_dir / "code_task" / "workspace" / "spam_model.py")
            self.assertEqual(before, after)
            self.assertFalse((run_dir / "code_task" / "patch.diff").exists())

    def test_apply_edits_rejects_protected_test_and_benchmark_files(self) -> None:
        with _temporary_root() as root:
            code_root = root / "toy_project"
            task_file = root / "task.md"
            _write_toy_project(code_root)
            write_text(code_root / "benchmark.py", "print('accuracy: 0.5')\n")
            write_text(task_file, "# Task\n\nImprove source behavior without changing validation targets.\n")
            run_dir = root / "runs" / "code-task-run"
            initialize_code_task(run_dir=run_dir, code_root=code_root, task_file=task_file)
            _approve_offline_patch_plan(run_dir)
            proposal_path = root / "protected_edits.json"
            write_json(
                proposal_path,
                {
                    "edits": [
                        {
                            "path": "tests/test_spam_model.py",
                            "old": "        self.assertEqual(predict('win now'), 'spam')\n",
                            "new": "        self.assertEqual(predict('win now'), 'ham')\n",
                            "reason": "Should be blocked because tests are read-only evidence.",
                        },
                        {
                            "path": "benchmark.py",
                            "old": "print('accuracy: 0.5')\n",
                            "new": "print('accuracy: 1.0')\n",
                            "reason": "Should be blocked because benchmarks are read-only evidence.",
                        },
                    ]
                },
            )

            before_test = read_text(run_dir / "code_task" / "workspace" / "tests" / "test_spam_model.py")
            before_benchmark = read_text(run_dir / "code_task" / "workspace" / "benchmark.py")
            with self.assertRaises(PatchValidationError) as caught:
                apply_patch_edits(run_dir, edits_file=proposal_path)

            self.assertIn("path is protected by the edit scope", str(caught.exception))
            self.assertEqual(
                before_test,
                read_text(run_dir / "code_task" / "workspace" / "tests" / "test_spam_model.py"),
            )
            self.assertEqual(
                before_benchmark,
                read_text(run_dir / "code_task" / "workspace" / "benchmark.py"),
            )
            self.assertFalse((run_dir / "code_task" / "patch.diff").exists())

    def test_propose_edits_drops_protected_llm_paths(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nImprove the spam model without changing tests.\n",
            )
            _approve_offline_patch_plan(run_dir)

            fake_client = _FakeRepairClient(
                {
                    "summary": "Attempt one valid edit and one protected edit.",
                    "edits": [
                        {
                            "path": "spam_model.py",
                            "old": TOY_PREDICT_SOURCE,
                            "new": (
                                "def predict(text):\n"
                                "    lowered = text.lower()\n"
                                "    return 'spam' if 'win' in lowered or 'prize' in lowered else 'ham'\n"
                            ),
                            "reason": "Improve source behavior.",
                        },
                        {
                            "path": "tests/test_spam_model.py",
                            "old": "        self.assertEqual(predict('win now'), 'spam')\n",
                            "new": "        self.assertEqual(predict('win now'), 'ham')\n",
                            "reason": "This protected edit should be dropped.",
                        },
                    ],
                    "validation": ["Run tests."],
                    "risks": ["Changing tests would invalidate evidence."],
                }
            )

            with patch("simple_ar.code_task.editing.patching.LLMClient.from_env", return_value=fake_client):
                result = propose_patch_edits(run_dir, use_llm=True)

            proposal = read_json(result.proposal_path)
            self.assertEqual(result.edit_count, 1)
            self.assertEqual([item["path"] for item in proposal["edits"]], ["spam_model.py"])
            self.assertIn(
                "Dropped edit for protected read-only path: tests/test_spam_model.py",
                proposal["warnings"],
            )

    def test_code_task_propose_and_apply_cli_with_manual_edits_file(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nDetect prize messages as spam.\n", init_cli=True,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                main(["code-task", "plan", str(run_dir), "--no-llm"])
                main(["code-task", "decide-plan", str(run_dir), "--decision", "approve"])

            propose_stdout = io.StringIO()
            with contextlib.redirect_stdout(propose_stdout):
                main(["code-task", "propose-edits", str(run_dir), "--no-llm"])
            self.assertIn("Edit count: 0", propose_stdout.getvalue())
            self.assertTrue((run_dir / "code_task" / "meta" / "proposed_edits.json").is_file())

            edits_file = _write_valid_edit_proposal(run_dir, path=root / "manual_edits.json")
            apply_stdout = io.StringIO()
            with contextlib.redirect_stdout(apply_stdout):
                main(["code-task", "apply-edits", str(run_dir), "--edits-file", str(edits_file)])

            self.assertIn("Changed files: 1", apply_stdout.getvalue())
            status_stdout = io.StringIO()
            with contextlib.redirect_stdout(status_stdout):
                main(["status", str(run_dir)])
            self.assertIn("Patch:", status_stdout.getvalue())
            self.assertIn("status: applied", status_stdout.getvalue())

    def test_code_task_apply_cli_reports_validation_errors_without_traceback(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nDetect prize messages as spam.\n", init_cli=True,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                main(["code-task", "plan", str(run_dir), "--no-llm"])
                main(["code-task", "decide-plan", str(run_dir), "--decision", "approve"])

            edits_file = root / "bad_edits.json"
            write_json(
                edits_file,
                {
                    "schema_version": 1,
                    "edits": [
                        {
                            "path": "spam_model.py",
                            "old": "def missing():\n    pass\n",
                            "new": "def missing():\n    return None\n",
                            "reason": "This cannot match the workspace.",
                        }
                    ],
                },
            )

            apply_stdout = io.StringIO()
            with contextlib.redirect_stdout(apply_stdout):
                with self.assertRaises(SystemExit) as caught:
                    main(["code-task", "apply-edits", str(run_dir), "--edits-file", str(edits_file)])

            self.assertEqual(caught.exception.code, 1)
            output = apply_stdout.getvalue()
            self.assertIn("Patch validation failed; no workspace files were changed.", output)
            self.assertIn("old text was not found", output)

    def test_validate_code_task_reports_warnings_and_strict_errors(self) -> None:
        with _temporary_root() as root:
            code_root = root / "toy_project"
            task_file = root / "task.md"
            _write_toy_project(code_root)
            write_text(
                code_root / "danger.py",
                "import os\n\n\ndef run():\n    os.system('echo unsafe')\n",
            )
            write_text(task_file, "# Task\n\nValidate risky code.\n")
            run_dir = root / "runs" / "code-task-run"
            initialize_code_task(run_dir=run_dir, code_root=code_root, task_file=task_file)

            result = validate_code_task(run_dir)

            self.assertEqual(result.status, "passed")
            self.assertEqual(result.error_count, 0)
            self.assertGreaterEqual(result.warning_count, 1)
            report = read_json(run_dir / "code_task" / "meta" / "validation_report.json")
            self.assertTrue(any(item["code"] == "risky_call" for item in report["issues"]))

            strict = validate_code_task(run_dir, strict=True)
            self.assertEqual(strict.status, "failed")
            self.assertGreaterEqual(strict.error_count, 1)

    def test_validation_uses_external_dependency_probe_without_importing_project(self):
        from simple_ar.code_task.execution.validation import _external_import_availability
        self.assertEqual(_external_import_availability(sys.executable, {"json", "simple_ar_no_such_package"}),
                         {"json": True, "simple_ar_no_such_package": False})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            write_text(project / "main.py", "import fictional_training_package\n")
            write_text(root / "task.md", "Inspect code, do not execute it.")
            run = root / "run"
            initialize_code_task(run_dir=run, code_root=project, task_file=root / "task.md",
                                 env_mode="external", python_executable=sys.executable)
            with patch("simple_ar.code_task.execution.validation._external_import_availability",
                       return_value={"fictional_training_package": True}) as probe:
                validate_code_task(run)
            probe.assert_called_once()
            report = read_json(run / "code_task/meta/validation_report.json")
            self.assertFalse(any(row["code"] == "missing_import" for row in report["issues"]))
            self.assertIn("static", report["validation_scope"])
            with patch("simple_ar.code_task.execution.validation._external_import_availability", return_value=None):
                validate_code_task(run)
            report = read_json(run / "code_task/meta/validation_report.json")
            self.assertEqual([row["code"] for row in report["issues"]], ["dependency_check_unavailable"])

    def test_run_code_task_benchmark_captures_outputs_and_updates_status(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nRun existing tests.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )

            result = run_code_task_benchmark(run_dir, timeout_sec=10)

            self.assertEqual(result.label, "patched")
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.returncode, 0)
            self.assertTrue((run_dir / "code_task" / "run" / "patched" / "execution_report.json").is_file())
            self.assertTrue((run_dir / "code_task" / "run" / "patched" / "stdout.txt").is_file())
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["benchmark"]["last_status"], "passed")
            self.assertEqual(manifest["benchmark"]["latest_label"], "patched")
            self.assertEqual(manifest["benchmark"]["runs"]["patched"]["status"], "passed")
            report = read_json(run_dir / "code_task" / "run" / "patched" / "execution_report.json")
            self.assertEqual(report["environment"]["mode"], "current")
            self.assertEqual(report["command"][0], sys.executable)
            self.assertEqual(manifest["status"], "benchmark_passed")
            attempt_1 = run_dir / "code_task" / "run" / "patched" / "attempts" / "attempt-001"
            self.assertTrue((attempt_1 / "execution_report.json").is_file())
            self.assertTrue((attempt_1 / "stdout.txt").is_file())
            self.assertTrue((attempt_1 / "stderr.txt").is_file())
            self.assertEqual(report["history_attempt"], "attempt-001")

            second = run_code_task_benchmark(run_dir, timeout_sec=10)

            self.assertEqual(second.status, "passed")
            attempt_2 = run_dir / "code_task" / "run" / "patched" / "attempts" / "attempt-002"
            self.assertTrue((attempt_2 / "execution_report.json").is_file())
            self.assertTrue((attempt_2 / "stdout.txt").is_file())
            manifest = read_json(run_dir / "manifest.json")
            run_record = manifest["benchmark"]["runs"]["patched"]
            self.assertEqual(run_record["latest_attempt"], "attempt-002")
            self.assertEqual(run_record["attempt_count"], 2)
            self.assertEqual(len(run_record["attempts"]), 2)
            latest_report = read_json(run_dir / "code_task" / "run" / "patched" / "execution_report.json")
            self.assertEqual(latest_report["history_attempt"], "attempt-002")

            workspace = run_dir / "code_task" / "workspace"
            write_text(workspace / "output_probe.py",
                "import os, sys\nfrom pathlib import Path\n"
                "root = Path(os.environ['SIMPLE_AR_OUTPUT_DIR'])\n"
                "target = Path(sys.argv[1])\n"
                "assert target.parent == root\n"
                "assert '{output_dir}' not in str(target)\n"
                "target.write_text('observed')\nprint(target)\n")
            output_check = run_code_task_benchmark(run_dir,
                command='python output_probe.py "{output_dir}/shortcheck.txt"',
                timeout_sec=10, skip_validation=True)
            self.assertEqual(output_check.status, "passed")
            owned_file = Path(output_check.stdout_path.read_text().strip())
            self.assertTrue(owned_file.is_relative_to(run_dir / "code_task" / "run" / "patched" / "process"))
            self.assertEqual(owned_file.read_text(), "observed")
            self.assertFalse((workspace / "{output_dir}").exists())

    def test_run_code_task_benchmark_stops_warning_flood_with_watchdog(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nStop noisy runaway benchmark output.\n",
                benchmark_command="python warning_flood.py",
            )
            write_text(
                run_dir / "code_task" / "workspace" / "warning_flood.py",
                (
                    "import sys, time\n"
                    "for _ in range(10000):\n"
                    "    print('ConvergenceWarning: STOP: TOTAL NO. OF ITERATIONS REACHED LIMIT', file=sys.stderr, flush=True)\n"
                    "    time.sleep(0.001)\n"
                ),
            )

            result = run_code_task_benchmark(run_dir, timeout_sec=30, skip_validation=True)

            self.assertEqual(result.status, "failed")
            report = read_json(result.report_path)
            self.assertEqual(report["runtime_watchdog"]["reason"], "warning_flood")
            self.assertIn("Runtime output watchdog", read_text(result.stderr_path))

    def test_run_code_task_baseline_records_pre_patch_result(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nCapture baseline.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )

            result = run_code_task_baseline(run_dir, timeout_sec=10)

            self.assertEqual(result.label, "baseline")
            self.assertEqual(result.status, "passed")
            self.assertTrue((run_dir / "code_task" / "run" / "baseline" / "execution_report.json").is_file())
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["status"], "baseline_passed")
            self.assertEqual(manifest["benchmark"]["latest_label"], "baseline")
            self.assertEqual(manifest["benchmark"]["runs"]["baseline"]["status"], "passed")
            summary = read_text(run_dir / "code_task" / "summary.md")
            self.assertIn("### Baseline", summary)
            self.assertIn("Environment mode: `current`", summary)

    def test_patched_run_writes_comparison_when_baseline_exists(self) -> None:
        with _temporary_root() as root:
            code_root = root / "metric_project"
            task_file = root / "task.md"
            _write_metric_project(code_root, value="0.50")
            write_text(task_file, "# Task\n\nImprove the printed accuracy metric.\n")
            run_dir = root / "runs" / "code-task-run"
            initialize_code_task(
                run_dir=run_dir,
                code_root=code_root,
                task_file=task_file,
                benchmark_command="python benchmark.py",
            )

            baseline = run_code_task_baseline(run_dir, timeout_sec=10)
            self.assertEqual(baseline.metrics["accuracy"], 0.5)
            write_text(run_dir / "code_task" / "workspace" / "metric_value.txt", "0.80\n")
            patched = run_code_task_benchmark(run_dir, timeout_sec=10)

            self.assertEqual(patched.metrics["accuracy"], 0.8)
            comparison_path = run_dir / "code_task" / "run" / "comparison.json"
            self.assertTrue(comparison_path.is_file())
            comparison = read_json(comparison_path)
            self.assertEqual(comparison["verdict"], "improved")
            self.assertAlmostEqual(comparison["deltas"]["accuracy"], 0.3)
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["benchmark"]["comparison"]["verdict"], "improved")
            self.assertEqual(manifest["layout"]["comparison"], "code_task/run/comparison.json")
            summary = read_text(run_dir / "code_task" / "summary.md")
            self.assertIn("## Result", summary)
            self.assertIn("Outcome: `improved`", summary)
            self.assertIn("Next step:", summary)
            self.assertIn("### Comparison", summary)
            self.assertIn("Verdict: `improved`", summary)
            self.assertIn("+0.3", summary)

    def test_patched_regression_sets_objective_status(self) -> None:
        with _temporary_root() as root:
            code_root = root / "metric_project"
            task_file = root / "task.md"
            _write_metric_project(code_root, value="0.80")
            write_text(task_file, "# Task\n\nImprove the printed accuracy metric.\n")
            run_dir = root / "runs" / "code-task-run"
            initialize_code_task(
                run_dir=run_dir,
                code_root=code_root,
                task_file=task_file,
                benchmark_command="python benchmark.py",
            )
            run_code_task_baseline(run_dir, timeout_sec=10)
            manifest = read_json(run_dir / "manifest.json")
            manifest["failure_analysis"] = {
                "status": "needs_repair",
                "analysis": "code_task/run/patched/failure_analysis.md",
            }
            manifest["repair"] = {
                "status": "repair_applied",
                "repair_count": 1,
                "latest_proposed_edits": "code_task/repairs/repair-001/proposed_edits.json",
            }
            write_json(run_dir / "manifest.json", manifest)
            failure_path = run_dir / "code_task" / "run" / "patched" / "failure_analysis.md"
            write_text(failure_path, "# Failure Analysis\n\nOld failure.\n")
            write_text(run_dir / "code_task" / "workspace" / "metric_value.txt", "0.50\n")

            run_code_task_benchmark(run_dir, timeout_sec=10)

            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["status"], "objective_regressed")
            self.assertEqual(manifest["objective"]["status"], "regressed")
            self.assertEqual(manifest["failure_analysis"]["status"], "resolved")
            self.assertEqual(manifest["repair"]["status"], "benchmark_passed")
            summary = read_text(run_dir / "code_task" / "summary.md")
            self.assertIn("Outcome: `regressed`", summary)
            self.assertIn("Patched status: `passed`", summary)
            self.assertIn("Verdict: `regressed`", summary)
            self.assertNotIn("Blocker:", summary)
            self.assertNotIn("Evidence-chain gap:", summary)
            self.assertIn("Attempted repairs: existing-project=1", summary)
            self.assertNotIn("## Failure Analysis", summary)
            self.assertNotIn("## Repair", summary)

    def test_manual_validate_and_run_sync_latest_batch_state(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nDetect prize messages as spam.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )
            generate_code_task_work_plan(run_dir, use_llm=False)
            create_code_task_batch(run_dir, work_item_id="W1")
            _approve_offline_patch_plan(run_dir)
            apply_patch_edits(run_dir, edits_file=_write_valid_edit_proposal(run_dir))

            validate_code_task(run_dir)
            batch_path = (
                run_dir
                / "code_task"
                / "attempts"
                / "attempt-001"
                / "batches"
                / "batch-001"
                / "batch_state.json"
            )
            self.assertEqual(read_json(batch_path)["state"], "validating")
            run_code_task_benchmark(run_dir, timeout_sec=10)

            self.assertEqual(read_json(batch_path)["state"], "completed")
            attempt = read_json(run_dir / "code_task" / "attempts" / "attempt-001" / "attempt_state.json")
            self.assertEqual(attempt["state"], "completed")
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["work_plan"]["status"], "completed")

    def test_comparison_uses_configured_direction_for_custom_metric(self) -> None:
        with _temporary_root() as root:
            code_root = root / "metric_project"
            task_file = root / "task.md"
            _write_metric_project(
                code_root,
                value="10.0",
                metric_name="custom_reward",
            )
            write_text(task_file, "# Task\n\nImprove the custom reward metric.\n")
            run_dir = root / "runs" / "code-task-run"
            initialize_code_task(
                run_dir=run_dir,
                code_root=code_root,
                task_file=task_file,
                benchmark_command="python benchmark.py",
                primary_metric="custom_reward",
                metric_directions={"custom_reward": "higher"},
            )

            baseline = run_code_task_baseline(run_dir, timeout_sec=10)
            self.assertEqual(baseline.metrics["custom_reward"], 10.0)
            write_text(
                run_dir / "code_task" / "workspace" / "metric_value.txt",
                "12.5\n",
            )
            run_code_task_benchmark(run_dir, timeout_sec=10)

            comparison = read_json(run_dir / "code_task" / "run" / "comparison.json")
            self.assertEqual(comparison["verdict"], "improved")
            self.assertEqual(comparison["metric_config"]["primary_metric"], "custom_reward")
            row = comparison["metrics"][0]
            self.assertEqual(row["name"], "custom_reward")
            self.assertEqual(row["direction"], "higher_is_better")
            self.assertEqual(row["direction_source"], "configured")
            self.assertEqual(row["interpretation"], "improved")
            self.assertEqual(row["is_primary"], True)
            summary = read_text(run_dir / "code_task" / "summary.md")
            self.assertIn("Primary metric: `custom_reward` (higher_is_better)", summary)
            self.assertIn("Outcome: `improved`", summary)

            status_stdout = io.StringIO()
            with contextlib.redirect_stdout(status_stdout):
                main(["status", str(run_dir)])
            status = status_stdout.getvalue()
            self.assertIn("- summary:", status)
            self.assertIn("- primary metric: custom_reward", status)
            self.assertIn("- comparison: improved", status)
            self.assertIn("custom_reward=+2.5", status)

    def test_unknown_metric_is_recorded_but_not_overinterpreted(self) -> None:
        with _temporary_root() as root:
            code_root = root / "metric_project"
            task_file = root / "task.md"
            _write_metric_project(code_root, value="10.0", metric_name="custom_reward")
            write_text(task_file, "# Task\n\nImprove an unknown custom metric.\n")
            run_dir = root / "runs" / "code-task-run"
            initialize_code_task(
                run_dir=run_dir,
                code_root=code_root,
                task_file=task_file,
                benchmark_command="python benchmark.py",
            )

            run_code_task_baseline(run_dir, timeout_sec=10)
            write_text(
                run_dir / "code_task" / "workspace" / "metric_value.txt",
                "12.5\n",
            )
            run_code_task_benchmark(run_dir, timeout_sec=10)

            comparison = read_json(run_dir / "code_task" / "run" / "comparison.json")
            self.assertEqual(comparison["verdict"], "inconclusive")
            self.assertEqual(comparison["deltas"]["custom_reward"], 2.5)
            self.assertEqual(comparison["metrics"][0]["direction"], "unknown")
            self.assertEqual(comparison["metrics"][0]["interpretation"], "changed")

    def test_code_task_init_cli_records_metric_config(self) -> None:
        with _temporary_root() as root:
            code_root = root / "metric_project"
            task_file = root / "task.md"
            output_root = root / "runs"
            _write_metric_project(code_root, value="0.50", metric_name="macro_f1")
            write_text(task_file, "# Task\n\nImprove macro F1.\n")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main(
                    [
                        "code-task",
                        "init",
                        "--code-root",
                        str(code_root),
                        "--task-file",
                        str(task_file),
                        "--output-root",
                        str(output_root),
                        "--benchmark-command",
                        "python benchmark.py",
                        "--primary-metric",
                        "macro_f1",
                        "--metric-direction",
                        "macro_f1=higher",
                        "--metric-direction",
                        "inference_time_ms=resource",
                    ]
                )

            run_dir = next(output_root.iterdir())
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["benchmark"]["primary_metric"], "macro_f1")
            self.assertEqual(
                manifest["benchmark"]["metric_directions"]["macro_f1"],
                "higher_is_better",
            )
            self.assertEqual(
                manifest["benchmark"]["metric_directions"]["inference_time_ms"],
                "resource",
            )
            output = stdout.getvalue()
            self.assertIn("Primary metric:", output)
            self.assertIn("macro_f1", output)
            self.assertIn("Metric directions:", output)

    def test_code_task_init_cli_reads_toml_config(self) -> None:
        with _temporary_root() as root:
            code_root = root / "metric_project"
            task_file = root / "task.md"
            output_root = root / "configured_runs"
            config_file = root / "code_task.toml"
            _write_metric_project(code_root, value="10.0", metric_name="custom_reward")
            write_text(task_file, "# Task\n\nImprove configured reward.\n")
            write_text(
                config_file,
                (
                    "[code_task]\n"
                    f'code_root = "{code_root.as_posix()}"\n'
                    f'task_file = "{task_file.as_posix()}"\n'
                    f'output_root = "{output_root.as_posix()}"\n'
                    'name = "configured-metric-task"\n'
                    "\n"
                    "[benchmark]\n"
                    'command = "python benchmark.py"\n'
                    'primary_metric = "custom_reward"\n'
                    "\n"
                    "[benchmark.metric_directions]\n"
                    'custom_reward = "higher"\n'
                    'latency_ms = "resource"\n'
                    "\n"
                    "[environment]\n"
                    'mode = "current"\n'
                    "\n"
                    "[safety]\n"
                    "max_file_bytes = 10000\n"
                ),
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main(["code-task", "init", "--config", str(config_file)])

            run_dir = next(output_root.iterdir())
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["benchmark"]["command"], "python benchmark.py")
            self.assertEqual(manifest["benchmark"]["primary_metric"], "custom_reward")
            self.assertEqual(
                manifest["benchmark"]["metric_directions"]["custom_reward"],
                "higher_is_better",
            )
            self.assertEqual(
                manifest["benchmark"]["metric_directions"]["latency_ms"],
                "resource",
            )
            self.assertEqual(manifest["copy"]["max_file_bytes"], 10000)
            self.assertIn("Config:", stdout.getvalue())

    def test_code_task_config_rejects_wrong_section_types(self) -> None:
        with _temporary_root() as root:
            config_file = root / "code_task.toml"
            write_text(
                config_file,
                (
                    "[code_task]\n"
                    f'code_root = "{root.as_posix()}"\n'
                    "task_file = [\"not\", \"a\", \"path\"]\n"
                ),
            )

            with self.assertRaises(CodeTaskConfigError) as raised:
                load_code_task_init_options(config_path=str(config_file))

            self.assertIn("Invalid code-task config", str(raised.exception))
            self.assertIn("task_file", str(raised.exception))

    def test_external_env_mode_records_python_policy_and_uses_it(self) -> None:
        with _temporary_root() as root:
            code_root = root / "toy_project"
            task_file = root / "task.md"
            _write_toy_project(code_root)
            write_text(task_file, "# Task\n\nRun with an explicit interpreter.\n")
            run_dir = root / "runs" / "code-task-run"
            # Preserve the selected venv entrypoint: resolving symlinks would
            # incorrectly expect its base interpreter instead.
            expected_python = sys.executable
            initialize_code_task(
                run_dir=run_dir,
                code_root=code_root,
                task_file=task_file,
                benchmark_command="python -m unittest discover -s tests",
                env_mode="external",
                python_executable=sys.executable,
            )

            result = probe_code_task_environment(run_dir)
            self.assertIn(result.status, {"ok", "warning"})
            controller_packages = root / "controller_packages"
            write_text(controller_packages / "unittest.py", "raise RuntimeError('controller import leaked')\n")
            with patch.dict("os.environ", {"PYTHONPATH": str(controller_packages)}):
                baseline = run_code_task_baseline(run_dir, timeout_sec=10)

            report = read_json(baseline.report_path)
            self.assertEqual(report["returncode"], 0)
            self.assertEqual(report["environment"]["mode"], "external")
            self.assertEqual(report["environment"]["python_executable"], expected_python)
            self.assertEqual(report["command"][0], expected_python)
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["environment"]["policy"]["mode"], "external")
            self.assertEqual(
                manifest["benchmark"]["runs"]["baseline"]["environment"]["mode"],
                "external",
            )

    def test_code_task_cli_can_override_env_mode_for_baseline(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nRun CLI baseline with explicit env.\n", init_cli=True,
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main(
                    [
                        "code-task",
                        "baseline",
                        str(run_dir),
                        "--timeout",
                        "10",
                        "--env-mode",
                        "external",
                        "--python",
                        sys.executable,
                    ]
                )

            self.assertIn("Status: passed", stdout.getvalue())
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["environment"]["policy"]["mode"], "external")

    def test_analyze_failure_and_offline_repair_proposal(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nBreak then diagnose the spam classifier.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )
            _approve_offline_patch_plan(run_dir)
            apply_patch_edits(run_dir, edits_file=_write_failing_edit_proposal(run_dir))
            failed = run_code_task_benchmark(run_dir, timeout_sec=10)
            self.assertEqual(failed.status, "failed")

            analysis = analyze_code_task_failure(run_dir)

            self.assertEqual(analysis.status, "needs_repair")
            self.assertEqual(analysis.source, "benchmark")
            analysis_text = read_text(analysis.analysis_path)
            self.assertIn("# Failure Analysis", analysis_text)
            self.assertIn("AssertionError", analysis_text)
            history_dir = run_dir / "code_task" / "run" / "patched" / "attempts" / "attempt-001"
            self.assertTrue((history_dir / "failure_analysis.md").is_file())
            self.assertTrue((history_dir / "failure_graph.json").is_file())
            manifest = read_json(run_dir / "manifest.json")
            latest_attempt = manifest["benchmark"]["runs"]["patched"]["attempts"][0]
            self.assertEqual(
                latest_attempt["failure_analysis"],
                "code_task/run/patched/attempts/attempt-001/failure_analysis.md",
            )

            repair = propose_repair_edits(run_dir, use_llm=False)
            self.assertEqual(repair.mode, "offline")
            self.assertEqual(repair.edit_count, 0)
            self.assertTrue((repair.repair_dir / "proposed_edits.json").is_file())
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["repair"]["status"], "repair_proposed")
            self.assertIn("## Repair", read_text(run_dir / "code_task" / "summary.md"))

    def test_failure_analysis_prefers_runtime_stderr_over_validation_warning(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nDiagnose runtime stderr before validation warnings.\n",
                benchmark_command="python broken_runtime.py",
            )
            write_text(
                run_dir / "code_task" / "workspace" / "broken_runtime.py",
                (
                    "import sys\n"
                    "print(\"Experiment failed: 'str' object has no attribute 'X'\", file=sys.stderr)\n"
                    "raise SystemExit(1)\n"
                ),
            )
            failed = run_code_task_benchmark(run_dir, timeout_sec=10, skip_validation=True)
            self.assertEqual(failed.status, "failed")

            analysis = analyze_code_task_failure(run_dir)

            self.assertEqual(analysis.status, "needs_repair")
            analysis_text = read_text(analysis.analysis_path)
            self.assertIn("Experiment failed: 'str' object has no attribute 'X'", analysis_text)
            self.assertNotIn("strongest error signal is: `warning", analysis_text)

    def test_greenfield_run_repair_analyzes_failure_when_budget_exhausted(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nDiagnose final failure even when repair budget is gone.\n",
                benchmark_command="python broken_runtime.py",
            )
            write_text(
                run_dir / "code_task" / "workspace" / "broken_runtime.py",
                "print(\"ERROR: final budget exhausted signal\")\nraise SystemExit(1)\n",
            )
            failed = run_code_task_benchmark(run_dir, timeout_sec=10, skip_validation=True)
            self.assertEqual(failed.status, "failed")

            repaired = _attempt_greenfield_run_repair(
                run_dir,
                code_task_paths(run_dir),
                [],
                repair_rounds=0,
                model=None,
                repair_model=None,
                use_llm=False,
                message_callback=None,
            )

            self.assertFalse(repaired)
            analysis_text = read_text(run_dir / "code_task" / "run" / "patched" / "failure_analysis.md")
            self.assertIn("final budget exhausted signal", analysis_text)

    def test_execute_runs_to_approval_gate(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nImprove the spam classifier.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )

            result = execute_code_task(run_dir, use_llm=False, timeout_sec=10)

            self.assertEqual(result.stop_reason, "approval_required")
            self.assertTrue((run_dir / "code_task" / "meta" / "environment_report.json").is_file())
            self.assertTrue((run_dir / "code_task" / "run" / "baseline" / "execution_report.json").is_file())
            self.assertFalse((run_dir / "code_task" / "work_plan.json").exists())
            self.assertFalse((run_dir / "code_task" / "attempts" / "attempt-001").exists())
            self.assertTrue((run_dir / "code_task" / "patch_plan.md").is_file())
            self.assertFalse((run_dir / "code_task" / "meta" / "proposed_edits.json").exists())
            self.assertEqual(
                [(step.step, step.status) for step in result.steps],
                [
                    ("probe", "done"),
                    ("baseline", "done"),
                    ("batch", "skipped"),
                    ("plan", "done"),
                ],
            )

    def test_execute_blocks_on_model_planning_failure_without_fallback(self) -> None:
        for step, module in (('work-plan', 'work_plan'), ('plan', 'planning')):
            with self.subTest(step=step), _temporary_root() as root:
                _, _, run_dir = _initialize_toy_task(root, '# Task\n\nImprove the spam classifier.\n',
                    benchmark_command=TOY_BENCHMARK_COMMAND)
                if step == 'plan':
                    first = execute_code_task(run_dir, use_llm=False, to_step='batch', timeout_sec=10)
                    self.assertEqual(first.stop_reason, 'stop_point')
                records = [run_dir / 'code_task' / name for name in ('work_plan.json', 'work_plan.md', 'patch_plan.md')]
                retained = {path: path.read_bytes() for path in records if path.is_file()}
                client = _FailingCodeTaskClient()
                with patch('simple_ar.code_task.editing.' + module + '.LLMClient.from_env', return_value=client):
                    result = execute_code_task(run_dir, use_llm=True, to_step=step,
                        timeout_sec=10, llm_retry_attempts=2)
                self.assertEqual(result.stop_reason, 'llm_planning_failed')
                self.assertEqual(result.steps[-1].step, step)
                self.assertEqual(result.steps[-1].status, 'blocked')
                self.assertIn('LLM response did not contain a JSON object', result.steps[-1].detail)
                self.assertEqual(client.calls, 1)
                self.assertEqual({path: path.read_bytes() for path in records if path.is_file()}, retained)

    def test_execute_uses_planning_fallback_only_when_explicitly_allowed(self) -> None:
        from simple_ar.integrations.llm import LLMResponseError, LLMStreamError, LLMDeadlineError
        from simple_ar.core.budget import BudgetExceededError
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nImprove the spam classifier with fallback allowed.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )
            fake_client = _FailingCodeTaskClient(LLMResponseError('invalid JSON', raw_response='{broken'))

            with patch("simple_ar.code_task.editing.work_plan.LLMClient.from_env", return_value=fake_client):
                result = execute_code_task(
                    run_dir,
                    use_llm=True,
                    to_step="work-plan",
                    timeout_sec=10,
                    allow_planning_fallback=True,
                    llm_retry_attempts=2,
                )

            self.assertEqual(result.stop_reason, "stop_point")
            self.assertEqual(result.steps[-1].step, "work-plan")
            self.assertEqual(result.steps[-1].status, "done")
            self.assertEqual(fake_client.calls, 2)
            work_plan = read_json(run_dir / "code_task" / "work_plan.json")
            self.assertEqual(work_plan["mode"], "offline")
            self.assertTrue(work_plan['approval']['required'])
            self.assertEqual(read_json(run_dir / 'code_task/meta/planning_steps/work-plan-response-001.json')
                ['raw_response'], '{broken')

            generate_patch_plan(run_dir, use_llm=False)
            record_plan_decision(run_dir, decision='approve')
            retained_paths = [run_dir / 'code_task' / name for name in
                ('work_plan.json', 'work_plan.md', 'patch_plan.md')]
            retained_paths.extend((run_dir / 'code_task/memory').rglob('*'))
            retained = {path: path.read_bytes() for path in retained_paths if path.is_file()}
            approved = read_json(run_dir / 'manifest.json')
            admission = LLMError('budget admission failed')
            admission.__cause__ = BudgetExceededError('remaining budget')
            cases = [(LLMError('provider unavailable'), None), (admission, None),
                (LLMStreamError('incomplete stream'), None), (LLMDeadlineError('deadline'), None),
                (LLMResponseError('unreconciled dispatch', raw_response='{broken'), 'unknown'),
                (LLMResponseError('pending dispatch', raw_response='{broken'), 'reserved'),
                (LLMError('provider failed during correction'), 'correction')]
            for generate in (generate_code_task_work_plan, generate_patch_plan):
                for error, status in cases:
                    for allow in (False, True):
                        with self.subTest(consumer=generate.__name__, error=str(error), fallback=allow):
                            client = Mock()
                            ledger = BudgetLedger({'llm_requests': 10, 'image_requests': 3})
                            client._budget_ledger = ledger
                            if status in ('unknown', 'reserved'):
                                ledger.reserve('image', {'image_requests': 1})
                                self.assertEqual(ledger.unknown_dimensions(), ())
                            ledger_after = []
                            def fail(*_args, **_kwargs):
                                if status == 'unknown':
                                    ledger.mark_unknown('image', reason='dispatch unknown',
                                        known_actual={'image_requests': 1})
                                ledger_after.append(ledger.to_dict())
                                if status == 'correction' and client.ask_json.call_count == 1:
                                    raise LLMResponseError('invalid JSON', raw_response='{broken')
                                raise error
                            client.ask_json.side_effect = fail
                            with patch.object(LLMClient, 'for_task', return_value=client), self.assertRaises(type(error)) as caught:
                                generate(run_dir, llm_client=client, llm_retry_attempts=3,
                                    allow_llm_fallback=allow, force=True)
                            self.assertIs(caught.exception, error)
                            self.assertEqual(client.ask_json.call_count, 2 if status == 'correction' else 1)
                            self.assertEqual(ledger.to_dict(), ledger_after[-1])
                            self.assertEqual({path: path.read_bytes() for path in retained}, retained)
                            manifest = read_json(run_dir / 'manifest.json')
                            self.assertEqual(manifest['plan'], approved['plan'])
                            self.assertEqual(manifest['work_plan'], approved['work_plan'])

            for generate in (generate_code_task_work_plan, generate_patch_plan):
                for allow in (False, True):
                    client = Mock()
                    errors = [LLMResponseError('invalid JSON', raw_response='{first'),
                              LLMResponseError('invalid JSON', raw_response='{second')]
                    client.ask_json.side_effect = errors
                    with self.subTest(consumer=generate.__name__, complete_format_failure=True, fallback=allow), \
                            patch.object(LLMClient, 'for_task', return_value=client):
                        if allow:
                            result = generate(run_dir, llm_client=client, llm_retry_attempts=2,
                                allow_llm_fallback=True, force=True)
                            self.assertEqual(result.mode, 'offline')
                            self.assertTrue(result.pending_approval)
                        else:
                            with self.assertRaises(LLMResponseError) as caught:
                                generate(run_dir, llm_client=client, llm_retry_attempts=2, force=True)
                            self.assertIs(caught.exception, errors[-1])
                        self.assertEqual(client.ask_json.call_count, 2)
                        self.assertIn('completed response', client.ask_json.call_args.args[1])
                client = Mock()
                ledger = BudgetLedger({'llm_requests': 2, 'total_tokens': 200})
                client._budget_ledger = ledger
                def complete(*_args, **_kwargs):
                    reservation = str(client.ask_json.call_count)
                    ledger.reserve(reservation, {'llm_requests': 1, 'total_tokens': 100})
                    ledger.settle(reservation, {'llm_requests': 1, 'total_tokens': 25})
                    if client.ask_json.call_count == 1:
                        raise LLMResponseError('invalid JSON', raw_response='{broken')
                    return {'summary': 'Corrected plan', 'items': [{'objective': 'Fix keyword handling',
                        'target_files': ['spam_model.py']}]}
                client.ask_json.side_effect = complete
                with self.subTest(consumer=generate.__name__, corrected=True), \
                        patch.object(LLMClient, 'for_task', return_value=client):
                    result = generate(run_dir, llm_client=client, llm_retry_attempts=2, force=True)
                    self.assertEqual(result.mode, 'llm')
                    self.assertTrue(result.pending_approval)
                    self.assertEqual(client.ask_json.call_count, 2)
                    self.assertEqual(ledger.remaining('llm_requests'), 0)
                    self.assertEqual(ledger.remaining('total_tokens'), 150)

    def test_execute_baseline_policies_stop_before_planning(self) -> None:
        for policy in ("auto", "run", "skip", "none", "provided"):
            with self.subTest(policy=policy), _temporary_root() as root:
                _, _, run_dir = _initialize_toy_task(
                    root, "# Task\n\nUse the requested baseline policy.\n",
                    benchmark_command=TOY_BENCHMARK_COMMAND,
                )
                metrics_file = root / "baseline_metrics.json"
                if policy == "provided":
                    write_json(metrics_file, {"accuracy": 0.75, "loss": 1.25})
                result = execute_code_task(
                    run_dir, use_llm=False, to_step="baseline", timeout_sec=10,
                    baseline_policy=policy, baseline_metrics_file=metrics_file,
                )
                self.assertEqual(result.stop_reason, "stop_point")
                self.assertEqual([step.step for step in result.steps], ["probe", "baseline"])
                self.assertFalse((run_dir / "code_task" / "patch_plan.md").exists())
                manifest = read_json(run_dir / "manifest.json")
                recorded = manifest["benchmark"]["baseline_policy"]
                report = run_dir / "code_task" / "run" / "baseline" / "execution_report.json"
                if policy in {"skip", "none"}:
                    self.assertFalse(report.exists())
                    self.assertEqual(recorded["policy"], policy)
                    self.assertEqual(recorded["status"], "skipped")
                    summary = read_text(run_dir / "code_task" / "summary.md")
                    self.assertIn("Baseline policy", summary)
                    self.assertIn(policy, summary)
                else:
                    self.assertEqual(read_json(report)["status"], "passed")
                    self.assertEqual(recorded["policy"], "provided" if policy == "provided" else "run")
                    if policy == "provided":
                        self.assertTrue(read_json(report)["provided_baseline"])
                        self.assertEqual(read_json(report)["metric_values"]["accuracy"], 0.75)
                        self.assertTrue(manifest["benchmark"]["runs"]["baseline"]["provided"])

    def test_execute_baseline_failure_keeps_immediate_and_committed_guards(self) -> None:
        for status in ("failed", "timed_out", "blocked_by_validation", "unknown"):
            for cached, recorded_status in ((False, status), (True, status), (False, None), (False, "passed")):
                with self.subTest(status=status, cached=cached, recorded=recorded_status), _temporary_root() as root:
                    _, _, run_dir = _initialize_toy_task(root, "# Task\n\nKeep baseline evidence.\n",
                        benchmark_command=TOY_BENCHMARK_COMMAND)
                    paths = code_task_paths(run_dir)

                    def commit_baseline(*_args, **_kwargs):
                        if recorded_status is not None:
                            manifest = read_json(run_dir / "manifest.json")
                            manifest.setdefault("benchmark", {}).setdefault("runs", {})["baseline"] = {"status": recorded_status}
                            write_json(run_dir / "manifest.json", manifest)
                        return SimpleNamespace(status=status, metrics={},
                            report_path=paths.run_artifact_dir / "baseline/execution_report.json",
                            metrics_path=paths.run_artifact_dir / "baseline/metrics.json")

                    if cached:
                        commit_baseline()
                    with patch("simple_ar.code_task.orchestration.execute.run_code_task_baseline",
                               side_effect=commit_baseline) as baseline, \
                         patch("simple_ar.code_task.orchestration.execute.generate_patch_plan") as plan:
                        result = execute_code_task(run_dir, use_llm=False)
                    self.assertEqual(result.stop_reason, "baseline_failed")
                    self.assertEqual(result.steps[-1].status, "skipped" if cached else "done")
                    self.assertEqual(baseline.call_count, 0 if cached else 1)
                    plan.assert_not_called()

    def test_execute_dry_run_has_no_side_effects(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nPreview orchestration.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )

            result = execute_code_task(run_dir, dry_run=True, use_llm=False)

            self.assertEqual(result.stop_reason, "dry_run")
            self.assertEqual(result.steps[-1].status, "would_run")
            self.assertEqual(result.steps[-1].step, "probe")
            self.assertFalse((run_dir / "code_task" / "meta" / "environment_report.json").exists())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main(["code-task", "execute", str(run_dir), "--dry-run", "--no-llm"])
            output = stdout.getvalue()
            self.assertIn("Stop reason", output)
            self.assertIn("dry_run", output)
            self.assertIn("probe", output)
            self.assertIn("would_run", output)

    def test_execute_cli_reads_runtime_config(self) -> None:
        with _temporary_root() as root:
            code_root = root / "toy_project"
            task_file = root / "task.md"
            config_file = root / "execute.toml"
            _write_toy_project(code_root)
            write_text(task_file, "# Task\n\nRun configured execute.\n")
            write_text(
                config_file,
                (
                    "[execute]\n"
                    'to_step = "baseline"\n'
                    "use_llm = false\n"
                    "timeout_sec = 10\n"
                    "max_files = 3\n"
                    "max_source_chars_per_file = 900\n"
                    "\n"
                    "[models.code_task]\n"
                    'planner = "planner-model"\n'
                    'editor = "editor-model"\n'
                    'repair = "repair-model"\n'
                    "\n"
                    "[budget]\n"
                    'profile = "normal"\n'
                    "max_batches = 2\n"
                    "cost_cap_usd = 1.0\n"
                    "\n"
                    "[budget.normal]\n"
                    "max_edits = 3\n"
                    "\n"
                    "[environment]\n"
                    'mode = "current"\n'
                ),
            )
            run_dir = root / "runs" / "code-task-run"
            initialize_code_task(
                run_dir=run_dir,
                code_root=code_root,
                task_file=task_file,
                benchmark_command="python -m unittest discover -s tests",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                main(["code-task", "execute", str(run_dir), "--config", str(config_file)])

            output = stdout.getvalue()
            self.assertIn("Stop reason", output)
            self.assertIn("stop_point", output)
            self.assertIn("baseline", output)
            self.assertIn("done", output)
            self.assertFalse((run_dir / "code_task" / "work_plan.json").exists())

    def test_execute_interactive_skips_completed_steps_without_prompting(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nResume already completed execute steps.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )
            result = execute_code_task(run_dir, to_step="baseline", use_llm=False, timeout_sec=10)
            self.assertEqual(result.stop_reason, "stop_point")

            stdout = io.StringIO()
            with patch("simple_ar.cli.main.confirm_next_step") as confirm:
                with contextlib.redirect_stdout(stdout):
                    main(["code-task", "execute", str(run_dir), "--to-step", "baseline", "--interactive", "--no-llm"])

            confirm.assert_not_called()
            output = stdout.getvalue()
            self.assertIn("probe", output)
            self.assertIn("baseline", output)
            self.assertIn("skipped", output)

            with patch("simple_ar.cli.main.confirm_next_step", return_value=True):
                with contextlib.redirect_stdout(io.StringIO()):
                    main(["code-task", "execute", str(run_dir), "--to-step", "plan", "--interactive", "--no-llm"])
            self.assertTrue((run_dir / "code_task/patch_plan.md").is_file())
            self.assertFalse((run_dir / "code_task/work_plan.json").exists())

    def test_execute_inline_review_can_approve_plan_and_continue(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nApprove plan inline and continue.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )

            stdout = io.StringIO()
            with (
                patch("sys.stdin.isatty", return_value=True),
                patch("simple_ar.cli.main.confirm_review_gate", return_value=True) as confirm,
                contextlib.redirect_stdout(stdout),
            ):
                main(
                    [
                        "code-task",
                        "execute",
                        str(run_dir),
                        "--to-step",
                        "propose-edits",
                        "--no-llm",
                    ]
                )

            confirm.assert_called_once()
            output = stdout.getvalue()
            self.assertIn("Patch Plan Review", output)
            self.assertTrue((run_dir / "code_task" / "meta" / "proposed_edits.json").is_file())
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["plan"]["status"], "approved")

    def test_execute_applies_reviewed_proposal_after_approval(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nAdd another spam keyword.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )
            execute_code_task(run_dir, use_llm=False, timeout_sec=10, to_step='batch')
            first = execute_code_task(run_dir, use_llm=False, timeout_sec=10)
            self.assertEqual(first.stop_reason, "approval_required")
            record_plan_decision(run_dir, decision="approve")
            _write_valid_edit_proposal(run_dir)

            result = execute_code_task(
                run_dir,
                use_llm=False,
                timeout_sec=10,
                apply_proposed_edits=True,
            )

            self.assertEqual(result.stop_reason, "completed")
            self.assertTrue((run_dir / "code_task" / "patch.diff").is_file())
            self.assertTrue((run_dir / "code_task" / "run" / "patched" / "execution_report.json").is_file())
            self.assertIn("'prize'", read_text(run_dir / "code_task" / "workspace" / "spam_model.py"))
            step_status = [(step.step, step.status) for step in result.steps]
            self.assertIn(("apply-edits", "done"), step_status)
            self.assertIn(("validate", "done"), step_status)
            self.assertIn(("run", "done"), step_status)
            batch_state = read_json(
                run_dir
                / "code_task"
                / "attempts"
                / "attempt-001"
                / "batches"
                / "batch-001"
                / "batch_state.json"
            )
            self.assertEqual(batch_state["state"], "completed")
            self.assertEqual(batch_state["validation_status"], "passed")
            self.assertEqual(batch_state["benchmark_status"], "passed")
            attempt_state = read_json(
                run_dir / "code_task" / "attempts" / "attempt-001" / "attempt_state.json"
            )
            self.assertEqual(attempt_state["state"], "completed")
            self.assertEqual(attempt_state["batches"][0]["state"], "completed")
            manifest = read_json(run_dir / "manifest.json")
            self.assertEqual(manifest["work_plan"]["status"], "completed")

            # Completed artifacts still honor every preparation stop point.
            preparation = ("probe", "baseline", "work-plan", "batch", "plan",
                           "propose-edits", "apply-edits", "review", "validate")
            for boundary in preparation:
                with self.subTest(to_step=boundary), patch(
                    "simple_ar.code_task.orchestration.execute.run_code_task_benchmark",
                ) as benchmark:
                    stopped = execute_code_task(run_dir, use_llm=False, to_step=boundary)
                self.assertEqual(stopped.stop_reason, "stop_point")
                expected = list(preparation[:preparation.index(boundary) + 1])
                if boundary not in {"work-plan", "batch"} and "work-plan" in expected:
                    expected.remove("work-plan")
                self.assertEqual([step.step for step in stopped.steps], expected)
                benchmark.assert_not_called()

            # A saved failure must not turn into success merely on resume.
            report_path = run_dir / "code_task" / "meta" / "review_report.json"
            saved_review = read_json(report_path)
            for status, expected in (("failed", "review_failed"), ("warning", "stop_point"), ("passed", "stop_point")):
                with self.subTest(cached_review=status):
                    write_json(report_path, {**saved_review, "status": status})
                    before = report_path.read_bytes()
                    with patch("simple_ar.code_task.orchestration.execute.review_code_task_changes") as reviewer, \
                         patch("simple_ar.code_task.orchestration.execute.run_code_task_benchmark") as benchmark:
                        resumed = execute_code_task(run_dir, use_llm=False, to_step="review")
                    self.assertEqual(resumed.stop_reason, expected)
                    reviewer.assert_not_called()
                    benchmark.assert_not_called()
                    self.assertEqual(report_path.read_bytes(), before)

            post_run = run_dir / "code_task" / "meta" / "review_report_post_run.json"
            write_json(post_run, {**read_json(post_run), "status": "failed"})
            before = post_run.read_bytes()
            with patch("simple_ar.code_task.orchestration.execute.review_code_task_changes") as reviewer:
                resumed = execute_code_task(run_dir, use_llm=False, to_step="run", timeout_sec=10)
            self.assertEqual(resumed.stop_reason, "review_failed")
            reviewer.assert_not_called()
            self.assertEqual(post_run.read_bytes(), before)

    def test_execute_failure_boundaries_keep_analysis_and_repair_separate(self) -> None:
        for failed_step in ("validate", "run"):
            with self.subTest(failed_step=failed_step), _temporary_root() as root:
                _, _, run_dir = _initialize_toy_task(root, "# Task\n\nRepair within the approved scope.\n",
                    benchmark_command=TOY_BENCHMARK_COMMAND)
                execute_code_task(run_dir, use_llm=False, baseline_policy="skip")
                record_plan_decision(run_dir, decision="approve")
                _write_valid_edit_proposal(run_dir)
                apply_patch_edits(run_dir)
                paths = code_task_paths(run_dir)
                for boundary in ("validate", "run", "analyze-failure", "repair"):
                    with self.subTest(to_step=boundary), contextlib.ExitStack() as stack:
                        def stub(name, **values):
                            return stack.enter_context(patch(
                                "simple_ar.code_task.orchestration.execute." + name,
                                return_value=SimpleNamespace(**values)))
                        stub("review_code_task_changes", status="passed", blocking_count=0, warning_count=0)
                        stub("validate_code_task", status="failed" if failed_step == "validate" else "passed",
                             error_count=int(failed_step == "validate"), warning_count=0)
                        benchmark = stub("run_code_task_benchmark", status="failed", metrics={},
                            report_path=paths.run_artifact_dir / "patched/execution_report.json",
                            metrics_path=paths.run_artifact_dir / "patched/metrics.json")
                        analysis = stub("analyze_code_task_failure", source=failed_step, status="failed",
                            analysis_path=paths.run_artifact_dir / "patched/failure_analysis.md")
                        repair = stub("propose_repair_edits", mode="offline", edit_count=1, selected_files=(),
                            proposal_path=paths.task_dir / "repairs/repair-001/proposed_edits.json")
                        result = execute_code_task(run_dir, use_llm=False, to_step=boundary, repair_rounds=1)
                        expected = ("repair_review_required" if boundary == "repair" else
                                    "failure_analyzed" if boundary == "analyze-failure" else
                                    "stop_point" if failed_step == "run" and boundary == "validate" else
                                    "validation_failed" if failed_step == "validate" else "benchmark_failed")
                        self.assertEqual(result.stop_reason, expected)
                        self.assertEqual(benchmark.call_count, int(failed_step == "run" and boundary != "validate"))
                        self.assertEqual(analysis.call_count, int(boundary in {"analyze-failure", "repair"}))
                        self.assertEqual(repair.call_count, int(boundary == "repair"))
                        self.assertEqual(read_text(paths.workspace_dir / "spam_model.py").count("'prize'"), 1)

    def test_execute_generates_repair_proposal_after_failed_run(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nBreak then repair the spam classifier.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )
            execute_code_task(run_dir, use_llm=False, timeout_sec=10, to_step='batch')
            first = execute_code_task(run_dir, use_llm=False, timeout_sec=10)
            self.assertEqual(first.stop_reason, "approval_required")
            record_plan_decision(run_dir, decision="approve")
            _write_failing_edit_proposal(run_dir)

            result = execute_code_task(
                run_dir,
                use_llm=False,
                timeout_sec=10,
                apply_proposed_edits=True,
                to_step="repair",
                repair_rounds=1,
            )

            self.assertEqual(result.stop_reason, "repair_review_required")
            self.assertTrue((run_dir / "code_task" / "run" / "patched" / "failure_analysis.md").is_file())
            self.assertTrue((run_dir / "code_task" / "repairs" / "repair-001" / "proposed_edits.json").is_file())
            repair_batch = read_json(
                run_dir
                / "code_task"
                / "attempts"
                / "attempt-001"
                / "batches"
                / "batch-002"
                / "batch_state.json"
            )
            self.assertEqual(repair_batch["kind"], "repair")
            self.assertEqual(repair_batch["parent_batch_id"], "batch-001")
            self.assertIn("repair_proposal", repair_batch["artifacts"])
            attempt_state = read_json(
                run_dir / "code_task" / "attempts" / "attempt-001" / "attempt_state.json"
            )
            self.assertEqual(attempt_state["state"], "failed")
            self.assertEqual(attempt_state["batches"][0]["state"], "failed")
            self.assertEqual(attempt_state["batches"][1]["kind"], "repair")
            step_status = [(step.step, step.status) for step in result.steps]
            self.assertIn(("analyze-failure", "done"), step_status)
            self.assertIn(("repair", "done"), step_status)

    def test_execute_reports_patch_apply_failure_without_traceback(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nHandle an invalid proposal.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )
            execute_code_task(run_dir, use_llm=False, timeout_sec=10)
            record_plan_decision(run_dir, decision="approve")
            write_json(
                run_dir / "code_task" / "meta" / "proposed_edits.json",
                {
                    "edits": [
                        {
                            "path": "spam_model.py",
                            "old": "text that is not in the file",
                            "new": "replacement",
                            "reason": "invalid proposal",
                        }
                    ]
                },
            )

            result = execute_code_task(
                run_dir,
                use_llm=False,
                timeout_sec=10,
                apply_proposed_edits=True,
            )

            self.assertEqual(result.stop_reason, "patch_apply_failed")
            self.assertEqual(result.steps[-1].step, "apply-edits")
            self.assertEqual(result.steps[-1].status, "blocked")
            self.assertIn("old text was not found", result.steps[-1].detail)

            restoration_failure = PatchValidationError("Rejected write")
            restoration_failure.add_note("Snapshot restore byte check failed for spam_model.py")
            with patch("simple_ar.code_task.orchestration.execute.apply_patch_edits",
                       side_effect=restoration_failure):
                restored = execute_code_task(run_dir, use_llm=False, timeout_sec=10, apply_proposed_edits=True)
            self.assertEqual(restored.stop_reason, "patch_rollback_failed")
            self.assertIn("byte check failed", restored.steps[-1].detail)
            self.assertIn("current workspace", restored.next_action)

            # File permissions are not an edit-budget approval request, even
            # when the failing file happens to contain 'budget' in its name.
            with patch("simple_ar.code_task.orchestration.execute.apply_patch_edits",
                       side_effect=PermissionError("budget.json: access denied")):
                with self.assertRaisesRegex(PermissionError, "access denied"):
                    execute_code_task(run_dir, use_llm=False, timeout_sec=10, apply_proposed_edits=True)

    def test_execute_regenerates_one_invalid_proposal_with_failure_context(self) -> None:
        with _temporary_root() as root:
            code_root, task_file = root / "project", root / "task.md"
            _write_toy_project(code_root)
            write_text(task_file, "Improve the classifier within its existing edit budget.")
            run_dir = root / "run"
            initialize_code_task(run_dir=run_dir, code_root=code_root, task_file=task_file,
                                 benchmark_command="python -m unittest discover -s tests")
            execute_code_task(run_dir, use_llm=False, timeout_sec=10)
            record_plan_decision(run_dir, decision="approve")
            proposal_path = run_dir / "code_task/meta/proposed_edits.json"
            write_json(proposal_path, {"edits": [], "budget": {"status": "rejected_absolute"},
                                      "warnings": ["Rejected suspected whole-file rewrite for spam_model.py."]})
            original = (run_dir / "code_task/workspace/spam_model.py").read_bytes()
            proposal = SimpleNamespace(mode="llm", edit_count=0, proposal_path=proposal_path,
                                       selected_files=("spam_model.py",))
            with patch("simple_ar.code_task.orchestration.execute.propose_patch_edits", return_value=proposal):
                rejected = execute_code_task(run_dir, use_llm=True, timeout_sec=10,
                    approval_note="approved isolated edits", apply_proposed_edits=True)
            self.assertEqual(rejected.stop_reason, "edit_budget_rejected")
            self.assertIn("whole-file rewrite", rejected.next_action)
            self.assertEqual((run_dir / "code_task/workspace/spam_model.py").read_bytes(), original)
            findings = read_jsonl(run_dir / "code_task/memory/review_findings.jsonl")
            self.assertTrue(any(row.get("key") == "edit-proposal-budget-rejected" for row in findings))
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nImprove the spam classifier.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )
            execute_code_task(run_dir, use_llm=False, timeout_sec=10)
            record_plan_decision(run_dir, decision="approve")
            proposal_path = run_dir / "code_task" / "meta" / "proposed_edits.json"
            write_json(proposal_path, {"edits": [{
                "path": "spam_model.py",
                "old": "text that is not in the file",
                "new": "replacement",
                "reason": "stale proposal",
            }]})
            workspace_file = run_dir / "code_task" / "workspace" / "spam_model.py"
            original = workspace_file.read_text(encoding="utf-8")
            first = execute_code_task(
                run_dir, use_llm=False, timeout_sec=10, apply_proposed_edits=True,
            )
            self.assertEqual(first.stop_reason, "patch_apply_failed")
            self.assertEqual(workspace_file.read_text(encoding="utf-8"), original)

            class RecoveryClient:
                def __init__(self) -> None:
                    self.user = ""

                def ask_json(self, _system: str, user: str, *, label: str = "", **kwargs: object) -> dict[str, object]:
                    del label, kwargs
                    self.user = user
                    return {
                        "summary": "Add the observed prize keyword handling.",
                        "edits": [{
                            "path": "spam_model.py",
                            "old": "return 'spam' if 'win' in text.lower() else 'ham'",
                            "new": "lowered = text.lower()\n    return 'spam' if any(keyword in lowered for keyword in ('win', 'prize')) else 'ham'",
                            "reason": "Use the exact current function body after the prior stale replacement failed.",
                        }],
                        "validation": ["Run the existing unit tests."],
                        "risks": [],
                    }

            client = RecoveryClient()
            # A connection failure has not produced a correction and must not
            # consume the one semantic correction on subsequent continuation.
            with patch("simple_ar.code_task.orchestration.execute.propose_patch_edits",
                       side_effect=LLMError("Connection error")):
                with self.assertRaisesRegex(LLMError, "Connection error"):
                    execute_code_task(
                        run_dir, llm_client=client, use_llm=True, timeout_sec=10,
                        apply_proposed_edits=True, approval_note="approved isolated correction",
                        to_step="apply-edits",
                    )
            self.assertEqual(workspace_file.read_text(encoding="utf-8"), original)
            with patch("simple_ar.code_task.editing.patching.LLMClient.for_task", return_value=client):
                recovered = execute_code_task(
                    run_dir, llm_client=client, use_llm=True, timeout_sec=10,
                    apply_proposed_edits=True, approval_note="approved isolated correction",
                    to_step="apply-edits",
                )
            self.assertEqual(recovered.stop_reason, "stop_point")
            self.assertIn("old text was not found", client.user)
            self.assertIn("return 'spam' if 'win' in text.lower() else 'ham'", client.user)
            patched = workspace_file.read_text(encoding="utf-8")
            self.assertIn("'prize'", patched)
            self.assertTrue((run_dir / "code_task" / "meta" / "applied_edits.json").is_file())

    def test_analyze_validation_failure_without_benchmark_run(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nRepair a syntax error before running tests.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )
            write_text(run_dir / "code_task" / "workspace" / "spam_model.py", "def broken(:\n")
            validation = validate_code_task(run_dir)
            self.assertEqual(validation.status, "failed")

            analysis = analyze_code_task_failure(run_dir)

            self.assertEqual(analysis.status, "needs_repair")
            self.assertEqual(analysis.source, "validation")
            self.assertEqual(analysis.analysis_path.name, "failure_analysis.md")
            self.assertIn("spam_model.py", analysis.implicated_files)
            analysis_text = read_text(analysis.analysis_path)
            self.assertIn("Static validation failed", analysis_text)

            repair = propose_repair_edits(run_dir, use_llm=False)
            self.assertEqual(repair.mode, "offline")
            proposal = read_json(repair.proposal_path)
            self.assertEqual(proposal["source_analysis"], "code_task/meta/failure_analysis.md")
            self.assertIn("spam_model.py", proposal["selected_files"])

    def test_repair_proposal_drops_edits_outside_selected_context(self) -> None:
        with _temporary_root() as root:
            code_root = root / "toy_project"
            task_file = root / "task.md"
            _write_toy_project(code_root)
            write_text(code_root / "extra.py", "VALUE = 1\n")
            write_text(task_file, "# Task\n\nRepair the broken spam classifier.\n")
            run_dir = root / "runs" / "code-task-run"
            initialize_code_task(
                run_dir=run_dir,
                code_root=code_root,
                task_file=task_file,
                benchmark_command="python -m unittest discover -s tests",
            )
            _approve_offline_patch_plan(run_dir)
            apply_patch_edits(run_dir, edits_file=_write_failing_edit_proposal(run_dir))
            failed = run_code_task_benchmark(run_dir, timeout_sec=10)
            self.assertEqual(failed.status, "failed")
            analyze_code_task_failure(run_dir)

            fake_client = _FakeRepairClient(
                {
                    "summary": "Attempt to repair an unrelated file.",
                    "edits": [
                        {
                            "path": "extra.py",
                            "old": "VALUE = 1\n",
                            "new": "VALUE = 2\n",
                            "reason": "This is outside the selected repair context.",
                        }
                    ],
                    "validation": ["Would need tests."],
                    "risks": ["Unrelated edit."],
                }
            )

            with patch("simple_ar.code_task.execution.repair.LLMClient.from_env", return_value=fake_client):
                repair = propose_repair_edits(run_dir, use_llm=True, max_files=1)

            proposal = read_json(repair.proposal_path)
            self.assertEqual(proposal["mode"], "llm")
            self.assertEqual(proposal["edits"], [])
            self.assertIn("spam_model.py", proposal["selected_files"])
            self.assertIn("Dropped edit outside repair context: extra.py", proposal["warnings"])

    def test_repair_creates_declared_missing_files_through_existing_apply(self) -> None:
        from simple_ar.code_task.execution.repair import RepairEvidence
        with _temporary_root() as root:
            _, _, run_dir = _initialize_toy_task(root, 'Create the missing adapter without replacing source.')
            _approve_offline_patch_plan(run_dir)
            paths = code_task_paths(run_dir)
            manifest = read_json(paths.manifest_path)
            manifest['code_task']['native_binding'] = {'binding': {'contract': {
                'new_files': ['src/adapter.py', 'src/empty.py', 'tests/new_test.py',
                              '../escape.py', 'spam_model.py/child.py', 'other.py']}}}
            manifest['edit_scope']['allowed_patterns'] = ['spam_model.py', 'src/*.py', 'tests/*']
            manifest['work_plan'] = {'approval': {'status': 'approved'}}
            write_json(paths.manifest_path, manifest)
            write_json(paths.task_dir / 'work_plan.json', {'new_files': [{'path': 'src/planned.py'}]})
            original_index = read_json(paths.meta_dir / 'codebase_index.json')
            original_source = read_text(paths.workspace_dir / 'spam_model.py')
            client = _FakeRepairClient({'edits': [
                {'path': name, 'old': '', 'new': '' if name.endswith('empty.py') else 'VALUE = 1\n',
                 'reason': 'Create missing source'} for name in
                ('src/adapter.py', 'src/planned.py', 'src/empty.py', 'src/unknown.py',
                 'tests/new_test.py', '../escape.py', 'spam_model.py/child.py', 'other.py')]})
            with patch.object(client, 'ask_json', wraps=client.ask_json) as call, patch(
                    'simple_ar.code_task.execution.repair.LLMClient.from_env', return_value=client):
                repair = propose_repair_edits(run_dir, max_files=5,
                    failure_evidence=RepairEvidence('validator/report.json', {'execution_status': 'failed'},
                                                   'Required creation missing: src/adapter.py'))
            proposal = read_json(repair.proposal_path)
            self.assertEqual([row['path'] for row in proposal['edits']], ['src/adapter.py', 'src/planned.py'])
            self.assertNotIn('src/adapter.py', proposal['selected_files'])
            prompt = call.call_args.args[1]
            self.assertIn('Declared missing creation targets', prompt)
            self.assertIn('src/adapter.py', prompt.split('Selected source snippets:')[0])
            self.assertNotIn('src/adapter.py', prompt.split('Selected source snippets:')[1])
            self.assertEqual(read_json(paths.meta_dir / 'codebase_index.json'), original_index)
            apply_patch_edits(run_dir, edits_file=repair.proposal_path)
            self.assertEqual(read_text(paths.workspace_dir / 'src/adapter.py'), 'VALUE = 1\n')
            self.assertEqual(read_text(paths.workspace_dir / 'src/planned.py'), 'VALUE = 1\n')
            self.assertEqual(read_text(paths.workspace_dir / 'spam_model.py'), original_source)
            self.assertFalse((paths.workspace_dir / 'src/unknown.py').exists())

            # An approved named JSON patch plan also works without a work-plan.
            manifest = read_json(paths.manifest_path)
            manifest.pop('work_plan', None)
            manifest['code_task'].pop('native_binding', None)
            manifest['plan']['patch_plan'] = 'code_task/patch_plan.json'
            write_json(paths.manifest_path, manifest)
            write_json(paths.task_dir / 'patch_plan.json', {'new_files': [{'path': 'src/json_adapter.py'}]})
            client = _FakeRepairClient({'edits': [{'path': 'src/json_adapter.py', 'old': '',
                'new': 'VALUE = 2\n', 'reason': 'Create the accepted plan target'}]})
            with patch('simple_ar.code_task.execution.repair.LLMClient.from_env', return_value=client):
                repair = propose_repair_edits(run_dir,
                    failure_evidence=RepairEvidence('validator/report.json', {'execution_status': 'failed'},
                                                   'Required creation missing: src/json_adapter.py'))
            self.assertEqual(repair.edit_count, 1)
            apply_patch_edits(run_dir, edits_file=repair.proposal_path)
            self.assertEqual(read_text(paths.workspace_dir / 'src/json_adapter.py'), 'VALUE = 2\n')

    def test_repair_creation_rechecks_absence_and_does_not_grant_unseen_source(self) -> None:
        from simple_ar.code_task.execution.repair import RepairEvidence
        with _temporary_root() as root:
            _, _, run_dir = _initialize_toy_task(root, 'Create only the frozen missing file.')
            _approve_offline_patch_plan(run_dir)
            paths = code_task_paths(run_dir)
            manifest = read_json(paths.manifest_path)
            manifest['code_task']['native_binding'] = {'binding': {'contract': {'new_files': ['src/raced.py']}}}
            manifest['work_plan'] = {'approval': {'status': 'pending'}}
            manifest['plan']['status'] = 'pending_approval'
            write_json(paths.manifest_path, manifest)
            write_text(paths.task_dir / 'patch_plan.md', '## New Files\n\n- `src/unapproved.py`: Not approved.\n')
            write_json(paths.task_dir / 'work_plan.json', {'new_files': ['src/unapproved.py']})
            source = read_text(paths.workspace_dir / 'spam_model.py')
            def reply(*args, **kwargs):
                write_text(paths.workspace_dir / 'src/raced.py', 'OWNER = 7\n')
                return {'edits': [
                    {'path': 'src/raced.py', 'old': '', 'new': 'OWNER = 9\n'},
                    {'path': 'src/unapproved.py', 'old': '', 'new': 'VALUE = 1\n'},
                    {'path': 'spam_model.py', 'old': source, 'new': 'VALUE = 1\n'}]}
            client = _FakeRepairClient({})
            with patch.object(client, 'ask_json', side_effect=reply) as call, patch(
                    'simple_ar.code_task.execution.repair.LLMClient.from_env', return_value=client):
                repair = propose_repair_edits(run_dir, max_files=1,
                    failure_evidence=RepairEvidence('validator/report.json', {'execution_status': 'failed'},
                                                   'Required creation missing: src/raced.py'))
            self.assertEqual(read_json(repair.proposal_path)['edits'], [])
            self.assertEqual(repair.selected_files, ())
            self.assertEqual(read_text(paths.workspace_dir / 'src/raced.py'), 'OWNER = 7\n')
            self.assertEqual(read_text(paths.workspace_dir / 'spam_model.py'), source)
            declaration = call.call_args.args[1].split('Declared missing creation targets')[1].split('Read-only context paths')[0]
            self.assertNotIn('src/unapproved.py', declaration)
            self.assertFalse((paths.workspace_dir / 'src/unapproved.py').exists())

    def test_repair_proposal_drops_diff_marker_edits(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nRepair the broken spam classifier.\n",
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )
            _approve_offline_patch_plan(run_dir)
            apply_patch_edits(run_dir, edits_file=_write_failing_edit_proposal(run_dir))
            failed = run_code_task_benchmark(run_dir, timeout_sec=10)
            self.assertEqual(failed.status, "failed")
            analyze_code_task_failure(run_dir)

            fake_client = _FakeRepairClient(
                {
                    "summary": "Accidentally return a diff hunk.",
                    "edits": [
                        {
                            "path": "spam_model.py",
                            "old": (
                                "-def predict(text):\n"
                                "-    return 'ham'\n"
                                "+def predict(text):\n"
                                "+    return 'spam'\n"
                            ),
                            "new": (
                                "def predict(text):\n"
                                "    return 'spam'\n"
                            ),
                            "reason": "The model should not put diff markers in old.",
                        }
                    ],
                    "validation": ["python -m unittest discover -s tests"],
                    "risks": [],
                }
            )

            with patch("simple_ar.code_task.execution.repair.LLMClient.from_env", return_value=fake_client):
                repair = propose_repair_edits(run_dir, use_llm=True)

            proposal = read_json(repair.proposal_path)
            self.assertEqual(proposal["edits"], [])
            self.assertIn(
                "Dropped edit for spam_model.py: old/new must be exact text, not a diff fragment.",
                proposal["warnings"],
            )

    def test_code_task_validate_run_and_failure_cli(self) -> None:
        with _temporary_root() as root:
            code_root, task_file, run_dir = _initialize_toy_task(
                root, "# Task\n\nRun CLI validation and tests.\n", init_cli=True,
                benchmark_command=TOY_BENCHMARK_COMMAND,
            )

            validate_stdout = io.StringIO()
            with contextlib.redirect_stdout(validate_stdout):
                main(["code-task", "validate", str(run_dir)])
            self.assertIn("Status: passed", validate_stdout.getvalue())

            run_stdout = io.StringIO()
            with contextlib.redirect_stdout(run_stdout):
                main(["code-task", "run", str(run_dir), "--timeout", "10"])
            self.assertIn("Status: passed", run_stdout.getvalue())
            self.assertTrue((run_dir / "code_task" / "run" / "patched" / "execution_report.json").is_file())

            status_stdout = io.StringIO()
            with contextlib.redirect_stdout(status_stdout):
                main(["status", str(run_dir)])
            self.assertIn("Validation:", status_stdout.getvalue())
            self.assertIn("last status: passed", status_stdout.getvalue())


@contextlib.contextmanager
def _temporary_root():
    """One isolated root per scenario, cleaned even when an assertion fails."""
    TEST_ROOT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
        yield Path(tmp)


def _generated_review(project: Path, rows: list[dict], **overrides) -> dict:
    inputs = dict(project_dir=project, code_artifacts={"generated_files": rows},
        result_schema={}, resource_plan={"max_files": 10, "max_generated_lines": 200},
        use_llm=False)
    return review_generated_project(**(inputs | overrides))


def _architecture_plan(**overrides):
    from simple_ar.code_task.generation.architecture import build_architecture_plan

    inputs = dict(contract={}, result_schema={}, resource_plan={}, domain_profile={})
    return build_architecture_plan(**(inputs | overrides))


def _project_bytes(project: Path) -> dict:
    return {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}


def _initialize_greenfield(run_dir: Path, task_file: Path, **overrides):
    """Shared empty-project boundary, with metric and resource choices explicit."""
    return initialize_code_task(run_dir=run_dir, code_root=None, task_file=task_file,
        kind="greenfield", workspace_mode="empty",
        benchmark_command="python generated_project/main.py", **overrides)


def _initialize_toy_task(
    root: Path, task_text: str, *, init_cli: bool = False, **options,
) -> tuple[Path, Path, Path]:
    """Create the shared project without supplying implicit task authorization."""
    code_root = root / "toy_project"
    task_file = root / "task.md"
    _write_toy_project(code_root)
    write_text(task_file, task_text)
    run_dir = root / "runs" / "code-task-run"
    if init_cli:
        args = ["code-task", "init", "--code-root", str(code_root),
                "--task-file", str(task_file), "--output-root", str(root / "runs")]
        for name, value in options.items():
            args.extend(["--" + name.replace("_", "-"), str(value)])
        with contextlib.redirect_stdout(io.StringIO()):
            main(args)
        run_dir = next((root / "runs").iterdir())
    else:
        initialize_code_task(
            run_dir=run_dir, code_root=code_root, task_file=task_file, **options,
        )
    return code_root, task_file, run_dir


def _approve_offline_patch_plan(run_dir: Path) -> None:
    """Prepare an approved plan only in scenarios that already grant approval."""
    generate_patch_plan(run_dir, use_llm=False)
    record_plan_decision(run_dir, decision="approve")


def _write_metric_project(
    code_root: Path,
    *,
    value: str,
    metric_name: str = "accuracy",
) -> None:
    write_text(code_root / "metric_value.txt", value + "\n")
    write_text(
        code_root / "benchmark.py",
        (
            "from pathlib import Path\n\n"
            "value = float(Path('metric_value.txt').read_text().strip())\n"
            f"print(f'{metric_name}: {{value:.6f}}')\n"
            "print('train_time_sec: 0.010000')\n"
        ),
    )


def _write_analysis_first_work_plan(run_dir: Path) -> None:
    work_plan = {
        "schema_version": 1,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "mode": "test",
        "summary": "Analysis item appears before the actual implementation item.",
        "goal": "Improve spam prediction.",
        "success_criteria": ["Tests pass after a useful code change."],
        "items": [
            {
                "id": "W1",
                "status": "pending",
                "objective": "Inspect current behavior and identify candidate improvements.",
                "target_files": ["spam_model.py"],
                "read_only_evidence": ["tests/test_spam_model.py"],
                "depends_on": [],
                "validation": ["python -m unittest discover -s tests"],
                "done_criteria": ["Behavior is understood."],
                "risk": "Low",
                "parallelizable": False,
                "budget_profile": "normal",
                "requires_budget_override": False,
                "suggested_budget_override": "",
                "context_request": {"query": "inspect", "files": ["spam_model.py"], "symbols": []},
            },
            {
                "id": "W2",
                "status": "pending",
                "objective": "Implement keyword handling improvement in the classifier.",
                "target_files": ["spam_model.py"],
                "read_only_evidence": ["tests/test_spam_model.py"],
                "depends_on": ["W1"],
                "validation": ["python -m unittest discover -s tests"],
                "done_criteria": ["The classifier recognizes the requested keyword."],
                "risk": "Low",
                "parallelizable": False,
                "budget_profile": "normal",
                "requires_budget_override": False,
                "suggested_budget_override": "",
                "context_request": {"query": "implement", "files": ["spam_model.py"], "symbols": []},
            },
        ],
        "context_requests": [],
        "risks": [],
        "approval": {"required": True, "status": "pending", "reason": "Review before editing."},
        "selected_files": ["spam_model.py", "tests/test_spam_model.py"],
        "context_pack": None,
        "run_context": {},
        "budget_profiles": {},
    }
    write_json(run_dir / "code_task" / "work_plan.json", work_plan)
    write_text(run_dir / "code_task" / "work_plan.md", "# Work Plan\n")
    manifest = read_json(run_dir / "manifest.json")
    manifest["layout"]["work_plan"] = "code_task/work_plan.json"
    manifest["layout"]["work_plan_markdown"] = "code_task/work_plan.md"
    manifest["work_plan"] = {
        "status": "pending_approval",
        "mode": "test",
        "path": "code_task/work_plan.json",
        "markdown": "code_task/work_plan.md",
        "item_count": 2,
        "selected_files": ["spam_model.py", "tests/test_spam_model.py"],
        "context_pack": None,
        "approval": work_plan["approval"],
    }
    write_json(run_dir / "manifest.json", manifest)


def _write_dependent_work_plan(run_dir: Path) -> None:
    work_plan = {
        "schema_version": 1,
        "generated_at": "2026-01-01T00:00:00+00:00",
        "mode": "test",
        "summary": "Three tightly coupled implementation items.",
        "goal": "Implement a feature, scorer, and config change together.",
        "success_criteria": ["The coupled implementation passes benchmark validation."],
        "items": [
            {
                "id": "W1",
                "status": "pending",
                "objective": "Add the feature producer.",
                "target_files": ["spam_model.py"],
                "read_only_evidence": ["tests/test_spam_model.py"],
                "depends_on": [],
                "validation": ["python -m unittest discover -s tests"],
                "done_criteria": ["The producer emits the new feature."],
                "risk": "Producer must remain backward compatible.",
                "parallelizable": False,
                "budget_profile": "normal",
                "requires_budget_override": False,
                "suggested_budget_override": "",
                "context_request": {"query": "producer", "files": ["spam_model.py"], "symbols": []},
            },
            {
                "id": "W2",
                "status": "pending",
                "objective": "Use the feature in scoring.",
                "target_files": ["extra.py"],
                "read_only_evidence": ["tests/test_spam_model.py"],
                "depends_on": ["W1"],
                "validation": ["python -m unittest discover -s tests"],
                "done_criteria": ["The scorer consumes the new feature."],
                "risk": "Scoring can drift if the feature is absent.",
                "parallelizable": False,
                "budget_profile": "normal",
                "requires_budget_override": False,
                "suggested_budget_override": "",
                "context_request": {"query": "scorer", "files": ["extra.py"], "symbols": []},
            },
            {
                "id": "W3",
                "status": "pending",
                "objective": "Enable the new behavior in configuration.",
                "target_files": ["pyproject.toml"],
                "read_only_evidence": ["tests/test_spam_model.py"],
                "depends_on": ["W2"],
                "validation": ["python -m unittest discover -s tests"],
                "done_criteria": ["The default config enables the feature."],
                "risk": "Config changes should be additive.",
                "parallelizable": False,
                "budget_profile": "normal",
                "requires_budget_override": False,
                "suggested_budget_override": "",
                "context_request": {"query": "config", "files": ["pyproject.toml"], "symbols": []},
            },
        ],
        "context_requests": [],
        "risks": [],
        "approval": {"required": True, "status": "pending", "reason": "Review before editing."},
        "selected_files": ["spam_model.py", "extra.py", "pyproject.toml", "tests/test_spam_model.py"],
        "context_pack": None,
        "run_context": {},
        "budget_profiles": {},
    }
    write_json(run_dir / "code_task" / "work_plan.json", work_plan)
    write_text(run_dir / "code_task" / "work_plan.md", "# Work Plan\n")
    manifest = read_json(run_dir / "manifest.json")
    manifest["layout"]["work_plan"] = "code_task/work_plan.json"
    manifest["layout"]["work_plan_markdown"] = "code_task/work_plan.md"
    manifest["work_plan"] = {
        "status": "pending_approval",
        "mode": "test",
        "path": "code_task/work_plan.json",
        "markdown": "code_task/work_plan.md",
        "item_count": 3,
        "selected_files": work_plan["selected_files"],
        "context_pack": None,
        "approval": work_plan["approval"],
    }
    write_json(run_dir / "manifest.json", manifest)


def _write_valid_edit_proposal(run_dir: Path, path: Path | None = None) -> Path:
    proposal_path = path or run_dir / "code_task" / "meta" / "proposed_edits.json"
    write_json(
        proposal_path,
        {
            "schema_version": 1,
            "edits": [
                {
                    "path": "spam_model.py",
                    "old": TOY_PREDICT_SOURCE,
                    "new": TOY_PREDICT_WITH_PRIZE,
                    "reason": "Extend the keyword baseline while preserving the public API.",
                }
            ],
        },
    )
    return proposal_path


def _write_failing_edit_proposal(run_dir: Path) -> Path:
    proposal_path = run_dir / "code_task" / "meta" / "proposed_edits.json"
    write_json(
        proposal_path,
        {
            "schema_version": 1,
            "edits": [
                {
                    "path": "spam_model.py",
                    "old": TOY_PREDICT_SOURCE,
                    "new": (
                        "def predict(text):\n"
                        "    return 'ham'\n"
                    ),
                    "reason": "Deliberately break the classifier for failure-analysis coverage.",
                }
            ],
        },
    )
    return proposal_path


def _git(cwd: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {completed.stderr or completed.stdout}"
        )


class _FakeRepairClient:
    def __init__(self, response: dict[str, object]) -> None:
        self._response = response

    def ask_json(self, system: str, user: str, *, label: str = "") -> dict[str, object]:
        return self._response


class _FakeCodeTaskClient:
    def ask_json(self, system: str, user: str, *, label: str = "") -> dict[str, object]:
        if label.startswith("code-task-review-"):
            return {"findings": []}
        responses = keyword_code_responses()
        if label not in responses:
            raise AssertionError(f"Unexpected LLM label: {label}")
        return responses[label]

class _FailingCodeTaskClient:
    def __init__(self, error: LLMError | None = None) -> None:
        self.calls = 0
        self.error = error or LLMError("LLM response did not contain a JSON object")

    def ask_json(self, system: str, user: str, *, label: str = "") -> dict[str, object]:
        self.calls += 1
        raise self.error


def _indexed_file(index: dict[str, object], path: str) -> dict[str, object]:
    files = index.get("files", [])
    if isinstance(files, list):
        for item in files:
            if isinstance(item, dict) and item.get("path") == path:
                return item
    raise AssertionError(f"Missing indexed file: {path}")


class WorkspaceSourceBindingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "git executable is not available")
    def test_git_head_dirty_data_overlay_and_checkout_blockers(self):
        with _temporary_root() as root:
            repo = root / "repo"
            source = repo / "package"
            write_text(source / "main.py", "VALUE = 1\n")
            write_text(source / "custom_env/pyvenv.cfg", "home = base\n")
            write_text(source / "custom_env/installed.py", "DEPENDENCY = 1\n")
            write_text(source / "data/input.txt", "committed\n")
            write_text(repo / "outside.py", "OTHER = 1\n")
            for args in (("init",), ("config", "user.email", "test@example.com"),
                         ("config", "user.name", "SimpleAR Test"),
                         ("config", "core.autocrlf", "false"),
                         ("add", "."), ("commit", "-m", "baseline")):
                _git(repo, *args)
            committed = hashlib.sha256((source / "main.py").read_bytes()).hexdigest()
            write_text(source / "main.py", "VALUE = 2\n")
            write_text(source / "untracked.py", "NEW = 1\n")
            write_text(source / "data/input.txt", "declared\n" * 500_001)
            spec = WorkspaceSpec(source, root / "explicit", "git_worktree",
                                 max_file_bytes=1, data_inputs=("data",))
            before_worktrees = subprocess.run(
                ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                capture_output=True, check=True,
            ).stdout
            binding = workspace_source_binding(spec)
            expected = binding["source_snapshots"]["git_worktree"]
            self.assertEqual(binding["git"]["project_relative_path"], "package")
            self.assertTrue(binding["git"]["origin_dirty"])
            self.assertEqual(expected["main.py"], committed)
            self.assertNotIn("untracked.py", expected)
            self.assertNotIn("custom_env/installed.py", expected)
            self.assertNotIn("outside.py", expected)
            self.assertEqual(expected["data/input.txt"],
                             hashlib.sha256((source / "data/input.txt").read_bytes()).hexdigest())
            self.assertFalse(spec.task_dir.exists())
            self.assertEqual(before_worktrees, subprocess.run(
                ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
                capture_output=True, check=True,
            ).stdout)
            result = create_workspace(spec)
            self.assertEqual(expected, {row["path"]: row["sha256"]
                                       for row in build_codebase_index(result.project_root)["files"]})
            auto = workspace_source_binding(WorkspaceSpec(source, root / "auto", "auto"))
            self.assertEqual(set(auto["source_snapshots"]), {"copy"})
            self.assertNotEqual(auto["source_snapshots"]["copy"]["main.py"], committed)
            _git(repo, "config", "core.autocrlf", "true")
            blocked = workspace_source_binding(WorkspaceSpec(source, root / "blocked", "git_worktree"))
            self.assertEqual(blocked["source_snapshots"], {})
            self.assertIn("autocrlf", blocked["blocked_modes"]["git_worktree"])
            _git(repo, "config", "core.autocrlf", "false")
            write_text(repo / ".gitattributes", "*.py text\n")
            _git(repo, "add", ".gitattributes")
            _git(repo, "commit", "-m", "attributes")
            blocked = workspace_source_binding(WorkspaceSpec(source, root / "attrs", "git_worktree"))
            self.assertIn(".gitattributes", blocked["blocked_modes"]["git_worktree"])
            _git(repo, "rm", ".gitattributes")
            _git(repo, "commit", "-m", "remove attributes")
            try:
                (source / "linked.py").symlink_to("main.py")
            except OSError:
                return  # Other Git assertions still run on Windows without symlink privilege.
            _git(repo, "config", "core.symlinks", "true")
            _git(repo, "add", "package/linked.py")
            _git(repo, "commit", "-m", "tracked symlink")
            blocked = workspace_source_binding(WorkspaceSpec(source, root / "links", "git_worktree"))
            self.assertIn("symlinks", blocked["blocked_modes"]["git_worktree"])

    def test_copy_and_sparse_share_binding_selection_and_index_boundary(self):
        with _temporary_root() as root:
            source = root / "source"
            for name in ("main.py", "src/keep.py", "src/drop.py", "data/input.txt",
                         "build/generated.py", ".hidden/hidden.py", ".env",
                         ".env.example", "custom_env/pyvenv.cfg", "custom_env/lib.py",
                         "cache/notes.txt"):
                write_text(source / name, "VALUE = 1\n")
            write_text(source / "huge.py", "x" * 2_000_001)
            # More skips than CopyReport records; inventories must remain exact.
            for number in range(205):
                write_text(source / f".env.{number}", "hidden")
            for mode, ceiling in (("copy", 2_000_000), ("sparse_copy", 2_000_000),
                                  ("copy", 0), ("sparse_copy", 0)):
                with self.subTest(mode=mode, ceiling=ceiling):
                    spec = WorkspaceSpec(source, source / f"output_{mode}_{ceiling}", mode,
                                         max_file_bytes=ceiling, include=("**",),
                                         exclude=("src/drop.py",))
                    write_text(spec.task_dir / "workspace/previous.py", "old")
                    # Recovery can rebind without consuming generated output.
                    rebound = workspace_source_binding(spec)
                    self.assertFalse(any(name.startswith(spec.task_dir.name + "/")
                                         for name in rebound["source_snapshots"][mode]))
                    with self.assertRaises(FileExistsError):
                        create_workspace(spec)
                    (spec.task_dir / "workspace/previous.py").unlink()
                    from simple_ar.code_task.analysis.index import _sha256
                    with patch("simple_ar.code_task.workspace.copy._sha256", wraps=_sha256) as hashed:
                        with patch("simple_ar.code_task.workspace.copy.shutil.copy2") as copied:
                            binding = workspace_source_binding(spec)
                            copied.assert_not_called()
                    self.assertEqual(
                        {call.args[0].relative_to(source).as_posix() for call in hashed.call_args_list},
                        set(binding["source_snapshots"][mode]),
                    )
                    expected = binding["source_snapshots"][mode]
                    self.assertNotIn("custom_env/lib.py", expected)
                    self.assertNotIn(".env.example", expected)
                    self.assertNotIn("build/generated.py", expected)
                    self.assertEqual("huge.py" in expected, ceiling == 0)
                    if mode == "sparse_copy":
                        self.assertNotIn("src/drop.py", expected)
                        self.assertNotIn("data/input.txt", expected)
                    result = create_workspace(spec)
                    actual = {row["path"]: row["sha256"] for row in build_codebase_index(result.project_root)["files"]}
                    self.assertEqual(expected, actual)
                    self.assertEqual(binding, workspace_source_binding(spec))
                    self.assertGreater(result.copy_report.skipped_count, len(result.copy_report.skipped))
                    self.assertTrue((result.project_root / "custom_env").exists())
                    self.assertFalse(any(name.startswith(spec.task_dir.name + "/") for name in expected))
            changed = WorkspaceSpec(source, root / "changed", "copy")
            before = workspace_source_binding(changed)
            write_text(source / "main.py", "VALUE = 2\n")
            after = workspace_source_binding(changed)
            self.assertNotEqual(before["source_snapshots"]["copy"]["main.py"],
                                after["source_snapshots"]["copy"]["main.py"])

    def test_declared_data_bypasses_cap_without_snapshot_truncation(self):
        with _temporary_root() as root:
            source = root / "source"
            write_text(source / "main.py", "VALUE = 1\n")
            write_text(source / "data/large.txt", "data\n" * 500_001)
            spec = WorkspaceSpec(source, root / "task", "copy", max_file_bytes=1,
                                 data_inputs=("data",))
            binding = workspace_source_binding(spec)
            expected = binding["source_snapshots"]["copy"]
            self.assertEqual(set(expected), {"data/large.txt"})
            self.assertFalse(spec.task_dir.exists())
            result = create_workspace(spec)
            self.assertEqual(expected, {row["path"]: row["sha256"]
                                       for row in build_codebase_index(result.project_root)["files"]})
            for mode in ("sparse_copy", "empty"):
                invalid = WorkspaceSpec(source, root / mode, mode, data_inputs=("data",))
                with self.assertRaises(WorkspaceModeError):
                    workspace_source_binding(invalid)
                with self.assertRaises(WorkspaceModeError):
                    create_workspace(invalid)
            for name in ("../outside", "/absolute", ".env"):
                write_text(source / ".env", "secret")
                with self.assertRaises(ValueError):
                    workspace_source_binding(WorkspaceSpec(source, root / "bad", "copy", data_inputs=(name,)))

    def test_copy_quota_rejection_precedes_materialization_and_sparse_defaults(self):
        from simple_ar.code_task.workspace.copy import copy_code_workspace
        with _temporary_root() as root:
            source = root / "source"
            write_text(source / "main.py", "VALUE = 1\n")
            write_text(source / "other.py", "OTHER = 1\n")
            write_text(source / "opaque.bin", "opaque")
            for limits in ({"max_files": 1}, {"max_total_bytes": 1}):
                destination = root / "overflow"
                with self.assertRaises(ValueError):
                    copy_code_workspace(source, destination, **limits)
                self.assertFalse(destination.exists())
            spec = WorkspaceSpec(source, root / "sparse", "sparse_copy")
            binding = workspace_source_binding(spec)
            self.assertNotIn("opaque.bin", binding["source_snapshots"]["sparse_copy"])
            result = create_workspace(spec)
            self.assertEqual(binding["source_snapshots"]["sparse_copy"],
                             {row["path"]: row["sha256"] for row in build_codebase_index(result.project_root)["files"]})
            write_text(source / "custom_env/pyvenv.cfg", "home = base\n")
            write_text(source / "custom_env/installed.py", "x")
            for mode, cap, includes in (("copy", 1, ()), ("sparse_copy", 0, ("*.py",))):
                spec = WorkspaceSpec(source, root / f"marker_{mode}", mode,
                                     max_file_bytes=cap, include=includes)
                blocked = workspace_source_binding(spec)
                self.assertNotIn(mode, blocked["source_snapshots"])
                self.assertIn("pyvenv.cfg", blocked["blocked_modes"][mode])
                # Keep the existing initializer available, but do not invent an
                # approval inventory when its venv boundary differs from source.
                result = create_workspace(spec)
                self.assertTrue((result.project_root / "custom_env/installed.py").exists())
                self.assertFalse((result.project_root / "custom_env/pyvenv.cfg").exists())

    def test_empty_binding_and_non_git_auto_are_read_only(self):
        with _temporary_root() as root:
            empty = WorkspaceSpec(None, root / "empty", "empty")
            binding = workspace_source_binding(empty)
            self.assertEqual(binding["source_snapshots"], {"empty": {}})
            self.assertIsNone(binding["source_root"])
            self.assertFalse(empty.task_dir.exists())
            source = root / "source"
            write_text(source / "main.py", "VALUE = 1\n")
            spec = WorkspaceSpec(source, root / "auto", "auto")
            binding = workspace_source_binding(spec)
            self.assertEqual(binding["requested_mode"], "auto")
            self.assertEqual(set(binding["source_snapshots"]), {"copy"})
            self.assertIn("git_worktree", binding["blocked_modes"])
            self.assertFalse(spec.task_dir.exists())

    def test_links_and_reparse_boundaries_and_venv_entrypoint(self):
        with _temporary_root() as root:
            source = root / "source"
            write_text(source / "main.py", "VALUE = 1\n")
            write_text(root / "outside.py", "PRIVATE = 1\n")
            try:
                (source / "linked.py").symlink_to(root / "outside.py")
                (root / "linked_source").symlink_to(source, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Creating symlinks is unavailable: {exc}")
            spec = WorkspaceSpec(source, root / "task", "copy")
            binding = workspace_source_binding(spec)
            self.assertNotIn("linked.py", binding["source_snapshots"]["copy"])
            with self.assertRaises(ValueError):
                workspace_source_binding(WorkspaceSpec(root / "linked_source", root / "bad", "copy"))
            with self.assertRaises(ValueError):
                workspace_source_binding(WorkspaceSpec(source, root / "bad", "copy", data_inputs=("linked.py",)))
            result = create_workspace(spec)
            self.assertFalse((result.project_root / "linked.py").exists())
            # Linux venv entrypoints are commonly symlinks to the base interpreter.
            entrypoint = source / ".venv/bin/python"
            entrypoint.parent.mkdir(parents=True)
            entrypoint.symlink_to(Path(sys.executable))
            self.assertEqual(_source_venv_python(source), Path(os.path.abspath(entrypoint)))
            self.assertNotEqual(_source_venv_python(source), entrypoint.resolve())
            if os.name != 'nt':
                # Prove interpreter identity, not only spelling of a fake link.
                subprocess.run([sys.executable, '-m', 'venv', '--without-pip', '--symlinks', str(source / '.venv')],
                    check=True, capture_output=True, text=True, timeout=30)
                selected = _source_venv_python(source)
                actual = subprocess.run([str(selected), '-c', 'import sys;print(sys.prefix)'],
                    check=True, capture_output=True, text=True, timeout=5)
                self.assertEqual(Path(actual.stdout.strip()), source / '.venv')
            # Exercise the Windows reparse predicate on every platform too.
            from simple_ar.code_task.workspace.copy import _is_link_or_reparse
            with patch("simple_ar.code_task.workspace.copy.stat.FILE_ATTRIBUTE_REPARSE_POINT", 1024, create=True):
                fake = Mock()
                fake.is_symlink.return_value = False
                fake.lstat.return_value.st_file_attributes = 1024
                self.assertTrue(_is_link_or_reparse(fake))


class GenerationAuthorizationTests(unittest.TestCase):
    """Exercise publication authority through real staging; only the model is fake."""

    def test_implementation_preserves_bound_originals_without_manufactured_edits(self):
        from simple_ar.code_task.generation.writer import write_implementation_unit
        from simple_ar.tools.materials import freeze_work_context

        supplied = {'user_requests': ['Preserve dependencies'], 'review_observations': [
            {'record': 'accepted-check', 'result': {'findings': ['Keep the original API']}}]}
        provider = Mock(return_value=supplied)
        window = freeze_work_context(provider, ['/mounted/helper.py'])
        provider.assert_called_once_with(['/mounted/helper.py'])
        supplied['review_observations'][0]['result']['findings'].append('Later feedback')
        self.assertEqual(window['work_context']['review_observations'][0]['result']['findings'],
            ['Keep the original API'])
        self.assertNotIn('text', window)  # Feedback cannot masquerade as a read source window.
        with self.assertRaisesRegex(ValueError, '4 MiB'):
            freeze_work_context(lambda targets: {'user_requests': ['界' * (4 * 1024 * 1024 // 3)]}, [])

        # Both explicit files and unfamiliar-project selections reach the writer
        # as a plan; feedback instead merges its complete bound inventory.
        for planned in (True, False):
            with self.subTest(planned=planned), _temporary_root() as root:
                originals = {'pkg/__init__.py': '', 'helper.py': '# keep CRLF\r\nVALUE = 1\r\n'}
                response = {'files': [{'path': path, 'replacements': []} for path in originals]}
                with patch('simple_ar.code_task.generation.writer.run_json_agent_step', return_value=response) as generate:
                    result = write_implementation_unit(project_dir=root / 'candidate', goal='Preserve dependencies',
                        files=[{'path': path} for path in originals] if planned else None,
                        source_windows=[window], original_files={path: {'text': text} for path, text in originals.items()},
                        client=Mock(), artifact_dir=root / 'records', retry_attempts=1,
                        validate_destination=lambda path: root / 'candidate' / path)
                prompt = json.loads(generate.call_args.kwargs['prompt'])
                self.assertEqual(prompt['source_windows'], [window])
                self.assertEqual(set(prompt['original_files']), set(originals))
                self.assertIn('not read source text or new permissions', generate.call_args.kwargs['system'])
                for row in result['files']:
                    raw = originals[row['path']].encode('utf-8')
                    self.assertEqual((root / 'candidate' / row['path']).read_bytes(), raw)
                    self.assertEqual(row['sha256'], hashlib.sha256(raw).hexdigest())

    def test_implementation_rejects_aliases_before_staging_in_every_mode(self):
        from pathlib import PureWindowsPath
        from simple_ar.code_task.generation.writer import write_implementation_unit

        # Simulate Windows lexical identity on Linux; this is not a Windows
        # filesystem integration test. Feedback must include omitted originals.
        for mode in ('planned', 'direct', 'feedback'):
            with self.subTest(mode=mode), _temporary_root() as root:
                destination = root / 'candidate'
                write_text(destination / 'previous.py', 'ORIGINAL = 1\n')
                before = _project_bytes(destination)
                response = {'files': [{'path': 'Helper.py', 'content': 'VALUE = 2\n'}]}
                if mode != 'feedback':
                    response['files'].insert(0, {'path': 'helper.py', 'content': 'VALUE = 1\n'})
                with patch('simple_ar.code_task.generation.writer.run_json_agent_step', return_value=response), \
                        patch('simple_ar.code_task.generation.writer.Path', PureWindowsPath), \
                        self.assertRaisesRegex(ValueError, 'alias on this filesystem'):
                    write_implementation_unit(project_dir=destination, goal='Prepare related files',
                        files=[{'path': row['path']} for row in response['files']] if mode == 'planned' else None,
                        source_windows=[], original_files={'helper.py': {'text': 'VALUE = 1\n'}}
                        if mode == 'feedback' else {}, client=Mock(), artifact_dir=root / 'records',
                        retry_attempts=1, validate_destination=lambda path: destination / path)
                self.assertEqual(_project_bytes(destination), before)
                self.assertFalse(list(root.glob('.candidate.candidate-*')))
                self.assertFalse(list(root.glob('.candidate.previous-*')))

    def test_implementation_native_case_identity_and_new_empty_file_boundary(self):
        from simple_ar.code_task.generation.writer import write_implementation_unit

        for planned in (True, False):
            for empty in (True, False):
                with self.subTest(planned=planned, empty=empty), _temporary_root() as root:
                    response = {'files': [{'path': 'helper.py', 'content': 'VALUE = 1\n'},
                        {'path': 'Helper.py' if not empty else 'pkg/__init__.py',
                         'content': '' if empty else 'VALUE = 2\n'}]}
                    def publish():
                        return write_implementation_unit(project_dir=root / 'candidate', goal='Prepare sources',
                            files=[{'path': row['path']} for row in response['files']] if planned else None,
                            source_windows=[], client=Mock(), artifact_dir=root / 'records', retry_attempts=1,
                            validate_destination=lambda path: root / 'candidate' / path)
                    with patch('simple_ar.code_task.generation.writer.run_json_agent_step', return_value=response):
                        if empty or os.name == 'nt':
                            with self.assertRaisesRegex(ValueError, 'empty' if empty else 'alias on this filesystem'):
                                publish()
                            self.assertFalse((root / 'candidate').exists())
                        else:
                            result = publish()
                            self.assertEqual(len(result['files']), 2)
                            for row in response['files']:
                                self.assertEqual((root / 'candidate' / row['path']).read_bytes(), row['content'].encode())

    @staticmethod
    def _client(contents):
        client = Mock()

        def respond(_system, prompt, *, label, **_kwargs):
            if label == 'greenfield-architecture':
                return {'objective': 'Print score.', 'architecture_summary': 'Bounded project.',
                        'files': [{'path': path, 'purpose': 'Implement score.'} for path in contents]}
            if not label.startswith('greenfield-unit-'):
                raise AssertionError(f'Unexpected model call: {label}')
            request = json.loads(prompt)
            return {'files': [{'path': row['path'], 'content': contents[row['path']]}
                              for row in request['planned_files']]}

        client.ask_json.side_effect = respond
        return client

    @staticmethod
    def _write(workspace, contents, memory, **policy):
        from functools import partial
        from simple_ar.code_task.editing.patching import validate_generated_project
        from simple_ar.code_task.generation.writer import write_generated_project

        # Omission must exercise the old public writer signature too.
        callback = {} if policy.pop('legacy', False) else {'validate_candidate': partial(
            validate_generated_project, workspace, prefix='generated_project',
            allowed_patterns=policy.pop('allowed_patterns', ('generated_project/**',)),
            protected_patterns=policy.pop('protected_patterns', ()), **policy)}
        return write_generated_project(project_dir=workspace / 'generated_project',
            architecture_plan={'files': [{'path': path, 'purpose': 'Implement score.'}
                                         for path in contents]},
            result_schema={'primary_metric': 'score'}, contract={}, memory=memory,
            client=GenerationAuthorizationTests._client(contents), files_per_batch=1, **callback)

    def _retained(self, workspace, expected):
        candidates = list(workspace.glob('.generated_project.candidate-*'))
        self.assertEqual(len(candidates), 1)
        for path, content in expected.items():
            self.assertEqual(read_text(candidates[0] / path), content)
        self.assertFalse(list(workspace.glob('.generated_project.previous-*')))
        return candidates[0]

    def test_writer_scope_checks_complete_candidate_and_preserves_previous_state(self):
        cases = (
            ('allowed', {}, {}, None),
            ('narrow', {'allowed_patterns': ('generated_project/main.py',)}, {}, 'outside_allowed_patterns'),
            ('protected_source', {'protected_patterns': ('generated_project/helper.py',)},
             {'helper.py': 'ORIGINAL = 7\n', 'main.py': 'ORIGINAL = 8\n'}, 'protected_path'),
            ('planned_file', {'protected_patterns': ('generated_project/helper.py',)}, {}, 'protected_path'),
            ('package_init', {'protected_patterns': ('generated_project/pkg/__init__.py',)}, {}, 'protected_path'),
            ('required_main', {'allowed_patterns': ('generated_project/helper.py',)}, {}, 'outside_allowed_patterns'),
            ('legacy', {'legacy': True}, {'old.py': 'ORIGINAL = 7\n'}, None),
        )
        for name, policy, previous, rejection in cases:
            with self.subTest(name=name), _temporary_root() as root:
                workspace = root / 'workspace'
                project = workspace / 'generated_project'
                contents = {'main.py': "print('score: 0.5')\n", 'helper.py': 'VALUE = 2\n'}
                if name == 'package_init':
                    contents['pkg/__init__.py'] = 'VALUE = 3\n'
                if name == 'required_main':
                    del contents['main.py']  # Writer's deterministic entrypoint is also guarded.
                for path, content in previous.items():
                    write_text(project / path, content)
                before = _project_bytes(project)
                memory = {'file_summaries': [{'path': 'original.py'}], 'accepted_decisions': ['keep']}
                saved_memory = json.dumps(memory, sort_keys=True)
                if rejection:
                    with self.assertRaisesRegex(PatchValidationError, rejection) as caught:
                        self._write(workspace, contents, memory, **policy)
                    self.assertEqual(_project_bytes(project), before)
                    self.assertEqual(project.exists(), bool(previous))
                    self.assertEqual(json.dumps(memory, sort_keys=True), saved_memory)
                    candidate = self._retained(workspace, contents)
                    self.assertTrue((candidate / 'main.py').is_file())
                    self.assertIn(str(candidate), '\n'.join(caught.exception.__notes__))
                else:
                    result = self._write(workspace, contents, memory, **policy)
                    self.assertEqual(_project_bytes(project),
                                     {Path(path): text.encode() for path, text in contents.items()})
                    self.assertEqual(result['project_dir'], str(project))
                    self.assertNotEqual(json.dumps(memory, sort_keys=True), saved_memory)
                    self.assertFalse(list(workspace.glob('.generated_project.candidate-*')))
                    if previous:
                        backups = list(workspace.glob('.generated_project.previous-*'))
                        self.assertEqual(len(backups), 1)
                        self.assertEqual(_project_bytes(backups[0]), before)

    def test_writer_budget_is_cumulative_and_approval_cannot_bypass_absolute_cap(self):
        from simple_ar.code_task.editing.budget import edit_budget_for_profile
        from simple_ar.code_task.editing.patching import EditBudgetApprovalRequired

        cases = (
            ('fits', 'normal', {}, False, False),
            ('whole_project_files', 'normal', {'max_files': 1}, False, True),
            ('whole_project_chars', 'normal', {'max_total_edit_chars': 25}, False, True),
            ('large_requires_approval', 'large', {}, False, True),
            ('large_approved', 'large', {}, True, False),
            ('normal_overflow_approved', 'normal', {'max_files': 1}, True, False),
            ('absolute_even_with_approval', 'absolute', {'max_new_chars': 100_000}, True, True),
        )
        for name, profile, overrides, approved, rejects in cases:
            with self.subTest(name=name), _temporary_root() as root:
                contents = {'main.py': "print('score: 0.5')\n", 'helper.py': 'VALUE = 2\n'}
                if name == 'absolute_even_with_approval':
                    contents['helper.py'] = "VALUE = '" + 'x' * 32_001 + "'\n"
                memory = {'accepted_decisions': ['preserve']}
                saved_memory = json.dumps(memory, sort_keys=True)
                policy = dict(budget=edit_budget_for_profile(profile, overrides=overrides),
                              allow_large_edits=approved)
                workspace = root / 'workspace'
                if rejects:
                    with self.assertRaises(EditBudgetApprovalRequired):
                        self._write(workspace, contents, memory, **policy)
                    self.assertFalse((workspace / 'generated_project').exists())
                    self.assertEqual(json.dumps(memory, sort_keys=True), saved_memory)
                    self._retained(workspace, contents)
                else:
                    self._write(workspace, contents, memory, **policy)
                    self.assertEqual(len(memory['generated_batches']), 2)
                    self.assertEqual(_project_bytes(workspace / 'generated_project'),
                                     {Path(path): text.encode() for path, text in contents.items()})

    def test_public_greenfield_explicit_scope_consumes_publication_guard(self):
        from simple_ar.code_task.editing.patching import EditBudgetApprovalRequired
        from simple_ar.code_task.generation.greenfield import generate_greenfield_code_task

        with _temporary_root() as root:
            write_text(root / 'task.md', 'Generate a bounded project printing score.')
            run = root / 'run'
            _initialize_greenfield(run_dir=run, task_file=root / 'task.md', primary_metric='score',
                                  edit_scope_allowed_patterns=('generated_project/**',))
            workspace = run / 'code_task/workspace'
            contents = {'main.py': "print('score: 0.5')\n", 'helper.py': 'VALUE = 2\n'}
            # Existing output and durable memory survive a real late publication failure.
            for path in contents:
                write_text(workspace / 'generated_project' / path, 'ORIGINAL = 1\n')
            before = _project_bytes(workspace / 'generated_project')
            memory_path = run / 'code_task/memory/implementation_memory.json'
            write_json(memory_path, {'accepted_decisions': ['preserve']})
            old_memory = memory_path.read_bytes()
            client = self._client(contents)
            with patch('simple_ar.code_task.generation.greenfield._llm_client', return_value=client), \
                    self.assertRaises(EditBudgetApprovalRequired):
                generate_greenfield_code_task(run, llm_client=client, max_files=2,
                    budget_profile='normal', edit_budget_overrides={'max_files': 1})
            self.assertTrue(any(call.kwargs['label'].startswith('greenfield-unit-')
                                for call in client.ask_json.call_args_list))
            self.assertEqual(_project_bytes(workspace / 'generated_project'), before)
            self.assertEqual(memory_path.read_bytes(), old_memory)
            self._retained(workspace, contents)
            self.assertFalse((run / 'code_task/meta/code_artifacts.json').exists())

    def test_public_greenfield_rejects_forbidden_plan_before_content_generation(self):
        from simple_ar.code_task.generation.greenfield import generate_greenfield_code_task

        for forbidden in ('helper.py', 'pkg/__init__.py', 'main.py'):
            with self.subTest(forbidden=forbidden), _temporary_root() as root:
                write_text(root / 'task.md', 'Generate a bounded project printing score.')
                run = root / 'run'
                _initialize_greenfield(run_dir=run, task_file=root / 'task.md', primary_metric='score',
                    edit_scope_allowed_patterns=('generated_project/**',),
                    edit_scope_protected_patterns=(f'generated_project/{forbidden}',))
                contents = {'main.py': "print('score: 0.5')\n", forbidden: 'VALUE = 2\n'}
                client = self._client(contents)
                with patch('simple_ar.code_task.generation.greenfield._llm_client', return_value=client), \
                        self.assertRaisesRegex(PatchValidationError, 'protected_path'):
                    generate_greenfield_code_task(run, llm_client=client, max_files=2)
                self.assertFalse(any(call.kwargs['label'].startswith('greenfield-unit-')
                                     for call in client.ask_json.call_args_list))
                self.assertFalse((run / 'code_task/workspace/generated_project').exists())

    def test_exact_greenfield_generation_feedback_and_api_guard_use_real_owners(self):
        for mode, failed_baseline in (('empty', False), ('copy', False), ('sparse_copy', False), ('copy', True)):
            with self.subTest(workspace=mode, failed_baseline=failed_baseline):
                self._exact_greenfield_generation_feedback(mode, failed_baseline=failed_baseline)

    def _exact_greenfield_generation_feedback(self, mode, *, failed_baseline):
        from simple_ar.app.tool_application import ToolApplication
        from simple_ar.tools.implementation import ImplementationToolGateway
        from simple_ar.tools.scientific import ScientificToolGateway
        from simple_ar.tools.specs import ToolResult
        from simple_ar.tools.workspace import WorkspaceToolGateway
        from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext, CapabilityRegistry, CapabilityResult
        from simple_ar.core.session import SessionController, BudgetState
        from simple_ar.code_task.runtime.state import load_code_task_manifest
        from simple_ar.research.preparation import run_preparation_capability
        with _temporary_root() as root, patch('simple_ar.research.preparation.run_preparation_capability',
                wraps=run_preparation_capability) as prepare:
            ledger = BudgetLedger({'llm_requests': 30, 'total_tokens': 50000,
                                   'process_invocations': 8, 'process_wall_seconds': 120})
            (root / 'attempts').mkdir()
            source = root / 'source'
            if mode != 'empty':
                write_text(source / 'baseline.py', 'raise SystemExit(1)\n' if failed_baseline else 'print("score: 0.5")\n')
                write_text(source / 'notes.txt', 'Read-only scaffold material.\n')
            original_source = _project_bytes(source)
            workspace = WorkspaceToolGateway(root, {'records': root / 'attempts',
                **({'source': source} if mode != 'empty' else {})}, ledger)
            fake = Mock()
            fake.effective_output_limit.return_value = None
            fake._budget_ledger = ledger
            broken = False
            labels = []
            contents = {'main.py': 'from model import predict\nprint("score:", int(predict("win") == "spam"))\n',
                        'model.py': TOY_PREDICT_SOURCE}
            responses = json.loads(json.dumps(keyword_code_responses()).replace(
                'tests/test_spam_model.py', 'generated_project/main.py').replace(
                'spam_model.py', 'generated_project/model.py'))
            def respond(_system, prompt, *, label='', **_kwargs):
                labels.append(label)
                call = f'fake-model-{len(labels)}'
                ledger.reserve(call, {'llm_requests': 1, 'total_tokens': 100},
                    session_id='same-task', attempt_id=identity, purpose=label)
                ledger.settle(call, {'llm_requests': 1, 'total_tokens': 100})
                if label == 'greenfield-architecture':
                    return {'objective': 'Create a local classifier.', 'files': [
                        {'path': 'main.py', 'purpose': 'CLI', 'entrypoint': True, 'dependencies': ['model.py'], 'public_api': []},
                        {'path': 'model.py', 'purpose': 'Classify text', 'public_api': ['predict(text)']}]}
                if label.startswith('greenfield-unit-'):
                    return {'files': [{'path': row['path'], 'content': contents[row['path']]}
                                      for row in json.loads(prompt)['planned_files']]}
                if label in responses:
                    if broken and label == 'code-task-propose-edits':
                        return {**responses[label], 'edits': [{'path': 'generated_project/model.py',
                            'old': TOY_PREDICT_WITH_PRIZE, 'new': 'def replacement(text):\n    return "spam"\n',
                            'reason': 'Break API to prove whole-project check'}]}
                    return responses[label]
                if 'review' in label:
                    return {'status': 'passed', 'summary': 'Mock model review only.', 'findings': []}
                raise AssertionError(f'Unexpected model label: {label}')
            fake.ask_json.side_effect = respond
            contract = {'project_path': '' if mode == 'empty' else 'source', 'goal': 'Create a local text classifier preserving predict(text).',
                'allowed_patterns': ['generated_project/**'], 'validation_command': ['python', '-m', 'compileall', '-q', 'generated_project'],
                'timeout_sec': 10,
                'preparation': {'kind': 'greenfield', 'workspace_mode': mode, 'max_file_bytes': 0, 'max_repairs': 0,
                    **({'workspace_include': ['baseline.py']} if mode == 'sparse_copy' else {})}}
            formal = {'command': ['python', 'generated_project/main.py'], 'cwd': str(root if mode == 'empty' else source),
                'timeout_sec': 6, 'result_schema': {'primary_metric': 'score', 'required_metrics': ['score']}}
            measurement = formal if mode == 'empty' else {'conditions': [
                {'id': 'baseline', 'role': 'baseline', 'seed': None, 'source': 'preparation', 'before_implementation': True,
                    'execution': {**formal, 'command': ['python', 'baseline.py']}},
                {'id': 'candidate', 'role': 'candidate', 'seed': None, 'source': 'implementation', 'execution': formal}]}
            owner = ImplementationToolGateway(root, workspace, ledger, 'fixture', 'same-task',
                exact=contract, client_factory=lambda _: fake, measurement=measurement)
            science = ScientificToolGateway(root, workspace, ledger, 'same-task', implementation=owner,
                execution_contract=measurement)
            measured = []
            owner.measurement_inputs = lambda origin, required: science.implementation_inputs(measured, origin, required=required)
            registry = CapabilityRegistry()
            def native(*, context, tool_name, arguments, approval):
                gateway = owner if tool_name in owner.context_tools else science
                result = gateway.execute_tool(tool_name, context, arguments, approval=approval)
                context.store.write_json('native_result.json', result.model_dump(mode='json'))
                if tool_name == 'measure_experiment' and result.data.get('result_ref'):
                    measured.append(result.data['result_ref'])
                return CapabilityResult(status='completed', artifacts=ToolApplication._native_artifacts(
                    context.store, result.model_dump(mode='json'), name=tool_name))
            registry.register('native_tool', native)
            controller = SessionController.create(root, session_id='same-task', topic=contract['goal'],
                registry=registry, budget=BudgetState(max_attempts=10))
            def invoke(tool_name, arguments, identity):
                gateway = owner if tool_name in owner.context_tools else science
                token = {'token': identity, 'binding': gateway.approval_binding(arguments, identity=identity)}
                controller.execute_attempt('native_tool', attempt_id=identity, tool_name=tool_name,
                    arguments=arguments, approval=token)
                store = ArtifactStore(root / 'attempts' / identity)
                self.assertEqual(store.read_capability_result().status, 'completed', store.read_json('capability_result.json'))
                result = store.read_json('native_result.json')
                return result, token
            prepared_locator = 'records/prepared/execution.json'
            if mode != 'empty':
                identity = 'prepared'
                result, _ = invoke('prepare_code_task', {}, identity)
                self.assertEqual(result['status'], 'ok', result)
                self.assertEqual(prepare.call_count, 1)
                fake.ask_json.assert_not_called()
                prepared = owner.prepared_source(prepared_locator, identity='baseline')
                prepared_cwd = Path(prepared['cwd'])
                self.assertEqual((prepared_cwd / 'baseline.py').read_bytes(), original_source[Path('baseline.py')])
                self.assertEqual((prepared_cwd / 'notes.txt').exists(), mode == 'copy')
                retained_preparation = _project_bytes(root / 'attempts/prepared')
                def reject_generation(reason):
                    calls, model_calls, budget = prepare.call_count, fake.ask_json.call_count, ledger.to_dict()
                    with self.assertRaisesRegex(ValueError, reason):
                        owner.approval_binding({'prepared_result': prepared_locator}, identity='denied')
                    # A direct implementation without its required ancestor must
                    # also stop before real preparation or any generation call.
                    token = {'token': 'denied', 'binding': owner.approval_binding({}, identity='denied')}
                    with self.assertRaisesRegex(ValueError, 'required prior measurements'):
                        owner.execute(CapabilityContext(ArtifactStore(root / 'attempts/denied'),
                            AttemptManifest('denied')), {}, approval=token)
                    self.assertEqual(prepare.call_count, calls)
                    self.assertEqual(fake.ask_json.call_count, model_calls)
                    self.assertEqual(ledger.to_dict(), budget)
                    self.assertFalse((workspace.workspace / 'code_tasks/denied').exists())
                    self.assertEqual(_project_bytes(source), original_source)
                reject_generation('required prepared-source measurements')
                identity = 'baseline'
                baseline_result, _ = invoke('measure_experiment', {**formal, 'command': ['python', 'baseline.py'],
                    'cwd': prepared['cwd'], 'condition_id': 'baseline', 'preparation_ref': prepared_locator}, identity)
                self.assertEqual(baseline_result['status'], 'ok', baseline_result)
                baseline = read_json(root / 'attempts/baseline/results.json')
                self.assertEqual(len(measured), 1)
                if failed_baseline:
                    self.assertEqual(baseline['status'], 'failed')
                    self.assertEqual(baseline['returncode'], 1)
                    reject_generation('required prepared-source measurements')
                    fake.ask_json.assert_not_called()
                    self.assertEqual(prepare.call_count, 1)
                    self.assertEqual(_project_bytes(root / 'attempts/prepared'), retained_preparation)
                    self.assertEqual(len([row for row in ledger.entries if row.purpose == 'local_process']), 1)
                    return
                self.assertEqual(baseline['status'], 'passed')
                self.assertEqual(baseline['metrics']['score'], 0.5)
                self.assertEqual(prepare.call_count, 1)
                fake.ask_json.assert_not_called()
                baseline_records = _project_bytes(root / 'attempts/baseline')
            prior = ''
            for number in range(1, 4):
                broken = number == 3
                identity = f'native-tool-{number:03d}'
                work = ({'prepared_result': prepared_locator} if mode != 'empty' else {}) if not prior else {
                    'instruction': 'Handle prize while preserving public API.', 'prior_result': prior}
                store = ArtifactStore(root / 'attempts' / identity)
                original = {}
                if number > 1:
                    previous_run = workspace.workspace / 'code_tasks' / f'native-tool-{number - 1:03d}'
                    previous_store = root / 'attempts' / f'native-tool-{number - 1:03d}'
                    original = {path: path.read_bytes() for folder in (previous_run, previous_store)
                                for path in folder.rglob('*') if path.is_file()}
                with patch('simple_ar.integrations.llm.LLMClient.for_task', return_value=fake), \
                        patch('simple_ar.code_task.generation.greenfield._llm_client', return_value=fake):
                    observed, approval = invoke('implement_code_task', work, identity)
                result = ToolResult.model_validate(observed)
                context = CapabilityContext(store, store.read_attempt_manifest())
                self.assertEqual({path: path.read_bytes() for path in original}, original)
                run = workspace.workspace / 'code_tasks' / identity
                manifest = load_code_task_manifest(run)
                self.assertEqual(manifest['code_task']['kind'], 'greenfield')
                self.assertEqual(len([name for name in labels if name == 'greenfield-architecture']), 1)
                if number < 3:
                    self.assertEqual(result.status, 'ok', result.error)
                    self.assertIn('generation_architecture_plan', result.data['artifact_refs'])
                    self.assertEqual(result.data['validation']['status'], 'passed')
                    self.assertEqual(result.data['validation']['metrics'], {})  # Compile/API validation is not a measured score.
                    from simple_ar.code_task.generation.greenfield import result_schema_from_manifest, _benchmark_command
                    self.assertEqual(result_schema_from_manifest(manifest)['primary_metric'], 'score')
                    self.assertIn('generated_project/main.py', _benchmark_command(manifest))
                    if mode != 'empty':
                        facts = approval['binding']['measurement_inputs']['observations']
                        self.assertEqual([fact['condition_id'] for fact in facts], ['baseline'])
                        self.assertEqual(facts[0]['metrics']['score'], 0.5)
                        self.assertIn('Recorded prior measurement evidence', store.read_json('inputs/brief.json')['request_text'])
                    if number == 1:
                        # A settled validator with no outer domain result must
                        # reuse committed generation, never call its model again.
                        (store.root / 'implementation.json').unlink()
                        before = ledger.to_dict()
                        with patch.object(fake, 'ask_json', side_effect=AssertionError('No model replay')), \
                                patch('simple_ar.integrations.llm.LLMClient.for_task', return_value=fake):
                            recovered = owner.execute(context, work, approval=approval)
                        self.assertEqual(recovered.status, 'ok', recovered.error)
                        self.assertEqual(recovered.data, result.data)
                        self.assertEqual(ledger.to_dict(), before)
                    prior = f'records/{identity}/implementation.json'
                    if number == 2:
                        self.assertEqual(manifest['implementation']['mode'], 'controlled_patch')
                        self.assertIn('prize', (code_task_paths(run).workspace_dir / 'generated_project/model.py').read_text())
                        self.assertEqual(approval['binding']['prior_source']['record'], 'records/native-tool-001/implementation.json')
                    measurement_identity = 'formal-' + identity
                    source_facts = owner.measurement_source(prior, identity=measurement_identity)
                    if mode != 'empty':
                        self.assertEqual(source_facts['preparation_origin']['path'], 'attempts/prepared/execution.json')
                    measured_result, _ = invoke('measure_experiment', {**formal, 'cwd': source_facts['cwd'],
                        'implementation_ref': prior, **({'condition_id': 'candidate'} if mode != 'empty' else {})}, measurement_identity)
                    self.assertEqual(measured_result['status'], 'ok', measured_result)
                    candidate = read_json(root / 'attempts' / measurement_identity / 'results.json')
                    self.assertEqual(candidate['status'], 'passed')
                    self.assertEqual(candidate['metrics']['score'], 1)
                    validation = read_json(store.resolve(result.data['artifact_refs']['validation_report']['path']))
                    self.assertNotEqual(candidate['measurement']['measurement_id'], validation['process']['invocation_id'])
                else:
                    self.assertEqual(result.status, 'blocked')
                    self.assertIsNone(result.data['validation'])
                    self.assertTrue(any(row['category'] in {'missing_local_api', 'interface_contract'}
                        for row in read_json(code_task_paths(run).meta_dir / 'review_report.json')['findings']))
                self.assertEqual(_project_bytes(source), original_source)
                if mode != 'empty':
                    self.assertEqual(_project_bytes(root / 'attempts/prepared'), retained_preparation)
                    self.assertEqual(_project_bytes(root / 'attempts/baseline'), baseline_records)
                    self.assertEqual((prepared_cwd / 'baseline.py').read_bytes(), original_source[Path('baseline.py')])
            self.assertEqual(prepare.call_count, 3 if mode == 'empty' else 4)
            self.assertEqual(len([row for row in ledger.entries if row.purpose == 'local_process']),
                4 if mode == 'empty' else 5)
            self.assertEqual(len(measured), 2 if mode == 'empty' else 3)


if __name__ == "__main__":
    unittest.main()
