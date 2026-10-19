"""Native-tool adapters over existing source search and document ingestion."""
from __future__ import annotations

import json
import hashlib
from dataclasses import replace
from pathlib import Path
import re
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from simple_ar.core.artifacts import write_json
from simple_ar.app.research_execution import merge_execution_protocol
from simple_ar.core.budget import BudgetError, BudgetLedger
from simple_ar.core.capabilities import CapabilityContext
from simple_ar.integrations.llm import LLMError
from simple_ar.research.planning.capability import ResearchPlanRequest, ResearchPlanResult, run_research_plan_capability
from simple_ar.research.evidence.reader import ReadRequest, ReadResult, run_read_capability
from simple_ar.research.brief import evidence_pack_from_read
from simple_ar.research.synthesis import SynthesisRequest, SynthesisResult, run_synthesis_capability
from simple_ar.research.assessment import IdeaAssessmentRequest, run_idea_assessment_capability
from simple_ar.research.design import ResearchDesignRequest, run_research_design_capability
from simple_ar.research.summary import render_summary
from simple_ar.research.workflow_contracts import ResearchBrief
from simple_ar.research.contracts import DocumentRecord, SourcePlan
from simple_ar.research.documents.ingest import DocumentBundle, build_document_bundle
from simple_ar.research.store.retrieval import rank_source_chunks, source_chunk_views
from simple_ar.research.preparation_assets import validate_public_url
from simple_ar.research.sources.capability import SearchRequest, SearchResult, SearchSelectionPolicy, search_sources, select_search_result
from simple_ar.research.sources.registry import SearchProviderRegistry, default_search_provider_registry
from simple_ar.tools.specs import CommonToolSpec, ToolResult


class SourcePolicy(BaseModel):
    """Caller-owned source contract, validated before creating a task.

    Explicit queries/providers bound search; materials are the initial selection,
    not a second file-access authority. The workspace owns read authorization.
    Limits apply to backend inputs, never just prompts.
    Network policy is an upper bound, not an access grant; the common gateway
    independently requires actual source-network permission.
    Factories must return clients bound to the parent attempt and ledger.
    """
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    queries: tuple[str, ...] = ()
    providers: tuple[str, ...] = ('arxiv', 'openalex', 'semantic_scholar', 'web')
    materials: tuple[str, ...] = ()
    max_results: int = Field(default=5, ge=1, le=1000)
    max_documents: int = Field(default=20, ge=1, le=1000)
    max_chunks: int | None = Field(default=80, ge=1, le=10000)
    idea_limit: int = Field(default=3, ge=1, le=20)
    cache_enabled: bool = True
    cache_dir: str | None = None
    read_max_shortlist: int = Field(default=20, ge=1, le=1000)
    max_iterations: int = Field(default=1, ge=0, le=100)
    require_fulltext: bool = True
    allow_pdf_download: bool = True
    max_fulltext_documents: int = Field(default=1, ge=0, le=1000)
    max_fulltext_fetch_attempts: int = Field(default=3, ge=0, le=1000)
    max_pdf_mb: int = Field(default=8, ge=1, le=100)
    max_pdf_pages: int = Field(default=60, ge=1, le=10000)
    keep_raw_pdf: bool = True
    parser_backend: Literal['basic', 'unstructured'] = 'basic'
    web_extract_backend: Literal['direct', 'tavily_basic'] = 'direct'
    novelty_backend: Literal['local'] = 'local'
    allow_network: bool = True
    feasibility_connection: str = ''
    require_design: bool = False
    interaction: Literal['assisted', 'checkpoints', 'autonomous'] = 'checkpoints'
    require_summary: bool = False
    selected_idea_id: str | None = None
    execution_schema: dict[str, Any] = Field(default_factory=dict)
    execution_boundary: dict[str, Any] = Field(default_factory=dict)
    entry_facts: dict[str, Any] = Field(default_factory=dict)
    source_workspace: str = ''

    @field_validator('queries', 'providers', 'materials', mode='before')
    @classmethod
    def sequences(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode='after')
    def validate_contract(self) -> SourcePolicy:
        for values in (self.queries, self.providers, self.materials):
            if any(not value.strip() for value in values) or len(set(values)) != len(values):
                raise ValueError('Source policy lists require unique nonempty values')
        if self.max_fulltext_documents > self.max_documents:
            raise ValueError('Fulltext document limit exceeds document limit')
        if self.cache_dir is not None and (not self.cache_dir.strip() or not Path(self.cache_dir).is_absolute()):
            raise ValueError('cache_dir must preserve an explicit absolute cache path')
        # Reject non-JSON execution contracts at admission, without filesystem IO.
        json.dumps(self.model_dump(mode='json'), allow_nan=False)
        return self

    def source_plan(self, *, local_documents: list[str] | None = None) -> SourcePlan:
        return SourcePlan(queries=list(self.queries), sources=list(self.providers),
            max_results_per_query=self.max_results, cache_enabled=self.cache_enabled,
            require_fulltext=(self.require_fulltext and self.max_fulltext_documents > 0
                and self.max_fulltext_fetch_attempts > 0), allow_pdf_download=self.allow_pdf_download,
            local_documents=local_documents or [], budget={
                **{key: getattr(self, key) for key in ('max_documents', 'max_chunks',
                    'max_fulltext_documents', 'max_fulltext_fetch_attempts', 'max_pdf_mb',
                    'max_pdf_pages', 'keep_raw_pdf', 'parser_backend', 'web_extract_backend')},
                'public_network_only': True})


class _Research(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operation: Literal['plan', 'ingest', 'read', 'summary', 'synthesize', 'assess', 'design']
    question: str = Field(min_length=1, max_length=16000)
    materials: list[str] = Field(default_factory=list, max_length=1000)
    document_record: str = ''
    plan_record: str = ''
    read_record: str = ''
    synthesis_record: str = ''
    previous_design: str = ''
    guidance: str = Field(default='', max_length=16000)
    idea_id: str | None = None
    use_llm: bool = False
    source_workspace: str = ''


class _Search(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=2000)
    providers: list[str] = Field(
        default_factory=lambda: ["arxiv", "openalex"], min_length=1, max_length=5)
    max_results: int = Field(default=5, ge=1, le=1000)


class _Acquire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    search_record: str = Field(default="", description="Saved evidence/.../search.json returned by search_sources.")
    paper_id: str = Field(default="", description="Exact ID in that saved search record, not a guessed citation.")
    url: str = Field(default="", description="Explicit public HTTPS document URL; no credentials. May accompany a saved search record and paper ID only when it equals that paper's recorded fulltext_url, preserving its metadata.")
    web_extract_backend: Literal['direct', 'tavily_basic'] | None = Field(default=None,
        description="Omit to retain the current backend. Explicit selection must match any caller-supplied source policy; Tavily requires web provider authority and its credential. No automatic fallback.")


class _Passages(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    document_record: str = Field(description="DocumentBundle record in saved evidence or a supplied read-only source mount.")
    query: str = Field(default="", max_length=2000,
        description="Question for lexical passage lookup; omit to inspect source identity and retained-text coverage without reading the full JSON.")
    limit: int = Field(default=5, ge=1, le=8)
    max_chars: int = Field(default=6000, ge=500, le=12000)


def source_tool_specs() -> list[CommonToolSpec]:
    return [CommonToolSpec(name=name, description=description, input_schema=model.model_json_schema(),
        permission_level=permission, risk_level="medium" if permission == "network" else "low", domain="sources")
        for name, model, permission, description in (
            ("search_sources", _Search, "network", "Search selected scholarly/web providers. Save metadata and failure history; excerpts are not original full text."),
            ("acquire_source", _Acquire, "network", "Acquire one saved search result or explicit public HTTPS source using existing bounded document parsers. Save original evidence and expose readable chunks; retrieval may fail."),
            ('research', _Research, 'read_only', 'Perform the requested professional research operation over authorized material or saved handoffs. No network. Design returns a frozen protocol for caller confirmation; it does not execute experiments.'),
            ('research_collect', _Research, 'network', 'Perform the requested ingest or summary, searching exact policy queries when no materials are supplied. Uses the same bounded source owner; no experiment execution.'),
            ("find_source_passages", _Passages, "read_only", "Query retained text for bounded lexical matches together with source titles, URLs and retained coverage. Omit the query only for identity/coverage inspection; a separate identity lookup is not needed after querying this record. Lexical selection is not exhaustive reading; no new network or model call."))]


def _source_identity(record: DocumentRecord, bundle: DocumentBundle) -> dict[str, Any]:
    """Project observed extraction coverage without certifying paper access."""
    extraction = record.metadata.get('fulltext_extraction') or {}
    coverage = extraction.get('coverage') or {}
    row = {'id': record.document_id, 'title': record.title, 'url': record.url,
           'status': record.extraction_status}
    if extraction.get('hint_kind'):
        row['hint_kind'] = extraction['hint_kind']
    # Older saved bundles retain this qualification only in the acquisition
    # manifest. Use the selected cached resource, never a failed alternative.
    hint = next((hint for document in bundle.fulltext_manifest.get('documents', [])
        if document.get('document_id') == record.document_id
        for hint in document.get('hints', [])
        if record.extraction_status == 'parsed' and hint.get('status') == 'cached'
        and hint.get('local_path')
        and (not extraction.get('source_path') or hint['local_path'] == extraction['source_path'])), {})
    scope = (coverage.get('content_scope') or hint.get('content_scope')
             or hint.get('acquisition', {}).get('content_scope') or record.metadata.get('content_scope'))
    if scope:
        row['content_scope'] = scope
    original_kind = coverage.get('original_hint_kind') or hint.get('original_hint', {}).get('kind')
    if original_kind:
        row['original_hint_kind'] = original_kind
    if not row.get('hint_kind') and hint.get('kind'):
        row['hint_kind'] = hint['kind']
    observed = {key: coverage[key] for key in ('total_pages', 'extracted_pages', 'truncated',
        'notebook_cells', 'extracted_cells', 'cells_with_omitted_outputs', 'unpaired_unicode_replacements')
        if key in coverage}
    if coverage.get('empty_text_pages'):
        observed['empty_text_page_count'] = len(coverage['empty_text_pages'])
    if observed:
        row['coverage'] = observed
    return row


class SourceToolGateway:
    @staticmethod
    def inventory_source(path: Path) -> dict[str, Any] | None:
        """Describe a bound original bundle for professional material selection.

        The caller owns path authorization and inventory digests. This is a
        read-only view of declared provenance, not source verification or a
        second index. Ordinary JSON and oversized files remain ordinary inputs.
        """
        if path.suffix.lower() != '.json' or path.stat().st_size > 8_000_000:
            return None
        try:
            payload = json.loads(path.read_bytes())
        except (ValueError, UnicodeError):
            return None
        if not isinstance(payload, dict) or payload.get('schema_version') != 'document_bundle.v1':
            return None
        bundle = DocumentBundle.from_handoff_dict(payload)
        return {'document_record': str(path),
            'documents': [_source_identity(row, bundle) for row in bundle.records[:20]],
            'documents_truncated': len(bundle.records) > 20,
            'retained_chunks': len(bundle.chunks),
            'retained_preview': source_chunk_views(bundle.chunks[:1], max_chars=1200),
            'scope': 'Declared source identity and bounded retained preview, not verified claims or exhaustive reading.'}

    @staticmethod
    def history_identity(name: str, data: dict[str, Any]) -> dict[str, Any]:
        """Keep acquisition identity, not source prose or a second evidence store.

        A successful adapter receipt can describe metadata-only/failed retrieval.
        Preserve those observed statuses with the bound record so compaction does
        not turn every source into an indistinguishable successful tool call.
        """
        if name != 'acquire_source':
            return {}
        identity = {key: data[key] for key in ('document_record', 'documents', 'text_bytes')
                    if key in data}
        manifest = data.get('acquisition')
        if isinstance(manifest, dict):
            acquisition: dict[str, Any] = {}
            budget = manifest.get('budget')
            if isinstance(budget, dict) and isinstance(budget.get('web_extract_backend'), str):
                acquisition['web_extract_backend'] = budget['web_extract_backend'][:64]
            hints = []
            for document in manifest.get('documents', [])[:20]:
                if not isinstance(document, dict):
                    continue
                for hint in document.get('hints', [])[:8]:
                    if not isinstance(hint, dict) or len(hints) >= 8:
                        continue
                    observed = {key: hint[key][:limit] for key, limit in
                        (('url', 2048), ('status', 64), ('reason', 300), ('kind', 64),
                         ('content_scope', 300)) if isinstance(hint.get(key), str)}
                    original_kind = hint.get('original_hint', {}).get('kind')
                    if isinstance(original_kind, str):
                        observed['original_hint_kind'] = original_kind[:64]
                    provenance = hint.get('acquisition')
                    if isinstance(provenance, dict) and isinstance(provenance.get('provider'), str):
                        observed['provider'] = provenance['provider'][:64]
                    if isinstance(provenance, dict) and isinstance(provenance.get('content_scope'), str):
                        observed.setdefault('content_scope', provenance['content_scope'][:300])
                    if observed:
                        hints.append(observed)
            if hints:
                acquisition['hints'] = hints
            if acquisition:
                identity['acquisition'] = acquisition
        return identity

    def retained_passages(self, observations: list[dict[str, Any]], *, before_turn: int) -> list[dict[str, Any]]:
        """Revalidate exact observed originals; no summaries, state or model call.

        The caller owns the journal and serialized context capacity. This owner
        keeps source admission, source versions and chunk-window interpretation.
        Old unpinned receipts remain readable, never blessed with a current hash.
        """
        windows = {}
        for observation in observations:
            data = observation['data']
            location, digest = data.get('document_path'), data.get('document_sha256')
            if (not isinstance(location, str) or not Path(location).is_absolute()
                    or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)):
                continue
            windows = {key: value for key, value in windows.items()
                       if key[0] != location or value['sha256'] == digest}
            for passage in data.get('matches', data.get('retained_preview', [])):
                start, end, text = passage.get('character_start'), passage.get('character_end'), passage.get('text')
                if (not isinstance(text, str) or not text or type(start) is not int
                        or type(end) is not int or not 0 <= start <= end
                        or end - start != len(text) or not passage.get('chunk_id')):
                    continue
                key = (location, passage['chunk_id'], start, end)
                windows[key] = {'path': location, 'sha256': digest,
                    'document_record': data['document_record'], 'documents': data.get('documents', []),
                    'passage': passage, 'turn': observation['turn'], 'record': observation['record']}
        digests, retained = {}, []
        for window in sorted(windows.values(), key=lambda value: value['turn'], reverse=True):
            if window['turn'] >= before_turn:
                continue
            location = window['path']
            if location not in digests:
                digests[location] = None
                try:
                    path = self.workspace.resolve_source_record(location)
                    if path == Path(location) and path.is_file() and path.stat().st_size <= 64 * 1024 * 1024:
                        with path.open('rb') as handle:
                            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
                        if self.workspace.resolve_source_record(location) == path:
                            digests[location] = digest
                except (ValueError, KeyError, OSError, RuntimeError):
                    pass
            if digests[location] != window['sha256']:
                continue
            passage = window['passage']
            if any(prior['path'] == location and prior['sha256'] == window['sha256']
                    and prior['passage']['chunk_id'] == passage['chunk_id']
                    and prior['passage']['character_start'] <= passage['character_start']
                    and passage['character_end'] <= prior['passage']['character_end'] for prior in retained):
                continue
            retained.append(window)
        return retained

    def __init__(self, root: Path, workspace: Any, ledger: BudgetLedger, *,
                 policy: SourcePolicy | dict[str, Any] | None = None,
                 registry: SearchProviderRegistry | None = None,
                 client_factory: Callable[[str], Any] | None = None,
                 feasibility_client_factory: Callable[[str], Any] | None = None,
                 execution_contract: dict[str, Any] | None = None,
                 authority_revision: int = 0) -> None:
        if type(authority_revision) is not int or authority_revision < 0:
            raise ValueError('Source authority revision must be a nonnegative integer')
        self.authority_revision = authority_revision
        self.root, self.workspace, self.ledger = root, workspace, ledger
        # Preserve the original config distinction across gateway reconstruction;
        # never add it to SourcePolicy or historical policy snapshots.
        self._policy_supplied = policy is not None
        self.policy = (SourcePolicy(max_results=20) if policy is None
            else SourcePolicy.model_validate(policy)).model_copy(deep=True)
        self.client_factory, self.feasibility_client_factory = client_factory, feasibility_client_factory
        # Runtime reference to the scientific owner's fixed authority, not source policy.
        self._declared_execution_contract = execution_contract
        self._caller_contract_snapshot = (self._protocol_bytes(execution_contract)
            if execution_contract is not None else None)
        self._caller_policy_snapshot = self._protocol_bytes(self.policy.model_dump(mode='json'))
        materials = self._material_paths(list(self.policy.materials)) if self.policy.materials else []
        # Runtime dependencies are never part of the persisted policy/binding.
        # An explicitly empty registry must stay empty after a dependency is lost.
        self._builtin_registry = registry is None
        self.registry = (registry if registry is not None else
            default_search_provider_registry(local_documents=[str(path) for path in materials]))

    context_tools = {'research', 'research_collect'}

    def call(self, name: str, arguments: dict[str, Any], *, identity: str) -> ToolResult:
        try:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", identity):
                raise ValueError("Invalid source invocation identity")
            if name in self.context_tools:
                raise ValueError('Research requires the actual common CapabilityContext')
            if name not in {'search_sources', 'acquire_source', 'find_source_passages'}:
                raise ValueError('Unknown source operation')
            if name == "find_source_passages":
                request = _Passages.model_validate(arguments)
                path = self.workspace.resolve_source_record(request.document_record)
                raw = path.read_bytes()
                bundle = DocumentBundle.from_handoff_dict(json.loads(raw))
                identity = {
                    "document_record": request.document_record,
                    "document_path": str(path),
                    "document_sha256": hashlib.sha256(raw).hexdigest(),
                    "documents": [_source_identity(record, bundle) for record in bundle.records[:20]],
                    "documents_truncated": len(bundle.records) > 20,
                    "retained_chunks": len(bundle.chunks), "retained_sections": len(bundle.sections),
                }
                if not request.query.strip():
                    data = {
                        **identity,
                        "retained_preview": source_chunk_views(bundle.chunks[:1], max_chars=1200),
                        "scope": "Source identity, retained coverage and a bounded retained-text preview; not full reading. Query passages for consequential claims. Supplied source metadata is not independently verified."}
                else:
                    selected = rank_source_chunks(bundle.chunks[:self.policy.max_chunks], request.query,
                        limit=min(request.limit, self.policy.max_chunks) if self.policy.max_chunks is not None else request.limit)
                    data = {
                        **identity,
                        "matches": source_chunk_views(selected, query=request.query, max_chars=request.max_chars),
                        "selected_chunks": len(selected),
                        "scope": "Lexical lookup in retained text; not full reading or proof of absence"}
                current = self.workspace.resolve_source_record(request.document_record)
                if current != path or current.read_bytes() != raw:
                    raise ValueError('Source record changed during passage selection')
                return ToolResult(tool_name=name, data=data)
            request = (_Search if name == "search_sources" else _Acquire).model_validate(arguments)
            directory = self.root / "evidence" / identity
            if name == "search_sources":
                self._validate_search(request)
                operation = lambda: self._search(request, directory)
                count = len(set(request.providers))
            else:
                backend = request.web_extract_backend
                if (backend is not None and self._policy_supplied
                        and backend != self.policy.web_extract_backend):
                    raise ValueError('Acquisition backend differs from caller-frozen source policy')
                if not self.policy.allow_network:
                    raise ValueError('Source policy denies network acquisition')
                if backend == 'tavily_basic':
                    if 'web' not in self.policy.providers:
                        raise ValueError('Tavily extraction requires the web provider in source policy')
                    from simple_ar.research.connectors.web import WebConnector
                    if not WebConnector()._api_key:
                        raise ValueError('Tavily page extraction requires TAVILY_API_KEY')
                papers = []
                records = []
                if request.search_record:
                    if not request.paper_id:
                        raise ValueError("Saved search acquisition requires its paper ID")
                    path = self.workspace.resolve(request.search_record)
                    if not path.is_relative_to(self.root / "evidence"):
                        raise ValueError("Use a saved search result from the task evidence directory")
                    result = SearchResult.from_handoff_dict(json.loads(path.read_text()))
                    papers = [paper for paper in result.papers if paper.id == request.paper_id]
                    if not papers:
                        raise ValueError("Paper ID is absent from this saved search result")
                    if request.url and any(request.url != paper.fulltext_url for paper in papers):
                        raise ValueError("Explicit URL differs from the saved paper's fulltext_url")
                    for paper in papers:
                        validate_public_url(paper.fulltext_url or paper.url)
                else:
                    if request.paper_id:
                        raise ValueError("Paper ID requires its saved search record")
                    validate_public_url(request.url)
                    records = [DocumentRecord(document_id=identity, title=request.url,
                        source="supporting_material", url=request.url,
                        metadata={"parent_document_id": "explicit_task_source"})]
                operation = lambda: self._acquire(papers, records, directory, web_extract_backend=backend)
                count = 1
            # Counts adapter/provider operations, not physical HTTP requests or
            # vendor credits. Existing acquisition owns redirect/byte limits.
            reservation = f"source:{identity}"
            if any(entry.reservation_id == reservation for entry in self.ledger.entries):
                raise ValueError("Source invocation already recorded; inspect its result before any replay")
            entry = self.ledger.reserve(reservation, {"source_operations": count}, purpose=name)
            if entry.status != "reserved":
                raise ValueError("Source operation already accounted; cannot replay")
            directory.mkdir(parents=True, exist_ok=True)
            try:
                data = operation()
            except Exception:
                self.ledger.settle(reservation, {"source_operations": count}, actual_source="adapter",
                    reason="Source adapter attempted; operation failed")
                raise
            except BaseException:
                self.ledger.mark_unknown(reservation, reason="Source operation interrupted; inspect saved records before replay")
                raise
            # Bind provider/credit provenance to the original manifest, without
            # equating adapter operations with vendor credits or HTTP requests.
            reason = (f"Acquisition provenance: {data['document_record']}; "
                f"web_extract_backend={data['acquisition']['budget']['web_extract_backend']}"
                if name == 'acquire_source' else '')
            self.ledger.settle(reservation, {"source_operations": count}, actual_source="adapter", reason=reason)
            return ToolResult(tool_name=name, data=data)
        except (ValueError, KeyError, OSError, BudgetError) as exc:
            return ToolResult(tool_name=name, status="blocked", error=str(exc))

    def _search(self, request: _Search, directory: Path, *, queries: tuple[str, ...] = (),
                client: Any = None, plan: ResearchPlanResult | None = None, question: str = '') -> dict[str, Any]:
        for query in queries or (request.query,):
            self._validate_search(request.model_copy(update={'query': query}))
        result = search_sources(SearchRequest(queries=queries or (request.query,),
            providers=tuple(dict.fromkeys(request.providers)), max_results_per_query=request.max_results,
            cache_dir=self._cache_dir(), cache_enabled=self.policy.cache_enabled,
            llm_client=client, problem_markdown=question,
            research_plan_json=json.dumps(plan.to_handoff_dict()) if plan else '{}'), registry=self.registry)
        result = (select_search_result(result, policy=SearchSelectionPolicy(topic=question, questions=plan.questions,
            query_plan=plan.query_plan, max_documents=self.policy.max_documents)) if plan else
            replace(result, selected_papers=tuple({paper.id: paper for paper in result.papers}.values())[:self.policy.max_documents]))
        write_json(directory / "search.json", result.to_handoff_dict())
        return {"status": result.status, "search_record": (directory / 'search.json').relative_to(self.root).as_posix(),
            "responses": result.to_dict()["responses"], "diagnostics": list(result.diagnostics),
            "papers": [{**paper.to_row(), "abstract": paper.abstract[:1500]}
                       for paper in {paper.id: paper for paper in result.papers}.values()],
            "scope": "Provider metadata/excerpts; acquire originals for consequential claims"}

    def _acquire(self, papers: list, records: list, directory: Path, *,
                 web_extract_backend: str | None = None) -> dict[str, Any]:
        bundle = self._bundle(papers, records, directory, network=True, web_extract_backend=web_extract_backend)
        write_json(directory / "document.json", bundle.to_handoff_dict())
        # Raw extraction and canonical provenance stay outside writable output.
        # Model text windows are served by the same mounted file reader.
        text = "\n\n".join(f"[{chunk.chunk_id}; document={chunk.document_id}; page={chunk.page}]\n{chunk.text}"
                           for chunk in bundle.chunks)
        (directory / "passages.txt").write_text(text, encoding="utf-8")
        return {"document_record": f"evidence/{directory.name}/document.json",
            "passages": f"evidence/{directory.name}/passages.txt", "text_bytes": len(text.encode()),
            "documents": [_source_identity(record, bundle) for record in bundle.records],
            "acquisition": bundle.fulltext_manifest, "extraction": bundle.fulltext_extraction,
            "scope": "Only acquired passages support original-text claims; metadata is not full text"}

    def _cache_dir(self) -> Path | None:
        if not self.policy.cache_enabled:
            return None
        return Path(self.policy.cache_dir) if self.policy.cache_dir else self.root / 'source_cache'

    def _bundle(self, papers: list, records: list, directory: Path, *, network: bool,
                local_documents: list[str] | None = None,
                web_extract_backend: str | None = None) -> DocumentBundle:
        plan = self.policy.source_plan(local_documents=local_documents)
        if web_extract_backend is not None:
            plan = replace(plan, budget={**plan.budget, 'web_extract_backend': web_extract_backend})
        if not network:
            plan = replace(plan, require_fulltext=False)
        return build_document_bundle(papers=papers, supporting_records=records, source_plan=plan,
            cache_dir=self._cache_dir() or directory / 'raw', extraction_dir=directory / 'parsed',
            max_chunks=self.policy.max_chunks)

    def _validate_providers(self, providers: list[str] | tuple[str, ...]) -> None:
        # Source-network authority is independent of a connector's name or IO.
        if not self.policy.allow_network and not (self._builtin_registry and set(providers) == {'local_files'}):
            raise ValueError('Source policy denies network search')
        if not set(providers) <= set(self.policy.providers):
            raise ValueError('Search providers/results exceed source policy')
        unknown = set(providers) - set(self.registry.names())
        if unknown:
            raise ValueError('Unknown search provider: ' + ', '.join(sorted(unknown)))

    def _validate_search(self, request: _Search) -> None:
        self._validate_providers(request.providers)
        if self.policy.queries and request.query not in self.policy.queries:
            raise ValueError('Query is outside the exact source policy')
        if request.max_results > self.policy.max_results:
            raise ValueError('Search providers/results exceed source policy')

    @staticmethod
    def _digest(path: Path) -> str:
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()

    def _material_paths(self, values: list[str]) -> list[Path]:
        paths = [self.workspace.resolve_source_record(value) for value in values]
        return self.validate_material_paths(paths, self.policy)

    @staticmethod
    def validate_material_paths(paths: list[Path], policy: SourcePolicy, *,
                                excluded_roots: tuple[Path, ...] = ()) -> list[Path]:
        """Shared read-only preflight for source use and caller configuration."""
        if len(paths) > policy.max_documents or len(set(paths)) != len(paths):
            raise ValueError('Material selection exceeds document limit or repeats a source')
        for path in paths:
            if any(path.is_relative_to(root.resolve()) for root in excluded_roots):
                raise ValueError('Generated output or attempt history is not original-source evidence')
            if not path.is_file() or path.stat().st_size > policy.max_pdf_mb * 1024 * 1024:
                raise ValueError('Material must be a bounded authorized file')
        return paths

    def _handoff(self, locator: str, schema: str) -> dict[str, Any]:
        path = self.workspace.resolve(locator)
        if path.is_relative_to((self.root / 'attempts').resolve()):
            receipt = json.loads((path.parent / 'source_result.json').read_text(encoding='utf-8'))
            if receipt['hashes'].get(str(path)) != self._digest(path):
                raise ValueError('Research handoff changed since its bound result')
        else:
            self.workspace.resolve_source_record(locator)
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('Research handoff exceeds bounded input size')
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('schema_version') != schema:
            raise ValueError(f'Expected {schema} handoff')
        return data

    def execute_tool(self, name: str, context: CapabilityContext, arguments: dict[str, Any],
                     *, approval: dict[str, Any] | None = None) -> ToolResult:
        """One caller-selected operation on the actual common attempt, never a session."""
        try:
            identity = context.attempt.attempt_id
            if (not isinstance(context, CapabilityContext)
                    or name not in self.context_tools or context.attempt.capability != 'native_tool'
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', identity)
                    or context.store.root.resolve() != (self.root / 'attempts' / identity).resolve()):
                raise ValueError('Research requires its actual common native-tool attempt')
            request = _Research.model_validate(arguments)
            network = name == 'research_collect'
            if network and (not self.policy.allow_network or request.operation not in {'ingest', 'summary'}):
                raise ValueError('Network collection is not authorized for this operation')
            paths = self._material_paths(request.materials or list(self.policy.materials))
            locators = [value for value in (request.plan_record, request.document_record, request.read_record,
                request.synthesis_record, request.previous_design) if value]
            inputs = [*paths, *(self.workspace.resolve(value) for value in locators)]
            source_locator = self.policy.source_workspace or request.source_workspace
            inventory = (self.workspace.bind_inventory(self.workspace.resolve(source_locator), identity=identity)
                if request.operation == 'design' and source_locator else None)
            binding = {'tool': name, 'request': request.model_dump(mode='json'),
                **self._revision_fields(),
                'policy': self.policy.model_dump(mode='json'),
                'inputs': {**{str(path): self._digest(path) for path in inputs}, **{
                    str(self.workspace.resolve(path)): digest for path, digest in (inventory or {}).get('file_hashes', {}).items()}}}
            if context.store.exists('source_request.json'):
                if context.store.read_json('source_request.json') != binding:
                    raise ValueError('Research request, policy or material bytes changed')
                if not context.store.exists('source_result.json'):
                    raise ValueError('Research was already attempted; inspect partial evidence and ledger, no blind replay')
                saved = context.store.read_json('source_result.json')
                if any(self._digest(Path(path)) != digest for path, digest in saved['hashes'].items()):
                    raise ValueError('Research result artifact changed')
                return ToolResult.model_validate(saved['result'])
            if network and not paths and not request.document_record:
                # Reject missing runtime services before writing an attempt or
                # reserving budget; saved evidence remains available for replay.
                self._validate_providers(self.policy.providers)
            context.store.write_json('source_request.json', binding, kind='source_request')
            if inventory:
                context.store.write_json('source_inventory.json', inventory, kind='source_inventory')
            result = self._research(context, request, paths, network=network)
            if any(self._digest(Path(path)) != digest for path, digest in binding['inputs'].items()):
                raise ValueError('Research inputs changed during the operation; inspect retained work')
            result.artifacts = [f'records/{identity}/{path}' for path in result.data['artifact_refs']]
            result.data['artifact_sha256'] = self.workspace.artifact_hashes(result.artifacts)
            result.metadata['capability_artifacts'] = [context.store.ref(path, kind='research_artifact').to_dict()
                for path in result.data['artifact_refs']]
            if 'document_bundle.json' in result.data['artifact_refs']:
                result.data['document_record'] = f'records/{identity}/document_bundle.json'
            if 'research_summary.json' in result.data['artifact_refs']:
                result.metadata['reusable_materials'] = {'summary': result.artifacts}
                summary = context.store.read_json('research_summary.json')
                result.data.update(summary_record=f'records/{identity}/research_summary.json',
                    summary_markdown=f'records/{identity}/research_summary.md',
                    status=summary['status'], unresolved=summary['diagnostics'] if summary['status'] == 'partial' else [])
                if summary['status'] == 'partial' and not result.data['unresolved']:
                    result.data['unresolved'] = ['Evidence summary remains partial; retained sources do not establish complete coverage.']
            context.store.write_json('source_result.json', {'result': result.model_dump(mode='json'),
                'hashes': {str(context.store.resolve(ref)): self._digest(context.store.resolve(ref))
                    for ref in result.data['artifact_refs']}}, kind='source_result')
            return result
        except (ValueError, KeyError, TypeError, OSError, BudgetError, LLMError) as exc:
            return ToolResult(tool_name=name, status='blocked', error=str(exc))

    def _research(self, context: CapabilityContext, request: _Research, paths: list[Path],
                  *, network: bool) -> ToolResult:
        artifacts, outcomes = [], []
        identity, policy = context.attempt.attempt_id, self.policy

        def run(capability: Callable, value: Any) -> None:
            result = capability(context=context, request=value)
            artifacts.extend(result.artifacts)
            outcomes.append(result.to_dict())
            if result.status == 'failed' or (result.status == 'blocked'
                    and op not in {'summary', 'assess', 'design'}):
                raise ValueError('; '.join(result.diagnostics) or 'Research capability blocked')

        def client(*, reviewer: bool = False) -> Any:
            if not request.use_llm:
                return None
            factory = (self.feasibility_client_factory or self.client_factory) if reviewer else self.client_factory
            if factory is None:
                raise ValueError('Research requires a parent budget-bound client factory')
            value = factory(identity)
            if (getattr(value, '_budget_ledger', None) is not self.ledger
                    or getattr(value, '_budget_attempt_id', None) != identity):
                raise ValueError('Research client is not bound to the parent ledger/attempt')
            return value

        op, bundle, read = request.operation, None, None
        plan = ResearchPlanResult.from_handoff_dict(self._handoff(request.plan_record, 'research_plan.v1')) if request.plan_record else None
        if op == 'plan':
            run(run_research_plan_capability, ResearchPlanRequest(topic=request.question,
                config={'research_queries': list(policy.queries), 'research_sources': list(policy.providers),
                        'research_auto_query_expansion': False}, default_max_results=policy.max_results,
                use_llm=request.use_llm, llm_client=client()))
            planned = context.store.read_json('research_plan.json')
            planned['source_plan'] = replace(policy.source_plan(), queries=list(policy.queries) or planned['source_plan']['queries']).to_row()
            context.store.write_json('research_plan.json', planned)
        elif op in {'ingest', 'read', 'summary', 'synthesize'}:
            if request.document_record:
                bundle = DocumentBundle.from_handoff_dict(self._handoff(request.document_record, 'document_bundle.v1'))
            elif op != 'synthesize':
                papers = []
                if network and not paths:
                    if not policy.providers:
                        raise ValueError('Collection needs configured providers or materials')
                    queries = policy.queries or (tuple(plan.source_plan.queries) if plan else (request.question,))
                    collections = sum(binding.get('tool') == 'research_collect'
                        for _, binding in self._source_requests()
                        if self._record_revision(binding) == self.authority_revision)
                    if collections > 1 + policy.max_iterations:
                        raise ValueError('Research collection iteration limit reached; initial collection plus configured followups only')
                    count = len(queries) * len(policy.providers)
                    reservation = f'source:{identity}'
                    if any(row.reservation_id == reservation for row in self.ledger.entries):
                        raise ValueError('Collection already recorded; no blind replay')
                    self.ledger.reserve(reservation, {'source_operations': count}, attempt_id=identity,
                        purpose='research collection')
                    try:
                        directory = self.root / 'evidence' / identity
                        directory.mkdir(parents=True, exist_ok=True)
                        self._search(_Search(query=queries[0], providers=list(policy.providers),
                            max_results=policy.max_results), directory, queries=queries,
                            client=client(), plan=plan, question=request.question)
                        found = SearchResult.from_handoff_dict(json.loads((directory / 'search.json').read_text()))
                        artifacts.append(context.store.write_json('search_result.json', found.to_handoff_dict(), kind='search_result'))
                        papers.extend(found.selected_papers)
                        outcomes.append({'status': found.status, 'diagnostics': list(found.diagnostics)})
                        self.ledger.settle(reservation, {'source_operations': count}, actual_source='adapter')
                    except BaseException:
                        self.ledger.mark_unknown(reservation, reason='Collection interrupted; inspect saved responses')
                        raise
                papers = list({paper.id: paper for paper in papers}.values())[:policy.max_documents]
                local, retained = [], []
                for path in paths:
                    if self.inventory_source(path) is not None:
                        retained.append(DocumentBundle.from_handoff_dict(json.loads(path.read_bytes())))
                    else:
                        local.append(str(path))
                reservation = f'source:{identity}:ingest'
                if papers and network:
                    self.ledger.reserve(reservation, {'source_operations': 1}, attempt_id=identity, purpose='research ingest')
                try:
                    bundle = self._bundle(papers, [], context.store.root, network=network, local_documents=local)
                except BaseException:
                    if papers and network:
                        self.ledger.mark_unknown(reservation, reason='Acquisition interrupted; no blind replay')
                    raise
                if papers and network:
                    self.ledger.settle(reservation, {'source_operations': 1}, actual_source='adapter')
                for saved in retained:
                    bundle.records.extend(saved.records)
                    bundle.sections.extend(saved.sections)
                    bundle.chunks.extend(saved.chunks)
            if bundle is not None:
                if len(bundle.records) > policy.max_documents:
                    raise ValueError('Selected bundle exceeds document policy; narrow the material selection')
                bundle.chunks[:] = bundle.chunks[:policy.max_chunks]
                context.store.write_json('document_bundle.json', bundle.to_handoff_dict())
                if not any(ref.path == 'document_bundle.json' for ref in artifacts):
                    artifacts.append(context.store.ref('document_bundle.json', kind='document_bundle'))
            if op != 'ingest':
                if bundle is None:
                    raise ValueError('A document_record is required to resolve the read handoff')
                if request.read_record:
                    read = ReadResult.from_handoff_dict(self._handoff(request.read_record, 'read_result.v1'), bundle=bundle)
                else:
                    # The shortlist bounds deterministic and model reading alike.
                    ids = tuple(row.document_id for row in bundle.records[:policy.read_max_shortlist])
                    run(run_read_capability, ReadRequest(bundle=bundle, document_ids=ids,
                        topic=request.question, config={'research_read_max_shortlist': policy.read_max_shortlist},
                        research_plan_json=json.dumps(plan.to_handoff_dict()) if plan else '{}',
                        use_llm=request.use_llm, llm_client=client()))
                    read = ReadResult.from_handoff_dict(context.store.read_json('read_result.json'), bundle=bundle)
            if op in {'summary', 'synthesize'}:
                pack = evidence_pack_from_read(request.question, read)
                run(run_synthesis_capability, SynthesisRequest(evidence_pack=pack,
                    idea_limit=policy.idea_limit, novelty_backend=policy.novelty_backend,
                    purpose='evidence_review' if op == 'summary' else 'research',
                    include_experiment_contract=op != 'summary', use_llm=request.use_llm, llm_client=client()))
                if op == 'summary':
                    from simple_ar.research.sources.capability import provided_materials_result
                    synthesis = SynthesisResult.from_handoff_dict(context.store.read_json('synthesis_result.json'))
                    search = found if network and not paths and not request.document_record else provided_materials_result(bundle.records)
                    artifacts.append(context.store.write_text('research_summary.md', render_summary(
                        ResearchBrief(request_text=request.question), synthesis,
                        search, bundle, read), kind='research_summary'))
                    artifacts.append(context.store.write_json('research_summary.json', {
                        'schema_version': 'research_summary.v1', 'question': request.question,
                        'status': 'partial' if read.status != 'completed' or synthesis.status != 'ready'
                            or search.status != 'completed' else 'completed',
                        'diagnostics': [*read.diagnostics, *synthesis.diagnostics, *search.diagnostics]}, kind='research_summary'))
        else:
            synthesis = SynthesisResult.from_handoff_dict(self._handoff(request.synthesis_record, 'synthesis_result.v1'))
            if len(synthesis.ideas) > policy.idea_limit:
                raise ValueError('Synthesis exceeds configured idea limit')
            selected = policy.selected_idea_id or request.idea_id
            if policy.selected_idea_id and request.idea_id not in {None, policy.selected_idea_id}:
                raise ValueError('Cannot replace the explicitly selected candidate')
            if op == 'assess' or (request.use_llm and selected is None):
                run(run_idea_assessment_capability, IdeaAssessmentRequest(candidates=synthesis.ideas,
                    novelty_checks=synthesis.novelty_checks, limit=policy.idea_limit,
                    available_evidence_refs=tuple(dict.fromkeys(ref for idea in synthesis.ideas for ref in idea.motivation_refs)),
                    objective=request.question, llm_client=client()))
                assessment = context.store.read_json('idea_assessment.json')
                selected = assessment.get('recommended_idea_id')
                if op == 'design' and selected is None:
                    return self._professional_result(request.operation, artifacts, outcomes, network=network,
                        domain_status='needs_review', abstention=assessment.get('recommendation_reason', 'Model abstained'))
            if op == 'design':
                if self.pending_decision():
                    raise ValueError('Resolve the pending protocol decision before requesting another design')
                previous = self._handoff(request.previous_design, 'research_design.v1') if request.previous_design else None
                source_locator = policy.source_workspace or request.source_workspace
                if policy.source_workspace and request.source_workspace and (
                        self.workspace.resolve(policy.source_workspace) != self.workspace.resolve(request.source_workspace)):
                    raise ValueError('Cannot replace the configured project source')
                source = self.workspace.resolve(source_locator) if source_locator else None
                inventory = context.store.read_json('source_inventory.json') if source else {}
                index = {}
                facts = dict(policy.entry_facts)
                if source:
                    from simple_ar.research.preparation import inspect_execution_entry
                    from simple_ar.code_task.review_pipeline import build_review_index
                    index = build_review_index(source)
                    if any(str((source / row['path']).resolve()) not in {
                            str(self.workspace.resolve(path)) for path in inventory['file_hashes']}
                            for row in index.get('files', [])):
                        raise ValueError('Project review index includes unmounted source paths')
                    facts = {**inspect_execution_entry({**policy.execution_boundary,
                        'code_task': {'code_root': str(source)}}), **facts}
                    artifacts.append(context.store.ref('source_inventory.json', kind='source_inventory'))
                    artifacts.append(context.store.write_json('project_inspection.json', facts, kind='project_inspection'))
                reviewer = client(reviewer=True)
                run(run_research_design_capability, ResearchDesignRequest(synthesis=synthesis,
                    topic=request.question, idea_id=selected, idea_id_is_fixed=True,
                    execution_schema=policy.execution_schema, execution_boundary=policy.execution_boundary,
                    entry_facts=facts, source_workspace=source, source_index=index,
                    previous_design=previous, implementation_feedback={'guidance': request.guidance} if previous else {},
                    use_llm=request.use_llm, llm_client=client(), feasibility_llm_client=reviewer))
                design = context.store.read_json('research_design.json')
                if request.use_llm and source is None:
                    # The existing assessment owner independently reviews the frozen proposal.
                    run(run_idea_assessment_capability, IdeaAssessmentRequest(
                        candidates=tuple(idea for idea in synthesis.ideas if idea.idea_id == design['selected_idea']['idea_id']),
                        novelty_checks=synthesis.novelty_checks,
                        available_evidence_refs=tuple(design['evidence_refs']),
                        limit=1, objective=request.question, constraints={'design': design}, llm_client=reviewer))
                    if context.store.read_json('idea_assessment.json').get('recommended_idea_id') is None:
                        design.update(status='needs_review', diagnostics=[*design['diagnostics'],
                            'Independent reviewer abstained; no accepted protocol.'])
                        context.store.write_json('research_design.json', design)
                if design['status'] != 'ready' or not design.get('execution_protocol'):
                    return self._professional_result(request.operation, artifacts, outcomes, network=network,
                        domain_status=design['status'], diagnostics=design['diagnostics'])
                base = dict(policy.execution_boundary)
                if source:
                    base.setdefault('cwd', str(source))
                base.setdefault('protocol', design['contract'])
                protocol_data = merge_execution_protocol(base, design['execution_protocol'])
                protocol_data['comparison_required'] = design['execution_protocol'].get('comparison_required', False)
                try:
                    self._execution_contract(protocol_data)
                except (ValueError, KeyError, TypeError) as exc:
                    design.update(status='needs_review', diagnostics=[*design['diagnostics'],
                        f'Execution protocol is not ready: {exc}'])
                    context.store.write_json('research_design.json', design)
                    return self._professional_result(request.operation, artifacts, outcomes, network=network,
                        domain_status='needs_review', diagnostics=design['diagnostics'])
                protocol = context.store.write_json('frozen_protocol.json', protocol_data, kind='frozen_protocol')
                artifacts.append(protocol)
        result = self._professional_result(op, artifacts, outcomes, network=network)
        data = result.data
        if op == 'design':
            data.update(design_record=f'records/{identity}/research_design.json',
                protocol_record=f'records/{identity}/frozen_protocol.json',
                protocol_sha256=self._digest(context.store.resolve('frozen_protocol.json')))
            decision = {'decision_id': identity + ':' + data['protocol_sha256'],
                **self._revision_fields(),
                'design_record': data['design_record'], 'protocol_record': data['protocol_record'],
                'protocol_sha256': data['protocol_sha256'],
                'design_sha256': self._digest(context.store.resolve('research_design.json'))}
            context.store.write_json('protocol_decision.json', decision)
            if policy.interaction == 'autonomous':
                context.store.write_json('protocol_response.json', {**decision,
                    'decision_response': 'accept', 'authority': 'caller autonomous policy', 'decision_guidance': ''})
            else:
                data['pending_decision'] = decision
        return result

    @staticmethod
    def _professional_result(operation: str, artifacts: list, outcomes: list, *, network: bool,
                             **extra: Any) -> ToolResult:
        refs = list(dict.fromkeys(ref.path for ref in artifacts))
        return ToolResult(tool_name='research_collect' if network else 'research', data={
            'operation': operation, 'artifact_refs': refs, 'outcomes': outcomes, **extra,
            'qualification': 'Partial statuses and domain diagnostics remain authoritative; no research quality certification.'})

    @staticmethod
    def _protocol_bytes(value: Any) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode('utf-8')

    def _revision_fields(self) -> dict[str, int]:
        # Missing is the original v0 wire format, including its hashed identities.
        return {'authority_revision': self.authority_revision} if self.authority_revision else {}

    def _record_revision(self, record: dict[str, Any]) -> int:
        if not isinstance(record, dict):
            raise ValueError('Source authority record must be an object')
        revision = record.get('authority_revision', 0)
        if type(revision) is not int or revision < 0 or revision > self.authority_revision:
            raise ValueError('Malformed or future source authority revision')
        return revision

    def _source_requests(self) -> list[tuple[Path, dict[str, Any]]]:
        rows = []
        for path in (self.root / 'attempts').glob('*/source_request.json'):
            binding = json.loads(path.read_text(encoding='utf-8'))
            self._record_revision(binding)
            rows.append((path, binding))
        return rows

    def _design_requests(self) -> list[Path]:
        return [path for path, binding in self._source_requests()
            if self._record_revision(binding) == self.authority_revision
            and binding['request']['operation'] == 'design']

    def _caller_protocol_files(self, binding: dict[str, Any] | None = None
                               ) -> tuple[Path, dict[str, bytes], dict[str, Any]]:
        if binding is None:
            if self._caller_contract_snapshot is None or self._declared_execution_contract is None:
                raise ValueError('Caller protocol requires the scientific owner fixed execution contract')
            if (self._protocol_bytes(self._declared_execution_contract) != self._caller_contract_snapshot
                    or self._protocol_bytes(self.policy.model_dump(mode='json')) != self._caller_policy_snapshot):
                raise ValueError('Caller execution contract or original policy changed')
            binding = {'proposal_kind': 'caller_execution_protocol',
                'provenance': 'caller-provided-not-research-design',
                'execution_contract': json.loads(self._caller_contract_snapshot),
                'policy': json.loads(self._caller_policy_snapshot), **self._revision_fields()}
        revision = self._record_revision(binding)
        if (set(binding) - {'proposal_kind', 'provenance', 'execution_contract', 'policy', 'authority_revision'}
                or binding.get('proposal_kind') != 'caller_execution_protocol'
                or binding.get('provenance') != 'caller-provided-not-research-design'
                or not isinstance(binding.get('execution_contract'), dict)
                or not isinstance(binding.get('policy'), dict)):
            raise ValueError('Invalid retained caller protocol binding')
        protocol_bytes = self._protocol_bytes(binding['execution_contract'])
        request_bytes = self._protocol_bytes(binding)
        identity = hashlib.sha256(request_bytes).hexdigest()
        directory = self.root / 'inputs' / 'caller-protocols' / identity
        if any(path.is_symlink() or path.is_junction()
                for path in (directory, directory.parent, directory.parent.parent)):
            raise ValueError('Caller protocol must remain in the actual session inputs directory')
        decision = {'proposal_kind': binding['proposal_kind'], 'provenance': binding['provenance'],
            **({'authority_revision': revision} if revision else {}),
            'decision_id': identity + ':' + hashlib.sha256(protocol_bytes).hexdigest(),
            'protocol_record': str((directory / 'frozen_protocol.json').resolve()),
            'protocol_sha256': hashlib.sha256(protocol_bytes).hexdigest(),
            'request_record': str((directory / 'caller_request.json').resolve()),
            'request_sha256': identity}
        return directory, {'caller_request.json': request_bytes,
            'frozen_protocol.json': protocol_bytes,
            'protocol_decision.json': self._protocol_bytes(decision)}, decision

    def declare_execution_protocol(self) -> dict[str, Any]:
        """Freeze the caller's scientific contract for semantic confirmation only.

        No design, attempt, model call, measurement or process approval is created.
        An interrupted or changed declaration is retained and cannot be repaired
        by replaying this method. Terminal responses are never overwritten.
        """
        if self._design_requests():
            raise ValueError('Existing research design request; confirm its existing design protocol')
        directory, files, decision = self._caller_protocol_files()
        # Validate all retained declarations before creating another one.
        self._decisions()
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            directory.mkdir()
        except FileExistsError:
            # _decisions also rejects incomplete directories and different bytes.
            self._decisions()
            return decision
        for name, content in files.items():
            with (directory / name).open('xb') as stream:
                stream.write(content)
        self._decisions()
        return decision

    def _decision_protocol(self, path: Path, decision: dict[str, Any]) -> Path:
        target = (Path(decision['protocol_record'])
            if path.parent.parent == self.root / 'inputs' / 'caller-protocols'
            else self.workspace.resolve(decision['protocol_record']))
        if target.resolve() != (path.parent / 'frozen_protocol.json').resolve():
            raise ValueError('Protocol record path differs from its actual decision')
        return target

    def _decisions(self) -> list[tuple[Path, dict[str, Any]]]:
        """One recovery/tamper check for both actual designs and caller protocols.

        Origin follows the storage namespace, never the absence of design fields.
        All consumers use these actual paths and the same bound response checks.
        """
        rows = []
        paths = list((self.root / 'attempts').glob('*/protocol_decision.json'))
        caller_root = self.root / 'inputs' / 'caller-protocols'
        if any(path.is_symlink() or path.is_junction() for path in (caller_root, caller_root.parent)):
            raise ValueError('Caller protocol must remain in the actual session inputs directory')
        if caller_root.exists():
            for directory in caller_root.iterdir():
                if not directory.is_dir() or directory.is_symlink() or directory.is_junction():
                    raise ValueError('Invalid retained caller protocol directory')
                paths.append(directory / 'protocol_decision.json')
        for path in sorted(paths, key=lambda item: item.stat().st_mtime_ns):
            decision = json.loads(path.read_text(encoding='utf-8'))
            revision = self._record_revision(decision)
            protocol = self._decision_protocol(path, decision)
            if path.parent.parent == caller_root:
                binding = json.loads((path.parent / 'caller_request.json').read_text(encoding='utf-8'))
                if self._record_revision(binding) != revision:
                    raise ValueError('Caller request and decision authority revisions differ')
                directory, files, expected = self._caller_protocol_files(
                    binding if revision != self.authority_revision else None)
                if directory != path.parent or decision != expected:
                    raise ValueError('Caller protocol differs from the fixed scientific contract or policy')
                if any(item.is_symlink() or not item.is_file() for item in directory.iterdir()):
                    raise ValueError('Caller protocol artifacts must be original regular files')
                if any((directory / name).read_bytes() != content for name, content in files.items()):
                    raise ValueError('Caller protocol bytes changed; decision is stale')
                if {item.name for item in directory.iterdir()} - {*files, 'protocol_response.json'}:
                    raise ValueError('Unexpected retained caller protocol artifacts')
            else:
                # A design cannot become a caller protocol by losing design_sha256.
                if decision.get('proposal_kind', 'research_design') != 'research_design':
                    raise ValueError('Design decision origin changed')
                receipt = json.loads((path.parent / 'source_result.json').read_text(encoding='utf-8'))
                binding = json.loads((path.parent / 'source_request.json').read_text(encoding='utf-8'))
                if self._record_revision(binding) != revision:
                    raise ValueError('Design request and decision authority revisions differ')
                if binding['request']['operation'] != 'design':
                    raise ValueError('Protocol decision has no actual design request')
                # Archived external sources may be replaced. Retained attempt
                # artifacts still belong to their original immutable evidence.
                if any(self._digest(Path(item)) != digest for item, digest in binding['inputs'].items()
                        if revision == self.authority_revision
                        or Path(item).absolute().is_relative_to((self.root / 'attempts').absolute())):
                    raise ValueError('Design source inputs changed; protocol decision is stale')
                design = self.workspace.resolve(decision['design_record'])
                if design.resolve() != (path.parent / 'research_design.json').resolve():
                    raise ValueError('Design record path differs from its actual decision')
                for target, key in ((design, 'design_sha256'), (protocol, 'protocol_sha256')):
                    if (self._digest(target) != decision[key]
                            or receipt['hashes'].get(str(target)) != decision[key]):
                        raise ValueError('Frozen design/protocol changed; decision is stale')
                if decision['decision_id'] != path.parent.name + ':' + decision['protocol_sha256']:
                    raise ValueError('Protocol decision identity mismatch')
            response_path = path.parent / 'protocol_response.json'
            if response_path.exists():
                response = json.loads(response_path.read_text(encoding='utf-8'))
                if (self._record_revision(response) != revision
                        or any(response.get(key) != value for key, value in decision.items())
                        or response.get('decision_response') not in {'accept', 'reject', 'revise'}):
                    raise ValueError('Protocol response identity changed; decision is stale')
            if revision == self.authority_revision:
                rows.append((path, decision))
        return rows

    def pending_decision(self) -> dict[str, Any] | None:
        for path, decision in self._decisions():
            if not (path.parent / 'protocol_response.json').exists():
                return decision
        return None

    def respond_decision(self, decision_id: str, decision_response: str,
                         decision_guidance: str | None = None) -> dict[str, Any]:
        """Caller-only exact reply; no tool or natural language can self-approve."""
        if decision_response not in {'accept', 'reject', 'revise'}:
            raise ValueError('Protocol response must be accept, reject or revise')
        decisions = self._decisions()
        for path, decision in decisions:
            response_path = path.parent / 'protocol_response.json'
            if (decision['decision_id'] == decision_id
                    and decision.get('proposal_kind') == 'caller_execution_protocol'
                    and response_path.exists()):
                receipt = json.loads(response_path.read_text(encoding='utf-8'))
                if (receipt['decision_response'] == decision_response
                        and receipt.get('decision_guidance') == (decision_guidance or '')):
                    return receipt
                raise ValueError('Caller protocol already answered with different response or guidance')
        pending = next(((path, decision) for path, decision in decisions
            if not (path.parent / 'protocol_response.json').exists()), None)
        if pending is None or pending[1]['decision_id'] != decision_id:
            raise ValueError('Protocol decision is stale, already answered or mismatched')
        decision_path, decision = pending
        path = decision_path.parent / 'protocol_response.json'
        receipt = {**decision, 'decision_response': decision_response,
            'decision_guidance': decision_guidance or '', 'authority': 'explicit caller response'}
        # Exclusive creation rejects duplicate replies without changing the old receipt.
        with path.open('x', encoding='utf-8') as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
        return receipt

    def has_started(self, identity: str) -> bool:
        return (self.root / 'attempts' / identity / 'source_request.json').is_file()

    def can_reconcile(self, name: str, identity: str) -> bool:
        try:
            directory = self.root / 'attempts' / identity
            saved = json.loads((directory / 'source_result.json').read_text(encoding='utf-8'))
            binding = json.loads((directory / 'source_request.json').read_text(encoding='utf-8'))
            return (self._record_revision(binding) == self.authority_revision
                and binding['tool'] == name and binding['policy'] == self.policy.model_dump(mode='json')
                and all(self._digest(Path(path)) == digest for path, digest in {**binding['inputs'], **saved['hashes']}.items())
                and not any(row.attempt_id == identity and row.status in {'reserved', 'unknown'} for row in self.ledger.entries))
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def execution_issues(self) -> list[str]:
        """Semantic protocol acceptance is independent of process permission."""
        try:
            designs = self._design_requests()
            decisions = self._decisions()
            if not self.policy.require_design and not designs and not decisions:
                return []
            if not designs and not decisions:
                return ['Requested design has no accepted frozen protocol.']
            selected = self._execution_decision(decisions, designs)
            if selected is None or not (selected[0].parent / 'protocol_response.json').exists():
                return ['Design awaits a ready frozen protocol and its bound acceptance.']
            path, decision = selected
            response = json.loads((path.parent / 'protocol_response.json').read_text(encoding='utf-8'))
            if response['decision_response'] != 'accept':
                return ['Latest design was not accepted; revise or resolve its bound protocol decision.']
            boundary = self._fixed_execution_boundary()
            if boundary is not None or (self._declared_execution_contract is None
                    and self.policy.execution_boundary.get('code_task') is not None):
                # Read this already verified decision directly: accepted_protocol
                # calls this gate and must not become a recursive acceptance check.
                protocol = json.loads(self._decision_protocol(path, decision).read_text(encoding='utf-8'))
                accepted = (protocol if decision.get('proposal_kind') == 'caller_execution_protocol'
                    else self._execution_contract(protocol))
                from simple_ar.tools.scientific import fixed_measurement_issues
                return fixed_measurement_issues(boundary, accepted) if boundary is not None else []
            return []
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return [str(exc)]

    @staticmethod
    def _execution_decision(decisions: list[tuple[Path, dict[str, Any]]],
                            designs: list[Path]) -> tuple[Path, dict[str, Any]] | None:
        if designs:
            latest = max(designs, key=lambda path: path.stat().st_mtime_ns).parent
            return next((row for row in decisions if row[0].parent == latest), None)
        return decisions[-1] if decisions else None

    def accepted_protocol(self) -> dict[str, Any] | None:
        if self.execution_issues():
            return None
        selected = self._execution_decision(self._decisions(), self._design_requests())
        if selected is None:
            return None
        path, decision = selected
        return {**decision, 'execution_protocol': json.loads(
            self._decision_protocol(path, decision).read_text(encoding='utf-8'))}

    def _fixed_execution_boundary(self) -> dict[str, Any] | None:
        if self._declared_execution_contract is not None:
            return self._declared_execution_contract
        boundary = dict(self.policy.execution_boundary)
        if boundary.pop('code_task', None) is not None:
            boundary.pop('environment', None)
        boundary.pop('comparison_required', None)
        return boundary if (self.policy.require_design
            and {'command', 'cwd', 'timeout_sec', 'result_schema'}.issubset(boundary)
            and any(key in boundary for key in ('baseline_policy', 'baseline', 'pairs'))) else None

    def _execution_contract(self, execution: dict[str, Any]) -> dict[str, Any]:
        """Use the scientific owner's existing declaration normalization."""
        from simple_ar.tools.scientific import freeze_measurement_set, measurement_contract, measurement_conditions
        execution = dict(execution)
        frozen = self.policy.execution_boundary
        if self._declared_execution_contract is None and frozen.get('code_task') is not None:
            from simple_ar.app.research_execution import compile_scoped_measurement
            from copy import deepcopy
            task = deepcopy(frozen['code_task'])
            for key in ('code_task', 'environment'):
                if key in execution and execution[key] != frozen.get(key):
                    raise ValueError('Scoped design cannot replace caller-frozen task or environment')
            comparison = execution.pop('comparison_required', False)
            execution.pop('code_task', None)
            execution.pop('environment', None)
            # This branch accepts only the legacy creation schema; native
            # condition roles cannot implicitly grant producing-source authority.
            if set(execution) - {'command', 'cwd', 'timeout_sec', 'label', 'result_schema', 'protocol',
                    'baseline', 'baseline_policy', 'baseline_ref', 'pairs', 'seeds', 'seed_count', 'seed_flag'}:
                raise ValueError('Scoped design requires the frozen legacy execution schema')
            source = task.get('code_root') or self.policy.source_workspace
            if source:
                project = self.workspace.resolve(str(source)).resolve()
                if not project.is_dir():
                    raise ValueError('Scoped source must be a mounted project directory')
                if self.policy.source_workspace and self.workspace.resolve(self.policy.source_workspace).resolve() != project:
                    raise ValueError('Scoped code root differs from the mounted source project')
                accepted_cwd = self.workspace.resolve(str(execution['cwd'])).resolve()
            else:
                if task.get('kind') != 'greenfield' or task.get('workspace_mode') != 'empty':
                    raise ValueError('Source-less scoped measurement requires empty greenfield preparation')
                # An origin spelling for relocation, not a mount or read grant.
                project = Path(str(frozen['cwd']))
                accepted_cwd = Path(str(execution['cwd']))
                if not project.is_absolute() or not accepted_cwd.is_absolute():
                    raise ValueError('Source-less origin cwd must be explicitly absolute')
                project, accepted_cwd = project.absolute(), accepted_cwd.absolute()
            if accepted_cwd != project:
                raise ValueError('Scoped measurement cwd must match the frozen source project')
            execution['cwd'] = str(project)
            for key in ('command', 'timeout_sec', 'result_schema', 'baseline', 'baseline_policy',
                    'baseline_ref', 'pairs', 'seeds', 'seed_count', 'seed_flag'):
                if key in frozen and execution.get(key) != frozen[key]:
                    raise ValueError('Scoped design differs from a caller-frozen execution field: ' + key)
            if any((execution.get('protocol') or {}).get(key) != value
                    for key, value in (frozen.get('protocol') or {}).items()):
                raise ValueError('Scoped design differs from caller-frozen protocol data')
            owner_execution = {**execution, 'code_task': task}
            if frozen.get('environment') is not None:
                owner_execution['environment'] = deepcopy(frozen['environment'])
            contract, _ = compile_scoped_measurement(owner_execution, task, execution)
            if not source and any(row.get('source') == 'preparation'
                    for row in measurement_conditions(contract)):
                raise ValueError('A source baseline requires a real frozen source project')
            reused = [row for row in measurement_conditions(contract) if row.get('reuse_ref')]
            if reused:
                # A bare design locator is not imported evidence. Only the
                # original hash-bound authority can authorize reuse in scope.
                declared = freeze_measurement_set({key: value for key, value in frozen.items()
                    if key not in {'code_task', 'environment', 'comparison_required'}})
                originals = {row['id']: row for row in measurement_conditions(declared)}
                for row in reused:
                    original = originals.get(row['id'], {})
                    digest = original.get('reuse_sha256')
                    if (not digest or row['reuse_ref'] != original.get('reuse_ref')
                            or self._digest(self.workspace.resolve(row['reuse_ref'])) != digest):
                        raise ValueError('Scoped baseline reuse requires original hash-bound measurement evidence')
                    row['reuse_sha256'] = digest
            if any(not row.get('reuse_ref') and (self.workspace.resolve(str(row['execution']['cwd'])).resolve()
                    if source else Path(str(row['execution']['cwd'])).absolute()) != project
                    for row in measurement_conditions(contract)):
                raise ValueError('Scoped condition cwd must match the frozen source project')
            if comparison and {row['role'] for row in measurement_conditions(contract)} != {'baseline', 'candidate'}:
                raise ValueError('Requested comparison has no explicit executable baseline/condition set')
            return contract
        # CodeTask owns implementation scope and preparation. Neither is a
        # measurement control or a grant derived from accepting a design.
        if execution.pop('code_task', None) is not None:
            execution.pop('environment', None)
        comparison = execution.pop('comparison_required', False)
        if any(key in execution for key in ('baseline', 'pairs', 'seeds', 'seed_count', 'baseline_policy')):
            contract = freeze_measurement_set(execution)
        else:
            contract = measurement_contract(execution)
        if self._declared_execution_contract is not None:
            # Creation snapshots external baseline bytes. A design may retain
            # the caller's original locator; bind it to that SAME saved result,
            # never ignore reuse identity when comparing physical conditions.
            declared = {row['id']: row for row in measurement_conditions(self._declared_execution_contract)}
            for row in measurement_conditions(contract):
                expected = declared.get(row['id'], {})
                if 'reuse_ref' in row and expected.get('reuse_sha256'):
                    if (row.get('reuse_sha256', expected['reuse_sha256']) != expected['reuse_sha256']
                            or self._digest(self.workspace.resolve(row['reuse_ref'])) != expected['reuse_sha256']):
                        raise ValueError('Design baseline differs from the retained measurement evidence')
                    row.update(reuse_ref=expected['reuse_ref'], reuse_sha256=expected['reuse_sha256'])
        if comparison and {row['role'] for row in measurement_conditions(contract)} != {'baseline', 'candidate'}:
            raise ValueError('Requested comparison has no explicit executable baseline/condition set')
        return contract

    def accepted_execution_contract(self) -> dict[str, Any] | None:
        """Project the accepted execution through the existing scientific owner.

        Condition matching, physical evidence and analysis coverage belong to
        ScientificToolGateway, including when this projection is unavailable.
        """
        accepted = self.accepted_protocol()
        if accepted is None:
            return None
        # Acceptance checked physical fields and explicitly declared metadata.
        # Extra design prose remains source evidence, not a replacement for the
        # caller's frozen measurement/approval contract (including absent protocol).
        boundary = self._fixed_execution_boundary()
        if self._declared_execution_contract is not None:
            return self._declared_execution_contract
        return self._execution_contract(boundary if boundary is not None else accepted['execution_protocol'])

    def implementation_evidence(self) -> dict[str, Any]:
        """Bind accepted original research records for implementation approval."""
        if issues := self.execution_issues():
            raise ValueError('; '.join(issues))
        accepted = self.accepted_protocol()
        if accepted is None or accepted.get('proposal_kind') == 'caller_execution_protocol':
            return {}
        directory = self.workspace.resolve(accepted['design_record']).parent
        names = ('research_design.json', 'frozen_protocol.json', 'protocol_decision.json',
            'protocol_response.json', 'source_request.json', 'source_result.json')
        hashes = {self.workspace.locator(str(directory / name)): self._digest(directory / name)
            for name in names}
        return {'decision_id': accepted['decision_id'],
            'design_ref': self.workspace.locator(str(directory / 'research_design.json')),
            'hashes': hashes}

    def delivery_issues(self, observations: list[dict[str, Any]]) -> list[str]:
        issues = self.execution_issues()
        try:
            if self.policy.require_summary:
                summaries = [path.parent / 'research_summary.json' for path, binding in self._source_requests()
                    if self._record_revision(binding) == self.authority_revision
                    and binding['request']['operation'] == 'summary'
                    and (path.parent / 'research_summary.json').exists()]
                valid = False
                for path in summaries:
                    receipt = json.loads((path.parent / 'source_result.json').read_text(encoding='utf-8'))
                    summary = json.loads(path.read_text(encoding='utf-8'))
                    text = path.with_suffix('.md')
                    valid |= (summary.get('schema_version') == 'research_summary.v1'
                        and summary.get('status') in {'completed', 'partial'}
                        and all(receipt['hashes'].get(str(item)) == self._digest(item) for item in (path, text)))
                if not valid:
                    issues.append('Required research summary has no bound summary artifact with explicit completion/partial qualification.')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            issues.append(str(exc))
        return issues
