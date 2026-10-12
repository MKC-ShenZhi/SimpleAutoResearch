from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
import sys

from simple_ar.core import ArtifactStore, AttemptManifest
from simple_ar.core.capabilities import CapabilityContext
from simple_ar.integrations.llm import LLMError
from simple_ar.research.contracts import (
    IdeaCandidate,
    NoveltyCheck,
    ResearchExperimentContract,
)
from simple_ar.research.design import (
    ResearchDesignRequest,
    ResearchDesignResult,
    build_research_design,
    run_research_design_capability,
)
from simple_ar.research.synthesis import SynthesisResult


class ResearchDesignTests(unittest.TestCase):
    _ALIGNED_REVIEW = {"verdict": "accept", "mechanism_alignment": "aligned",
                       "mechanism_rationale": "The edit preserves the selected intervention.",
                       "premise_verdict": "supported", "premise_rationale": "The observed producer and consumer agree.",
                       "blocking_issues": [], "delegated_checks": []}

    def test_initial_research_design_clarifies_method_before_code_task(self):
        from unittest.mock import Mock
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "model.py").write_text("def forward(x):\n    return x\n", encoding="utf-8")
            (workspace / "data.npy").write_bytes(b"not source")
            index = source_file_inventory(workspace)
            self.assertEqual([row["path"] for row in index["files"]], ["model.py"])
            client = Mock()
            client.ask_json.side_effect = [
                {"selected_idea_id": "idea-002", "rationale": "Test a bounded mechanism.",
                 "execution_protocol": {}},
                {"status": "ready", "implementation_spec":
                    "Observe a changed forward output on a small input; keep the accepted evaluator.",
                  "unresolved_questions": [], "target_paths": ["model.py"],
                  "code_task_questions": ["Check the input shape at the forward call site."],
                  "source_quotes": [{"path": "model.py", "quote": "return x"}]},
                {**self._ALIGNED_REVIEW,
                 "delegated_checks": ["Verify the output dtype with a small local probe."]},
            ]
            result = build_research_design(ResearchDesignRequest(
                synthesis=self._synthesis(), idea_id="idea-002", idea_id_is_fixed=False,
                execution_boundary={"code_task": {"code_root": str(workspace)}},
                source_workspace=workspace, source_index=index,
                use_llm=True, llm_client=client,
            ))
            self.assertEqual(result.status, "ready")
            self.assertIn("changed forward output", result.implementation_spec)
            self.assertEqual(result.code_task_questions,
                             ("Check the input shape at the forward call site.",
                              "Verify the output dtype with a small local probe."))
            self.assertEqual(ResearchDesignResult.from_handoff_dict(result.to_handoff_dict()).code_task_questions,
                             result.code_task_questions)
            self.assertIn("def forward", client.ask_json.call_args.args[1])
            refinement_prompt = client.ask_json.call_args_list[1].args[1]
            self.assertIn('"fixed_idea_id": ""', refinement_prompt)

    def test_initial_design_does_not_accept_missing_or_different_mechanism_review(self):
        from unittest.mock import Mock
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        for review in (
            {"verdict": "accept", "blocking_issues": [], "delegated_checks": []},
            {"verdict": "accept", "mechanism_alignment": "different",
             "mechanism_rationale": "The edit changes a different model behavior.",
             "blocking_issues": [], "delegated_checks": []},
            {"verdict": "accept", "mechanism_alignment": "aligned",
             "mechanism_rationale": "The intervention appears aligned.",
             "premise_verdict": "unknown", "premise_rationale": "The producer path is missing.",
             "blocking_issues": ["The producer path still needs inspection."], "delegated_checks": []},
        ):
            with self.subTest(review=review), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                (workspace / "model.py").write_text("def forward(x):\n    return x\n", encoding="utf-8")
                client = Mock()
                client.ask_json.side_effect = [
                    {"selected_idea_id": "idea-002", "rationale": "Test a bounded mechanism."},
                    {"status": "ready", "implementation_spec": "Change the observed forward output.",
                     "unresolved_questions": [], "target_paths": ["model.py"],
                     "source_quotes": [{"path": "model.py", "quote": "return x"}]},
                    review,
                    {"status": "blocked", "implementation_spec": "",
                     "unresolved_questions": ["Cannot preserve the chosen mechanism."]},
                ]
                result = build_research_design(ResearchDesignRequest(
                    synthesis=self._synthesis(), idea_id="idea-002", idea_id_is_fixed=False,
                    execution_boundary={"code_task": {"code_root": str(workspace)}},
                    source_workspace=workspace, source_index=source_file_inventory(workspace),
                    use_llm=True, llm_client=client,
                ))
                self.assertEqual(result.status, "blocked")
                self.assertEqual(client.ask_json.call_count, 4)
                self.assertIn(
                    "mechanism alignment" if not review.get("mechanism_alignment")
                    or review.get("mechanism_alignment") == "different"
                    else "producer path",
                    client.ask_json.call_args.args[1],
                )

    def test_core_premise_contradiction_cannot_be_delegated_across_project_types(self):
        from unittest.mock import Mock
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        for filename, source in (
            ("trainer.py", "def train(items):\n    return items[::-1]\n"),
            ("pipeline.ts", "function train(items) { return items.reverse(); }\n"),
        ):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                (workspace / filename).write_text(source, encoding="utf-8")
                quote = "return items[::-1]" if filename.endswith(".py") else "return items.reverse()"
                client = Mock()
                client.ask_json.side_effect = [
                    {"selected_idea_id": "idea-002", "rationale": "Try the selected direction."},
                    {"status": "ready", "implementation_spec": "Apply the candidate to matching items.",
                     "unresolved_questions": [], "target_paths": [filename],
                     "source_quotes": [{"path": filename, "quote": quote}]},
                    {"verdict": "revise", "mechanism_alignment": "aligned",
                     "mechanism_rationale": "The proposed edit is in scope.",
                     "premise_verdict": "contradicted",
                     "premise_rationale": "The observed consumer reverses the intended item identity.",
                     "blocking_issues": ["The candidate assumes matching items but the consumer reorders them."],
                     "delegated_checks": ["Check dtype before editing."]},
                    {"status": "blocked", "implementation_spec": "",
                     "unresolved_questions": ["The required item identity is not preserved."]},
                ]
                result = build_research_design(ResearchDesignRequest(
                    synthesis=self._synthesis(), idea_id="idea-002", idea_id_is_fixed=False,
                    execution_boundary={"code_task": {"code_root": str(workspace)}},
                    source_workspace=workspace, source_index=source_file_inventory(workspace),
                    use_llm=True, llm_client=client,
                ))
                self.assertEqual(result.status, "blocked")
                self.assertEqual(result.code_task_questions, ())
                self.assertIn("contradicted", client.ask_json.call_args.args[1])

    def test_delegated_code_questions_reach_existing_code_task_handoff(self):
        from simple_ar.research.implementation import ImplementationRequest, _prepare_research_task

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "code_task" / "task"
            task_dir.mkdir(parents=True)
            (task_dir / "task.md").write_text("# Original task\n", encoding="utf-8")
            store = ArtifactStore(root / "attempt")
            design = replace(
                build_research_design(ResearchDesignRequest(synthesis=self._synthesis())),
                implementation_spec="Change the observed forward path; validate its output.",
                code_task_questions=("Check helper keyword arguments before editing.",),
            )
            ref = store.write_json("inputs/design.json", design.to_handoff_dict(),
                                   kind="research_design", schema="research_design.v1", producer="test")
            context = CapabilityContext(store=store,
                attempt=AttemptManifest(attempt_id="implement-1", capability="implement"),
                inputs=(ref,))
            request = ImplementationRequest(root, root, "Authorized isolated edits", object())
            _prepare_research_task(context, request, task_dir)
            task = (task_dir / "task.md").read_text(encoding="utf-8")
            self.assertIn("Check helper keyword arguments before editing.", task)
            self.assertIn("return a design_gap instead of guessing", task)

    def test_initial_design_reads_active_config_and_rejects_protected_targets(self):
        from unittest.mock import Mock
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "model.py").write_text("def build(k):\n    return k\n", encoding="utf-8")
            (workspace / "experiment.toml").write_text("[model]\nk = 32\n", encoding="utf-8")
            client = Mock()
            client.ask_json.side_effect = [
                {"selected_idea_id": "idea-002", "rationale": "Try a bounded change."},
                {"status": "ready", "implementation_spec": "Change protected config only.",
                 "unresolved_questions": [], "target_paths": ["experiment.toml"],
                 "source_quotes": [{"path": "experiment.toml", "quote": "k = 32"}]},
                {"status": "ready", "implementation_spec": "Change model.py based on the active k=32.",
                 "unresolved_questions": [], "target_paths": ["model.py"],
                 "source_quotes": [{"path": "experiment.toml", "quote": "k = 32"},
                                   {"path": "model.py", "quote": "return k"}]},
                self._ALIGNED_REVIEW,
            ]
            result = build_research_design(ResearchDesignRequest(
                synthesis=self._synthesis(), idea_id="idea-002", idea_id_is_fixed=False,
                execution_boundary={"code_task": {
                    "code_root": str(workspace), "allowed_patterns": ["model.py"],
                    "protected_patterns": ["experiment.toml"]},
                    "protocol": {"comparison_conditions": {"source_config": "experiment.toml"}}},
                source_workspace=workspace, source_index=source_file_inventory(workspace),
                use_llm=True, llm_client=client,
            ))
            self.assertEqual(result.status, "ready")
            self.assertEqual(client.ask_json.call_count, 4)
            self.assertIn("k = 32", client.ask_json.call_args.args[1])
            self.assertIn("not editable", client.ask_json.call_args_list[2].args[1])

    def test_active_config_excerpt_does_not_force_a_format_only_design_rewrite(self):
        from unittest.mock import Mock
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "model.py").write_text(
                "def build(config):\n    return Model(**config['model'])\n", encoding="utf-8",
            )
            (workspace / "experiment.toml").write_text(
                "[model]\nshare_training_batches = false\n", encoding="utf-8",
            )
            client = Mock()
            client.ask_json.side_effect = [
                {"selected_idea_id": "idea-002", "rationale": "Try a bounded change."},
                {"status": "ready", "implementation_spec":
                    "Use one effective shared-batch value for model construction and training; "
                    "leave the accepted evaluator unchanged.",
                 "unresolved_questions": [], "target_paths": ["model.py"],
                 "source_quotes": [{"path": "model.py", "quote": "Model(**config['model'])"}]},
                self._ALIGNED_REVIEW,
            ]
            result = build_research_design(ResearchDesignRequest(
                synthesis=self._synthesis(), idea_id="idea-002", idea_id_is_fixed=False,
                execution_boundary={"code_task": {
                    "code_root": str(workspace), "allowed_patterns": ["model.py"],
                    "protected_patterns": ["experiment.toml"]},
                    "protocol": {"comparison_conditions": {"source_config": "experiment.toml"}}},
                source_workspace=workspace, source_index=source_file_inventory(workspace),
                use_llm=True, llm_client=client,
            ))
            self.assertEqual(result.status, "ready")
            self.assertEqual(client.ask_json.call_count, 3)
            self.assertIn("share_training_batches = false", client.ask_json.call_args_list[1].args[1])
            self.assertIn("unchanged evaluator", client.ask_json.call_args_list[2].args[1])

    def test_initial_design_revises_when_review_finds_dormant_edit(self):
        from unittest.mock import Mock
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "model.py").write_text("def build(k=5):\n    return k\n", encoding="utf-8")
            (workspace / "experiment.toml").write_text("[model]\nk = 32\n", encoding="utf-8")
            client = Mock()
            client.ask_json.side_effect = [
                {"selected_idea_id": "idea-002", "rationale": "Try a bounded change."},
                {"status": "ready", "implementation_spec": "Change the default k=5 to 3.",
                 "unresolved_questions": [], "target_paths": ["model.py"],
                 "source_quotes": [{"path": "experiment.toml", "quote": "k = 32"},
                                   {"path": "model.py", "quote": "def build(k=5)"}]},
                {"verdict": "revise", "mechanism_alignment": "different",
                 "mechanism_rationale": "The default is overridden by active configuration.",
                 "premise_verdict": "contradicted", "premise_rationale": "The active config overrides k.",
                 "blocking_issues": ["Changing the default is dormant: active config supplies k=32."],
                 "delegated_checks": []},
                {"status": "blocked", "implementation_spec": "", "unresolved_questions": [
                    "A config-only change is outside the authorized edit scope."]},
            ]
            result = build_research_design(ResearchDesignRequest(
                synthesis=self._synthesis(), idea_id="idea-002", idea_id_is_fixed=False,
                execution_boundary={"code_task": {"code_root": str(workspace),
                    "allowed_patterns": ["model.py"], "protected_patterns": ["experiment.toml"]},
                    "protocol": {"comparison_conditions": {"source_config": "experiment.toml"}}},
                source_workspace=workspace, source_index=source_file_inventory(workspace),
                use_llm=True, llm_client=client,
            ))
            self.assertEqual(result.status, "blocked")
            self.assertEqual(client.ask_json.call_count, 4)
            self.assertIn("dormant", client.ask_json.call_args_list[-1].args[1])

    def test_initial_design_blocks_unreadable_active_config(self):
        from unittest.mock import Mock
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "model.py").write_text("pass\n", encoding="utf-8")
            client = Mock()
            client.ask_json.return_value = {"selected_idea_id": "idea-002", "rationale": "Try it."}
            result = build_research_design(ResearchDesignRequest(
                synthesis=self._synthesis(), idea_id="idea-002", idea_id_is_fixed=False,
                execution_boundary={"code_task": {"code_root": str(workspace)},
                    "protocol": {"comparison_conditions": {"source_config": "missing.toml"}}},
                source_workspace=workspace, source_index=source_file_inventory(workspace),
                use_llm=True, llm_client=client,
            ))
            self.assertEqual(result.status, "blocked")
            self.assertIn("source config", result.diagnostics[0])

    def test_source_inventory_retains_declared_config_beyond_generic_limit(self):
        from simple_ar.code_task.analysis.source_context import requested_source_context, source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "model.py").write_text("pass\n", encoding="utf-8")
            (workspace / "experiment.toml").write_text("[model]\nk = 32\n", encoding="utf-8")
            (workspace / "z_worker.py").write_text("def compute():\n    return 42\n", encoding="utf-8")
            index = source_file_inventory(workspace, max_files=1,
                                          required_paths=("experiment.toml", "../outside.toml"))
            self.assertEqual([row["path"] for row in index["files"]], ["model.py", "experiment.toml"])
            found = requested_source_context(workspace, index,
                {"files": ["z_worker.py"], "symbols": ["compute"]},
                supplied=[], max_files=1, max_chars=100)
            self.assertEqual(len(found), 1)
            self.assertIn("return 42", found[0]["text"])
            self.assertEqual(found[0]["access_role"], "read_only")
            self.assertEqual([row["path"] for row in index["files"]], ["model.py", "experiment.toml"])
            # A named read bypasses only inventory truncation, not workspace,
            # environment, binary or character-budget restrictions.
            (workspace / ".env.txt").write_text("secret", encoding="utf-8")
            (workspace / "image.png").write_bytes(b"not source text")
            (workspace / "runtime").mkdir()
            (workspace / "runtime/pyvenv.cfg").write_text("home = ignored", encoding="utf-8")
            (workspace / "runtime/helper.py").write_text("secret", encoding="utf-8")
            rejected = requested_source_context(workspace, index,
                {"files": ["../outside.py", ".env.txt", "image.png", "runtime/helper.py", "missing.py"]},
                supplied=[], max_files=5, max_chars=100)
            self.assertEqual(rejected, [])

    def test_source_followup_uses_new_symbol_window_in_requested_file(self):
        from simple_ar.code_task.analysis.source_context import requested_source_context, source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "model.py").write_text(
                "def forward():\n    return 1\n" + "# padding\n" * 700
                + "def forward_again():\n    return 2\n", encoding="utf-8",
            )
            (workspace / "report.json").write_text('{"forward": "irrelevant"}', encoding="utf-8")
            index = source_file_inventory(workspace)
            request = {"files": ["model.py"], "symbols": ["forward"], "query": "find forward aggregation"}
            first = requested_source_context(workspace, index, request,
                supplied=[], max_files=2, max_chars=1000)
            second = requested_source_context(workspace, index, request,
                supplied=first, max_files=2, max_chars=1000)
            self.assertEqual([row["path"] for row in second], ["model.py"])
            self.assertIn("forward_again", second[0]["text"])
            self.assertGreater(second[0]["source_offset"], first[0]["source_offset"])
            self.assertEqual(requested_source_context(workspace, index, request,
                supplied=first + second, max_files=2, max_chars=1000), [])

    def test_focused_source_read_includes_adjacent_unseen_behavior(self):
        from simple_ar.code_task.analysis.source_context import requested_source_context, source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = (
                "def train_batch():\n" + "    # setup\n" * 125
                + "    optimizer.step()\n" + "    # trailing\n" * 80
            )
            (workspace / "model.py").write_text(source, encoding="utf-8")
            index = source_file_inventory(workspace)
            found = requested_source_context(
                workspace, index, {"files": ["model.py"], "symbols": ["train_batch"], "query": ""},
                supplied=[], max_files=2, max_chars=1000, max_total_chars=2000,
                max_windows_per_file=2,
            )
            self.assertEqual(len(found), 2)
            self.assertEqual(found[1]["source_offset"], found[0]["source_offset"] + 1000)
            self.assertIn("optimizer.step()", found[1]["text"])
            self.assertLessEqual(sum(len(row["text"]) for row in found), 2000)

    def test_source_followup_uses_query_after_symbol_window_is_exhausted(self):
        from simple_ar.code_task.analysis.source_context import requested_source_context, source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "model.py").write_text(
                "def forward():\n    return 1\n" + "# padding\n" * 700
                + "predictions = scores.mean(1)\n", encoding="utf-8",
            )
            index = source_file_inventory(workspace)
            request = {"files": ["model.py"], "symbols": ["Model.forward"],
                       "query": "Where are predictions aggregated?"}
            first = requested_source_context(workspace, index,
                {"files": ["model.py"], "symbols": ["Model.forward"], "query": ""},
                supplied=[], max_files=1, max_chars=1000)
            second = requested_source_context(workspace, index, request,
                supplied=first, max_files=1, max_chars=1000)
            self.assertIn("predictions = scores.mean(1)", second[0]["text"])

    def test_source_followup_preserves_unseen_symbol_gap_then_reaches_query(self):
        from simple_ar.code_task.analysis.source_context import requested_source_context, source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = ("x" * 2881 + "forward" + "x" * 3512 + "LinearEfficientEnsemble"
                      + "x" * 1930 + "forward" + "x" * 7290 + "currently" + "x" * 2701
                      + "predictions = heads.mean(1)\n")
            (workspace / "model.py").write_text(source, encoding="utf-8")
            index = source_file_inventory(workspace)
            supplied = [
                {"path": "model.py", "text": source[:6000], "source_offset": 0},
                {"path": "model.py", "text": source[6869:12869], "source_offset": 6869},
            ]
            request = {"files": ["model.py"], "symbols": ["Model.forward", "LinearEfficientEnsemble"],
                       "query": "How are predictions currently aggregated?"}
            found = requested_source_context(workspace, index, request,
                supplied=supplied, max_files=1, max_chars=6000)
            self.assertEqual(len(found), 1)
            # An unseen named gap is still evidence, even when neighboring
            # windows overlap heavily. Do not restore a novelty quota that
            # discards it; the next request must make progress to the query.
            self.assertEqual(found[0]["source_offset"], 6000)
            self.assertEqual(found[0]["text"], source[6000:6869])
            self.assertIn("LinearEfficientEnsemble", found[0]["text"])
            followup = requested_source_context(workspace, index, request,
                supplied=[*supplied, *found], max_files=1, max_chars=6000,
                max_total_chars=6000 - sum(len(row["text"]) for row in found))
            self.assertEqual(len(followup), 1)
            self.assertIn("predictions = heads.mean(1)", followup[0]["text"])
            self.assertGreaterEqual(followup[0]["source_offset"], 12869)
            self.assertLessEqual(sum(len(row["text"]) for row in [*found, *followup]), 6000)

    def test_exact_source_lookup_reaches_a_downstream_consumer(self):
        from simple_ar.code_task.analysis.source_context import requested_source_context, source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "producer.py").write_text(
                "def forward(x):\n    return x\n" + "# pad\n" * 500, encoding="utf-8")
            (workspace / "consumer.py").write_text(
                "# pad\n" * 500 + "def evaluate(heads):\n    return heads.mean(1)\n",
                encoding="utf-8")
            index = source_file_inventory(workspace)
            request = {"files": ["producer.py", "consumer.py"],
                       "symbols": ["forward"], "query": "where does output change?",
                       "literal": "heads.mean(1)"}
            found = requested_source_context(workspace, index, request,
                supplied=[], max_files=2, max_chars=500)
            self.assertEqual([row["path"] for row in found], ["consumer.py"])
            self.assertIn("heads.mean(1)", found[0]["text"])
            self.assertGreater(found[0]["start_line"], 400)
            self.assertGreaterEqual(found[0]["end_line"], found[0]["start_line"])
            self.assertEqual(requested_source_context(workspace, index, request,
                supplied=found, max_files=2, max_chars=500), [])

    def test_exact_source_lookup_keeps_preceding_call_setup(self):
        from simple_ar.code_task.analysis.source_context import requested_source_context, source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = ("a" * 1200 + "setup_bins = compute_bins(data)\n" + "b" * 1300
                      + "model = Model(bins=setup_bins)\n" + "c" * 5000)
            (workspace / "model.py").write_text(source, encoding="utf-8")
            found = requested_source_context(
                workspace, source_file_inventory(workspace),
                {"files": ["model.py"], "literal": "model = Model(bins=setup_bins)"},
                supplied=[], max_files=1, max_chars=4000,
            )
            self.assertEqual(len(found), 1)
            self.assertIn("setup_bins = compute_bins(data)", found[0]["text"])
            self.assertIn("model = Model(bins=setup_bins)", found[0]["text"])

    def test_construction_question_prefers_python_call_site_over_definition(self):
        from simple_ar.code_task.analysis.source_context import requested_source_context, source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = ("class Predictor:\n    def __init__(self, width):\n        self.width = width\n"
                      + "# unrelated\n" * 400
                      + "width = config['width']\nmodel = Predictor(width=width)\n"
                      + "# tail\n" * 200)
            (workspace / "model.py").write_text(source, encoding="utf-8")
            found = requested_source_context(
                workspace, source_file_inventory(workspace),
                {"files": ["model.py"], "symbols": ["Predictor.__init__"],
                 "query": "Where is the Predictor constructed from config?"},
                supplied=[], max_files=1, max_chars=1000,
            )
            self.assertEqual(len(found), 1)
            self.assertIn("width = config['width']", found[0]["text"])
            self.assertIn("model = Predictor(width=width)", found[0]["text"])

    def test_source_followup_continues_named_constructor_before_later_call_site(self):
        from simple_ar.code_task.analysis.source_context import requested_source_context, source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = (
                "class Predictor:\n    def __init__(self, width):\n        self.width = width\n"
                + "        self.width += 1\n" * 55
                + "        self.embedding = make_embedding(width)\n"
                + "\n# unrelated\n" * 400
                + "model = Predictor(width=config['width'])\n"
            )
            (workspace / "model.py").write_text(source, encoding="utf-8")
            index = source_file_inventory(workspace)
            first = requested_source_context(
                workspace, index, {"files": ["model.py"], "query": "", "symbols": []},
                supplied=[], max_files=1, max_chars=1000,
            )
            followup = requested_source_context(
                workspace, index,
                {"files": ["model.py"], "symbols": ["Predictor.__init__"],
                 "query": "Where is Predictor constructed and how is embedding enabled?"},
                supplied=first, max_files=1, max_chars=1000,
            )
            self.assertEqual(followup[0]["source_offset"], 1000)
            self.assertIn("self.embedding = make_embedding(width)", followup[0]["text"])
            self.assertNotIn("model = Predictor(", followup[0]["text"])

    def test_bounded_line_range_fills_only_unseen_gap(self):
        from simple_ar.code_task.analysis.source_context import requested_source_context, source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = "".join(f"line {i:03d}\n" for i in range(1, 61))
            (workspace / "model.py").write_text(source, encoding="utf-8")
            index = source_file_inventory(workspace)
            supplied = [{"path": "model.py", "text": "".join(source.splitlines(keepends=True)[:30]),
                         "source_offset": 0}]
            found = requested_source_context(
                workspace, index,
                {"files": ["model.py"], "line_range": {"start": 27, "end": 35}},
                supplied=supplied, max_files=1, max_chars=100,
            )
            self.assertEqual(found[0]["start_line"], 31)
            self.assertIn("line 035", found[0]["text"])
            self.assertNotIn("line 030", found[0]["text"])
            self.assertEqual(requested_source_context(
                workspace, index,
                {"files": ["model.py"], "line_range": {"start": 27, "end": 35}},
                supplied=supplied + found, max_files=1, max_chars=100), [])
            self.assertEqual(requested_source_context(workspace, index,
                {"files": ["../outside.py"], "line_range": {"start": 1, "end": 2}},
                supplied=[], max_files=1, max_chars=100), [])

    def test_design_source_read_bridges_short_gap_before_new_use_site(self):
        from simple_ar.code_task.analysis.source_context import source_file_inventory
        from simple_ar.research.design import _bridge_adjacent_source_gaps

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = ("# before\n" * 400 + "bin_edges = compute_bins(data)\n"
                      + "# setup\n" * 30 + "model = Model(bins=bin_edges)\n"
                      + "# after\n" * 400)
            (workspace / "model.py").write_text(source, encoding="utf-8")
            gap_start = source.index("bin_edges =")
            gap_end = source.index("model = Model")
            left = {"path": "model.py", "text": source[:gap_start], "source_offset": 0,
                    "start_line": 1, "end_line": source.count("\n", 0, gap_start) + 1}
            right = {"path": "model.py", "text": source[gap_end:gap_end + 1000],
                     "source_offset": gap_end,
                     "start_line": source.count("\n", 0, gap_end) + 1,
                     "end_line": source.count("\n", 0, gap_end + 999) + 1}
            bridged = _bridge_adjacent_source_gaps(
                workspace, source_file_inventory(workspace), [left], [right],
                max_chars=2000, max_files=1,
            )
            self.assertEqual(len(bridged), 1)
            self.assertEqual(bridged[0]["lookup_basis"], "adjacent_gap")
            self.assertIn("bin_edges = compute_bins(data)", bridged[0]["text"])
            self.assertEqual(bridged[0]["source_offset"] + len(bridged[0]["text"]), gap_end)

    def test_refinement_accepts_precise_line_range_without_requiring_baseline_measurement(self):
        from unittest.mock import Mock
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "model.py").write_text(
                "".join(f"# line {i}\n" for i in range(1, 45))
                + "model = Model(bins=bin_edges)\n", encoding="utf-8")
            original = build_research_design(ResearchDesignRequest(synthesis=self._synthesis()))
            client = Mock()
            client.ask_json.side_effect = [
                {"status": "inspect_source", "context_request": {
                    "files": ["model.py"], "line_range": {"start": 43, "end": 45},
                    "query": "", "symbols": []}},
                {"status": "ready", "implementation_spec":
                    "Use observed model construction and let the experiment stage measure the baseline.",
                 "unresolved_questions": []},
            ]
            result = build_research_design(ResearchDesignRequest(
                synthesis=self._synthesis(), previous_design=original.to_handoff_dict(),
                source_workspace=workspace, source_index=source_file_inventory(workspace),
                use_llm=True, llm_client=client,
            ))
            self.assertEqual(result.status, "ready")
            self.assertIn("model = Model(bins=bin_edges)", client.ask_json.call_args.args[1])
            self.assertIn("or a measured baseline", client.ask_json.call_args.args[1])

    def test_refinement_accepts_unwrapped_read_only_source_query(self):
        from unittest.mock import Mock
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "model.py").write_text("def train(x):\n    return model(x)\n", encoding="utf-8")
            previous = build_research_design(ResearchDesignRequest(synthesis=self._synthesis()))
            client = Mock()
            client.ask_json.side_effect = [
                {"files": ["model.py"], "symbols": ["train"], "query": "where is training called?", "literal": ""},
                {"status": "ready", "implementation_spec": "Use the observed training path for a bounded candidate.",
                 "unresolved_questions": []},
            ]
            result = build_research_design(ResearchDesignRequest(
                synthesis=self._synthesis(), previous_design=previous.to_handoff_dict(),
                source_workspace=workspace, source_index=source_file_inventory(workspace),
                use_llm=True, llm_client=client,
            ))
            self.assertEqual(result.status, "ready")
            self.assertIn("return model(x)", client.ask_json.call_args_list[1].args[1])

    def test_refinement_reasks_once_for_invalid_decision_shape(self):
        from unittest.mock import Mock

        previous = build_research_design(ResearchDesignRequest(synthesis=self._synthesis()))
        client = Mock()
        client.ask_json.side_effect = [
            {"status": "need_more_context", "implementation_spec": ""},
            {"status": "blocked", "implementation_spec": "", "unresolved_questions": ["Source is unavailable."]},
        ]
        result = build_research_design(ResearchDesignRequest(
            synthesis=self._synthesis(), previous_design=previous.to_handoff_dict(),
            use_llm=True, llm_client=client,
        ))
        self.assertEqual(result.status, "blocked")
        self.assertIn("required decision shape", client.ask_json.call_args_list[1].args[1])
        self.assertEqual(client.ask_json.call_count, 2)

    def test_refinement_allows_one_contiguous_final_source_window(self):
        from unittest.mock import Mock
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "model.py").write_text(
                "".join(f"# source line {number:03d}: {'x' * 15}\n" for number in range(1, 281)),
                encoding="utf-8",
            )
            previous = build_research_design(ResearchDesignRequest(synthesis=self._synthesis()))
            client = Mock()
            requests = [
                {"status": "inspect_source", "context_request": {
                    "files": ["model.py"], "symbols": [], "query": "",
                    "line_range": {"start": start, "end": start + 19},
                }} for start in (1, 21, 41)
            ]
            requests.append({"status": "inspect_source", "context_request": {
                "files": ["model.py"], "symbols": [], "query": "",
                "line_range": {"start": 61, "end": 260},
            }})
            client.ask_json.side_effect = [
                *requests,
                {"status": "ready", "implementation_spec": "Use the observed source behavior.",
                 "unresolved_questions": []},
            ]
            result = build_research_design(ResearchDesignRequest(
                synthesis=self._synthesis(), previous_design=previous.to_handoff_dict(),
                source_workspace=workspace, source_index=source_file_inventory(workspace),
                use_llm=True, llm_client=client,
            ))
            self.assertEqual(result.status, "ready")
            self.assertEqual(client.ask_json.call_count, 5)
            self.assertIn("# source line 260", client.ask_json.call_args_list[4].args[1])

    def test_refinement_rejects_noncontiguous_fourth_source_request(self):
        from unittest.mock import Mock
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "model.py").write_text(
                "".join(f"# source line {number}\n" for number in range(1, 151)), encoding="utf-8",
            )
            previous = build_research_design(ResearchDesignRequest(synthesis=self._synthesis()))
            client = Mock()
            client.ask_json.side_effect = [
                {"status": "inspect_source", "context_request": {
                    "files": ["model.py"], "symbols": [], "query": "",
                    "line_range": {"start": start, "end": start + 19},
                }} for start in (1, 21, 41, 101)
            ] + [
                {"status": "blocked", "implementation_spec": "",
                 "unresolved_questions": ["The source does not establish an implementable change."]}
            ]
            result = build_research_design(ResearchDesignRequest(
                synthesis=self._synthesis(), previous_design=previous.to_handoff_dict(),
                source_workspace=workspace, source_index=source_file_inventory(workspace),
                use_llm=True, llm_client=client,
            ))
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.diagnostics, ("The source does not establish an implementable change.",))
            self.assertEqual(client.ask_json.call_count, 5)
            self.assertIn("Already supplied source ranges", client.ask_json.call_args_list[4].args[1])

    def test_contiguous_read_allows_revisiting_a_partially_seen_last_line(self):
        from simple_ar.research.design import _is_contiguous_source_read

        excerpts = [{"path": "model.py", "end_line": 374, "text": "partial"}]
        query = {"files": ["model.py"], "symbols": [], "query": "", "literal": "",
                 "line_range": {"start": 374, "end": 573}}
        self.assertTrue(_is_contiguous_source_read(query, excerpts))
        query["line_range"] = {"start": 372, "end": 571}
        self.assertFalse(_is_contiguous_source_read(query, excerpts))

    def test_refinement_reads_other_target_after_exact_literal_was_supplied(self):
        from unittest.mock import Mock
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            literal = "loss = loss_fn(apply_model('train', batch), targets)"
            (workspace / "train.py").write_text(
                "def train(batch, targets):\n    " + literal + "\n", encoding="utf-8")
            (workspace / "evaluate.py").write_text(
                "# padding\n" * 500
                + "def evaluate(predictions):\n    return predictions.mean(1)\n",
                encoding="utf-8",
            )
            original = build_research_design(ResearchDesignRequest(synthesis=self._synthesis()))
            client = Mock()
            client.ask_json.side_effect = [
                {"status": "inspect_source", "context_request": {
                    "files": ["train.py"], "symbols": [], "query": "", "literal": literal}},
                {"status": "inspect_source", "context_request": {
                    "files": ["train.py", "evaluate.py"], "symbols": ["evaluate"],
                    "query": "where are predictions aggregated?", "literal": literal}},
                {"status": "ready", "implementation_spec": "Preserve paired training and evaluation.",
                 "unresolved_questions": []},
            ]
            result = build_research_design(ResearchDesignRequest(
                synthesis=self._synthesis(), previous_design=original.to_handoff_dict(),
                source_workspace=workspace, source_index=source_file_inventory(workspace),
                use_llm=True, llm_client=client,
            ))
            self.assertEqual(result.status, "ready")
            next_prompt = client.ask_json.call_args_list[2].args[1]
            self.assertIn('"literal": "already_supplied"', next_prompt)
            self.assertIn('"lookup_basis": "query_or_symbol"', next_prompt)
            self.assertIn("predictions.mean(1)", next_prompt)

    def test_refinement_marks_nonliteral_evidence_when_exact_is_absent(self):
        from unittest.mock import Mock
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "invoice.ts").write_text(
                "export function invoiceTotal(lines) { return lines.reduce(sumLine, 0); }\n",
                encoding="utf-8",
            )
            original = build_research_design(ResearchDesignRequest(synthesis=self._synthesis()))
            client = Mock()
            client.ask_json.side_effect = [
                {"status": "inspect_source", "context_request": {
                    "files": ["invoice.ts"], "symbols": ["invoiceTotal"],
                    "query": "where is invoiceTotal implemented?", "literal": "missingFunction("}},
                {"status": "blocked", "implementation_spec": "", "unresolved_questions": [
                    "The exact requested call was not observed in the supplied code."]},
            ]
            result = build_research_design(ResearchDesignRequest(
                synthesis=self._synthesis(), previous_design=original.to_handoff_dict(),
                source_workspace=workspace, source_index=source_file_inventory(workspace),
                use_llm=True, llm_client=client,
            ))
            self.assertEqual(result.status, "blocked")
            next_prompt = client.ask_json.call_args_list[1].args[1]
            self.assertIn('"literal": "not_observed"', next_prompt)
            self.assertIn('"lookup_basis": "query_or_symbol"', next_prompt)
            self.assertIn("invoiceTotal", next_prompt)

    def test_initial_feasibility_requires_code_evidence_not_just_config(self):
        from unittest.mock import Mock
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "consumer.py").write_text(
                "def score(heads):\n    return heads.mean(1)\n", encoding="utf-8")
            (workspace / "experiment.toml").write_text("k = 32\n", encoding="utf-8")
            client = Mock()
            client.ask_json.side_effect = [
                {"selected_idea_id": "idea-002", "rationale": "Try an in-scope change."},
                {"status": "ready", "implementation_spec": "Change scoring aggregation.",
                 "unresolved_questions": [], "target_paths": ["consumer.py"],
                 "source_quotes": [{"path": "experiment.toml", "quote": "k = 32"}]},
                {"status": "ready", "implementation_spec": "Change scoring aggregation.",
                 "unresolved_questions": [], "target_paths": ["consumer.py"],
                 "source_quotes": [{"path": "experiment.toml", "quote": "k = 32"},
                                   {"path": "consumer.py", "quote": "heads.mean(1)"}]},
                self._ALIGNED_REVIEW,
            ]
            result = build_research_design(ResearchDesignRequest(
                synthesis=self._synthesis(), idea_id="idea-002", idea_id_is_fixed=False,
                execution_boundary={"code_task": {"code_root": str(workspace)},
                    "protocol": {"comparison_conditions": {"source_config": "experiment.toml"}}},
                source_workspace=workspace, source_index=source_file_inventory(workspace),
                use_llm=True, llm_client=client,
            ))
            self.assertEqual(result.status, "ready")
            self.assertEqual(client.ask_json.call_count, 4)
            self.assertIn("exact observed source quote from at least one existing target",
                          client.ask_json.call_args_list[2].args[1])

    def test_initial_feasibility_uses_separate_reviewer_when_supplied(self):
        from unittest.mock import Mock
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "service.ts").write_text(
                "export function total(lines) { return lines.reduce(sumLine, 0); }\n",
                encoding="utf-8",
            )
            author = Mock()
            author.ask_json.side_effect = [
                {"selected_idea_id": "idea-002", "rationale": "Bounded change."},
                {"status": "ready", "implementation_spec": "Fix aggregation.",
                 "unresolved_questions": [], "target_paths": ["service.ts"],
                 "source_quotes": [{"path": "service.ts", "quote": "lines.reduce(sumLine, 0)"}]},
            ]
            reviewer = Mock()
            reviewer.ask_json.return_value = self._ALIGNED_REVIEW
            result = build_research_design(ResearchDesignRequest(
                synthesis=self._synthesis(), idea_id="idea-002", idea_id_is_fixed=False,
                execution_boundary={"code_task": {"code_root": str(workspace)}},
                source_workspace=workspace, source_index=source_file_inventory(workspace),
                use_llm=True, llm_client=author, feasibility_llm_client=reviewer,
            ))
            self.assertEqual(result.status, "ready")
            self.assertEqual(author.ask_json.call_count, 2)
            reviewer.ask_json.assert_called_once()
            self.assertEqual(reviewer.ask_json.call_args.kwargs["label"],
                             "research-design-feasibility-review")

    def test_initial_feasibility_can_add_a_new_file_with_existing_integration_context(self):
        from unittest.mock import Mock
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "service.ts").write_text(
                "export function invoiceTotal(lines) { return lines.reduce(sumLine, 0); }\n",
                encoding="utf-8",
            )
            index = source_file_inventory(workspace)
            self.assertEqual(index["files"][0]["role_tags"], ["source"])
            client = Mock()
            client.ask_json.side_effect = [
                {"selected_idea_id": "idea-002", "rationale": "Add a bounded implementation."},
                {"status": "ready", "implementation_spec":
                    "Add a line calculator and call it from the existing invoice total; "
                    "validate mixed taxable/exempt lines with a small fixture.",
                 "unresolved_questions": [], "target_paths": ["service.ts", "line_calculator.ts"],
                 "source_quotes": [{"path": "service.ts", "quote": "lines.reduce(sumLine, 0)"}]},
                self._ALIGNED_REVIEW,
            ]
            result = build_research_design(ResearchDesignRequest(
                synthesis=self._synthesis(), idea_id="idea-002", idea_id_is_fixed=False,
                execution_boundary={"code_task": {"code_root": str(workspace)}},
                source_workspace=workspace, source_index=index,
                use_llm=True, llm_client=client,
            ))
            self.assertEqual(result.status, "ready")
            self.assertEqual(client.ask_json.call_count, 3)

    def test_source_inventory_keeps_non_python_code_ahead_of_config(self):
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "a.json").write_text('{"mode": "test"}', encoding="utf-8")
            (workspace / "b.yaml").write_text("mode: test\n", encoding="utf-8")
            (workspace / "service.ts").write_text("export const value = 1;\n", encoding="utf-8")
            index = source_file_inventory(workspace, max_files=1)
            self.assertEqual([row["path"] for row in index["files"]], ["service.ts"])

    def test_initial_feasibility_can_plan_a_greenfield_source_file(self):
        from unittest.mock import Mock
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            client = Mock()
            client.ask_json.side_effect = [
                {"selected_idea_id": "idea-002", "rationale": "Start a small project."},
                {"status": "ready", "implementation_spec":
                    "Create a standalone parser with a fixture for input and output checks.",
                 "unresolved_questions": [], "target_paths": ["parser.py"]},
                self._ALIGNED_REVIEW,
            ]
            result = build_research_design(ResearchDesignRequest(
                synthesis=self._synthesis(), idea_id="idea-002", idea_id_is_fixed=False,
                execution_boundary={"code_task": {"code_root": str(workspace)}},
                source_workspace=workspace, source_index=source_file_inventory(workspace),
                use_llm=True, llm_client=client,
            ))
            self.assertEqual(result.status, "ready")
            self.assertEqual(client.ask_json.call_count, 3)

    def test_initial_feasibility_reserves_budget_for_three_source_followups(self):
        from unittest.mock import Mock
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "model.py").write_text(
                "# prefix\n" * 800 + "def late_one():\n    pass\n"
                + "# middle\n" * 800 + "def late_two():\n    return 2\n"
                + "# more\n" * 800 + "def late_three():\n    return 3\n", encoding="utf-8",
            )
            (workspace / "helper.py").write_text(
                "# prefix\n" * 800 + "def late_one():\n    return 1\n", encoding="utf-8",
            )
            (workspace / "experiment.toml").write_text("[model]\nk = 32\n", encoding="utf-8")
            client = Mock()
            client.ask_json.side_effect = [
                {"selected_idea_id": "idea-002", "rationale": "Inspect the implementation."},
                {"status": "inspect_source", "context_request": {"files": ["model.py", "helper.py"],
                    "symbols": ["late_one"], "query": ""}},
                {"status": "inspect_source", "context_request": {"files": ["model.py"],
                    "symbols": ["late_two"], "query": ""}},
                {"status": "inspect_source", "context_request": {"files": ["model.py"],
                    "symbols": ["late_three"], "query": ""}},
                {"status": "ready", "implementation_spec": "Modify model.py at late_three.",
                    "target_paths": ["model.py"],
                    "source_quotes": [{"path": "experiment.toml", "quote": "k = 32"},
                                      {"path": "model.py", "quote": "def late_three()"}],
                    "unresolved_questions": []},
                self._ALIGNED_REVIEW,
            ]
            result = build_research_design(ResearchDesignRequest(
                synthesis=self._synthesis(), idea_id="idea-002", idea_id_is_fixed=False,
                execution_boundary={"code_task": {"code_root": str(workspace),
                    "allowed_patterns": ["model.py", "helper.py"]},
                    "protocol": {"comparison_conditions": {"source_config": "experiment.toml"}}},
                source_workspace=workspace, source_index=source_file_inventory(workspace),
                use_llm=True, llm_client=client,
            ))
            self.assertEqual(result.status, "ready")
            self.assertIn("late_three", client.ask_json.call_args_list[4].args[1])

    def test_feasibility_audit_can_request_one_final_bounded_source_read(self):
        from unittest.mock import Mock
        from simple_ar.code_task.analysis.source_context import source_file_inventory

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = "def start():\n    return 1\n" + "# padding\n" * 500
            for marker in ("first", "second", "third", "audit_target"):
                source += f"# {marker}\n" + "# padding\n" * 500
            (workspace / "model.py").write_text(source, encoding="utf-8")
            inspect = lambda marker: {"status": "inspect_source", "context_request": {
                "files": ["model.py"], "symbols": [], "query": "", "literal": marker}}
            ready = {"status": "ready", "implementation_spec": "Change the observed source path.",
                     "target_paths": ["model.py"],
                     "source_quotes": [{"path": "model.py", "quote": "def start()"}],
                     "unresolved_questions": []}
            client = Mock()
            client.ask_json.side_effect = [
                {"selected_idea_id": "idea-002", "rationale": "Inspect source first."},
                inspect("first"), inspect("second"), inspect("third"), ready,
                {"verdict": "revise", "mechanism_alignment": "uncertain",
                 "mechanism_rationale": "The relevant source is not yet visible.",
                 "premise_verdict": "unknown", "premise_rationale": "The consumer is not visible.",
                 "blocking_issues": ["Inspect audit_target before editing."], "delegated_checks": []},
                inspect("audit_target"), ready, self._ALIGNED_REVIEW,
            ]
            result = build_research_design(ResearchDesignRequest(
                synthesis=self._synthesis(), idea_id="idea-002", idea_id_is_fixed=False,
                execution_boundary={"code_task": {"code_root": str(workspace),
                    "allowed_patterns": ["model.py"]}},
                source_workspace=workspace, source_index=source_file_inventory(workspace),
                use_llm=True, llm_client=client,
            ))
            self.assertEqual(result.status, "ready")
            self.assertEqual(client.ask_json.call_count, 9)
            self.assertIn("audit_target", client.ask_json.call_args_list[7].args[1])

    def test_refinement_reads_real_source_and_persists_trace_on_provider_failure(self):
        from unittest.mock import Mock
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            source = "# padding\n" * 900 + "def end_task(model):\n    return model.copy()\n"
            (workspace / "lifecycle.py").write_text(source, encoding="utf-8")
            original = build_research_design(ResearchDesignRequest(synthesis=self._synthesis()))
            client = Mock()
            inspect = {"status": "inspect_source", "context_request": {
                "files": ["lifecycle.py"], "symbols": ["end_task"], "query": ""}}
            client.ask_json.side_effect = [inspect, LLMError("provider unavailable")]
            request = ResearchDesignRequest(synthesis=self._synthesis(), previous_design=original.to_handoff_dict(),
                source_workspace=workspace, source_index={"files": [{"path": "lifecycle.py"}]},
                use_llm=True, llm_client=client)
            context = CapabilityContext(store=ArtifactStore(root / "attempt"),
                attempt=AttemptManifest(attempt_id="design-1", capability="research_design"))
            with self.assertRaisesRegex(LLMError, "provider unavailable"):
                run_research_design_capability(context=context, request=request)
            trace = json.loads((root / "attempt" / "design_refinement_trace.json").read_text())
            excerpt = trace["turns"][0]["source_excerpts"][0]
            self.assertIn("def end_task", excerpt["text"])
            self.assertTrue(excerpt["truncated"])
            self.assertIn("def end_task", client.ask_json.call_args.args[1])
            self.assertEqual((workspace / "lifecycle.py").read_text(), source)
            # A repeated request terminates rather than looping on unchanged evidence.
            client.ask_json.side_effect = [inspect, inspect]
            self.assertEqual(build_research_design(request).status, "blocked")

    def test_refinement_reselects_only_available_candidates_with_same_protocol(self):
        from unittest.mock import Mock
        synthesis = self._synthesis()
        original = build_research_design(ResearchDesignRequest(synthesis=synthesis))
        alternate = replace(original.selected_idea, idea_id="alternate", title="Simpler candidate",
            hypothesis="An alternative may help", proposed_change="Use a simpler objective")
        synthesis = replace(synthesis, ideas=(*synthesis.ideas, alternate))
        client = Mock()
        client.ask_json.return_value = {"status": "ready", "implementation_spec": "Implement the simpler objective; coefficient 0.1 is our trial choice.",
            "unresolved_questions": [], "selected_idea_id": "alternate", "selection_rationale": "Original candidate is not implementable."}
        request = ResearchDesignRequest(synthesis=synthesis, previous_design=original.to_handoff_dict(), use_llm=True, llm_client=client)
        result = build_research_design(request)
        self.assertEqual(result.selected_idea, alternate)
        self.assertEqual(result.contract.hypothesis, alternate.hypothesis)
        self.assertEqual(result.execution_protocol, original.execution_protocol)
        self.assertEqual(result.contract.dataset, original.contract.dataset)
        self.assertEqual(result.contract.implementation_scope, original.contract.implementation_scope)
        self.assertIsNone(result.novelty_check)
        with self.assertRaises(LLMError):
            build_research_design(replace(request, idea_id=original.selected_idea.idea_id))
        self.assertEqual(build_research_design(replace(request, idea_id=original.selected_idea.idea_id,
            idea_id_is_fixed=False)).selected_idea, alternate)
        different_data = replace(alternate, required_datasets=["other"])
        self.assertEqual(build_research_design(replace(request,
            synthesis=replace(synthesis, ideas=(different_data,)))).status, "blocked")

    def test_measured_revision_cannot_reselect_a_tried_or_evidence_blocked_idea(self):
        from unittest.mock import Mock

        synthesis = self._synthesis()
        original = build_research_design(ResearchDesignRequest(synthesis=synthesis))
        alternate = replace(original.selected_idea, idea_id="alternate", title="Distinct candidate")
        synthesis = replace(synthesis, ideas=(*synthesis.ideas, alternate))
        client = Mock()
        client.ask_json.return_value = {"status": "ready", "implementation_spec": "Implement the selected idea.",
            "unresolved_questions": [], "selected_idea_id": "alternate", "selection_rationale": "Test a distinct direction."}
        feedback = {"kind": "research_revision", "candidate_options": [
            {"idea_id": original.selected_idea.idea_id, "selected": True, "tried": True, "assessment_status": "ready"},
            {"idea_id": "alternate", "selected": False, "tried": False, "assessment_status": "ready"},
        ]}
        request = ResearchDesignRequest(synthesis=synthesis, previous_design=original.to_handoff_dict(),
            implementation_feedback=feedback, use_llm=True, llm_client=client)
        self.assertEqual(build_research_design(request).selected_idea.idea_id, "alternate")
        client.ask_json.return_value["selected_idea_id"] = original.selected_idea.idea_id
        self.assertEqual(build_research_design(request).status, "blocked")
        client.ask_json.return_value["selected_idea_id"] = "alternate"
        feedback["candidate_options"][1]["assessment_status"] = "needs_evidence"
        self.assertEqual(build_research_design(replace(request, implementation_feedback=feedback)).status, "blocked")

    def test_implementation_refinement_preserves_contract_and_surfaces_missing_evidence(self):
        from unittest.mock import Mock
        original = build_research_design(ResearchDesignRequest(synthesis=self._synthesis()))
        client = Mock()
        request = ResearchDesignRequest(synthesis={}, previous_design=original.to_handoff_dict(),
            implementation_feedback={"questions": ["When is the checkpoint updated?"]},
            use_llm=True, llm_client=client)
        client.ask_json.return_value = {"status": "ready", "implementation_spec": "Chosen engineering detail: snapshot after end_task.",
                                      "unresolved_questions": [], "execution_protocol": {"command": ["unapproved"]}}
        refined = build_research_design(request)
        self.assertEqual(refined.contract, original.contract)
        self.assertEqual(refined.execution_protocol, original.execution_protocol)
        self.assertIn("end_task", ResearchDesignResult.from_handoff_dict(refined.to_handoff_dict()).implementation_spec)
        client.ask_json.return_value = {"status": "blocked", "implementation_spec": "", "unresolved_questions": ["Need the actual method definition."]}
        blocked = build_research_design(request)
        self.assertEqual(blocked.status, "blocked")
        self.assertEqual(blocked.diagnostics, ("Need the actual method definition.",))
        client.ask_json.return_value = {"status": "ready", "implementation_spec": "Guess", "unresolved_questions": ["Unknown shape"]}
        with self.assertRaises(LLMError):
            build_research_design(request)

    def test_literature_baseline_does_not_request_a_control_run(self) -> None:
        result = build_research_design(ResearchDesignRequest(
            synthesis=self._synthesis(),
            execution_boundary={"command": [sys.executable, "benchmark.py"]},
        ))
        self.assertFalse(result.execution_protocol["comparison_required"])
        self.assertEqual(result.execution_protocol["baseline_policy"], "skip")

    def test_explicit_execution_boundary_overrides_conflicting_model_protocol(self) -> None:
        class FakeClient:
            model = "fake-protocol-model"

            def ask_json(self, _system: str, _user: str, *, label: str = "", **kwargs: object):
                return {
                    "selected_idea_id": "idea-002",
                    "rationale": "Use the inspected benchmark with the caller's declared conditions.",
                    "execution_protocol": {
                        "command": [sys.executable, "benchmark.py", "--batch-size", "4"],
                        "pairs": [
                            {
                                "seed": 0,
                                "baseline_command": [sys.executable, "benchmark.py", "--seed", "0"],
                                "candidate_command": [sys.executable, "benchmark.py", "--seed", "0"],
                            },
                        ],
                        "comparison_required": False,
                        "baseline_policy": "skip",
                        "result_schema": {"primary_metric": "loss"},
                    },
                }

        result = build_research_design(
            ResearchDesignRequest(
                synthesis=self._synthesis(),
                use_llm=True,
                llm_client=FakeClient(),
                execution_schema={"primary_metric": "f1"},
                execution_boundary={
                    "command": [sys.executable, "benchmark.py"],
                    "seeds": [7, 9],
                    "seed_flag": "--seed",
                    "baseline_policy": "run",
                    "result_schema": {"primary_metric": "f1"},
                },
                entry_facts={
                    "benchmark_argv": [sys.executable, "benchmark.py"],
                    "authorized_argv_prefixes": [[sys.executable, "benchmark.py"]],
                },
            )
        )

        self.assertEqual(result.execution_protocol["seeds"], [7, 9])
        self.assertNotIn("pairs", result.execution_protocol)
        self.assertEqual(result.execution_protocol["baseline_policy"], "run")
        self.assertTrue(result.execution_protocol["comparison_required"])
        self.assertEqual(result.execution_protocol["result_schema"], {"primary_metric": "f1"})

    def test_llm_design_rejects_pair_command_outside_inspected_entry(self) -> None:
        class FakeClient:
            model = "fake-protocol-model"

            def ask_json(self, _system: str, _user: str, *, label: str = "", **kwargs: object):
                return {
                    "selected_idea_id": "idea-002",
                    "rationale": "Try an uninspected command.",
                    "execution_protocol": {
                        "pairs": [
                            {
                                "seed": 0,
                                "baseline_command": [sys.executable, "other.py"],
                                "candidate_command": [sys.executable, "benchmark.py"],
                            },
                        ],
                    },
                }

        with self.assertRaises(LLMError):
            build_research_design(
                ResearchDesignRequest(
                    synthesis=self._synthesis(),
                    use_llm=True,
                    llm_client=FakeClient(),
                    entry_facts={
                        "authorized_argv_prefixes": [[sys.executable, "benchmark.py"]],
                    },
                )
            )

    def test_llm_design_binds_inspected_entry_facts_to_execution_protocol(self) -> None:
        class FakeClient:
            model = "fake-protocol-model"

            def ask_json(self, _system: str, _user: str, *, label: str = "", **kwargs: object):
                self.label = label
                return {
                    "selected_idea_id": "idea-002",
                    "rationale": "The supplied entrypoint supports a small measured comparison.",
                    "execution_protocol": {
                        "command": [sys.executable, "benchmark.py", "--batch-size", "4"],
                        "seeds": [0, 1],
                        "seed_flag": "--seed",
                        "baseline_policy": "run",
                        "comparison_required": True,
                        "decision_reason": "The selected direction needs two comparable seed conditions.",
                        "stopping_criteria": ["Stop after both bounded conditions pass."],
                    },
                }

        result = build_research_design(
            ResearchDesignRequest(
                synthesis=self._synthesis(),
                use_llm=True,
                llm_client=FakeClient(),
                execution_schema={"primary_metric": "f1"},
                execution_boundary={"result_schema": {"primary_metric": "f1"}},
                entry_facts={
                    "benchmark_argv": [sys.executable, "benchmark.py"],
                    "authorized_argv_prefixes": [[sys.executable, "benchmark.py"]],
                    "input_refs": ["preparation:entry-facts"],
                },
            )
        )

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.execution_protocol["seeds"], [0, 1])
        self.assertEqual(result.execution_protocol["command"][-2:], ["--batch-size", "4"])
        from simple_ar.app.research_execution import merge_execution_protocol, normalize_execution_config

        execution = normalize_execution_config(merge_execution_protocol({}, result.execution_protocol))
        self.assertEqual(
            execution["pairs"][0]["baseline_command"],
            execution["pairs"][0]["candidate_command"],
        )
        self.assertEqual(result.execution_protocol["input_refs"], ["preparation:entry-facts"])

    def test_model_cannot_choose_provenance_refs_but_command_boundary_still_applies(self) -> None:
        class FakeClient:
            model = "fake-protocol-model"

            def ask_json(self, _system: str, _user: str, *, label: str = "", **kwargs: object):
                return {
                    "selected_idea_id": "idea-002",
                    "rationale": "Use the inspected benchmark.",
                    "execution_protocol": {
                        "input_refs": ["invented:uninspected-input"],
                        "baseline_policy": "skip",
                    },
                }

        trace: list[dict[str, object]] = []
        result = build_research_design(ResearchDesignRequest(
            synthesis=self._synthesis(), use_llm=True, llm_client=FakeClient(),
            execution_boundary={"command": [sys.executable, "benchmark.py"]},
            entry_facts={
                "authorized_argv_prefixes": [[sys.executable, "benchmark.py"]],
                "input_refs": [{"path": "inputs/brief.json", "kind": "research_brief"}],
            },
        ), trace=trace)
        self.assertEqual(
            result.execution_protocol["input_refs"],
            [{"path": "inputs/brief.json", "kind": "research_brief"}],
        )
        self.assertEqual(trace[0]["kind"], "ignored_model_input_refs")

    def test_llm_mode_selects_only_an_existing_candidate(self) -> None:
        class FakeClient:
            model = "fake-design-model"

            def __init__(self) -> None:
                self.labels: list[str] = []
                self.users: list[str] = []

            def ask_json(
                self,
                _system: str,
                _user: str,
                *,
                label: str = "",
            ) -> dict[str, str]:
                self.labels.append(label)
                self.users.append(_user)
                return {
                    "selected_idea_id": "idea-002",
                    "rationale": "It has a measurable metric and a bounded change.",
                }

        client = FakeClient()
        result = build_research_design(
            ResearchDesignRequest(
                synthesis=self._synthesis(),
                topic="reliable agents",
                execution_context="Dataset: prepared digits benchmark; do not substitute another task.",
                use_llm=True,
                llm_client=client,
            )
        )

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.generation_mode, "llm")
        self.assertEqual(result.selected_idea.idea_id, "idea-002")
        self.assertEqual(
            result.selection_rationale,
            "It has a measurable metric and a bounded change.",
        )
        self.assertEqual(client.labels, ["research-design"])
        self.assertIn("Topic: reliable agents", client.users[0])
        self.assertIn("prepared digits benchmark", client.users[0])
        restored = ResearchDesignResult.from_handoff_dict(result.to_handoff_dict())
        self.assertEqual(restored.selection_rationale, result.selection_rationale)

    def test_selects_requested_idea_and_round_trips_handoff(self) -> None:
        synthesis = self._synthesis()

        result = build_research_design(
            ResearchDesignRequest(synthesis=synthesis, idea_id="idea-002")
        )

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.generation_mode, "deterministic")
        self.assertEqual(result.selected_idea.idea_id, "idea-002")
        self.assertEqual(result.contract.contract_id, "contract-1/idea-002")
        self.assertEqual(result.contract.baseline, "calibration-baseline")
        self.assertEqual(result.contract.metrics, ["f1"])
        self.assertEqual(result.contract.expected_outcome, "f1 improves")
        restored = ResearchDesignResult.from_handoff_dict(result.to_handoff_dict())
        self.assertEqual(restored.contract, result.contract)
        self.assertEqual(restored.novelty_check.idea_id, "idea-002")

    def test_prepared_execution_boundary_does_not_invent_protocol_facts(self) -> None:
        result = build_research_design(
            ResearchDesignRequest(
                synthesis=self._synthesis(),
                idea_id="idea-002",
                execution_schema={
                    "primary_metric": "accuracy",
                    "required_metrics": ["accuracy", "macro_f1"],
                },
                execution_boundary={"code_task": {"code_root": "examples/code_task_digits_mlp/project"}},
                execution_context=(
                    "Prepared project: examples/code_task_digits_mlp/project."
                ),
            )
        )

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.contract.baseline, "calibration-baseline")
        self.assertEqual(result.contract.dataset, "unknown")
        self.assertEqual(result.contract.metrics, ["accuracy", "macro_f1"])
        self.assertEqual(result.contract.dataset_refs, [])
        self.assertEqual(result.contract.split_spec, {})
        self.assertEqual(result.contract.comparison_conditions, {})
        self.assertEqual(
            [item["name"] for item in result.contract.metric_specs],
            ["accuracy", "macro_f1"],
        )

    def test_prepared_design_preserves_explicit_evaluation_facts(self):
        declared = {"dataset": "user-data", "dataset_refs": [{"asset_id": "user-data"}],
                    "split_spec": {"split": "fixed-validation"},
                    "comparison_conditions": {"seed": 0, "epochs": 1}}
        result = build_research_design(ResearchDesignRequest(
            synthesis=self._synthesis(), idea_id="idea-002",
            execution_schema={"required_metrics": ["accuracy"]},
            execution_boundary={"code_task": {"code_root": "prepared"}, "protocol": declared},
            execution_context="Prepared user project",
        ))
        for name, value in declared.items():
            self.assertEqual(getattr(result.contract, name), value)
        self.assertEqual(result.contract.metric_specs, [{"name": "accuracy"}])

    def test_default_selection_prefers_a_more_executable_candidate(self) -> None:
        synthesis = SynthesisResult(
            status="ready",
            gap_summary="The fixture contains two directions.",
            ideas=(
                IdeaCandidate(
                    idea_id="idea-001",
                    title="Underspecified direction",
                    hypothesis="A change may help.",
                    proposed_change="try a change",
                    feasibility="medium",
                ),
                IdeaCandidate(
                    idea_id="idea-002",
                    title="Grounded direction",
                    hypothesis="Validation improves accuracy.",
                    motivation_refs=["paper-1#claim-1"],
                    proposed_change="add validation",
                    expected_outcome="accuracy improves",
                    required_baselines=["baseline"],
                    required_datasets=["fixture"],
                    metrics=["accuracy"],
                    feasibility="medium",
                ),
            ),
            novelty_checks=(),
            experiment_contract=ResearchExperimentContract(
                contract_id="contract-1",
                hypothesis="A change may help.",
                proposed_change="try a change",
            ),
        )

        result = build_research_design(ResearchDesignRequest(synthesis=synthesis))

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.selected_idea.idea_id, "idea-002")
        self.assertEqual(result.contract.contract_id, "contract-1/idea-002")

    def test_llm_mode_rejects_a_candidate_outside_the_handoff(self) -> None:
        class FakeClient:
            def ask_json(
                self,
                _system: str,
                _user: str,
                *,
                label: str = "",
            ) -> dict[str, str]:
                return {
                    "selected_idea_id": "invented-idea",
                    "rationale": "This is not in the supplied candidates.",
                }

        with self.assertRaises(LLMError):
            build_research_design(
                ResearchDesignRequest(
                    synthesis=self._synthesis(),
                    use_llm=True,
                    llm_client=FakeClient(),
                )
            )

    def test_non_ready_synthesis_is_not_approved(self) -> None:
        result = build_research_design(
            ResearchDesignRequest(
                synthesis=SynthesisResult(
                    status="needs_review",
                    gap_summary="insufficient evidence",
                    ideas=(),
                    novelty_checks=(),
                    diagnostics=("No source chunks are available.",),
                )
            )
        )

        self.assertEqual(result.status, "needs_review")
        self.assertIsNone(result.contract)
        self.assertIn("needs_review", result.diagnostics[0])

    def test_explicit_candidate_keeps_contract_without_approving_uncertain_science(self):
        synthesis = replace(self._synthesis(), status="needs_review", diagnostics=("Novelty is uncertain.",))
        result = build_research_design(ResearchDesignRequest(
            synthesis=synthesis, idea_id="idea-002", selection_rationale="Compare the bounded hypothesis.",
        ))
        self.assertEqual(result.status, "needs_review")
        self.assertEqual(result.source_synthesis_status, "needs_review")
        self.assertEqual(result.selected_idea.idea_id, "idea-002")
        self.assertEqual(result.contract.contract_id, "contract-1/idea-002")
        self.assertIn("Novelty is uncertain.", result.diagnostics)
        missing = build_research_design(ResearchDesignRequest(synthesis=synthesis))
        self.assertIsNone(missing.contract)

    def test_missing_contract_is_blocked(self) -> None:
        result = build_research_design(
            ResearchDesignRequest(
                synthesis=SynthesisResult(
                    status="ready",
                    gap_summary="ready",
                    ideas=(),
                    novelty_checks=(),
                )
            )
        )

        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.contract)

    def test_execution_schema_mismatch_requires_review(self) -> None:
        result = build_research_design(
            ResearchDesignRequest(
                synthesis=self._synthesis(),
                execution_schema={
                    "primary_metric": "loss",
                    "required_metrics": ["loss"],
                    "metric_directions": {"loss": "lower"},
                },
            )
        )

        self.assertEqual(result.status, "needs_review")
        self.assertIsNotNone(result.contract)
        self.assertTrue(any("do not overlap" in item for item in result.diagnostics))

    def test_execution_schema_accepts_metric_embedded_in_extracted_prose(self) -> None:
        synthesis = self._synthesis()
        contract = ResearchExperimentContract(
            contract_id="contract-prose",
            hypothesis=synthesis.experiment_contract.hypothesis,
            metrics=["The fixture reports accuracy: 0.75."],
            proposed_change="add validation",
        )
        result = build_research_design(
            ResearchDesignRequest(
                synthesis=SynthesisResult(
                    status="ready",
                    gap_summary=synthesis.gap_summary,
                    ideas=(),
                    novelty_checks=(),
                    experiment_contract=contract,
                ),
                execution_schema={"primary_metric": "accuracy"},
            )
        )

        self.assertEqual(result.status, "ready")

    def test_capability_persists_one_typed_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = CapabilityContext(
                store=ArtifactStore(Path(tmp)),
                attempt=AttemptManifest(
                    attempt_id="design-001",
                    capability="research_design",
                ),
            )
            result = run_research_design_capability(
                context=context,
                request=ResearchDesignRequest(synthesis=self._synthesis()),
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.artifacts[0].path, "research_design.json")
            payload = context.store.read_json(result.artifacts[0])
            self.assertEqual(payload["schema_version"], "research_design.v1")
            self.assertEqual(payload["contract"]["contract_id"], "contract-1/idea-002")
            self.assertEqual(payload["contract"]["baseline"], "calibration-baseline")
            self.assertEqual(payload["contract"]["expected_outcome"], "f1 improves")

    @staticmethod
    def _synthesis() -> SynthesisResult:
        contract = ResearchExperimentContract(
            contract_id="contract-1",
            hypothesis="Validation improves reliable agent accuracy.",
            motivation_refs=["paper-1#claim-1"],
            baseline="baseline",
            dataset="fixture",
            metrics=["accuracy"],
            proposed_change="add validation",
        )
        ideas = (
            IdeaCandidate(
                idea_id="idea-001",
                title="Validation",
                hypothesis="Validation improves accuracy.",
                motivation_refs=["paper-1#claim-1"],
                proposed_change="add validation",
                metrics=["accuracy"],
            ),
            IdeaCandidate(
                idea_id="idea-002",
                title="Calibration",
                hypothesis="Calibration improves F1.",
                motivation_refs=["paper-2#claim-1"],
                proposed_change="add calibration",
                expected_outcome="f1 improves",
                required_baselines=["calibration-baseline"],
                metrics=["f1"],
            ),
        )
        return SynthesisResult(
            status="ready",
            gap_summary="The fixture leaves room for validation.",
            ideas=ideas,
            novelty_checks=(
                NoveltyCheck(
                    idea_id="idea-001",
                    status="local_risk_hint",
                ),
                NoveltyCheck(
                    idea_id="idea-002",
                    status="local_risk_hint",
                ),
            ),
            experiment_contract=contract,
        )


if __name__ == "__main__":
    unittest.main()
