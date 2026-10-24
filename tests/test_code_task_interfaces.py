from __future__ import annotations

import tempfile
import json
import unittest
import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch

from simple_ar.code_task.analysis.interfaces import (
    dependency_context,
    find_local_api_mismatches,
    order_file_specs,
    snippet_api_contract,
)
from simple_ar.code_task.analysis.entrypoints import analyze_entrypoint_debuggability
from simple_ar.code_task.analysis.resource_static import analyze_resource_risks
from simple_ar.code_task.generation.common import safe_relative_path, string_list
from simple_ar.code_task.generation.review import (
    GREENFIELD_REVIEW_CONTRACT_VERSION,
    is_current_greenfield_review,
    review_generated_project,
)
from simple_ar.code_task.generation.task_contract import contract_prompt_view
from simple_ar.code_task.generation.writer import _response_self_reports_defect, write_generated_project
from simple_ar.code_task import initialize_code_task, review_code_task_changes, build_code_task_context_pack
from simple_ar.code_task.analysis.index import build_codebase_index
from simple_ar.code_task.analysis.repo_map import build_repo_map
from simple_ar.code_task.execution.environment import resolve_code_task_command
from simple_ar.core.artifacts import read_json, read_jsonl, write_json, write_text


def project_bytes(project: Path) -> dict:
    return {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}


def generation_client(response: dict) -> Mock:
    return Mock(ask_json=Mock(return_value=response), _budget_ledger=None)


def generated_review(project: Path, rows: list[dict], **overrides) -> dict:
    inputs = dict(project_dir=project, code_artifacts={"generated_files": rows},
        result_schema={"primary_metric": "score", "required_metrics": ["score"]},
        resource_plan={"max_files": 4, "max_generated_lines": 200}, use_llm=False)
    return review_generated_project(**(inputs | overrides))


def generate_project(project_dir: Path, **overrides) -> dict:
    """Use a fresh one-entrypoint request; scenarios state only their differences."""
    inputs = dict(project_dir=project_dir,
        architecture_plan={"files": [{"path": "main.py", "kind": "source", "entrypoint": True}]},
        result_schema={}, contract={}, memory={})
    return write_generated_project(**(inputs | overrides))


class CodeTaskInterfaceTests(unittest.TestCase):
    def test_source_loader_attributes_are_not_missing_local_apis_but_typos_still_are(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / 'pkg').mkdir()
            (project / 'pkg/__init__.py').write_text('VALUE = 1\n')
            (project / 'pkg/implementation.py').write_text('VALUE = 2\n')
            (project / 'consumer.py').write_text(
                'import pkg as package\nimport pkg.implementation as local\n'
                'from pkg.implementation import __file__\n'
                'values = (local.__file__, local.__name__, local.__doc__, local.__package__, '
                'local.__loader__, local.__spec__, local.__cached__, local.__builtins__, '
                'local.__dict__, package.__path__)\n'
                'unknown = local.__invented_loader_flag__\n'
                'bad_package = local.__path__\n')
            findings = find_local_api_mismatches(project)
            self.assertEqual({row['missing_symbol'] for row in findings},
                             {'__invented_loader_flag__', '__path__'})

    def test_external_python_preserves_virtualenv_symlink_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "env" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
            executable.parent.mkdir(parents=True)
            try:
                executable.symlink_to(sys.executable)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"Symlink creation unavailable: {exc}")
            command = resolve_code_task_command(
                ["python", "-V"], env_mode="external", python_executable=executable,
            )
            self.assertEqual(command, [os.path.abspath(executable), "-V"])
            self.assertNotEqual(command[0], str(executable.resolve()))

    def test_generation_common_helpers_normalize_paths_and_lists(self) -> None:
        self.assertEqual(safe_relative_path("pkg\\runner.py"), "pkg/runner.py")
        self.assertEqual(safe_relative_path("../escape.py"), "")
        from simple_ar.code_task.generation.file_specs import normalize_plan_path
        from simple_ar.code_task.analysis.interfaces import _safe_path
        from simple_ar.code_task.generation.agent_backend import _safe_project_path, _is_deliverable_project_file
        from simple_ar.code_task.generation.review import _is_reviewable_generated_path
        for value in ("/absolute.py", "C:/escape.py", "C:escape.py", "\\\\server\\share\\file.py",
                      "pkg/../escape.py", "pkg/file.py:stream", ".", "bad\0.py"):
            with self.subTest(value=value):
                for normalize in (safe_relative_path, normalize_plan_path, _safe_path, _safe_project_path):
                    self.assertEqual(normalize(value), "")
                self.assertFalse(_is_deliverable_project_file(value))
                self.assertFalse(_is_reviewable_generated_path(value))
        self.assertEqual(normalize_plan_path("generated_project/pkg/runner.py"), "pkg/runner.py")
        for value, expected in (("pkg/runner.py", True), (".env.example", True),
                                (".env", False), ("pkg/__pycache__/runner.pyc", False),
                                ("review.md", False), ("pkg/debug.log", False)):
            with self.subTest(value=value):
                self.assertEqual(_is_deliverable_project_file(value), expected)
                self.assertEqual(_is_reviewable_generated_path(value), expected)
        self.assertEqual(string_list([" a ", "", 3], limit=2), ["a", "3"])

    def test_file_specs_are_ordered_dependencies_first(self) -> None:
        files = [
            {"path": "main.py", "dependencies": ["pkg/runner.py"]},
            {"path": "pkg/runner.py", "dependencies": ["pkg/data.py"]},
            {"path": "pkg/data.py", "dependencies": []},
        ]

        ordered = order_file_specs(files)

        self.assertEqual([row["path"] for row in ordered], ["pkg/data.py", "pkg/runner.py", "main.py"])

    def test_dependency_context_exposes_actual_generated_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = ("# coding: latin-1\n# café\n"
                "def load_text_classification_splits(config_path=None):\n    return [], {}\n"
                + "# padding\n" * 20 + "def later_api():\n    return 1\n")
            (project / "pkg").mkdir()
            (project / "pkg/data.py").write_bytes(source.encode("latin-1"))
            from simple_ar.code_task.analysis import source_context as owner
            with patch.object(owner, "read_code_task_text", wraps=owner.read_code_task_text) as read:
                context = dependency_context(project,
                    {"path": "pkg/runner.py", "dependencies": ["pkg/data.py"]}, max_source_chars=30)
            self.assertEqual(read.call_count, 1)
            dependency = context["dependencies"][0]
            self.assertTrue(dependency["available"])
            self.assertIn("def load_text_classification_splits(config_path=None)", dependency["public_api"])
            self.assertNotIn("load_text_classification_dataset", str(context))
            # API observation uses the complete decoded file even when its
            # excerpt is bounded. Generation and repair share the same reader.
            self.assertIn("def later_api()", dependency["public_api"])
            self.assertEqual(dependency["source_excerpt"], source[:30])
            for anchors in ((), (("pkg/data.py", 20), ("pkg/data.py", 21)), (("pkg/data.py", 9999),)):
                with patch.object(owner, "read_code_task_text", wraps=owner.read_code_task_text) as read:
                    rows = owner.source_context_for_files(project, ["pkg/data.py"],
                        max_chars_per_file=100, anchors=anchors, editable_files=[], include_unanchored_tail=True)
                self.assertEqual(read.call_count, 1)
                self.assertTrue(rows)
                self.assertLessEqual(sum(len(row["text"]) for row in rows), 100)
                for row in rows:
                    offset = row["source_offset"]
                    self.assertEqual(row["text"], source[offset:offset + len(row["text"])])
                    self.assertEqual(row["source_chars"], len(source))
                    self.assertEqual(row["access_role"], "read_only")
            write_text(project / "isolated/pyvenv.cfg", "home = python\n")
            write_text(project / "isolated/hidden.py", "def private_dependency(): pass\n")
            with patch.object(owner, "read_code_task_text", side_effect=AssertionError("Private source read")):
                context = dependency_context(project, {"dependencies": ["isolated/hidden.py", "../outside.py"]})
                self.assertNotIn("public_api", context["dependencies"][0])
                self.assertEqual(len(context["dependencies"]), 1)
                self.assertEqual(owner.source_context_for_files(project, ["isolated/hidden.py"],
                    max_chars_per_file=10, include_unanchored_tail=True), [])

    def test_resource_static_flags_fit_inside_nested_loops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_text(
                project / "search.py",
                (
                    "def run(model, datasets, candidates):\n"
                    "    for dataset in datasets:\n"
                    "        for candidate in candidates:\n"
                    "            model.fit(dataset.X, dataset.y)\n"
                    "    return model\n"
                ),
            )

            analysis = analyze_resource_risks(project)

            self.assertGreaterEqual(analysis["nested_fit_call_count"], 1)
            self.assertGreaterEqual(analysis["max_fit_loop_depth"], 2)
            self.assertEqual(analysis["files"][0]["path"], "search.py")

    def test_writer_treats_runtime_output_paths_as_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "generated_project"
            artifacts = generate_project(project,
                architecture_plan={
                    "files": [
                        {"path": "main.py", "kind": "source", "entrypoint": True},
                        {"path": "artifacts", "purpose": "Runtime output directory."},
                        {"path": "artifacts/results.json", "purpose": "Runtime evidence bundle."},
                        {"path": "submission/results", "purpose": "Submission metrics directory."},
                        {"path": "submission/results/metrics.json", "purpose": "Runtime metric mirror."},
                    ]
                },
                result_schema={"required_metrics": []}, client=None, allow_fallback=True,
            )

            by_path = {row["path"]: row for row in artifacts["generated_files"]}
            self.assertTrue((project / "artifacts").is_dir())
            self.assertTrue((project / "artifacts" / "results.json").is_file())
            self.assertTrue((project / "submission" / "results").is_dir())
            self.assertEqual(by_path["artifacts"]["mode"], "deterministic_runtime_placeholder")
            self.assertEqual(by_path["artifacts/results.json"]["line_count"], 0)

    def test_writer_strips_single_markdown_fence_from_file_content(self) -> None:
        client = generation_client({"files": [{"path": "main.py",
            "content": "```python\nprint('score: 1.0')\n```"}], "summary": "Fenced but valid Python."})
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "generated_project"
            artifacts = generate_project(project, result_schema={"required_metrics": ["score"]}, client=client, allow_fallback=False,
                agent_step_dir=Path(tmp) / "steps",
            )

            self.assertEqual(artifacts["generated_files"][0]["mode"], "llm_repaired")
            self.assertEqual((project / "main.py").read_text(encoding="utf-8"), "print('score: 1.0')\n")
            write_text(project / "user-notes.md", "Keep the previous delivered project.\n")
            before = project_bytes(project)
            generate_project(project, result_schema={"required_metrics": ["score"]}, client=client, allow_fallback=False)
            previous = list(Path(tmp).glob(".generated_project.previous-*"))
            self.assertEqual(len(previous), 1)
            self.assertEqual(project_bytes(previous[0]), before)
            self.assertFalse((project / "user-notes.md").exists())
            self.assertFalse(list(Path(tmp).glob(".generated_project.candidate-*")))
            current = (project / "main.py").read_bytes()
            rename = Path.rename

            def reject_candidate(source: Path, target: Path) -> Path:
                if ".candidate-" in source.name:
                    raise OSError("simulated publication failure")
                return rename(source, target)

            with patch.object(Path, "rename", reject_candidate), self.assertRaisesRegex(OSError, "publication failure"):
                generate_project(project, client=client)
            self.assertEqual((project / "main.py").read_bytes(), current)
            self.assertEqual(len(list(Path(tmp).glob(".generated_project.previous-*"))), 1)
            self.assertEqual(len(list(Path(tmp).glob(".generated_project.candidate-*"))), 1)
            if os.name != "nt":
                alias = Path(tmp) / "alias"
                alias.symlink_to(project, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, "non-symlink"):
                    generate_project(alias, architecture_plan={})
                self.assertEqual((project / "main.py").read_bytes(), current)

    def test_writer_rejections_preserve_project_memory_and_diagnostics(self) -> None:
        from simple_ar.integrations.llm import LLMError

        cases = (
            ("syntax", [{"path": "main.py", "content": "def broken(:\n    pass\n"}], 100),
            ("line_limit", [{"path": "main.py", "content": "a = 1\n"},
                            {"path": "helper.py", "content": "a = 1\nb = 2\n"}], 2),
            ("traversal", [{"path": "../escape.py", "content": "a = 1\n"}], 100),
            ("empty", [], 100),
        )
        for name, rows, limit in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project, steps = root / "project", root / "steps"
                write_text(project / "main.py", "print('original result')\n")
                write_text(project / "outputs/measurement.json", '{"actual": 7}\n')
                before = project_bytes(project)
                memory = {"prior": True}
                client = generation_client({"files": rows})
                plan = {"files": [{"path": row["path"], "kind": "source"} for row in rows]} if limit == 2 else {
                    "files": [{"path": "main.py", "kind": "source", "entrypoint": True}]}
                with self.assertRaises(LLMError):
                    generate_project(project, architecture_plan=plan, memory=memory, client=client, max_generated_lines=limit,
                        retry_attempts=2, allow_fallback=False, agent_step_dir=steps,
                    )
                self.assertEqual(memory, {"prior": True})
                self.assertEqual(project_bytes(project), before)
                self.assertFalse((root / "escape.py").exists())
                self.assertEqual(len(list(root.glob(".project.candidate-*"))), 1)
                self.assertFalse(list(root.glob(".project.previous-*")))
                if name == "syntax":
                    failure = read_json(steps / "invalid_files/main.py/attempt-001.json")
                    self.assertEqual(failure["validation"]["reason"], "syntax_error")
                    self.assertTrue((steps / "invalid_files/main.py/attempt-001.py").is_file())

    def test_writer_generates_related_batches_without_a_twelve_file_project_limit(self) -> None:
        class Client:
            def __init__(self):
                self.requests = []

            def ask_json(self, _system, prompt, **kwargs):
                request = json.loads(prompt)
                self.requests.append((request, kwargs))
                return {"files": [
                    {"path": row["path"], "content": "def value():\n    return 1\n"}
                    for row in request["planned_files"]], "summary": "Related implementations."}

        with tempfile.TemporaryDirectory() as tmp:
            client = Client()
            memory = {"retained": "prior decisions"}
            files = [{"path": "main.py", "kind": "source"}] + [
                {"path": f"module_{index}.py", "kind": "source"} for index in range(12)]
            artifacts = generate_project(Path(tmp) / "project", architecture_plan={"files": files}, memory=memory, client=client,
                files_per_batch=4,
            )
            self.assertEqual([len(request["planned_files"]) for request, _ in client.requests], [4, 4, 4, 1])
            self.assertEqual(len(artifacts["generated_files"]), 13)
            self.assertEqual(artifacts["schema_version"], "greenfield_code_artifacts.v1")
            self.assertEqual(artifacts["entrypoint"], "main.py")
            self.assertEqual(memory["retained"], "prior decisions")
            self.assertEqual(len(memory["file_summaries"]), 13)
            self.assertEqual(len(memory["generated_batches"]), 4)
            self.assertEqual(len(client.requests[1][0]["implementation_memory"]["file_summaries"]), 4)
            self.assertTrue(all(kwargs["max_output_tokens"] is None for _, kwargs in client.requests))
            single_unit = Client()
            large = generate_project(Path(tmp) / "large_unit", architecture_plan={"files": files}, client=single_unit,
                files_per_batch=13,
            )
            self.assertEqual(len(single_unit.requests), 1)
            self.assertEqual(len(large["generated_files"]), 13)

    def test_writer_retries_invalid_json_as_one_unit_and_keeps_explicit_fallback(self) -> None:
        from simple_ar.core.budget import BudgetLedger
        class Client:
            def __init__(self, valid_on_retry):
                self.calls = 0
                self.valid_on_retry = valid_on_retry
                self._budget_ledger = BudgetLedger({'llm_requests': 4, 'total_tokens': 400})
                self._budget_ledger.reserve('older-call', {'llm_requests': 1, 'total_tokens': 100})
                self.old_unknown = self._budget_ledger.mark_unknown('older-call',
                    reason='earlier transport failed', known_actual={'llm_requests': 1},
                    retain_reservation=True).to_dict()

            def ask_json(self, _system, prompt, **kwargs):
                self.calls += 1
                reservation = str(self.calls)
                self._budget_ledger.reserve(reservation, {'llm_requests': 1, 'total_tokens': 100})
                self._budget_ledger.settle(reservation, {'llm_requests': 1, 'total_tokens': 25})
                if self.calls > 1:
                    self_feedback = prompt.split("Validation feedback: ")[-1]
                    assert "JSON" in self_feedback
                return {"files": [
                    {"path": "main.py", "content": "print('ok')\n"},
                    {"path": "config.json", "content": "{}" if self.valid_on_retry and self.calls > 1 else "{broken"}]}

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            plan = {"files": [{"path": "main.py", "kind": "source"},
                {"path": "config.json", "kind": "config"}]}
            options = dict(project_dir=project, architecture_plan=plan,
                result_schema={}, contract={}, retry_attempts=3)
            client = Client(True)
            artifacts = generate_project(**options, client=client)
            self.assertEqual(client.calls, 2)
            self.assertEqual(client._budget_ledger.remaining('llm_requests'), 1)
            self.assertEqual(client._budget_ledger.remaining('total_tokens'), 250)
            self.assertEqual(json.loads((project / "config.json").read_text()), {})
            self.assertTrue(all(row["mode"] == "llm" for row in artifacts["generated_files"]))
            before = project_bytes(project)
            memory = {"retained": ["original"]}
            failed = Client(False)
            from simple_ar.integrations.llm import LLMError
            with self.assertRaises(LLMError):
                generate_project(**options, memory=memory, client=failed)
            self.assertEqual(failed.calls, 3)
            self.assertEqual(memory, {"retained": ["original"]})
            self.assertEqual(project_bytes(project), before)
            fallback = Client(False)
            artifacts = generate_project(**options, client=fallback, allow_fallback=True)
            self.assertEqual(fallback.calls, 3)
            self.assertTrue(all(row["mode"] == "fallback" for row in artifacts["generated_files"]))
            for retained_client in (client, failed, fallback):
                self.assertEqual(retained_client._budget_ledger.entries[0].to_dict(), retained_client.old_unknown)
            before = project_bytes(project)
            for status in ('unknown', 'unknown_accounted', 'reserved'):
                with self.subTest(status=status):
                    uncertain = Mock()
                    ledger = BudgetLedger({'llm_requests': 3, 'total_tokens': 300, 'image_requests': 3})
                    uncertain._budget_ledger = ledger
                    amounts = {'image_requests': 1} if status != 'unknown' else {'llm_requests': 1, 'total_tokens': 100}
                    ledger.reserve('prior', amounts)
                    if status != 'unknown':
                        self.assertEqual(ledger.unknown_dimensions(), ())
                    ledger_after = []
                    def incomplete(*_args, **_kwargs):
                        if status != 'reserved':
                            ledger.mark_unknown('prior', reason='dispatch unknown',
                                known_actual=amounts if status == 'unknown_accounted' else None)
                        ledger_after.append(ledger.to_dict())
                        return {'files': []}
                    uncertain.ask_json.side_effect = incomplete
                    with self.assertRaises(ValueError):
                        generate_project(**options, memory=memory, client=uncertain, allow_fallback=True)
                    self.assertEqual(uncertain.ask_json.call_count, 1)
                    self.assertEqual(ledger.to_dict(), ledger_after[0])
                    self.assertEqual(memory, {"retained": ["original"]})
                    self.assertEqual(project_bytes(project), before)

    def test_generation_failure_ownership_and_completed_json_correction(self) -> None:
        from simple_ar.code_task.generation.agent_step import run_json_agent_step
        from simple_ar.code_task.generation.architecture import build_architecture_plan
        from simple_ar.code_task.generation.writer import write_implementation_unit
        from simple_ar.core.budget import BudgetLedger, BudgetExceededError
        from simple_ar.integrations.llm import LLMError, LLMResponseError, LLMStreamError, LLMDeadlineError

        budget_error = LLMError('request admission failed')
        budget_error.__cause__ = BudgetExceededError('remaining budget')
        failures = [(LLMError('provider unavailable'), False, False), (budget_error, False, False),
            (LLMStreamError('partial stream'), True, False),
            (LLMDeadlineError('deadline; usage unknown'), True, False),
            (LLMResponseError('completed invalid JSON', raw_response='{broken'), True, False),
            (LLMResponseError('dispatch unknown but dimensions accounted', raw_response='{broken'), 'unknown_accounted', False),
            (LLMResponseError('pending operation', raw_response='{broken'), 'reserved', False),
            (LLMError('provider failed during correction'), False, True)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / 'project'
            write_text(project / 'main.py', "print('original')\n")
            original = (project / 'main.py').read_bytes()
            for owner in ('step', 'compact', 'tool_agent', 'legacy_writer', 'native_writer'):
                for error, unknown, correction in failures:
                    with self.subTest(owner=owner, error=type(error).__name__, status=unknown):
                        ledger = BudgetLedger({'llm_requests': 10, 'total_tokens': 1000})
                        ledger.reserve('prior', {'llm_requests': 1, 'total_tokens': 100})
                        # These uncertainties arise within the operation boundary.
                        if unknown in ('unknown_accounted', 'reserved'):
                            ledger.settle('prior', {'llm_requests': 1, 'total_tokens': 25})
                            ledger.reserve('image', {'image_requests': 1})
                            self.assertEqual(ledger.unknown_dimensions(), ())
                        elif not unknown:
                            ledger.settle('prior', {'llm_requests': 1, 'total_tokens': 25})
                        client = Mock()
                        client._budget_ledger = ledger
                        ledger_after = []
                        def fail(*_args, **_kwargs):
                            if unknown == 'unknown_accounted':
                                ledger.mark_unknown('image', reason='dispatch unknown', known_actual={'image_requests': 1})
                            elif unknown and unknown != 'reserved':
                                ledger.mark_unknown('prior', reason='retained uncertain charge')
                            ledger_after.append(ledger.to_dict())
                            if correction and client.ask_json.call_count == 1:
                                raise LLMResponseError('invalid JSON', raw_response='{broken')
                            raise error
                        client.ask_json.side_effect = fail
                        memory = {'retained': True}
                        with self.assertRaises(type(error)) as caught:
                            if owner == 'step':
                                run_json_agent_step(client=client, system='JSON', prompt='goal',
                                    label='unit', stage='unit', attempt_index=1, retry_attempts=3,
                                    max_output_tokens=100, artifact_dir=root / owner)
                            elif owner in ('compact', 'tool_agent'):
                                build_architecture_plan(contract={}, result_schema={}, resource_plan={},
                                    domain_profile={}, client=client, planning_mode=owner,
                                    retry_attempts=3, allow_fallback=True, planning_dir=root / owner)
                            elif owner == 'legacy_writer':
                                generate_project(project, architecture_plan={'files': [{'path': 'main.py', 'kind': 'source'}]},
                                    memory=memory, client=client, retry_attempts=3, allow_fallback=True,
                                    agent_step_dir=root / owner,
                                )
                            else:
                                write_implementation_unit(project_dir=project, goal='goal',
                                    files=[{'path': 'main.py'}], source_windows=[], client=client,
                                    artifact_dir=root / owner)
                        self.assertIs(caught.exception, error)
                        self.assertEqual(client.ask_json.call_count, 2 if correction else 1)
                        self.assertEqual(ledger.to_dict(), ledger_after[-1])
                        self.assertEqual(memory, {'retained': True})
                        self.assertEqual((project / 'main.py').read_bytes(), original)

            client = Mock()
            ledger = BudgetLedger({'llm_requests': 2, 'total_tokens': 200})
            client._budget_ledger = ledger
            def complete(*_args, **_kwargs):
                reservation = str(client.ask_json.call_count)
                ledger.reserve(reservation, {'llm_requests': 1, 'total_tokens': 100})
                ledger.settle(reservation, {'llm_requests': 1, 'total_tokens': 25})
                if client.ask_json.call_count == 1:
                    raise LLMResponseError('invalid JSON', raw_response='{broken')
                return {'ok': True}
            client.ask_json.side_effect = complete
            steps = root / 'corrected'
            result = run_json_agent_step(client=client, system='JSON', prompt='goal', label='unit',
                stage='unit', attempt_index=1, retry_attempts=2, max_output_tokens=100, artifact_dir=steps)
            self.assertEqual(result, {'ok': True})
            self.assertEqual(client.ask_json.call_count, 2)
            self.assertEqual(ledger.remaining('llm_requests'), 0)
            self.assertEqual(ledger.remaining('total_tokens'), 150)
            self.assertNotIn('transport failed', client.ask_json.call_args.args[1])
            self.assertEqual(read_json(steps / 'unit/attempt-001-response-001.json')['raw_response'], '{broken')
            rows = read_jsonl(steps / 'agent_steps.jsonl')
            self.assertEqual([row['status'] for row in rows], ['failed', 'passed'])
            self.assertNotEqual(rows[0]['prompt_sha256'], rows[1]['prompt_sha256'])

    def test_retired_stage_profiles_require_one_existing_named_planner(self) -> None:
        from simple_ar.code_task.runtime.config import load_code_task_execute_options, CodeTaskConfigError
        from simple_ar.code_task.generation.greenfield import _llm_client
        from simple_ar.integrations.llm import LLMError
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            for stage in ("requirements", "architecture", "interfaces", "file_plan", "planning_review"):
                write_text(config, f'[models.code_task]\n{stage} = "profile:saved"\n')
                with self.subTest(stage=stage), self.assertRaisesRegex(CodeTaskConfigError, "stage model routes are retired"):
                    load_code_task_execute_options(config_path=str(config))
            write_text(config, '[models.code_task]\nplanner = "profile:saved"\n')
            options = load_code_task_execute_options(config_path=str(config))
            self.assertEqual(options.planner_model, "profile:saved")
            self.assertEqual(options.planning_mode, "compact")
            self.assertEqual(options.planning_review_rounds, 0)
            with patch("simple_ar.code_task.generation.greenfield.LLMClient.for_task",
                       side_effect=LLMError("selected connection unavailable")) as select:
                with self.assertRaises(LLMError):
                    _llm_client(root, model=options.planner_model, use_llm=True,
                                allow_fallback=True, message_callback=None)
                self.assertEqual(select.call_args.kwargs["model"], "profile:saved")


    def test_shared_unit_retains_exact_replacements_and_single_attempt_policy(self) -> None:
        from simple_ar.code_task.generation.writer import write_implementation_unit
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = "VALUE = 1\n# preserve this note\n"
            client = generation_client({"files": [{"path": "module.py", "replacements": [
                {"old_text": "VALUE = 1", "new_text": "VALUE = 2"}]}], "unresolved": []})
            observations = [{'record': 'records/failed/native_result.json',
                'arguments': {'argv': ['python', 'check.py'], 'cwd': '/candidate'},
                'result': {'status': 'ok', 'data': {'returncode': 1,
                    'stdout': '', 'stderr': 'AssertionError: actual behavior differs\n'}}}]
            result = write_implementation_unit(project_dir=root / "unit", goal="Change value",
                files=[{"path": "module.py"}], source_windows=[], client=client,
                artifact_dir=root / "steps", original_files={"module.py": {"text": original}},
                execution_observations=observations)
            client.ask_json.assert_called_once()
            self.assertEqual(json.loads(client.ask_json.call_args.args[1])['execution_observations'], observations)
            self.assertEqual((root / "unit/module.py").read_text(), "VALUE = 2\n# preserve this note\n")
            self.assertEqual(result["unresolved"], [])
            invalid = generation_client({"files": [{"path": "module.py", "content": "def broken(:\n"}], "unresolved": []})
            with self.assertRaises(SyntaxError):
                write_implementation_unit(project_dir=root / "unit", goal="New module",
                    files=[{"path": "module.py"}], source_windows=[], client=invalid, artifact_dir=root / "failed")
            invalid.ask_json.assert_called_once()
            self.assertEqual((root / "unit/module.py").read_text(), "VALUE = 2\n# preserve this note\n")

    def test_implementation_owner_selects_new_files_and_preserves_input_recovery(self) -> None:
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.tools.implementation import ImplementationRequest, ImplementationToolGateway
        from simple_ar.tools.workspace import WorkspaceToolGateway

        cases = [(inputs, 'direct') for inputs in ('goal', 'windows', 'project', 'saved_project', 'explicit')]
        cases += [('project', 'primitive'), ('project', 'inputs_only'), ('project', 'exact'), ('explicit', 'unmarked')]
        for inputs, consumer in cases:
            with self.subTest(inputs=inputs, consumer=consumer), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / 'source'
                original = 'VALUE = 1\n# Preserve the complete original.\n'
                source.mkdir()
                (source / 'library.py').write_bytes(original.encode('utf-8'))
                write_text(source / 'method.txt', 'Original method definition.\n')
                ledger = BudgetLedger({'llm_requests': 2})
                workspace = WorkspaceToolGateway(root, {'source': source}, ledger)
                if consumer in ('primitive', 'inputs_only', 'exact'):
                    write_json(root / 'session_manifest.json', {'session_id': 'session',
                        'budget': {'recorded_attempts': []}})
                    write_json(root / 'actions.json', {'config':
                        {'professional_inputs': 'owner.v3'} if consumer != 'primitive' else {}})
                output = workspace.resolve('workspace/implementations/unit', write=True)
                owner = root / 'attempts/unit/implementation'
                arguments = {'goal': 'Create a library and usage documentation.'}
                selected = [{'path': 'library.py', 'purpose': 'Library implementation.'},
                            {'path': 'USAGE.md', 'purpose': 'Usage documentation.'}]
                if inputs == 'windows':
                    arguments['source_windows'] = [{'path': 'source/method.txt'}]
                if inputs in ('project', 'saved_project'):
                    arguments['project_path'] = 'source'
                if inputs == 'explicit':
                    arguments['files'] = selected
                if inputs == 'saved_project':
                    # Retained pre-change selection shape: restore without
                    # reinspection or adding fields to its durable binding.
                    inventory = workspace.bind_inventory(source.resolve(), identity='unit')
                    saved_input = {'request': ImplementationRequest.model_validate(arguments).model_dump(),
                        'source_windows': [], **inventory}
                    saved_files = [dict(selected[0], source_path=str((source / 'library.py').resolve()),
                        source_sha256=inventory['file_hashes'][str((source / 'library.py').resolve())]), selected[1]]
                    write_json(owner / 'selection-input.json', saved_input)
                    write_json(owner / 'selection-response.json', {'files': selected})
                    write_json(owner / 'selection.json', {'input': saved_input, 'files': saved_files,
                        'coverage': {'inventory_scope': inventory['inventory_scope']},
                        'source_context': [], 'declared_project': {}})
                client = Mock()
                client._budget_ledger = ledger
                factory = Mock(return_value=client)
                observations = Mock(return_value=[{'result': {'data': {'returncode': 1,
                    'stderr': 'Original diagnostic.\n'}}}])

                def respond(_system, prompt, *, label, **_kwargs):
                    ledger.reserve(label, {'llm_requests': 1})
                    ledger.settle(label, {'llm_requests': 1})
                    request = json.loads(prompt)
                    if label == 'implementation-selection':
                        self.assertEqual(inputs, 'project')
                        self.assertFalse(output.exists())
                        self.assertEqual(request['goal'], arguments['goal'])
                        self.assertEqual(request['project_path'], str(source.resolve()))
                        self.assertIn('project_index', request)
                        return {'files': selected}
                    self.assertEqual(label, 'implementation-unit')
                    self.assertEqual(request['goal'], arguments['goal'])
                    if inputs in ('goal', 'windows'):
                        # New units choose files and content in one writer call.
                        self.assertIsNone(request['planned_files'])
                    else:
                        self.assertEqual([row['path'] for row in request['planned_files']],
                            ['library.py', 'USAGE.md'])
                    self.assertEqual(request['execution_observations'], observations.return_value)
                    if inputs in ('project', 'saved_project'):
                        self.assertEqual(request['original_files']['library.py']['text'], original)
                        implementation = {'path': 'library.py', 'replacements': [
                            {'old_text': 'VALUE = 1', 'new_text': 'VALUE = 2'}]}
                    else:
                        self.assertEqual(request['original_files'], {})
                        implementation = {'path': 'library.py', 'content': 'VALUE = 2\n'}
                    if inputs == 'windows':
                        self.assertIn('Original method definition.', str(request['source_windows']))
                    return {'files': [implementation, {'path': 'USAGE.md', 'content': '# Usage\n'}],
                        'unresolved': ['Execution remains to be verified.']}

                client.ask_json.side_effect = respond
                gateway = ImplementationToolGateway(root, workspace, ledger, 'model', 'session',
                    client_factory=factory, execution_observations=observations,
                    exact=({'goal': arguments['goal'], 'project_path': str(source),
                        'allowed_patterns': ['*.py'], 'validation_command': ['python', 'library.py']}
                        if consumer == 'exact' else None))
                with patch.object(workspace, 'bind_inventory', wraps=workspace.bind_inventory) as inventory_calls:
                    if inputs == 'saved_project':
                        with patch('simple_ar.code_task.generation.selection.implementation_selection_context',
                                   side_effect=AssertionError('Use retained selection')):
                            result = gateway.call('prepare_implementation', arguments, identity='unit')
                    else:
                        result = gateway.call('prepare_implementation', arguments, identity='unit')
                    self.assertEqual(result.status, 'ok', result.error)
                    if inputs in ('goal', 'windows', 'explicit'):
                        inventory_calls.assert_not_called()
                self.assertEqual((source / 'library.py').read_bytes(), original.encode('utf-8'))
                self.assertEqual((output / 'library.py').read_bytes(),
                    (original.replace('VALUE = 1', 'VALUE = 2') if inputs in ('project', 'saved_project')
                        else 'VALUE = 2\n').encode('utf-8'))
                self.assertFalse((output / 'main.py').exists())
                self.assertEqual(result.data['unresolved'], ['Execution remains to be verified.'])
                self.assertEqual(client.ask_json.call_count, 2 if inputs == 'project' else 1)
                self.assertEqual(ledger.remaining('llm_requests'), 0 if inputs == 'project' else 1)
                factory.assert_called_once_with('unit')
                if inputs in ('goal', 'windows'):
                    binding = read_json(owner / 'request.json')
                    self.assertEqual(binding['generation_method'], 'direct_creation.v1')
                    self.assertEqual(binding['request']['files'], [])
                    self.assertFalse((owner / 'selection-input.json').exists())
                    self.assertFalse((owner / 'selection.json').exists())
                    self.assertEqual([row['path'] for row in result.data['files']],
                        ['library.py', 'USAGE.md'])
                if inputs == 'explicit':
                    binding = read_json(owner / 'request.json')
                    self.assertNotIn('selection', binding)
                    self.assertEqual(binding['request'], {**arguments, 'source_windows': []})
                    self.assertEqual(binding['argv_semantics'], 'candidate_relative.v2')
                    if consumer == 'unmarked':
                        # Historical cached receipts must not gain semantics
                        # from today's defaults, even with a new context owner.
                        binding.pop('argv_semantics')
                        write_json(owner / 'request.json', binding)
                        gateway.work_context = Mock(side_effect=AssertionError('Keep frozen context'))
                retained = {path: path.read_bytes() for path in owner.rglob('*.json')}
                before = ledger.to_dict()
                # Recover with changed history and no model connection.
                gateway.client_factory = Mock(side_effect=AssertionError('No redispatch'))
                observations.return_value = []
                restored = gateway.call('prepare_implementation', arguments, identity='unit')
                self.assertEqual(restored.model_dump(), result.model_dump())
                self.assertEqual(ledger.to_dict(), before)
                self.assertEqual({path: path.read_bytes() for path in retained}, retained)
                changed = gateway.call('prepare_implementation', dict(arguments, goal='Different goal'), identity='unit')
                self.assertEqual(changed.status, 'error')
                if inputs == 'windows':
                    write_text(source / 'method.txt', 'Changed method definition.\n')
                    self.assertEqual(gateway.call('prepare_implementation', arguments, identity='unit').status, 'error')
                if inputs in ('project', 'saved_project'):
                    write_text(source / 'library.py', 'VALUE = 3\n')
                    self.assertEqual(gateway.call('prepare_implementation', arguments, identity='unit').status, 'error')
                write_text(output / 'USAGE.md', '# Changed candidate\n')
                self.assertEqual(gateway.call('prepare_implementation', arguments, identity='unit').status, 'error')
                self.assertEqual(ledger.to_dict(), before)

    def test_implementation_selection_keeps_permissions_budget_and_no_replay(self) -> None:
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.integrations.llm import LLMStreamError
        from simple_ar.tools.implementation import ImplementationToolGateway
        from simple_ar.tools.workspace import WorkspaceToolGateway

        cases = [('project', failure) for failure in (
            'traversal', 'private', 'missing_project', 'overlap',
            'unknown_selection', 'unknown_writer', 'budget')]
        cases += [('direct', failure) for failure in (
            'traversal', 'private', 'unknown_writer', 'budget')]
        for route, failure in cases:
            with self.subTest(route=route, failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                ledger = BudgetLedger({'llm_requests': 1 if failure == 'budget' else 2})
                workspace = WorkspaceToolGateway(root, {}, ledger)
                arguments = {'goal': 'Create a library.'}
                if route == 'project':
                    write_text(workspace.workspace / 'source/library.py', 'VALUE = 1\n')
                    arguments['project_path'] = 'workspace/source'
                if failure in ('missing_project', 'overlap'):
                    arguments['project_path'] = 'workspace/missing' if failure == 'missing_project' else 'workspace'
                client = Mock()
                client._budget_ledger = ledger
                physical_calls = []

                def respond(_system, _prompt, *, label, **_kwargs):
                    # Corrections are new physical attempts, not idempotent
                    # reuses of an already settled logical-label reservation.
                    reservation = f'{label}:{client.ask_json.call_count}'
                    ledger.reserve(reservation, {'llm_requests': 1})
                    physical_calls.append(label)
                    if ((failure == 'unknown_selection' and label == 'implementation-selection')
                            or (failure == 'unknown_writer' and label == 'implementation-unit')):
                        ledger.mark_unknown(reservation, reason='Interrupted response')
                        raise LLMStreamError('Incomplete response')
                    ledger.settle(reservation, {'llm_requests': 1})
                    path = '../escape.py' if failure == 'traversal' else '.env' if failure == 'private' else 'library.py'
                    if label == 'implementation-unit':
                        # Budget's completed invalid response needs correction,
                        # but the exhausted ledger must prevent that dispatch.
                        return {'files': [{'path': path, **({} if failure == 'budget'
                            else {'content': 'VALUE = 2\n'})}]}
                    return {'files': [{'path': path, 'purpose': 'Library implementation.'}]}

                client.ask_json.side_effect = respond
                gateway = ImplementationToolGateway(root, workspace, ledger, 'model', 'session',
                    client_factory=Mock(return_value=client))
                result = gateway.call('prepare_implementation', arguments, identity='unit')
                self.assertEqual(result.status, 'error')
                calls = client.ask_json.call_count
                self.assertEqual(calls, 0 if failure in ('missing_project', 'overlap')
                    else 2 if (route == 'project' and failure in ('unknown_writer', 'budget')
                        or route == 'direct' and failure in ('traversal', 'private')) else 1)
                if failure in ('missing_project', 'overlap'):
                    expected_physical = []
                elif route == 'project':
                    expected_physical = ['implementation-selection']
                    if failure == 'unknown_writer':
                        expected_physical.append('implementation-unit')
                else:
                    expected_physical = ['implementation-unit'] * calls
                self.assertEqual(physical_calls, expected_physical)
                self.assertEqual(len(ledger.entries), len(expected_physical))
                if failure.startswith('unknown'):
                    self.assertIsNone(ledger.remaining('llm_requests'))
                    self.assertEqual(ledger.entries[-1].status, 'unknown')
                else:
                    self.assertEqual(ledger.remaining('llm_requests'),
                        (1 if failure == 'budget' else 2) - len(expected_physical))
                self.assertFalse((workspace.workspace / 'implementations/unit').exists())
                self.assertFalse((workspace.workspace / 'implementations/escape.py').exists())
                self.assertFalse((workspace.workspace / 'escape.py').exists())
                if route == 'project':
                    self.assertEqual((workspace.workspace / 'source/library.py').read_text(), 'VALUE = 1\n')
                before = ledger.to_dict()
                self.assertEqual(gateway.call('prepare_implementation', arguments, identity='unit').status, 'error')
                self.assertEqual(client.ask_json.call_count, calls)
                self.assertEqual(ledger.to_dict(), before)
                if failure.startswith('unknown'):
                    self.assertTrue(ledger.unknown_dimensions())

    def test_professional_revision_keeps_unmarked_parent_material_required(self) -> None:
        import hashlib
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.core.capabilities import ArtifactStore
        from simple_ar.tools.implementation import ImplementationRequest, ImplementationToolGateway
        from simple_ar.tools.implementation_work import prepare_work
        from simple_ar.tools.workspace import WorkspaceToolGateway

        for parent_contract in ({}, {'producer_completion': 'goal_findings.v1'},
                {'producer_completion': 'goal_findings.v1', 'work_contract': 'shared.v2'}):
            with self.subTest(parent_contract=parent_contract), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                ledger = BudgetLedger({'llm_requests': 2})
                workspace = WorkspaceToolGateway(root, {'records': root / 'attempts'}, ledger)
                project = workspace.workspace / 'project'
                write_text(project / 'worker.py', 'VALUE = 1\n')
                text = 'Required original material.\n' * 1500
                notes = workspace.workspace / 'notes.txt'
                write_text(notes, text)
                request_window = {'path': 'workspace/notes.txt', 'max_bytes': 64000, 'full_digest': True}
                observed = workspace.call('read_file', request_window, identity='original-read')
                self.assertEqual(observed.status, 'ok', observed.error)
                row = {'request': request_window, **observed.data,
                    'role': 'supplied_source_or_diagnostic_not_certified_fact'}
                self.assertGreater(len(row['text']), 32000)  # Exceeds optional seed allowance.
                prior = ArtifactStore(root / 'attempts/previous/implementation')
                prior_request = ImplementationRequest(goal='Original work', source_windows=[request_window],
                    complete_work='work_contract' in parent_contract, **parent_contract).model_dump()
                prior.write_json('request.json', {'request': prior_request,
                    'source_windows': [row]})
                prior.write_json('result.json', {})
                retained = {'result_ref': 'records/previous/implementation/result.json',
                    'result_sha256': hashlib.sha256(prior.resolve('result.json').read_bytes()).hexdigest(),
                    'source_binding_sha256': hashlib.sha256(prior.resolve('request.json').read_bytes()).hexdigest(),
                    'project_root': str(project), 'original_files': {}, 'source_windows': [row]}
                gateway = ImplementationToolGateway(root, workspace, ledger, 'model', 'session',
                    client_factory=Mock())
                gateway._retained_candidate = Mock(return_value=retained)
                gateway._publish_implementation = Mock(return_value='published')
                current = ImplementationRequest(goal='Revise the registered project', complete_work=True,
                    producer_completion='goal_findings.v1', work_contract='shared.v2')
                store = ArtifactStore(root / 'attempts/revision/implementation')
                original_binding = prior.resolve('request.json').read_bytes()
                # Registration is checked by the preceding owner. Exercise the
                # real material reads, source presentation and byte guards here.
                members = {Path(path).relative_to(project).as_posix(): digest for path, digest in
                    workspace.bind_inventory(project, identity='previous')['file_hashes'].items()}
                with patch('simple_ar.code_task.generation.selection.registered_project_files',
                        return_value=members), \
                        patch('simple_ar.code_task.generation.writer.write_implementation_unit',
                            return_value={}) as writer:
                    self.assertEqual(prepare_work(gateway, current, store,
                        workspace.workspace / 'revision', project, 'revision', None), 'published')
                    material = [item for item in writer.call_args.kwargs['source_windows'] if 'request' in item]
                    self.assertEqual(len(material), 1)
                    self.assertEqual(material[0]['text'], text)
                    self.assertEqual(material[0]['sha256'], row['sha256'])
                    self.assertEqual(material[0]['role'], 'retained_observed_material_not_certified_fact')
                    self.assertIn(material[0]['observation_ref'],
                        writer.call_args.kwargs['investigation'].context()['working_set']['required_refs'])
                    self.assertEqual(prior.resolve('request.json').read_bytes(), original_binding)
                    write_text(notes, 'Changed material\n')
                    with self.assertRaisesRegex(ValueError, 'Retained material changed'):
                        prepare_work(gateway, current, ArtifactStore(root / 'attempts/changed/implementation'),
                            workspace.workspace / 'changed', project, 'changed', None)
                    self.assertEqual(writer.call_count, 1)
                    self.assertEqual(ledger.entries, ())

    def test_source_snapshot_contract_requires_native_preparation(self) -> None:
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.core.capabilities import ArtifactStore, AttemptManifest
        from simple_ar.code_task.generation.selection import ImplementationSources
        from simple_ar.tools.workspace import WorkspaceToolGateway

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = WorkspaceToolGateway(root, {}, BudgetLedger({}))
            sources = ImplementationSources(workspace=workspace, project=None, roots=[], windows=[],
                originals={}, identity='unit')
            owner = ArtifactStore(root / 'attempts/unit/implementation')
            binding = {}
            sources.retain_binding(binding, owner)
            self.assertNotIn('execution_snapshot', binding)
            write_json(root / 'session_manifest.json', {'session_id': 'session'})
            sources.retain_binding(binding, owner)
            self.assertNotIn('execution_snapshot', binding)
            ArtifactStore(owner.root.parent).write_attempt_manifest(AttemptManifest('unit', capability='native_tool'))
            sources.retain_binding(binding, owner)
            self.assertEqual(binding['execution_snapshot'], 'implementation_inputs.v1')

    def test_review_accepts_runtime_placeholders_as_runtime_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_text(project / "main.py", "def main():\n    print('score: 1.0')\n")
            (project / "artifacts").mkdir()
            write_text(project / "artifacts" / "results.json", "{}\n")

            review = generated_review(project,
                [
                {"path": "main.py", "kind": "source", "mode": "llm", "line_count": 2},
                {"path": "artifacts", "kind": "runtime_dir", "mode": "deterministic_runtime_placeholder", "line_count": 0},
                {"path": "artifacts/results.json", "kind": "output_placeholder",
                 "mode": "deterministic_runtime_placeholder", "line_count": 0},
                ],
                resource_plan={"max_files": 8, "max_generated_lines": 200},
            )

            self.assertNotIn("missing_file", {row["category"] for row in review["findings"]})

    def test_valid_unicode_identifiers_survive_generation_review_and_repair_guards(self) -> None:
        import subprocess
        import sys
        from simple_ar.code_task.generation.writer import _validate_file_content
        from simple_ar.code_task.generation.generated_project_repair import _post_write_static_guard
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = "def 求和(数值):\n    return sum(数值)\n\ndef main():\n    print(求和([1, 2]))\n\nif __name__ == '__main__':\n    main()\n"
            write_text(
                project / "main.py",
                source,
            )
            self.assertTrue(_validate_file_content(source, filename="main.py").valid)
            self.assertEqual(_post_write_static_guard(target=project / "main.py", rel_path="main.py"), "")

            review = generated_review(project, [{"path": "main.py", "mode": "llm", "line_count": 5}])

            self.assertFalse(any(row["severity"] == "blocking" for row in review["findings"]))
            executed = subprocess.run([sys.executable, str(project / "main.py")], capture_output=True, text=True, timeout=5)
            self.assertEqual(executed.returncode, 0, executed.stderr)
            self.assertEqual(executed.stdout.strip(), "3")
            self.assertFalse(_validate_file_content("def 求和(:", filename="main.py").valid)

    def test_writer_rejects_self_reported_defective_response(self) -> None:
        self.assertTrue(
            _response_self_reports_defect(
                {
                    "summary": (
                        "Created main.py. Note: the file content includes an import typo that should be "
                        "corrected before execution."
                    )
                }
            )
        )
        self.assertFalse(_response_self_reports_defect({"summary": "Corrected the import typo and verified the file."}))
        self.assertFalse(
            _response_self_reports_defect(
                {"summary": "Creates output directories before execution and writes measured metrics."}
            )
        )

    def test_entrypoint_review_requires_preserved_traceback(self) -> None:
        cases = (
            ("stderr_summary", "import sys\n\ndef main():\n    try:\n"
             "        raise RuntimeError('hidden')\n    except Exception as exc:\n"
             "        print(f'ERROR: {exc}', file=sys.stderr)\n        return 1\n", True),
            ("stdout_summary", "def main():\n    try:\n        print('score: 1.0')\n"
             "    except Exception as exc:\n        print(f'ERROR: {exc}')\n        return 1\n", True),
            ("traceback", "import traceback\n\ndef main():\n    try:\n        print('score: 1.0')\n"
             "    except Exception:\n        traceback.print_exc()\n        return 1\n", False),
        )
        for name, source, blocked in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                write_text(project / "main.py", source)
                analysis = analyze_entrypoint_debuggability(project)
                self.assertEqual([row["path"] for row in analysis["findings"]],
                                 ["main.py"] if blocked else [])
                review = generated_review(project,
                    [{"path": "main.py", "mode": "llm",
                    "line_count": len(source.splitlines())}],
                )
                self.assertEqual(any(row["category"] == "entrypoint_exception_suppresses_traceback"
                                     for row in review["findings"]), blocked)
                if blocked:
                    self.assertEqual(review["status"], "failed")

    def test_existing_context_snippets_expose_api_without_extra_repo_reads(self) -> None:
        contract = snippet_api_contract(
            [
                {
                    "path": "service.py",
                    "text": "class Service:\n    def run(self, value: int) -> str:\n        return str(value)\n",
                }
            ]
        )

        self.assertIn("class Service", contract["service.py"][0])
        self.assertTrue(any("Service.def run" in row for row in contract["service.py"]))

        from io import BytesIO
        from PIL import Image
        from simple_ar.code_task.analysis.context import (
            ensure_code_task_context_pack, load_latest_code_task_context_pack, read_source_snippets,
        )
        from simple_ar.code_task.analysis.source_context import requested_source_context
        from simple_ar.code_task.runtime.state import code_task_paths
        from simple_ar.core.artifacts import write_jsonl
        with tempfile.TemporaryDirectory() as folder:
            root, project = Path(folder), Path(folder) / 'project'
            (project / 'outputs').mkdir(parents=True)
            image = BytesIO()
            Image.new('RGB', (2, 2), 'red').save(image, format='PNG')
            (project / 'outputs/figure.png').write_bytes(image.getvalue())
            # A text extension must not bypass the content boundary either.
            (project / 'outputs/not_text.csv').write_bytes(image.getvalue())
            write_text(project / 'outputs/results.json', '{"score": 4}\n')
            write_text(project / 'outputs/values.csv', 'value\n4\n')
            diagrams = {'outputs/method.svg': '<svg xmlns="http://www.w3.org/2000/svg"><text>Method</text></svg>\n',
                        'outputs/method.dot': 'digraph method { input -> output; }\n',
                        'Dockerfile': 'FROM python:3.12\nCOPY analysis.py /app/\n',
                        'outputs/source.custom': 'A legitimate UTF-8 source: 方法\n'}
            for name, content in diagrams.items():
                write_text(project / name, content)
            text = 'LABEL = "alpha\u0085beta\u2028gamma\u2029delta"\n'
            write_text(project / 'analysis.py', text)
            (project / 'legacy.py').write_bytes('# coding: cp1252\nLABEL = "café"\n'.encode('cp1252'))
            task = root / 'task.md'
            write_text(task, 'Inspect analysis.py legacy.py outputs/figure.png outputs/results.json outputs/values.csv')
            initialize_code_task(run_dir=root / 'run', code_root=project, task_file=task,
                edit_scope_allowed_patterns=('analysis.py', 'legacy.py', 'outputs/**'))
            workspace = code_task_paths(root / 'run').workspace_dir
            index = build_codebase_index(workspace)
            self.assertIn('outputs/figure.png', [row['path'] for row in index['files']])
            selected = ['outputs/figure.png', 'outputs/not_text.csv', 'analysis.py', 'legacy.py',
                        'outputs/results.json', 'outputs/values.csv', *diagrams]
            snippets = read_source_snippets(workspace, selected, max_chars_per_file=2000)
            rows = {row['path']: row for row in snippets}
            self.assertNotIn('outputs/figure.png', rows)
            self.assertNotIn('outputs/not_text.csv', rows)
            self.assertEqual(rows['analysis.py']['text'], text)
            self.assertIn('café', rows['legacy.py']['text'])
            self.assertIn('outputs/results.json', rows)
            self.assertIn('outputs/values.csv', rows)
            for name, content in diagrams.items():
                self.assertEqual(rows[name]['text'], content)
            lookup = requested_source_context(workspace, index, {'files': selected}, supplied=[],
                max_files=8, max_chars=2000, max_total_chars=10000)
            self.assertFalse(any(row['path'].endswith('figure.png') or row['path'].endswith('not_text.csv')
                                 for row in lookup))
            pack = build_code_task_context_pack(root / 'run', max_files=8, max_source_chars_per_file=2000,
                max_total_chars=10000)
            self.assertNotIn('outputs/figure.png', pack.selected_files)
            saved = read_jsonl(pack.snippets_path)
            self.assertTrue(any(row['path'] == 'analysis.py' and '\u0085' in row['text'] for row in saved))
            # Simulate the pre-fix cached context. Refresh must retain it as
            # evidence, not mutate its snippets or reuse its prompt garbage.
            write_jsonl(pack.snippets_path, [*saved, {'path': 'outputs/figure.png',
                'text': image.getvalue().decode('utf-8', errors='replace')}])
            write_text(pack.prompt_context_path, 'Old binary source preview')
            old_bytes = pack.snippets_path.read_bytes()
            self.assertIsNone(load_latest_code_task_context_pack(root / 'run'))
            fresh = ensure_code_task_context_pack(root / 'run', query=task.read_text(), max_files=8,
                max_source_chars_per_file=2000)
            self.assertIsNotNone(fresh)
            self.assertNotEqual(fresh.context_pack_path, pack.context_pack_path)
            self.assertNotIn('outputs/figure.png', fresh.selected_files)
            # Cached editable vector/code sources remain eligible regardless
            # of their extension; they must not cause perpetual rebuilding.
            fresh_rows = list(fresh.snippets)
            for name, content in diagrams.items():
                if not any(row['path'] == name for row in fresh_rows):
                    fresh_rows.append({'path': name, 'text': content})
            write_jsonl(fresh.snippets_path, fresh_rows)
            self.assertEqual(load_latest_code_task_context_pack(root / 'run').context_pack_path,
                             fresh.context_pack_path)
            self.assertEqual(pack.snippets_path.read_bytes(), old_bytes)
            self.assertEqual(pack.prompt_context_path.read_text(), 'Old binary source preview')
            self.assertNotIn('Old binary source preview', fresh.prompt_context_path.read_text())

    def test_review_blocks_cross_file_api_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_text(project / "main.py", "from pkg.runner import run_experiment\n")
            write_text(project / "pkg" / "__init__.py", "")
            write_text(
                project / "pkg" / "data.py",
                "def load_text_classification_splits(config_path=None):\n    return [], {}\n",
            )
            write_text(
                project / "pkg" / "runner.py",
                (
                    "from . import data as data_module\n\n"
                    "def run_experiment():\n"
                    "    return data_module.load_text_classification_dataset()\n"
                ),
            )
            artifacts = {
                "generated_files": [
                    {"path": "main.py", "mode": "llm", "line_count": 1},
                    {"path": "pkg/__init__.py", "mode": "llm", "line_count": 1},
                    {"path": "pkg/data.py", "mode": "llm", "line_count": 2},
                    {"path": "pkg/runner.py", "mode": "llm", "line_count": 4},
                ]
            }

            mismatches = find_local_api_mismatches(project)
            review = generated_review(project, artifacts["generated_files"],
                resource_plan={"max_files": 8, "max_generated_lines": 200},
            )

            self.assertEqual(mismatches[0]["missing_symbol"], "load_text_classification_dataset")
            self.assertEqual(review["status"], "failed")
            self.assertTrue(any(row["category"] == "missing_local_api" for row in review["findings"]))

    def test_star_reexports_and_loaded_submodules_are_not_missing_apis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_text(project / "lib" / "__init__.py", "from .util import *\n")
            write_text(project / "lib" / "util.py", "Thing = int\n")
            write_text(project / "lib" / "deep.py", "def run():\n    return 1\n")
            write_text(
                project / "caller.py",
                "import lib\nimport lib.deep\nfrom lib import Thing\n"
                "result = lib.deep.run()\nvalue = lib.Thing(1)\n",
            )
            self.assertEqual(find_local_api_mismatches(project), [])

            # Possible module bindings include branch-local imports/exports.
            # Python scope, not package names or task exceptions, owns this.
            write_text(project / "bridge.py",
                "try:\n    from collections.abc import Mapping as MappingAlias\n"
                "except ImportError:\n    MappingAlias = dict\n"
                "if True:\n    class BranchType:\n        private_member = 1\n"
                "    def branch_function():\n        private_local = 1\n"
                "else:\n    alternative = 2\n"
                "left, right = (1, 2)\nimport os.path\n")
            write_text(project / "dynamic.py", "def __getattr__(name):\n    return name\n")
            write_text(project / "conditional.py", "if True:\n    from lib.util import *\n")
            write_text(project / "consumer.py",
                "from bridge import MappingAlias, BranchType, branch_function, alternative, left, right, os\n"
                "from bridge import private_member, private_local, missing, path\n"
                "from dynamic import runtime_name\nfrom conditional import Thing\n")
            with patch('importlib.import_module', side_effect=AssertionError('Do not execute user code')):
                findings = find_local_api_mismatches(project)
            self.assertEqual({row['missing_symbol'] for row in findings},
                             {'private_member', 'private_local', 'missing', 'path'})

    def test_review_warns_when_planned_public_api_is_not_exported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_text(project / "main.py", "def main():\n    print('score: 1.0')\n")
            write_text(project / "pkg" / "__init__.py", "")
            write_text(project / "pkg" / "data.py", "def load_rows():\n    return []\n")

            review = generated_review(project,
                [
                    {"path": "main.py", "mode": "llm", "line_count": 2},
                    {"path": "pkg/__init__.py", "mode": "llm", "line_count": 1},
                    {"path": "pkg/data.py", "mode": "llm", "line_count": 2},
                ],

                architecture_plan={
                    "files": [
                        {"path": "main.py", "public_api": ["main(argv=None)"]},
                        {"path": "pkg/data.py", "public_api": ["load_dataset(config)"]},
                    ]
                },
                resource_plan={"max_files": 8, "max_generated_lines": 200},
            )

            self.assertTrue(any(row["category"] == "planned_api_not_exported" for row in review["findings"]))
            self.assertTrue(all(row["severity"] != "blocking" for row in review["findings"] if row["category"] == "planned_api_not_exported"))

    def test_review_does_not_block_defensive_placeholder_policy_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_text(
                project / "main.py",
                (
                    "def main():\n"
                    "    # Refusing to emit placeholder metrics keeps failed benchmark paths honest.\n"
                    "    print('accuracy: 0.91')\n"
                ),
            )

            review = generated_review(project, [{"path": "main.py", "mode": "llm", "line_count": 3}],
                result_schema={"primary_metric": "accuracy", "required_metrics": ["accuracy"]},
                contract={"objective": "Run an experiment and evaluate metrics without placeholder values."},
            )

            self.assertFalse(any(row["category"] == "placeholder_execution_path" for row in review["findings"]))

    def test_review_blocks_mixed_llm_and_core_fallback_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            write_text(project / "main.py", "def main():\n    return 0\n")
            write_text(project / "pkg" / "models.py", "# Reserved generated module.\n")
            review = generated_review(project,
                [
                    {"path": "main.py", "mode": "llm", "line_count": 2},
                    {"path": "pkg/models.py", "mode": "fallback", "line_count": 1},
                ],
                result_schema={}, resource_plan={"max_files": 8, "max_generated_lines": 200},
            )

            self.assertEqual(review["status"], "failed")
            self.assertTrue(any(row["category"] == "mixed_generation_fallback" for row in review["findings"]))

    def _task_surface_review_inputs(self, project: Path) -> dict:
        write_text(
            project / "main.py",
            "from pathlib import Path\n"
            "class ExperimentSummary:\n    pass\n"
            "def final_metrics_from_evaluation():\n    return {'best_score': 1.0}\n"
            "def main(output_root):\n"
            "    for stem, suffix in [('results', 'json'), ('report', 'md'), ('condition_results', 'jsonl')]:\n"
            "        (Path(output_root) / (stem + '.' + suffix)).write_text('{}')\n"
            "    return {'best_score': 1.0, 'task_count': 1}\n",
        )
        return {
            "project_dir": project,
            "code_artifacts": {"generated_files": [{"path": "main.py", "mode": "llm", "line_count": 10}]},
            "result_schema": {"required_metrics": ["best_score", "task_count"]},
            "resource_plan": {"max_files": 4, "max_generated_lines": 200},
            "dependency_advice": {
                "packages": [{"package": "sample-lib", "import_name": "sample_lib", "status": "installed"}],
            },
            "use_llm": False,
        }

    def test_review_does_not_infer_task_acceptance_from_prose(self) -> None:
        tasks = {
            "legacy_phrasing": (
                "Create README.md, self-check, list-datasets, list-models, --mode report; "
                "prefer sample-lib when available; at least two tasks; single authoritative reporting; "
                "artifacts/results.json, artifacts/report.md, artifacts/condition_results.jsonl."
            ),
            "arbitrary_phrasing": (
                "Expose inspect-corpus and summarize-run; write exports/findings.ndjson; "
                "use another-library if installed; run a varied suite."
            ),
            "negation": (
                "Do not add self-check, list-datasets, list-models, or --mode report. "
                "README.md and artifacts/results.json are obsolete examples. "
                "Avoid sample-lib even when available; do not require multiple tasks."
            ),
            "indirect_paths": (
                "Write artifacts/results.json, artifacts/report.md, artifacts/condition_results.jsonl "
                "by joining the caller's output directory with computed stems and extensions."
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            inputs = self._task_surface_review_inputs(Path(tmp))
            for name, task in tasks.items():
                with self.subTest(name=name):
                    review = review_generated_project(**inputs, contract={"task": task})
                    self.assertFalse(review["findings"])
                    self.assertEqual(review["metadata"]["task_acceptance"], "not_verified")
                    self.assertEqual(review["metadata"]["deterministic_review_scope"],
                                     "structural_checks_and_scaffold_risks")
            self.assertTrue(is_current_greenfield_review(review))
            previous = dict(review, metadata=dict(review["metadata"],
                            review_contract_version=GREENFIELD_REVIEW_CONTRACT_VERSION - 1))
            saved = json.dumps(previous, sort_keys=True)
            self.assertFalse(is_current_greenfield_review(previous))
            self.assertEqual(json.dumps(previous, sort_keys=True), saved)

    def test_review_still_blocks_missing_structured_obligation_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inputs = self._task_surface_review_inputs(Path(tmp))
            review = review_generated_project(
                **inputs,
                contract={"implementation_contract": {"obligations": [{
                    "id": "export-findings", "requirement": "Export the findings.",
                    "owner_files": ["exporter.py"],
                }]}},
            )
            self.assertEqual(review["status"], "failed")
            self.assertEqual(
                [(row["category"], row["severity"]) for row in review["findings"]],
                [("implementation_obligation_owner_missing", "blocking")],
            )

    def test_review_passes_original_requirements_to_advisory_llm(self) -> None:
        tail = "Do not publish exports/findings.ndjson until all configured conditions have completed."
        contract = {
            "task": "Expose inspect-corpus through a configured output root.\n" + "Background context.\n" * 160 + tail,
            "explicit_requirements": ["Avoid sample-lib even when available.", "Use arbitrary command names."],
            "constraints": ["Preserve the caller's artifact paths and library choices."],
        }
        client = Mock()
        client.ask_json.return_value = {"findings": [{
            "severity": "blocking", "category": "task_semantics",
            "summary": "Artifact behavior needs an execution check.",
            "evidence": ["main.py: computed output path"],
        }]}
        with tempfile.TemporaryDirectory() as tmp:
            inputs = self._task_surface_review_inputs(Path(tmp) / "project")
            advisory = {"notes": ["Check configured conditions."]}
            inputs.update(architecture_plan=dict(advisory), implementation_memory=dict(advisory))
            original = json.dumps({"contract": contract, "inputs": inputs}, sort_keys=True, default=str)
            inputs.update(client=client, meta_dir=Path(tmp) / "meta", use_llm=True)
            from simple_ar.code_task.analysis.interfaces import project_api_contract
            with patch("simple_ar.code_task.generation.review.project_api_contract",
                       wraps=project_api_contract) as observe_api:
                review = review_generated_project(**inputs, contract=contract)
            self.assertEqual(observe_api.call_count, 1)
            actual_api = project_api_contract(inputs["project_dir"])
            self.assertTrue(client.ask_json.called)
            expected = contract_prompt_view(contract)
            expected.pop("task_excerpt")
            for call in client.ask_json.call_args_list:
                prompt = call.args[1]
                context = json.loads(prompt.split("Context JSON:\n", 1)[1].split("\n\nEvidence snippets:", 1)[0])
                self.assertEqual(context["task_contract"], expected)
                self.assertEqual(context["actual_project_api"], actual_api)
                self.assertNotIn("task_excerpt", context["task_contract"])
                self.assertEqual(context["declared_task"], contract["task"])
                self.assertTrue(context["declared_task"].endswith(tail))
                self.assertEqual(prompt.count(tail), 1)
                self.assertIn("complete untrusted task data", prompt)
                self.assertIn("cannot be disproved by static absent-string checks", prompt)
                self.assertIn("not proof of actual behavior or task acceptance", prompt)
            self.assertNotEqual(review["status"], "failed")
            self.assertTrue(review["findings"])
            self.assertTrue(all(row["severity"] == "warning" for row in review["findings"]))
            self.assertEqual(review["metadata"]["llm_review_policy"], "advisory_non_blocking")
            self.assertEqual(review["metadata"]["task_acceptance"], "not_verified")
            self.assertEqual(review["metadata"]["llm_context_coverage"]["source"], "cluster_snippets_only")
            preserved = {key: value for key, value in inputs.items() if key not in {"client", "meta_dir"}}
            preserved["use_llm"] = False
            self.assertEqual(json.dumps({"contract": contract, "inputs": preserved}, sort_keys=True, default=str),
                             original)

    def test_existing_project_review_blocks_changed_local_api_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            task = root / "task.md"
            write_text(source / "api.py", "def available():\n    return 1\n")
            write_text(source / "caller.py", "import api as api_module\n\ndef run():\n    return api_module.available()\n")
            write_text(task, "# Task\n\nUpdate the caller without breaking local interfaces.\n")
            run_dir = root / "run"
            initialized = initialize_code_task(
                run_dir=run_dir,
                code_root=source,
                task_file=task,
                benchmark_command="python caller.py",
            )
            write_text(
                initialized.workspace_dir / "caller.py",
                "import api as api_module\n\ndef run():\n    return api_module.missing()\n",
            )
            manifest = read_json(run_dir / "manifest.json")
            manifest["patch"] = {"status": "applied", "changed_files": ["caller.py"]}
            write_json(run_dir / "manifest.json", manifest)

            result = review_code_task_changes(run_dir, use_llm=False)
            report = read_json(result.report_path)

            self.assertEqual(result.status, "failed")
            self.assertTrue(any(row["category"] == "interface_compatibility" for row in report["findings"]))


class LocalImportTests(unittest.TestCase):
    """Static local-interface discovery without importing the project."""

    def project(self, directory):
        root = Path(directory) / 'project'
        root.mkdir()
        sources = {
            'src/acme/__init__.py': '',
            'src/acme/entry.py': 'from .worker import transform\nfrom . import settings\nimport numpy\n',
            'src/acme/worker.py': 'from . import hidden\ndef transform(x):\n    return x\n',
            'src/acme/settings.py': 'SCALE = 1\n',
            'src/acme/hidden.py': 'VALUE = 2\n',
            'noise.py': 'def entry_transform():\n    return "noise"\n',
            'src.py': 'import acme.worker as worker\n',
        }
        for name, content in sources.items():
            write_text(root / name, content)
        return root

    def test_exact_relative_references_do_not_import_project_or_external_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.project(directory)
            with patch('importlib.import_module', side_effect=AssertionError('No imports')):
                index = build_codebase_index(root)
                mapping = build_repo_map(index)
            entry = next(row for row in index['files'] if row['path'] == 'src/acme/entry.py')
            self.assertEqual(entry['python']['imports'], ['numpy', 'worker'])  # Historical top-level summary unchanged.
            refs = entry['python']['import_references']
            self.assertIn({'module': 'worker', 'level': 1, 'names': ['transform']}, refs)
            rows = {row['path']: row for row in mapping['files']}
            self.assertEqual(rows['src/acme/entry.py']['local_import_paths'],
                ['src/acme/worker.py', 'src/acme/__init__.py', 'src/acme/settings.py'])
            self.assertEqual(rows['src.py']['local_import_paths'], ['src/acme/worker.py'])
            self.assertNotIn('local_import_paths', rows['noise.py'])

    def test_ambiguous_root_src_modules_and_escape_are_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.project(directory)
            write_text(root / 'acme/worker.py', 'VALUE = "alternative"\n')
            write_text(root / 'acme/escape.py', 'from ...outside import value\n')
            rows = {row['path']: row for row in build_repo_map(build_codebase_index(root))['files']}
            self.assertNotIn('local_import_paths', rows['src.py'])
            self.assertNotIn('local_import_paths', rows['acme/escape.py'])
            self.assertIn('src/acme/worker.py', rows['src/acme/entry.py'].get('local_import_paths', []))
            self.assertNotIn('acme/worker.py', rows['src/acme/entry.py'].get('local_import_paths', []))

    def test_legacy_index_keeps_no_guessed_association(self):
        with tempfile.TemporaryDirectory() as directory:
            index = build_codebase_index(self.project(directory))
            for row in index['files']:
                row.get('python', {}).pop('import_references', None)
            self.assertFalse(any(row.get('local_import_paths') for row in build_repo_map(index)['files']))

    def test_explicit_targets_required_evidence_then_one_hop_reading_with_original_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            root = self.project(directory)
            write_text(root / 'conditions.toml', 'scale = 1\n')
            write_text(directory / 'task.md', 'Fix entry_transform in noise.py')
            run = directory / 'run'
            initialize_code_task(run_dir=run, code_root=root, task_file=directory / 'task.md',
                benchmark_command='python -m unittest', edit_scope_allowed_patterns=('*.py',),
                edit_scope_protected_patterns=('src/acme/worker.py',))
            pack = build_code_task_context_pack(run, max_files=3,
                preferred_paths=('src/acme/entry.py',), max_total_chars=300, max_source_chars_per_file=100)
            self.assertEqual(pack.selected_files, ('src/acme/entry.py', 'src/acme/worker.py', 'src/acme/__init__.py'))
            rows = read_jsonl(pack.snippets_path)
            self.assertEqual(rows[1]['access_role'], 'read_only_evidence')
            self.assertTrue(any('static local import' in reason for reason in rows[1]['reasons']))
            self.assertNotIn('src/acme/hidden.py', pack.selected_files)  # No transitive expansion.
            self.assertLessEqual(sum(row['chars'] for row in rows), 300)


if __name__ == "__main__":
    unittest.main()
