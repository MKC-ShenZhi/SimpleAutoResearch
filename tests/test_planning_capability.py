from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from simple_ar.core import CapabilityRegistry, SessionController
from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
from simple_ar.literature.models import Paper
from simple_ar.research import (
    ResearchPlanRequest,
    ResearchPlanResult,
    search_request_from_plan,
)
from simple_ar.research.sources import SearchProviderRegistry
from simple_ar.research.sources.base import SearchQuery, SearchResponse
from simple_ar.research.planning.capability import (
    build_research_plan,
    build_requested_research_plan,
    run_research_plan_capability,
)
from simple_ar.research.task_plan import TaskPlanRequest


class PlanningCapabilityTests(unittest.TestCase):

    def test_quote_backed_temporal_scope_survives_plan_and_search_recovery(self) -> None:
        topic = "Survey interval methods published from 2010 through 2014."
        scope = {"start_year": 2010, "end_year": 2014,
                 "basis_quote": "published from 2010 through 2014"}
        class Client:
            def ask_json(self, system, user, **kwargs):
                self.prompt = user
                return {"queries": ["interval methods"],
                        "questions": [{"question": "Which interval methods?", "facet": "method"}],
                        "temporal_scope": self.scope}
        client = Client()
        client.scope = scope
        request = ResearchPlanRequest(topic=topic, use_llm=True, llm_client=client,
                                      config={"research_sources": ["fixture"]})
        result = build_requested_research_plan(request)
        self.assertIn("basis_quote", client.prompt)
        self.assertEqual(result.source_plan.filters["temporal_scope"], scope)
        restored = ResearchPlanResult.from_handoff_dict(result.to_handoff_dict())
        self.assertEqual(search_request_from_plan(restored).filters["temporal_scope"], scope)
        for value in ({**scope, "basis_quote": "from 2010 to 2014"},
                      {**scope, "end_year": 2015}, {**scope, "start_year": True},
                      {**scope, "start_year": 2015}, {**scope, "extra": True}):
            client.scope = value
            with self.subTest(scope=value), self.assertRaises(ValueError):
                build_requested_research_plan(request)
        client.scope = None
        self.assertNotIn("temporal_scope", build_requested_research_plan(request).source_plan.filters)
        # Old/offline plans do not guess a range from topic or generated prose.
        legacy = build_research_plan(ResearchPlanRequest(topic=topic,
            problem_markdown="Generated problem: 2000 through 2005"))
        restored = ResearchPlanResult.from_handoff_dict(legacy.to_handoff_dict())
        self.assertNotIn("temporal_scope", search_request_from_plan(restored).filters)
        client.scope = scope
        generated_only = ResearchPlanRequest(topic="Survey interval methods", problem_markdown=topic,
                                             use_llm=True, llm_client=client)
        with self.assertRaises(ValueError):
            build_requested_research_plan(generated_only)

    def test_direct_material_report_retains_handoff_without_query_model(self) -> None:
        from dataclasses import replace
        from simple_ar.research.task_plan import default_task_steps

        base = TaskPlanRequest(
            task_kind="survey", goal="Compare supplied notes", request_text="Compare supplied notes",
            requested_outputs=("report",),
            config={"research_materials_only": True, "research_local_documents": ["notes.md"]},
        )
        class Client:
            model = "fixture-planner"
            def __init__(self):
                self.labels = []
            def ask_json(self, _system, _prompt, *, label="", **_kwargs):
                self.labels.append(label)
                if label != "task-plan":
                    raise AssertionError("Direct writing has no query-planning consumer")
                return {"steps": default_task_steps(base)}

        client = Client()
        with tempfile.TemporaryDirectory() as tmp:
            context = CapabilityContext(store=ArtifactStore(Path(tmp)),
                                        attempt=AttemptManifest(attempt_id="plan-local", capability="plan"))
            result = run_research_plan_capability(context=context, request=ResearchPlanRequest(
                topic=base.goal, config=base.config, use_llm=True, llm_client=client,
                task_plan_request=replace(base, use_llm=True, llm_client=client),
            ))
            plans = {ref.kind: context.store.read_json(ref) for ref in result.artifacts}
            self.assertEqual(plans["research_plan"]["planner"], "deterministic")
            self.assertTrue(plans["research_plan"]["research_questions"]["questions"])
            self.assertNotIn("read", [row["action"] for row in plans["task_plan"]["steps"]])
        self.assertEqual(client.labels, ["task-plan"])

    def test_rejected_task_route_does_not_spend_a_query_planning_call(self) -> None:
        class Client:
            model = "fixture-planner"

            def __init__(self) -> None:
                self.labels: list[str] = []

            def ask_json(self, _system, _prompt, *, label="", **_kwargs):
                self.labels.append(label)
                return {"steps": [{"action": "prepare_execution"}]}

        client = Client()
        task = TaskPlanRequest(
            task_kind="research", goal="Compare a candidate", request_text="Compare a candidate",
            requested_outputs=("experiments",),
            execution={"command": ["python", "measure.py"]},
            use_llm=True, llm_client=client,
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = CapabilityContext(
                store=ArtifactStore(Path(tmp)),
                attempt=AttemptManifest(attempt_id="plan-invalid", capability="plan"),
            )
            with self.assertRaisesRegex(ValueError, "after one correction"):
                run_research_plan_capability(
                    context=context,
                    request=ResearchPlanRequest(
                        topic="candidate comparison", use_llm=True, llm_client=client,
                        task_plan_request=task,
                    ),
                )
            self.assertTrue((Path(tmp) / "task_plan_proposals.json").is_file())
            self.assertFalse((Path(tmp) / "research_plan.json").exists())
        self.assertEqual(client.labels, ["task-plan", "task-plan"])

    def test_caution_in_user_goal_does_not_exclude_the_research_topic(self) -> None:
        result = build_research_plan(ResearchPlanRequest(
            topic="continual learning",
            problem_markdown=("Study continual learning with a small replay memory. "
                              "Do not assume a particular algorithm.\n"
                              "Out of scope: large language model training.")))
        self.assertEqual(result.query_plan.negative_terms,
                         ["Out of scope: large language model training."])

    def test_capability_can_use_explicit_llm_planner(self) -> None:
        class FakeClient:
            model = "fake-research-model"

            def ask_json(self, system: str, user: str, *, label: str = "", **kwargs: object) -> dict[str, object]:
                self.label = label
                self.max_output_tokens = kwargs.get("max_output_tokens")
                return {
                    "questions": [
                        {
                            "question": "Which methods improve reliable coding agents?",
                            "facet": "method",
                            "rationale": "Compare method families.",
                            "required": True,
                            "negative_scope": [],
                            "success_criteria": ["Find method evidence."],
                        }
                    ],
                    "queries": [
                        "reliable coding agents benchmark",
                        "LLM code agent validation",
                    ],
                    "query_specs": [{"facet": "method", "title_keywords": ["coding", "agents"],
                                     "abstract_keywords": ["validation", "bounded", "memory"],
                                     "rationale": "Retrieve the topic before checking supporting conditions."}],
                    "required_facets": ["method", "benchmark"],
                    "negative_terms": [],
                    "rationale": "Use focused method and benchmark searches.",
                }

        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            context = CapabilityContext(
                store=ArtifactStore(Path(tmp)),
                attempt=AttemptManifest(attempt_id="plan-001", capability="plan"),
            )
            result = run_research_plan_capability(
                context=context,
                request=ResearchPlanRequest(
                    topic="reliable coding agents",
                    config={"research_sources": ["fixture"]},
                    use_llm=True,
                    llm_client=client,
                ),
            )
            payload = context.store.read_json(result.artifacts[0])

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.provenance["mode"], "llm")
        self.assertEqual(result.provenance["model"], "fake-research-model")
        self.assertEqual(client.label, "research-planner")
        self.assertIsNone(client.max_output_tokens)
        self.assertEqual(payload["planner"], "llm")
        self.assertEqual(payload["query_plan"]["queries"], ["coding agents"])
        with tempfile.TemporaryDirectory() as tmp:
            run_research_plan_capability(
                context=CapabilityContext(
                    store=ArtifactStore(Path(tmp)),
                    attempt=AttemptManifest(attempt_id="plan-override", capability="plan"),
                ),
                request=ResearchPlanRequest(
                    topic="reliable coding agents",
                    config={"research_planning_max_output_tokens": 2048},
                    use_llm=True,
                    llm_client=client,
                ),
            )
        self.assertEqual(client.max_output_tokens, 2048)

    def test_plan_reuses_existing_deterministic_planners(self) -> None:
        result = build_research_plan(
            ResearchPlanRequest(
                topic="reliable coding agents",
                problem_markdown="# Problem\nStudy bounded agent evaluation.",
                config={
                    "research_required_facets": ["method", "benchmark"],
                    "research_sources": ["fixture"],
                    "research_queries": ["reliable coding agents"],
                },
                default_query="coding agents",
                default_max_results=4,
            )
        )

        self.assertTrue(result.questions)
        self.assertEqual(result.query_plan.planner, "deterministic")
        self.assertEqual(result.source_plan.sources, ["fixture"])
        self.assertEqual(result.source_plan.max_results_per_query, 4)
        self.assertIn("benchmark", result.query_plan.required_facets)

    def test_plan_handoff_round_trips_without_llm_or_network(self) -> None:
        result = build_research_plan(ResearchPlanRequest(topic="agent evaluation"))

        restored = ResearchPlanResult.from_handoff_dict(result.to_handoff_dict())

        self.assertEqual(restored.query_plan.to_row(), result.query_plan.to_row())
        self.assertEqual(restored.source_plan.to_row(), result.source_plan.to_row())
        self.assertEqual(
            [question.to_row() for question in restored.questions],
            [question.to_row() for question in result.questions],
        )

    def test_plan_adapts_to_search_without_invoking_a_provider(self) -> None:
        result = build_research_plan(
            ResearchPlanRequest(
                topic="agent evaluation",
                config={"research_sources": ["fixture"]},
            )
        )

        request = search_request_from_plan(result)

        self.assertEqual(request.providers, ("fixture",))
        self.assertEqual(request.queries, tuple(result.query_plan.queries))
        self.assertEqual(
            request.max_results_per_query,
            result.source_plan.max_results_per_query,
        )

    def test_capability_persists_research_plan_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = CapabilityContext(
                store=ArtifactStore(Path(tmp)),
                attempt=AttemptManifest(attempt_id="plan-001", capability="plan"),
            )

            result = run_research_plan_capability(
                context=context,
                request=ResearchPlanRequest(topic="agent evaluation"),
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.artifacts[0].path, "research_plan.json")
            payload = context.store.read_json(result.artifacts[0])
            self.assertEqual(payload["schema_version"], "research_plan.v1")
            self.assertEqual(payload["planner"], "deterministic")

    def test_plan_handoff_can_drive_search_in_a_session_fixture(self) -> None:
        class Connector:
            source_name = "fixture"

            def search(self, request: SearchQuery) -> SearchResponse:
                return SearchResponse(
                    source=self.source_name,
                    query=request.query,
                    papers=[
                        Paper(
                            id="fixture-paper",
                            title="Fixture evidence",
                            authors=[],
                            abstract="A bounded fixture result.",
                            url="https://example.test/fixture-paper",
                            source="fixture",
                        )
                    ],
                )

        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            from simple_ar.research import register_research_capabilities

            register_research_capabilities(registry, names=("plan", "search"))
            controller = SessionController.create(
                Path(tmp),
                session_id="plan-search-flow",
                topic="fixture research",
                profile="research_brief",
                registry=registry,
                budget=None,
            )

            plan_result = controller.execute_attempt(
                "plan",
                attempt_id="plan-001",
                request=ResearchPlanRequest(
                    topic="fixture research",
                    config={"research_sources": ["fixture"]},
                ),
            )
            self.assertEqual(plan_result.status, "completed")

            plan_ref = controller.attempt_output_ref(
                "plan-001",
                kind="research_plan",
                schema="research_plan.v1",
            )
            restored = ResearchPlanResult.from_handoff_dict(
                controller.store.read_json(plan_ref)
            )
            search_result = controller.execute_attempt(
                "search",
                attempt_id="search-001",
                inputs=(plan_ref,),
                request=search_request_from_plan(restored),
                registry=SearchProviderRegistry({"fixture": Connector}),
            )

            self.assertEqual(search_result.status, "completed")
            payload = controller.store.read_json(
                controller.attempt_output_ref(
                    "search-001",
                    kind="search_result",
                    schema="search_handoff.v1",
                )
            )
            self.assertEqual(payload["papers"][0]["id"], "fixture-paper")


if __name__ == "__main__":
    unittest.main()
