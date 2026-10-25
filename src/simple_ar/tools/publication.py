"""Template-first publication export through the shared rendering process owner."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Literal, Callable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from simple_ar.core.budget import BudgetError
from simple_ar.core.capabilities import ArtifactStore
from simple_ar.core.process import ProcessSpec, run_process
from simple_ar.report.export import (ReportExportError, ReportExportResourceError, export_acm_report,
    publication_template_inputs, validate_publication_checks)
from simple_ar.tools.specs import CommonToolSpec, ToolResult
from simple_ar.tools.workspace import DeliveryRequest, resolve_mounted_path


class PublicationTemplate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    path: str = Field(min_length=1, max_length=500,
        description='Trusted workspace Pandoc LaTeX template, relative to the Markdown directory. Requires $body$ after \\begin{document} and the standard $for(header-includes)$$header-includes$$endfor$ hook in the preamble for renderer-owned support. Source YAML metadata is excluded. No partials/scripts/downloads. Not arbitrary conference TeX or a sandbox.')
    assets: list[str] = Field(default_factory=list, max_length=32,
        description='Explicit flat companion filenames beside the template: cls/sty/bst/tex/png/jpg/jpeg/pdf, at most 16 MB total. Copied to project root; reference those names there. No automatic directory copy.')
    engine: str = Field(default='pdflatex', pattern='^(pdflatex|xelatex)$',
        description='Installed trusted TeX engine; xelatex for multilingual fonts supplied by template. BibTeX only; no custom build commands.')
    top_level_division: Literal['section', 'chapter', 'part'] | None = Field(default=None,
        exclude_if=lambda value: value is None,
        description='Optional Pandoc body heading level required by the supplied layout, e.g. chapter for a report/book template. No class-name inference or scientific restructuring. Omit to retain the historical section-based body.')


class PublicationChecks(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    max_pages: int | None = Field(default=None, gt=0,
        description='User-declared maximum for all PDF pages, including references and appendices; not an inferred venue policy.')
    forbidden_identifiers: list[str] | None = Field(default=None, min_length=1, max_length=32,
        description='User-supplied exact case-sensitive Unicode literals, 1–32 unique nonblank strings of at most 200 characters without controls. Checks extracted text only; no regex, OCR or anonymity certification.')

    @model_validator(mode='after')
    def validate_rules(self) -> PublicationChecks:
        validate_publication_checks(self.model_dump(exclude_none=True))
        return self


class PublicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    markdown: str = Field(description="Workspace Markdown file; local figures and references.bib use its directory.")
    title: str = Field(default="", max_length=500)
    compile_pdf: bool = True
    citation_style: Literal['numeric', 'template'] | None = Field(default=None,
        exclude_if=lambda value: value is None,
        description='Optional publication presentation choice. Select numeric for readable bracketed numbers, including narrative citations and authorless sources; supplied templates must load natbib and the default bibliography style becomes plainurl (TeX urlbst package, retaining URL/DOI; a literal template style still owns its choice). Select template to explicitly honor template citation defaults (built-in ACM stays numeric). May be chosen from the ordinary publication goal; no hashes or source metadata edits needed. Omit/null preserves historical rendering, request bindings and manifest shape. Exact citekeys, BibTeX records and template class/body remain unchanged.')
    checks: PublicationChecks | None = Field(default=None, exclude_if=lambda value: value is None,
        description='Optional finite rules explicitly supplied by the user. Requires compile_pdf=true and installed Poppler tools. Missing tools, bounded-output failure or rule failure retain diagnostics and prevent final delivery. No automatic edits or recompile; absence of literals never certifies anonymity.')
    template: PublicationTemplate | None = Field(default=None,
        description='Explicit user-provided trusted template bundle. Omit for the historical ACM manuscript layout. Compilation does not establish anonymous/page/official compliance.')
    render_preview: bool = Field(default=False, exclude_if=lambda value: value is False,
        description="Explicitly render every compiled PDF page with Poppler: at most 12 pages, 120 DPI scaled to a maximum 1600 pixels per dimension. Requires compile_pdf=true. Returns PNG locators for optional inspect_images; rendering is not visual review. Incomplete previews prevent delivery. False preserves historical request bindings.")
    delivery: DeliveryRequest | None = Field(default=None,
        description="Optional explicit final delivery after successful export/compilation. Exported files are added automatically; artifacts selects other already-existing outputs or retained task evidence. Disclose unresolved work. This does not certify scientific or visual quality. Request this operation alone when delivering.")
    @model_validator(mode="after")
    def validate_preview(self) -> PublicationRequest:
        if self.render_preview and not self.compile_pdf:
            raise ValueError("render_preview requires compile_pdf=true")
        if self.checks is not None and not self.compile_pdf:
            raise ValueError('checks requires compile_pdf=true')
        return self


class ExportRequest(PublicationRequest):
    """Saved Writer preparation compatibility; ordinary exports use PublicationRequest."""
    candidate_result: str = Field(default='',max_length=500,exclude_if=lambda value:not value,
        description='Same-task writing-work receipt for historical prepared-publication control; not final delivery.')

    @model_validator(mode='after')
    def validate_candidate(self) -> ExportRequest:
        if self.candidate_result and self.delivery is not None:
            raise ValueError('Candidate preparation has no final delivery authority')
        return self


def publication_tool_specs(*, preparation: bool = True) -> list[CommonToolSpec]:
    return [CommonToolSpec(name="export_report", domain="publication", permission_level="execution",
        completion={'field': 'delivery', 'closing': True},
        risk_level="medium", input_schema=(ExportRequest if preparation else PublicationRequest).model_json_schema(),
        description="Convert canonical workspace Markdown, bibliography and local figures into an editable TeX project using an explicit trusted Pandoc LaTeX template bundle, or ACM by default. Template bytes and companions share existing approval/recovery bindings. No official template discovery, scripts or shell escape; trusted TeX is not sandboxed. Optional finite user-declared checks use Poppler through the same process ledger: all-page count and exact literals in UTF-8 extracted text, separately reported. Literal absence never certifies anonymity; images, metadata and indirect identity are uncovered. Rendering is not scientific, visual or official compliance review. Optional delivery requires every requested check and preview to pass; failure retains the project without revision or recompilation.")]


def export_input_binding(source: Path, request: ExportRequest, *, output_root: Path | None = None,
                         include_images: bool = True) -> dict[str, Any]:
    """Pin authoring inputs once for approval and export recovery, without secrets."""
    root = source.parent.resolve()
    inputs = {}
    for path in (source, root / 'references.bib', root / 'figures/figures_manifest.json'):
        if path.exists() and not path.resolve().is_relative_to(root):
            raise ValueError('Export input escapes the manuscript directory')
        key = path.relative_to(root).as_posix()
        inputs[key] = None
        if path.is_file():
            with path.open('rb') as file:
                inputs[key] = hashlib.file_digest(file, 'sha256').hexdigest()
    if request.template is not None:
        for path in publication_template_inputs(root, request.template.model_dump()).values():
            authorized = resolve_mounted_path({'workspace': root}, str(path))
            if output_root is not None and authorized.is_relative_to(output_root.resolve()):
                raise ValueError('Derived publication files cannot supply a new template')
            with authorized.open('rb') as file:
                inputs[authorized.relative_to(root).as_posix()] = hashlib.file_digest(file, 'sha256').hexdigest()
    if not include_images:
        return {**request.model_dump(exclude_none=True), 'inputs': inputs}
    # Approval must cover actual figure bytes, including standalone Markdown
    # images without a generated manifest. Bind the authorized local image
    # inventory rather than introducing a second Markdown/Pandoc parser here.
    directories, visited, entries, image_bytes = [root], set(), 0, 0
    while directories:
        directory = directories.pop()
        if directory in visited:
            continue
        visited.add(directory)
        for candidate in sorted(directory.iterdir()):
            if output_root is not None and candidate.resolve().is_relative_to(output_root.resolve()):
                continue  # Derived exports are not authoring inputs.
            entries += 1
            if entries > 4096:
                raise ValueError('Publication image inventory exceeds 4096 entries; use a focused manuscript directory')
            try:
                path = resolve_mounted_path({'workspace': root}, str(candidate))
            except ValueError:
                # Private paths are not authoring inputs. An escaping symlink
                # cannot be silently omitted from an approval inventory.
                if not candidate.resolve().is_relative_to(root):
                    raise ValueError('Publication asset escapes the manuscript directory')
                continue
            if path.is_dir():
                directories.append(path)
            elif path.is_file() and path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.svg', '.pdf'}:
                key = path.relative_to(root).as_posix()
                if key in inputs:
                    continue
                image_bytes += path.stat().st_size
                if image_bytes > 256_000_000:
                    raise ValueError('Publication image inventory exceeds 256 MB; use a focused manuscript directory')
                with path.open('rb') as file:
                    inputs[key] = hashlib.file_digest(file, 'sha256').hexdigest()
    return {**request.model_dump(exclude_none=True), 'inputs': inputs, 'image_binding_version': 1}


class PublicationToolGateway:
    @property
    def context_tools(self) -> set[str]:
        return {'export_report'} if self.owned_execution else set()

    def can_reconcile(self, name: str, identity: str) -> bool:
        return name == 'export_report' and (
            self.workspace.workspace / 'publications' / identity / 'export.json').is_file() and self.has_started(identity)

    def has_started(self, identity: str) -> bool:
        return (self.root / 'tools' / 'publications' / identity / 'request.json').is_file()

    def __init__(self, root: Path, workspace: Any, ledger: Any, *,
                 report_source: Callable[[str], dict | None] | None = None,
                 candidate_source: Callable[[str, str], dict] | None = None,
                 owned_execution: bool = False) -> None:
        self.root, self.workspace, self.ledger = root, workspace, ledger
        self.report_source = report_source
        self.candidate_source, self.owned_execution = candidate_source, owned_execution

    def _source_identity(self, request: ExportRequest) -> dict | None:
        if request.candidate_result:
            if self.candidate_source is None:
                raise ValueError('Candidate preparation requires a registered writing-work owner')
            source = self.candidate_source(request.markdown, request.candidate_result)
            if source.get('kind') != 'publication_candidate' or source.get('adopted') is not False:
                raise ValueError('Candidate source must prove explicitly unadopted writing work')
        else:
            source = self.report_source(request.markdown) if self.report_source else None
        if source is not None and 'publication_options' in source:
            excluded = {'markdown', 'delivery', 'candidate_result'}
            expected = ExportRequest(markdown='', **source['publication_options']).model_dump(exclude=excluded)
            if request.model_dump(exclude=excluded) != expected:
                raise ValueError('Publication options differ from the admitted document work')
        return source

    def approval_binding(self, arguments: dict, *, identity: str) -> dict:
        request = ExportRequest.model_validate(arguments)
        source = self.workspace.resolve(request.markdown, write=True)
        binding = export_input_binding(source, request,
            output_root=self.workspace.workspace / 'publications')
        if request.candidate_result:
            binding['report_identity'] = self._source_identity(request)
        return binding

    def execute_tool(self, name: str, context: Any, arguments: dict, *, approval: dict | None = None) -> ToolResult:
        if name not in self.context_tools or approval is None:
            return ToolResult(tool_name=name, status='blocked', error='Publication needs bound execution approval')
        if not self.has_started(context.attempt.attempt_id) and approval.get('binding') != self.approval_binding(
                arguments, identity=context.attempt.attempt_id):
            return ToolResult(tool_name=name, status='blocked', error='Publication inputs changed after approval')
        return self.call(name, arguments, identity=context.attempt.attempt_id)

    def inspection_source(self, result_ref: str, *, session_id: str) -> dict:
        """Read a proved prepared view; rendering success grants no adoption."""
        from simple_ar.tools.professional_outcomes import accepted_native_result, native_result_path
        path = native_result_path(self.root, self.workspace, result_ref)
        relative = path.relative_to((self.root/'attempts').resolve())
        observed = json.loads(path.read_bytes())
        _, receipt_sha = accepted_native_result(self.root, session_id, observed, relative.parts[0])
        data = observed.get('data', {})
        if (observed.get('tool_name') != 'export_report' or observed.get('status') != 'ok'
                or observed.get('error') or not data.get('candidate_preparation')):
            raise ValueError('Publication receipt is not complete candidate preparation')
        owner = ArtifactStore(self.root/'tools/publications'/relative.parts[0])
        saved, result = owner.read_json('request.json'), owner.read_json('result.json')
        request = {key: value for key, value in saved.items()
            if key not in {'inputs', 'image_binding_version', 'output_binding_version', 'report_identity'}}
        binding = self.approval_binding(request, identity=relative.parts[0])
        if binding != {key: value for key, value in saved.items() if key != 'output_binding_version'}:
            raise ValueError('Prepared publication source or inputs changed')
        hashes = result['artifact_sha256']
        if (data.get('artifact_sha256') != hashes or self.workspace.artifact_hashes(hashes) != hashes
                or data.get('report_identity') != saved.get('report_identity')
                or result.get('report_identity') != saved.get('report_identity')
                or data['project'] != f'workspace/publications/{relative.parts[0]}'):
            raise ValueError('Prepared publication outputs or source identity changed')
        project = self.workspace.resolve(data['project'])
        manifest = ArtifactStore(project).read_json('export.json')
        if manifest != data.get('manifest') or manifest.get('status') not in {'exported', 'compiled'}:
            raise ValueError('Prepared publication manifest is incomplete or changed')
        ref = manifest.get('inspection_view', {})
        if ref.get('path') != 'publication_view.json' or ref.get('schema') != 'publication_view.v1':
            raise ValueError('Publication has no supported actual inspection view')
        view_path = project/'publication_view.json'
        view = json.loads(view_path.read_bytes())
        if (hashlib.sha256(view_path.read_bytes()).hexdigest() != ref['sha256']
                or view.get('schema_version') != ref['schema']
                or view.get('input_body_sha256') != binding['inputs'][Path(saved['markdown']).name]
                or view.get('source_markdown_sha256') != hashlib.sha256((project/'source.md').read_bytes()).hexdigest()
                or view.get('caption_owner') != saved['report_identity'].get('figure_caption_owner', 'markdown')):
            raise ValueError('Prepared inspection view differs from its candidate or actual export')
        return {'contract': 'prepared_publication.v1', 'prepared_result': self.workspace.locator(str(path)),
            'prepared_result_sha256': receipt_sha, 'source': saved['report_identity'],
            'view_path': self.workspace.locator(str(view_path)), 'view_sha256': ref['sha256'],
            'view': view, 'adopted': False,
            'pages': data.get('preview_images', []), 'visual_reviewed': False}

    def check_report_delivery(self, observations: list[dict], identity: dict | None, artifacts: list[str], *,
                              formats: list[str], manuscript: str | None, proposed: dict | None = None) -> set[Path]:
        """Prove requested publication roles belong to this authoring version."""
        source = manuscript or (identity or {}).get('manuscript')
        if not source:
            raise ValueError('Canonical conversion delivery must identify its actual input manuscript')
        if identity is not None and set(formats) <= {'markdown'}:
            return set()
        if proposed is not None:
            if proposed.get('candidate_result'):
                raise ValueError('Candidate preparation cannot satisfy final publication delivery')
            if self.workspace.resolve(proposed['markdown']) != self.workspace.resolve(source):
                raise ValueError('Proposed publication uses a different authoring version')
            if 'pdf' in formats and not proposed.get('compile_pdf', True):
                raise ValueError('The declared PDF obligation requires compilation')
            return set()  # Recheck actual owner results before terminal commit.
        delivered = {self.workspace.resolve(path) for path in artifacts}
        for row in observations:
            data = row.get('data') or {}
            if data.get('candidate_preparation') or data.get('report_identity', {}).get('kind') == 'publication_candidate':
                continue
            if row.get('tool_name') != 'export_report' or row.get('status') != 'ok' or not data.get('project'):
                continue
            project = self.workspace.resolve(data['project'])
            owner = ArtifactStore(self.root / 'tools/publications' / project.name)
            saved, result = owner.read_json('request.json'), owner.read_json('result.json')
            if (self.workspace.resolve(saved['markdown']) != self.workspace.resolve(source)
                    or saved.get('report_identity') != identity or data.get('report_identity') != identity
                    or result.get('report_identity') != identity):
                continue
            hashes = result.get('artifact_sha256')
            if (not hashes or data.get('artifact_sha256') != hashes
                    or self.workspace.artifact_hashes(hashes) != hashes
                    or ArtifactStore(project).read_json('export.json') != data['manifest']):
                raise ValueError('Publication project or owner evidence changed')
            request = ExportRequest.model_validate({key: value for key, value in saved.items()
                if key not in {'inputs', 'image_binding_version', 'output_binding_version', 'report_identity'}})
            current = export_input_binding(self.workspace.resolve(saved['markdown']), request,
                output_root=self.workspace.workspace / 'publications')
            if current['inputs'] != saved.get('inputs'):
                raise ValueError('Publication authoring inputs changed')
            self.workspace.check_artifact_producers(str(self.workspace.resolve(saved['markdown']).parent / key)
                for key, digest in current['inputs'].items() if digest is not None)
            required = {project / 'main.pdf'} if 'pdf' in formats else set()
            if 'pdf' in formats and data['manifest'].get('compiled') is not True:
                continue
            if 'tex' in formats:
                required.update(self.workspace.resolve(path) for path in hashes)
            if not required:
                required.add(project / 'source.md')
            if required.issubset(delivered) and all(path.is_file() for path in required):
                return {self.workspace.resolve(path) for path in hashes}
        raise ValueError('Requested report formats need a byte-bound publication of the same manuscript, figures and bibliography')

    def call(self, name: str, arguments: dict[str, Any], *, identity: str) -> ToolResult:
        try:
            if name != "export_report" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", identity):
                raise ValueError("Invalid publication invocation")
            request = ExportRequest.model_validate(arguments)
            if request.candidate_result and not self.owned_execution:
                raise ValueError('This task retains its historical publication execution contract')
            source = self.workspace.resolve(request.markdown, write=True)
            if not source.is_file() or source.suffix.lower() != ".md" or source.stat().st_size > 256000:
                raise ValueError("Use an existing workspace Markdown file no larger than 256 KiB")
            if request.delivery is not None:
                self.workspace.resolve_delivery(request.delivery)  # Reject invalid extra artifacts before rendering.
            prefix = f"workspace/publications/{identity}"
            output = self.workspace.resolve(prefix, write=True)
            owner = ArtifactStore(self.root / "tools" / "publications" / identity)
            saved = owner.read_json('request.json') if owner.exists('request.json') else None
            binding = export_input_binding(source, request, output_root=self.workspace.workspace / 'publications',
                include_images=saved is None or 'image_binding_version' in saved)
            producer_cache = {}
            self.workspace.check_artifact_producers((str(source.parent / key)
                for key, digest in binding['inputs'].items() if digest is not None), cache=producer_cache)
            report_identity = self._source_identity(request)
            if report_identity is not None:
                binding['report_identity'] = report_identity
            if saved is not None:
                output_binding_version = saved.get('output_binding_version')
                if output_binding_version not in {None, 1}:
                    raise ValueError('Unsupported publication output binding version; preserve the receipt')
                # Historical exports only pinned the Markdown body. Restore
                # their recorded version; do not invent missing past bindings.
                if 'image_binding_version' in saved:
                    current = binding
                elif 'inputs' in saved:
                    # Old terminal exports retain their recorded coverage;
                    # never upgrade their historical approval after the fact.
                    current = {**request.model_dump(exclude_none=True), 'inputs': {
                        key: binding['inputs'].get(key) for key in saved['inputs']}}
                else:
                    current = {**request.model_dump(exclude_none=True), 'sha256': binding['inputs'][source.name]}
                recorded_inputs = {key: value for key, value in saved.items() if key != 'output_binding_version'}
                if recorded_inputs != current:
                    raise ValueError("Export inputs changed; retain the previous version")
                if not (output / "export.json").is_file():
                    raise ValueError("Interrupted export has no terminal manifest; inspect process receipts, do not replay")
                manifest = ArtifactStore(output).read_json("export.json")
            else:
                # Mark new requests before rendering so an interrupted commit
                # cannot be mistaken for a historical, unbound export.
                owner.write_json("request.json", {**binding, 'output_binding_version': 1})
                render_source = source
                staged_hashes = {}
                if request.candidate_result:
                    # Freeze presentation dependencies in this approved operation;
                    # never repair or enrich an older candidate in place.
                    staged = owner.root / 'source'
                    staged.mkdir()
                    for key, digest in binding['inputs'].items():
                        if digest is None:
                            continue
                        raw = (source.parent / key).read_bytes()
                        if hashlib.sha256(raw).hexdigest() != digest:
                            raise ValueError('Candidate input changed while preparing publication')
                        target = staged / key
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(raw)
                        staged_hashes[key] = digest
                    captions = report_identity.get('provided_figure_captions', {})
                    if captions:
                        manifest_path = staged / 'figures/figures_manifest.json'
                        manifest_path.parent.mkdir(parents=True, exist_ok=True)
                        existing = json.loads(manifest_path.read_bytes()) if manifest_path.exists() else {'figures': []}
                        by_path = {row['path']: row for row in existing['figures']}
                        for path, caption in captions.items():
                            by_path[path] = {**by_path.get(path, {}), 'path': path, 'caption': caption}
                        existing['figures'] = list(by_path.values())
                        manifest_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                        staged_hashes['figures/figures_manifest.json'] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                    render_source = staged / source.name
                count = 0

                def render(argv, *, cwd, text=None, timeout=60, env=None):
                    nonlocal count
                    count += 1
                    process_id = f"{identity}-render-{count}"
                    logs = self.root / "tools" / "processes" / process_id
                    command = list(argv)
                    if text is not None:
                        logs.mkdir(parents=True, exist_ok=True)
                        input_path = logs / "input.txt"
                        input_path.write_text(text, encoding="utf-8")
                        command.append(str(input_path))  # Pandoc accepts a file instead of stdin.
                    environment = {key: os.environ[key] for key in ("PATH", "LANG", "SYSTEMROOT", "WINDIR") if key in os.environ}
                    environment.update({key: value for key, value in (env or {}).items()
                                        if key in {"openin_any", "openout_any", "shell_escape", "LC_ALL"}})
                    try:
                        result = run_process(ProcessSpec(argv=command, cwd=cwd, timeout_sec=timeout,
                            env=environment, output_dir=logs, invocation_id=process_id,
                            attempt_id=identity, tail_bytes=12000, log_bytes=2000000), budget_ledger=self.ledger)
                    except BudgetError as exc:
                        raise ReportExportResourceError(f"Rendering budget unavailable; inspect tools/processes/{process_id}: {exc}") from exc
                    if result.returncode or result.stop_reason or any(
                            stream["log_bytes_discarded"] for stream in result.record["streams"].values()):
                        raise ReportExportError(f"Renderer failed or output truncated; inspect tools/processes/{process_id}")
                    return (logs / "stdout.log").read_text(encoding="utf-8", errors="replace")

                def validate_asset(path):
                    if request.candidate_result:
                        if not path.resolve().is_relative_to(render_source.parent.resolve()):
                            raise ReportExportError('Prepared asset escapes its bound source directory')
                        digest = hashlib.sha256(path.read_bytes()).hexdigest()
                        if staged_hashes.get(path.relative_to(render_source.parent).as_posix()) != digest:
                            raise ReportExportError('Prepared candidate asset changed during rendering')
                        return digest
                    authorized = self.workspace.resolve(str(path))
                    self.workspace.check_artifact_producers([str(authorized)], cache=producer_cache)
                    key = authorized.relative_to(source.parent).as_posix()
                    with authorized.open('rb') as file:
                        digest = hashlib.file_digest(file, 'sha256').hexdigest()
                    if binding['inputs'].get(key) != digest:
                        raise ReportExportError('Figure/template is outside the bound authoring inputs or changed during rendering')
                    return digest

                try:
                    manifest = export_acm_report(render_source.parent, output, title=request.title or None,
                        body_name=source.name, compile_pdf=request.compile_pdf, render_preview=request.render_preview, runner=render,
                        validate_asset=validate_asset,
                        caption_owner=(report_identity or {}).get('figure_caption_owner', 'markdown'),
                        **({'citation_style': request.citation_style} if request.citation_style is not None else {}),
                        **({'checks': request.checks.model_dump(exclude_none=True)} if request.checks is not None else {}),
                        **({'template': request.template.model_dump()} if request.template is not None else {}))
                except ReportExportError:
                    if not (output / 'export.json').is_file():
                        raise
                    # A failed preview can still retain accurate compilation
                    # evidence. Return that terminal manifest without delivery.
                    manifest = ArtifactStore(output).read_json('export.json')
                if request.candidate_result and any(hashlib.sha256((render_source.parent/key).read_bytes()).hexdigest() != digest
                        for key, digest in staged_hashes.items()):
                    raise ReportExportError('Prepared candidate inputs changed during publication')
            if report_identity is not None and self._source_identity(request) != report_identity:
                raise ValueError('Canonical authoring identity changed during publication')
            if completed_view := (report_identity or {}).get('prepared_publication'):
                inspected = self.workspace.resolve(completed_view['view_path'])
                if hashlib.sha256(inspected.read_bytes()).hexdigest() != completed_view['view_sha256']:
                    raise ValueError('Independently inspected publication view changed')
                before, after = json.loads(inspected.read_bytes()), ArtifactStore(output).read_json('publication_view.json')
                if before != after:
                    changed = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
                    raise ValueError('Publication differs from the independently inspected candidate in '
                        + ', '.join(changed) + '; retain both without delivery')
            self.workspace.check_artifact_producers((str(source.parent / key)
                for key, digest in binding['inputs'].items() if digest is not None), cache=producer_cache, refresh=True)
            if (self.report_source is not None or request.candidate_result) and export_input_binding(source, request,
                    output_root=self.workspace.workspace / 'publications')['inputs'] != binding['inputs']:
                raise ValueError('Publication authoring inputs changed during rendering')
            artifacts = [f"{prefix}/{file}" for file in ("main.tex", "body.tex", "references.bib", "source.md", "export.json", "main.pdf")
                         if (output / file).is_file()]
            completed = manifest['status'] in {'exported', 'compiled'}
            if request.checks is not None:
                completed = completed and manifest.get('compiled') is True and manifest.get('checks', {}).get('status') == 'passed'
            if request.render_preview:
                completed = completed and manifest.get('compiled') is True and manifest.get('pdf_preview', {}).get('rendered') is True
            hashes = None
            if completed or request.checks is not None or (saved is not None and owner.exists('result.json')):
                if saved is not None and not owner.exists('result.json'):
                    if output_binding_version is not None:
                        raise ValueError('Interrupted publication has no successful output byte binding; inspect retained files and process receipts, do not replay')
                    return ToolResult(tool_name=name, status='blocked', artifacts=artifacts,
                        error='Historical publication has no output byte binding; its manifest records past rendering only. Inspect the retained project or request a new export identity; no automatic delivery or recompilation.',
                        data={'historical_manifest': manifest, 'project': prefix, 'artifacts': artifacts,
                            'output_integrity': 'historical_unbound',
                            'scope': 'Historical rendering receipt only; current project bytes and compilation are unverified.'})
                # Workspace owns bounded directory expansion and file digests.
                # Include figures, editable variants, source attachments, build
                # records and instructions, rather than a second filename list.
                artifacts = self.workspace.resolve_delivery(DeliveryRequest(
                    summary='Editable publication project', artifacts=[prefix]))['artifacts']
                hashes = self.workspace.artifact_hashes(artifacts)
                if saved is not None:
                    retained = owner.read_json('result.json').get('artifact_sha256')
                    if not retained or retained != hashes:
                        raise ValueError('Publication project files changed or are missing; saved rendering applies only to the original byte-bound version, not this project. Preserve both versions; no replay')
                else:
                    if request.checks is not None:
                        receipt = manifest.get('checks', {})
                        observed = {f'{prefix}/main.pdf': receipt.get('pdf_sha256')}
                        observed.update({f"{prefix}/{row['evidence']}": row['evidence_sha256']
                            for row in receipt.get('results', {}).values() if 'evidence_sha256' in row})
                        if any(digest is not None and hashes.get(path) != digest for path, digest in observed.items()):
                            raise ValueError('Publication PDF/check evidence changed after observation; retain files and process receipts, no delivery or replay')
                    owner.write_json('result.json', {'artifact_sha256': hashes,
                        **({'report_identity': report_identity} if report_identity is not None else {})})
            preview_images = [f"{prefix}/{path}" for path in manifest.get('pdf_preview', {}).get('pages', [])]
            delivery = None
            if completed and request.delivery is not None:
                if request.compile_pdf and not (output / 'main.pdf').is_file():
                    raise ValueError('Compiled publication PDF is missing; no delivery')
                delivery = self.workspace.resolve_delivery(request.delivery.model_copy(update={
                    'artifacts': [*request.delivery.artifacts, *artifacts]}))
            return ToolResult(tool_name=name, status="ok" if completed else "error",
                error='' if completed else str((manifest.get('error') if request.checks is not None else None)
                    or manifest.get('compile_note') or manifest.get('error') or 'Publication did not complete; inspect the retained manifest'),
                data={"manifest": manifest, "project": prefix, "artifacts": artifacts,
                    **({'failure_kind': manifest['failure_kind'],
                        'next_action': 'Rendering resource allowance exhausted. Retain the manuscript and failed publication; explicit resource authorization is required before new rendering.'}
                       if manifest.get('failure_kind') == 'resource_exhausted' else {}),
                    **({'report_identity': report_identity} if report_identity is not None else {}),
                    **({'candidate_preparation': {'source_result': request.candidate_result,
                        'adopted': False, 'delivery_authority': False}}
                       if request.candidate_result else {}),
                    **({'artifact_sha256': hashes, 'output_integrity': 'byte_bound'} if hashes is not None else {}),
                    **({'delivery': delivery} if delivery is not None else {}),
                    **({'preview_images': preview_images,
                        'preview_scope': 'All PDF pages rendered; visual quality not reviewed. Pass these locators to inspect_images in groups of at most four if review is needed.'}
                       if request.render_preview and completed else {}),
                    "scope": (manifest['checks']['scope'] if request.checks is not None and 'checks' in manifest
                              else "Rendering only; scientific and visual quality need independent inspection")}, artifacts=artifacts)
        except Exception as exc:
            retained = []
            if 'request' in locals():
                try:
                    source = self.workspace.resolve(request.markdown)
                    if source.is_file() and source.is_relative_to(self.workspace.workspace):
                        retained.append(request.markdown)
                except (ValueError, OSError):
                    pass
            return ToolResult(tool_name=name, status="error",
                artifacts=retained, summary='Export stopped; source manuscript retained.',
                data={'incomplete': True,
                    **({'failure_kind': 'resource_exhausted'} if isinstance(exc, ReportExportResourceError) else {}),
                    'next_action': ('Rendering resource allowance exhausted; retain existing artifacts and request explicit resource authorization before new rendering.'
                        if isinstance(exc, ReportExportResourceError) else
                        'Correct the reported source/asset or build issue, then export a new version. Do not repeat an unchanged failed export.')},
                error=f"Publication unavailable ({type(exc).__name__}: {exc}); inspect saved export/process records. Source and failed version retained; no blind replay.")
