"""Template-first publication export through the shared rendering process owner."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
from typing import Any, Literal

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


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    markdown: str = Field(description="Workspace Markdown file; local figures and references.bib use its directory.")
    title: str = Field(default="", max_length=500)
    compile_pdf: bool = True
    checks: PublicationChecks | None = Field(default=None, exclude_if=lambda value: value is None,
        description='Optional finite rules explicitly supplied by the user. Requires compile_pdf=true and installed Poppler tools. Missing tools, bounded-output failure or rule failure retain diagnostics and prevent final delivery. No automatic edits or recompile; absence of literals never certifies anonymity.')
    template: PublicationTemplate | None = Field(default=None,
        description='Explicit user-provided trusted template bundle. Omit for the historical ACM manuscript layout. Compilation does not establish anonymous/page/official compliance.')
    render_preview: bool = Field(default=False, exclude_if=lambda value: value is False,
        description="Explicitly render every compiled PDF page with Poppler: at most 12 pages, 120 DPI scaled to a maximum 1600 pixels per dimension. Requires compile_pdf=true. Returns PNG locators for optional inspect_images; rendering is not visual review. Incomplete previews prevent delivery. False preserves historical request bindings.")
    delivery: DeliveryRequest | None = Field(default=None,
        description="Optional explicit final delivery after successful export/compilation. Exported files are added automatically; artifacts selects other already-existing outputs or retained task evidence. Disclose unresolved work. This does not certify scientific or visual quality. Request this operation alone when delivering.")

    @model_validator(mode="after")
    def validate_preview(self) -> ExportRequest:
        if self.render_preview and not self.compile_pdf:
            raise ValueError("render_preview requires compile_pdf=true")
        if self.checks is not None and not self.compile_pdf:
            raise ValueError('checks requires compile_pdf=true')
        return self


def publication_tool_specs() -> list[CommonToolSpec]:
    return [CommonToolSpec(name="export_report", domain="publication", permission_level="execution",
        risk_level="medium", input_schema=ExportRequest.model_json_schema(),
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
    def can_reconcile(self, name: str, identity: str) -> bool:
        return name == 'export_report' and (
            self.workspace.workspace / 'publications' / identity / 'export.json').is_file() and self.has_started(identity)

    def has_started(self, identity: str) -> bool:
        return (self.root / 'tools' / 'publications' / identity / 'request.json').is_file()

    def __init__(self, root: Path, workspace: Any, ledger: Any) -> None:
        self.root, self.workspace, self.ledger = root, workspace, ledger

    def call(self, name: str, arguments: dict[str, Any], *, identity: str) -> ToolResult:
        try:
            if name != "export_report" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", identity):
                raise ValueError("Invalid publication invocation")
            request = ExportRequest.model_validate(arguments)
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
                    authorized = self.workspace.resolve(str(path))
                    key = authorized.relative_to(source.parent).as_posix()
                    with authorized.open('rb') as file:
                        digest = hashlib.file_digest(file, 'sha256').hexdigest()
                    if binding['inputs'].get(key) != digest:
                        raise ReportExportError('Figure/template is outside the bound authoring inputs or changed during rendering')
                    return digest

                try:
                    manifest = export_acm_report(source.parent, output, title=request.title or None,
                        body_name=source.name, compile_pdf=request.compile_pdf, render_preview=request.render_preview, runner=render,
                        validate_asset=validate_asset, caption_owner='markdown',
                        **({'checks': request.checks.model_dump(exclude_none=True)} if request.checks is not None else {}),
                        **({'template': request.template.model_dump()} if request.template is not None else {}))
                except ReportExportError:
                    if not (output / 'export.json').is_file():
                        raise
                    # A failed preview can still retain accurate compilation
                    # evidence. Return that terminal manifest without delivery.
                    manifest = ArtifactStore(output).read_json('export.json')
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
                    owner.write_json('result.json', {'artifact_sha256': hashes})
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
                    **({'artifact_sha256': hashes, 'output_integrity': 'byte_bound'} if hashes is not None else {}),
                    **({'delivery': delivery} if delivery is not None else {}),
                    **({'preview_images': preview_images,
                        'preview_scope': 'All PDF pages rendered; visual quality not reviewed. Pass these locators to inspect_images in groups of at most four if review is needed.'}
                       if request.render_preview and completed else {}),
                    "scope": (manifest['checks']['scope'] if request.checks is not None and 'checks' in manifest
                              else "Rendering only; scientific and visual quality need independent inspection")}, artifacts=artifacts)
        except Exception as exc:
            return ToolResult(tool_name=name, status="error",
                error=f"Publication unavailable ({type(exc).__name__}: {exc}); inspect saved export/process records. Source and failed version retained; no blind replay.")
