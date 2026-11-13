from __future__ import annotations

import tempfile
import json
import unittest
from pathlib import Path

from simple_ar.core import BudgetState, CapabilityRegistry, SessionController
from simple_ar.literature.models import Paper
from simple_ar.research.brief import evidence_pack_from_read
from simple_ar.research.contracts import DocumentRecord, SourcePlan, TextChunk
from simple_ar.research.documents import DocumentBundle, DocumentIngestRequest
from simple_ar.research.evidence.reader import ReadRequest, ReadResult
from simple_ar.research.sources import (
    SearchProviderRegistry,
    SearchRequest,
    SearchResult,
)
from simple_ar.research.sources.base import SearchQuery, SearchResponse
from simple_ar.research.synthesis import SynthesisRequest
from simple_ar.research import register_research_capabilities


class LiteratureSessionFlowTests(unittest.TestCase):
    def test_retained_documents_share_the_source_window(self) -> None:
        from simple_ar.app.tool_application import ToolApplication

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = root / 'sources'
            sources.mkdir()
            originals = {}
            for name in ('first', 'second'):
                bundle = DocumentBundle(
                    records=[DocumentRecord(document_id=name, title=name, source='fixture')],
                    fulltext_manifest={}, fulltext_extraction={}, sections=[],
                    chunks=[TextChunk(chunk_id=f'{name}-{index}', document_id=name,
                                      text=f'{name} evidence {index}') for index in range(3)])
                path = sources / f'{name}.json'
                path.write_text(json.dumps(bundle.to_handoff_dict()), encoding='utf-8')
                originals[path] = path.read_bytes()
            app = ToolApplication.create(root / 'task', 'Compare the supplied documents.',
                model=None, sources={'papers': sources},
                research_policy={'materials': ['papers/first.json', 'papers/second.json'],
                                 'max_chunks': 3, 'allow_network': False})
            app.run(actions=[{'tool_name': 'research', 'arguments': {
                'operation': 'ingest', 'question': 'Compare the sources'}}], progress=lambda _: None)
            observation = app.session.store.read_json(app.state['turns'][-1]['tools'][-1]['result'])
            self.assertEqual(observation['status'], 'ok', observation)
            merged = json.loads(app.workspace.resolve(observation['data']['document_record']).read_text())
            self.assertEqual([row['document_id'] for row in merged['chunks']], ['first', 'second', 'first'])
            self.assertEqual({row['document_id'] for row in merged['documents']}, {'first', 'second'})
            self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def test_search_to_synthesis_uses_persisted_attempt_handoffs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper_path = root / "fixture-paper.md"
            paper_path.write_text(
                "# Method\n\n"
                "A validation method improves reliable agent behavior.\n\n"
                "# Experiments\n\n"
                "The AgentBench dataset reports accuracy and runtime metrics.\n",
                encoding="utf-8",
            )

            registry = CapabilityRegistry()
            register_research_capabilities(
                registry,
                names=("search", "document_ingest", "read", "synthesize"),
            )
            controller = SessionController.create(
                root / "session",
                session_id="literature-flow",
                topic="reliable agents",
                profile="research_brief",
                registry=registry,
                budget=BudgetState(max_attempts=6),
            )

            result = controller.execute_attempt(
                "search",
                attempt_id="search-001",
                request=SearchRequest(
                    queries=("reliable agents",),
                    providers=("fixture",),
                    max_results_per_query=1,
                ),
                registry=SearchProviderRegistry(
                    {"fixture": lambda: _FixtureConnector(paper_path)}
                ),
            )
            self.assertEqual(result.status, "completed")

            search_ref = controller.attempt_output_refs("search-001")[0]
            search_payload = controller.store.read_json(search_ref)
            search_result = SearchResult.from_handoff_dict(search_payload)
            self.assertEqual(search_result.papers[0].id, "paper-1")

            source_plan = SourcePlan(
                queries=["reliable agents"],
                sources=["local_files"],
                max_results_per_query=1,
                require_fulltext=True,
                budget={"max_fulltext_documents": 1},
            )
            result = controller.execute_attempt(
                "document_ingest",
                attempt_id="document-001",
                inputs=(search_ref,),
                request=DocumentIngestRequest(
                    papers=search_result.papers,
                    source_plan=source_plan,
                    cache_dir=root / "cache",
                    extraction_dir=root / "extracted",
                ),
            )
            self.assertEqual(result.status, "completed")

            document_ref = controller.attempt_output_refs("document-001")[0]
            bundle = DocumentBundle.from_handoff_dict(
                controller.store.read_json(document_ref)
            )
            self.assertEqual(len(bundle.records), 1)
            self.assertTrue(bundle.chunks)

            result = controller.execute_attempt(
                "read",
                attempt_id="read-001",
                inputs=(document_ref,),
                request=ReadRequest(bundle=bundle),
            )
            self.assertEqual(result.status, "completed")

            read_ref = controller.attempt_output_refs("read-001")[0]
            read_result = controller.store.read_json(read_ref)
            self.assertEqual(read_result["schema_version"], "read_result.v1")
            restored_read = ReadResult.from_handoff_dict(
                read_result,
                bundle=bundle,
            )

            result = controller.execute_attempt(
                "synthesize",
                attempt_id="synthesis-001",
                inputs=(read_ref,),
                request=SynthesisRequest(
                    evidence_pack=evidence_pack_from_read(
                        "reliable agents",
                        restored_read,
                    )
                ),
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(controller.manifest.status, "running")
            self.assertEqual(controller.manifest.decisions, [])
            self.assertEqual(len(controller.list_attempts()), 4)
            self.assertEqual(
                {
                    item.attempt_id: item.parent_attempt
                    for item in controller.list_attempts()
                },
                {
                    "search-001": None,
                    "document-001": "search-001",
                    "read-001": "document-001",
                    "synthesis-001": "read-001",
                },
            )
            self.assertEqual(
                controller.store.read_json(
                    "attempts/synthesis-001/synthesis_result.json"
                )["schema_version"],
                "synthesis_result.v1",
            )


class _FixtureConnector:
    source_name = "fixture"

    def __init__(self, path: Path) -> None:
        self.path = path

    def search(self, request: SearchQuery) -> SearchResponse:
        return SearchResponse(
            source=self.source_name,
            query=request.query,
            papers=[
                Paper(
                    id="paper-1",
                    title="Reliable agents",
                    authors=["Fixture Author"],
                    abstract="A validation method improves reliable agent behavior.",
                    url=self.path.as_uri(),
                    source="local_files",
                    source_id=str(self.path),
                    fulltext_url=self.path.as_uri(),
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
