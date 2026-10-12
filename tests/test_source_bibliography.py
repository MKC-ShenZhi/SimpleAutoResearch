"""Source-local bibliography adoption, material writing and saved recovery."""
import copy
import unittest
import json

from unittest.mock import Mock, patch
from simple_ar.literature.models import Paper, bibliographic_details
from simple_ar.research.contracts import DocumentRecord, DocumentSection, TextChunk
from simple_ar.research.documents.ingest import DocumentBundle
from simple_ar.research.evidence.bibliography import apply_bibliographic_note, front_matter_view
from simple_ar.research.evidence.reader import ReadRequest, ReadResult, read_documents
from simple_ar.core.capabilities import ArtifactRef
from simple_ar.report.projection import attach_report_read_evidence, _paper_source_handles, build_material_report_inputs, apply_report_bibliography, bibliography_planning_views
from simple_ar.report.schema import ReportContext, ReportMemory, ReportRuntimeConfig, ReportSectionPlan, SourceHandle
from simple_ar.report.citations import references_markdown
from simple_ar.literature.bibtex import papers_to_bibtex
from simple_ar.research.sources.capability import provided_materials_result
from simple_ar.research.evidence.screening import _paper_screening_record
from types import SimpleNamespace
from simple_ar.integrations.llm import LLMError
from simple_ar.report.agent import run_report_agent, _outline_planner_prompt
from simple_ar.report.templates import load_report_template_bundle
from simple_ar.report.tool_gateway import ReportToolGateway


class SourceBibliographyTests(unittest.TestCase):
    def test_local_filename_placeholder_is_not_presented_as_known_paper_title(self):
        local = DocumentRecord('doc', 'input paper', 'local_files', source_id='/private/input_paper.pdf')
        self.assertEqual(_paper_screening_record(local.to_row(), 1)['title_role'],
                         'filename_placeholder_not_publication_title')
        supplied = {**local.to_row(), 'title': 'Actual user title'}
        self.assertEqual(_paper_screening_record(supplied, 1)['title_role'], 'supplied_metadata')
        provider = {**local.to_row(), 'metadata': {'paper_id': 'provider'}}
        self.assertEqual(_paper_screening_record(provider, 1)['title_role'], 'supplied_metadata')

    def setUp(self):
        self.record = DocumentRecord('doc', 'input paper', 'local_files', source_id='/private/input_paper.pdf',
            url='/private/input_paper.pdf', extraction_status='parsed')
        self.header = DocumentSection('front', 'doc', 'front_matter', 'Front matter',
            'A source title\nAda Example, Grace Example\n2025-04-03\nhttps://example.org/study\nDOI 10.1234/study')
        self.paper = provided_materials_result([self.record]).papers[0].to_row()
        self.fields = [{'field': field, 'value': value, 'section_id': 'front', 'quote': quote, 'complete': True}
            for field, value, quote in (
                ('title', 'A source title', 'A source title'),
                ('authors', ['Ada Example', 'Grace Example'], 'Ada Example, Grace Example'),
                ('published', '2025-04-03', '2025-04-03'),
                ('url', 'https://example.org/study', 'https://example.org/study'),
                ('doi', '10.1234/study', 'DOI 10.1234/study'))]
        self.note = {'paper_id': 'doc', 'title': 'A source title', 'bibliographic_fields': self.fields}

    def project(self, **kwargs):
        return apply_bibliographic_note(kwargs.get('paper', self.paper), kwargs.get('record', self.record),
            kwargs.get('section', self.header), kwargs.get('note', self.note))

    def test_same_source_fields_fill_missing_values_with_locators_and_no_identity_claim(self):
        before = copy.deepcopy(self.paper)
        row, origins = self.project()
        self.assertEqual(row['authors'], ['Ada Example', 'Grace Example'])
        self.assertEqual(bibliographic_details(Paper.from_row(row))['missing_fields'], [])
        self.assertEqual(set(origins), {'title', 'authors', 'published', 'url', 'doi'})
        self.assertEqual(origins['doi']['section_id'], 'front')
        self.assertEqual(bibliographic_details(Paper.from_row(row))['verification_status'], 'not_independently_verified')
        self.assertEqual(self.paper, before)

    def test_source_local_public_url_is_not_mistaken_for_private_path(self):
        row, _ = self.project()
        self.assertEqual(bibliographic_details(Paper.from_row(row))['url'], 'https://example.org/study')
        self.assertEqual(bibliographic_details(Paper.from_row(self.paper))['url'], '')

    def test_provider_and_recorded_fields_are_not_overwritten(self):
        row, _ = self.project(paper={**self.paper, 'title': 'User title', 'authors': ['User Author'], 'published': '2024',
            'url': 'https://example.org/original', 'doi': '10.1234/original'})
        self.assertEqual(row['title'], 'User title')
        self.assertEqual(row['authors'], ['User Author'])
        self.assertEqual(row['published'], '2024')
        self.assertEqual(row['doi'], '10.1234/original')
        self.assertEqual(row['url'], 'https://example.org/original')
        record = DocumentRecord('doc', 'input paper', 'local_files', metadata={'paper_id': 'provider'})
        self.assertEqual(self.project(record=record), (self.paper, {}))

    def test_other_document_or_non_front_matter_is_not_citation_evidence(self):
        for section in (DocumentSection('front', 'other', 'front_matter', '', self.header.text),
                        DocumentSection('front', 'doc', 'references', '', self.header.text), None):
            self.assertEqual(self.project(section=section), (self.paper, {}))

    def test_bad_quotes_and_values_are_ignored_without_discarding_good_fields(self):
        for changes in ({'quote': 'Invented'}, {'section_id': 'other'}, {'value': ['Ada Example', 'Invented']},
                        {'value': ['Ada Example', 'Ada Example']}, {'value': 'Ada Example'}):
            fields = copy.deepcopy(self.fields)
            fields[1].update(changes)
            row, origins = self.project(note={**self.note, 'bibliographic_fields': fields})
            self.assertEqual(row['authors'], [])
            self.assertEqual(row['doi'], '10.1234/study')
            self.assertNotIn('authors', origins)

    def test_partial_byline_stays_explicit(self):
        fields = copy.deepcopy(self.fields)
        fields[1].pop('complete')
        row, _ = self.project(note={**self.note, 'bibliographic_fields': fields})
        self.assertTrue(any('completeness is not established' in message for message in row['bibliographic_notes']))

    def test_incremental_projection_preserves_notes_without_repeating_extraction_caveat(self):
        title_note = {'bibliographic_fields': [self.fields[0]]}
        first, _ = self.project(note=title_note)
        first['bibliographic_notes'].insert(0, 'User-supplied limitation.')
        before = copy.deepcopy(first)
        second, origins = self.project(paper=first, note=self.note)
        self.assertEqual(first, before)
        self.assertNotIn('title', origins)
        self.assertIn('authors', origins)
        self.assertEqual(len(second['bibliographic_notes']), len(set(second['bibliographic_notes'])))
        self.assertEqual(second['bibliographic_notes'][0], 'User-supplied limitation.')
        repeated, origins = self.project(paper=second)
        self.assertEqual(repeated, second)
        self.assertEqual(origins, {})

    def test_explicit_registry_stamp_maps_locator_without_inferring_year_or_changing_identity(self):
        for identifier in ('2306.12345v2', 'hep-th/9901001v3'):
            section = DocumentSection('front', 'doc', 'front_matter', '', f'arXiv:{identifier} [category]\nA source title')
            row, origins = self.project(section=section, note={'title': 'A source title'})
            self.assertEqual(row['url'], f'https://arxiv.org/abs/{identifier}')
            self.assertEqual(row['published'], None)
            self.assertEqual(row['source_id'], '/private/input_paper.pdf')
            self.assertEqual(origins['url']['quote'], f'arXiv:{identifier}')
        for text in ('Named 2306.12345.pdf', 'Discusses arXiv:2306.12345 inside prose.',
                     'arXiv:2306.12345v1\narXiv:2306.12345v2', 'arXiv:not-an-id'):
            row, _ = self.project(section=DocumentSection('front', 'doc', 'front_matter', '', text), note={})
            self.assertEqual(row['url'], self.paper['url'])

    def test_malformed_optional_field_does_not_break_an_otherwise_valid_read(self):
        row, _ = self.project(note={**self.note, 'bibliographic_fields': [{'field': [], 'quote': 'A source title'}]})
        self.assertEqual(row['title'], 'A source title')

    def test_unread_tail_and_invalid_date_are_not_adopted(self):
        section = DocumentSection('front', 'doc', 'front_matter', '', 'x' * 12000 + '\n2025-04-03\nA source title')
        self.assertEqual(self.project(section=section), (self.paper, {}))
        fields = [{'field': 'published', 'value': '2025-99-99', 'quote': '2025-99-99', 'section_id': 'front'}]
        section = DocumentSection('front', 'doc', 'front_matter', '', '2025-99-99')
        self.assertEqual(self.project(section=section, note={'bibliographic_fields': fields}), (self.paper, {}))
        for label in ('Draft version', 'Received', 'Accepted', 'Revised', 'Submitted', 'Updated', 'Copyright'):
            for quote in ('2025-04-03', f'{label}: 2025-04-03'):
                with self.subTest(label=label, quote=quote):
                    section = DocumentSection('front', 'doc', 'front_matter', '', f'{label}: 2025-04-03')
                    fields = [{'field': 'published', 'value': '2025-04-03',
                               'quote': quote, 'section_id': 'front'}]
                    self.assertEqual(self.project(section=section, note={'bibliographic_fields': fields}), (self.paper, {}))
        section = DocumentSection('front', 'doc', 'front_matter', '',
                                  'Received: 2025-04-03\nPublished online: 2025-04-03')
        # Use the publication occurrence when the same calendar date has both roles.
        fields[0]['quote'] = 'Published online: 2025-04-03'
        row, _ = self.project(section=section, note={'bibliographic_fields': fields})
        self.assertEqual(row['published'], '2025-04-03')

    def test_fields_roundtrip_and_reach_shared_references_and_bibtex(self):
        from dataclasses import replace
        from simple_ar.report.citations import citation_map_artifact
        for document_id, linked in (('doc', False), ('doc', True), ('résumé/文献', True)):
            for note_id in ('document', 'citation'):
                with self.subTest(document_id=document_id, linked=linked, note_id=note_id):
                    record = (replace(self.record, document_id=document_id) if not linked else
                        DocumentRecord(document_id, 'https://example.org/study.pdf?version=2',
                            'supporting_material', url='https://example.org/study.pdf?version=2',
                            metadata={'kind': 'supporting_material'}, extraction_status='parsed'))
                    header = replace(self.header, document_id=document_id)
                    bundle = DocumentBundle([record], {}, {}, [header],
                        [TextChunk('body', document_id, 'Observed results with qualifications.',
                            metadata={'section': 'results'})])
                    context, memory = build_material_report_inputs(topic='Review', documents=bundle,
                        documents_ref=ArtifactRef('documents.json'),
                        assets=[] if linked else [SimpleNamespace(locator=record.source_id, role='paper')])
                    paper_id = context.papers[0]['id']
                    note = {**self.note, 'paper_id': paper_id if note_id == 'citation' else document_id}
                    read = ReadResult(status='completed', bundle=bundle, paper_notes=(note,))
                    restored = ReadResult.from_handoff_dict(read.to_handoff_dict(), bundle=bundle)
                    original = copy.deepcopy((context.model_dump(), bundle.to_handoff_dict()))
                    projected, memory = attach_report_read_evidence(context, memory, documents=bundle,
                        read=restored, read_ref=ArtifactRef('read.json'))
                    paper = Paper.from_row(projected.papers[0])
                    self.assertEqual(paper.id, paper_id)
                    self.assertEqual(paper.title, 'A source title')
                    self.assertIn('Ada Example, Grace Example (2025)', references_markdown([paper]))
                    self.assertIn('author = {Ada Example and Grace Example}', papers_to_bibtex([paper]))
                    self.assertEqual(citation_map_artifact({paper_id: 1}, [paper])['entries'][0]
                        ['bibliography']['authors'], paper.authors)
                    self.assertEqual(memory.source_handles[0].metadata['bibliographic_sources']
                        ['published']['quote'], '2025-04-03')
                    self.assertEqual(context.model_dump(), original[0])
                    self.assertEqual(bundle.to_handoff_dict(), original[1])
                    if linked:
                        self.assertEqual(paper_id, 'material-' + document_id.encode().hex())
                        self.assertEqual(paper.url, record.url)  # A known locator is not overwritten.
                    # Both spellings can appear across planner/writer responses;
                    # acceptance remains idempotent and same-source, not fuzzy.
                    repeated, _ = apply_report_bibliography(projected, memory, documents=bundle,
                        notes=[{**note, 'paper_id': document_id}, {**note, 'paper_id': paper_id}])
                    self.assertEqual(repeated.papers, projected.papers)

    def test_existing_read_call_receives_front_matter_without_extra_model_request(self):
        client = Mock()
        client.ask_json_many.return_value = [self.note]
        bundle = DocumentBundle([self.record], {}, {}, [self.header], [])
        read = read_documents(ReadRequest(bundle=bundle, use_llm=True, llm_client=client,
            config={'read_screening': 'deterministic'}))
        self.assertEqual(client.ask_json_many.call_count, 1)
        self.assertIn('https://example.org/study', client.ask_json_many.call_args.args[0][0].user)
        self.assertEqual(read.paper_notes[0]['bibliographic_fields'], self.fields)
        self.assertFalse(front_matter_view(self.header)['truncated'])


class MaterialBibliographyTests(unittest.TestCase):
    def inputs(self):
        record = DocumentRecord(document_id="doc", title="paper input", source="local_files",
            source_id="/private/paper_input.pdf", extraction_status="parsed")
        header = "A supplied study\nAda Example, Bo Example\narXiv:2501.12345v2 5 Feb 2025"
        front = DocumentSection("front", "doc", "front_matter", "Front matter", header, line_start=1)
        bundle = DocumentBundle([record], {}, {}, [front],
            [TextChunk("body", "doc", "Observed results with qualifications.", metadata={"section": "results"})])
        context, memory = build_material_report_inputs(topic="Explain these results", documents=bundle,
            documents_ref=ArtifactRef("documents.json"), assets=[SimpleNamespace(locator=record.source_id, role="paper")])
        memory.section_plan = [ReportSectionPlan(section_id="body", heading="Results", goal="Explain", evidence_handles=[context.source_handles[0].handle])]
        fields = [
            {"field": "title", "value": "A supplied study", "quote": "A supplied study", "section_id": "front"},
            {"field": "authors", "value": ["Ada Example", "Bo Example"], "quote": "Ada Example, Bo Example", "section_id": "front", "complete": True},
            {"field": "published", "value": "2025", "quote": "5 Feb 2025", "section_id": "front"},
        ]
        note = {"paper_id": "doc", "bibliographic_fields": fields}
        response = {"title": "Observed results", "sections": [
            {"heading": "Results", "goal": "Explain", "evidence_handles": [context.source_handles[0].handle]},
            {"heading": "Limits", "goal": "Qualify", "evidence_handles": [context.source_handles[0].handle]},
        ], "source_notes": [note]}
        return context, memory, bundle, note, response

    def test_observed_identifier_is_usable_without_a_model_or_filename_guess(self):
        context, _, bundle, _, _ = self.inputs()
        from simple_ar.report.narrative import _prompt_handle_view, review_source_evidence
        bundle.chunks.extend(TextChunk(f"span-{index}", "doc", "A retained passage.",
            metadata={"section_id": f"part-{index}", "heading": f"Independent experiment {index}"})
            for index in range(9))
        navigable, _ = build_material_report_inputs(topic="Explain results", documents=bundle,
            documents_ref=ArtifactRef("documents.json"), assets=[])
        source_view = _prompt_handle_view(navigable.source_handles[0])
        directory = source_view['metadata']['source_directory']
        self.assertEqual(directory['total_sections'], 10)
        self.assertEqual(directory['headings'][-1], 'Independent experiment 8')
        self.assertEqual(navigable.source_handles[0].metadata['source_directory']['sections'][-1]
            ['heading'], 'Independent experiment 8')  # Original addresses remain retained.
        self.assertNotIn('span-8', [row['chunk_id'] for row in source_view['metadata']['evidence_passages']])
        self.assertEqual(review_source_evidence([source_view])[0]['metadata']['source_directory'], directory)
        self.assertEqual(len(bundle.chunks), 10)  # Navigation does not rewrite the source.
        paper = Paper.from_row(context.papers[0])
        self.assertEqual(paper.url, "https://arxiv.org/abs/2501.12345v2")
        self.assertEqual(paper.title, "paper input")
        self.assertEqual(paper.authors, [])
        self.assertIsNone(paper.published)
        self.assertIsNone(bundle.records[0].url)
        # Reusing a draft's citation identities must not promote it to primary
        # source evidence or require parsing arbitrary BibTeX with ad-hoc regex.
        import tempfile
        from pathlib import Path
        from simple_ar.report.citations import citation_map_artifact
        from simple_ar.research.documents.ingest import build_document_bundle
        from simple_ar.research.contracts import SourcePlan
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recorded-identities.json"
            source = Paper("study-2025", "Recorded study", ["Ada"], "", "https://example.org/study", published="2025")
            path.write_text(json.dumps(citation_map_artifact({source.id: 1}, [source], {"P8": source.id})), encoding="utf-8")
            draft = Path(directory) / "draft.md"
            draft.write_text("A supplied draft is not evidence of its cited original sources.", encoding="utf-8")
            bundle = build_document_bundle(papers=[], source_plan=SourcePlan([], local_documents=[str(path), str(draft)]),
                cache_dir=None, extraction_dir=Path(directory))
            context, memory = build_material_report_inputs(topic="Revise the supplied draft", documents=bundle,
                documents_ref=ArtifactRef("documents.json"), assets=[SimpleNamespace(locator=str(path), role="material")])
            self.assertEqual([p["id"] for p in context.papers], [source.id])
            retained = Paper.from_row(context.papers[0])
            self.assertEqual(retained.authors, ["Ada"])
            self.assertEqual(retained.abstract, "")
            self.assertEqual(context.citation_key_map, {"P8": source.id})
            self.assertEqual(memory.source_handles[-1].citation_key, "P8")
            self.assertIn("primary text was not rechecked", retained.bibliographic_notes[0])
            self.assertEqual(memory.source_handles[-1].metadata["document_chunk_count"], 0)
            self.assertEqual(memory.source_handles[-1].metadata["evidence_role"], "reused_reference_metadata_not_primary_text")
            self.assertFalse(any(row.title == "recorded identities" for row in memory.source_handles))
            self.assertEqual(len(memory.source_handles), 2)  # Draft plus typed reference; no duplicate map-as-document.
            for index in (1, 2):
                dataset = Paper(f"dataset-{index}", f"Recorded dataset {index}", [], "", "")
                bundle.records.append(DocumentRecord(document_id=f"analysis-{index}", title=dataset.title,
                    source="local_analysis", metadata={"code_analysis": {"schema_version": "code_analysis.v1",
                        "artifact": f"analysis-{index}/analysis.json", "results": {"citation_map":
                            citation_map_artifact({dataset.id: 1}, [dataset], {"P8": dataset.id})}}}))
            combined, _ = build_material_report_inputs(topic="Combine supplied results", documents=bundle,
                documents_ref=ArtifactRef("documents.json"), assets=[SimpleNamespace(locator=str(path), role="material")])
            self.assertEqual(combined.citation_key_map, {"P8": source.id, "P1": "dataset-1", "P2": "dataset-2"})
            # Retrieved source identity survives bundle reuse. Same-title
            # sources with different explicit identities must remain distinct.
            for storage_id, paper_id in (("storage-42", source.id), ("storage-99", "distinct-study")):
                bundle.records.append(DocumentRecord(document_id=storage_id, title=source.title,
                    source="fixture", authors=source.authors, url=source.url, published=source.published,
                    metadata={"paper_id": paper_id, "retained_source_role": "paper",
                              "retained_bundle": str(path)}))
                bundle.chunks.append(TextChunk(f"chunk-{storage_id}", storage_id, "Original source conditions."))
            before = copy.deepcopy(bundle.to_handoff_dict())
            reused, _ = build_material_report_inputs(topic="Check the original conditions", documents=bundle,
                documents_ref=ArtifactRef("documents.json"), assets=[SimpleNamespace(locator=str(path), role="material")])
            self.assertEqual(sum(p['id'] == source.id for p in reused.papers), 1)
            self.assertEqual(sum(p['id'] == "distinct-study" for p in reused.papers), 1)
            primary = [h for h in reused.source_handles if h.paper_id == source.id]
            self.assertEqual(len(primary), 1)
            self.assertEqual(primary[0].metadata['document_id'], "storage-42")
            self.assertEqual(primary[0].metadata['document_chunk_count'], 1)
            self.assertEqual(primary[0].citation_key, "P8")
            self.assertEqual(bundle.to_handoff_dict(), before)
            linked_id = "material-" + "repo-snapshot".encode().hex()
            linked = Paper(linked_id, "Linked docs", [], "", "https://example.org/repo")
            path.write_text(json.dumps(citation_map_artifact({source.id: 1, linked.id: 2},
                [source, linked], {"P8": source.id, "P9": linked.id})), encoding="utf-8")
            bundle.records.append(DocumentRecord(document_id="repo-snapshot", title=linked.title,
                source="supporting_material", url=linked.url, metadata={"kind": "supporting_material"}))
            bundle.chunks.append(TextChunk("repo-text", "repo-snapshot", "Public installation instructions."))
            linked_context, _ = build_material_report_inputs(topic="Reuse sources and software docs", documents=bundle,
                documents_ref=ArtifactRef("documents.json"), assets=[SimpleNamespace(locator=str(path), role="material")])
            linked_handles = [h for h in linked_context.source_handles if h.paper_id == linked.id]
            self.assertEqual(len(linked_handles), 1)
            self.assertEqual(linked_handles[0].kind, "material")
            self.assertEqual(linked_handles[0].metadata['document_chunk_count'], 1)
            self.assertEqual(linked_context.citation_key_map['P9'], linked.id)
            # Known source metadata must not disappear just because a document
            # is linked supporting material rather than a provider search hit.
            from dataclasses import replace
            bundle.records[-1] = replace(bundle.records[-1], authors=['René Example'],
                published='2024', doi='10.1234/linked',
                metadata={'kind': 'supporting_material', 'bibliographic_notes': ['Supplied version notice.']})
            known, _ = build_material_report_inputs(topic='Reuse known identity', documents=bundle,
                documents_ref=ArtifactRef('documents.json'), assets=[])
            paper = Paper.from_row(next(row for row in known.papers if row['id'] == linked.id))
            self.assertEqual((paper.authors, paper.published, paper.doi),
                             (['René Example'], '2024', '10.1234/linked'))
            self.assertIn('Supplied version notice.', paper.bibliographic_notes)
            handle = next(row for row in known.source_handles if row.paper_id == linked.id)
            self.assertEqual(handle.metadata['bibliography'], bibliographic_details(paper))

    def test_projection_assembly_and_bibtex_use_the_same_accepted_fields(self):
        context, memory, bundle, note, _ = self.inputs()
        original = copy.deepcopy((context.model_dump(), memory.model_dump(), bundle.to_handoff_dict()))
        memory.source_handles.append(SourceHandle(handle="extra", kind="material"))
        # A planner and a later writer may fill different fields for one source.
        notes = [{**note, "bibliographic_fields": [field]} for field in note["bibliographic_fields"]]
        projected, saved = apply_report_bibliography(context, memory, documents=bundle, notes=notes)
        paper = Paper.from_row(projected.papers[0])
        self.assertEqual(paper.authors, ["Ada Example", "Bo Example"])
        self.assertEqual(projected.source_handles[0].title, "A supplied study")
        self.assertEqual(saved.source_handles[-1].handle, "extra")
        self.assertIn("Ada Example, Bo Example (2025)", references_markdown([paper]))
        self.assertIn("title = {A supplied study}", papers_to_bibtex([paper]))
        self.assertEqual(projected.source_handles[0].metadata["authors"], paper.authors)
        self.assertEqual(context.model_dump(), original[0])
        self.assertEqual(bundle.to_handoff_dict(), original[2])
        self.assertEqual(projected.source_handles[0].metadata["bibliography"]["verification_status"], "not_independently_verified")
        partial, partial_memory = context, memory
        for proposal in notes:
            partial, partial_memory = apply_report_bibliography(partial, partial_memory,
                documents=bundle, notes=[proposal])
        self.assertEqual(partial.papers, projected.papers)
        self.assertEqual(partial.source_handles[0].metadata["bibliographic_sources"],
                         projected.source_handles[0].metadata["bibliographic_sources"])
        # A registered execution package keeps its typed roles when reused;
        # it must not become this writing task's own execution or paper.
        import tempfile
        from pathlib import Path
        from simple_ar.report.narrative import _prompt_handle_view, review_source_evidence
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.json"
            payload = {"schema_version": "report_experiment_evidence.v1", "records": {
                "experiment_plan": {"hypothesis": "A configured comparison"},
                "results": {"execution_record": {"invocation_id": "saved-run", "duration_sec": 12.5},
                            "implementation": {"method_validation": {"status": "not_checked"}}}}}
            path.write_text(json.dumps(payload), encoding="utf-8")
            bundle.records.append(DocumentRecord(document_id="observations", title="Saved run",
                source="local_files", source_id=str(path), local_path=str(path), extraction_status="parsed"))
            bundle.chunks.append(TextChunk("observed", "observations", json.dumps(payload)))
            projected, _ = build_material_report_inputs(topic="Explain saved observations", documents=bundle,
                documents_ref=ArtifactRef("documents.json"), assets=[])
            view = _prompt_handle_view(next(row for row in projected.source_handles if row.paper_id == "observations"))
            evidence = view["metadata"]["recorded_execution_evidence"]
            self.assertEqual(evidence["declared_protocol"]["hypothesis"], "A configured comparison")
            self.assertEqual(evidence["execution_records"][0]["duration_sec"], 12.5)
            self.assertEqual(review_source_evidence([view])[0], view)
            self.assertEqual(projected.results["session_execution"], "not_requested")
            self.assertNotIn("observations", [row["id"] for row in projected.papers])

    def test_foreign_unsupported_and_recorded_fields_cannot_be_promoted(self):
        context, memory, bundle, note, _ = self.inputs()
        bad = copy.deepcopy(note)
        bad["bibliographic_fields"][0]["value"] = "Another study"
        bad["bibliographic_fields"][1]["section_id"] = "foreign"
        bad["bibliographic_fields"][2]["value"] = "2024"
        projected, _ = apply_report_bibliography(context, memory, documents=bundle, notes=[bad])
        self.assertEqual(projected.papers, context.papers)
        context.papers[0]["authors"] = ["Recorded author"]
        projected, _ = apply_report_bibliography(context, memory, documents=bundle, notes=[note])
        self.assertEqual(projected.papers[0]["authors"], ["Recorded author"])

    def test_front_matter_is_bounded_and_only_registered_papers_are_requested(self):
        context, _, bundle, _, _ = self.inputs()
        bundle.sections[0] = DocumentSection("front", "doc", "front_matter", "Front matter", "x" * 30000)
        views = bibliography_planning_views(context, bundle)
        self.assertEqual(len(views[0]["text"]), 12000)
        self.assertTrue(views[0]["truncated"])
        self.assertIn("title", views[0]["missing_fields"])
        self.assertIn("published", views[0]["missing_fields"])
        self.assertNotIn("year", views[0]["missing_fields"])
        self.assertEqual(bibliography_planning_views(context, None), [])
        context.papers = []
        self.assertEqual(bibliography_planning_views(context, bundle), [])

    def test_survey_planning_also_uses_one_source_and_output_object(self):
        context, memory, bundle, _, _ = self.inputs()
        config = ReportRuntimeConfig(template="survey", outline_strategy="adaptive")
        template = load_report_template_bundle(report_mode="research_only", config=config)
        prompt = _outline_planner_prompt(context=context, memory=memory, config=config,
            template=template, source_front_matter=bibliography_planning_views(context, bundle))
        payload = json.loads(prompt.split("\n\n", 1)[1])
        self.assertIn("source_notes", payload["output_schema"])
        self.assertIn("sections", payload["output_schema"])
        self.assertEqual(payload["source_front_matter"][0]["paper_id"], "doc")

    def test_planner_fields_survive_first_writer_failure_without_a_second_plan(self):
        context, memory, bundle, note, response = self.inputs()
        config = ReportRuntimeConfig(template="source_review", outline_strategy="adaptive", max_review_iterations=0)
        template = load_report_template_bundle(report_mode=context.report_mode, config=config)
        gateway = ReportToolGateway(context, documents=bundle)
        gateway.call_counts["get_paper_brief"] = 2
        client, checkpoints = Mock(), []
        client.ask_json.return_value = response
        with patch("simple_ar.report.agent._draft_section_with_recovery", side_effect=LLMError("First writer outage")):
            with self.assertRaises(LLMError):
                run_report_agent(client=client, context=context, memory=memory, template=template,
                    config=config, gateway=gateway, checkpoint_sink=checkpoints.append)
        self.assertEqual(client.ask_json.call_count, 1)
        self.assertIn("source_front_matter", client.ask_json.call_args.args[1])
        prompt = client.ask_json.call_args.args[1]
        payload = json.loads(prompt.split("\n\n", 1)[1])
        self.assertIn("source_notes", payload["output_schema"])
        self.assertIn("sections", payload["output_schema"])
        self.assertEqual(payload["source_front_matter"][0]["paper_id"], "doc")
        saved = checkpoints[-1]
        self.assertEqual(saved["memory"]["outline_planning"]["source_notes"], [note])
        self.assertEqual(saved["sections"], [])
        self.assertEqual(context.papers[0]["title"], "paper input")
        self.assertEqual(gateway.context.papers[0]["title"], "A supplied study")
        self.assertEqual(gateway.call_counts["get_paper_brief"], 2)
        labels = []
        class ResumeClient:
            def ask_json(self, system, prompt, *, label="", **unused):
                labels.append(label)
                self_test.assertIn("A supplied study", prompt)
                if "reviewer" in label:
                    return {"verdict": "pass"}
                view = json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])[0]
                return {"section_id": view["section"]["section_id"], "draft_markdown": "A bounded source result."}
        self_test = self
        with patch("simple_ar.report.agent._maybe_adapt_outline", side_effect=AssertionError("No second plan")):
            resumed = run_report_agent(client=ResumeClient(), context=context, memory=memory, template=template,
                config=config, gateway=ReportToolGateway(context, documents=bundle), completed_checkpoint=saved)
        self.assertEqual(len(resumed.sections), 2)
        self.assertTrue(all("outline" not in label for label in labels))
        # Template-only writing must retain the same bibliography responsibility
        # in joint, single-section and format-recovery paths, without a new call.
        for scope in ("document", "section"):
            with self.subTest(scope=scope):
                context, memory, bundle, note, _ = self.inputs()
                memory.section_plan.append(ReportSectionPlan(section_id="limits", heading="Limits", goal="Qualify"))
                config = ReportRuntimeConfig(template="source_review", outline_strategy="template",
                    draft_scope=scope, review_scope="document", document_review=True, max_review_iterations=0)
                calls, snapshots = [], []
                class TemplateClient:
                    def ask_json(self, system, prompt, *, label="", **unused):
                        calls.append(label)
                        view = json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])[0]
                        if "reviewer" in label:
                            self_test.assertIn("A supplied study", prompt)
                            self_test.assertIn("Ada Example", prompt)
                            return {"section_reviews": []}
                        self_test.assertNotIn("outline", label)
                        if len(calls) == 1:
                            self_test.assertIn("source_front_matter", view)
                            return {"source_notes": [{**note, "title": "REJECTED"}]}  # No valid body.
                        if len(calls) == 2:
                            self_test.assertIn("source_front_matter", view)
                        rows = view.get("sections", [{"section": view.get("section")}])
                        drafts = [{"section_id": row["section"]["section_id"],
                                   "draft_markdown": "A bounded observation."} for row in rows]
                        return {**({"sections": drafts} if "sections" in view else drafts[0]),
                                "source_notes": [note]}
                template = load_report_template_bundle(report_mode=context.report_mode, config=config)
                gateway = ReportToolGateway(context, documents=bundle)
                result = run_report_agent(client=TemplateClient(), context=context, memory=memory,
                    template=template, config=config, gateway=gateway, checkpoint_sink=snapshots.append)
                self.assertEqual(result.memory.outline_planning["source_notes"], [note])
                self.assertEqual(gateway.context.papers[0]["title"], "A supplied study")
                self.assertEqual(context.papers[0]["title"], "paper input")
                projected, _ = apply_report_bibliography(context, result.memory, documents=bundle,
                    notes=result.memory.outline_planning["source_notes"])
                self.assertEqual(projected.papers[0]["authors"], ["Ada Example", "Bo Example"])
                count = len(calls)
                run_report_agent(client=TemplateClient(), context=context, memory=memory,
                    template=template, config=config, gateway=ReportToolGateway(context, documents=bundle),
                    completed_checkpoint=snapshots[-1])
                self.assertEqual(len(calls), count)
