"""Use the report Writer for a document work unit's observed materials."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any

from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
from simple_ar.report.projection import (
    apply_report_bibliography, build_material_report_inputs, selected_material_window,
)
from simple_ar.report.schema import (
    ReportContext, ReportRuntimeConfig, ReportTemplateBundle, SourceHandle, finding_requires_resolution,
)
from simple_ar.research.contracts import TextChunk
from simple_ar.research.documents.ingest import DocumentBundle


def _observed_documents(observations: list[dict]) -> DocumentBundle:
    """Join selected read windows, preserving the exact excerpts actually seen."""
    records, chunks, sources = {}, [], {}
    for observation in observations:
        if observation['kind'] == 'material_window':
            request, view = observation['observation']['request'], observation['observation']['data']
            selection = {key: view[key] for key in ('size_bytes', 'next_offset_bytes', 'has_more', 'start_line') if key in view}
            selection['offset_bytes'] = view.get('offset_bytes', request.get('offset_bytes', 0))
            record, chunk = selected_material_window(document_id=observation['id'],
                path=request['path'], view=view, selection=selection, start_line=request.get('start_line'))
            records[record.document_id] = record
            chunks.append(chunk)
        elif observation['kind'] == 'source_passages':
            data = observation['data']
            path = data['document_path']
            if path not in sources:
                sources[path] = DocumentBundle.from_handoff_dict(json.loads(Path(path).read_text(encoding='utf-8')))
            original = {row.document_id: row for row in sources[path].records}
            for number, match in enumerate(data.get('matches', []), 1):
                source = original[match['document_id']]
                document_id = f"source-{list(sources).index(path) + 1}-{source.document_id}"
                if document_id not in records:
                    metadata = {'evidence_role': 'selected_source_passages_not_independently_verified',
                        'fulltext_extraction': {'coverage': {'content_scope': 'selected_passages', 'truncated': True}}}
                    if source.is_original_source:
                        metadata['paper_id'] = source.metadata.get('paper_id') or source.document_id
                        if source.metadata.get('bibliographic_notes'):
                            metadata['bibliographic_notes'] = source.metadata['bibliographic_notes']
                    records[document_id] = replace(source, document_id=document_id, local_path=None,
                        abstract='', metadata=metadata)
                chunks.append(TextChunk(chunk_id=f"{observation['id']}-{number}", document_id=document_id,
                    text=match['text'], source_path=match.get('source_path'), page=match.get('page'),
                    line_start=match.get('line_start'), line_end=match.get('line_end'),
                    metadata={'observed_window': {key: match[key] for key in
                        ('chunk_id', 'character_start', 'character_end', 'total_characters', 'truncated') if key in match},
                        **{key: match[key] for key in ('section', 'section_id', 'heading') if key in match}}))
    return DocumentBundle(list(records.values()), {}, {}, [], chunks)


def write_observed_report(*, store: ArtifactStore, identity: str, requirement: dict,
                          title: str, goal: str, work_context: dict, observations: list[dict],
                          attachments: list[str], prior_draft: str, client: Any) -> dict:
    """Draft, review and assemble using the existing Writer and shared ledger client."""
    from simple_ar.report.capability import ReportAssemblyRequest, run_report_capability
    from simple_ar.report.assembler import ReportAssemblyMode
    from simple_ar.report.memory import initialize_report_memory
    from simple_ar.report.ports import DeterministicFigureRenderer
    from simple_ar.report.writing import ReportWritingRequest, retained_writing_client, run_report_writing_capability

    documents = _observed_documents(observations)
    documents_ref = store.write_json('documents.json', documents.to_handoff_dict(),
        kind='documents', schema='document_bundle.v1')
    if documents.chunks:
        context, projected = build_material_report_inputs(topic=title, documents=documents,
            documents_ref=documents_ref, assets=[], selected_evidence=True, max_selected_characters=None)
    else:
        context, projected = ReportContext(topic=title, report_mode='supplied_materials'), None
    context.goal_markdown = json.dumps({'goal': goal, 'work_context': work_context,
        'available_attachments': attachments}, ensure_ascii=False)
    if prior_draft:
        context.source_handles.append(SourceHandle(handle='prior_draft', kind='prior_draft',
            title='Previous version', summary='Earlier prose to revise, not primary evidence.',
            metadata={'evidence_role': 'prior_draft_not_primary_evidence', 'draft_markdown': prior_draft}))
    config = ReportRuntimeConfig.model_validate(requirement['config'])
    template = ReportTemplateBundle.model_validate(requirement['template'])
    memory = initialize_report_memory(context=context, template=template)
    if projected is not None:
        memory.limitations.extend(projected.limitations)
        memory.key_decisions.extend(projected.key_decisions)
        memory.claims_evidence_matrix.extend(projected.claims_evidence_matrix)
    writer = ArtifactStore(store.root / 'writer')
    if store.exists('report-writer-result.json'):
        completed = store.read_json('report-writer-result.json')
    else:
        writer_client = (retained_writing_client(store=writer, identity=identity, client=client,
            contract='atomic_complete.v2') if requirement['method'] == 'sections' else client)
        resume = (store.ref('writer/sections.json', kind='report_checkpoint', schema='report_checkpoint.v1')
            if writer.exists('sections.json') else None)
        result = run_report_writing_capability(context=CapabilityContext(writer, AttemptManifest(identity),
            inputs=(documents_ref, *((resume,) if resume else ())), input_store=store), request=ReportWritingRequest(report_context=context,
                memory=memory, config=config, template=template, llm_client=writer_client,
                method=requirement['method'], prior_draft=prior_draft, output_outline=requirement['outline'], resume_ref=resume))
        writer_ref = next((row for row in reversed(result.artifacts) if row.kind == 'report_writer_result'), None)
        if writer_ref is None:
            raise ValueError('Report writing has no complete candidate; retain its checkpoint: ' + '; '.join(result.diagnostics))
        completed = {'path': writer_ref.path, 'status': result.status, 'diagnostics': list(result.diagnostics)}
        store.write_json('report-writer-result.json', completed)
    written = writer.read_json(completed['path'])
    if not written.get('sections'):
        raise ValueError('Report writing produced no usable section; retain its checkpoint')
    from simple_ar.report.schema import ReportMemory
    memory = ReportMemory.model_validate(written['memory'])
    gaps = [str(row.message or row.type) for row in memory.reviewer_findings if finding_requires_resolution(row)]
    if completed['status'] != 'completed':
        gaps.extend(completed['diagnostics'] or ['Report writing or its review is incomplete.'])
    context, memory = apply_report_bibliography(context, memory, documents=documents,
        notes=memory.outline_planning.get('source_notes', []))
    assembly = ArtifactStore(store.root / 'assembly')
    mode = ReportAssemblyMode.WHOLE_ARTICLE if requirement['method'] == 'article' else ReportAssemblyMode.SECTION_DRAFTS
    planned = memory.document_plan.sections if memory.document_plan and mode == ReportAssemblyMode.WHOLE_ARTICLE else memory.section_plan
    assembled = run_report_capability(context=CapabilityContext(assembly, AttemptManifest(identity)),
        request=ReportAssemblyRequest(title=title, sections=tuple(written['sections']), config=config,
            document_plan=memory.document_plan, papers=tuple(context.papers),
            citation_key_map=context.citation_key_map, assembly_mode=mode),
        figure_renderer=DeterministicFigureRenderer(legacy=False,
            section_headings={row.section_id: row.heading for row in planned}))
    if assembled.status != 'completed':
        gaps.extend(assembled.diagnostics or ('Report assembly is incomplete.',))
    removed_citations = assembly.read_json('citation_cleanup.json')['removed_citations']
    if removed_citations:
        gaps.append('Unresolved citation references were removed during assembly: '
            + ', '.join(map(str, removed_citations)))
    outputs = ['references.bib', 'citation_map.json']
    outputs.extend(row.path for row in assembled.artifacts
        if row.kind == 'figure' and row.status == 'available')
    return {'markdown': assembly.read_text('report_body.md'), 'gaps': gaps,
        'outputs': list(dict.fromkeys(outputs)), 'writer_result': completed['path']}
