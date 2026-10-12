"""Publication metadata consistency and delivery, without identity certification."""
import unittest
import tempfile

from pathlib import Path
from simple_ar.literature.bibtex import papers_to_bibtex
from simple_ar.literature.models import Paper, bibliographic_details
from simple_ar.literature.openalex_client import _paper_from_work
from simple_ar.report.agent import _prompt_handle_view, _writer_prompt, _reviewer_prompt
from simple_ar.report.citations import references_markdown, citation_map_artifact
from simple_ar.report.projection import _paper_source_handles, attach_report_read_evidence
from simple_ar.report.schema import ReportContext, ReportMemory, ReportRuntimeConfig, ReportSectionPlan, ReportSectionDraft, ReportToolCall, SourceHandle
from simple_ar.report.templates import load_report_template_bundle
from simple_ar.report.tool_gateway import ReportToolGateway
from simple_ar.research.contracts import SourcePlan, DocumentRecord, DocumentSection
from simple_ar.research.documents.ingest import DocumentBundle
from simple_ar.research.documents.records import build_document_records
from simple_ar.research.documents.sections import build_document_sections
from simple_ar.research.store.chunking import build_text_chunks
from simple_ar.research.sources.capability import provided_materials_result
from simple_ar.core.capabilities import ArtifactRef
from simple_ar.research.evidence.reader import ReadResult
from simple_ar.report.audit import build_report_audit


class BibliographicDeliveryTests(unittest.TestCase):
    def local_title_projection(self, *, recorded_title='paper input', proposed='A supplied study',
                               header='A supplied\nstudy\nExample Authors', header_document='doc', metadata=None):
        record = DocumentRecord('doc', recorded_title, 'local_files', source_id='/private/paper_input.pdf',
                                extraction_status='parsed', metadata=metadata or {})
        bundle = DocumentBundle([record], {}, {}, [DocumentSection('header', header_document, 'front_matter',
                                                                  'Front matter', header)], [])
        search = provided_materials_result(bundle.records)
        handles = _paper_source_handles(search)
        context = ReportContext(topic='Review', report_mode='research_only',
                                papers=[paper.to_row() for paper in search.selected_papers], source_handles=handles)
        memory = ReportMemory(objective='Review', report_mode='research_only', source_handles=handles)
        read = ReadResult(status='completed', bundle=bundle, paper_notes=({'paper_id': 'doc', 'title': proposed},))
        projected, projected_memory = attach_report_read_evidence(context, memory, documents=bundle, read=read,
            read_ref=ArtifactRef('read.json', schema='read_result.v1'))
        return context, record, projected, projected_memory

    def test_filename_title_uses_same_source_reading_proposal_without_guessing_bibliography(self):
        original, record, projected, memory = self.local_title_projection()
        paper = Paper.from_row(projected.papers[0])
        self.assertEqual(paper.title, 'A supplied study')
        self.assertEqual(memory.source_handles[0].title, paper.title)
        self.assertEqual(projected.source_handles[0].metadata['title_source']['section_id'], 'header')
        self.assertEqual(_prompt_handle_view(projected.source_handles[0])['metadata']['title_source']['quote'], paper.title)
        bibliography = projected.source_handles[0].metadata['bibliography']
        self.assertEqual(bibliography['verification_status'], 'not_independently_verified')
        self.assertEqual(bibliography['authors'], [])
        self.assertEqual(bibliography['year'], '')
        self.assertEqual(bibliography['doi'], '')
        self.assertIn('A supplied study', references_markdown([paper]))
        self.assertIn('title = {A supplied study}', papers_to_bibtex([paper]))
        self.assertEqual(citation_map_artifact({'doc': 1}, [paper])['entries'][0]['bibliography']['title'], paper.title)
        self.assertNotIn('/private/', references_markdown([paper]))
        self.assertEqual(original.papers[0]['title'], 'paper input')
        self.assertEqual(record.title, 'paper input')

    def test_title_proposal_cannot_overwrite_recorded_metadata_or_use_other_source(self):
        for kwargs in ({'recorded_title': 'User supplied title'}, {'metadata': {'paper_id': 'provider-paper'}},
                       {'header_document': 'foreign'}, {'header': 'No matching title'},
                       {'proposed': 'Invented title'}, {'proposed': 'x' * 241}):
            with self.subTest(kwargs=kwargs):
                original, record, projected, _ = self.local_title_projection(**kwargs)
                self.assertEqual(projected.papers, original.papers)
                self.assertNotIn('title_source', projected.source_handles[0].metadata)

    def paper(self, **updates):
        values = dict(id='source-1', title='A documented method', authors=['Ada Example', 'Grace Example'],
                      abstract='Source abstract.', url='https://example.org/paper', published='2024-03-04',
                      source='openalex', source_id='W1', doi='10.1234/example')
        return Paper(**{**values, **updates})

    def records(self, paper):
        return build_document_records(papers=[paper], source_plan=SourcePlan(queries=['test'], sources=['openalex']))

    def test_publication_fields_survive_document_checkpoint_and_provided_projection(self):
        paper = self.paper()
        records = self.records(paper)
        recovered = DocumentBundle.from_handoff_dict(DocumentBundle(records, {}, {}, [], []).to_handoff_dict())
        projected = provided_materials_result(recovered.records).selected_papers[0]
        self.assertEqual(projected.authors, paper.authors)
        self.assertEqual(projected.doi, paper.doi)
        self.assertEqual(projected.published, paper.published)
        self.assertEqual(projected.source, paper.source)
        self.assertEqual(bibliographic_details(projected)['verification_status'], 'not_independently_verified')

    def test_readable_references_bibtex_and_map_share_available_metadata(self):
        paper = self.paper()
        refs = references_markdown([paper], {paper.id: 1})
        bibtex = papers_to_bibtex([paper])
        metadata = citation_map_artifact({paper.id: 1}, [paper])['entries'][0]['bibliography']
        self.assertIn('Ada Example, Grace Example (2024)', refs)
        self.assertIn('DOI: 10.1234/example', refs)
        self.assertIn('author = {Ada Example and Grace Example}', bibtex)
        self.assertIn('year = {2024}', bibtex)
        self.assertIn('doi = {10.1234/example}', bibtex)
        self.assertEqual(metadata['missing_fields'], [])
        self.assertEqual(metadata['verification_status'], 'not_independently_verified')
        for title in ('Métadonnées & 50% #1', 'https://example.org/paper.pdf?part=a_b&view=2#results'):
            with self.subTest(title=title):
                paper = self.paper(title=title, authors=['René Example'])
                bibliography = papers_to_bibtex([paper])
                self.assertIn(r'\&', bibliography)
                self.assertIn('author = {René Example}', bibliography)
                self.assertIn('url = {https://example.org/paper}', bibliography)
                self.assertEqual(citation_map_artifact({paper.id: 1}, [paper])['entries'][0]['title'], title)
                self.assertIn(title, references_markdown([paper]))
                self.assertNotIn('key =', bibliography)

    def test_missing_metadata_is_visible_without_publishing_local_locations(self):
        paper = self.paper(source='local_files', authors=[], published=None, doi=None,
                           url='/private/user/source.pdf', source_id='/private/user/source.pdf')
        refs = references_markdown([paper])
        bibtex = papers_to_bibtex([paper])
        artifact = citation_map_artifact({paper.id: 1}, [paper])
        self.assertIn('Bibliographic details unavailable: authors, year, public URL or DOI', refs)
        self.assertNotIn('/private/user', str((refs, bibtex, artifact)))
        self.assertNotIn('author =', bibtex)
        self.assertNotIn('year =', bibtex)
        self.assertIn('key = {source-1}', bibtex)
        self.assertEqual(paper.source_id, '/private/user/source.pdf')  # Original provenance is untouched.

    def test_unrecognized_date_is_not_reinterpreted_as_a_year(self):
        paper = self.paper(published='in press')
        self.assertEqual(bibliographic_details(paper)['published'], 'in press')
        self.assertIn('year', bibliographic_details(paper)['missing_fields'])
        self.assertNotIn('year =', papers_to_bibtex([paper]))
        self.assertNotIn('(in p)', references_markdown([paper]))
        self.assertEqual(bibliographic_details(self.paper(published='2025'))['year'], '2025')

    def test_local_source_retains_a_known_public_locator_in_reader_references(self):
        paper = self.paper(source='local_files', source_id='/private/user/source.pdf',
                           url='https://example.org/paper')
        references = references_markdown([paper])
        self.assertIn('https://example.org/paper', references)
        self.assertIn('supplied local document', references)
        self.assertNotIn('/private/user', references)

    def test_written_month_dates_are_calendar_checked_without_rewriting_source(self):
        for value in ('8 May 2024', '8 Nov 2024', 'November 8, 2024', 'Nov 8, 2024'):
            with self.subTest(value=value):
                paper = self.paper(published=value)
                self.assertEqual(bibliographic_details(paper)['year'], '2024')
                self.assertEqual(paper.published, value)
                self.assertIn('year = {2024}', papers_to_bibtex([paper]))
        for value in ('31 February 2024', '08/05/2024', 'published in 2024', 'May 2024'):
            self.assertEqual(bibliographic_details(self.paper(published=value))['year'], '')

    def test_bibliographic_metadata_is_not_inferred_from_abstract(self):
        paper = self.paper(authors=[], published=None, doi=None,
                           abstract='A 2023 study by Someone at Example Conference, DOI 10.5678/guess.')
        details = bibliographic_details(paper)
        self.assertEqual(details['authors'], [])
        self.assertEqual(details['year'], '')
        self.assertEqual(details['doi'], '')
        self.assertNotIn('Someone', references_markdown([paper]))

    def test_writer_and_reviewer_share_bounded_unverified_metadata(self):
        paper = self.paper(authors=[f'Author {i}' for i in range(10)])
        search = provided_materials_result(self.records(paper))
        handle = _paper_source_handles(search)[0]
        projection = _prompt_handle_view(handle)['metadata']['bibliography']
        self.assertEqual(projection['author_names_omitted_from_prompt'], 4)
        self.assertEqual(projection['recorded_authors_count'], 10)
        self.assertEqual(len(projection['authors']), 6)
        self.assertEqual(projection['doi'], paper.doi)
        self.assertEqual(projection['verification_status'], 'not_independently_verified')
        context = ReportContext(topic='Describe the existing method', report_mode='source_review',
                                source_handles=[handle], papers=[paper.to_row()])
        memory = ReportMemory(source_handles=[handle])
        section = ReportSectionPlan(section_id='method', heading='Method', goal='Describe', evidence_handles=[handle.handle])
        config = ReportRuntimeConfig(template='source_review')
        template = load_report_template_bundle(report_mode=context.report_mode, config=config)
        common = dict(context=context, template=template, memory=memory, section=section)
        writer = _writer_prompt(**common, config=config, extra_context=[], source_batch_index=1, source_batch_count=1,
                                previous_draft=None, review=None, include_previous_draft=False, draft_mode='first_draft')
        reviewer = _reviewer_prompt(**common, draft=ReportSectionDraft(section_id='method', heading='Method', draft_markdown='Source account.'))
        for prompt in (writer, reviewer):
            self.assertIn('not_independently_verified', prompt)
            self.assertIn('author_names_omitted_from_prompt', prompt)
            self.assertIn('omission here is not a provider completeness finding', prompt)
            self.assertIn('10.1234/example', prompt)

    def test_provider_author_limit_is_retained_without_inventing_remaining_authors(self):
        paper = _paper_from_work({'id': 'https://openalex.org/W1', 'title': 'Large collaboration',
            'authorships': [{'author': {'display_name': f'Author {i}'}} for i in range(100)]})
        restored = provided_materials_result(self.records(paper)).selected_papers[0]
        self.assertEqual(restored.bibliographic_notes, paper.bibliographic_notes)
        self.assertIn('may be incomplete', bibliographic_details(restored)['notes'][0])
        refs = references_markdown([restored])
        self.assertIn('Author 5, et al.', refs)
        self.assertNotIn('Author 99', refs)
        self.assertIn('Author 99', papers_to_bibtex([restored]))
        self.assertIn('note =', papers_to_bibtex([restored]))
        self.assertEqual(Paper.from_row(paper.to_row()).bibliographic_notes, paper.bibliographic_notes)

    def test_old_paper_rows_without_metadata_notes_still_load(self):
        row = self.paper().to_row()
        del row['bibliographic_notes']
        self.assertEqual(Paper.from_row(row).bibliographic_notes, [])

    def test_preamble_is_retained_as_source_not_guessed_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.txt'
            path.write_text('Unverified version notice 2025\nNamed Author\nAbstract\nEvidence only.\nMethods\nA bounded procedure.', encoding='utf-8')
            record = DocumentRecord('doc', 'Recorded source', 'local_files', local_path=str(path), extraction_status='parsed')
            sections = build_document_sections([record])
            self.assertEqual(sections[0].section, 'front_matter')
            self.assertEqual((sections[0].line_start, sections[0].line_end), (1, 2))
            self.assertIn('Named Author', sections[0].text)
            self.assertEqual(record.authors, [])
            self.assertIsNone(record.published)
            chunks = build_text_chunks([record], sections=sections)
            self.assertTrue(any('Named Author' in row.text for row in chunks))
            self.assertEqual(build_text_chunks([record], sections=sections, max_chunks=1)[0].text, 'Evidence only.')
            self.assertTrue(any('Named Author' in row.text for row in build_text_chunks([record], sections=sections, max_chunks=3)))

    def test_no_front_matter_is_invented_before_an_initial_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.txt'
            path.write_text('Abstract\nEvidence only.', encoding='utf-8')
            record = DocumentRecord('doc', 'Source', 'local_files', local_path=str(path), extraction_status='parsed')
            self.assertEqual([row.section for row in build_document_sections([record])], ['abstract'])

    def test_brief_reads_saved_header_despite_chunk_cap_and_deleted_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.txt'
            path.write_text('Recorded preprint v2 2024\nAda Example\nAbstract\nEvidence only.', encoding='utf-8')
            record = DocumentRecord('doc', 'Recorded title', 'local_files', local_path=str(path), extraction_status='parsed')
            sections = build_document_sections([record])
            bundle = DocumentBundle([record], {}, {}, sections, build_text_chunks([record], sections=sections, max_chunks=1))
            restored = DocumentBundle.from_handoff_dict(bundle.to_handoff_dict())
            path.unlink()  # Only the owned fixture: the tool must not depend on this path.
            handle = SourceHandle(handle='paper:doc', kind='paper', paper_id='doc', title='Same title', citation_key='P1')
            context = ReportContext(topic='Check attribution', report_mode='source_review', source_handles=[handle])
            result = ReportToolGateway(context, documents=restored).call(ReportToolCall(
                tool_name='get_paper_brief', arguments={'citation_key': 'P1'}))
            header = result.content['source_front_matter'][0]
            self.assertEqual(header['status'], 'available')
            self.assertEqual(header['identity_verification'], 'not_performed')
            self.assertIn('Ada Example', header['passages'][0]['text'])
            self.assertFalse(header['passages'][0]['truncated'])
            self.assertEqual(restored.chunks[0].text, 'Evidence only.')

    def test_brief_header_is_source_scoped_and_missing_legacy_header_not_fabricated(self):
        from simple_ar.research.contracts import DocumentSection
        record = DocumentRecord('other', 'Same title', 'local_files')
        section = DocumentSection('other-header', 'other', 'front_matter', 'Front matter', 'Different Author')
        bundle = DocumentBundle([record], {}, {}, [section], [])
        handle = SourceHandle(handle='paper:doc', kind='paper', paper_id='doc', title='Same title')
        context = ReportContext(topic='Check attribution', report_mode='source_review', source_handles=[handle])
        for documents in (bundle, None):
            result = ReportToolGateway(context, documents=documents).call(ReportToolCall(
                tool_name='get_paper_brief', arguments={'handle': handle.handle}))
            header = result.content['source_front_matter'][0]
            self.assertEqual(header['status'], 'not_retained')
            self.assertEqual(header['passages'], [])
            self.assertNotIn('Different Author', str(result.content))

    def test_brief_header_output_is_bounded_without_changing_stored_source(self):
        from simple_ar.research.contracts import DocumentSection
        header = DocumentSection('header', 'doc', 'front_matter', 'Front matter', 'Author ' * 1000)
        bundle = DocumentBundle([], {}, {}, [header], [])
        handle = SourceHandle(handle='paper:doc', kind='paper', paper_id='doc', title='Source')
        result = ReportToolGateway(ReportContext(topic='Check attribution', report_mode='source_review', source_handles=[handle]), documents=bundle).call(
            ReportToolCall(tool_name='get_paper_brief', arguments={'paper_id': 'doc'}))
        passage = result.content['source_front_matter'][0]['passages'][0]
        self.assertEqual(len(passage['text']), 1600)
        self.assertTrue(passage['truncated'])
        self.assertEqual(passage['total_characters'], len(header.text))
        self.assertEqual(header.text, 'Author ' * 1000)

    def test_header_budget_omission_is_not_reported_as_missing_source(self):
        from simple_ar.research.contracts import DocumentSection
        handles = [SourceHandle(handle=f'source:{i}', kind='paper', paper_id='p',
                                metadata={'document_id': f'doc-{i}'}) for i in range(4)]
        sections = [DocumentSection(f'header-{i}', f'doc-{i}', 'front_matter', 'Front matter', 'x' * 2000)
                    for i in range(4)]
        gateway = ReportToolGateway(ReportContext(topic='Attribution', report_mode='source_review', source_handles=handles),
                                    documents=DocumentBundle([], {}, {}, sections, []))
        result = gateway.call(ReportToolCall(tool_name='get_paper_brief', arguments={'paper_id': 'p'}))
        headers = result.content['source_front_matter']
        self.assertEqual(sum(len(p['text']) for row in headers for p in row['passages']), 4800)
        self.assertEqual(headers[-1]['status'], 'omitted_from_tool_window')
        self.assertEqual(headers[-1]['omitted_sections'], 1)


class BibliographicConsistencyTests(unittest.TestCase):
    def paper(self, **changes):
        values = dict(id='p', title='Recorded study', authors=['Recorded Author'], abstract='',
            published='2024-02-29', source='arxiv', source_id='2404.15018v1',
            url='https://arxiv.org/abs/2404.15018v1')
        return Paper(**{**values, **changes})

    def test_impossible_date_is_not_exported_as_a_known_year(self):
        for date in ('2023-02-29', '2024-02-30', '2024-13', '0000', '2024-00-01'):
            with self.subTest(date=date):
                paper = self.paper(published=date)
                details = bibliographic_details(paper)
                self.assertEqual(details['year'], '')
                self.assertTrue(details['consistency_issues'])
                self.assertNotIn('year =', papers_to_bibtex([paper]))
                self.assertEqual(paper.published, date)

    def test_partial_dates_and_unknown_dates_do_not_create_conflicts(self):
        for value, year in (('2024', '2024'), ('2024-02', '2024'),
                            ('2024-02-29', '2024'), ('in press', ''), (None, '')):
            with self.subTest(value=value):
                details = bibliographic_details(self.paper(published=value))
                self.assertEqual(details['year'], year)
                self.assertEqual(details['consistency_issues'], [])

    def test_arxiv_identity_and_explicit_version_conflicts_are_visible(self):
        for changes in ({'url': 'https://arxiv.org/abs/2404.99999v1'},
                        {'fulltext_url': 'https://arxiv.org/pdf/2404.15018v2.pdf'},
                        {'doi': '10.48550/arXiv.2404.99999'},
                        {'url': 'https://doi.org/10.48550/arXiv.2404.99999'},
                        {'fulltext_url': 'doi:10.48550/arXiv.2404.15018v2'}):
            with self.subTest(changes=changes):
                details = bibliographic_details(self.paper(**changes))
                self.assertTrue(details['consistency_issues'])
                self.assertEqual(details['verification_status'], 'not_independently_verified')

    def test_unpinned_versions_and_different_publication_years_are_not_guessed(self):
        for changes in ({'url': 'https://arxiv.org/abs/2404.15018'},
                        {'published': '2026-03-04'},
                        {'source_id': 'arXiv:hep-th/9901001v2',
                         'url': 'https://arxiv.org/pdf/hep-th/9901001v2'}):
            with self.subTest(changes=changes):
                self.assertEqual(bibliographic_details(self.paper(**changes))['consistency_issues'], [])

    def test_doi_wrappers_are_normalized_but_not_verified(self):
        for value in ('doi:10.1234/example', 'https://doi.org/10.1234/example'):
            with self.subTest(value=value):
                paper = self.paper(doi=value)
                details = bibliographic_details(paper)
                self.assertEqual(details['doi'], '10.1234/example')
                self.assertIn('doi = {10.1234/example}', papers_to_bibtex([paper]))
                self.assertEqual(paper.doi, value)
                self.assertEqual(details['verification_status'], 'not_independently_verified')

    def test_doi_subprefix_unicode_and_encoded_punctuation_are_not_rejected(self):
        for value, identifier in (('10.500.100/研究 example', '10.500.100/研究 example'),
            ('doi:10.1234/a%23b', '10.1234/a#b'),
            ('https://doi.org/10.1234/a%2Fb', '10.1234/a/b'),
            ('10.123456789012/example', '10.123456789012/example')):
            with self.subTest(value=value):
                details = bibliographic_details(self.paper(doi=value))
                self.assertEqual(details['doi'], identifier)
                self.assertEqual(details['consistency_issues'], [])

    def test_doi_link_conflict_is_not_silently_resolved(self):
        paper = self.paper(doi='10.1234/one', url='https://doi.org/10.1234/two')
        details = bibliographic_details(paper)
        self.assertTrue(details['consistency_issues'])
        self.assertEqual(details['doi'], paper.doi)
        self.assertEqual(details['url'], paper.url)
        same = self.paper(doi='10.1234/Example', url='https://doi.org/10.1234/example')
        self.assertEqual(bibliographic_details(same)['consistency_issues'], [])

    def test_unrecognized_doi_is_retained_only_as_raw_provenance(self):
        for value in ('unverified identifier', '10/example', '10.١٢٣٤/example', '10.1234/a\tb'):
            with self.subTest(value=value):
                paper = self.paper(doi=value)
                details = bibliographic_details(paper)
                self.assertEqual(details['doi'], '')
                self.assertTrue(details['consistency_issues'])
                self.assertNotIn('doi =', papers_to_bibtex([paper]))
                self.assertEqual(Paper.from_row(paper.to_row()).doi, paper.doi)

    def test_diagnostics_reach_prompts_and_all_reference_consumers(self):
        paper = self.paper(url='https://arxiv.org/abs/2404.15018v2')
        records = build_document_records(papers=[paper], source_plan=SourcePlan(queries=['test'], sources=['arxiv']))
        handle = _paper_source_handles(provided_materials_result(records))[0]
        details = bibliographic_details(paper)
        self.assertTrue(details['consistency_issues'])
        self.assertEqual(_prompt_handle_view(handle)['metadata']['bibliography']['consistency_issues'],
                         details['consistency_issues'])
        self.assertEqual(citation_map_artifact({'p': 1}, [paper])['entries'][0]['bibliography']['consistency_issues'],
                         details['consistency_issues'])
        for issue in details['consistency_issues']:
            self.assertIn(issue, references_markdown([paper]))
            self.assertIn(issue, papers_to_bibtex([paper]))

    def test_audit_warns_for_cited_conflicts_not_unused_or_missing_metadata(self):
        for paper, body, expected in ((self.paper(url='https://arxiv.org/abs/2404.15018v2'), 'Observation [@p].', 'warning'),
            (self.paper(url='https://arxiv.org/abs/2404.15018v2'), 'Other observation [@q].', 'passed'),
            (self.paper(authors=[], published=None, doi=None), 'Observation [@p].', 'passed')):
            with self.subTest(body=body, expected=expected):
                audit = build_report_audit(report=body, report_body=body,
                    context=ReportContext(topic='Study', report_mode='source_review',
                        papers=[paper.to_row(), self.paper(id='q').to_row()]), memory=ReportMemory())
                self.assertEqual(audit.status, expected)
