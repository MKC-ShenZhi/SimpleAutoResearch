from __future__ import annotations

from dataclasses import replace
import json
import shutil
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pathlib import Path

from simple_ar.app.research_application import (
    ResearchApplicationServices,
    ResearchApplicationError,
    create_session,
    load_session,
)
from simple_ar.core.budget import BudgetLedger
from simple_ar.integrations.llm import LLMClient, LLMError, LLMSettings
from simple_ar.research.planning.capability import ResearchPlanRequest
from simple_ar.research.implementation import ImplementationRequest
from simple_ar.research.workflow_contracts import ResearchBrief
from simple_ar.research.task_plan import TaskPlanResult, TaskPlanStep


from simple_ar.code_task import initialize_code_task
from simple_ar.code_task.runtime.state import code_task_paths
from simple_ar.core.artifacts import read_json
from tests.code_task_fixtures import (
    TOY_PREDICT_SOURCE,
    TOY_PREDICT_WITH_PRIZE,
    keyword_code_responses,
    write_toy_project as _write_toy_project,
    write_keyword_validation_test,
)

class ResearchApplicationTests(unittest.TestCase):
    def _paper_session(self, root, text, *, filename="paper.md", services=None, **brief_fields):
        """Create a real session with one supplied paper; callers own the protocol."""
        paper = root / filename
        paper.write_text(text, encoding="utf-8")
        app = create_session(
            ResearchBrief(
                asset_requests=({"locator": str(paper), "role": "paper"},),
                **brief_fields,
            ),
            root=root / "session",
            services=services,
        )
        return app, paper

    def test_empty_ingest_remains_pending_and_explicit_resume_retries_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.html"
            source.write_text("<html><body></body></html>", encoding="utf-8")
            app = create_session(ResearchBrief(request_text="Write from the supplied source", requested_outputs=("report",),
                asset_requests=({"locator": str(source), "role": "paper"},)), root=root / "session",
                services=ResearchApplicationServices(config={"research_task_kind": "writing"}))
            initial = app.advance(max_actions=2)
            self.assertEqual(initial.status, "paused")
            self.assertEqual(initial.next_action, "document_ingest")
            self.assertNotIn("documents", initial.state_refs)
            self.assertFalse(any(row["capability"] == "report_write" for row in initial.attempts))
            attempts = list(app.controller.list_attempts())
            failed = next(row for row in attempts if row.capability == "document_ingest")
            self.assertEqual(failed.status, "blocked")
            source.write_text("<h1>Supplied source</h1><p>Now usable original evidence.</p>", encoding="utf-8")
            restored = load_session(root / "session")
            restored.continue_session(reason="The user replaced the empty supplied document.")
            completed = restored.advance(max_actions=1)
            self.assertEqual(completed.next_action, "report_write")
            self.assertIn("Now usable original evidence", "\n".join(row.text for row in restored._load_documents().chunks))
            self.assertEqual(len(restored.controller.list_attempts()), len(attempts) + 1)
            self.assertEqual(next(row for row in restored.controller.list_attempts()
                                  if row.attempt_id == failed.attempt_id).status, "blocked")
            # A saved partial code result is evidence, not a completed task.
            implementation = restored.controller.store.write_json("checks/implementation.json", {
                "status": "incomplete", "stop_reason": "validation_failed"})
            restored.controller.manifest.state_refs["implementation"] = implementation
            step = SimpleNamespace(state_name="implementation", capability="implement")
            attempt = SimpleNamespace(status="completed", trigger="application:implementation", capability="implement")
            with patch.object(restored, "_attempt_for_ref", return_value=attempt):
                self.assertFalse(restored._step_completed(step))
                restored.controller.store.write_json(implementation.path, {
                    "status": "validated", "stop_reason": "stop_point"})
                self.assertTrue(restored._step_completed(step))

    def test_local_document_selection_uses_parser_formats_without_importing_raw_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paper = root / "paper.HTML"
            material = root / "notes.htm"
            raw_table = root / "observations.csv"
            paper.write_text("<h1>Primary paper</h1><p>Original conditions.</p>", encoding="utf-8")
            material.write_text("<h1>Supplied notes</h1><p>Declared observations.</p>", encoding="utf-8")
            raw_table.write_text("a,b\n1,2\n", encoding="utf-8")
            app = create_session(ResearchBrief(request_text="Write from supplied sources",
                requested_outputs=("report",), asset_requests=(
                    {"locator": str(paper), "role": "paper"},
                    {"locator": str(material), "role": "material"},
                    {"locator": str(raw_table), "role": "material"})), root=root / "session",
                services=ResearchApplicationServices(config={"research_task_kind": "writing"}))
            self.assertEqual(app._local_documents(), (paper, material))

    def test_action_stop_uses_one_existing_pause_and_view_without_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'session'
            app = create_session(ResearchBrief(request_text='Summarize supplied evidence',
                requested_outputs=('summary',)), root=root)
            before_attempts = app.view().attempts
            with patch.object(app.controller, 'pause', wraps=app.controller.pause) as pause, \
                    patch.object(app, '_persist_application_views', wraps=app._persist_application_views) as persist:
                self.assertFalse(app._pause_action('  Keep the measured inputs; clarify scope.  '))
            pause.assert_called_once_with('  Keep the measured inputs; clarify scope.  ')
            persist.assert_called_once_with()
            restored = load_session(root)
            self.assertEqual(restored.view().status, 'paused')
            self.assertEqual(restored.view().status_reason, 'Keep the measured inputs; clarify scope.')
            self.assertEqual(restored.view().attempts, before_attempts)
            with patch.object(app.controller, 'pause', side_effect=RuntimeError('Cannot stop a live action')), \
                    patch.object(app, '_persist_application_views') as persist:
                with self.assertRaisesRegex(RuntimeError, 'live action'):
                    app._pause_action('Do not swallow lifecycle errors')
            persist.assert_not_called()

    def test_writing_auto_template_keeps_started_snapshot_across_default_change(self):
        from simple_ar.report.schema import ReportRuntimeConfig
        from simple_ar.report.templates import load_report_template_bundle
        from simple_ar.report.writing import ReportWritingRequest
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = root / "notes.md"
            material.write_text("# Notes\nOne supplied observation.\n", encoding="utf-8")
            app = create_session(ResearchBrief(request_text="Explain notes", requested_outputs=("report",),
                asset_requests=({"locator": str(material), "role": "material"},)), root=root / "session",
                services=ResearchApplicationServices(config={"research_task_kind": "writing"}))
            app.advance(max_actions=2)
            report_context, memory, _, _, _ = app._report_writing_parts()
            config = ReportRuntimeConfig(template="analysis_report")
            template = load_report_template_bundle(report_mode="supplied_materials", config=config)
            memory.template = "analysis_report"
            request = ReportWritingRequest(report_context, memory, config, template, object())
            # Simulate an old writer that froze the resolved default then failed.
            with patch("simple_ar.report.writing.run_report_agent", side_effect=LLMError("Interrupted")):
                app._execute("report_write", "writer", request, ())
            restored = load_session(root / "session")
            self.assertEqual(restored._report_writing_parts()[3].name, "analysis_report")
            with patch.object(restored, "_effective_config", return_value={**restored._effective_config(),
                    "report": {"template": "experiment"}}):
                self.assertEqual(restored._report_writing_parts()[3].name, "experiment")

    def test_writing_uses_frozen_material_and_resumes_without_research_or_processes(self):
        from simple_ar.report.schema import AgentReportResult, ReportSectionDraft, ReportToolCall
        from simple_ar.report.agent import _prompt_handle_view

        for kind in ("writing", "survey"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                material = root / "external results.markdown"
                material.write_text("# Results\nA single external observation suggests a difference.\n", encoding="utf-8")
                app = create_session(ResearchBrief(request_text="Write an honest summary", requested_outputs=("report",),
                    asset_requests=({"locator": str(material), "role": "material"},)), root=root / "session",
                    services=ResearchApplicationServices(config={"research_task_kind": kind,
                        "research_materials_only": True, "research_plan_mode": "deterministic"},
                        budget_limits={"process_invocations": 0}))
                if kind == "survey":
                    with patch.object(app, "_execution_config", return_value={"research_materials_only": False}):
                        self.assertNotIn("research_sources", app._plan_config())
                    with patch.object(app, "_execution_config", return_value={
                            "research_materials_only": False, "research_sources": ["arxiv"]}):
                        self.assertEqual(app._plan_config()["research_sources"], ["arxiv"])
                self.assertEqual(app._plan_config()["research_sources"], ["local_files"])
                view = app.advance(max_actions=2)
                self.assertEqual(view.next_action, "report_write", view.status_reason)
                documents_ref = view.state_refs["documents"]
                self.assertNotIn("synthesis", view.state_refs)
                material.write_text("Changed input that must not replace extracted evidence.", encoding="utf-8")
                app = load_session(root / "session", services=ResearchApplicationServices(
                    llm_client=LLMClient(LLMSettings(api_key="fixture"))))

                def writer(**kwargs):
                    context, memory = kwargs["context"], kwargs["memory"]
                    self.assertEqual(context.report_mode, "supplied_materials")
                    self.assertEqual(context.papers, [])
                    self.assertEqual(context.metric_sources, [])
                    self.assertEqual(context.synthesis_markdown, "")
                    self.assertEqual(kwargs["template"].name, "material_report")
                    handle = memory.source_handles[0]
                    self.assertEqual(_prompt_handle_view(handle)["metadata"]["evidence_role"], "user_supplied_unverified")
                    result = kwargs["gateway"].call(ReportToolCall(tool_name="get_neighbor_chunks", arguments={"handle": handle.handle}))
                    self.assertIn("single external observation", result.content["chunks"][0]["text"])
                    self.assertNotIn("Changed input", json.dumps(result.content))
                    return AgentReportResult(report_body="", memory=memory, used_agent=True,
                        sections=[ReportSectionDraft(section_id="results", heading="Results and Limitations",
                            draft_markdown="The supplied notes describe one external observation. No experiment was conducted in this task; the result is not independently verified.",
                            used_sources=[handle.handle])])

                with patch("simple_ar.report.writing.run_report_agent", side_effect=LLMError("Temporary outage")):
                    view = app.advance(max_actions=1)
                self.assertEqual(view.status, "paused")
                app = load_session(root / "session", services=ResearchApplicationServices(
                    llm_client=LLMClient(LLMSettings(api_key="fixture"))))
                app.continue_session()
                with patch("simple_ar.report.writing.run_report_agent", side_effect=writer):
                    view = app.advance(max_actions=3)
                self.assertEqual(view.status, "completed", view.status_reason)
                self.assertEqual(view.state_refs["documents"], documents_ref)
                self.assertCountEqual([row["capability"] for row in view.attempts],
                                 ["plan", "document_ingest", "report_write", "report_write", "report", "report_audit"])
                self.assertNotIn("experiment", view.state_refs)
                self.assertNotIn("synthesis", view.state_refs)
                self.assertEqual(app.budget_ledger.remaining("process_invocations"), 0)

    def test_article_writer_recovers_then_refreshes_with_prior_draft_and_retained_ledger(self):
        from simple_ar.report.writing import ARTICLE_OWNER, write_whole_article

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            material = root / "observations.md"
            material.write_text("# Observations\nOne recorded observation under one declared condition.\n", encoding="utf-8")
            client = LLMClient(LLMSettings(api_key="fixture"))
            config = {"research_task_kind": "writing", "research_materials_only": True,
                      "research_plan_mode": "deterministic", "report_writing_method": "article",
                      "report": {"reviewer": "disabled", "max_review_iterations": 0,
                                 "allow_source_backtracking": False, "outline_strategy": "auto"}}
            app = create_session(ResearchBrief(request_text="Explain the observation without claiming verification",
                requested_outputs=("report",), asset_requests=({"locator": str(material), "role": "material"},)),
                root=root / "session", services=ResearchApplicationServices(config=config,
                    llm_client=client, budget_limits={"process_invocations": 0}))
            self.assertEqual(app.advance(max_actions=2).next_action, "report_write")
            material.write_text("Changed external source, not accepted evidence.", encoding="utf-8")
            article = "## Observations\n\nThe supplied material records one observation under one declared condition.\n\n## Interpretation and limits\n\nThis task did not run an experiment or independently verify the observation. It cannot establish comparative performance or reliability."
            def write(system, prompt, **kwargs):
                inputs = json.loads(prompt)
                self.assertIn("recorded observation", json.dumps(inputs))
                self.assertNotIn("Changed external source", json.dumps(inputs))
                return {"draft_markdown": article}
            def persist_then_interrupt(**kwargs):
                write_whole_article(**kwargs)
                raise RuntimeError("Worker interrupted after saving the original response")
            with patch.object(LLMClient, "ask_json", side_effect=write) as model, patch(
                    "simple_ar.report.writing.write_whole_article", side_effect=persist_then_interrupt):
                view = app.advance(max_actions=1)
                self.assertEqual(model.call_count, 1, view.status_reason)
            self.assertEqual(view.status, "paused")
            app = load_session(root / "session", services=ResearchApplicationServices(llm_client=client))
            app.continue_session()
            with patch.object(LLMClient, "ask_json", side_effect=AssertionError("cached article replayed")), patch(
                    "simple_ar.report.writing.run_report_agent", side_effect=AssertionError("old Writer scheduled")):
                view = app.advance(max_actions=3)
            self.assertEqual(view.status, "completed", view.status_reason)
            self.assertEqual(view.state_refs["writer"].schema, ARTICLE_OWNER)
            report = app.controller.store.read_text(app.controller.manifest.state_refs["report"])
            self.assertIn("Interpretation and limits", report)
            self.assertNotIn("experiment", view.state_refs)
            app.budget_ledger.reserve("retained-unknown", {"llm_requests": 1})
            app.budget_ledger.save()
            ledger = (root / "session" / "budget_ledger.json").read_bytes()
            attempts = view.attempts
            restored = load_session(root / "session", services=ResearchApplicationServices(llm_client=client))
            with patch.object(LLMClient, "ask_json", side_effect=AssertionError("terminal recovery calls model")):
                recovered = restored.advance(max_actions=3)
            self.assertEqual(recovered.attempts, attempts)
            self.assertEqual((root / "session" / "budget_ledger.json").read_bytes(), ledger)
            initial_ref = restored.controller.manifest.state_refs["report"]
            with self.assertRaises(ResearchApplicationError):
                restored.request_report(report_config={"reviewer": "llm"})
            self.assertEqual(restored.controller.manifest.state_refs["report"], initial_ref)
            restored.request_report(refresh=True)
            def revise(system, prompt, **kwargs):
                self.assertEqual(json.loads(prompt)["prior_draft"], article)
                return {"draft_markdown": article.replace("## Observations", "## Decision note")}
            with patch.object(LLMClient, "ask_json", side_effect=revise) as model:
                refreshed = restored.advance(max_actions=3)
            self.assertEqual(model.call_count, 1)
            self.assertEqual(refreshed.status, "completed", refreshed.status_reason)
            self.assertNotEqual(refreshed.state_refs["report"], initial_ref)
            self.assertEqual(restored.controller.store.read_text(initial_ref), report)

    def test_writing_preserves_paper_material_roles_and_missing_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = [("paper.md", "paper", "# Prior work\nA published-source claim is not a local measurement."),
                      ("notes.md", "material", "# Notes\nUser-provided observations."),
                      ("empty.md", "material", "")]
            for name, _, body in inputs:
                (root / name).write_text(body, encoding="utf-8")
            app = create_session(ResearchBrief(request_text="Use the supplied inputs", requested_outputs=("report",),
                asset_requests=tuple({"locator": str(root / name), "role": role} for name, role, _ in inputs)),
                root=root / "session", services=ResearchApplicationServices(config={"research_task_kind": "writing"}))
            view = app.advance(max_actions=2)
            self.assertEqual(view.next_action, "report_write", view.status_reason)
            context, memory = app.report_inputs()
            self.assertEqual(len(context.papers), 1)
            self.assertEqual([handle.kind for handle in context.source_handles], ["paper", "material", "material"])
            self.assertFalse(context.metric_sources)
            self.assertIn("Unavailable material text: empty", " ".join(memory.limitations))
            self.assertNotIn("synthesis", view.state_refs)

    def test_experiment_inputs_share_provenance_without_requiring_fake_design(self):
        from simple_ar.core.capabilities import ArtifactRef

        for kind, expected in (("research", ["design", "runtime_config", "preparation"]),
                               ("measurement", ["runtime_config", "preparation"]),
                               ("reproduction", ["runtime_config", "read", "synthesis", "preparation"])):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                app = create_session(ResearchBrief(request_text="Check the accepted inputs."), root=Path(tmp) / "session")
                refs = {name: ArtifactRef(f"{name}.json") for name in expected}
                app.controller.manifest.state_refs.update(refs)
                with patch.object(app, "_task_kind", return_value=kind):
                    self.assertEqual(app._experiment_input_refs(), tuple(refs[name] for name in expected))
                    del app.controller.manifest.state_refs["preparation"]
                    self.assertEqual(app._experiment_input_refs(), tuple(refs[name] for name in expected[:-1]))
                    del app.controller.manifest.state_refs["runtime_config"]
                    with self.assertRaisesRegex(ResearchApplicationError, "runtime_config"):
                        app._experiment_input_refs()

    def test_survey_source_bound_and_user_request_reach_report_without_truncating_search(self):
        from simple_ar.report.schema import ReportContext, ReportMemory

        goal = "Compare three primary studies and explain uncertainty, not a broad literature survey."
        with tempfile.TemporaryDirectory() as tmp:
            app = create_session(
                ResearchBrief(request_text=goal, objective="Study comparison",
                              requested_outputs=("report",), hard_constraints=("At most three citations.",)),
                root=Path(tmp) / "session",
                services=ResearchApplicationServices(config={
                    "research_task_kind": "survey", "report": {"max_cited_sources": 3},
                }),
            )
            plan = SimpleNamespace(source_plan=SimpleNamespace(budget={"max_documents": 12}))
            self.assertEqual(app._search_limit(plan), 12)
            context = ReportContext(topic="Study comparison", report_mode="research_only")
            with patch.object(app, "report_inputs", return_value=(context, ReportMemory())):
                projected, memory, config, _, _ = app._report_writing_parts()
            self.assertEqual(config.max_cited_sources, 3)
            self.assertEqual(projected.max_section_sources, 3)
            self.assertEqual(projected.survey_contract["max_cited_sources"], 3)
            self.assertIn(goal, projected.problem_markdown)
            self.assertIn("At most three citations.", memory.objective)
            self.assertIn("at most 3 distinct sources across all sections", memory.objective)
            app.services = replace(app.services, llm_client=object())
            with patch.object(app, '_execute', return_value=True) as execute:
                self.assertTrue(app._run_plan_action())
            planning = execute.call_args.args[2]
            self.assertTrue(planning.use_llm)  # Open search needs scientific queries and constraints.
            self.assertFalse(planning.task_plan_request.use_llm)  # Fixed dispatch needs no model call.
            from simple_ar.research.contracts import QueryPlan, SourcePlan
            from simple_ar.research.planning.capability import ResearchPlanResult
            from simple_ar.research.sources.capability import SearchResult
            for model, auto, rounds, cap, expected in (
                (object(), True, 2, 3, 1), (None, True, 2, 3, 3),
                (object(), False, 2, 3, 3), (object(), True, 1, 3, 3), (object(), True, 2, 1, 1),
            ):
                with self.subTest(auto=auto, rounds=rounds, cap=cap, model=model is not None):
                    app.services = replace(app.services, llm_client=model)
                    planned = ResearchPlanResult((), QueryPlan("Study", ["seed"], queries=["seed"],
                        auto_expansion=auto, max_rounds=rounds), SourcePlan(["seed"], require_fulltext=True,
                        allow_pdf_download=True, budget={"max_documents": cap, "max_fulltext_documents": 6}))
                    with patch.object(app, "_load_plan", return_value=planned), patch.object(app, "_input_refs", return_value=()), \
                            patch.object(app, "_execute", return_value=True) as execute, \
                            patch.object(app, "_load_search", return_value=SearchResult("completed", (), selected_papers=(object(),))):
                        self.assertTrue(app._run_search_action())
                    policy = execute.call_args.kwargs["selection_policy"]
                    self.assertEqual(policy.max_documents, expected)
                    reserved = model is not None and auto and rounds > 1
                    self.assertEqual(policy.next_query_limit, 0 if reserved else 3)
                    self.assertEqual(policy.query_plan.max_rounds, 1 if reserved else rounds)
                    # Source selection owns its bound; acquisition must not
                    # silently reserve two more slots and starve known papers.
                    app.controller.manifest.state_refs["search"] = app.controller.manifest.state_refs["brief"]
                    with patch.object(app, "_load_plan", return_value=planned), patch.object(app, "_input_refs", return_value=()), \
                            patch.object(app, "_execute", return_value=False) as execute, \
                            patch.object(app, "_load_search", return_value=SearchResult("completed", (), selected_papers=(object(),))):
                        self.assertFalse(app._run_document_ingest_action())
                    self.assertEqual(execute.call_args.args[2].source_plan.budget, planned.source_plan.budget)

    def test_evidence_followup_cycle_recovers_empty_ingest_and_keeps_base_refs(self):
        from simple_ar.core.capabilities import CapabilityResult
        from simple_ar.literature.models import Paper
        from simple_ar.research.contracts import QueryPlan, SourcePlan, ResearchQuestion
        from simple_ar.research.documents.ingest import build_document_bundle
        from simple_ar.research.evidence.reader import ReadRequest
        from simple_ar.research.planning.capability import ResearchPlanResult
        from simple_ar.research.sources.base import SearchResponse
        from simple_ar.research.sources.capability import SearchResult
        from simple_ar.research.sources.registry import SearchProviderRegistry

        for outcome in ("empty", "abstract", "exhausted", "web", "mixed"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                webpage = outcome in {"web", "mixed"}
                old = Paper("old", "Seed study", [], "Original evidence." + (
                    " Author materials: https://example.test/code" if webpage else ""), "", source="fixture")
                fresh = Paper("new", "Additional study on new question", [], "New abstract evidence.",
                    "https://example.invalid/new.html", source="fixture", fulltext_url="https://example.invalid/new.html")
                source = SourcePlan(["seed"], sources=["fixture"], require_fulltext=outcome == "exhausted" or webpage,
                    allow_pdf_download=True, budget={"max_documents": 3, "max_follow_up_queries": 1,
                                                   "max_fulltext_documents": 2 if webpage else 1,
                                                   "max_fulltext_fetch_attempts": 2, "max_pdf_mb": 1})
                planned = ResearchPlanResult((ResearchQuestion("q", "Study", "general", "Compare evidence"),),
                    QueryPlan("Study", ["seed"], queries=["seed"], max_rounds=2), source)
                bundle = build_document_bundle(papers=[old], source_plan=source, cache_dir=None, extraction_dir=Path(tmp))
                if outcome == "exhausted":
                    bundle.fulltext_manifest.update(cached_count=1, fetch_attempt_count=2)
                def base_search(*, context, **_):
                    value = SearchResult("completed", (SearchResponse("fixture", "seed", [old]),),
                        papers=(old,), selected_papers=(old,), response_rounds=(1,))
                    return CapabilityResult("completed", (context.store.write_json("search_result.json", value.to_handoff_dict(),
                        kind="search_result", schema="search_handoff.v1", producer="test"),))
                def base_ingest(*, context, **_):
                    return CapabilityResult("completed", (context.store.write_json("document_bundle.json", bundle.to_handoff_dict(),
                        kind="document_bundle", schema="document_bundle.v1", producer="test"),))
                connector = SimpleNamespace(source_name="fixture", search=lambda request: SearchResponse("fixture", request.query,
                    [] if outcome == "empty" else [old, fresh]))
                services = ResearchApplicationServices(config={"research_task_kind": "survey", "interaction": "autonomous"},
                    search_registry=SearchProviderRegistry({"fixture": lambda: connector}))
                app = create_session(ResearchBrief(request_text="Study", requested_outputs=("report",)),
                                     root=Path(tmp) / "session", services=services)
                bundle.fulltext_manifest["cache_dir"] = str(app._cache_dir("literature"))
                refs = app.controller.manifest.state_refs
                refs["plan"] = app.controller.store.write_json("planning/plan.json", planned.to_handoff_dict(),
                    kind="research_plan", schema="research_plan.v1", producer="test")
                task = TaskPlanResult("survey", "Study", tuple(TaskPlanStep(a, a, c, s, "", "") for a, c, s in (
                    ("search", "search", "search"), ("document_ingest", "document_ingest", "documents"),
                    ("read", "read", "read"), ("synthesize", "synthesize", "synthesis"),
                    ("report_write", "report_write", "writer"))), "deterministic")
                refs["task_plan"] = app.controller.store.write_json("planning/task_plan.json", task.to_handoff_dict(),
                    kind="task_plan", schema="research_task_plan.v1", producer="test")
                app.controller.registry.register("search", base_search, replace=True)
                app.controller.registry.register("document_ingest", base_ingest, replace=True)
                app._execute("search", "search", None, ())
                app._execute("document_ingest", "documents", None, ())
                with patch("simple_ar.app.research_application.new_source_queries",
                           return_value=() if outcome == "web" else ("seed", "new question")):
                    app._execute("read", "read", ReadRequest(bundle=bundle, topic="Study"), ())
                    base_refs = {name: refs[name] for name in ("search", "documents", "read")}
                    # Supplied cached bundles are merged after acquisition;
                    # they must not consume the reserved followup allowance.
                    acquired = bundle.fulltext_manifest["document_count"]
                    retained_bundle = replace(bundle, records=[*bundle.records,
                        replace(bundle.records[0], document_id="retained-a", metadata={"retained_bundle": "saved.json"}),
                        replace(bundle.records[0], document_id="retained-b", metadata={"retained_bundle": "saved.json"})])
                    refs["documents"] = app.controller.store.write_json("retained-input-check.json",
                        retained_bundle.to_handoff_dict(), kind="document_bundle", schema="document_bundle.v1", producer="test")
                    self.assertEqual(app._evidence_followup_inputs()[-1], 3 - acquired)
                    refs["documents"] = base_refs["documents"]
                    self.assertEqual(app._next_action(), "search_evidence:1")
                    app = load_session(app.controller.store.root, services=services)
                    with patch.object(app, "_provider_registry", wraps=app._provider_registry) as providers:
                        self.assertTrue(app._run_action("search_evidence:1"), app.view().status_reason)
                        if outcome == "web":
                            providers.assert_not_called()
                    self.assertEqual([r.query for r in app._load_search().responses], ["seed"])
                    followup_responses = app._state_payload("search_evidence_1")["responses"]
                    self.assertEqual([r["query"] for r in followup_responses
                                      if r["source"] != "supporting_material"],
                                     [] if outcome == "web" else ["new question"])
                    local_candidates = [r for r in followup_responses if r["source"] == "supporting_material"]
                    self.assertEqual(len(local_candidates), int(outcome == "mixed"))
                    if local_candidates:
                        self.assertIn("no provider request", local_candidates[0]["message"])
                    if outcome not in {"empty", "web"}:
                        selected = app._state_payload("search_evidence_1")
                        self.assertIn("new", selected["selected_paper_ids"])
                        self.assertEqual(len(selected["selected_paper_ids"]), 2 if outcome == "mixed" else 1)
                        self.assertTrue(any(row["reason"] == "already_retained_source"
                                            for row in selected["selection"]))
                    from io import BytesIO
                    from simple_ar.research.documents.fulltext import _fetch_remote_hint
                    def response_for(*args, **kwargs):
                        response = BytesIO(b"<html><p>Author states CPU use; this is not measured runtime.</p></html>")
                        response.headers = {"Content-Type": "text/html"}
                        return response
                    with patch("simple_ar.research.preparation_assets.public_document_response", side_effect=response_for), patch(
                            "simple_ar.research.documents.fulltext.urllib.request.urlopen", side_effect=response_for), patch(
                            "simple_ar.research.documents.fulltext._fetch_remote_hint",
                            side_effect=_fetch_remote_hint if webpage else None) as fetch:
                        with patch.object(app, "_record_attempt_outputs", side_effect=KeyboardInterrupt):
                            with self.assertRaises(KeyboardInterrupt):
                                app._run_action("ingest_evidence:1")
                        if not webpage:
                            fetch.assert_not_called()
                    count = len(app.controller.list_attempts())
                    app = load_session(app.controller.store.root, services=services)
                    app._reconcile_running_attempt()
                    self.assertEqual(len(app.controller.list_attempts()), count)

                    self.assertEqual(app._next_action(), "read_evidence:1")
                    self.assertEqual(app._input_refs("documents")[0], base_refs["documents"])
                    self.assertTrue(app._run_action("read_evidence:1"))
                    self.assertEqual(app._next_action(), "synthesize")
                    self.assertEqual({n: app.controller.manifest.state_refs[n] for n in base_refs}, base_refs)
                    self.assertNotEqual(app._input_refs("documents")[0], app.controller.manifest.state_refs["documents_evidence_1"])
                    expected = 1 if outcome == "empty" else 3 if outcome == "mixed" else 2
                    self.assertEqual(len(app._load_documents().records), expected)
                    if webpage:
                        linked = [r for r in app._load_documents().records if r.metadata.get("kind") == "supporting_material"]
                        self.assertEqual(len(linked), 1)
                        self.assertIn(linked[0].url, linked[0].metadata["parent_quote"])
                        self.assertNotIn(linked[0].document_id, {c.paper_id for c in app._load_read().paper_cards})
                        self.assertTrue(any(c.document_id == linked[0].document_id for c in app._load_documents().chunks))
                        from simple_ar.research.evidence.reader import read_documents
                        fresh_bundle = type(bundle).from_handoff_dict(app._state_payload("documents_evidence_1"))
                        dropped = [{"paper_id": r.document_id, "decision": "drop"}
                                   for r in fresh_bundle.records if r.metadata.get("kind") != "supporting_material"]
                        with patch("simple_ar.research.evidence.reader.screen_papers_with_llm", return_value=dropped) as screen, patch(
                                "simple_ar.research.evidence.reader.read_paper_notes_with_llm", return_value=[]) as notes:
                            observed = read_documents(ReadRequest(bundle=fresh_bundle, topic="CPU cost",
                                use_llm=True, llm_client=object()))
                        if outcome == "web":
                            screen.assert_not_called()
                        else:
                            self.assertTrue(all(r["metadata"].get("kind") != "supporting_material"
                                for r in screen.call_args.kwargs["papers"]))
                        self.assertIn(linked[0].document_id, notes.call_args.kwargs["evidence_snippets_by_document"])
                        self.assertEqual(observed.paper_cards, ())
                    self.assertEqual(len(app._load_search().selected_papers), expected - int(webpage))
                    self.assertEqual(app._load_search().response_rounds,
                                     (1,) if outcome == "web" else (1, 2, 2) if outcome == "mixed" else (1, 2))
                    with patch.object(app, "_report_writing_parts", return_value=(
                        SimpleNamespace(source_handles=[]), None, None, None, None)), \
                         patch.object(app, "_execute", return_value=True) as write:
                        self.assertTrue(app._run_report_write_action())
                        document_inputs = [ref for ref in write.call_args.args[3]
                                           if ref.schema == "document_bundle.v1"]
                        self.assertEqual(document_inputs, [app._input_refs("documents")[0]])
                    raw = app._state_payload("documents_evidence_1")
                    if outcome == "exhausted":
                        self.assertEqual(raw["fulltext_manifest"]["budget"]["max_fulltext_documents"], 0)
                        self.assertEqual(raw["fulltext_manifest"]["budget"]["max_fulltext_fetch_attempts"], 0)
                        self.assertTrue(raw["fulltext_manifest"]["allow_pdf_download"])
                    count = len(app.controller.list_attempts())
                    app = load_session(app.controller.store.root, services=services)
                    self.assertEqual(app._next_action(), "synthesize")
                    self.assertEqual(len(app.controller.list_attempts()), count)

    def test_separate_feasibility_client_uses_session_and_attempt_budget(self):
        from simple_ar.research.design import ResearchDesignRequest

        class Client:
            def __init__(self):
                self.bindings = []

            def with_budget(self, ledger, *, session_id="", attempt_id=""):
                self.bindings.append((ledger, session_id, attempt_id))
                return self

        author, reviewer = Client(), Client()
        with tempfile.TemporaryDirectory() as tmp:
            app = create_session(
                ResearchBrief(request_text="Design a bounded experiment."),
                root=Path(tmp) / "session",
                services=ResearchApplicationServices(
                    llm_client=author, feasibility_llm_client=reviewer,
                    config={"feasibility_review_model": "review-model"},
                ),
            )
            request = ResearchDesignRequest(synthesis={}, use_llm=True, llm_client=author,
                                            feasibility_llm_client=reviewer)
            bound = app._request_for_attempt(request, "research-design-0001")
            self.assertIs(bound.feasibility_llm_client, reviewer)
            self.assertEqual(reviewer.bindings[-1],
                             (app.budget_ledger, app.controller.manifest.session_id, "research-design-0001"))
            restored_reviewer = Client()
            restored = load_session(Path(tmp) / "session", services=ResearchApplicationServices(
                llm_client=Client(), feasibility_llm_client=restored_reviewer,
            ))
            self.assertEqual(restored.services.config["feasibility_review_model"], "review-model")
            self.assertEqual(restored_reviewer.bindings[-1][1], app.controller.manifest.session_id)

    def test_report_input_gap_pauses_without_uncaught_exception_or_writer_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_session(
                ResearchBrief(request_text="Report measured evidence.", requested_outputs=("report",)),
                root=Path(tmp) / "session",
            )
            with patch.object(
                app, "_report_writing_parts",
                side_effect=ResearchApplicationError("Analyze the latest measurement first."),
            ):
                self.assertFalse(app._run_action("report_write"))
            view = app.view()
            self.assertEqual(view.status, "paused")
            self.assertIn("Analyze the latest measurement first", view.status_reason)
            self.assertNotIn("writer", view.state_refs)

    def test_failed_run_diagnosis_keeps_captured_runtime_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_session(
                ResearchBrief(request_text="Explain a failed candidate."),
                root=Path(tmp) / "session",
            )
            diagnosis = app.controller.store.write_json(
                "attempts/experiment-0001/diagnosis.json", {
                    "status": "failed", "summary": "Missing declared metrics.",
                    "deficiencies": [{"category": "runtime", "code": "nonzero_returncode",
                                      "message": "Process returned 1."}],
                    "context": {"stderr_tail": "Traceback: TypeError: invalid argument type"},
                }, kind="experiment_diagnosis", producer="test",
            )
            result = app.controller.store.write_json(
                "attempts/experiment-0001/results.json", {
                    "status": "failed", "execution_status": "failed",
                    "artifacts": {"diagnosis": "diagnosis.json"},
                }, kind="experiment_result", producer="test",
            )
            self.assertEqual(app._experiment_diagnosis_ref(result).path, diagnosis.path)
            from simple_ar.report.execution_evidence import compact_failure_diagnosis
            compact = compact_failure_diagnosis(app.controller.store.read_json(diagnosis))
            self.assertIn("TypeError", compact["stderr_tail"])
            self.assertEqual(compact["deficiencies"][0]["code"], "nonzero_returncode")

    def test_failed_followup_observation_reaches_reanalysis_in_new_and_saved_plans(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_session(
                ResearchBrief(request_text="Analyze a bounded failed observation."),
                root=Path(tmp) / "session",
            )
            candidate = TaskPlanStep(
                step_id="candidate", action="research_candidate:1", capability="experiment",
                state_name="experiment_revision_1", problem_solved="", observation="",
            )
            failed_ref = app.controller.store.write_json(
                "outputs/failed_revision.json", {"status": "failed", "execution_status": "failed"},
                kind="experiment_result", schema="experiment_result.v1", producer="test",
            )
            baseline_ref = app.controller.store.write_json(
                "outputs/baseline.json", {"status": "passed", "execution_status": "passed"},
                kind="experiment_result", schema="experiment_result.v1", producer="test",
            )
            app.controller.manifest.state_refs["experiment_revision_1"] = failed_ref
            app.controller.manifest.state_refs["baseline"] = baseline_ref
            for condition in (
                "after_observation:experiment_revision_1",
                "after_success:experiment_revision_1",  # Older persisted plan.
            ):
                analysis = TaskPlanStep(
                    step_id="reanalysis", action="reanalysis:1", capability="analysis",
                    state_name="analysis_r1", problem_solved="", observation="",
                    condition=condition,
                )
                plan = TaskPlanResult(
                    task_kind="research", goal="Analyze the failed attempt.",
                    steps=(candidate, analysis), mode="deterministic",
                )
                app.controller.manifest.state_refs["task_plan"] = app.controller.store.write_json(
                    f"outputs/plan-{condition.split(':', 1)[0]}.json", plan.to_handoff_dict(),
                    kind="task_plan", schema="research_task_plan.v1", producer="test",
                )
                with patch.object(app, "_load_task_plan", return_value=plan), patch.object(
                    app, "_step_completed", side_effect=lambda step: step.action == candidate.action,
                ):
                    self.assertEqual(
                        app._failed_followup_measurement(1),
                        ("experiment_revision_1", failed_ref),
                    )
                    self.assertTrue(app._step_condition_applies(analysis))
                    with patch.object(app, "_analysis_context", return_value={}), patch.object(
                        app, "_execute", return_value=True,
                    ) as execute:
                        self.assertTrue(app._run_action("reanalysis:1"))
                    self.assertEqual(execute.call_args.kwargs["result_ref"], failed_ref)
                    self.assertEqual(execute.call_args.kwargs["baseline_ref"], baseline_ref)

    def test_repaired_revision_selects_latest_measurement_in_plan_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_session(
                ResearchBrief(request_text="Compare the repaired candidate."),
                root=Path(tmp) / "session",
            )
            steps = tuple(TaskPlanStep(
                step_id=action, action=action, capability="experiment",
                state_name=state, problem_solved="", observation="",
            ) for action, state in (
                ("research_candidate:1", "experiment_revision_1"),
                ("retest_candidate:1_2", "experiment_revision_1_repair_2"),
                ("retest_candidate:1_10", "experiment_revision_1_repair_10"),
            ))
            refs = app.controller.manifest.state_refs
            for step in steps:
                failed = step == steps[0]
                refs[step.state_name] = app.controller.store.write_json(
                    f"outputs/{step.state_name}.json",
                    {"status": "failed" if failed else "passed"},
                    kind="experiment_result", schema="experiment_result.v1", producer="test",
                )
            plan = TaskPlanResult(
                task_kind="research", goal="Compare revised measurements.",
                steps=steps, mode="deterministic",
            )
            with patch.object(app, "_load_task_plan", return_value=plan), patch.object(
                app, "_step_completed", return_value=True,
            ):
                self.assertEqual(app._revision_candidate_refs(1)[-1],
                                 refs["experiment_revision_1_repair_10"])
                self.assertIsNone(app._failed_followup_measurement(1))

    def test_resume_requires_model_binding_from_runtime_or_legacy_usage_not_allowance(self):
        for origin in ("runtime", "legacy_usage", "authorized_allowance"):
            with self.subTest(origin=origin), tempfile.TemporaryDirectory() as tmp:
                session = Path(tmp) / "session"
                app = create_session(
                    ResearchBrief(request_text="Study the supplied question.", requested_outputs=("summary",)),
                    root=session,
                    services=ResearchApplicationServices(
                        llm_client=object() if origin == "runtime" else None,
                    ),
                )
                self.assertEqual(app.controller.store.read_json(
                    app.view().state_refs["runtime_config"])["llm_required"], origin == "runtime")
                if origin == "legacy_usage":
                    app.budget_ledger.reserve("legacy-call", {"llm_requests": 1})
                    app.budget_ledger.save()
                elif origin == "authorized_allowance":
                    app.budget_ledger.authorize_remaining(
                        {"llm_requests": 3}, authorization_id="additional-allowance",
                        reason="Allow future model calls if explicitly configured.",
                    )
                resumed = load_session(session)
                if origin == "authorized_allowance":
                    resumed.require_llm_binding(include_legacy_usage=True)
                elif origin == "legacy_usage":
                    with self.assertRaisesRegex(ResearchApplicationError, "cannot silently continue"):
                        resumed.require_llm_binding(include_legacy_usage=True)
                else:
                    before = resumed.view()
                    with self.assertRaisesRegex(ResearchApplicationError, "cannot silently continue"):
                        resumed.advance()
                    with self.assertRaisesRegex(ResearchApplicationError, "cannot silently continue"):
                        resumed.continue_session()
                    self.assertEqual(resumed.view().state_refs, before.state_refs)
                    self.assertEqual(resumed.view().attempts, before.attempts)
                    load_session(session, services=ResearchApplicationServices(
                        llm_client=object(),
                    )).require_llm_binding()


    def test_prepared_reproduction_runs_fixed_command_and_preserves_paper_report_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            protocol = {"contract_id": "reproduce-source-subset", "hypothesis": "Check one published conclusion",
                        "dataset": "Declared synthetic adaptation", "expected_outcome": "Observed value near 0.7; not a full paper reproduction",
                        "metrics": ["observed"], "comparison_conditions": {"scope": "adapted", "seed": 3}}
            app, paper = self._paper_session(
                root,
                "# A source claim\nThe stated method has a reference value under stated conditions.\n",
                request_text="Reproduce the supplied conclusion without innovation.",
                intents=("reproduction",),
                requested_outputs=("experiments",),
                services=ResearchApplicationServices(config={"research_task_kind": "reproduction", "research_materials_only": True,
                    "interaction": "autonomous", "execution": {"command": [sys.executable, "-c", "print('observed: 0.7')"],
                    "cwd": str(root), "timeout_sec": 5, "protocol": protocol, "result_schema": {"primary_metric": "observed"}}},
                    budget_limits={"process_invocations": 1, "process_wall_seconds": 10}),
            )
            view = app.advance(max_actions=12)
            self.assertEqual(view.status, "completed", view.status_reason)
            self.assertNotIn("design", view.state_refs)
            self.assertNotIn("assessment", view.state_refs)
            self.assertNotIn("implementation", view.state_refs)
            measurement = app.latest_experiment_ref()
            result = app.controller.store.read_json(measurement)
            self.assertEqual(result["experiment_contract"]["hypothesis"], protocol["hypothesis"])
            self.assertEqual(result["metrics"]["observed"], 0.7)
            with patch.object(app, "_load_documents", wraps=app._load_documents) as documents, \
                    patch.object(app, "_load_synthesis", wraps=app._load_synthesis) as synthesis, \
                    patch.object(app, "_load_read", wraps=app._load_read) as read:
                context, _ = app.report_inputs()
            documents.assert_called_once()
            synthesis.assert_called_once()
            read.assert_called_once()
            self.assertEqual(context.hypothesis_markdown, protocol["hypothesis"])
            self.assertEqual(context.experiment_plan["dataset"], protocol["dataset"])
            self.assertTrue(context.papers)
            resumed = load_session(root / "session")
            self.assertEqual(resumed.advance(max_actions=1).status, "completed")
            self.assertEqual(resumed.latest_experiment_ref(), measurement)
            self.assertEqual(len(resumed.view().attempts), len(view.attempts))
            reopened = resumed.request_report()
            self.assertEqual(reopened.next_action, "plan")
            extended = resumed.advance(max_actions=1)
            self.assertEqual(extended.next_action, "report_write", extended.status_reason)
            self.assertEqual(resumed.latest_experiment_ref(), measurement)
            self.assertEqual(sum(a["capability"] == "experiment" for a in extended.attempts), 1)

    def test_fixed_protocol_technical_retry_does_not_require_research_design(self):
        for kind in ("measurement", "reproduction"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paper = root / "paper.md"
                paper.write_text("# Reference\nCheck the fixed protocol result.\n", encoding="utf-8")
                config = {"research_task_kind": kind, "research_materials_only": kind == "reproduction", "interaction": "autonomous",
                    "execution": {"command": [sys.executable, "-c", "raise SystemExit(2)"], "cwd": str(root), "timeout_sec": 5,
                        "protocol": {"hypothesis": "Check an existing conclusion", "dataset": "Synthetic fixture",
                            "expected_outcome": "Value 0.7", "metrics": ["observed"]},
                        "result_schema": {"primary_metric": "observed"}}}
                app = create_session(ResearchBrief(request_text="Run the declared protocol.", intents=(kind,), requested_outputs=("experiments",),
                    asset_requests=({"locator": str(paper), "role": "paper"},) if kind == "reproduction" else ()),
                    root=root / "session", services=ResearchApplicationServices(config=config,
                        budget_limits={"process_invocations": 2, "process_wall_seconds": 10}))
                self.advance_to(app, "experiment")
                app.advance()
                retried = app.retry_experiment(command=(sys.executable, "-c", "print('observed: 0.7')"), cwd=root, timeout_sec=5)
                self.assertEqual(retried.status, "completed", retried.status_reason)
                self.assertNotIn("design", retried.state_refs)
                result = app.controller.store.read_json(retried.state_refs["experiment"])
                self.assertEqual(result["metrics"]["observed"], 0.7)
                self.assertEqual(len([a for a in retried.attempts if a["capability"] == "experiment"]), 2)

    def test_direct_measurement_runs_without_research_discovery_or_design(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_session(ResearchBrief(
                request_text="Measure the supplied command once.",
                intents=("measurement",), requested_outputs=("experiments",),
            ), root=root / "session", services=ResearchApplicationServices(
                config={"research_task_kind": "measurement", "interaction": "autonomous", "execution": {
                    "command": [sys.executable, "-c", "print('accuracy: 0.7')"],
                    "cwd": str(root), "timeout_sec": 5, "result_schema": {"primary_metric": "accuracy"},
                }}, budget_limits={"process_invocations": 1, "process_wall_seconds": 10},
            ))
            completed = app.advance(max_actions=8)
            self.assertEqual(completed.status, "completed", completed.status_reason)
            self.assertIn("no research candidate", completed.status_reason)
            self.assertEqual([row["capability"] for row in reversed(completed.attempts)], ["plan", "experiment", "analysis"])
            self.assertNotIn("search", completed.state_refs)
            self.assertNotIn("design", completed.state_refs)
            self.assertEqual(app.controller.store.read_json(completed.state_refs["experiment"])["status"], "passed")
            resumed = load_session(root / "session")
            self.assertEqual(resumed.view().status, "completed")
            self.assertEqual(len(resumed.view().attempts), 3)

    def test_explicit_interrupted_recovery_retries_current_step_without_losing_evidence(self):
        from simple_ar.core.budget import BudgetConflictError

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = root / "paper.md"
            paper.write_text("# Evidence\nA bounded source.\n", encoding="utf-8")
            session = root / "session"
            app = create_session(ResearchBrief(
                request_text="Summarize the supplied evidence.",
                requested_outputs=("summary",),
                asset_requests=({"locator": str(paper), "role": "paper"},),
            ), root=session, services=ResearchApplicationServices(
                config={"research_materials_only": True},
                budget_limits={"llm_requests": 1, "total_tokens": 100},
            ))
            inputs = dict(app.view().state_refs)

            def interrupt(*, context, **_):
                app.budget_ledger.reserve("interrupted-plan-call", {"llm_requests": 1, "total_tokens": 60},
                    attempt_id=context.attempt.attempt_id, purpose="Interrupted LLM planning request")
                raise KeyboardInterrupt()

            app.controller.registry.register("plan", interrupt, replace=True)
            with self.assertRaises(KeyboardInterrupt):
                app.advance()
            app = load_session(session)
            self.assertEqual(app.view().status, "running")
            self.assertEqual(app.view().next_action, "plan")
            with self.assertRaisesRegex(ResearchApplicationError, "no persisted result"):
                app.advance()
            with self.assertRaisesRegex(ResearchApplicationError, "running/empty"):
                app.continue_session()
            self.assertEqual(app.budget_ledger.entries[0].status, "reserved")
            with self.assertRaisesRegex(BudgetConflictError, "calls remain in flight"):
                app.budget_ledger.authorize_remaining({"llm_requests": 2, "total_tokens": 120},
                    authorization_id="after-confirmed-stop", reason="Allow future calls only after stopping the old worker.")

            recovered = app.recover_interrupted_attempt(reason="Confirmed stopped test worker.")
            self.assertEqual(recovered.status, "running")
            self.assertEqual(recovered.next_action, "plan")
            self.assertEqual(recovered.budget["attempts"], 1)
            self.assertEqual(recovered.attempts[0]["status"], "failed")
            old_usage = app.budget_ledger.entries[0]
            self.assertEqual(old_usage.status, "unknown")
            self.assertEqual(old_usage.actual, {})
            self.assertEqual(old_usage.reserved, {"llm_requests": 1, "total_tokens": 60})
            self.assertEqual(old_usage.attempt_id, recovered.attempts[0]["attempt_id"])
            self.assertEqual(app.budget_ledger.remaining("total_tokens"), 40)
            unknown_record = old_usage.to_dict()
            app.budget_ledger.authorize_remaining({"llm_requests": 2, "total_tokens": 120},
                authorization_id="after-confirmed-stop", reason="Explicit allowance for future calls; old usage stays unknown.")
            self.assertEqual(app.budget_ledger.remaining("llm_requests"), 2)
            self.assertEqual(app.budget_ledger.remaining("total_tokens"), 120)
            self.assertEqual(app.budget_ledger.entries[0].to_dict(), unknown_record)
            self.assertEqual(BudgetLedger.load(session / "budget_ledger.json").entries[0].to_dict(), unknown_record)
            for name, ref in inputs.items():
                self.assertEqual(recovered.state_refs[name], ref)
            with self.assertRaisesRegex(ResearchApplicationError, "exactly one current running"):
                app.recover_interrupted_attempt()
            resumed = app.advance(max_actions=1)
            self.assertEqual(resumed.attempts[1]["capability"], "plan")
            self.assertIn("task_plan", resumed.state_refs)
            self.assertEqual(app.budget_ledger.entries[0].to_dict(), unknown_record)

    def test_uncertain_goal_uses_analysis_report_and_writer_recovery_keeps_measurement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, paper = self._paper_session(
                root,
                "# Classifier\nAccuracy measures correct predictions.",
                request_text="Evaluate this classifier.",
                requested_outputs=("experiments",),
                services=ResearchApplicationServices(config={"research_materials_only": True,
                    "interaction": "checkpoints", "execution": {
                    "command": [sys.executable, "-c", "print('accuracy: 0.7')"],
                    "cwd": str(root), "timeout_sec": 5,
                }}, budget_limits={"process_invocations": 1, "process_wall_seconds": 10}),
            )
            initial = app.advance(max_actions=20)
            first_gate = initial.work_plan["interaction"]["decision"]
            if isinstance(first_gate, dict) and first_gate.get("stage") == "execution_protocol":
                app.continue_session(
                    decision_id=first_gate["id"], decision_response="accept",
                )
                initial = app.advance(max_actions=20)
            self.assertEqual(initial.status, "completed", initial.status_reason)
            measurement = app.latest_experiment_ref()
            app.request_report()
            delivery_pause = app.advance()
            delivery_gate = delivery_pause.work_plan["interaction"]["decision"]
            self.assertEqual((delivery_pause.status, delivery_gate["stage"]), ("paused", "delivery"))
            app.continue_session(
                decision_id=delivery_gate["id"], decision_response="accept",
            )
            client = LLMClient(LLMSettings(api_key="test", api_mode="chat"))
            app.services = replace(app.services, llm_client=client)
            with patch("simple_ar.report.writing.run_report_agent", return_value=None) as writer:
                app.advance()
                self.assertEqual(writer.call_args.kwargs["template"].name, "analysis_report")
                self.assertEqual(writer.call_args.kwargs["memory"].template, "analysis_report")
                self.assertEqual(writer.call_args.kwargs["context"].results["delivery"]["goal_assessment"]["status"], "inconclusive")
            app = load_session(root / "session", services=ResearchApplicationServices(llm_client=client))
            app.continue_session()
            from simple_ar.report.schema import AgentReportResult, ReportSectionDraft
            def deliver(**kwargs):
                paper_id = kwargs["context"].papers[0]["id"]
                return AgentReportResult(report_body="", memory=kwargs["memory"], used_agent=True,
                    sections=[ReportSectionDraft(section_id="limitations", heading="Interpretation And Limits",
                        draft_markdown=f"Accuracy measures correct predictions [@{paper_id}]. The local research goal remains unassessed.",
                        used_sources=[paper_id])])
            with patch("simple_ar.report.writing.run_report_agent", side_effect=deliver) as writer:
                completed = app.advance(max_actions=3)
                self.assertEqual(writer.call_args.kwargs["template"].name, "analysis_report")
            self.assertEqual(completed.status, "completed", completed.status_reason)
            self.assertIn("report_audit", completed.state_refs)
            self.assertEqual(app.latest_experiment_ref(), measurement)
            self.assertEqual(sum(a["capability"] == "experiment" for a in app.view().attempts), 1)

    def test_input_request_stops_before_report_and_survives_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, paper = self._paper_session(
                root,
                "# Replay\nMemory retains previous examples.",
                request_text="Summarize replay",
                requested_outputs=("report",),
                services=ResearchApplicationServices(config={"research_materials_only": True}),
            )
            self.advance_to(app, "report_write")
            before = app.view()
            # Exercise the application boundary with a persisted decision,
            # without a model call or any experimental subprocess.
            decision = app.controller.store.write_json(
                "outputs/research_decision.json",
                {"action": "request_input", "decision_reason": "Clarify the requested comparison."},
                kind="research_decision", schema="research_decision.v1", producer="test",
            )
            app.controller.manifest.state_refs["decision"] = decision
            app.controller.save()
            app = load_session(root / "session")
            with patch.object(app, "_run_action") as run_action:
                paused = app.advance(max_actions=20)
                run_action.assert_not_called()
            self.assertEqual(paused.status, "paused")
            self.assertIsNone(paused.next_action)
            self.assertEqual(paused.status_reason, "Clarify the requested comparison.")
            self.assertEqual(paused.work_plan["research_decision"], {
                "action": "request_input", "decision_reason": "Clarify the requested comparison.",
            })
            self.assertEqual(paused.attempts, before.attempts)
            self.assertNotIn("writer", paused.state_refs)
            app = load_session(root / "session")
            with self.assertRaisesRegex(ResearchApplicationError, "pending required_input"):
                app.request_report()
            with self.assertRaisesRegex(ResearchApplicationError, "pending required_input"):
                app.request_reanalysis()
            self.assertEqual(app.advance().attempts, before.attempts)
            with self.assertRaisesRegex(ResearchApplicationError, "no enabled next action"):
                app.continue_session()
            app.continue_session(revised_brief=replace(
                app.brief, accepted_assumptions=("Compare only the supplied sources.",),
            ))
            self.assertNotIn("decision", app.view().state_refs)
            self.assertIsNotNone(app.view().next_action)
            self.assertEqual(app.view().state_refs["documents"], before.state_refs["documents"])
            self.assertNotIn("read", before.state_refs)
            self.assertTrue(app.controller.store.exists(decision))

            decision_id = "fedcba9876543210"
            proposal = {
                "action": "request_input",
                "interaction": {
                    "id": decision_id, "stage": "required_input", "status": "pending",
                    "reason": "The comparison condition is missing.",
                    "identity": {"fixture": "pending-input"},
                },
            }
            ref = app.controller.store.write_json(
                f"outputs/research-decision-{decision_id}.json", proposal,
                kind="research_decision", schema="research_decision.v1", producer="test",
            )
            app.controller.manifest.state_refs["decision"] = ref
            app.controller.save()
            app.controller.pause("The comparison condition is missing.")
            attempts = len(app.controller.list_attempts())
            rejected = app.continue_session(
                decision_id=decision_id, decision_response="reject",
            )
            saved = app.controller.store.read_json(rejected.state_refs["decision"])
            self.assertEqual(saved["action"], "stop")
            self.assertEqual(saved["interaction"]["status"], "rejected")
            self.assertIsNone(rejected.next_action)
            self.assertEqual(len(rejected.attempts), attempts)

    def test_interaction_reply_replay_finishes_revision_without_overwriting_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "session"
            app, paper = self._paper_session(
                root,
                "# Replay\nA local source for a narrow report.",
                request_text="Summarize the supplied source.",
                requested_outputs=("report",),
                services=ResearchApplicationServices(
                config={"research_materials_only": True},
            ),
            )
            self.advance_to(app, "report_write")
            decision_id = "0123456789abcdef"
            proposal_payload = {
                "action": "request_input", "decision_reason": "Clarify the report scope.",
                "interaction": {
                    "id": decision_id, "stage": "required_input", "status": "pending",
                    "question": "Which audience should the report address?",
                    "reason": "The audience changes the requested scope.",
                    "options": [{"id": "revise", "label": "Clarify"},
                                {"id": "reject", "label": "Stop"}],
                    "identity": {"research_identity": "fixture"},
                },
            }
            proposal_ref = app.controller.store.write_json(
                f"outputs/research-decision-{decision_id}.json", proposal_payload,
                kind="research_decision", schema="research_decision.v1", producer="test",
            )
            app.controller.manifest.state_refs["decision"] = proposal_ref
            app.controller.save()
            app.controller.pause("Clarify the report scope.")
            attempts_before = app.view().attempts
            reply = {
                "decision_id": decision_id, "decision_response": "revise",
                "decision_guidance": "Write for an engineering team.",
                "revised_brief": replace(
                    app.brief, accepted_assumptions=("Address the engineering team.",),
                ),
            }
            with patch.object(app.controller, "continue_with_revision", side_effect=RuntimeError("interrupted after reply")):
                with self.assertRaisesRegex(RuntimeError, "interrupted after reply"):
                    app.continue_session(**reply)

            response_path = f"outputs/research-decision-{decision_id}-response.json"
            response_payload = app.controller.store.read_json(response_path)
            self.assertEqual(response_payload["prior_decision_ref"], proposal_ref.to_dict())
            self.assertEqual(
                response_payload["interaction"]["response"]["revision"]["brief"]["accepted_assumptions"],
                ["Address the engineering team."],
            )
            self.assertEqual(app.controller.store.read_json(proposal_ref)["interaction"]["status"], "pending")
            self.assertNotEqual(app.controller.manifest.state_refs["decision"].path, proposal_ref.path)

            resumed = load_session(session)
            changed_reply = dict(reply)
            changed_reply["revised_brief"] = replace(
                reply["revised_brief"], accepted_assumptions=("A different audience.",),
            )
            with self.assertRaisesRegex(ResearchApplicationError, "different revision inputs"):
                resumed.continue_session(**changed_reply)
            recovery_reply = {key: value for key, value in reply.items() if key != "revised_brief"}
            completed_revision = resumed.continue_session(**recovery_reply)
            self.assertIn("Write for an engineering team.", resumed.brief.request_text)
            self.assertEqual(resumed.brief.accepted_assumptions, ("Address the engineering team.",))
            self.assertEqual(completed_revision.attempts, attempts_before)
            revision = completed_revision.revision
            replayed = load_session(session).continue_session(**reply)
            self.assertEqual(replayed.revision, revision)
            self.assertEqual(replayed.attempts, attempts_before)

    def test_assisted_confirms_protocol_once_then_runs_pair_after_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = root / "calls.txt"
            def command(role, value):
                code = (
                    "from pathlib import Path; "
                    f"Path({str(calls)!r}).open('a').write({(role + chr(10))!r}); "
                    f"print('accuracy: {value}')"
                )
                return [sys.executable, "-c", code]

            app, paper = self._paper_session(
                root,
                "# Paired test\nMeasure accuracy on the supplied fixed examples.",
                request_text="Compare the supplied classifier with its baseline.",
                objective="Compare the supplied classifier with its baseline.",
                requested_outputs=("experiments",),
                services=ResearchApplicationServices(
                config={"interaction": "assisted", "research_materials_only": True, "execution": {
                    "command": command("candidate", 0.5),
                    "baseline": {"command": command("baseline", 0.6)},
                    "cwd": str(root), "timeout_sec": 5,
                    "result_schema": {"primary_metric": "accuracy", "required_metrics": ["accuracy"]},
                }},
                budget_limits={"process_invocations": 2, "process_wall_seconds": 10},
            ),
            )
            pending = app.advance(max_actions=30)
            self.assertEqual(pending.status, "paused", pending.status_reason)
            gate = pending.work_plan["interaction"]["decision"]
            self.assertIsInstance(gate, dict, pending.status_reason + repr(pending.work_plan.get("steps")))
            self.assertEqual(gate["stage"], "execution_protocol")
            self.assertFalse(calls.exists())

            app = load_session(root / "session")
            refs = app.controller.manifest.state_refs
            stored_gate = app.controller.store.read_json(refs["decision"])["interaction"]
            self.assertTrue(app._interaction_identity_is_current(stored_gate))
            saved_services = app.services
            changed_execution = dict(saved_services.config["execution"])
            changed_execution["timeout_sec"] = 6
            app.services = replace(saved_services, config={
                **saved_services.config, "execution": changed_execution,
            })
            with self.assertRaisesRegex(ResearchApplicationError, "no longer matches"):
                app.continue_session(decision_id=gate["id"], decision_response="accept")
            app.services = saved_services
            app.continue_session(decision_id=gate["id"], decision_response="accept")
            completed = app.advance(max_actions=30)
            self.assertEqual(completed.status, "completed", completed.status_reason)
            self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), ["baseline", "candidate"])
            measured_attempts = completed.attempts

            restored = load_session(root / "session")
            replay = restored.continue_session(decision_id=gate["id"], decision_response="accept")
            self.assertEqual(replay.attempts, measured_attempts)
            self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), ["baseline", "candidate"])

    def advance_to(self, app, action):
        for _ in range(app.services.max_attempts):
            view = app.view()
            if view.next_action == action or view.status in {"paused", "blocked", "completed"}:
                break
            app.advance()
        self.assertEqual(app.view().next_action, action, app.view().status_reason)

    def test_missing_selected_design_stops_code_preparation_before_processes(self):
        from simple_ar.research.design import ResearchDesignResult
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = root / "paper.md"
            paper.write_text("# Replay\nA small memory retains prior examples.", encoding="utf-8")
            app = create_session(ResearchBrief(request_text="Study replay", requested_outputs=("experiments",),
                asset_requests=({"locator": str(paper), "kind": "file", "role": "paper"},)), root=root / "session",
                services=ResearchApplicationServices(max_attempts=20, config={"execution": {
                    "command": [sys.executable, "-c", "print('accuracy: 1')"], "cwd": str(root), "timeout_sec": 5,
                    "code_task": {"code_root": str(root), "approval_note": "Only isolated edits."},
                }}, budget_limits={"process_invocations": 2, "process_wall_seconds": 10}))
            with patch("simple_ar.research.design.build_research_design", return_value=ResearchDesignResult(
                status="needs_review", contract=None, diagnostics=("No candidate selected.",),
            )):
                view = app.advance(max_actions=20)
            self.assertEqual(view.status, "paused")
            self.assertIn("selected research design contract", view.status_reason)
            self.assertNotIn("preparation", view.state_refs)
            self.assertFalse(any("process_invocations" in e.reserved for e in app.budget_ledger.entries))

    def test_terminal_attempt_reservation_becomes_unknown_without_refunding_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_session(ResearchBrief(request_text="Study agents"), root=tmp,
                                 services=ResearchApplicationServices(budget_limits={"total_tokens": 100}))
            app.advance()
            attempt_id = app.controller.list_attempts()[0].attempt_id
            app.budget_ledger.reserve("interrupted-call", {"total_tokens": 60}, attempt_id=attempt_id)
            app.controller.pause("Inspect recovery without starting another action.")
            resumed = load_session(tmp)
            resumed.advance()
            self.assertEqual(resumed.budget_ledger.entries[-1].status, "unknown")
            self.assertEqual(resumed.budget_ledger.entries[-1].actual, {})
            self.assertEqual(resumed.budget_ledger.remaining("total_tokens"), 40)
            saved = (Path(tmp) / "budget_ledger.json").read_bytes()
            resumed.advance()
            self.assertEqual((Path(tmp) / "budget_ledger.json").read_bytes(), saved)

    def test_design_receives_omitted_policy_as_omitted(self):
        from simple_ar.research import design as design_module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen = {}
            real = design_module.build_research_design

            def capture(request, **kwargs):
                seen["boundary"] = dict(request.execution_boundary)
                return real(request, **kwargs)

            app, paper = self._paper_session(
                root,
                "# Fixture\nA small measured comparison.\n",
                request_text="Measure the supplied fixture.",
                requested_outputs=("experiments",),
                services=ResearchApplicationServices(
                    config={
                        "research_materials_only": True,
                        "execution": {
                            "command": [sys.executable, "-c", "print('accuracy: 1')"],
                            "cwd": str(root), "timeout_sec": 5,
                        },
                    },
                ),
            )
            with patch.object(design_module, "build_research_design", side_effect=capture):
                app.advance(max_actions=20)
            self.assertIn("boundary", seen)
            self.assertNotIn("baseline_policy", seen["boundary"])

    def test_analysis_context_exposes_only_accepted_seed_extension_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = [sys.executable, "-c", "print('accuracy: 1')"]
            app = create_session(
                ResearchBrief(request_text="Compare the supplied fixture.", requested_outputs=("experiments",)),
                root=root / "session",
                services=ResearchApplicationServices(config={"execution": {
                    "command": command,
                    "baseline": {"command": command, "label": "baseline"},
                    "cwd": str(root),
                    "timeout_sec": 5,
                    "seeds": [0, 1],
                    "seed_flag": "--seed",
                    "baseline_policy": "run",
                }}),
            )

            protocol = app._analysis_context()["metadata"]["execution_protocol"]

            self.assertEqual(protocol["declared_seeds"], [0, 1])
            self.assertEqual(protocol["seed_flag"], "--seed")
            self.assertTrue(protocol["can_extend_seed_condition"])
            self.assertEqual(protocol["condition_mode"], "paired_seed")

            fixed = dict(app.services.config["execution"])
            fixed.pop("seeds")
            fixed.pop("seed_flag")
            fixed_protocol = app._analysis_execution_protocol(fixed)
            self.assertFalse(fixed_protocol["can_extend_seed_condition"])
            self.assertEqual(fixed_protocol["condition_mode"], "fixed_command")
            fixed.update(command=[*command, "--seed", "0"], seed_flag="--seed")
            fixed_protocol = app._analysis_execution_protocol(fixed)
            self.assertEqual(fixed_protocol["declared_seeds"], [0])
            self.assertFalse(fixed_protocol["can_extend_seed_condition"])

    def test_revision_preparation_preserves_original_step_reference(self):

        with tempfile.TemporaryDirectory() as tmp:
            app = create_session(ResearchBrief(request_text="Compare candidates."), root=Path(tmp))
            original_workspace = Path(tmp) / "original"
            revised_workspace = Path(tmp) / "revised"
            original_workspace.mkdir()
            revised_workspace.mkdir()
            original = app.controller.store.write_json("original.json", {"workspace": str(original_workspace), "execution": {
                "cwd": str(original_workspace), "code_task": {"code_root": str(original_workspace)}}},
                kind="prepared_execution", schema="prepared_execution.v1", producer="test")
            revised = app.controller.store.write_json("revised.json", {"workspace": str(revised_workspace), "execution": {
                "cwd": str(revised_workspace), "code_task": {"code_root": str(revised_workspace)}}},
                kind="prepared_execution", schema="prepared_execution.v1", producer="test")
            app.controller.manifest.state_refs["preparation"] = original
            with patch.object(app.controller, "attempt_output_ref", return_value=revised):
                app._record_attempt_outputs("prepare_execution", "preparation_r1", "prepare-2", None)
            self.assertEqual(app.controller.manifest.state_refs["preparation"], original)
            plan = TaskPlanResult(
                task_kind="research", goal="Compare candidates.", mode="test",
                steps=(
                    TaskPlanStep("initial", "prepare_execution", "prepare_execution", "preparation", "Prepare", "Inspect."),
                    TaskPlanStep("revision-1", "prepare_candidate:1", "prepare_execution", "preparation_r1", "Revise", "Inspect."),
                ),
            )
            app.controller.manifest.state_refs["task_plan"] = app.controller.store.write_json(
                "planning/task_plan.json", plan.to_handoff_dict(), kind="task_plan",
                schema="research_task_plan.v1", producer="test",
            )
            with patch.object(app, "_attempt_for_ref", return_value=SimpleNamespace(status="completed")):
                self.assertEqual(app._effective_config()["execution"]["cwd"], str(revised_workspace))
                supplement, reason = app._code_task_supplement_candidate_config({
                    "cwd": str(original_workspace), "code_task": {"code_root": str(original_workspace)},
                })
                self.assertEqual(reason, "")
                self.assertEqual(supplement["cwd"], str(revised_workspace))
                self.assertEqual(supplement["code_task"]["code_root"], str(revised_workspace))
            with patch.object(app, "_attempt_for_ref", return_value=SimpleNamespace(status="completed")):
                self.assertTrue(app._state_succeeded("preparation_r1"))
            with patch.object(app, "_attempt_for_ref", return_value=SimpleNamespace(status="failed")):
                self.assertFalse(app._state_succeeded("preparation_r1"))

            index_path = Path(tmp) / "code_task_run" / "code_task" / "meta" / "codebase_index.json"
            index_path.parent.mkdir(parents=True)
            index_path.write_text(json.dumps({"files": [{"path": "learner.py"}]}), encoding="utf-8")
            implementation = app.controller.store.write_json("implementation.json", {
                "code_task_run_dir": str(index_path.parents[2]),
            }, kind="implementation_result", schema="research_implementation.v1", producer="test")
            design = app.controller.store.write_json("design.json", {"selected_idea": {"idea_id": "old"}},
                kind="research_design", schema="research_design.v1", producer="test")
            decision = app.controller.store.write_json("decision.json", {"decision_reason": "Try another direction."},
                kind="research_decision", schema="research_decision.v1", producer="test")
            app.controller.manifest.state_refs.update({"design": design, "decision": decision,
                "implementation_r1": implementation})
            for name in ("synthesis", "assessment"):
                app.controller.manifest.state_refs[name] = app.controller.store.write_json(
                    f"{name}.json", {}, kind=name, schema=f"{name}.v1", producer="test")
            revised_plan = replace(plan, steps=(*plan.steps,
                TaskPlanStep("revision-implementation", "revise_candidate:1", "implement", "implementation_r1", "Implement", "Validate."),
            ))
            app.controller.manifest.state_refs["task_plan"] = app.controller.store.write_json(
                "planning/task_plan-revised.json", revised_plan.to_handoff_dict(), kind="task_plan",
                schema="research_task_plan.v1", producer="test",
            )
            app.services = replace(app.services, llm_client=object())
            with (patch.object(app, "_attempt_for_ref", return_value=SimpleNamespace(status="completed")),
                  patch.object(app, "_step_completed", return_value=True),
                  patch.object(app, "_analysis_candidate_options", return_value=[]),
                  patch.object(app, "_research_history", return_value=[]),
                  patch.object(app, "_load_synthesis", return_value=SimpleNamespace(ideas=())),
                  patch.object(app, "_execute", return_value=True) as execute):
                self.assertTrue(app._run_action("research_design_revision:2"))
            request = execute.call_args.args[2]
            self.assertEqual(request.source_workspace, revised_workspace)
            self.assertEqual(request.source_index["files"][0]["path"], "learner.py")
            self.assertIn(implementation, execute.call_args.args[3])
            self.assertIn(revised, execute.call_args.args[3])
            baseline_measured = app.controller.store.write_json("revision-baseline-result.json", {"status": "passed"},
                kind="experiment_result", schema="experiment_result.v1", producer="test")
            measured = app.controller.store.write_json("revision-result.json", {
                "status": "passed", "implementation_ref": implementation.to_dict(),
            },
                kind="experiment_result", schema="experiment_result.v1", producer="test")
            app.controller.manifest.state_refs["experiment_revision_1"] = measured
            with patch.object(app, "_step_completed", return_value=True):
                collection_ref = app._write_supplement_collection(2, {"seed": 5}, [(baseline_measured, measured)])
            collection = app.controller.store.read_json(collection_ref)
            self.assertEqual(collection["candidate_revision"], 1)
            self.assertEqual(collection["implementation_ref"], implementation.to_dict())
            unlinked = app.controller.store.write_json("revision-unlinked-result.json", {"status": "passed"},
                kind="experiment_result", schema="experiment_result.v1", producer="test")
            unlinked_collection_ref = app._write_supplement_collection(
                3, {"seed": 6}, [(baseline_measured, unlinked)],
            )
            self.assertIsNone(app.controller.store.read_json(unlinked_collection_ref)["implementation_ref"])

    def test_application_repairs_failed_candidate_without_repeating_baseline(self):
        self._exercise_code_measurement_boundary(repair_failure=True)

    def test_application_stops_after_authorized_repair_limit(self):
        self._exercise_code_measurement_boundary(repair_failure=True, repair_succeeds=False)

    def test_application_accepts_protocol_and_recovers_preparation_with_declared_large_data(self):
        self._exercise_code_measurement_boundary(
            repair_failure=False, prepare_source=True, protocol_gate=True, declared_data=True,
        )

    def test_application_implements_once_after_all_paired_baselines(self):
        self._exercise_code_measurement_boundary(repair_failure=False, prepare_source=True, paired=True)

    def test_application_does_not_edit_after_a_failed_paired_baseline(self):
        self._exercise_code_measurement_boundary(repair_failure=False, prepare_source=True, paired=True, baseline_failure=True)

    def test_application_repairs_paired_candidate_without_mixing_revisions(self):
        self._exercise_code_measurement_boundary(repair_failure=True, prepare_source=True, paired=True)

    def test_application_does_not_pool_a_successful_old_seed_after_later_failure(self):
        self._exercise_code_measurement_boundary(repair_failure=True, prepare_source=True, paired=True, late_failure=True)

    def test_paired_repair_limit_keeps_failure_and_missing_seed(self):
        self._exercise_code_measurement_boundary(repair_failure=True, repair_succeeds=False, prepare_source=True, paired=True)

    def _exercise_code_measurement_boundary(self, *, repair_failure, repair_succeeds=True, prepare_source=False, paired=False, baseline_failure=False, late_failure=False, protocol_gate=False, declared_data=False):
        """Own preparation recovery, paired seeds and initial measurement repairs.

        Ordinary successful modification and scientific revision live in
        _exercise_code_task_lifecycle; source-only cases stop after preparation
        and the first real baseline instead of repeating that lifecycle.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, task, code_run = root / "project", root / "task.md", root / "code-task"
            _write_toy_project(project)
            if declared_data:
                (project / "input.bin").write_bytes(b"x" * 2_000_001)
            (project / "evaluate.py").write_text(
                "from spam_model import predict\nimport sys\n"
                "with open('evaluation_count.txt', 'a') as log: log.write('run\\n')\n"
                "print('accuracy:', sum(predict(x) == 'spam' for x in ('win', 'prize')) / 2)\n"
                + ("assert len(open('input.bin', 'rb').read()) == 2000001\n" if declared_data else "")
                + ("raise SystemExit(3 if sys.argv[-1] == '1' else 0)\n" if baseline_failure else ""),
                encoding="utf-8",
            )
            task.write_text("Improve spam prediction for prize messages.", encoding="utf-8")
            if not prepare_source:
                initialize_code_task(run_dir=code_run, code_root=project, task_file=task,
                                     benchmark_command="python evaluate.py")
            workspace = project if prepare_source else code_task_paths(code_run).workspace_dir
            paper = root / "paper.md"
            paper.write_text("# Keyword classifier improvement\n\nCompare a keyword classifier improvement using spam features.\n", encoding="utf-8")
            command = [sys.executable, "evaluate.py"]
            app = create_session(ResearchBrief(
                request_text="Compare a keyword classifier improvement.", requested_outputs=("experiments",),
                asset_requests=({"locator": str(paper), "role": "paper"},
                    *(({"locator": str(project / "input.bin"), "role": "dataset"},) if declared_data else ())),
            ), root=root / "session", services=ResearchApplicationServices(max_results=1, max_attempts=24 if paired else 16, config={
                **({"interaction": "assisted"} if protocol_gate else {}),
                "execution": {"command": command, "baseline": {"command": command},
                              **({"pairs": [{"seed": seed, "baseline_command": command + [str(seed)],
                                               "candidate_command": command + [str(seed)]} for seed in (0, 1)]} if paired else {}),
                              "cwd": str(workspace), "timeout_sec": 5,
                              "code_task": {("code_root" if prepare_source else "run_dir"): str(project if prepare_source else code_run), "approval_note": "Authorize isolated source edits only.",
                                            "max_repairs": int(repair_failure)},
                              "result_schema": {"primary_metric": "accuracy", "direction": "higher"},
                              "protocol": {"contract_id": "keyword-pair", "hypothesis": "Improve keyword coverage.",
                                           "dataset_refs": [{"asset_id": "two-messages", "revision": "1"}],
                                           "split_spec": {"held_out": [0, 1]},
                                           "metric_specs": [{"name": "accuracy", "unit": "fraction"}],
                                           "comparison_conditions": {"seed": 0},
                                           "protected_assets": [{"asset_id": "evaluator", "path": "evaluate.py"}]}},
            }, budget_limits={"llm_requests": 12, "total_tokens": 50000,
                              "process_invocations": (5 + int(late_failure) if repair_failure else 4) if paired else 3 if repair_failure else 2,
                               "process_wall_seconds": 30 if paired else 15}))
            def advance_until(*actions):
                for _ in range(6):
                    view = app.view()
                    if view.next_action in actions:
                        return view
                    self.assertEqual(view.status, "running", view.status_reason)
                    app.advance(max_actions=1)
                self.fail(f"Did not reach {actions!r}; next={app.view().next_action!r}")

            if prepare_source:
                app.advance(max_actions=20 if protocol_gate else 8)
                if protocol_gate:
                    pending = app.view()
                    self.assertEqual(pending.status, "paused", pending.status_reason)
                    gate = pending.work_plan["interaction"]["decision"]
                    self.assertEqual(gate["stage"], "execution_protocol")
                    self.assertFalse((project / "evaluation_count.txt").exists())
                    app.continue_session(decision_id=gate["id"], decision_response="accept")
                    app = load_session(root / "session")
                advance_until("prepare_execution")
                self.assertEqual(app.view().next_action, "prepare_execution")
                with patch.object(app, "_persist_application_views", side_effect=RuntimeError("interrupted preparation")):
                    with self.assertRaisesRegex(RuntimeError, "interrupted preparation"):
                        app.advance()
                app = load_session(root / "session")
                with patch("simple_ar.research.preparation.initialize_code_task", side_effect=AssertionError("Do not prepare twice")):
                    app.advance()
                prepared = app.controller.store.read_json(app.view().state_refs["preparation"])
                code_run = Path(prepared["execution"]["code_task"]["run_dir"])
                workspace = Path(prepared["execution"]["cwd"])
                self.assertNotEqual(workspace, project)
                self.assertEqual(prepared["source_project"], str(project))
                if declared_data:
                    self.assertEqual((workspace / "input.bin").read_bytes(), (project / "input.bin").read_bytes())
                    self.assertEqual(prepared["workspace_info"]["patterns"]["data_inputs"], ["input.bin"])
            else:
                app.advance(max_actions=9)
            if paired:
                app.advance()
            advance_until("implement")
            self.assertEqual(app.view().next_action, "implement", repr(app.view()))
            if prepare_source and not paired:
                # The real baseline reads declared data and proves the accepted
                # protocol survived preparation recovery. Editing and candidate
                # delivery are owned by the scientific lifecycle below.
                baseline = app.controller.store.read_json(app.view().state_refs["baseline"])
                self.assertEqual(baseline["execution_status"], "passed")
                self.assertEqual(baseline["metrics"]["accuracy"], 0.5)
                self.assertEqual((workspace / "evaluation_count.txt").read_text().splitlines(), ["run"])
                self.assertFalse((project / "evaluation_count.txt").exists())
                self.assertEqual((workspace / "spam_model.py").read_bytes(), (project / "spam_model.py").read_bytes())
                before = app.view()
                ledger = app.budget_ledger.to_dict()
                app = load_session(root / "session")
                self.assertEqual(app.view().state_refs, before.state_refs)
                self.assertEqual(app.view().attempts, before.attempts)
                self.assertEqual(app.budget_ledger.to_dict(), ledger)
                self.assertEqual(app.budget_ledger.remaining("process_invocations"), 1)
                return
            if baseline_failure:
                stopped = app.advance()
                self.assertEqual(stopped.status, "paused")
                self.assertIn("paired baseline failed", stopped.status_reason)
                self.assertNotIn("implementation", stopped.state_refs)
                self.assertEqual((workspace / "evaluation_count.txt").read_text().splitlines(), ["run"] * 2)
                self.assertNotIn("lowered =", (workspace / "spam_model.py").read_text())
                return
            client = LLMClient(LLMSettings(api_key="test-key", api_mode="chat", max_output_tokens=2048))
            app.services = replace(app.services, llm_client=client)
            responses_by_label = keyword_code_responses()
            payloads = [responses_by_label[label] for label in
                        ("code-task-work-plan", "code-task-plan", "code-task-propose-edits")]
            if repair_failure:
                broken = "(text.lower() if __import__('sys').argv[-1] == '0' else text.lowerr())" if late_failure else "text.lowerr()"
                payloads[2]["edits"][0]["new"] = payloads[2]["edits"][0]["new"].replace("text.lower()", broken)
            payloads += [{"findings": []}] * 8
            responses = [{"choices": [{"message": {"content": json.dumps(payload)}}],
                          "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50}}
                         for payload in payloads]
            with patch("simple_ar.integrations.llm._call_openai_sdk", side_effect=responses) as transport, patch.object(
                LLMClient, "from_env", side_effect=AssertionError("Use the session client"),
            ):
                with patch.object(app, "_persist_application_views", side_effect=RuntimeError("interrupted implementation")):
                    with self.assertRaisesRegex(RuntimeError, "interrupted implementation"):
                        app.advance()
            sent = json.dumps(transport.call_args_list[0].args[1], ensure_ascii=False)
            self.assertIn("Execution boundary and observed baseline", sent)
            self.assertIn("keyword-pair", sent)
            design = app.controller.store.read_json(app.controller.manifest.state_refs["design"])
            self.assertIn(design["contract"]["hypothesis"], sent)
            frozen = read_json(code_task_paths(code_run).task_dir / "research_handoff.json")
            implementation_task = (code_task_paths(code_run).task_dir / "task.md").read_text(encoding="utf-8")
            self.assertIn("Implementation-only CodeTask", implementation_task)
            self.assertIn("owns literature review, experiment execution, result analysis", implementation_task)
            self.assertNotIn("complete academic Markdown paper", implementation_task)
            if paired:
                self.assertEqual(len(frozen["consumed"]["paired_baselines"]), 2)
                self.assertEqual([row["metrics"]["accuracy"] for row in frozen["consumed"]["paired_baselines"]], [0.5, 0.5])
            self.assertEqual(frozen["consumed"]["baseline"]["metrics"]["accuracy"], 0.5)
            if prepare_source:
                self.assertIn("Compare a keyword classifier improvement.", frozen["original_task"])
                self.assertNotIn("lowered =", (project / "spam_model.py").read_text())
                self.assertFalse((project / "evaluation_count.txt").exists())
            else:
                self.assertEqual(frozen["original_task"], "Improve spam prediction for prize messages.")
            app = load_session(root / "session")
            if repair_failure:
                self.assertEqual(app.advance(max_actions=2 if late_failure else 1).next_action, "matrix_repair_1" if paired else "repair:1")
                failure_key = f"matrix_candidate_{int(late_failure)}" if paired else "experiment"
                initial_failure = app.controller.store.read_json(app.view().state_refs[failure_key])
                self.assertEqual(initial_failure["execution_status"], "failed")
                app.services = replace(app.services, llm_client=client)
                repair_payload = {"summary": "Fix typo", "edits": [{"path": "spam_model.py",
                    "old": "text.lowerr()", "new": "text.lower()" if repair_succeeds else "text.lower_again()", "reason": "AttributeError"}],
                    "validation": ["Run evaluator separately"], "risks": []}
                repaired_responses = [{"choices": [{"message": {"content": json.dumps(payload)}}],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50}}
                    for payload in [repair_payload] + [{"findings": []}] * 8]
                with patch("simple_ar.integrations.llm._call_openai_sdk", side_effect=repaired_responses):
                    with patch.object(app, "_persist_application_views", side_effect=RuntimeError("interrupted repair")):
                        with self.assertRaisesRegex(RuntimeError, "interrupted repair"):
                            app.advance()
                app = load_session(root / "session")
            view = app.advance(max_actions=3 if paired else 2)
            if paired:
                self.assertEqual(view.status, "completed", view.status_reason)
                if not repair_succeeds:
                    collection = app.controller.store.read_json(view.state_refs["matrix_results"])
                    self.assertEqual(collection["candidate_revision"], 1)
                    self.assertEqual(collection["pairs"][0]["candidate"], view.state_refs["matrix_candidate_r1_0"].to_dict())
                    self.assertIsNone(collection["pairs"][1]["candidate"])
                    self.assertNotIn("matrix_repair_2", view.state_refs)
                    analysis = app.controller.store.read_json(view.state_refs["analysis"])
                    self.assertEqual(analysis["execution_status"], "incomplete")
                    self.assertEqual((workspace / "evaluation_count.txt").read_text().splitlines(), ["run"] * 4)
                    self.assertEqual(app.budget_ledger.remaining("process_invocations"), 1)
                    return
                revision = 1 if repair_failure else 0
                for i in range(2):
                    self.assertEqual(app.controller.store.read_json(view.state_refs[f"matrix_baseline_{i}"])["metrics"]["accuracy"], 0.5)
                    key = f"matrix_candidate_r{revision}_{i}" if revision else f"matrix_candidate_{i}"
                    candidate_result = app.controller.store.read_json(view.state_refs[key])
                    self.assertEqual(candidate_result["metrics"]["accuracy"], 1.0)
                    expected_revision = "matrix_repair_1" if revision else "implementation"
                    self.assertEqual(candidate_result["implementation_ref"], view.state_refs[expected_revision].to_dict())
                collection = app.controller.store.read_json(view.state_refs["matrix_results"])
                self.assertEqual(collection["candidate_revision"], revision)
                implementation_key = "matrix_repair_1" if revision else "implementation"
                self.assertEqual(collection["implementation_ref"], view.state_refs[implementation_key].to_dict())
                self.assertEqual(len(collection["superseded_candidates"]), revision * (1 + int(late_failure)))
                if late_failure:
                    old_success = app.controller.store.read_json(view.state_refs["matrix_candidate_0"])
                    self.assertEqual(old_success["status"], "passed")
                    self.assertNotIn(view.state_refs["matrix_candidate_0"].to_dict(), [row["candidate"] for row in collection["pairs"]])
                self.assertEqual(len([a for a in view.attempts if a["capability"] == "implement"]), 1 + revision)
                for attempt in view.attempts:
                    if attempt["trigger"].startswith("application:matrix_candidate_"):
                        expected = "matrix_repair_1" if "_r1_" in attempt["trigger"] else "implementation"
                        self.assertIn(view.state_refs[expected].to_dict(), attempt["inputs"])
                self.assertEqual((workspace / "evaluation_count.txt").read_text().splitlines(), ["run"] * (4 + revision + int(late_failure)))
                self.assertEqual(app.budget_ledger.remaining("process_invocations"), 0)
                return
            self.assertEqual(view.status, "completed", view.status_reason)
            self.assertIn("implementation", view.state_refs)
            implementation_payload = app.controller.store.read_json(view.state_refs["implementation"])
            self.assertEqual(implementation_payload["status"], "validated")
            self.assertEqual(implementation_payload["method_validation"]["status"], "未检查")
            final_key = "experiment_repair_1" if repair_failure else "experiment"
            expected_implementation = view.state_refs["repair_1"] if repair_failure else view.state_refs["implementation"]
            final_result = app.controller.store.read_json(view.state_refs[final_key])
            self.assertEqual(final_result["implementation_ref"], expected_implementation.to_dict())
            self.assertEqual(app.latest_experiment_ref(), view.state_refs[final_key])
            delivery = next(row for row in view.work_plan["requested_outputs"] if row["name"] == "experiments")
            self.assertEqual(delivery["artifact"], view.state_refs[final_key].to_dict())
            analysis_payload = app.controller.store.read_json(view.state_refs["analysis"])
            self.assertEqual(analysis_payload["execution_ref"], delivery["artifact"])
            snapshot = app.controller.store.read_text(app.export_session())
            self.assertIn("## Latest candidate measurement", snapshot)
            self.assertIn(view.state_refs[final_key].path, snapshot.split("## Latest candidate measurement")[1])
            comparison = app.controller.store.read_json(view.state_refs["comparison"])
            self.assertEqual(comparison["verdict"], "improved" if repair_succeeds else "inconclusive")
            report_context, report_memory = app.report_inputs()
            self.assertEqual(report_context.results["implementation"]["artifact"], expected_implementation.path)
            unrelated = app.controller.store.write_json(
                "outputs/unrelated_implementation.json", {"status": "validated", "artifact_refs": {}},
                kind="implementation_result",
            )
            app.controller.manifest.state_refs["unrelated_implementation"] = unrelated
            with patch.object(app, "_accepted_plan_steps", return_value=[
                {"capability": "implement", "state_name": "unrelated_implementation"},
            ]):
                measured_report, _ = app.report_inputs()
            self.assertEqual(measured_report.results["implementation"]["artifact"], expected_implementation.path)
            self.assertEqual(report_context.results["execution_status"], "passed" if repair_succeeds else "failed")
            for metric in report_context.metric_sources:
                expected_ref = view.state_refs["baseline"] if metric.label == "baseline" else (
                    view.state_refs["comparison"] if metric.label == "comparison_delta" else view.state_refs[final_key])
                self.assertEqual(metric.artifact, expected_ref.path)
            self.assertEqual(report_memory.metric_sources, report_context.metric_sources)
            self.assertEqual(report_context.results["comparisons"][0]["verdict"], comparison["verdict"])
            if repair_succeeds:
                self.assertEqual(comparison["deltas"]["accuracy"], 0.5)
            self.assertEqual((workspace / "evaluation_count.txt").read_text().splitlines(), ["run"] * (3 if repair_failure else 2))
            if repair_failure:
                self.assertEqual(app.controller.store.read_json(view.state_refs["experiment"]), initial_failure)
                self.assertIn("repair_1", view.state_refs)
                self.assertIn("experiment_repair_1", view.state_refs)
                self.assertNotIn("repair_2", view.state_refs)
                repair_artifact = app.controller.store.read_json(view.state_refs["repair_1"])
                self.assertIn("failure_evidence", repair_artifact["artifact_refs"])
                self.assertIn("repair_proposal", repair_artifact["artifact_refs"])
                expected_status = "passed" if repair_succeeds else "failed"
                self.assertEqual(app.controller.store.read_json(view.state_refs["experiment_repair_1"])["execution_status"], expected_status)
            self.assertEqual(app.budget_ledger.remaining("process_invocations"), 0)
            implementation_id = next(item["attempt_id"] for item in view.attempts if item["capability"] == "implement")
            implementation_attempt = next(item for item in view.attempts if item["attempt_id"] == implementation_id)
            self.assertIn(view.state_refs["baseline"].path, [ref["path"] for ref in implementation_attempt["inputs"]])
            model_entries = [entry for entry in app.budget_ledger.entries if "llm_requests" in entry.actual]
            self.assertGreater(len(model_entries), 2)
            implementation_ids = {item["attempt_id"] for item in view.attempts if item["capability"] == "implement"}
            self.assertTrue(all(entry.attempt_id in implementation_ids for entry in model_entries))
            self.assertIn("evaluate.py", read_json(code_run / "manifest.json")["edit_scope"]["protected_patterns"])
            # A later research revision must not silently reuse this accepted patch plan.
            from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
            from simple_ar.research.implementation import ImplementationRequest, run_implementation_capability
            changed_store = ArtifactStore(root / "revised-inputs")
            design["contract"]["proposed_change"] = "A different research intervention"
            revised_design = changed_store.write_json("design.json", design, kind="research_design")
            baseline_payload = app.controller.store.read_json(view.state_refs["baseline"])
            baseline_ref = changed_store.write_json("baseline.json", baseline_payload, kind="experiment_result")
            context = CapabilityContext(store=ArtifactStore(root / "revision-attempt"),
                                        attempt=AttemptManifest("implement-revised"),
                                        inputs=(revised_design, baseline_ref), input_store=changed_store)
            request = ImplementationRequest(code_run, workspace, "Allow source edits", client,
                                            frozen["consumed"]["protocol"])
            old_task = (code_task_paths(code_run).task_dir / "task.md").read_text(encoding="utf-8")
            with patch("simple_ar.integrations.llm._call_openai_sdk", side_effect=AssertionError("No stale-plan execution")):
                with self.assertRaisesRegex(ValueError, "Research inputs changed"):
                    run_implementation_capability(context=context, request=request)
            self.assertEqual((code_task_paths(code_run).task_dir / "task.md").read_text(encoding="utf-8"), old_task)
            # The session evidence remains readable without the external CodeTask run.
            copied_session = root / "portable-session"
            shutil.copytree(root / "session", copied_session)
            code_run.rename(root / "code-task-unavailable")
            portable = load_session(copied_session)
            implementation_ref = portable.view().state_refs["implementation"]
            implementation = portable.controller.store.read_json(implementation_ref)
            self.assertEqual(implementation["artifact_base"], "attempt")
            self.assertTrue({"patch", "validation", "patch_plan", "research_handoff"}.issubset(implementation["artifact_refs"]))
            attempt_store = ArtifactStore(copied_session / Path(implementation_ref.path).parent)
            from simple_ar.core.capabilities import ArtifactRef
            for name, row in implementation["artifact_refs"].items():
                contents = attempt_store.read_text(ArtifactRef.from_dict(row))
                self.assertTrue(contents.strip(), name)
            patch_ref = ArtifactRef.from_dict(implementation["artifact_refs"]["patch"])
            self.assertIn("prize", attempt_store.read_text(patch_ref))
            self.assertEqual(portable.advance().status, "completed")

    def test_code_task_lifecycle_preserves_measurement_refinement_and_repair(self):
        for scenario, options in (
            ("design_gap", {"refine": True}),
            ("failed_revision", {"revised_failure": True}),
        ):
            with self.subTest(scenario=scenario):
                self._exercise_code_task_lifecycle(**options)

    def test_verification_only_work_is_not_a_scientific_candidate_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_session(
                ResearchBrief(request_text="Evaluate an existing candidate."),
                root=Path(tmp) / "session",
                services=ResearchApplicationServices(),
            )
            proposal = {
                "reason": "Mechanism evidence is missing.",
                "revision_intent": "Add logging and repeat the same seed.",
                "revision_purpose": "verification_only",
                "revision_base": "candidate",
            }
            self.assertIn("cannot trigger another identical training run", app._validate_revision_recommendation(proposal))
            proposal["revision_purpose"] = "method_change"
            proposal["revision_intent"] = "Change the candidate rule based on the observed error class."
            self.assertEqual(app._validate_revision_recommendation(proposal), "")


    def _exercise_code_task_lifecycle(self, *, refine=False, revised_failure=False):
        """Own the measured scientific revision and structured design feedback."""
        class FakeClient:
            model = "fake-research-and-code-model"

            def __init__(self):
                self.edit_count = 0
                self.analysis_count = 0
                self.gap_returned = False
                self.refinement_calls = 0

            def ask(self, _system, _user, *, label=""):
                if label not in {"result-analysis", "experiment-analysis"}:
                    raise AssertionError(f"Unexpected text LLM label: {label}")
                self.analysis_count += 1
                revise = self.analysis_count == 1
                return json.dumps({"recommendation": {
                    "action": "revise_candidate" if revise else "stop",
                    "task_disposition": "continue" if revise else "stop",
                    "reason": "Test a distinct candidate once." if revise else "The bounded comparison is complete.",
                    "alternatives_considered": [] if revise else ["No additional supplied candidate is justified by this fixture."],
                    "continuation_conditions": [] if revise else ["A new evidence-backed candidate would require an explicit revised task."],
                    "revision_intent": "Add lottery keyword support." if revise else "Report the measured comparison.",
                    "revision_purpose": "method_change" if revise else "unspecified",
                    "revision_constraints": ["Preserve the evaluator and existing API."],
                }})

            def ask_json(
                self,
                _system: str,
                _user: str,
                *,
                label: str = "",
                **kwargs: object,
            ) -> dict[str, object]:
                del kwargs
                if label == "research-design-refinement":
                    assert "def predict" in _user, "Design refinement must receive the inspected source, not just the model's complaint."
                    self.refinement_calls += 1
                    if self.refinement_calls == 1:
                        raise LLMError("simulated transport interruption")
                    if self.refinement_calls == 2:
                        return {"status": "inspect_source", "context_request": {
                            "files": ["spam_model.py"], "symbols": ["predict"], "query": ""}}
                    assert '"source_excerpts": [{"path": "spam_model.py"' in _user
                    assert '"research_materials": {"schema_version": "synthesis_result.v1"' in _user
                    return {"status": "ready", "implementation_spec": "Engineering choice: add prize to the keyword set; retain predict(text) and existing win handling.",
                            "unresolved_questions": []}
                if label.startswith("code-task-review-"):
                    return {"findings": []}
                if label == "code-task-repair" and revised_failure:
                    return {
                        "summary": "Replace the unresolved variable from the failed run.",
                        "edits": [{
                            "path": "spam_model.py",
                            "old": "    return unresolved_keyword\n",
                            "new": "    return 'spam' if any(keyword in lowered for keyword in ('win', 'prize', 'lottery')) else 'ham'\n",
                            "reason": "The failed candidate called an undefined name.",
                        }],
                        "validation": ["Remeasure under the accepted protocol."],
                        "risks": [],
                    }
                if label in {"code-task-work-plan", "code-task-plan"}:
                    return keyword_code_responses()[label]
                if label == "code-task-propose-edits":
                    if refine and not self.gap_returned:
                        self.gap_returned = True
                        return {"summary": "Clarify the allowed keyword behavior.", "edits": [],
                                "implementation_feedback": {"kind": "design_gap", "reason": "Keyword behavior needs clarification.",
                                                            "questions": ["Should prize retain win handling?"]}}
                    self.edit_count += 1
                    response = keyword_code_responses()[label]
                    if self.edit_count > 1:
                        edit = response["edits"][0]
                        edit["old"] = TOY_PREDICT_WITH_PRIZE
                        edit["new"] = TOY_PREDICT_WITH_PRIZE.replace("('win', 'prize')", "('win', 'prize', 'lottery')")
                        if revised_failure:
                            edit["new"] = "def predict(text):\n    lowered = text.lower()\n    return unresolved_keyword\n"
                        response["summary"] = edit["reason"] = "Add lottery keyword support."
                    return response
                raise AssertionError(f"Unexpected JSON LLM label: {label}")


        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "spam_model.py").write_text(TOY_PREDICT_SOURCE, encoding="utf-8")
            (project / "benchmark.py").write_text(
                "from spam_model import predict\n\n"
                "rows = [('win now', 'spam'), ('prize only', 'spam'), ('lottery only', 'spam')]\n"
                "correct = sum(predict(text) == label for text, label in rows)\n"
                "print(f'accuracy: {correct / len(rows):.6f}')\n",
                encoding="utf-8",
            )
            write_keyword_validation_test(project)
            execution = {
                "command": [sys.executable, "benchmark.py"],
                "baseline": {"command": [sys.executable, "benchmark.py"]},
                "cwd": str(project), "timeout_sec": 20,
                "result_schema": {"primary_metric": "accuracy", "metric_directions": {"accuracy": "higher"}},
                "code_task": {"code_root": str(project), "allowed_patterns": ["spam_model.py"],
                              "approval_note": "Approve only the isolated spam_model.py keyword edit.",
                              "max_repairs": 1 if revised_failure else 0},
            }
            app, _ = self._paper_session(
                root, "# Method\n\nValidation improves reliable agent behavior.\n\n"
                "# Results\n\nThe fixture reports accuracy: 0.75.\n",
                filename="reliable_agents.md",
                request_text="Study reliable agents; add prize keyword support without changing tests.",
                requested_outputs=("experiments",), services=ResearchApplicationServices(
                max_results=1, max_attempts=32,
                config={"research_queries": ["reliable agents"], "research_max_iterations": 2},
                budget_limits={"process_invocations": 4 if revised_failure else 3, "process_wall_seconds": 60},
            ))
            paused = app.advance(max_actions=10)
            self.assertEqual(paused.status, "paused")
            research_refs = {
                name: paused.state_refs[name]
                for name in ("documents", "read", "synthesis")
                if name in paused.state_refs
            }
            app.supply_execution(execution, task_text="Keep the existing prediction API unchanged.")
            app = load_session(root / "session")
            self.assertIn("Keep the existing prediction API", app.brief.request_text)
            self.assertNotEqual(app.view().state_refs.get("design"), paused.state_refs.get("design"))
            for name, ref in research_refs.items():
                # supply_execution also changes the task text here; evidence
                # projections must be refreshed for that revised request.
                self.assertNotEqual(app.view().state_refs.get(name), ref)
            self.advance_to(app, "implement")
            if not refine:
                # A no-model proposal is a real persisted blocker with no
                # structured design feedback, as in older server sessions.
                blocked = app.advance()
                self.assertEqual(blocked.status, "paused", blocked.status_reason)
                self.assertEqual(blocked.next_action, "implement")
                baseline_ref = blocked.state_refs["baseline"]
                blocked_attempt = app.controller.manifest.current_attempt
                old_result = root / "session" / "attempts" / blocked_attempt / "capability_result.json"
                old_bytes = old_result.read_bytes()
                app = load_session(root / "session")
                self.assertEqual(app.advance().status, "paused")
                self.assertEqual(len(app.view().attempts), len(blocked.attempts))
                app.continue_session(reason="Retry the blocked implementation with a model.")
                # The retry intent must survive another reload before execution.
                app = load_session(root / "session")
                self.assertIsNone(app.controller.manifest.current_attempt)
                self.assertEqual(app.view().budget["attempts"], blocked.budget["attempts"])
            client = FakeClient()
            app.services = replace(app.services, llm_client=client)
            with patch.object(LLMClient, "for_task", return_value=client):
                if refine:
                    # Crash after persisting a blocked result, before routing
                    # its feedback: reload must recover, not rerun the editor.
                    with patch.object(app, "_schedule_implementation_refinement", side_effect=RuntimeError("interrupted routing")):
                        with self.assertRaisesRegex(RuntimeError, "interrupted routing"):
                            app.advance()
                    app = load_session(root / "session", services=ResearchApplicationServices(llm_client=client))
                    attempt_count = len(app.view().attempts)
                    with patch.object(app, "_run_action", return_value=False):
                        implemented = app.advance()
                    self.assertEqual(len(implemented.attempts), attempt_count)
                else:
                    implemented = app.advance()
            if not refine:
                self.assertEqual(len(implemented.attempts), len(blocked.attempts) + 1)
                self.assertEqual(implemented.state_refs["baseline"], baseline_ref)
                self.assertEqual(old_result.read_bytes(), old_bytes)
            if refine:
                self.assertEqual(implemented.next_action, "refine_implementation:1", implemented.status_reason)
                baseline_ref = implemented.state_refs["baseline"]
                old_preparation = app.controller.store.read_json(implemented.state_refs["preparation"])
                # Inspect the actual frozen handoff from the failed attempt instead of guessing its layout.
                feedback = app._state_payload("feedback:1")
                old_handoff = Path(feedback["code_task_run_dir"]) / "code_task" / "research_handoff.json"
                old_bytes = old_handoff.read_bytes()
                app = load_session(root / "session", services=ResearchApplicationServices(llm_client=client))
                interrupted = app.advance()
                self.assertEqual(interrupted.status, "paused")
                self.assertEqual(interrupted.next_action, "refine_implementation:1")
                app = load_session(root / "session", services=ResearchApplicationServices(llm_client=client))
                app.continue_session(interaction="checkpoints")
                clarified = app.advance()
                self.assertEqual(clarified.next_action, "prepare_implementation:1", clarified.status_reason)
                self.assertEqual(client.refinement_calls, 3)
                # Every downstream consumer must use the clarified design.
                refined_ref = app._implementation_design_ref()
                refined = app.controller.store.read_json(refined_ref)
                self.assertIn("Engineering choice", refined["implementation_spec"])
                self.assertEqual(app._execution_contract(), refined["contract"])
                self.assertEqual(app._interaction_identity("execution_protocol", "prepare_implementation:1")["design_ref"], refined_ref.to_dict())
                gate_view = app.advance()
                self.assertEqual(gate_view.status, "paused")
                gate = gate_view.work_plan["interaction"]["decision"]
                self.assertEqual(gate["stage"], "execution_protocol")
                app.continue_session(decision_id=gate["id"], decision_response="accept", interaction="autonomous")
                app = load_session(root / "session", services=ResearchApplicationServices(llm_client=client))
                with patch.object(LLMClient, "for_task", return_value=client):
                    implemented = app.advance(max_actions=2)
                self.assertEqual(implemented.state_refs["baseline"], baseline_ref)
                self.assertEqual(old_handoff.read_bytes(), old_bytes)
                self.assertNotEqual(app.controller.store.read_json(app._active_preparation_ref())["workspace"], old_preparation["workspace"])
                self.assertEqual(sum(a["capability"] == "experiment" for a in implemented.attempts), 1)
            self.assertEqual(implemented.next_action, "experiment", implemented.status_reason)
            implementation_ref = implemented.state_refs["implementation"]
            task_input_ref = app.controller.store.ref(
                Path(implementation_ref.path).parent / "inputs" / "research_code_task.md",
                kind="task_input",
            )
            implementation_task = app.controller.store.read_text(task_input_ref)
            self.assertIn("## User research objective", implementation_task)
            self.assertIn("Study reliable agents; add prize keyword support without changing tests.", implementation_task)
            self.assertIn("selecting a concrete option within the accepted design and CodeTask scope", implementation_task)
            prepared = app.controller.store.read_json(app._active_preparation_ref())
            candidate_workspace = Path(prepared["workspace"])
            self.assertIn("prize", (candidate_workspace / "spam_model.py").read_text(encoding="utf-8"))
            revision_config, revision_reason = app._revision_execution_config()
            self.assertEqual(revision_reason, "")
            self.assertEqual(
                Path(revision_config["code_task"]["code_root"]).resolve(),
                candidate_workspace.resolve(),
            )
            first_round = app.advance(max_actions=3)
            if first_round.next_action == "prepare_candidate:1":
                first_round = app.advance(max_actions=1)
            self.assertEqual(first_round.next_action, "revise_candidate:1", first_round.status_reason)
            self.assertIn("preparation_r1", first_round.state_refs)
            baseline_ref = first_round.state_refs["baseline"]
            first_candidate_ref = first_round.state_refs["experiment"]
            baseline = app.controller.store.read_json(baseline_ref)
            first_candidate = app.controller.store.read_json(first_candidate_ref)
            self.assertAlmostEqual(baseline["metrics"]["accuracy"], 1 / 3, places=5)
            self.assertAlmostEqual(first_candidate["metrics"]["accuracy"], 2 / 3, places=5)

            app = load_session(root / "session")
            self.assertEqual(app.view().next_action, "revise_candidate:1")
            self.assertEqual(app.controller.manifest.state_refs["baseline"], baseline_ref)
            self.assertEqual(app.controller.manifest.state_refs["experiment"], first_candidate_ref)
            app.services = replace(app.services, llm_client=client)
            with patch.object(LLMClient, "for_task", return_value=client):
                implemented_revision = app.advance(max_actions=1)
            self.assertEqual(implemented_revision.next_action, "research_candidate:1", implemented_revision.status_reason)
            revised_measurement = app.advance(max_actions=1)
            self.assertEqual(
                revised_measurement.next_action,
                "repair_candidate:1_1" if revised_failure else "reanalysis:1",
                revised_measurement.status_reason,
            )
            if revised_failure:
                with patch.object(LLMClient, "for_task", return_value=client):
                    repaired = app.advance(max_actions=2)
                self.assertEqual(repaired.next_action, "reanalysis:1", repaired.status_reason)
                self.assertIn("implementation_r1_repair_1", repaired.state_refs)
                self.assertIn("experiment_revision_1_repair_1", repaired.state_refs)
                self.assertEqual(app.controller.store.read_json(
                    repaired.state_refs["experiment_revision_1"])["execution_status"], "failed")
                revision = app.controller.store.read_json(repaired.state_refs["implementation_r1"])
                repair = app.controller.store.read_json(repaired.state_refs["implementation_r1_repair_1"])
                self.assertEqual(repair["failure_ref"], repaired.state_refs["experiment_revision_1"].to_dict())
                self.assertIsNone(repair["validation"])
                revision_handoff = app.controller.store.read_json(
                    Path(repaired.state_refs["implementation_r1"].path).parent / revision["artifact_refs"]["research_handoff"]["path"])
                repair_handoff = app.controller.store.read_json(
                    Path(repaired.state_refs["implementation_r1_repair_1"].path).parent / repair["artifact_refs"]["research_handoff"]["path"])
                self.assertEqual(repair_handoff["consumed"], revision_handoff["consumed"])
                self.assertIn("Add lottery keyword support.", repair_handoff["consumed"]["revision_instruction"])
                repair_attempt = next(row for row in repaired.attempts
                    if row["trigger"] == "application:implementation_r1_repair_1")
                self.assertEqual(repair_attempt["capability"], "implement")
                self.assertIn(baseline_ref.to_dict(), repair_attempt["inputs"])
            revision_ref = revised_measurement.state_refs["experiment_revision_1"]
            revision_step = next(step for step in app._load_task_plan().steps if step.state_name == "experiment_revision_1")
            self.assertTrue(app._step_completed(revision_step), app.controller.store.read_json(revision_ref))
            self.assertEqual(app.latest_experiment_ref(),
                             app.view().state_refs["experiment_revision_1_repair_1"] if revised_failure else revision_ref)
            final = app.advance(max_actions=1)
            self.assertEqual(final.status, "completed", final.status_reason)
            if refine:
                report_context, _ = app.report_inputs()
                self.assertTrue(any(handle.artifact == refined_ref.path for handle in report_context.source_handles))
            baseline = app.controller.store.read_json(final.state_refs["baseline"])
            first_candidate = app.controller.store.read_json(final.state_refs["experiment"])
            candidate = app.controller.store.read_json(final.state_refs[
                "experiment_revision_1_repair_1" if revised_failure else "experiment_revision_1"])
            self.assertAlmostEqual(candidate["metrics"]["accuracy"], 1.0)
            self.assertEqual(app.budget_ledger.remaining("process_invocations"), 0)
            self.assertNotIn("'prize'", (project / "spam_model.py").read_text())
            analysis = app.controller.store.read_json(final.state_refs["analysis_r1"])["analysis"]
            decision_context = analysis["decision_context"]
            self.assertIn("prize", decision_context["research_goal"])
            checkpoint = decision_context["analysis_checkpoint"]
            current_candidate = checkpoint["current_candidate"]
            current_result_key = "experiment_revision_1_repair_1" if revised_failure else "experiment_revision_1"
            current_implementation_key = "implementation_r1_repair_1" if revised_failure else "implementation_r1"
            self.assertEqual(current_candidate["artifact_ref"]["path"], final.state_refs[current_result_key].path)
            self.assertEqual(current_candidate["implementation_ref"]["path"], final.state_refs[current_implementation_key].path)
            roles = {row["role"]: row for row in checkpoint["measurements"]}
            self.assertEqual(roles["baseline"]["artifact_ref"]["path"], final.state_refs["baseline"].path)
            self.assertEqual(roles["current_candidate"]["artifact_ref"]["path"], final.state_refs[current_result_key].path)
            self.assertEqual(checkpoint["stage"], "post_measurement_decision")
            history = decision_context["research_history"]
            implementation = next(row for row in reversed(history) if row["capability"] == "implement")
            implementation_output = next(
                output for output in implementation["outputs"]
                if output["kind"] == "implementation_result"
            )
            self.assertTrue(implementation_output["patch_available"])
            self.assertIn("lottery", implementation_output["patch_excerpt"])
            self.assertFalse(implementation_output["patch_truncated"])
            if not revised_failure:
                self.assertEqual(implementation_output["validation_report"]["status"], "passed")
            history_refs = {
                output["ref"]["path"]
                for row in history for output in row.get("outputs", [])
            }
            self.assertIn(final.state_refs["baseline"].path, history_refs)
            self.assertIn(final.state_refs["experiment"].path, history_refs)
            self.assertIn(final.state_refs["experiment_revision_1"].path, history_refs)
            if revised_failure:
                self.assertIn(final.state_refs["experiment_revision_1_repair_1"].path, history_refs)
            measurements = [
                output["metrics"]
                for row in history if row["capability"] == "experiment"
                for output in row.get("outputs", []) if output["kind"] == "experiment_result"
            ]
            self.assertEqual(
                [row.get("accuracy") for row in measurements],
                [baseline["metrics"]["accuracy"], first_candidate["metrics"]["accuracy"],
                 *([None] if revised_failure else []), candidate["metrics"]["accuracy"]],
            )
            context, _ = app.report_inputs()
            self.assertIn("implementation", context.results)
            self.assertIn("lottery", str(context.results["implementation"]))
            if revised_failure:
                history = context.results["measurement_history"]
                self.assertEqual(
                    [row["status"] for row in history[-3:]],
                    ["passed", "failed", "passed"],
                )
                self.assertTrue(any(
                    row.label == "experiment" and row.name == "accuracy"
                    for row in context.metric_sources
                ))
            before = final.state_refs
            attempts_before_preference = len(final.attempts)
            reloaded = load_session(root / "session", services=ResearchApplicationServices(llm_client=client))
            restored = reloaded.advance(max_actions=5)
            self.assertEqual(restored.state_refs, before)
            self.assertEqual(len(restored.attempts), attempts_before_preference)
            self.assertEqual(reloaded.budget_ledger.remaining("process_invocations"), 0)

            reloaded.continue_session(
                revised_brief=replace(reloaded.brief, preferences=("Keep the report concise.",)),
            )
            preference_resume = reloaded.advance(max_actions=5)
            self.assertEqual(len(preference_resume.attempts), attempts_before_preference)
            self.assertEqual(preference_resume.state_refs["implementation_r1"], before["implementation_r1"])
            self.assertEqual(preference_resume.state_refs["experiment_revision_1"], before["experiment_revision_1"])
            if revised_failure:
                self.assertEqual(preference_resume.state_refs["implementation_r1_repair_1"], before["implementation_r1_repair_1"])
                self.assertEqual(preference_resume.state_refs["experiment_revision_1_repair_1"], before["experiment_revision_1_repair_1"])
            self.assertEqual(reloaded.budget_ledger.remaining("process_invocations"), 0)

    def test_code_task_supplement_uses_original_baseline_and_current_candidate(self):
        from simple_ar.app.research_execution import normalize_execution_config
        from simple_ar.code_task.orchestration.workflow import initialize_code_task

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "project"
            source.mkdir()
            (source / "model.py").write_text(
                "def predict(text):\n"
                "    return 'spam' if 'win' in text.lower() else 'ham'\n",
                encoding="utf-8",
            )
            (source / "benchmark.py").write_text(
                "import sys\nfrom pathlib import Path\n"
                "from model import predict\n"
                "role = sys.argv[1]\nseed = sys.argv[-1]\n"
                "with Path('runs.txt').open('a') as handle: handle.write(f'{role}:{seed}\\n')\n"
                "rows = [('win now', 'spam'), ('prize only', 'spam')]\n"
                "score = sum(predict(text) == label for text, label in rows) / len(rows)\n"
                "print('accuracy:', score)\n",
                encoding="utf-8",
            )
            task_file = root / "task.md"
            task_file.write_text("Apply the bounded candidate patch in the isolated workspace.\n", encoding="utf-8")
            initialized = initialize_code_task(
                run_dir=root / "initial_code_task",
                code_root=source,
                task_file=task_file,
                benchmark_command="python benchmark.py candidate",
                workspace_mode="copy",
                edit_scope_allowed_patterns=("model.py",),
            )
            candidate_workspace = initialized.workspace_dir
            (candidate_workspace / "model.py").write_text(
                "def predict(text):\n"
                "    return 'spam' if any(word in text.lower() for word in ('win', 'prize')) else 'ham'\n",
                encoding="utf-8",
            )
            contract = {
                "contract_id": "code-task-supplement-v1",
                "hypothesis": "The isolated candidate improves the supplied evaluator.",
                "dataset_refs": [{"asset_id": "fixture", "revision": "1"}],
                "split_spec": {"name": "fixed"},
                "metric_specs": [{"name": "accuracy", "unit": "fraction", "direction": "higher"}],
                "comparison_conditions": {"evaluator": "benchmark"},
                "protected_assets": [{"asset_id": "evaluator", "path": "benchmark.py"}],
            }
            raw_execution = {
                "command": [sys.executable, "benchmark.py", "candidate"],
                "baseline": {"command": [sys.executable, "benchmark.py", "baseline"]},
                "cwd": str(source), "timeout_sec": 5,
                "seeds": [0, 1], "seed_flag": "--seed", "baseline_policy": "run",
                "result_schema": {"primary_metric": "accuracy", "direction": "higher"},
                "protocol": contract,
                "code_task": {
                    "code_root": str(source),
                    "allowed_patterns": ["model.py"],
                    "approval_note": "Use only the isolated model.py workspace.",
                },
            }
            app = create_session(
                ResearchBrief(request_text="Compare the supplied candidate.", requested_outputs=("experiments",)),
                root=root / "session",
                services=ResearchApplicationServices(
                    config={"research_materials_only": True, "execution": raw_execution},
                    budget_limits={"process_invocations": 2, "process_wall_seconds": 20},
                ),
            )
            design_ref = app.controller.store.write_json(
                "inputs/design.json", {"contract": contract, "execution_protocol": {}},
                kind="research_design", schema="research_design.v1", producer="test",
            )
            decision_ref = app.controller.store.write_json(
                "outputs/research_decision.json", {
                    "recommendation": {
                        "action": "supplement",
                        "reason": "Check the same evaluator under one new seed.",
                        "supplement": {"seed": 2, "gap": "one additional paired condition"},
                    },
                }, kind="research_decision", schema="research_decision.v1", producer="test",
            )
            active_execution = normalize_execution_config({
                **raw_execution,
                "cwd": str(candidate_workspace),
                "code_task": {
                    **raw_execution["code_task"],
                    "run_dir": str(initialized.run_dir),
                },
            })
            preparation_ref = app.controller.store.write_json(
                "inputs/prepared_execution.json", {
                    "schema_version": "prepared_execution.v1",
                    "execution": active_execution,
                    "source_project": str(source.resolve()),
                    "workspace": str(candidate_workspace),
                    "limitations": [],
                }, kind="prepared_execution", schema="prepared_execution.v1", producer="test",
            )
            implementation_ref = app.controller.store.write_json(
                "inputs/implementation.json", {"status": "validated"},
                kind="implementation_result", schema="research_implementation.v1", producer="test",
            )
            app.controller.manifest.state_refs.update({
                "design": design_ref, "decision": decision_ref, "preparation": preparation_ref,
                "implementation": implementation_ref,
            })
            app.controller.save()

            with (patch.object(app, "_pause_action", return_value=False) as rejected,
                    patch.object(app, "_execute") as dispatch):
                self.assertFalse(app._run_action("supplement_baseline:1_0"))
                rejected.assert_called_once_with("Supplement action does not match the saved measurement condition count.")
                dispatch.assert_not_called()
            self.assertTrue(app._run_action("supplement_baseline:1"))
            self.assertTrue(app._run_action("supplement_candidate:1"))
            baseline_ref = app.controller.manifest.state_refs["baseline_supplement_1"]
            candidate_ref = app.controller.manifest.state_refs["experiment_supplement_1"]
            baseline = app.controller.store.read_json(baseline_ref)
            candidate = app.controller.store.read_json(candidate_ref)
            self.assertEqual(baseline["metrics"]["accuracy"], 0.5)
            self.assertEqual(candidate["metrics"]["accuracy"], 1.0)
            self.assertNotIn("implementation_ref", baseline)
            self.assertEqual(candidate["implementation_ref"], implementation_ref.to_dict())
            baseline_prep = app.controller.store.read_json(
                app.controller.manifest.state_refs["preparation_supplement_1"]
            )
            self.assertNotEqual(Path(baseline_prep["workspace"]).resolve(), candidate_workspace.resolve())
            self.assertNotIn("prize", (Path(baseline_prep["workspace"]) / "model.py").read_text(encoding="utf-8"))
            self.assertIn("prize", (candidate_workspace / "model.py").read_text(encoding="utf-8"))
            self.assertEqual(
                baseline["preparation"]["source_ref"]["path"],
                app.controller.manifest.state_refs["preparation_supplement_1"].path,
            )
            self.assertEqual(
                candidate["preparation"]["source_ref"]["path"],
                preparation_ref.path,
            )

    def test_explicit_pairs_recover_each_measurement_without_repeating_baselines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "evaluate.py"
            script.write_text(
                "import sys\nfrom pathlib import Path\n"
                "with Path('runs.txt').open('a') as f: f.write(sys.argv[1]+'\\n')\n"
                "labels=[0,1,1,0]\n"
                "predictions=[0,1,0,0] if sys.argv[1].startswith('baseline') else [0,0,0,0]\n"
                "print('accuracy:', sum(a==b for a,b in zip(labels,predictions))/len(labels))\n",
                encoding="utf-8",
            )
            pairs = [{"seed": seed,
                "baseline_command": [sys.executable, str(script), f"baseline-{seed}"],
                "candidate_command": [sys.executable, str(script), f"candidate-{seed}"]} for seed in (0, 1)]
            app, paper = self._paper_session(
                root,
                "# Classification\nCompare measured classifier outcomes.\n",
                request_text="Compare classifiers.",
                requested_outputs=("experiments", "report"),
                services=ResearchApplicationServices(max_results=1, max_attempts=24, config={"execution": {
                    "pairs": pairs, "cwd": str(root), "timeout_sec": 5,
                    "result_schema": {"primary_metric": "accuracy", "direction": "higher"},
                    "protocol": {"contract_id": "fixture-v1", "hypothesis": "Compare the fixture conditions.",
                                 "dataset_refs": [{"asset_id": "fixture"}], "split_spec": {"name": "test"},
                                 "metric_specs": [{"name": "accuracy", "unit": "fraction"}],
                                 "comparison_conditions": {"batch_size": 1}}},
                    # This fixture asserts inline aggregate rows; the default
                    # linked presentation keeps them in the evidence package.
                    "report": {"data_tables": "full", "figures": {"enabled": True, "max_figures": 1}}},
                    budget_limits={"process_invocations": 4, "process_wall_seconds": 20}),
            )
            plan = app.controller.store.read_json(app.view().state_refs["work_plan"])
            self.assertEqual(plan["requested_outputs"][0]["status"], "pending")
            self.advance_to(app, "research_design")
            app.advance(max_actions=1)
            self.assertEqual(app.view().next_action, "plan")
            # The first plan ends at the design checkpoint.  The same plan
            # capability then binds the accepted execution protocol before
            # any measurement action is selected.
            app.advance(max_actions=1)
            self.assertEqual(app.view().next_action, "matrix_baseline_0")
            with patch.object(app, "_persist_application_views", side_effect=RuntimeError("after measurement")):
                with self.assertRaisesRegex(RuntimeError, "after measurement"):
                    app.advance()
            app = load_session(root / "session")
            app.advance()
            plan = app.controller.store.read_json(app.view().state_refs["work_plan"])
            self.assertEqual(plan["requested_outputs"][0]["status"], "partial")
            final = app.advance(max_actions=3)
            self.assertEqual(final.next_action, "report_write", final.status_reason)
            self.assertNotIn("report", final.state_refs)
            collection = app.controller.store.read_json(final.state_refs["matrix_results"])
            self.assertEqual([row["seed"] for row in collection["pairs"]], [0, 1])
            self.assertTrue(all(row["baseline"] and row["candidate"] for row in collection["pairs"]))
            for i in range(2):
                for role in ("baseline", "candidate"):
                    result = app.controller.store.read_json(final.state_refs[f"matrix_{role}_{i}"])
                    self.assertEqual(result["status"], "passed")
            self.assertEqual((root / "runs.txt").read_text().splitlines(),
                             ["baseline-0", "baseline-1", "candidate-0", "candidate-1"])
            self.assertEqual(len(app.budget_ledger.entries), 4)
            analysis = app.controller.store.read_json(final.state_refs["analysis"])
            self.assertEqual(analysis["execution_ref"], final.state_refs["matrix_results"].to_dict())
            self.assertTrue(analysis["analysis"]["claims"])
            self.assertEqual(
                analysis["analysis"]["claims"][0]["claim"],
                "Compare measured classifier outcomes.",
            )
            self.assertEqual(
                analysis["analysis"]["claims"][0]["verdict"],
                "not_evaluated",
            )
            self.assertIn(
                "accuracy",
                {
                    row["name"]
                    for row in analysis["analysis"]["metric_summary"]["metrics"]
                },
            )
            paired = app.controller.store.read_json(
                app.controller.store.ref(Path(final.state_refs["analysis"].path).parent / "paired_analysis.json")
            )
            self.assertEqual(paired["planned_pairs"], 2)
            self.assertEqual(len(paired["paired_summary"]), 1)
            self.assertIn("Planned pairs: 2", analysis["analysis"]["readme_markdown"])
            context, memory = app.report_inputs()
            self.assertEqual(len(context.results.get("comparisons", [])), 2, context.results)
            self.assertTrue(context.experiment_plan.get("hypothesis"))
            self.assertEqual(len(context.experiment_plan["paired_protocols"]), 4)
            measured_sources = [m for m in context.metric_sources if m.source_kind == "measured"]
            self.assertEqual({m.label for m in measured_sources},
                             {f"{role}:seed={seed}" for role in ("baseline", "candidate") for seed in (0, 1)})
            self.assertEqual(len([m for m in context.metric_sources if m.source_kind == "measured"]), 4)
            self.assertEqual(len([m for m in context.metric_sources if m.source_kind == "derived_comparison"]), 2)
            self.assertEqual(len([m for m in context.metric_sources if m.source_kind == "derived_summary"]), 5)
            self.assertEqual(len({m.artifact for m in measured_sources}), 4)
            derived = [m for m in context.metric_sources if m.source_kind == "derived_comparison"]
            self.assertEqual([m.value for m in derived], [-0.25, -0.25])
            self.assertEqual(memory.metric_sources, context.metric_sources)
            self.assertEqual({m.artifact for m in derived},
                             {(Path(final.state_refs["analysis"].path).parent / "paired_analysis.json").as_posix()})
            from simple_ar.report.schema import AgentReportResult, ReportSectionDraft
            app.services = replace(app.services, llm_client=LLMClient(LLMSettings(api_key="fixture-key", api_mode="chat")))
            def writer(**kwargs):
                paper_id = kwargs["context"].papers[0]["id"]
                return AgentReportResult(report_body="", memory=kwargs["memory"], used_agent=True,
                    sections=[ReportSectionDraft(section_id=str(i), heading=heading,
                        draft_markdown=f"Fixture paired evaluation; no significance claim [@{paper_id}].",
                        used_sources=[paper_id]) for i, heading in enumerate(("Abstract", "Introduction", "Related Work",
                            "Method", "Experimental Setup", "Results", "Limitations", "Conclusion"))])
            with patch("simple_ar.report.writing.run_report_agent", side_effect=writer):
                final = app.advance(max_actions=3)
            self.assertEqual(final.status, "completed", final.status_reason)
            report = app.controller.store.read_text(final.state_refs["report"])
            self.assertIn("Aggregate Paired Metrics", report)
            self.assertIn("`accuracy`", report)
            self.assertNotIn("baseline:seed=0", report)
            audit = app.controller.store.read_json(final.state_refs["report_audit"])
            self.assertEqual(
                set(audit["metric_audit"]["matched_metrics"]),
                {
                    m.metric_id
                    for m in context.metric_sources
                    if m.source_kind == "derived_summary"
                },
            )
            report_dir = Path(final.state_refs["report"].path).parent
            report_text = app.controller.store.read_text(final.state_refs["report"])
            self.assertIn("Measured comparisons", report_text, msg=report_text)
            self.assertTrue((app.controller.store.resolve(report_dir / "figures" / "paired-1.svg")).is_file())
            self.assertIn("paired-1.svg", report_text)
            self.assertEqual(len(app.budget_ledger.entries), 4)

    def test_compact_seed_protocol_skips_baseline_and_reloads_without_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "measure.py"
            script.write_text(
                "import sys\nfrom pathlib import Path\n"
                "with Path('calls.txt').open('a') as handle: handle.write(sys.argv[-1] + '\\n')\n"
                "print('accuracy:', 0.5 + int(sys.argv[-1]) / 10)\n",
                encoding="utf-8",
            )
            app, paper = self._paper_session(
                root,
                "# Seed comparison\nA bounded fixture supports two measured conditions.\n",
                request_text="Run two different seeds for this bounded fixture.",
                requested_outputs=("experiments",),
                services=ResearchApplicationServices(
                    max_results=1,
                    max_chunks=10,
                    max_attempts=20,
                    config={
                        "research_materials_only": True,
                        "execution": {
                            "command": [sys.executable, str(script)],
                            "cwd": str(root),
                            "timeout_sec": 5,
                            "seeds": [0, 1],
                            "seed_flag": "--seed",
                            "baseline_policy": "skip",
                            "result_schema": {"primary_metric": "accuracy", "direction": "higher"},
                        },
                    },
                    budget_limits={"process_invocations": 2, "process_wall_seconds": 20},
                ),
            )
            finished = app.advance(max_actions=20)
            self.assertEqual(finished.status, "completed", finished.status_reason)
            plan = app.controller.store.read_json(finished.state_refs["work_plan"])
            actions = [row["action"] for row in plan["accepted_plan"]["steps"]]
            self.assertNotIn("matrix_baseline_0", actions)
            self.assertEqual(plan["execution_protocol"]["seeds"], [0, 1])
            self.assertEqual(plan["execution_decision"]["baseline"]["mode"], "skip")
            collection = app.controller.store.read_json(finished.state_refs["matrix_results"])
            self.assertTrue(all(row["baseline"] is None and row["candidate"] for row in collection["pairs"]))
            self.assertEqual((root / "calls.txt").read_text(encoding="utf-8").splitlines(), ["0", "1"])
            attempts = len(finished.attempts)
            restored = load_session(root / "session")
            resumed = restored.advance(max_actions=20)
            self.assertEqual(len(resumed.attempts), attempts)
            self.assertEqual((root / "calls.txt").read_text(encoding="utf-8").splitlines(), ["0", "1"])

    def test_analysis_recommendation_accepts_one_supplement_then_stops_without_rerun(self):
        from simple_ar.result_analysis import AnalysisRecommendation, AnalysisResult

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "measure.py"
            script.write_text(
                "import sys\nfrom pathlib import Path\n"
                "role, _, seed = sys.argv[1:]\n"
                "with Path('calls.txt').open('a') as handle: handle.write(f'{role}:{seed}\\n')\n"
                "labels = [0, 1, 1, 0]\n"
                "predictions = ([0, 1, 0, 0] if role == 'baseline' else\n"
                "              ([0, 1, 1, 0] if int(seed) == 2 else [0, 0, 0, 0]))\n"
                "correct = sum(label == prediction for label, prediction in zip(labels, predictions))\n"
                "print('accuracy:', correct / len(labels))\n",
                encoding="utf-8",
            )
            execution = {
                "command": [sys.executable, str(script), "candidate"],
                "baseline": {"command": [sys.executable, str(script), "baseline"]},
                "cwd": str(root), "timeout_sec": 5,
                "seeds": [0, 1], "seed_flag": "--seed",
                "result_schema": {"primary_metric": "accuracy", "direction": "higher"},
                "protocol": {
                    "contract_id": "supplement-fixture-v1",
                    "hypothesis": "The candidate improves the measured fixture.",
                    "dataset_refs": [{"asset_id": "fixture", "revision": "1"}],
                    "split_spec": {"name": "fixed"},
                    "metric_specs": [{"name": "accuracy", "unit": "fraction"}],
                    "comparison_conditions": {"evaluator": "fixture"},
                    "protected_assets": [{"asset_id": "evaluator", "path": str(script)}],
                },
            }
            app, paper = self._paper_session(
                root,
                "# Supplement\nA bounded result supports one extra seed.\n",
                request_text="Compare the supplied fixture under accepted seed conditions.",
                requested_outputs=("experiments",),
                services=ResearchApplicationServices(
                    max_results=1, max_chunks=10, max_attempts=40,
                    config={
                        "research_materials_only": True,
                        "interaction": "assisted",
                        "research_max_iterations": 1,
                        "execution": execution,
                    },
                    budget_limits={"process_invocations": 8, "process_wall_seconds": 80},
                ),
            )
            analysis_calls = []

            def analysis_with_bounded_proposal(request, *, client=None):
                del client
                analysis_calls.append(request.context)
                if len(analysis_calls) == 1:
                    recommendation = AnalysisRecommendation(
                        action="supplement",
                        reason="The first comparison needs two additional seeds to check the observed directional gap.",
                        evidence_refs=["paired_summary:accuracy"],
                        supplement={
                            "seeds": [2, 3],
                            "gap": "single-seed directional evidence",
                            "conditions": "same evaluator and split under seeds 2 and 3",
                            "metric": "accuracy",
                        },
                    )
                else:
                    recommendation = AnalysisRecommendation(
                        action="stop",
                        reason="The bounded supplement has been re-analyzed; retain the measured evidence.",
                        evidence_refs=["paired_summary:accuracy"],
                    )
                return AnalysisResult(
                    readme_markdown="# Fixture analysis\n",
                    status="passed",
                    recommendation=recommendation,
                )

            # The process computes accuracy from labels/predictions. The
            # recommendation is a test double for the model boundary, not live
            # model acceptance evidence.
            with patch("simple_ar.research.analysis.analyze_results", side_effect=analysis_with_bounded_proposal):
                pending = app.advance(max_actions=40)
                self.assertEqual(pending.status, "paused", pending.status_reason)
                gate = pending.work_plan["interaction"]["decision"]
                self.assertEqual(gate["stage"], "execution_protocol")
                app = load_session(root / "session")
                app.continue_session(decision_id=gate["id"], decision_response="accept")
                pending = app.advance(max_actions=40)
                self.assertEqual(pending.status, "paused", pending.status_reason)
                gate = pending.work_plan["interaction"]["decision"]
                self.assertEqual(gate["stage"], "research_choice")
                persisted_gate = app.controller.store.read_json(pending.state_refs["decision"])["interaction"]
                self.assertEqual(persisted_gate["proposed_action"], "supplement")
                app = load_session(root / "session")
                app.continue_session(decision_id=gate["id"], decision_response="accept")
                final = app.advance(max_actions=40)
            self.assertEqual(final.status, "completed", final.status_reason)
            self.assertIn("analysis_r1", final.state_refs)
            self.assertEqual(len(analysis_calls), 2)
            decision = app.controller.store.read_json(final.state_refs["decision"])
            self.assertEqual(decision["action"], "stop")
            self.assertEqual(decision["research_iteration"], 1)
            self.assertFalse(decision["bounded_cycle"]["automatic_follow_up"])
            self.assertEqual(
                decision["identity"]["analysis_ref"]["path"],
                final.state_refs["analysis_r1"].path,
            )
            self.assertIn("prior_decision_ref", decision)
            plan = app.controller.store.read_json(final.state_refs["task_plan"])
            self.assertIn("supplement_baseline:1_0", [row["action"] for row in plan["steps"]])
            self.assertIn("supplement_baseline:1_1", [row["action"] for row in plan["steps"]])
            self.assertEqual(
                (root / "calls.txt").read_text(encoding="utf-8").splitlines(),
                [
                    "baseline:0", "baseline:1", "candidate:0", "candidate:1",
                    "baseline:2", "baseline:3", "candidate:2", "candidate:3",
                ],
            )
            history = app._research_history()
            supplement_history = next(
                row for row in history if row["action"] == "baseline_supplement_1_0"
            )
            self.assertTrue(supplement_history.get("outputs"))
            blocked, reason = app._supplement_execution_config(
                2,
                {"seed": 2, "gap": "repeat the completed supplement"},
            )
            self.assertIsNone(blocked)
            self.assertIn("already measured", reason)
            first_candidate = app.controller.store.read_json(final.state_refs["matrix_candidate_0"])
            self.assertEqual(first_candidate["metrics"]["accuracy"], 0.5)
            attempts = len(final.attempts)
            restored = load_session(root / "session")
            resumed = restored.advance(max_actions=40)
            self.assertEqual(resumed.status, "completed")
            self.assertEqual(len(resumed.attempts), attempts)
            self.assertEqual(
                (root / "calls.txt").read_text(encoding="utf-8").splitlines(),
                [
                    "baseline:0", "baseline:1", "candidate:0", "candidate:1",
                    "baseline:2", "baseline:3", "candidate:2", "candidate:3",
                ],
            )

    def test_baseline_reuse_preserves_condition_and_protected_asset_rules(self):
        from simple_ar.app.research_execution import execution_request
        from simple_ar.core.capabilities import ArtifactRef
        from simple_ar.experiment.execution.measurement import measurement_reuse_matches
        for protected in (True, False):
            with self.subTest(protected=protected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                script = root / "measure.py"
                script.write_text(
                    "import sys\nfrom pathlib import Path\n"
                    "with Path('calls.txt').open('a') as handle: handle.write(sys.argv[1] + '\\n')\n"
                    "print('accuracy: 0.7')\n" if protected else "import sys\nprint('accuracy: 0.7')\n",
                    encoding="utf-8",
                )
                execution = {
                    "command": [sys.executable, str(script), "candidate"],
                    "baseline": {"command": [sys.executable, str(script), "baseline"]},
                    "cwd": str(root), "timeout_sec": 5,
                    "result_schema": {"primary_metric": "accuracy", "direction": "higher"},
                    "protocol": {
                        "contract_id": "reuse-fixture-v1" if protected else "same-session-reuse-v1",
                        "hypothesis": "The same evaluator condition remains comparable.",
                        "dataset_refs": [{"asset_id": "fixture", "revision": "v1"}],
                        "split_spec": {"name": "test"},
                        "metric_specs": [{"name": "accuracy", "unit": "fraction"}],
                        "comparison_conditions": {"method": "fixture"},
                    },
                }
                if protected:
                    execution["protocol"]["protected_assets"] = [
                        {"asset_id": "evaluator", "path": "measure.py"},
                    ]
                app, paper = self._paper_session(
                    root,
                    ("# Baseline reuse\nA fixture keeps the comparison boundary explicit.\n" if protected else
                     "# Baseline reuse\nThe evaluator is fixed for this session.\n"),
                    request_text="Measure the fixture.",
                    requested_outputs=("experiments",),
                    services=ResearchApplicationServices(
                        max_results=1, max_chunks=10, max_attempts=20,
                        config={"research_materials_only": True, "execution": execution},
                        budget_limits={"process_invocations": 2, "process_wall_seconds": 20},
                    ),
                )
                self.advance_to(app, "baseline")
                after_baseline = app.advance(max_actions=1)
                baseline_ref = after_baseline.state_refs["baseline"]
                payload = app.controller.store.read_json(baseline_ref)
                expected = execution_request(execution, condition='baseline', contract=app._execution_contract())
                retained = json.loads(json.dumps(payload))
                ledger = app.budget_ledger.to_dict()
                with patch('simple_ar.app.research_application.measurement_reuse_matches',
                           wraps=measurement_reuse_matches) as shared:
                    self.assertTrue(app._measurement_ref_matches(baseline_ref, execution))
                shared.assert_called_once()
                self.assertTrue(shared.call_args.kwargs['same_session_result'])
                self.assertEqual(measurement_reuse_matches(payload, expected), protected)
                for change in (
                    {'status': 'failed'}, {'command': [sys.executable, 'different.py']},
                    {'result_schema': {}}, {'validity_status': 'invalid'},
                    {'experiment_contract': {**payload['experiment_contract'], 'split_spec': {'name': 'other'}}},
                    {'measurement': {**payload.get('measurement', {}), 'asset_integrity': {'status': 'changed'}}},
                ):
                    with self.subTest(change=change):
                        self.assertFalse(measurement_reuse_matches({**payload, **change}, expected,
                                                                  same_session_result=True))
                if protected:
                    incomplete = {**payload, 'measurement': {**payload['measurement'],
                        'asset_integrity': {'status': 'observed_unchanged', 'before': {}}}}
                    self.assertFalse(measurement_reuse_matches(incomplete, expected, same_session_result=True))
                prepared = {**payload, 'preparation': {'source_ref': {'path': 'inputs/prior.json',
                    'kind': 'prepared_execution'}}}
                source = {'source_project': str(root), 'workspace': str(root / 'old-copy')}
                current = {**source, 'workspace': str(root / 'new-copy')}
                self.assertTrue(measurement_reuse_matches(prepared, expected, same_session_result=True,
                                                         actual_source=source, current_source=current))
                self.assertEqual(measurement_reuse_matches(prepared, expected,
                    actual_source=source, current_source=current), protected)
                self.assertFalse(measurement_reuse_matches(prepared, expected, same_session_result=True))
                other = root / 'other-source'
                other.mkdir()
                self.assertFalse(measurement_reuse_matches(prepared, expected, same_session_result=True,
                    actual_source=source, current_source={'source_project': str(other)}))
                # Reference ownership remains in the application, ahead of a permissive domain stub.
                prior_preparation = app.controller.manifest.state_refs.get('preparation')
                app.controller.manifest.state_refs['preparation'] = ArtifactRef(
                    'inputs/unowned.json', kind='prepared_execution')
                reader = app.controller.store.read_json
                def read_result(ref):
                    return prepared if ref == baseline_ref else reader(ref)
                try:
                    with patch.object(app.controller.store, 'read_json', side_effect=read_result), patch(
                            'simple_ar.app.research_application.measurement_reuse_matches', return_value=True) as shared:
                        self.assertFalse(app._measurement_ref_matches(baseline_ref, execution))
                        shared.assert_not_called()
                finally:
                    if prior_preparation is None:
                        app.controller.manifest.state_refs.pop('preparation', None)
                    else:
                        app.controller.manifest.state_refs['preparation'] = prior_preparation
                self.assertEqual(payload, retained)
                self.assertEqual(app.budget_ledger.to_dict(), ledger)
                if not protected:
                    self.assertTrue(app._measurement_ref_matches(baseline_ref, execution))
                    continue
                self.assertEqual((root / "calls.txt").read_text(encoding="utf-8").splitlines(), ["baseline"])
                original_evaluator = script.read_text(encoding="utf-8")
                script.write_text(original_evaluator + "\n# protected evaluator changed\n", encoding="utf-8")
                self.assertFalse(app._measurement_ref_matches(baseline_ref, execution))
                script.write_text(original_evaluator, encoding="utf-8")
                self.assertTrue(app._measurement_ref_matches(baseline_ref, execution))
                reuse_execution = {
                    **execution, "baseline_policy": "reuse", "baseline_ref": baseline_ref.path,
                }
                revised = replace(app.brief, request_text="Measure the fixture again without repeating a valid baseline.")
                app.controller.pause("Stop at the baseline before accepting a plan revision.")
                app.continue_session(
                    reason="Reuse the passed same-condition baseline.", revised_brief=revised,
                    revised_execution=reuse_execution,
                )
                finished = app.advance(max_actions=20)
                self.assertEqual(finished.status, "completed", finished.status_reason)
                self.assertEqual(finished.work_plan["execution_decision"]["baseline"]["mode"], "reuse")
                self.assertEqual((root / "calls.txt").read_text(encoding="utf-8").splitlines(), ["baseline", "candidate"])
                candidate_step = next(step for step in app._load_task_plan().steps if step.action == "experiment")
                self.assertTrue(app._step_completed(candidate_step))
                script.write_text(original_evaluator + "\n# protected evaluator changed after measurement\n", encoding="utf-8")
                self.assertTrue(app._step_completed(candidate_step))
                completed_services = app.services
                app.services = replace(app.services, config={
                    **app.services.config,
                    "execution": {**reuse_execution, "command": [sys.executable, str(script), "candidate-v2"]},
                })
                self.assertTrue(app._step_completed(candidate_step))
                app.services = completed_services
                script.write_text(original_evaluator, encoding="utf-8")
                self.assertTrue(app._step_completed(candidate_step))

                mismatched_protocol = {
                    **execution["protocol"],
                    "dataset_refs": [{"asset_id": "different", "revision": "v2"}],
                }
                mismatched_execution = {
                    **reuse_execution,
                    "protocol": mismatched_protocol,
                }
                app.continue_session(
                    reason="Check that a changed protocol cannot reuse the old baseline.",
                    revised_brief=replace(app.brief, request_text="Measure the fixture under a changed dataset reference."),
                    revised_execution=mismatched_execution,
                )
                self.assertNotIn("baseline", app.controller.manifest.state_refs)
                self.assertNotIn("experiment", app.controller.manifest.state_refs)
                rejected = app.advance(max_actions=20)
                self.assertEqual(rejected.status, "paused")
                self.assertIn("Baseline reuse requested", rejected.status_reason)
                self.assertEqual((root / "calls.txt").read_text(encoding="utf-8").splitlines(), ["baseline", "candidate"])


    def test_revised_code_root_drops_old_preparation_before_execution_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_project = root / "old-project"
            new_project = root / "new-project"
            old_data = root / "old-data.csv"
            new_data = root / "new-data.csv"
            old_project.mkdir()
            new_project.mkdir()
            old_data.write_text("x,label\n1,a\n", encoding="utf-8")
            new_data.write_text("x,label\n2,b\n", encoding="utf-8")
            old_execution = {
                "command": [sys.executable, "-c", "print('accuracy: 0.5')"],
                "cwd": str(old_project), "timeout_sec": 5,
                "result_schema": {"primary_metric": "accuracy"},
                "code_task": {"code_root": str(old_project), "allowed_patterns": ["*.py"]},
            }
            app = create_session(
                ResearchBrief(
                    request_text="Inspect this bounded project.", requested_outputs=("experiments",),
                    asset_requests=({"locator": str(old_data), "kind": "dataset", "role": "dataset"},),
                ),
                root=root / "session",
                services=ResearchApplicationServices(config={"execution": old_execution}),
            )
            app.controller.pause("Seed an existing prepared workspace for revision coverage.")
            preparation = app.controller.store.write_json(
                "inputs/prepared_execution.json",
                {"execution": old_execution, "source_project": str(old_project), "workspace": str(old_project)},
                kind="prepared_execution", schema="prepared_execution.v1", producer="test",
            )
            app.controller.manifest.state_refs["preparation"] = preparation
            app.controller.save()

            app.continue_session(
                reason="Use the explicitly revised data asset.",
                revised_brief=replace(
                    app.brief,
                    asset_requests=({"locator": str(new_data), "kind": "dataset", "role": "dataset"},),
                ),
            )
            self.assertNotIn("preparation", app.controller.manifest.state_refs)
            self.assertEqual(app.brief.asset_requests[0]["locator"], str(new_data))
            self.assertTrue(app.controller.store.exists(preparation))

            app.controller.pause("Stop before accepting the project-root revision.")
            preparation = app.controller.store.write_json(
                "inputs/prepared_execution_revision.json",
                {"execution": old_execution, "source_project": str(old_project), "workspace": str(old_project)},
                kind="prepared_execution", schema="prepared_execution.v1", producer="test",
            )
            app.controller.manifest.state_refs["preparation"] = preparation
            app.controller.save()
            new_execution = {
                **old_execution,
                "cwd": str(new_project),
                "code_task": {**old_execution["code_task"], "code_root": str(new_project)},
            }
            app.continue_session(
                reason="Use the explicitly revised project root.", revised_execution=new_execution,
            )

            self.assertNotIn("preparation", app.controller.manifest.state_refs)
            resolved = app._effective_config()["execution"]
            self.assertEqual(Path(resolved["code_task"]["code_root"]).resolve(), new_project.resolve())
            self.assertEqual(Path(resolved["cwd"]).resolve(), new_project.resolve())
            self.assertTrue(app.controller.store.exists(preparation))

    def test_code_task_input_revision_retires_prepared_run_not_history_or_budget(self):
        for change in ("goal", "constraint", "edit_scope", "preference"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                project = root / "project"
                project.mkdir()
                execution = {"command": [sys.executable, "-c", "print('ok')"],
                    "cwd": str(project), "timeout_sec": 5,
                    "code_task": {"code_root": str(project), "allowed_patterns": ["module.py"]}}
                app = create_session(ResearchBrief(request_text="Fix the supplied public issue.",
                    objective="Fix the supplied public issue.", requested_outputs=("bug_fix",)),
                    root=root / "session", services=ResearchApplicationServices(
                        config={"research_task_kind": "bug_fix", "execution": execution},
                        budget_limits={"llm_requests": 9}))
                app.controller.pause("Seed the existing task-bound preparation.")
                prepared = app.controller.store.write_json("inputs/prepared_execution.json",
                    {"execution": {**execution, "cwd": str(root / "prepared_workspace"),
                        "code_task": {"run_dir": str(root / "prepared_run")}},
                        "workspace": str(root / "prepared_workspace")},
                    kind="prepared_execution", schema="prepared_execution.v1", producer="test")
                app.controller.manifest.state_refs["preparation"] = prepared
                app._persist_inputs(())
                app = load_session(root / "session")
                self.assertEqual(app.services.config["execution"], execution)
                original = app.controller.store.read_json(prepared)
                if change == "edit_scope":
                    app.continue_session(revised_execution={**execution,
                        "code_task": {**execution["code_task"], "allowed_patterns": ["*.py"]}})
                else:
                    revision = {"goal": {"objective": "Also preserve inherited behavior."},
                        "constraint": {"hard_constraints": ("Keep inherited behavior.",)},
                        "preference": {"preferences": ("Keep the status concise.",)}}[change]
                    app.continue_session(revised_brief=replace(app.brief, **revision))
                self.assertEqual("preparation" in app.view().state_refs, change == "preference")
                self.assertEqual(app.controller.store.read_json(prepared), original)
                self.assertEqual(app.budget_ledger.remaining("llm_requests"), 9)
                self.assertFalse(app.budget_ledger.entries)
                restored = load_session(root / "session")
                self.assertEqual("preparation" in restored.view().state_refs, change == "preference")

    def test_execution_revision_reuses_matching_baseline_and_remeasures_changed_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = root / "paper.md"
            paper.write_text("# Fixed comparison\nA small fixture has an explicit held-out condition.\n", encoding="utf-8")
            data = root / "data.csv"
            data.write_text("x,label\n1,a\n", encoding="utf-8")
            script = root / "measure.py"
            script.write_text(
                "import sys\nfrom pathlib import Path\n"
                "with Path('calls.txt').open('a') as f: f.write(sys.argv[1] + '\\n')\n"
                "print('accuracy: 0.6' if sys.argv[1] == 'baseline' else 'accuracy: 0.7')\n",
                encoding="utf-8",
            )
            protocol = {
                "contract_id": "resume-condition-v1",
                "dataset_refs": [{"asset_id": "fixture", "revision": "v1"}],
                "split_spec": {"name": "heldout"},
                "metric_specs": [{"name": "accuracy", "unit": "fraction"}],
                "comparison_conditions": {"method": "fixed"},
                "protected_assets": [
                    {"asset_id": "data", "path": str(data)},
                    {"asset_id": "evaluator", "path": str(script)},
                ],
            }
            execution = {
                "command": [sys.executable, str(script), "candidate-v1"],
                "baseline": {"command": [sys.executable, str(script), "baseline"]},
                "baseline_policy": "run", "cwd": str(root), "timeout_sec": 5,
                "result_schema": {"primary_metric": "accuracy", "direction": "higher"},
                "protocol": protocol,
            }
            app = create_session(
                ResearchBrief(
                    request_text="Compare the fixed CPU fixture under its held-out condition.",
                    requested_outputs=("experiments",),
                    asset_requests=(
                        {"locator": str(paper), "role": "paper"},
                        {"asset_id": "fixture", "locator": str(data), "kind": "dataset", "role": "dataset"},
                    ),
                ),
                root=root / "session",
                services=ResearchApplicationServices(
                    max_results=1, max_chunks=10, max_attempts=32,
                    config={"research_materials_only": True, "execution": execution},
                    budget_limits={"process_invocations": 8, "process_wall_seconds": 80},
                ),
            )
            first = app.advance(max_actions=32)
            self.assertEqual(first.status, "completed", first.status_reason)
            original_baseline = first.state_refs["baseline"]
            original_candidate = first.state_refs["experiment"]
            original_attempt_count = len(first.attempts)
            calls = root / "calls.txt"
            self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), ["baseline", "candidate-v1"])

            revised_execution = {
                **execution,
                "command": [sys.executable, str(script), "candidate-v2"],
            }
            app.continue_session(
                reason="Revise only the candidate invocation; retain the matching control.",
                revised_execution=revised_execution,
            )
            second = app.advance(max_actions=32)
            self.assertEqual(second.status, "completed", second.status_reason)
            self.assertEqual(second.state_refs["baseline"], original_baseline)
            self.assertNotEqual(second.state_refs["experiment"], original_candidate)
            self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), [
                "baseline", "candidate-v1", "candidate-v2",
            ])

            app.continue_session(reason="Recheck unchanged inputs.", revised_brief=replace(app.brief))
            unchanged = app.advance(max_actions=32)
            self.assertEqual(unchanged.status, "completed", unchanged.status_reason)
            self.assertEqual(unchanged.state_refs["baseline"], second.state_refs["baseline"])
            self.assertEqual(unchanged.state_refs["experiment"], second.state_refs["experiment"])
            self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), [
                "baseline", "candidate-v1", "candidate-v2",
            ])

            data.write_text("x,label\n2,b\n", encoding="utf-8")
            app.continue_session(
                reason="The protected data changed; retain history but refresh its comparison.",
                revised_brief=replace(app.brief),
            )
            third = app.advance(max_actions=32)
            self.assertEqual(third.status, "completed", third.status_reason)
            self.assertNotEqual(third.state_refs["baseline"], second.state_refs["baseline"])
            self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), [
                "baseline", "candidate-v1", "candidate-v2", "baseline", "candidate-v2",
            ])
            self.assertGreater(len(third.attempts), original_attempt_count)
            self.assertTrue(app.controller.store.exists(original_baseline))
            attempts = third.attempts
            resumed = load_session(root / "session")
            final = resumed.advance(max_actions=32)
            self.assertEqual(final.attempts, attempts)
            self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), [
                "baseline", "candidate-v1", "candidate-v2", "baseline", "candidate-v2",
            ])

    def test_literature_report_does_not_require_or_launch_experiments(self):
        from simple_ar.report.schema import AgentReportResult, ReportSectionDraft

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, paper = self._paper_session(
                root,
                "# Calibration\n\nCalibration compares confidence with accuracy. Evidence remains limited.",
                request_text="Review calibration evidence.",
                requested_outputs=("report",),
                services=ResearchApplicationServices(max_results=1,
                budget_limits={"process_invocations": 0}),
            )
            self.advance_to(app, "report_write")
            view = app.view()
            self.assertEqual(view.next_action, "report_write")
            self.assertNotIn("synthesis", view.state_refs)
            self.assertIn("read", view.state_refs)
            app.services = replace(app.services, llm_client=LLMClient(LLMSettings(api_key="fixture")))

            def writer(**kwargs):
                context = kwargs["context"]
                self.assertEqual(context.report_mode, "research_only")
                self.assertEqual(context.results, {})
                self.assertEqual(context.metric_sources, [])
                self.assertEqual(kwargs["template"].name, "source_review")
                self.assertIn("No experiment", context.evidence_summary)
                citation = context.papers[0]["id"]
                self.assertEqual(context.citation_key_map, {"P1": citation})
                self.assertEqual(next(h for h in context.source_handles if h.kind == "paper").citation_key, "P1")
                self.assertEqual(kwargs["memory"].source_handles, context.source_handles)
                return AgentReportResult(report_body="", memory=kwargs["memory"], used_agent=True,
                    sections=[ReportSectionDraft(section_id="review", heading="Evidence and Limitations",
                        draft_markdown=f"Prior work discusses calibration [@{citation}]. No experiment was conducted.",
                        used_sources=[f"paper:{citation}"])])

            with patch("simple_ar.report.writing.run_report_agent", side_effect=writer):
                view = app.advance(max_actions=3)
            self.assertEqual(view.status, "completed", view.status_reason)
            self.assertIn("report_audit", view.state_refs)
            self.assertNotIn("experiment", view.state_refs)
            self.assertNotIn("design", view.state_refs)
            self.assertEqual(app.budget_ledger.remaining("process_invocations"), 0)
            self.assertFalse(any("process_invocations" in entry.actual for entry in app.budget_ledger.entries))
            report = app.controller.store.read_text(view.state_refs["report"])
            self.assertIn("No experiment was conducted", report)
            self.assertIn("References", report)

    def test_report_audit_warning_is_deliverable_but_failed_audit_is_not(self):
        from simple_ar.report.schema import AgentReportResult, ReportSectionDraft, ReviewerFinding

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, paper = self._paper_session(
                root,
                "# Calibration\n\nCalibration evidence is limited.\n",
                request_text="Review calibration evidence.",
                requested_outputs=("report",),
                services=ResearchApplicationServices(max_results=1),
            )
            self.advance_to(app, "report_write")
            app.services = replace(app.services, llm_client=LLMClient(LLMSettings(api_key="fixture")))

            def writer(**kwargs):
                paper_id = kwargs["context"].papers[0]["id"]
                memory = kwargs["memory"].model_copy(update={"reviewer_findings": [ReviewerFinding(
                    finding_id="warning-1", type="style", severity="major", message="Needs a bounded qualification.") ]})
                return AgentReportResult(report_body="", memory=memory, used_agent=True, sections=[
                    ReportSectionDraft(section_id="review", heading="Evidence",
                                       draft_markdown=f"Calibration evidence is limited [@{paper_id}].",
                                       used_sources=[paper_id])])

            with patch("simple_ar.report.writing.run_report_agent", side_effect=writer):
                view = app.advance(max_actions=3)
            audit = app.controller.store.read_json(view.state_refs["report_audit"])
            self.assertEqual(audit["status"], "warning")
            old_report = view.state_refs["report"]
            old_body = app.controller.store.read_text(old_report)
            old_read = view.state_refs["read"]
            attempts_before_revision = len(view.attempts)
            refreshed = app.continue_session(
                revised_brief=replace(
                    app.brief,
                    preferences=("Keep the report concise.",),
                ),
            )
            self.assertEqual(refreshed.next_action, "report_write")
            self.assertEqual(refreshed.state_refs["read"], old_read)
            self.assertNotIn("report", refreshed.state_refs)
            self.assertEqual(len(refreshed.attempts), attempts_before_revision)
            with patch("simple_ar.report.writing.run_report_agent", side_effect=writer):
                view = app.advance(max_actions=3)
            self.assertNotEqual(view.state_refs["report"], old_report)
            self.assertEqual(app.controller.store.read_text(old_report), old_body)

        self.assertEqual(view.status, "completed", view.status_reason)

    def test_overbroad_report_candidate_remains_inspectable_before_delivery(self):
        from simple_ar.report.schema import AgentReportResult, ReportSectionDraft

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            papers = []
            for index in (1, 2):
                paper = root / f"source-{index}.md"
                paper.write_text(f"# Calibration source {index}\n\nCalibration evidence for comparison.\n", encoding="utf-8")
                papers.append({"locator": str(paper), "role": "paper"})
            app = create_session(
                ResearchBrief(request_text="Compare calibration evidence in the supplied studies, citing at most one.",
                              requested_outputs=("report",), asset_requests=tuple(papers)),
                root=root / "session",
                services=ResearchApplicationServices(max_results=2,
                    config={"report": {"max_cited_sources": 1}}),
            )
            # Simulate a persisted/legacy selection that contains more papers
            # than a newer delivery bound; the final gate must still enforce it.
            with patch.object(app, "_search_limit", return_value=2):
                self.advance_to(app, "report_write")
            app.services = replace(app.services, llm_client=LLMClient(LLMSettings(api_key="fixture")))

            def overbroad_writer(**kwargs):
                ids = [paper["id"] for paper in kwargs["context"].papers]
                self.assertEqual(len(ids), 2)
                return AgentReportResult(report_body="", memory=kwargs["memory"], used_agent=True,
                    sections=[ReportSectionDraft(section_id="review", heading="Evidence",
                        draft_markdown=f"Two prior works are noted [@{ids[0]}] [@{ids[1]}].")])

            with patch("simple_ar.report.writing.run_report_agent", side_effect=overbroad_writer):
                view = app.advance(max_actions=3)
            self.assertEqual(view.status, "paused")
            self.assertNotIn('report', view.state_refs)
            self.assertIn('max_cited_sources', view.status_reason)
            self.assertIn('writer', view.state_refs)
            saved_writer = app.controller.store.read_json(view.state_refs['writer'])
            self.assertIn('Two prior works', saved_writer['sections'][0]['draft_markdown'])
            # More capacity retains the rejected candidate and does not itself
            # execute another call or override the delivery constraint.
            refs = dict(view.state_refs)
            attempt_count = len(view.attempts)
            authorized = app.continue_session(authorization_id="saved-report-check",
                authorization_reason="Inspect the saved candidate separately.",
                authorize_remaining={"total_tokens": 10000, "llm_requests": 1}, additional_attempts=3)
            self.assertEqual(authorized.status, "running")
            self.assertEqual(authorized.state_refs, refs)
            self.assertEqual(len(authorized.attempts), attempt_count)
            with self.assertRaisesRegex(ResearchApplicationError, 'current running'):
                app.continue_session(authorization_id="saved-report-check",
                    authorization_reason="Inspect the saved candidate separately.",
                    authorize_remaining={"total_tokens": 10000, "llm_requests": 1}, additional_attempts=3)
            self.assertEqual(len(app.controller.list_attempts()), attempt_count)

    def test_request_report_reopens_completed_prefix_without_rerunning_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "evaluate.py"
            script.write_text("print('accuracy: 0.75')\n", encoding="utf-8")
            app, paper = self._paper_session(
                root,
                "# Classification\n\nThe fixture describes an accuracy metric.\n",
                request_text="Evaluate the fixture.",
                requested_outputs=("experiments",),
                services=ResearchApplicationServices(
                    max_results=1,
                    config={
                        "execution": {
                            "command": [sys.executable, str(script)],
                            "cwd": str(root),
                            "timeout_sec": 5,
                            "result_schema": {"primary_metric": "accuracy"},
                        }
                    },
                    budget_limits={"process_invocations": 1, "process_wall_seconds": 10},
                ),
            )
            completed = app.advance(max_actions=20)
            self.assertEqual(completed.status, "completed", completed.status_reason)
            analysis_ref = completed.state_refs["analysis"]
            measurement_ref = app.latest_experiment_ref()
            remaining_processes = app.budget_ledger.remaining("process_invocations")
            refreshed = app.request_reanalysis()
            self.assertEqual(refreshed.next_action, "analysis")
            self.assertEqual(refreshed.requested_outputs, ("experiments",))
            app = load_session(root / "session")
            refreshed = app.advance(max_actions=1)
            self.assertEqual(refreshed.status, "completed", refreshed.status_reason)
            self.assertNotEqual(refreshed.state_refs["analysis"], analysis_ref)
            self.assertTrue(app.controller.store.exists(analysis_ref))
            self.assertEqual(app.latest_experiment_ref(), measurement_ref)
            self.assertEqual(app.budget_ledger.remaining("process_invocations"), remaining_processes)
            self.assertEqual(sum(a["capability"] == "experiment" for a in refreshed.attempts), 1)
            analysis_ref = refreshed.state_refs["analysis"]
            completed = refreshed
            attempt_count = len(completed.attempts)

            reopened = app.request_report(
                refresh=True,
                report_config={"template": "experiment", "reviewer": "disabled"},
            )

            self.assertEqual(reopened.status, "running")
            self.assertEqual(reopened.next_action, "report_write")
            self.assertEqual(reopened.state_refs["analysis"], analysis_ref)
            self.assertEqual(len(reopened.attempts), attempt_count)
            self.assertEqual(sum(item["capability"] == "experiment" for item in reopened.attempts), 1)
            runtime = app.controller.store.read_json(reopened.state_refs["runtime_config"])
            self.assertEqual(runtime["config"]["report"]["reviewer"], "disabled")
            self.assertEqual(runtime["config"]["report"]["template"], "experiment")
            self.assertEqual(app.controller.manifest.revision, 2)
            self.assertEqual(app.brief.revision, 2)
            self.assertIn("report", app.brief.requested_outputs)

    def test_brief_revision_reuses_unaffected_source_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, paper = self._paper_session(
                root,
                "# Replay\n\nA small memory retains earlier examples.\n",
                request_text="Study replay methods.",
                requested_outputs=("summary",),
                services=ResearchApplicationServices(
                    max_results=1, max_chunks=10, max_attempts=16,
                    config={"research_materials_only": True},
                ),
            )
            original = app.advance(max_actions=12)
            self.assertEqual(original.status, "completed", original.status_reason)
            retained = {name: original.state_refs[name] for name in ("documents", "read")}
            app.continue_session(
                revised_brief=replace(app.brief, preferences=("Keep the summary concise.",)),
            )
            self.assertNotIn("synthesis", app.controller.manifest.state_refs)
            self.assertNotIn("summary", app.controller.manifest.state_refs)
            for name, ref in retained.items():
                self.assertEqual(app.controller.manifest.state_refs[name], ref)
                self.assertTrue(app.controller.store.exists(ref))

            revised = app.advance(max_actions=12)
            self.assertEqual(revised.status, "completed", revised.status_reason)
            for name, ref in retained.items():
                self.assertEqual(revised.state_refs[name], ref)
            self.assertEqual(sum(item["capability"] == "document_ingest" for item in revised.attempts), 1)
            self.assertEqual(sum(item["capability"] == "read" for item in revised.attempts), 1)

    def test_csv_baseline_preparation_trains_and_scores_real_labels(self):
        for swapped in (False, True):
            with self.subTest(swapped=swapped), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paper = root / "paper.md"
                paper.write_text("# Text classification baseline\n\nText classification baseline uses word counts and logistic regression.\n", encoding="utf-8")
                data = root / "reviews.csv"
                labels = ("negative", "positive") if swapped else ("positive", "negative")
                data.write_text("text,label,split\nbright sunny good,positive,train\nbright good,positive,train\n"
                                "dark rainy bad,negative,train\ndark bad,negative,train\n"
                                f"bright sunny good,{labels[0]},eval\ndark rainy bad,{labels[1]},eval\n", encoding="utf-8")
                app = create_session(ResearchBrief(
                    request_text="Text classification baseline", requested_outputs=("experiments", "report") if not swapped else ("experiments",),
                    asset_requests=({"locator": str(paper), "role": "paper"}, {"locator": str(data), "role": "dataset"}),
                ), root=root / "session", services=ResearchApplicationServices(max_results=1,
                    config={"execution": {"dataset": str(data), "timeout_sec": 10}},
                    budget_limits={"process_invocations": 1, "process_wall_seconds": 10}))
                self.advance_to(app, "experiment")
                prepared = app.view()
                self.assertIn("preparation", prepared.state_refs, prepared.status_reason)
                self.assertNotIn("experiment", prepared.state_refs)
                self.assertEqual(app.budget_ledger.remaining("process_invocations"), 1)
                app = load_session(root / "session")
                view = app.advance(max_actions=2)
                if not swapped:
                    from simple_ar.report.schema import AgentReportResult, ReportSectionDraft
                    from simple_ar.core.artifacts import read_json
                    self.assertEqual(view.next_action, "report_write")
                    app.services = replace(app.services, llm_client=LLMClient(LLMSettings(api_key="test-key", api_mode="chat")))
                    saved_sections = {"sections": [{"section_id": "abstract"}]}
                    def interrupted_writer(**kwargs):
                        kwargs["checkpoint_sink"](saved_sections)
                        return None
                    with patch("simple_ar.report.writing.run_report_agent", side_effect=interrupted_writer):
                        failed_write = app.advance()
                    self.assertEqual(failed_write.status, "paused")
                    self.assertNotIn("report", failed_write.state_refs)
                    failed_attempt = app.controller.manifest.current_attempt
                    self.assertTrue((app.controller.store.root / "attempts" / failed_attempt / "report_inputs.json").is_file())
                    app.continue_session(reason="Retry the failed writer without rerunning research.")
                    def writer(**kwargs):
                        self.assertEqual(kwargs["completed_checkpoint"], saved_sections)
                        attempt_id = app.controller.manifest.current_attempt
                        attempt = app.controller.store.root / "attempts" / attempt_id
                        self.assertTrue((attempt / "report_inputs.json").is_file())
                        self.assertEqual(app.controller.manifest.status, "running")
                        snapshot = read_json(attempt / "report_inputs.json")
                        self.assertTrue(snapshot["snapshot_id"])
                        kwargs["client"].ask_json("Fixture writer", "Return an empty JSON object.", label="report-test")
                        paper_id = kwargs["context"].papers[0]["id"]
                        return AgentReportResult(report_body="", memory=kwargs["memory"], used_agent=True,
                            sections=[ReportSectionDraft(section_id=str(i), heading=heading,
                                draft_markdown=f"Exploratory local text baseline evaluation [@{paper_id}].",
                                used_sources=[paper_id]) for i, heading in enumerate((
                                "Abstract", "Introduction", "Related Work", "Method", "Experimental Setup", "Results", "Limitations", "Conclusion"))])
                    response = {"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}
                    with patch("simple_ar.report.writing.run_report_agent", side_effect=writer), patch("simple_ar.integrations.llm._call_openai_sdk", return_value=response):
                        with patch.object(app, "_persist_application_views", side_effect=RuntimeError("interrupted writer")):
                            with self.assertRaisesRegex(RuntimeError, "interrupted writer"):
                                app.advance()
                    app = load_session(root / "session")
                    with patch("simple_ar.report.writing.run_report_agent", side_effect=AssertionError("Do not rewrite completed paper")):
                        view = app.advance(max_actions=2)
                    self.assertIn("report_audit", view.state_refs, view.status_reason)
                    writer_ids = {item["attempt_id"] for item in view.attempts if item["capability"] == "report_write"}
                    llm_entries = [entry for entry in app.budget_ledger.entries if "llm_requests" in entry.actual]
                    self.assertTrue(llm_entries)
                    self.assertTrue(all(entry.attempt_id in writer_ids for entry in llm_entries))
                    report = app.controller.store.read_text(view.state_refs["report"])
                    self.assertIn("Abstract", report)
                    self.assertIn("References", report)
                self.assertEqual(view.status, "completed", view.status_reason)
                result = app.controller.store.read_json(app.latest_experiment_ref())
                self.assertEqual(result["execution_status"], "passed")
                self.assertEqual(result["metrics"]["accuracy"], 0.0 if swapped else 1.0)
                self.assertEqual(result["metrics"]["train_examples"], 4)
                self.assertEqual(result["metrics"]["eval_examples"], 2)
                self.assertEqual(result["measurement"]["asset_integrity"]["status"], "observed_unchanged")
                prep = app.controller.store.read_json(view.state_refs["preparation"])
                self.assertTrue(any("possible leakage" in text for text in prep["limitations"]))
                self.assertEqual(result["preparation"]["source_ref"], view.state_refs["preparation"].to_dict())
                self.assertEqual(result["preparation"]["limitations"], prep["limitations"])
                analysis = app.controller.store.read_json(view.state_refs["analysis"])["analysis"]
                for limitation in prep["limitations"]:
                    self.assertIn(limitation, analysis["audit"]["limitations"])
                    self.assertIn(limitation, analysis["readme_markdown"])
                self.assertNotIn("implementation", view.state_refs)
                report_context, report_memory = app.report_inputs()
                self.assertEqual(report_context.results["metrics"]["accuracy"], result["metrics"]["accuracy"])
                self.assertEqual(report_context.experiment_plan, result["experiment_contract"])
                metric = next(row for row in report_context.metric_sources if row.name == "accuracy")
                self.assertEqual(metric.measurement_id, result["measurement"]["measurement_id"])
                self.assertEqual(metric.protocol_fingerprint, result["measurement"]["protocol_fingerprint"])
                self.assertEqual(metric.condition_id, "text_baseline")
                self.assertEqual(metric.source_kind, "measured")
                self.assertEqual(metric.unit, "fraction")
                self.assertEqual(metric.direction, "higher")
                for limitation in prep["limitations"]:
                    self.assertIn(limitation, report_memory.limitations)
                self.assertEqual(app.budget_ledger.remaining("process_invocations"), 0)

    def test_csv_preparation_does_not_guess_splits_or_truncate_data(self):
        from simple_ar.research.text_dataset import read_text_dataset
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.csv"
            path.write_text("text,label,split\nhello,a,train\nworld,b,train\nhello,a,test\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "no split is inferred"):
                read_text_dataset(path)
            with self.assertRaisesRegex(ValueError, "byte"):
                read_text_dataset(path, max_bytes=4)
            path.write_text("text,label,split\nhello,a,train\nworld,b,train\nhello,a,eval\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "row preparation limit"):
                read_text_dataset(path, max_rows=2)

    def test_paired_experiment_recovers_baseline_and_finishes_negative_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evaluator = root / "evaluate.py"
            evaluator.write_text(
                "import sys\nfrom pathlib import Path\n"
                "condition = sys.argv[1]\n"
                "with Path('runs.txt').open('a') as log: log.write(condition + '\\n')\n"
                "data = [(0, 0), (1, 1), (2, 1), (3, 0)]\n"
                "predictions = [int(x > 0) if condition == 'baseline' else 0 for x, y in data]\n"
                "accuracy = sum(p == y for p, (x, y) in zip(predictions, data)) / len(data)\n"
                "print('accuracy:', accuracy)\n", encoding="utf-8",
            )
            execution = {
                "command": [sys.executable, str(evaluator), "candidate"],
                "baseline": {"command": [sys.executable, str(evaluator), "baseline"]},
                "cwd": str(root), "timeout_sec": 5,
                "result_schema": {"primary_metric": "accuracy", "direction": "higher", "required_metrics": ["accuracy"]},
                "protocol": {"contract_id": "paired-fixture", "hypothesis": "Check a threshold change.",
                             "dataset_refs": [{"asset_id": "four-examples", "revision": "1"}],
                             "split_spec": {"held_out": [0, 1, 2, 3]},
                             "metric_specs": [{"name": "accuracy", "unit": "fraction", "direction": "higher"}],
                             "comparison_conditions": {"seed": 0},
                             "protected_assets": [{"asset_id": "evaluator", "path": str(evaluator)}]},
            }
            app, paper = self._paper_session(
                root,
                "# Classification\n\nEvaluate threshold classifiers on held-out data.\n",
                request_text="Compare threshold classifiers.",
                requested_outputs=("experiments",),
                services=ResearchApplicationServices(
                max_results=1, config={"execution": execution},
                budget_limits={"process_invocations": 2, "process_wall_seconds": 10},
            ),
            )
            self.advance_to(app, "baseline")
            with patch.object(app, "_persist_application_views", side_effect=RuntimeError("interrupted baseline")):
                with self.assertRaisesRegex(RuntimeError, "interrupted baseline"):
                    app.advance()
            app = load_session(root / "session")
            measured = app.advance()
            self.assertIn("baseline", measured.state_refs)
            self.assertIn("experiment", measured.state_refs)
            self.assertEqual(measured.next_action, "analysis")
            app = load_session(root / "session")
            final = app.advance()
            self.assertEqual(final.status, "completed")
            comparison = app.controller.store.read_json(final.state_refs["comparison"])
            self.assertEqual(comparison["comparability"], "declared_match")
            baseline = app.controller.store.read_json(final.state_refs["baseline"])
            self.assertEqual(baseline["measurement"]["asset_integrity"]["status"], "observed_unchanged")
            self.assertEqual(comparison["verdict"], "regressed")
            self.assertEqual(comparison["deltas"]["accuracy"], -0.25)
            analysis = app.controller.store.read_json(final.state_refs["analysis"])
            self.assertEqual(analysis["analysis"]["status"], "metric_below_target")
            decision = app.controller.store.read_json(final.state_refs["decision"])
            self.assertEqual(decision["action"], "stop")
            # A deterministic analysis cannot choose a scientific revision;
            # the old verdict branch supplied this option without evidence.
            self.assertEqual(decision["continuation_options"], [])
            self.assertIn("Deterministic", decision["decision_reason"])
            app.advance(max_actions=20)
            self.assertEqual((root / "runs.txt").read_text().splitlines(), ["baseline", "candidate"])
            self.assertEqual(app.budget_ledger.remaining("process_invocations"), 0)

    def test_experiment_budget_exhaustion_preserves_research_without_launch(self):
        for limits in ({"process_invocations": 0, "process_wall_seconds": 5},
                       {"process_invocations": 1}, {"process_wall_seconds": 5}):
            with self.subTest(limits=limits), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                app, _ = self._paper_session(root, "# Evaluation\n\nValidation measures accuracy.\n",
                    request_text="Evaluate validation.", requested_outputs=("experiment", "research_summary"),
                    services=ResearchApplicationServices(max_results=1, config={"execution": {
                        "command": [sys.executable, "-c", "from pathlib import Path; Path('ran').touch()"],
                        "cwd": str(root), "timeout_sec": 5}}, budget_limits=limits))
                view = app.advance(max_actions=10)
                self.assertEqual(view.status, "paused")
                self.assertIn("summary", view.state_refs)
                self.assertNotIn("experiment", view.state_refs)
                self.assertFalse((root / "ran").exists())
                self.assertEqual(len(app.budget_ledger.entries), 0)

    def test_explicit_retry_reuses_research_and_reanalyzes_corrected_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            services = ResearchApplicationServices(max_results=1, config={
                "execution": {
                    "command": [sys.executable, "-c", "print('accuracy: 0.2'); raise SystemExit(1)"],
                    "cwd": str(root), "timeout_sec": 5,
                    "result_schema": {"primary_metric": "accuracy", "required_metrics": ["accuracy"]},
                    "protocol": {
                        "contract_id": "retry-fixture", "hypothesis": "Fix a command failure.",
                        "dataset_refs": [{"asset_id": "fixture", "revision": "1"}],
                        "split_spec": {"evaluation": "all"},
                        "metric_specs": [{"name": "accuracy", "unit": "fraction"}],
                        "comparison_conditions": {"seed": 0},
                    },
                },
            }, budget_limits={"process_invocations": 2, "process_wall_seconds": 10})
            app, paper = self._paper_session(
                root,
                "# Evaluation\n\nMeasure classifier accuracy.\n",
                request_text="Evaluate classifier accuracy.",
                requested_outputs=("experiment",),
                services=services,
            )
            self.advance_to(app, "experiment")
            failed = app.advance()
            self.assertEqual(failed.next_action, "analysis")
            observation = app.controller.store.read_json(failed.state_refs["experiment"])
            self.assertEqual((observation["returncode"], observation["metrics"]["accuracy"]), (1, 0.2))
            first_attempt = next(item["attempt_id"] for item in failed.attempts if item["capability"] == "experiment")
            synthesis_ref = failed.state_refs["synthesis"]

            retried = app.retry_experiment(
                command=(sys.executable, "-c", "print('accuracy: 0.8')"),
                cwd=root,
                timeout_sec=5,
                parent_attempt_id=first_attempt,
            )

            self.assertEqual(retried.status, "completed")
            self.assertEqual(retried.revision, 1)
            self.assertEqual(app.brief.revision, 2)
            self.assertEqual(retried.state_refs["synthesis"], synthesis_ref)
            self.assertEqual(
                [item["capability"] for item in retried.attempts].count("experiment"), 2
            )
            retry_attempt = next(
                item for item in retried.attempts
                if item["capability"] == "experiment" and item["attempt_id"] != first_attempt
            )
            self.assertEqual(retry_attempt["parent_attempt"], first_attempt)
            result = app.controller.store.read_json(retried.state_refs["experiment"])
            self.assertEqual(result["execution_status"], "passed")
            self.assertEqual(result["metrics"]["accuracy"], 0.8)
            self.assertEqual(result["measurement"]["protocol_status"], "declared")
            self.assertEqual(result["experiment_contract"]["contract_id"], "retry-fixture")
            self.assertEqual(app.budget_ledger.remaining("process_invocations"), 0)

    def test_model_assessment_drives_design_once_and_abstention_keeps_summary(self):

        for recommend in (True, False, "provider_error"):
            with self.subTest(recommend=recommend), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paper = root / "paper.md"
                paper.write_text("# Results\n\nValidation improves accuracy for reliable agents.\n", encoding="utf-8")
                execution = {"command": [sys.executable, "evaluate.py"], "cwd": str(root), "timeout_sec": 5,
                             "protocol": {"protected_assets": [{"path": "heldout.csv"}]}}
                if recommend is not True:
                    (root / "model.py").write_text("def predict():\n    return 1\n", encoding="utf-8")
                    execution["code_task"] = {"code_root": str(root), "allowed_patterns": ["model.py"]}
                app = create_session(
                    ResearchBrief(request_text="Study validation for agents", requested_outputs=("research_design", "research_summary"),
                                  asset_requests=({"locator": str(paper), "role": "paper"},)),
                    root=root / "session", services=ResearchApplicationServices(max_results=1, config={
                        "execution": execution}),
                )
                self.advance_to(app, "assess_ideas")
                synthesis = app.controller.store.read_json(app.view().state_refs["synthesis"])
                self.assertIn("heldout.csv", synthesis["execution_context"])
                self.assertIn("Study validation for agents", synthesis["execution_context"])
                self.assertNotIn("experiment", app.view().state_refs)

                class Client:
                    calls = 0
                    selected = None
                    first_response = None
                    labels = None

                    def ask_json(self, system, user, **kwargs):
                        self.calls += 1
                        if self.labels is None:
                            self.labels = []
                        self.labels.append(kwargs.get("label"))
                        if kwargs.get("label") == "research-design":
                            return dict(selected_idea_id=self.selected,
                                        rationale="Chosen from shared evidence",
                                        execution_protocol={"comparison_required": False, "baseline_policy": "skip"})
                        if kwargs.get("label") == "research-idea-assessment-selection-review":
                            assert self.first_response is not None
                            assert "pre-experiment choice" in system
                            return self.first_response
                        if recommend == "provider_error":
                            raise LLMError("provider unavailable")
                        payload = json.loads(user)
                        assert "heldout.csv" in payload["constraints"]["research_request"]
                        self.selected = payload["candidates"][-1]["idea_id"] if recommend is True else None
                        rows = []
                        for candidate in payload["candidates"]:
                            rows.append(dict(
                                idea_id=candidate["idea_id"], relevance="Relevant", differentiation="Unknown",
                                feasibility="Small experiment", cost="One comparison", falsifiability="No gain",
                                recommendation="Check effect", supporting_evidence_refs=[payload["evidence"][0]["chunk_id"]],
                                counter_evidence_refs=[], unknowns=["Effect uncertain"],
                            ))
                        self.first_response = dict(
                            assessments=rows, recommended_idea_id=self.selected,
                            recommendation_reason="Chosen from shared evidence",
                        )
                        return self.first_response

                client = Client()
                app.services = replace(app.services, llm_client=client)
                # Synthesis permits paper IDs as motivation, while the model's
                # detailed comparison must still cite the supplied text chunks.
                read = app._load_read()
                paper_ref = read.paper_cards[0].paper_id
                self.assertNotIn(paper_ref, {chunk.chunk_id for chunk in read.bundle.chunks})
                source = app._load_synthesis()
                source = replace(source, ideas=tuple(
                    replace(idea, motivation_refs=(paper_ref,)) for idea in source.ideas
                ))
                with patch.object(app, "_load_synthesis", return_value=source):
                    view = app.advance(max_actions=2)
                assessment = app.controller.store.read_json(view.state_refs["assessment"])
                self.assertFalse(any("unresolved evidence refs" in item for item in assessment["diagnostics"]))
                self.assertEqual(client.calls, 1 if recommend == "provider_error" else 2)
                self.assertIn("summary", view.state_refs)
                if recommend is True:
                    self.assertNotIn("research-idea-assessment-selection-review", client.labels)
                    self.assertEqual(view.status, "completed")
                    design = app.controller.store.read_json(view.state_refs["design"])
                    self.assertEqual(design["selected_idea"]["idea_id"], client.selected)
                    self.assertEqual(design["selection_rationale"], "Chosen from shared evidence")
                else:
                    if recommend is False:
                        self.assertIn("research-idea-assessment-selection-review", client.labels)
                    self.assertEqual(view.status, "paused")
                    self.assertNotIn("design", view.state_refs)

    def test_llm_request_keeps_application_attempt_id_in_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = LLMClient(
                LLMSettings(
                    api_key="test-key",
                    api_mode="chat",
                    max_output_tokens=5,
                    retry_attempts=1,
                )
            )
            app = create_session(
                ResearchBrief(request_text="Study agents"),
                root=tmp,
                services=ResearchApplicationServices(llm_client=client),
            )
            request = ResearchPlanRequest(
                topic="Study agents",
                use_llm=True,
                llm_client=app.services.llm_client,
            )
            response = {"choices": [{"message": {"content": "ok"}}]}
            with patch(
                "simple_ar.integrations.llm._call_openai_sdk", return_value=response
            ):
                implementation = ImplementationRequest(Path(tmp), Path(tmp), "authorized", app.services.llm_client)
                for identity, original in (("plan-0001", request), ("implement-0002", implementation)):
                    bound = app._request_for_attempt(original, identity)
                    if isinstance(bound, ImplementationRequest):
                        self.assertIs(bound.budget_ledger, app.budget_ledger)
                        self.assertIs(bound.message_callback, app.services.message_callback)
                        self.assertEqual((bound.session_id, bound.attempt_id),
                                         (app.controller.manifest.session_id, identity))
                        self.assertIsNone(original.budget_ledger)
                    bound.llm_client.ask("system", "user", label=identity)

            ledger = BudgetLedger.load(Path(tmp) / "budget_ledger.json")
            self.assertEqual([row.attempt_id for row in ledger.entries], ["plan-0001", "implement-0002"])

    def test_continue_retries_failed_analysis_without_repeating_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, paper = self._paper_session(
                root,
                "# Evaluation\nMeasure classifier accuracy.\n",
                request_text="Measure classifier accuracy.",
                requested_outputs=("experiments",),
                services=ResearchApplicationServices(budget_limits={"process_invocations": 1, "process_wall_seconds": 5}, config={"execution": {
                    "command": [sys.executable, "-c", "print('accuracy: 0.5')"],
                    "cwd": str(root), "timeout_sec": 5,
                    "result_schema": {"primary_metric": "accuracy", "required_metrics": ["accuracy"]},
                }}),
            )
            self.advance_to(app, "analysis")
            measurement = app.view().state_refs["experiment"]
            with patch("simple_ar.research.analysis.run_result_analysis", side_effect=RuntimeError("provider budget exhausted")):
                self.assertEqual(app.advance().status, "paused")
            resumed = load_session(root / "session")
            resumed.continue_session(reason="Budget available; retry analysis only.")
            view = resumed.advance(max_actions=5)
            self.assertEqual(view.status, "completed", view.status_reason)
            self.assertEqual(view.state_refs["experiment"], measurement)
            self.assertEqual(sum(a["capability"] == "experiment" for a in view.attempts), 1)
            self.assertEqual(sum(a["capability"] == "analysis" for a in view.attempts), 2)
            context = app.controller.store.read_json(view.state_refs["analysis"])["analysis"]["decision_context"]
            failed_analysis = next(
                row for row in context["research_history"]
                if row["capability"] == "analysis" and row["status"] == "failed"
            )
            self.assertIn("provider budget exhausted", " ".join(failed_analysis["diagnostics"]))
            self.assertEqual(context["remaining_authorized_rounds"], 1)

    def test_reload_attaches_completed_plan_before_advancing_to_search(self):
        # Core owns the result/attempt/session commit-gap matrix. This adapter
        # owns attaching the accepted plan to application state after core commits.
        with tempfile.TemporaryDirectory() as tmp:
            app = create_session(ResearchBrief(request_text="Study agents"), root=tmp)
            with patch.object(app.controller, "attempt_output_ref", side_effect=RuntimeError("interrupted")):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    app.advance()
            resumed = load_session(tmp)
            with patch.object(resumed, "_run_action", return_value=False) as next_action:
                view = resumed.advance()
            next_action.assert_called_once_with("search")
            self.assertEqual(len(view.attempts), 1)
            self.assertEqual(view.budget["attempts"], 1)
            self.assertIn("plan", view.state_refs)

    def test_reload_services_preserve_limits_and_missing_config_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_session(
                ResearchBrief(request_text="Study agents"), root=tmp,
                services=ResearchApplicationServices(max_results=2, max_chunks=None, idea_limit=1),
            )
            resumed = load_session(tmp, services=ResearchApplicationServices(llm_client=object()))
            self.assertEqual(resumed.services.max_results, 2)
            self.assertIsNone(resumed.services.max_chunks)
            self.assertEqual(resumed.services.idea_limit, 1)
            (Path(tmp) / app.controller.manifest.state_refs["runtime_config"].path).unlink()
            with self.assertRaisesRegex(ResearchApplicationError, "runtime configuration"):
                load_session(tmp)

    def test_application_advances_local_research_to_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, paper = self._paper_session(
                root,
                "# Method\n\n"
                "Validation improves reliable agent behavior.\n\n"
                "# Results\n\n"
                "The AgentBench dataset reports accuracy: 0.75.\n",
                filename="reliable_agents.md",
                request_text="Study validation methods for reliable agents.",
                objective="Understand how validation affects reliable agents.",
                requested_outputs=("research_summary",),
                services=ResearchApplicationServices(max_results=2, max_chunks=20,
                    config={"execution": {
                        "command": [sys.executable, "-c", "from pathlib import Path; Path('ran').touch()"],
                        "cwd": str(root), "timeout_sec": 5,
                    }}, budget_limits={"process_invocations": 1, "process_wall_seconds": 5}),
            )

            initial = app.view()
            self.assertEqual(initial.status, "created")
            self.assertEqual(initial.next_action, "plan")
            self.assertIn("brief", initial.state_refs)
            self.assertIn("assets", initial.state_refs)
            self.assertIsNotNone(app.controller.manifest.budget_ledger_ref)
            self.assertTrue((root / "session" / "budget_ledger.json").is_file())
            self.assertEqual(initial.work_plan["next_action"], "plan")
            self.assertEqual(initial.work_plan["status"], "ready")
            self.assertNotIn("context", initial.work_plan)  # No unconsumed duplicate task/plan/budget view.
            self.assertNotIn("brief", initial.work_plan["accepted_refs"])
            self.assertNotIn("assets", initial.work_plan["accepted_refs"])
            self.assertTrue((root / "session" / "planning" / "work_plan.json").is_file())
            self.assertFalse((root / "session" / "planning" / "readiness.json").exists())
            self.assertNotIn("readiness", initial.to_dict())

            partial = app.advance(max_actions=2)
            self.assertEqual(partial.status, "running")
            self.assertEqual(partial.next_action, "document_ingest")
            self.assertEqual(partial.budget["attempts"], 2)
            app = load_session(root / "session")
            final = app.advance(max_actions=4)

            self.assertEqual(final.status, "completed")
            self.assertEqual(final.budget["attempts"], 6)
            # A configured command alone does not authorize a measurement.
            self.assertFalse((root / "ran").exists())
            self.assertEqual(app.budget_ledger.remaining("process_invocations"), 1)
            self.assertIsNone(final.next_action)
            self.assertEqual(
                [item["capability"] for item in final.attempts],
                ["document_ingest", "plan", "read", "search", "summary", "synthesize"],
            )
            self.assertTrue(
                (root / "session" / "outputs" / "research_summary.md").is_file()
            )
            summary = (
                root / "session" / "outputs" / "research_summary.md"
            ).read_text(encoding="utf-8")
            self.assertIn("## Research question", summary)
            self.assertIn("## Evidence collection", summary)
            self.assertIn("## Synthesis", summary)
            self.assertIn("summary", final.state_refs)
            self.assertIn("summary_snapshot", final.state_refs)
            self.assertEqual(final.work_plan["status"], "completed")
            self.assertEqual(final.work_plan["requested_outputs"][0]["status"], "satisfied")

            restored = load_session(root / "session")
            self.assertEqual(restored.view().status, "completed")
            restored_view = restored.advance(max_actions=1)
            self.assertEqual(
                sum(item["capability"] == "summary" for item in restored_view.attempts),
                1,
            )
            export_ref = restored.export_session()
            self.assertEqual(export_ref.kind, "session_snapshot")
            self.assertTrue((root / "session" / export_ref.path).is_file())
            restored.continue_session(revised_brief=ResearchBrief(
                request_text="Compare validation methods for reliable agents.",
                requested_outputs=("research_summary",),
                asset_requests=({"locator": str(paper), "role": "paper"},),
            ))
            revised = restored.advance()
            self.assertEqual(revised.next_action, "search")
            self.assertEqual(revised.budget["attempts"], 7)
            self.assertNotEqual(revised.state_refs["plan"], final.state_refs["plan"])

    def test_requested_experiment_preserves_summary_and_design_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, paper = self._paper_session(
                root,
                "# Results\n\nValidation reports accuracy 0.75 for reliable agents.\n",
                filename="agents.md",
                request_text="Study reliable agents.",
                requested_outputs=("experiment", "report", "research_summary"),
                services=ResearchApplicationServices(max_results=1, max_chunks=10),
            )

            view = app.advance(max_actions=9)

            self.assertEqual(view.status, "paused")
            self.assertIn("Provide execution", view.status_reason)
            self.assertEqual(
                [item["capability"] for item in view.attempts],
                ["assess_ideas", "document_ingest", "plan", "read", "research_design", "search", "summary", "synthesize"],
            )
            self.assertNotIn("experiment", {item["capability"] for item in view.attempts})
            self.assertTrue((root / "session" / "outputs" / "research_summary.md").is_file())
            assessment_ref = view.state_refs["assessment"]
            self.assertTrue((root / "session" / assessment_ref.path).is_file())
            self.assertIn("design", view.state_refs)

            self.assertEqual(view.work_plan["status"], "partial")
            outputs = {row["name"]: row for row in view.work_plan["requested_outputs"]}
            self.assertEqual(outputs["experiment"]["status"], "blocked")
            self.assertEqual(outputs["report"]["status"], "pending")

    def test_continue_retries_invalid_assessment_without_replaying_research(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, paper = self._paper_session(
                root,
                "# Results\n\nValidation reports accuracy 0.75 for reliable agents.\n",
                filename="agents.md",
                request_text="Study reliable agents.",
                requested_outputs=("experiment", "report"),
                services=ResearchApplicationServices(max_results=1),
            )
            self.advance_to(app, "research_design")
            self.assertEqual(app.view().next_action, "research_design")
            retained = {k: app.view().state_refs[k] for k in ("plan", "search", "read", "synthesis")}
            original = app.view().state_refs["assessment"]
            failed = app.controller.store.write_json(
                "invalid-assessment.json", {"generation_mode": "deterministic_fallback"},
                kind="idea_assessment", schema="idea_assessment.v1", producer="test")
            app.controller.manifest.state_refs["assessment"] = failed
            app.controller.pause("Invalid comparison")
            app.continue_session(reason="Retry comparison")
            self.assertEqual(app.view().next_action, "assess_ideas")
            app.advance(max_actions=1)
            self.assertEqual(app.view().next_action, "research_design")
            self.assertNotEqual(app.view().state_refs["assessment"], original)
            self.assertEqual(retained, {k: app.view().state_refs[k] for k in retained})
            self.assertTrue((app.controller.store.root / original.path).exists())

    def test_deterministic_plan_mode_keeps_later_llm_stages_available(self) -> None:
        class Client:
            model = "fake"

            def ask_json(self, system, user, **kwargs):
                raise AssertionError("deterministic plan mode must not invoke the planner")

        with tempfile.TemporaryDirectory() as tmp:
            app = create_session(
                ResearchBrief(request_text="Study agents"),
                root=Path(tmp),
                services=ResearchApplicationServices(
                    llm_client=Client(),
                    config={"research_plan_mode": "deterministic"},
                ),
            )
            view = app.advance(max_actions=1)

        self.assertEqual(view.status, "running")
        self.assertEqual(view.next_action, "search")
        self.assertIn("plan", view.state_refs)


if __name__ == "__main__":
    unittest.main()
