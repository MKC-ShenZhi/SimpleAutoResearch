import sys
import tempfile
import unittest
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from simple_ar.app.research_execution import (
    execution_request,
    execution_protocol,
    merge_execution_protocol,
    normalize_execution_config,
    code_task_validation,
    implementation_request,
)
from simple_ar.integrations.llm import LLMClient, LLMSettings
from simple_ar.research.task_plan import (
    TaskPlanResult,
    TaskPlanStep,
    TaskPlanRequest,
    append_research_followup,
    action_binding,
    build_task_plan,
    default_task_steps,
    insert_evidence_followup,
    _llm_prompt,
)
from simple_ar.research.workflow_contracts import ResearchBrief


class TaskPlanTests(unittest.TestCase):
    def test_reproduction_code_task_requires_independent_prepared_validation(self):
        execution = {"command": ["python", "formal.py"], "protocol": {
            "hypothesis": "One author claim", "dataset": "Fixed author data",
            "expected_outcome": "The declared metric criterion"}, "code_task": {
            "code_root": str(Path.cwd()), "approval_note": "Adapt results only",
            "validation_command": ["python", "tests/check.py"], "validation_timeout_sec": 10}}
        request = TaskPlanRequest(task_kind="reproduction", goal="Check a fixed claim",
            request_text="Check a fixed claim", requested_outputs=("experiments",),
            config={"research_materials_only": True, "research_local_documents": ["paper.md"]},
            execution=execution)
        plan = build_task_plan(request)
        self.assertEqual([step.action for step in plan.steps],
            ["document_ingest", "read", "synthesize", "prepare_execution", "implement", "experiment", "analysis"])
        self.assertEqual(TaskPlanResult.from_handoff_dict(plan.to_handoff_dict()), plan)
        argv, timeout = code_task_validation(execution, required=True)
        self.assertEqual(argv, (sys.executable, "tests/check.py"))
        self.assertEqual(timeout, 10)
        for changes in ({"validation_command": None}, {"validation_timeout_sec": True},
                        {"validation_command": ["python", "formal.py"]}, {"env_mode": "venv"},
                        {"approval_note": ""}):
            invalid = {**execution, "code_task": {**execution["code_task"], **changes}}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_task_plan(replace(request, execution=invalid))
        for invalid in ({**execution, "environment": {}}, {**execution, "protocol": {}}):
            with self.assertRaises(ValueError):
                code_task_validation(invalid, required=True)
        alias = {**execution, "command": [sys.executable, "tests/check.py"]}
        with self.assertRaisesRegex(ValueError, "differ"):
            code_task_validation(alias, required=True)

    def test_material_writing_plan_requires_no_fake_synthesis_or_execution(self):
        request = TaskPlanRequest(task_kind="writing", goal="Write from existing results", request_text="Write from existing results",
            requested_outputs=("report",), config={"research_materials_only": True, "research_local_documents": ["notes.md"]})
        plan = build_task_plan(request)
        self.assertEqual([step.action for step in plan.steps], ["document_ingest", "report_write", "report", "report_audit"])
        self.assertEqual(TaskPlanResult.from_handoff_dict(plan.to_handoff_dict()), plan)
        for changes in ({"config": {}}, {"execution": {"command": ["python", "run.py"]}},
                        {"requested_outputs": ("report", "experiments")}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_task_plan(replace(request, **changes))

    def test_prepared_reproduction_uses_evidence_and_fixed_protocol_not_innovation(self):
        request = TaskPlanRequest(task_kind="reproduction", goal="Reproduce one source result", request_text="Reproduce one source result",
            requested_outputs=("experiments", "report"), config={"research_materials_only": True, "research_local_documents": ["paper.pdf"]},
            execution={"command": [sys.executable, "experiment.py"], "protocol": {
                "hypothesis": "A source claim", "dataset": "Declared synthetic adaptation", "expected_outcome": "Coverage within declared tolerance"}})
        plan = build_task_plan(request)
        self.assertEqual([step.action for step in plan.steps], ["document_ingest", "read", "synthesize", "experiment", "analysis", "report_write", "report", "report_audit"])
        restored = TaskPlanResult.from_handoff_dict(plan.to_handoff_dict())
        self.assertEqual(restored, plan)
        for changes in ({"config": {"research_local_documents": ["paper.pdf"]}},
                        {"execution": {"command": ["python"], "protocol": {}}},
                        {"execution": {**request.execution, "baseline_policy": "run"}},
                        {"requested_outputs": ("report",)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_task_plan(replace(request, **changes))

    def test_planner_distinguishes_process_authority_from_available_work(self) -> None:
        survey = TaskPlanRequest(task_kind="survey", goal="Review evidence", request_text="Review evidence", requested_outputs=("report",))
        def boundary(request):
            return json.loads(_llm_prompt(request, default_task_steps(request)).split("\n\n", 1)[1])
        data = boundary(survey)
        self.assertEqual(data["planning_boundary"]["allowed_process_steps"], [])
        self.assertIn("read", data["available_non_process_actions"])
        self.assertIn("report_audit", data["available_non_process_actions"])
        self.assertIsNone(data["stop_after_action"])
        self.assertNotIn("checkpoint", data["planning_boundary"])
        measurement = replace(survey, task_kind="measurement", requested_outputs=("experiments",), execution={"command": [sys.executable, "-V"], "cwd": str(Path.cwd()), "timeout_sec": 5})
        measured = boundary(measurement)
        self.assertEqual(measured["available_non_process_actions"], ["analysis"])
        self.assertIsNone(measured["stop_after_action"])
        research = boundary(replace(measurement, task_kind="research"))
        self.assertEqual(research["stop_after_action"], "research_design")
        self.assertNotIn("experiment", research["available_non_process_actions"])

    def test_single_corrected_response_wrapper_keeps_all_plan_checks(self) -> None:
        request = TaskPlanRequest(task_kind="survey", goal="Review evidence", request_text="Review evidence")
        valid = default_task_steps(request)
        invalid = [dict(row) for row in valid]
        invalid[-1]["condition"] = "run regardless of evidence"

        class Client:
            model = "wrapped-json-fixture"

            def __init__(self) -> None:
                self.calls = 0

            def ask_json(self, *_args, **_kwargs):
                self.calls += 1
                return {"steps": invalid} if self.calls == 1 else {"response": {"steps": valid}}

        client = Client()
        trace: list[dict] = []
        result = build_task_plan(replace(request, use_llm=True, llm_client=client), trace=trace)
        self.assertEqual([step.action for step in result.steps], [row["action"] for row in valid])
        self.assertEqual(client.calls, 2)
        self.assertEqual(trace[1]["normalized_from_wrapper"], "response")

        class AmbiguousClient:
            def ask_json(self, *_args, **_kwargs):
                return {"response": {"steps": valid}, "unrelated": True}

        with self.assertRaisesRegex(ValueError, "non-empty steps list"):
            build_task_plan(replace(request, use_llm=True, llm_client=AmbiguousClient()))

    def test_direct_measurement_uses_only_the_supplied_command(self) -> None:
        request = TaskPlanRequest(
            task_kind="measurement", goal="Measure an existing benchmark",
            request_text="Measure an existing benchmark", requested_outputs=("experiments",),
            execution={"command": [sys.executable, "-V"], "cwd": str(Path.cwd()), "timeout_sec": 5},
        )
        result = build_task_plan(request)
        self.assertEqual([step.action for step in result.steps], ["experiment", "analysis"])
        self.assertEqual(TaskPlanResult.from_handoff_dict(result.to_handoff_dict()).task_kind, "measurement")
        class Client:
            def ask_json(self, *_args, **_kwargs):
                return {"steps": [{"action": "search"}, {"action": "experiment"}, {"action": "analysis"}]}
        with self.assertRaisesRegex(ValueError, "Direct measurement requires exactly"):
            build_task_plan(replace(request, use_llm=True, llm_client=Client()))

    def test_execution_extension_keeps_model_chosen_provided_materials_route(self) -> None:
        request = TaskPlanRequest(
            task_kind="research", goal="Improve the supplied method.",
            request_text="Improve the supplied method.",
            requested_outputs=("experiments", "report"),
            config={"research_local_documents": ["paper.pdf"]},
            execution={"command": [sys.executable, "-V"],
                       "cwd": str(Path.cwd()), "timeout_sec": 5},
        )

        class Client:
            model = "fixture-planner"

            def ask_json(self, *_args, **_kwargs):
                return {"steps": [{"action": action} for action in (
                    "document_ingest", "read", "synthesize", "summarize",
                    "assess_ideas", "research_design",
                )]}

        prior = build_task_plan(replace(request, use_llm=True, llm_client=Client()))
        self.assertNotIn("search", [step.action for step in prior.steps])
        extended = build_task_plan(replace(
            request, execution_protocol_accepted=True, prior_plan=prior,
        ))
        actions = [step.action for step in extended.steps]
        self.assertEqual(actions[:len(prior.steps)], [step.action for step in prior.steps])
        self.assertNotIn("search", actions)
        self.assertEqual(actions.count("document_ingest"), 1)
        self.assertEqual(actions[-3:], ["report_write", "report", "report_audit"])
        self.assertIn("experiment", actions)

    def test_research_execution_resolves_bare_python_with_code_task_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = {"cwd": tmp, "timeout_sec": 5}
            cases = (
                ("bare-current", ["python", "-c", "print('ok')"], "current", None,
                 sys.executable),
                ("bare-external", ["python3", "-c", "print('ok')"], "external",
                 sys.executable, sys.executable),
                ("explicit-current", [sys.executable, "-V"], "current", None,
                 sys.executable),
                ("explicit-external", [sys.executable, "-V"], "external", sys.executable,
                 sys.executable),
                ("non-python", ["benchmark-tool", "--version"], "external",
                 sys.executable, "benchmark-tool"),
            )
            with patch("simple_ar.code_task.execution.environment.subprocess.run") as probe:
                for name, command, mode, executable, expected in cases:
                    with self.subTest(name=name):
                        config = {
                            **base,
                            "command": command,
                            "code_task": {
                                "env_mode": mode,
                                "python_executable": executable,
                            },
                        }
                        request = execution_request(config)
                        self.assertEqual(Path(request.run.command[0]), Path(expected))
                        self.assertEqual(request.run.command[1:], command[1:])

                pair_config = {
                    **base,
                    "pairs": [{
                        "seed": 0,
                        "candidate_command": ["python", "-c", "print('candidate')"],
                        "baseline_command": ["python3", "-c", "print('baseline')"],
                    }],
                    "code_task": {
                        "env_mode": "external",
                        "python_executable": sys.executable,
                    },
                }
                for condition, marker in (("candidate", "candidate"), ("baseline", "baseline")):
                    with self.subTest(condition=condition):
                        request = execution_request(pair_config, condition=condition)
                        self.assertEqual(Path(request.run.command[0]), Path(sys.executable))
                        self.assertIn(marker, request.run.command[-1])
                probe.assert_not_called()

    def test_fixed_command_seed_is_preserved_without_inventing_pairs(self):
        from simple_ar.experiment.execution.backend import RunResult
        from simple_ar.experiment.execution.results import build_canonical_results
        for argv in (["measure.py", "--rng", "0"], ["measure.py", "--rng=0"]):
            config = {"command": argv, "seed_flag": "--rng",
                      "cwd": str(Path.cwd()), "timeout_sec": 5}
            normalized = normalize_execution_config(config)
            self.assertNotIn("pairs", normalized)
            self.assertEqual(execution_protocol(normalized)["seeds"], [0])
            self.assertEqual(normalize_execution_config(normalized), normalized)
            request = execution_request(normalized)
            self.assertEqual(request.run.command, argv)
            self.assertEqual(request.experiment_contract.comparison_conditions["seed"], 0)
            result = build_canonical_results(
                RunResult(0, False, "", "", command=argv),
                experiment_contract=request.experiment_contract.to_row(),
            )
            self.assertEqual(result["measurement"]["seed"], 0)
            self.assertEqual(result["measurement"]["protocol_status"], "incomplete")
        unbound = execution_request({"command": argv, "cwd": str(Path.cwd()), "timeout_sec": 5})
        self.assertIsNone(unbound.experiment_contract)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            execution_request({**config, "protocol": {"comparison_conditions": {"seed": 9}}})
        with self.assertRaisesRegex(ValueError, "repeated seed"):
            execution_request({**config, "command": ["measure.py", "--rng=0", "--rng", "1"]})

    def test_seed_expansion_replaces_existing_flag_instead_of_duplicating_it(self):
        normalized = normalize_execution_config({
            "command": ["measure.py", "--rng=0"], "seed_flag": "--rng", "seeds": [3],
            "cwd": str(Path.cwd()), "timeout_sec": 5,
        })
        request = execution_request(normalized)
        self.assertEqual(request.run.command, ["measure.py", "--rng=3"])
        self.assertEqual(request.experiment_contract.comparison_conditions["seed"], 3)

    def test_protocol_seed_batch_does_not_authorize_command_expansion(self):
        config = {"command": ["evaluate.py", "--folds", "5"],
                  "cwd": str(Path.cwd()), "timeout_sec": 5,
                  "protocol": {"comparison_conditions": {"seeds": [2, 7]}}}
        normalized = normalize_execution_config(config)
        self.assertNotIn("pairs", normalized)
        self.assertEqual(normalize_execution_config(normalized), normalized)
        request = execution_request(normalized)
        self.assertEqual(request.run.command, config["command"])
        self.assertEqual(request.experiment_contract.comparison_conditions["seeds"], [2, 7])
        view = execution_protocol(normalized)
        self.assertEqual((view["condition_count"], view["seeds"], view["paired"]), (1, [2, 7], False))

    def test_protocol_seeds_expand_only_with_explicit_binding(self):
        config = {"command": ["measure.py"], "seed_flag": "--rng",
                  "cwd": str(Path.cwd()), "timeout_sec": 5,
                  "protocol": {"comparison_conditions": {"seeds": [2, 7]}}}
        result = normalize_execution_config(config)
        self.assertEqual(result["pairs"][1]["candidate_command"], ["measure.py", "--rng", "7"])

    def test_declared_seed_batch_still_rejects_invalid_conditions(self):
        for seeds in ([1, 1], [False], [], ["1"]):
            with self.subTest(seeds=seeds), self.assertRaises(ValueError):
                normalize_execution_config({"protocol": {"comparison_conditions": {"seeds": seeds}}})

    def test_explicit_seed_count_cannot_be_silently_ignored_by_protocol(self):
        config = {"command": ["measure.py"], "seed_count": 2,
                  "protocol": {"comparison_conditions": {"seeds": [7, 9]}}}
        with self.assertRaisesRegex(ValueError, "seed_flag"):
            normalize_execution_config(config)

    def test_summary_capability_spelling_is_normalized_without_relaxing_boundaries(self) -> None:
        request = TaskPlanRequest(task_kind="survey", goal="Read papers", request_text="Read papers")
        class Client:
            def ask_json(self, *args, **kwargs):
                rows = default_task_steps(request)
                for row in rows:
                    if row["action"] == "summarize":
                        row["action"] = "summary"
                        row["capability"] = "invented_route"
                        row["state_name"] = "invented_state"
                return {"steps": rows}
        result = build_task_plan(replace(request, use_llm=True, llm_client=Client()))
        step = next(step for step in result.steps if step.action == "summarize")
        self.assertEqual((step.capability, step.state_name), ("summary", "summary"))
        self.assertEqual(TaskPlanResult.from_handoff_dict(result.to_handoff_dict()), result)
        corrupt = result.to_handoff_dict()
        corrupt["steps"][0]["capability"] = "invented_route"
        with self.assertRaisesRegex(ValueError, "boundary mismatch"):
            TaskPlanResult.from_handoff_dict(corrupt)
        rows = result.to_handoff_dict()
        rows["steps"][0]["action"] = "invented_action"
        with self.assertRaisesRegex(ValueError, "Unsupported task plan action"):
            TaskPlanResult.from_handoff_dict(rows)

    def test_compact_seed_protocol_is_explicit_and_budget_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {
                "command": [sys.executable, "measure.py"],
                "cwd": str(root),
                "timeout_sec": 5,
                "seed_flag": "--seed",
                "seeds": [0, 1],
                "baseline_policy": "skip",
            }
            normalized = normalize_execution_config(config)
            self.assertEqual([row["seed"] for row in normalized["pairs"]], [0, 1])
            self.assertEqual(normalized["pairs"][1]["candidate_command"][-2:], ["--seed", "1"])
            self.assertEqual(execution_protocol(config)["baseline_policy"], "skip")
            request = execution_request(normalized, pair_index=1)
            self.assertEqual(request.run.command[-2:], ["--seed", "1"])
            natural_only = normalize_execution_config({
                "command": config["command"], "cwd": config["cwd"], "timeout_sec": 5,
            }, task_text="Run two different seeds.")
            self.assertNotIn("pairs", natural_only)
            repeated = normalize_execution_config({
                **natural_only, "seeds": [0, 1], "seed_flag": "--seed",
                "protocol": {"comparison_conditions": {"split": "validation"}},
            })
            self.assertEqual(repeated["baseline_policy"], "skip")
            self.assertEqual(normalize_execution_config(repeated), repeated)
            nine = normalize_execution_config({
                **config, "seeds": list(range(9)),
            })
            self.assertEqual(len(nine["pairs"]), 9)
            with self.assertRaisesRegex(ValueError, "seed_flag"):
                normalize_execution_config({**config, "seed_flag": "", "seeds": [0, 1]})
            with self.assertRaisesRegex(ValueError, "run, skip or reuse"):
                normalize_execution_config({**config, "baseline_policy": "auto"})

    def test_resolved_run_materializes_a_baseline_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = {
                "command": [sys.executable, "measure.py"],
                "cwd": str(root),
                "timeout_sec": 5,
            }
            resolved = normalize_execution_config(merge_execution_protocol(
                raw, {"comparison_required": True, "baseline_policy": "run"},
            ))
            self.assertEqual(resolved["baseline"]["command"], raw["command"])
            result = build_task_plan(TaskPlanRequest(
                task_kind="research", goal="Measure the fixture", request_text="Measure the fixture",
                requested_outputs=("experiments",),
                config={"research_materials_only": True, "research_local_documents": ["note.md"]},
                execution=resolved, execution_protocol_accepted=True,
            ))
            actions = [step.action for step in result.steps]
            self.assertLess(actions.index("baseline"), actions.index("experiment"))
            self.assertEqual(execution_request(resolved, condition="baseline").run.label, "baseline")
            for step in TaskPlanResult.from_handoff_dict(result.to_handoff_dict()).steps:
                self.assertEqual(action_binding(step.action), (step.capability, step.state_name))

    def test_model_can_omit_search_when_supplied_materials_satisfy_task(self) -> None:
        request = TaskPlanRequest(
            task_kind="survey", goal="Compare supplied notes", request_text="Only compare these notes",
            config={"research_local_documents": ["note.md"]},
        )
        class Client:
            def ask_json(self, *args, **kwargs):
                return {"steps": [row for row in default_task_steps(request) if row["action"] != "search"]}
        result = build_task_plan(replace(request, use_llm=True, llm_client=Client()))
        self.assertEqual(result.steps[0].action, "document_ingest")
        self.assertNotIn("input_names", result.steps[0].to_dict())

    def test_survey_rejects_dynamic_process_actions(self) -> None:
        request = TaskPlanRequest(task_kind="survey", goal="Read papers", request_text="Read papers")
        class Client:
            def ask_json(self, *args, **kwargs):
                return {"steps": default_task_steps(request) + [{"action": self.action}]}
        client = Client()
        for action in ("matrix_baseline_999", "matrix_candidate_0", "retest:1", "matrix_repair_1"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "authorized protocol"):
                client.action = action
                build_task_plan(replace(request, use_llm=True, llm_client=client))

    def test_deterministic_bug_plan_has_no_research_or_experiment_steps(self) -> None:
        request = TaskPlanRequest(
            task_kind="bug_fix",
            goal="Fix the scoped classifier bug.",
            request_text="Fix the scoped classifier bug.",
            requested_outputs=("bug_fix",),
            execution={"code_task": {"run_dir": "/prepared/run"}},
        )
        result = build_task_plan(request)

        self.assertEqual([step.action for step in result.steps], ["implement"])
        self.assertEqual({step.capability for step in result.steps}, {"implement"})
        self.assertEqual(result.mode, "deterministic")

        # The same authorized code deliverable can consume sources and write
        # its results under different entry labels, without inventing a study.
        for kind in ("bug_fix", "survey", "research"):
            for config, prefix in (({}, []), ({"research_materials_only": True,
                    "research_local_documents": ["guide.md"]}, ["document_ingest", "read"]),
                    ({"research_materials_only": False}, ["search", "document_ingest", "read"])):
                with self.subTest(kind=kind, config=config):
                    combined = build_task_plan(replace(request, task_kind=kind, config=config,
                        requested_outputs=("bug_fix", "report")))
                    self.assertEqual([step.action for step in combined.steps],
                        prefix + ["implement", "report_write", "report", "report_audit"])
                    self.assertEqual(TaskPlanResult.from_handoff_dict(combined.to_handoff_dict()), combined)
                    class Client:
                        def ask_json(self, *args, **kwargs):
                            return {"steps": [step.to_dict() for step in combined.steps
                                if step.action not in {"search", "document_ingest", "read"}]}
                    direct = build_task_plan(replace(request, task_kind=kind, config=config,
                        requested_outputs=("bug_fix", "report"), use_llm=True, llm_client=Client()))
                    self.assertEqual([step.action for step in direct.steps],
                        ["implement", "report_write", "report", "report_audit"])
                    if prefix and prefix[0] == "search":
                        extended = insert_evidence_followup(combined, round_index=1, read_state="read", queries=["official API behavior"])
                        self.assertLess([step.action for step in extended.steps].index("read_evidence:1"),
                            [step.action for step in extended.steps].index("implement"))
                        self.assertEqual(TaskPlanResult.from_handoff_dict(extended.to_handoff_dict()), extended)

    def test_llm_plan_is_validated_before_it_can_be_consumed(self) -> None:
        class Client:
            model = "fixture-task-planner"

            def ask_json(self, _system, _user, *, label="", **kwargs):
                self.label = label
                self.tokens = kwargs["max_output_tokens"]
                return {"steps": default_task_steps(self.request)}

        request = TaskPlanRequest(
            task_kind="survey",
            goal="Survey bounded agent evaluation.",
            request_text="Survey bounded agent evaluation.",
            requested_outputs=("research_summary",),
        )
        client = Client()
        client.request = request
        request = TaskPlanRequest(
            task_kind=request.task_kind,
            goal=request.goal,
            request_text=request.request_text,
            requested_outputs=request.requested_outputs,
            use_llm=True,
            llm_client=client,
        )
        result = build_task_plan(request)

        self.assertEqual(result.mode, "llm")
        self.assertEqual(result.model, "fixture-task-planner")
        self.assertEqual(client.label, "task-plan")
        self.assertIsNone(client.tokens)
        self.assertNotIn("experiment", [step.action for step in result.steps])
        from dataclasses import replace

        build_task_plan(replace(request, config={"research_task_planning_max_output_tokens": 2048}))
        self.assertEqual(client.tokens, 2048)

    def test_incomplete_llm_sequence_is_compiled_with_trace(self) -> None:
        request = TaskPlanRequest(
            task_kind="research",
            goal="Compare a supplied classifier.",
            request_text="Compare a supplied classifier.",
            requested_outputs=("experiments",),
            hard_constraints=("Use only the supplied benchmark.",),
            preferences=("Keep the plan short.",),
            config={
                "research_materials_only": True,
                "research_local_documents": ["notes.md"],
            },
            execution={"command": [sys.executable, "benchmark.py"]},
        )

        class Client:
            model = "fixture-invalid-planner"
            calls = 0

            def ask_json(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 2:
                    return {"steps": default_task_steps(request)}
                return {
                    "steps": [
                        {"action": "document_ingest"},
                        {"action": "read"},
                        {"action": "assess_ideas"},
                        {"action": "research_design"},
                    ]
                }

        client = Client()
        trace = []
        result = build_task_plan(replace(request, use_llm=True, llm_client=client), trace=trace)

        self.assertEqual(result.mode, "llm")
        self.assertEqual(client.calls, 1)
        self.assertEqual(trace[0]["compiler_added_actions"], ["synthesize"])
        self.assertTrue(result.diagnostics)
        self.assertIn("Use only the supplied benchmark.", result.assumptions)
        actions = [step.action for step in result.steps]
        self.assertLess(actions.index("synthesize"), actions.index("assess_ideas"))
        self.assertLess(actions.index("assess_ideas"), actions.index("research_design"))

    def test_research_plan_compiles_missing_evidence_chain_without_inventing_processes(self) -> None:
        request = TaskPlanRequest(
            task_kind="research", goal="Improve a supplied project", request_text="Improve a supplied project",
            requested_outputs=("experiments", "report"),
            execution={"command": [sys.executable, "benchmark.py"]},
        )

        class Client:
            model = "fixture-incomplete-planner"
            calls = 0

            def ask_json(self, *_args, **_kwargs):
                self.calls += 1
                return {"steps": [{"action": "research_design"}]}

        client = Client()
        trace = []
        result = build_task_plan(replace(request, use_llm=True, llm_client=client), trace=trace)
        actions = [step.action for step in result.steps]
        self.assertEqual(client.calls, 1)
        self.assertEqual(actions, [
            "search", "document_ingest", "read", "synthesize", "assess_ideas", "research_design",
        ])
        self.assertEqual(trace[0]["response"]["steps"], [{"action": "research_design"}])
        self.assertEqual(set(trace[0]["compiler_added_actions"]), set(actions[:-1]))
        self.assertIn("Plan compiler supplied", result.diagnostics[0])
        self.assertNotIn("experiment", actions)
        self.assertNotIn("report", actions)

    def test_compiler_preserves_supplied_materials_boundary_and_rejects_missing_assets(self) -> None:
        class Client:
            calls = 0

            def ask_json(self, *_args, **_kwargs):
                self.calls += 1
                return {"steps": [{"action": "research_design"}]}

        supplied = TaskPlanRequest(
            task_kind="research", goal="Use supplied evidence", request_text="Use supplied evidence",
            requested_outputs=("experiments",),
            config={"research_materials_only": True, "research_local_documents": ["note.md"]},
            execution={"command": [sys.executable, "benchmark.py"]},
        )
        result = build_task_plan(replace(supplied, use_llm=True, llm_client=Client()))
        self.assertEqual(result.steps[0].action, "document_ingest")
        self.assertNotIn("search", [step.action for step in result.steps])

        missing = replace(supplied, config={"research_materials_only": True})
        client = Client()
        with self.assertRaisesRegex(ValueError, "requires supplied local documents"):
            build_task_plan(replace(missing, use_llm=True, llm_client=client))
        self.assertEqual(client.calls, 0)

        # The same compiler binds report inputs to real documents. Optional
        # notes still have their own dependencies; explicit old plans survive.
        class ReportClient:
            def __init__(self, actions):
                self.actions = actions

            def ask_json(self, *_args, **_kwargs):
                return {"steps": [{"action": action} for action in self.actions]}

        for kind in ("survey", "research"):
            for outputs in (("report",), ("paper",), ("full_paper",)):
                report = replace(supplied, task_kind=kind, requested_outputs=outputs, execution=None)
                for optional in ([], ["read"], ["synthesize"]):
                    with self.subTest(kind=kind, outputs=outputs, optional=optional):
                        trace = []
                        plan = build_task_plan(replace(report, use_llm=True,
                            llm_client=ReportClient(optional + ["report_audit"])), trace=trace)
                        actions = [step.action for step in plan.steps]
                        self.assertEqual(actions[0], "document_ingest")
                        self.assertEqual("read" in actions, bool(optional))
                        self.assertEqual("synthesize" in actions, "synthesize" in optional)
                        self.assertNotIn("search", actions)
                        self.assertEqual(TaskPlanResult.from_handoff_dict(plan.to_handoff_dict()), plan)
                for changes in ({"intents": ("assessment",)}, {"requested_outputs": ("report", "summary")}):
                    with self.subTest(changes=changes):
                        plan = build_task_plan(replace(report, **changes))
                        self.assertIn("synthesize", [step.action for step in plan.steps])
                for invalid in (["report_write", "document_ingest", "report", "report_audit"],
                                ["document_ingest", "report_write", "read", "report", "report_audit"],
                                ["document_ingest", "read", "report_write", "synthesize", "report", "report_audit"]):
                    with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "missing prerequisite"):
                        build_task_plan(replace(report, use_llm=True, llm_client=ReportClient(invalid)))
                discovered = replace(report, config={})
                direct = build_task_plan(discovered)
                self.assertEqual([step.action for step in direct.steps],
                    ["search", "document_ingest", "read", "report_write", "report", "report_audit"])
                compiled = build_task_plan(replace(discovered, use_llm=True,
                    llm_client=ReportClient(["report_audit"])))
                self.assertEqual([step.action for step in compiled.steps], [step.action for step in direct.steps])
                self.assertEqual(TaskPlanResult.from_handoff_dict(direct.to_handoff_dict()), direct)
                with self.assertRaisesRegex(ValueError, 'missing prerequisite'):
                    build_task_plan(replace(discovered, use_llm=True,
                        llm_client=ReportClient(["search", "document_ingest", "report_write", "read", "report", "report_audit"])))

    def test_compiler_does_not_reorder_explicit_steps_or_deliver_before_design(self) -> None:
        request = TaskPlanRequest(
            task_kind="research", goal="Measure a method", request_text="Measure a method",
            requested_outputs=("experiments", "report"),
            execution={"command": [sys.executable, "benchmark.py"]},
        )

        class Client:
            def __init__(self, steps):
                self.steps = steps

            def ask_json(self, *_args, **_kwargs):
                return {"steps": self.steps}

        reversed_steps = [
            {"action": "research_design"}, {"action": "search"},
            {"action": "document_ingest"}, {"action": "read"},
            {"action": "synthesize"}, {"action": "assess_ideas"},
        ]
        with self.assertRaisesRegex(ValueError, "missing prerequisite"):
            build_task_plan(replace(request, use_llm=True, llm_client=Client(reversed_steps)))

        early_delivery = [
            {"action": "search"}, {"action": "document_ingest"}, {"action": "read"},
            {"action": "synthesize"}, {"action": "assess_ideas"},
            {"action": "report_write"}, {"action": "report"}, {"action": "report_audit"},
            {"action": "research_design"},
        ]
        with self.assertRaisesRegex(ValueError, "Pre-design research plans must stop"):
            build_task_plan(replace(request, use_llm=True, llm_client=Client(early_delivery)))

    def test_requested_report_cannot_be_demoted_to_conditional_delivery(self) -> None:
        request = TaskPlanRequest(
            task_kind="survey", goal="Write a report", request_text="Write a report",
            requested_outputs=("report",),
        )

        class Client:
            def ask_json(self, *_args, **_kwargs):
                return {"steps": [
                    {"action": "report_audit", "condition": "on_request:report"},
                ]}

        with self.assertRaisesRegex(ValueError, "Requested report steps cannot be conditional"):
            build_task_plan(replace(request, use_llm=True, llm_client=Client()))

        design_and_report = replace(
            request, task_kind="research", requested_outputs=("research_design", "report"),
        )
        with self.assertRaisesRegex(ValueError, "Requested report steps cannot be conditional"):
            build_task_plan(replace(design_and_report, use_llm=True, llm_client=Client()))

        class AuditOnly:
            def ask_json(self, *_args, **_kwargs):
                return {"steps": [{"action": "report_audit"}]}

        compiled = build_task_plan(replace(design_and_report, use_llm=True, llm_client=AuditOnly()))
        actions = [step.action for step in compiled.steps]
        self.assertLess(actions.index("research_design"), actions.index("report_write"))
        self.assertEqual(actions[-1], "report_audit")

    def test_pre_design_process_proposals_report_all_deferred_boundary_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            execution = {
                "pairs": [{
                    "seed": 0,
                    "baseline_command": [sys.executable, "-c", "print('baseline')"],
                    "candidate_command": [sys.executable, "-c", "print('candidate')"],
                }],
                "cwd": tmp,
                "timeout_sec": 5,
                "code_task": {"code_root": tmp},
            }
            request = TaskPlanRequest(
                task_kind="research",
                goal="Compare the supplied fixture.",
                request_text="Compare the supplied fixture.",
                requested_outputs=("experiments",),
                config={
                    "research_materials_only": True,
                    "research_local_documents": ["fixture.md"],
                },
                execution=execution,
            )

            class Client:
                model = "fixture-boundary-planner"

                def __init__(self):
                    self.calls = 0
                    self.prompts = []

                def ask_json(self, _system, prompt, **_kwargs):
                    self.calls += 1
                    self.prompts.append(prompt)
                    if self.calls == 1:
                        return {"steps": [
                            {"action": "document_ingest"},
                            {"action": "read"},
                            {"action": "synthesize"},
                            {"action": "assess_ideas"},
                            {"action": "research_design"},
                            {"action": "prepare_execution"},
                            {"action": "implement"},
                            {"action": "summarize"},
                        ]}
                    return {"steps": [
                        {"action": "document_ingest"},
                        {"action": "read"},
                        {"action": "synthesize"},
                        {"action": "assess_ideas"},
                        {"action": "research_design"},
                    ]}

            client = Client()
            trace = []
            result = build_task_plan(
                replace(request, use_llm=True, llm_client=client), trace=trace,
            )

            self.assertEqual(client.calls, 2)
            self.assertEqual([step.action for step in result.steps][-1], "research_design")
            self.assertIn("category=not_ready", trace[0]["validation_error"])
            self.assertIn("action='prepare_execution'", trace[0]["validation_error"])
            self.assertIn("action='implement'", trace[0]["validation_error"])
            self.assertIn("research_design must produce the accepted execution protocol", trace[0]["validation_error"])
            self.assertIn('"protocol_accepted": false', client.prompts[0])
            self.assertIn('"deferred_process_steps"', client.prompts[0])
            self.assertIn('"suggested_steps"', client.prompts[0])
            self.assertNotIn('"default_steps"', client.prompts[0])

    def test_bug_plan_correction_is_bounded_and_records_rejected_proposals(self) -> None:
        request = TaskPlanRequest(task_kind="bug_fix", goal="Fix totals", request_text="Fix totals")
        class Client:
            calls = 0
            def ask_json(self, system, prompt, **kwargs):
                self.calls += 1
                if self.calls == 2:
                    assert "Bug-fix plans" in prompt
                return {"steps": [{"action": "summarize", "capability": "wrong"}]}
        client = Client()
        trace = []
        with self.assertRaisesRegex(ValueError, "after one correction"):
            build_task_plan(replace(request, use_llm=True, llm_client=client), trace=trace)
        self.assertEqual(client.calls, 2)
        self.assertEqual(len(trace), 2)
        self.assertTrue(all(row["validation_error"] for row in trace))
        self.assertEqual(trace[0]["response"]["steps"][0]["capability"], "wrong")
        from types import SimpleNamespace
        from simple_ar.core import ArtifactStore
        from simple_ar.research.planning.capability import ResearchPlanRequest, run_research_plan_capability
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "after one correction"):
                run_research_plan_capability(
                    context=SimpleNamespace(store=ArtifactStore(Path(tmp))),
                    request=ResearchPlanRequest(
                        topic="Fix totals", task_plan_only=True,
                        task_plan_request=replace(request, use_llm=True, llm_client=Client()),
                    ),
                )
            saved = json.loads((Path(tmp) / "task_plan_proposals.json").read_text())
            self.assertEqual(len(saved["proposals"]), 2)
            self.assertFalse((Path(tmp) / "task_plan.json").exists())

    def test_bug_plan_missing_implementation_keeps_complete_correction_feedback(self) -> None:
        request = TaskPlanRequest(task_kind="bug_fix", goal="Fix totals", request_text="Fix totals")

        class Client:
            calls = 0

            def ask_json(self, *_args, **_kwargs):
                self.calls += 1
                return {"steps": [{"action": "prepare_execution"}]}

        client = Client()
        trace = []
        with self.assertRaisesRegex(ValueError, "after one correction") as raised:
            build_task_plan(
                replace(request, use_llm=True, llm_client=client), trace=trace,
            )
        self.assertEqual(client.calls, 2)
        self.assertEqual(len(trace), 2)
        self.assertIn("Bug-fix plans may contain only preparation and implementation", trace[0]["validation_error"])
        self.assertNotIn("not in list", str(raised.exception))

    def test_revision_followup_can_route_bounded_technical_repairs(self) -> None:
        plan = TaskPlanResult(
            status="accepted", mode="deterministic", task_kind="research",
            goal="Continue a failed candidate measurement.",
            steps=(TaskPlanStep(
                step_id="design", action="research_design", capability="research_design",
                state_name="design", problem_solved="", observation="",
            ),),
        )
        extended = append_research_followup(
            plan, 1, action="revise_candidate", repair_count=2,
        )
        rows = {step.action: step for step in extended.steps}
        self.assertEqual(rows["repair_candidate:1_1"].condition,
                         "on_failure:experiment_revision_1")
        self.assertEqual(rows["retest_candidate:1_1"].condition,
                         "after_success:implementation_r1_repair_1")
        self.assertEqual(rows["repair_candidate:1_2"].condition,
                         "on_failure:experiment_revision_1_repair_1")
        self.assertEqual(rows["retest_candidate:1_2"].condition,
                         "after_success:implementation_r1_repair_2")
        self.assertEqual(rows["reanalysis:1"].condition,
                         "after_observation:experiment_revision_1")
        self.assertEqual(
            TaskPlanResult.from_handoff_dict(extended.to_handoff_dict()).steps,
            extended.steps,
        )
        for step in TaskPlanResult.from_handoff_dict(extended.to_handoff_dict()).steps:
            with self.subTest(action=step.action):
                self.assertEqual(action_binding(step.action), (step.capability, step.state_name))
        for action in ("unknown", "repair_candidate:1", "research_candidate:bogus", "search_evidence:2", "read_evidence:0"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "Unsupported task plan action"):
                action_binding(action)
        with self.assertRaisesRegex(ValueError, "not supported"):
            append_research_followup(
                plan, 1, action="revise_candidate", pair_count=1, repair_count=1,
            )

    def test_followup_persists_design_gate_and_multiple_supplement_pairs(self) -> None:
        plan = TaskPlanResult(
            status="accepted", mode="deterministic", task_kind="research",
            goal="Bounded continuation.",
            steps=(TaskPlanStep(
                step_id="design", action="research_design", capability="research_design",
                state_name="design", problem_solved="", observation="",
            ),),
        )
        revised = append_research_followup(plan, 1, action="revise_candidate", revision_base="baseline")
        self.assertEqual(revised.steps[1].action, "research_design_revision:1")
        self.assertEqual(revised.steps[2].condition, "after_success:design_revision_1")
        paired = append_research_followup(plan, 1, action="revise_candidate", pair_count=1)
        rows = {step.action: step for step in paired.steps}
        self.assertEqual(rows["research_candidate:1_0"].condition,
                         "after_success:implementation_r1")
        self.assertEqual(rows["reanalysis:1"].condition,
                         "after_observation:experiment_revision_1_0")
        supplemented = append_research_followup(plan, 2, supplement_count=2)
        actions = [step.action for step in supplemented.steps]
        self.assertEqual(actions[1:5], [
            "supplement_baseline:2_0", "supplement_baseline:2_1",
            "supplement_candidate:2_0", "supplement_candidate:2_1",
        ])
        self.assertEqual(actions[-1], "reanalysis:2")
        self.assertEqual(
            supplemented.steps[-1].condition,
            "after_observation:baseline_supplement_2_0",
        )
        for route in (revised, paired, supplemented):
            for step in TaskPlanResult.from_handoff_dict(route.to_handoff_dict()).steps:
                with self.subTest(action=step.action):
                    self.assertEqual(action_binding(step.action), (step.capability, step.state_name))
        invalid = supplemented.to_handoff_dict()
        invalid["steps"][-1]["condition"] = "after_observation:missing_experiment"
        with self.assertRaisesRegex(ValueError, "earlier experiment step"):
            TaskPlanResult.from_handoff_dict(invalid)


if __name__ == "__main__":
    unittest.main()
