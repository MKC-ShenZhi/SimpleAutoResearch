"""Export existing report artifacts through Pandoc, without running research."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable, Literal
from urllib.parse import unquote, urlsplit


class ReportExportError(ValueError):
    pass


class ReportExportResourceError(ReportExportError):
    """Resource owner refused further dispatch; do not attempt later checks."""


def validate_publication_checks(checks: dict[str, Any]) -> None:
    """Bound user literals only; no policy discovery, regex or inferred identity."""
    if not isinstance(checks, dict) or not checks or set(checks) - {'max_pages', 'forbidden_identifiers'}:
        raise ReportExportError('checks requires max_pages and/or forbidden_identifiers')
    if 'max_pages' in checks and (type(checks['max_pages']) is not int or checks['max_pages'] < 1):
        raise ReportExportError('max_pages must be a positive integer')
    if 'forbidden_identifiers' in checks:
        items = checks['forbidden_identifiers']
        if (not isinstance(items, list) or not 1 <= len(items) <= 32
                or any(not isinstance(item, str) or not item.strip() or len(item) > 200
                       or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in item) for item in items)
                or len(set(items)) != len(items)):
            raise ReportExportError('forbidden_identifiers requires 1 to 32 unique nonblank literals, at most 200 characters each, without control characters')


def publication_template_inputs(root: Path, template: dict[str, Any]) -> dict[str, Path]:
    """Explicit, flat trusted-TeX bundle; no discovery, downloads or scripts.

    Paths are relative to the manuscript, companions to the template directory.
    Copy companions to the project root so ordinary TeX class/style lookup works.
    This bounds imported bytes, not the behavior of trusted TeX or installed packages.
    """
    root = root.resolve()
    name = template['path']
    relative = Path(name)
    if (relative.is_absolute() or '\\' in name or '..' in relative.parts
            or relative.suffix.lower() not in {'.tex', '.latex'}):
        raise ReportExportError('Use a manuscript-relative Pandoc .tex/.latex template without traversal')
    assets = template.get('assets', [])
    if len(assets) > 32 or len({item.lower() for item in assets}) != len(assets):
        raise ReportExportError('Template companions must be unique, at most 32 files')
    files = {'template.latex': root / relative}
    for asset in assets:
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', asset)
                or Path(asset).suffix.lower() not in {'.cls', '.sty', '.bst', '.tex', '.png', '.jpg', '.jpeg', '.pdf'}
                or asset.lower() in {'main.tex', 'main.pdf', 'body.tex', 'references.bib', 'source.md', 'template.latex'}):
            raise ReportExportError('Unsupported or reserved template companion filename')
        files[asset] = root / relative.parent / asset
    total = 0
    for target, path in files.items():
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ReportExportError('Missing or out-of-scope template input: ' + str(path))
        if any(part.startswith('.') for part in (*path.relative_to(root).parts, *resolved.relative_to(root).parts)):
            raise ReportExportError('Private template inputs are not supported')
        total += resolved.stat().st_size
        if total > 16_000_000:
            raise ReportExportError('Template bundle exceeds 16 MB')
    text = files['template.latex'].read_text(encoding='utf-8')
    header_hook = re.search(r'\$for\(header-includes\)\$\s*\$header-includes\$\s*\$endfor\$', text)
    if ('$body$' not in text or header_hook is None or r'\begin{document}' not in text
            or not header_hook.end() <= text.index(r'\begin{document}') < text.index('$body$')):
        raise ReportExportError('Pandoc template requires $body$ and the standard header-includes hook in the preamble')
    # Pandoc partials could read an unlisted file during template expansion.
    # Support variables/conditionals/loops only, not partial/function syntax.
    # Consume every slot in order, including escaped literal dollars ($$), so
    # parentheses in ordinary TeX between slots are not mistaken for partials.
    directives = [match[1] for match in re.finditer(r'\$\$|\$([^$]*)\$', text) if match[1] is not None]
    # Reject alternative braced directives as well, so a partial cannot evade
    # this deliberately narrow dialect. No fallback to Pandoc's partial search.
    if '${' in text or any(('(' in item or ')' in item)
            and not re.fullmatch(r'(?:if|elseif|for)\([A-Za-z0-9_.-]+\)', item) for item in directives):
        raise ReportExportError('Pandoc template partials/functions are not supported; inline them explicitly')
    if template.get('engine', 'pdflatex') not in {'pdflatex', 'xelatex'}:
        raise ReportExportError('Supported template engines: pdflatex, xelatex; BibTeX only')
    if template.get('top_level_division') not in {None, 'section', 'chapter', 'part'}:
        raise ReportExportError('Supported top-level divisions: section, chapter, part')
    return files


def _run(argv: list[str], *, cwd: Path, text: str | None = None, timeout: int = 60,
         env: dict[str, str] | None = None) -> str:
    result = subprocess.run(argv, cwd=cwd, input=text, text=True, encoding="utf-8",
                            errors="replace", capture_output=True, timeout=timeout, env=env)
    if result.returncode:
        raise ReportExportError(f"{Path(argv[0]).name} failed: {(result.stderr or result.stdout)[-2500:]}")
    return result.stdout


def _nodes(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from _nodes(item)


def _asset_path(root: Path, target: str) -> Path:
    url = urlsplit(target)
    if url.scheme or url.netloc or url.query or url.fragment:
        raise ReportExportError(f"Export requires a local image inside the report directory: {target}")
    path = (root / unquote(url.path)).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ReportExportError(f"Missing or out-of-scope report image: {target}")
    if path.suffix.lower() not in {".svg", ".png", ".jpg", ".jpeg", ".pdf"}:
        raise ReportExportError(f"Unsupported report image format: {path.suffix}")
    return path


def _copy_image(source: Path, output: Path, index: int | str, *, convert_svg: bool = True,
                runner: Callable | None = None) -> str:
    assets = output / "figures"
    assets.mkdir(exist_ok=True)
    target = assets / f"figure-{index}{source.suffix.lower()}"
    shutil.copyfile(source, target)
    if source.suffix.lower() == ".svg":
        # Framework figures are self-contained. Do not follow external image or
        # stylesheet references while converting a supplied SVG.
        text = source.read_text(encoding="utf-8")
        references = re.findall(r"url\(([^)]*)\)", text, re.I)
        if re.search(r"<!DOCTYPE[^>]*\[|<!ENTITY|@import|<\?xml-stylesheet", text, re.I) or any(
            not css.strip().strip("'\"").startswith("#") for css in references
        ):
            raise ReportExportError("SVG contains external resource references; use a self-contained figure.")
        # Plotting libraries can emit an external DTD declaration even when
        # every rendered element is self-contained. Remove that declaration
        # before parsing/conversion; do not fetch it or allow internal entities.
        normalized = re.sub(r"<!DOCTYPE[^>]*>", "", text, flags=re.I)
        import xml.etree.ElementTree as ET
        try:
            svg = ET.fromstring(normalized)
        except ET.ParseError as exc:
            raise ReportExportError("Malformed SVG image.") from exc
        if svg.tag not in {"svg", "{http://www.w3.org/2000/svg}svg"} or any(
            element.tag.rsplit("}", 1)[-1] in {"script", "foreignObject"} for element in svg.iter()
        ):
            raise ReportExportError("Export requires a static SVG figure.")
        # Rasterized plot components (for example a heatmap) are still
        # self-contained when embedded in an image element. They are not
        # external fetches or a promise that every mark is editable vector art.
        for element in svg.iter():
            for name, value in element.attrib.items():
                if name.rsplit("}", 1)[-1] != "href":
                    continue
                value = value.strip()
                if not (value.startswith("#") or (
                        element.tag.rsplit("}", 1)[-1] == "image"
                        and re.fullmatch(r"data:image/(?:png|jpeg);base64,[A-Za-z0-9+/=\s]+", value, re.I))):
                    raise ReportExportError("SVG contains external resource references; use a self-contained figure.")
        if normalized != text:
            target.write_text(normalized, encoding="utf-8")
        if not convert_svg:
            return target.relative_to(output).as_posix()
        converter = shutil.which("rsvg-convert")
        if not converter:
            raise ReportExportError("SVG export needs rsvg-convert (librsvg); the original report is unchanged.")
        pdf = target.with_suffix(".pdf")
        (runner or _run)([converter, "--format=pdf", "--output", str(pdf), str(target)], cwd=output)
        target = pdf
    return target.relative_to(output).as_posix()


def _copy_experiment_records(root: Path, output: Path) -> list[str]:
    """Copy the native recorded package, not arbitrary local link targets."""
    package_path = root / "experiment_evidence.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    if package.get("schema_version") != "report_experiment_evidence.v1":
        raise ReportExportError("Unrecognized experiment record package.")
    sources = package.get("source_artifacts")
    if not isinstance(sources, list) or any(not isinstance(row, dict)
            or not isinstance(row.get("copied_path"), str)
            or not re.fullmatch(r"experiment_sources/[0-9]{3}(?:\.[A-Za-z0-9_-]+)?", row["copied_path"]) for row in sources):
        raise ReportExportError("Experiment record source manifest is invalid.")
    paths = list(dict.fromkeys(["experiment_evidence.md", "experiment_evidence.json",
                               *(row["copied_path"] for row in sources)]))
    for relative in paths:
        source = root / relative
        if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(root):
            raise ReportExportError(f"Missing or out-of-scope experiment attachment: {relative}")
    for relative in paths:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
    return paths


def _plain_inlines(inlines: list[dict]) -> str:
    return " ".join(str(node.get("c", "")) for node in _nodes(inlines) if node.get("t") == "Str")


def _bind_figure_captions(ast: dict, captions: dict[str, list[dict]], *,
                          caption_owner: Literal['manifest', 'markdown'] = 'manifest') -> list[str]:
    """Bind recorded figure captions, for both legacy and modern Pandoc ASTs.

    Managed exports retain manifest precedence; canonical Markdown exports use
    recorded captions only when Pandoc has no authored caption. Only an
    AST-identical adjacent paragraph
    (optionally wrapped in emphasis) is redundant; never infer caption prose.
    Walk block containers too, without rebinding a modern Figure's inner image.
    """
    if caption_owner not in {'manifest', 'markdown'}:
        raise ReportExportError('Unknown figure caption owner')
    bound, blocks = [], []
    pending_caption = None
    for block in ast["blocks"]:
        if pending_caption is not None and block.get("t") in {"Para", "Plain"}:
            inlines = block["c"]
            if len(inlines) == 1 and inlines[0].get("t") == "Emph":
                inlines = inlines[0]["c"]
            if inlines == pending_caption:
                pending_caption = None
                continue
        pending_caption = None
        standalone = block.get("t") == "Figure" or (block.get("t") in {"Para", "Plain"}
            and len(block["c"]) == 1 and block["c"][0].get("t") == "Image")
        images = [node for node in _nodes(block) if node.get("t") == "Image"] if standalone else []
        if len(images) == 1:
            image = images[0]
            path = image["c"][-1][0]
            # Modern Figure captions are separate from accessibility alt text.
            # Legacy implicit figures render the image alt only with the fig: marker.
            caption = image['c'][1] if image['c'][-1][1].startswith('fig:') else []
            authored = bool(caption)
            if block['t'] == 'Figure':
                short_caption, long_caption = block['c'][1]
                authored = bool(short_caption) or any(row.get('c') for row in long_caption)
                caption = (long_caption[0]['c'] if len(long_caption) == 1
                           and long_caption[0].get('t') in {'Para', 'Plain'} else [])
            if path in captions and (caption_owner == 'manifest' or not authored):
                caption = deepcopy(captions[path])
                image["c"][1] = caption
                if block["t"] == "Figure":
                    block["c"][1] = [None, [{"t": "Plain", "c": deepcopy(caption)}]]
                else:
                    image["c"][-1][1] = "fig:"  # legacy implicit-figure marker
                bound.append(path)
            pending_caption = caption or None
        if not standalone:
            def visit(value: Any) -> None:
                if isinstance(value, list):
                    if any(isinstance(row, dict) and row.get('t') in {'Para', 'Plain', 'Figure'} for row in value):
                        nested = {'blocks': value}
                        bound.extend(_bind_figure_captions(nested, captions, caption_owner=caption_owner))
                        value[:] = nested['blocks']
                    else:
                        for row in value:
                            visit(row)
                elif isinstance(value, dict):
                    visit(value.get('c'))
            visit(block.get('c'))
        blocks.append(block)
    ast["blocks"] = blocks
    return bound


def _tex_literal(text: str) -> str:
    """Escape text, grouping each macro/UTF-8 glyph for seqsplit's scanner."""
    # seqsplit scans tokens and otherwise discards ordinary space tokens.
    escapes = {" ": r"\ ", "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$",
               "&": r"\&", "#": r"\#", "%": r"\%", "_": r"\_",
               "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join("{" + escapes.get(char, char) + "}" if char in escapes or ord(char) > 127
                   else char for char in text)


def _breakable_code(text: str) -> str:
    """Let seqsplit wrap escaped code, including unpunctuated identifiers."""
    return r"\texttt{\seqsplit{" + _tex_literal(text) + "}}"


def _prepare_technical_content(fragments: Any, output: Path) -> None:
    """Prepare only the TeX AST; canonical code and display text stay intact."""
    block_index = 0
    for node in _nodes(fragments):
        if node.get('t') == 'Table':
            # Pandoc <=2.9/API1.20 uses five Table fields with numeric widths;
            # modern Table uses six fields with alignment/ColWidth pairs. Both
            # select nonwrapping natural columns when every width is default.
            # Let the writer wrap within the available width without scaling
            # type or losing longtable paging. Any authored width is preserved.
            columns = node['c'][2]
            if len(node['c']) == 5:
                if columns and all(width == 0 for width in columns):
                    node['c'][2] = [1 / len(columns)] * len(columns)
            elif len(node['c']) == 6:
                if columns and all(width['t'] == 'ColWidthDefault' for _, width in columns):
                    for column in columns:
                        column[1] = {'t': 'ColWidth', 'c': 1 / len(columns)}
            else:
                raise ReportExportError('Unsupported Pandoc Table protocol; preserve the source and renderer version')
        elif node.get("t") == "CodeBlock":
            block_index += 1
            path = output / "code" / f"block-{block_index}.txt"
            path.parent.mkdir(exist_ok=True)
            # External verbatim input cannot terminate a TeX environment or
            # execute commands. Only exporter-owned filenames enter TeX.
            path.write_text(node["c"][1], encoding="utf-8")
            node.update(t="RawBlock", c=["latex",
                r"\VerbatimInput[breaklines=true,breakanywhere=true,breaksymbolleft={},breaksymbolright={},breakanywheresymbolpre={},breakanywheresymbolpost={}]{" + path.relative_to(output).as_posix() + "}"])
        elif node.get("t") == "Code":
            node.update(t="RawInline", c=["latex", _breakable_code(node["c"][1])])
        elif (node.get("t") == "Str" and not any(char.isspace() for char in node["c"])
                and ("/" in node["c"] or len(node["c"]) >= 32)):
            # Long prose tokens also occur as checksums/identifiers. Keep
            # their surrounding font; math and link targets are separate nodes.
            node.update(t="RawInline", c=["latex", r"\seqsplit{" + _tex_literal(node["c"]) + "}"])


def _scientific_unicode_preamble(*fragments: str) -> str:
    """Declare scientific glyphs for pdfLaTeX without rewriting source prose.

    Pandoc preserves bare Greek letters and many mathematical symbols. Their
    fixed TeX equivalents work in prose, math and escaped code alike; this is
    not a general multilingual font system or permission to inject a preamble.
    """
    greek = dict(zip("αβγδεζηθικλμνξοπρστυφχψω", (
        "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi o pi rho sigma tau upsilon phi chi psi omega"
    ).split(), strict=True))
    greek.update(dict(zip("ΓΔΘΛΞΠΣΥΦΨΩ", "Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega".split(), strict=True)))
    symbols = {char: "\\" + name if name != "o" else "o" for char, name in greek.items()}
    symbols.update({"ς": r"\varsigma", "ϑ": r"\vartheta", "ϕ": r"\varphi", "ϵ": r"\varepsilon",
                    "∞": r"\infty", "≤": r"\leq", "≥": r"\geq", "≠": r"\neq",
                    "≈": r"\approx", "±": r"\pm", "∓": r"\mp", "×": r"\times",
                    "÷": r"\div", "∈": r"\in", "∉": r"\notin", "∂": r"\partial",
                    "∇": r"\nabla", "∑": r"\sum", "∏": r"\prod", "∫": r"\int",
                    "√": r"\surd", "µ": r"\mu", "−": "-"})
    for chars, operator in (("₀₁₂₃₄₅₆₇₈₉", "_"), ("⁰¹²³⁴⁵⁶⁷⁸⁹", "^")):
        symbols.update({char: "{}" + operator + "{" + str(number) + "}"
                        for number, char in enumerate(chars)})
    for chars, operator in (("₊₋₌₍₎", "_"), ("⁺⁻⁼⁽⁾", "^")):
        symbols.update({char: "{}" + operator + "{" + sign + "}"
                        for char, sign in zip(chars, "+-=()", strict=True)})
    present = set("".join(fragments)) & symbols.keys()
    return "".join(f"\\DeclareUnicodeCharacter{{{ord(char):04X}}}{{\\ensuremath{{{symbols[char]}}}}}\n"
                   for char in sorted(present))


def export_acm_report(report_dir: Path, output_dir: Path, *, title: str | None = None,
                      compile_pdf: bool = False, render_preview: bool = False, body_name: str = "report_body.md",
                      runner: Callable | None = None, validate_asset: Callable[[Path], str | None] | None = None,
                      caption_owner: Literal['manifest', 'markdown'] = 'manifest',
                      template: dict[str, Any] | None = None,
                      checks: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compatibility entry point for the shared Pandoc publication converter.

    Pandoc and optional TeX/librsvg are external rendering dependencies. No
    model is called and no upstream evidence or session state is changed.
    Managed report consumers keep manifest captions by default; direct canonical
    workspace publication explicitly selects Markdown ownership.
    """
    root, output = report_dir.resolve(), output_dir.resolve()
    template_files = publication_template_inputs(root, template) if template is not None else {}
    if caption_owner not in {'manifest', 'markdown'}:
        raise ReportExportError('Unknown figure caption owner')
    if render_preview and not compile_pdf:
        raise ReportExportError("render_preview requires compile_pdf=true")
    if checks is not None:
        validate_publication_checks(checks)
        if not compile_pdf:
            raise ReportExportError('checks requires compile_pdf=true')
    if Path(body_name).name != body_name:
        raise ReportExportError("Report body must be a filename inside the report directory")
    body_path = root / body_name
    run = runner or _run
    if not body_path.is_file():
        raise ReportExportError("Expected report_body.md from report assembly; display-only report.md loses citation keys.")
    if output.exists():
        raise ReportExportError("Choose a new output directory; existing exports are retained.")
    pandoc = shutil.which("pandoc")
    if not pandoc:
        raise ReportExportError("Install Pandoc to export a publication project. No model or experiment is required.")
    source = body_path.read_text(encoding="utf-8")
    # Pandoc otherwise expands tabs before parsing literal code. Preserve
    # code bytes here; TeX owns their display width, not source normalization.
    ast = json.loads(run([pandoc, "--preserve-tabs", "--from=markdown-raw_tex-raw_html+tex_math_single_backslash", "--to=json"], cwd=root, text=source))
    ast["meta"] = {}  # Input metadata must not supply a TeX preamble or includes.
    captions = {}
    figure_variants = {}
    figure_manifest = root / "figures/figures_manifest.json"
    if figure_manifest.exists() and not figure_manifest.resolve().is_relative_to(root):
        raise ReportExportError("Figure manifest escapes the report directory")
    if figure_manifest.is_file():
        for row in json.loads(figure_manifest.read_text(encoding="utf-8")).get("figures", []):
            if row.get("exports"):
                figure_variants[row["path"]] = row["exports"]
            if not row.get("caption"):
                continue
            parsed = json.loads(run([pandoc, "--preserve-tabs", "--from=markdown-raw_tex-raw_html+tex_math_single_backslash", "--to=json"],
                                     cwd=root, text=row["caption"]))["blocks"]
            if len(parsed) != 1 or parsed[0].get("t") not in {"Para", "Plain"}:
                raise ReportExportError("Recorded figure caption must be one Markdown paragraph.")
            if row['path'] in captions and captions[row['path']] != parsed[0]['c']:
                raise ReportExportError("Conflicting recorded captions for the same figure path.")
            captions[row["path"]] = parsed[0]["c"]
    bound_captions = _bind_figure_captions(ast, captions, caption_owner=caption_owner)
    source_ast = deepcopy(ast)
    blocks = ast["blocks"]
    # Explicit titles are literal prose, not one unbreakable identifier.
    title_inlines = [{"t": "Space"} if token.isspace() else {"t": "Str", "c": token}
                    for token in re.split(r"(\s+)", title or "") if token]
    if blocks and blocks[0].get("t") == "Header" and blocks[0]["c"][0] == 1:
        header = blocks.pop(0)
        if not title_inlines:
            title_inlines = header["c"][2]
    if not title_inlines:
        title_inlines = [{"t": "Str", "c": "Research report"}]
    abstract: list[dict] = []
    for index, block in enumerate(blocks):
        if block.get("t") == "Header" and _plain_inlines(block["c"][2]).lower() in {
            "abstract", "abstract / executive summary", "summary", "executive summary",
        }:
            end = index + 1
            while end < len(blocks) and blocks[end].get("t") != "Header":
                end += 1
            abstract = blocks[index + 1:end]
            del blocks[index:end]
            break
    # Markdown reports use H2 for top-level sections; LaTeX starts at section.
    for node in _nodes(blocks):
        if node.get("t") == "Header":
            node["c"][0] = max(1, node["c"][0] - 1)
    citations = {citation["citationId"] for node in _nodes([blocks, abstract])
                 if node.get("t") == "Cite" for citation in node["c"][0]}
    bibliography_path = root / "references.bib"
    if bibliography_path.exists() and not bibliography_path.resolve().is_relative_to(root):
        raise ReportExportError("Bibliography escapes the report directory")
    bibliography = bibliography_path.read_text(encoding="utf-8") if bibliography_path.is_file() else ""
    keys = set(re.findall(r"@\w+\s*\{\s*([^,\s]+)\s*,", bibliography))
    if citations - keys:
        raise ReportExportError("Unresolved bibliography keys: " + ", ".join(sorted(citations - keys)))
    image_sources = {node["c"][-1][0]: _asset_path(root, node["c"][-1][0])
                     for node in _nodes([blocks, abstract]) if node.get("t") == "Image"}
    if validate_asset is not None:
        for path in image_sources.values():
            validate_asset(path)
    output.mkdir(parents=True)
    manifest: dict[str, Any] = {"schema_version": "report_export.v1", "format": "acmart-manuscript",
                               "source_report": str(body_path), "status": "exporting",
                               "compiled": False, "quality_checked": False,
                               "figure_caption_owner": caption_owner,
                               "bound_figure_captions": bound_captions}
    if render_preview:
        manifest['pdf_preview'] = {'rendered': False, 'visual_reviewed': False,
            'max_pages': 12, 'dpi': 120, 'max_dimension_px': 1600, 'pages': []}
    if checks is not None:
        manifest['checks'] = {'rules': deepcopy(checks), 'status': 'blocked',
            'scope': 'User-declared rules only; no official certification. Literal absence does not establish anonymity. Images, metadata and indirect identity are not covered.',
            'results': {key: {'status': 'blocked', 'error': 'No compiled PDF checked'} for key in checks}}
    try:
        if template_files:
            manifest['format'] = 'pandoc-latex-template'
            manifest['template'] = {'path': template['path'], 'engine': template.get('engine', 'pdflatex'),
                'inputs': {}, 'trust': 'User-supplied trusted TeX; restricted compilation is not a sandbox'}
            for target, path in template_files.items():
                content = path.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                if validate_asset is not None:
                    bound_digest = validate_asset(path)
                    if bound_digest is not None and bound_digest != digest:
                        raise ReportExportError('Template input changed during copying')
                (output / target).write_bytes(content)
                manifest['template']['inputs'][path.relative_to(root).as_posix()] = digest
            manifest['compliance'] = {'official_requirements': 'unverified', 'anonymity': 'unverified',
                'page_limits': 'unverified', 'visual_layout': 'unverified',
                'citation_keys': 'checked', 'markdown_image_paths': 'checked'}
        image_targets = {url: _copy_image(path, output, index, runner=runner)
                         for index, (url, path) in enumerate(image_sources.items(), start=1)}
        # The body may prefer a PNG preview while its registered figure has
        # editable vector exports. Preserve those variants without changing
        # the chosen display image or copying unrelated source directories.
        manifest["figure_exports"] = {}
        for index, url in enumerate(image_sources, start=1):
            variants = figure_variants.get(url, {})
            if not isinstance(variants, dict):
                raise ReportExportError("Recorded figure exports must be a format-to-path map.")
            for format_name, target in variants.items():
                if format_name not in {"png", "svg", "pdf", "jpg", "jpeg"} or not isinstance(target, str):
                    raise ReportExportError("Unsupported recorded figure export.")
                source_asset = _asset_path(root, target)
                if validate_asset is not None:
                    validate_asset(source_asset)
                if source_asset.suffix.lower().lstrip(".") != format_name:
                    raise ReportExportError("Recorded figure export format does not match its file.")
                copied = _copy_image(source_asset, output, f"{index}-source-{format_name}", convert_svg=False, runner=runner)
                manifest["figure_exports"].setdefault(url, {})[format_name] = copied
        recorded_links = {node["c"][-1][0] for node in _nodes([blocks, abstract]) if node.get("t") == "Link"}
        copied_records = _copy_experiment_records(root, output) if recorded_links & {
            "experiment_evidence.md", "experiment_evidence.json"} else []
        manifest["experiment_record_files"] = copied_records
        external_sources: list[str] = []
        for node in _nodes([blocks, abstract, source_ast]):
            if node.get("t") == "Image":
                node["c"][-1][0] = image_targets[node["c"][-1][0]]
            elif node.get("t") == "Link":
                target = node["c"][-1][0]
                url = urlsplit(target)
                if target in image_targets:
                    node["c"][-1][0] = image_targets[target]
                elif not url.scheme and not url.netloc and url.path and target not in {"references.bib", *copied_records}:
                    # Source evidence may live outside this report. Do not
                    # silently copy data or ship dangling filesystem links.
                    external_sources.append(target)
                    node.update(t="Span", c=[node["c"][0], node["c"][1]])
        manifest["external_source_references"] = list(dict.fromkeys(external_sources))
        manifest["external_sources_included"] = False
        _prepare_technical_content([title_inlines, blocks, abstract], output)

        def latex(fragment: list[dict]) -> str:
            document = {**ast, "blocks": fragment}
            division = template.get('top_level_division') if template_files else None
            return run([pandoc, "--from=json", "--to=latex", "--natbib", "--no-highlight",
                        *(['--top-level-division=' + division] if division else [])],
                        cwd=output, text=json.dumps(document)).strip()

        heading = latex([{"t": "Plain", "c": title_inlines}])
        body_tex = latex(blocks)
        # Pandoc's bare figure environment defaults to tbp, often pushing
        # figures away from their owning prose. Prefer near-source placement
        # while retaining TeX's pagination/floating decisions; do not force H.
        body_tex = body_tex.replace("\\begin{figure}\n", "\\begin{figure}[htbp]\n")
        (output / "body.tex").write_text(body_tex + "\n", encoding="utf-8")
        abstract_tex = latex(abstract) if abstract else ""
        (output / "references.bib").write_text(bibliography, encoding="utf-8")
        markdown = run([pandoc, "--from=json", "--to=markdown", "--wrap=none"],
                        cwd=output, text=json.dumps(source_ast))
        (output / "source.md").write_text(markdown, encoding="utf-8")
        main = (
            "% acmart manuscript demonstration; no conference submission metadata is inferred.\n"
            "\\documentclass[manuscript,screen,nonacm]{acmart}\n"
            "\\usepackage{longtable,booktabs,array,calc}\n"
            "\\usepackage{fvextra,seqsplit,etoolbox,xparse}\n"
            # Titles/captions are moving arguments: preserve the library call
            # through TeX expansion, and retain literal text in PDF metadata.
            "\\robustify\\seqsplit\n"
            "\\pdfstringdefDisableCommands{\\def\\seqsplit#1{#1}}\n"
            # acmart enables expansion even when a minimal TeX installation
            # falls back to bitmap fonts. Keep protrusion, but do not require
            # scalable fonts just to export an otherwise valid manuscript.
            "\\microtypesetup{expansion=false}\n"
            + '$for(header-includes)$\n$header-includes$\n$endfor$\n'
            +
            "\\providecommand{\\tightlist}{\\setlength{\\itemsep}{0pt}\\setlength{\\parskip}{0pt}}\n"
            "\\providecommand{\\passthrough}[1]{#1}\n"
            "\\settopmatter{printacmref=false}\n"
            "\\citestyle{acmnumeric}\n"
            # ACM can emit literal nested brackets for missing authors/dates.
            # xparse collects balanced optional delimiters before natbib sees
            # the label. Keep this in the portable source, not a build-time
            # rewrite of .bbl; no publication fields or keys are substituted.
            "\\let\\saroriginalbibitem\\bibitem\n"
            "\\RenewDocumentCommand{\\bibitem}{o m}{%\n"
            "  \\IfNoValueTF{#1}{\\saroriginalbibitem{#2}}{\\saroriginalbibitem[{#1}]{#2}}}\n"
            # No author metadata was supplied. Suppress acmart's empty default
            # address footnote; users can add real authors/addresses themselves.
            "\\authorsaddresses{}\n"
            "\\setlength{\\emergencystretch}{1em}\n"
            "\\title[Research report]{$title$}\n\\begin{document}\n"
            + "$if(abstract)$\\begin{abstract}\n$abstract$\n\\end{abstract}\n$endif$"
            + "\\maketitle\n$body$\n"
            + "$if(bibliography)$\\bibliographystyle{ACM-Reference-Format}\n\\bibliography{references}\n$endif$"
            + "\\end{document}\n"
        )
        if not template_files:
            (output / 'template.latex').write_text(main, encoding='utf-8')
        # The same standalone Pandoc expansion owns both built-in and supplied
        # layouts. Only exporter-created metadata reaches the trusted template;
        # canonical YAML/raw TeX remains excluded by the existing reader.
        preamble = ("\\usepackage{longtable,booktabs,array,calc}\n"
            "\\usepackage{fvextra,seqsplit,etoolbox}\n"
            "\\robustify\\seqsplit\n"
            "\\providecommand{\\tightlist}{\\setlength{\\itemsep}{0pt}\\setlength{\\parskip}{0pt}}\n"
            "\\providecommand{\\passthrough}[1]{#1}\n"
            "\\setlength{\\emergencystretch}{1em}\n") if template_files else ''
        # One graphicx owner for built-in and supplied templates, including
        # older Pandoc includegraphics and newer pandocbounded output. Preserve
        # small intrinsic sizes; shrink large figures to both page dimensions
        # with room for captions. Tables remain unscaled, wrapping longtables.
        preamble += ("\\usepackage{graphicx}\n"
            "\\providecommand{\\pandocbounded}[1]{#1}\n"
            "\\makeatletter\n"
            "\\def\\sarmaxwidth{\\ifdim\\Gin@nat@width>\\linewidth\\linewidth\\else\\Gin@nat@width\\fi}\n"
            "\\def\\sarmaxheight{\\ifdim\\Gin@nat@height>0.8\\textheight0.8\\textheight\\else\\Gin@nat@height\\fi}\n"
            "\\makeatother\n"
            "\\setkeys{Gin}{width=\\sarmaxwidth,height=\\sarmaxheight,keepaspectratio}\n")
        if not template_files or template.get('engine', 'pdflatex') == 'pdflatex':
            preamble += _scientific_unicode_preamble(heading, body_tex, abstract_tex, bibliography, source)
        def meta_tex(value: str) -> dict:
            return {'t': 'MetaBlocks', 'c': [{'t': 'RawBlock', 'c': ['latex', value]}]}
        metadata = {'title': meta_tex(heading),
            'header-includes': {'t': 'MetaList', 'c': [meta_tex(preamble)]}}
        if abstract_tex:
            metadata['abstract'] = meta_tex(abstract_tex)
        if citations:
            metadata['bibliography'] = {'t': 'MetaString', 'c': 'references.bib'}
            metadata['biblio-style'] = {'t': 'MetaString', 'c': 'plainnat' if template_files else 'ACM-Reference-Format'}
        main = run([pandoc, '--from=json', '--to=latex', '--natbib', '--no-highlight', '--wrap=none',
            '--standalone', '--template=template.latex'], cwd=output,
            text=json.dumps({**ast, 'meta': metadata,
                'blocks': [{'t': 'RawBlock', 'c': ['latex', r'\input{body.tex}']}]}))
        (output / "main.tex").write_text(main, encoding="utf-8")
        readme = (
            "Editable ACM acmart manuscript demonstration, not a conference-specific submission.\n"
            "Edit main.tex/body.tex/references.bib and the figures. The export does not certify research claims.\n"
            "Class and bibliography style: https://ctan.org/pkg/acmart (install via your TeX distribution).\n"
            "Dependencies: pdflatex, bibtex, acmart and its packages; Pandoc for regeneration; librsvg for SVG conversion.\n"
            "Technical text wrapping requires fvextra and seqsplit from your TeX distribution.\n"
            "Fenced code is literal editable input in code/block-*.txt; keep code/ with the project.\n"
            "Install the full CM-Super font package for Computer Modern fallback text symbols; its minimal 10pt-only subset may prevent restricted builds at other sizes.\n"
            "Compile in this directory: pdflatex -no-shell-escape -halt-on-error main.tex; bibtex main; "
            "pdflatex -no-shell-escape -halt-on-error main.tex (twice).\n"
            "main.tex protects ACM's incomplete-record citation labels using xparse, including ordinary clean builds. "
            "Missing authors/dates remain missing; BibTeX may warn about those fields. Do not replace them with guesses.\n"
            "source.md uses the exported figure paths, not the original report directory.\n"
            "Native experiment record links and their explicitly registered source copies travel with this project.\n"
            "Other local source-evidence links are retained as labels; their original targets are listed in export.json.\n"
            "No arbitrary linked datasets or source directories are copied automatically.\n"
            "The built-in compile option restricts file access and records logs. Add author/venue metadata yourself.\n"
        )
        if template_files:
            readme = (
                'Editable publication converted with the explicitly supplied Pandoc LaTeX template.\n'
                'Canonical Markdown, bibliography and figure inputs were not edited. source.md uses portable figure paths.\n'
                'Keep template.latex, companion files, code/ and figures/ with main.tex/body.tex/references.bib.\n'
                'Supported contract: $body$, $title$, $abstract$, the standard '
                '$for(header-includes)$$header-includes$$endfor$ hook (required in preamble), '
                '$bibliography$, $biblio-style$; no Pandoc partials, scripts, filters, downloads or arbitrary metadata.\n'
                'header-includes contains renderer-owned support only; source YAML metadata is excluded.\n'
                'Templates own class, packages, title/abstract placement and BibTeX style. '
                'Only pdflatex or xelatex with BibTeX is supported; no Biber or external build recipes.\n'
                'Body hierarchy uses the declared top_level_division when supplied; no class-name inference.\n'
                'pdfLaTeX support covers ordinary Latin text and the bounded scientific Greek/math helper, not CJK.\n'
                'Broader Unicode requires optional xelatex and suitable installed fonts/packages selected by the template; '
                'no fonts are provisioned and CJK rendering is unverified.\n'
                'Fenced code remains literal in code/block-*.txt; fvextra and seqsplit are required.\n'
                'Trusted TeX can execute TeX primitives and installed packages. No shell escape is enabled; '
                'file restrictions and resource limits are not a security sandbox. Use only trusted templates/companions.\n'
                'No official requirements, author anonymity, page limit or visual compliance is certified. '
                'Inspect export.json, build.log and any requested full preview independently.\n'
                'Missing authors/dates remain missing. External evidence paths are listed in export.json, not copied.\n'
                f"Rebuild here: {template.get('engine', 'pdflatex')} -no-shell-escape -halt-on-error main.tex; "
                'bibtex main (if cited); repeat the TeX command twice.\n')
        (output / 'README.txt').write_text(readme, encoding='utf-8')
        manifest["status"] = "exported"
        if compile_pdf:
            manifest.update(_compile(output, bool(citations), runner=runner,
                **({'engine': template.get('engine', 'pdflatex')} if template_files else {})))
        if checks is not None:
            if manifest['compiled']:
                _check_publication_pdf(output, manifest['checks'], runner=run)
            if manifest['checks']['status'] != 'passed':
                manifest['status'] = 'checks_failed'
                manifest['error'] = 'Declared publication checks did not pass; inspect checks.results. Editable project retained; no automatic revision or recompilation.'
        if render_preview:
            if not manifest['compiled']:
                raise ReportExportError(manifest.get('compile_note') or "PDF compilation did not produce a PDF; requested preview unavailable")
            manifest['pdf_preview'].update(_render_pdf_preview(output, runner=run))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        manifest.update(status="failed", error=str(exc))
        raise ReportExportError(f"Export failed; retained files and export.json at {output}: {exc}") from exc
    finally:
        (output / "export.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def _check_publication_pdf(output: Path, receipt: dict[str, Any], *, runner: Callable) -> None:
    """Independent observations through the existing runner and process owner."""
    pdf = output / 'main.pdf'
    results = receipt['results']
    try:
        if pdf.is_symlink() or not pdf.is_file() or not 0 < pdf.stat().st_size <= 32_000_000:
            raise ReportExportError('Checks require a nonempty PDF no larger than 32 MB')
        digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    except OSError as exc:
        for row in results.values():
            row.update(status='blocked', error=str(exc))
        return
    except ReportExportError as exc:
        for row in results.values():
            row.update(status='blocked', error=str(exc))
        return
    receipt['pdf_sha256'] = digest
    evidence = output / 'checks'
    evidence.mkdir()
    for rule, row in results.items():
        try:
            executable = shutil.which('pdfinfo' if rule == 'max_pages' else 'pdftotext')
            if not executable:
                raise ReportExportError('Required Poppler tool unavailable: ' + ('pdfinfo' if rule == 'max_pages' else 'pdftotext'))
            argv = ([executable, str(pdf)] if rule == 'max_pages' else
                    [executable, '-enc', 'UTF-8', str(pdf), '-'])
            text = runner(argv, cwd=output, timeout=30, env={'LC_ALL': 'C'})
            raw = text.encode('utf-8', errors='strict')
            if len(raw) > (64_000 if rule == 'max_pages' else 1_000_000) or '\ufffd' in text:
                raise ReportExportError('Poppler output exceeds check bound or contains replacement characters; no complete check')
            path = evidence / ('pdfinfo.txt' if rule == 'max_pages' else 'text.txt')
            path.write_bytes(raw)
            row.clear()
            row.update(evidence=path.relative_to(output).as_posix(),
                evidence_sha256=hashlib.sha256(raw).hexdigest())
            if rule == 'max_pages':
                counts = re.findall(r'^Pages:\s*([0-9]+)\s*$', text, re.M)
                if len(counts) != 1 or int(counts[0]) < 1:
                    raise ReportExportError('Poppler did not report one positive PDF page count')
                count = int(counts[0])
                row.update(status='passed' if count <= receipt['rules'][rule] else 'failed',
                    page_count=count, max_pages=receipt['rules'][rule], scope='All PDF pages, including references and appendices')
            else:
                if not text.strip():
                    raise ReportExportError('PDF has no extractable text; literal check unavailable')
                matches = [{'literal': literal, 'count': text.count(literal)}
                           for literal in receipt['rules'][rule] if literal in text]
                row.update(status='failed' if matches else 'passed', matches=matches,
                    matching='Case-sensitive exact Unicode substrings; no normalization, regex, whitespace joining or OCR',
                    scope='Extracted text only; absence does not establish anonymity. Images, metadata and indirect identity are not covered.')
        except ReportExportResourceError as exc:
            row.update(status='blocked', error=str(exc))
            raise
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            row.update(status='blocked', error=str(exc))
    # Pin both observations to the same visible PDF, including failed rules.
    if hashlib.sha256(pdf.read_bytes()).hexdigest() != digest:
        for row in results.values():
            row.update(status='blocked', error='PDF changed during checks; observations cannot be combined')
    receipt['status'] = ('passed' if all(row['status'] == 'passed' for row in results.values())
                         else 'blocked' if any(row['status'] == 'blocked' for row in results.values()) else 'failed')


def _render_pdf_preview(output: Path, *, runner: Callable) -> dict[str, Any]:
    """Render all actual pages under fixed bounds; never infer visual quality."""
    pdfinfo, pdftoppm = shutil.which('pdfinfo'), shutil.which('pdftoppm')
    if not pdfinfo or not pdftoppm:
        raise ReportExportError("PDF preview requires Poppler pdfinfo and pdftoppm; compiled PDF and editable source retained")
    pdf = output / 'main.pdf'
    if not pdf.is_file() or not pdf.stat().st_size:
        raise ReportExportError("Compiled PDF is missing or empty; requested preview unavailable")
    info = runner([pdfinfo, str(pdf)], cwd=output, timeout=30, env={'LC_ALL': 'C'})
    counts = re.findall(r'^Pages:\s*([0-9]+)\s*$', info, re.M)
    if len(counts) != 1:
        raise ReportExportError("Poppler did not report one PDF page count; no bounded full preview")
    page_count = int(counts[0])
    if not 1 <= page_count <= 12:
        raise ReportExportError(f"PDF has {page_count} pages; full preview requires 1 to 12 pages. No pages silently omitted")
    previews = output / 'previews'
    previews.mkdir()
    runner([pdftoppm, '-png', '-r', '120', '-scale-to', '1600', '-f', '1', '-l', str(page_count),
            str(pdf), str(previews / 'page')], cwd=output, timeout=90)
    expected = [previews / f'page-{page:0{len(str(page_count))}d}.png' for page in range(1, page_count + 1)]
    if set(previews.iterdir()) != set(expected):
        raise ReportExportError("Poppler preview files do not match every requested PDF page; retained output is incomplete or unexpected")
    # Pillow already validates image inputs for inspect_images. Check dimensions
    # before decoding; 4 pages at 5 MiB each fit that tool's 20 MiB input bound.
    from PIL import Image
    for path in expected:
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 5 * 1024 * 1024:
            raise ReportExportError("PDF preview page is missing or exceeds 5 MiB; no complete bounded preview")
        try:
            with Image.open(path) as image:
                if image.format != 'PNG' or not (1 <= image.width <= 1600 and 1 <= image.height <= 1600):
                    raise ReportExportError("PDF preview page exceeds the 1600 pixel dimension limit or is not PNG")
                image.verify()
            with Image.open(path) as image:
                image.load()
        except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
            raise ReportExportError(f"Invalid PDF preview PNG; retained page {path.name}: {exc}") from exc
    return {'rendered': True, 'page_count': page_count,
            'pages': [path.relative_to(output).as_posix() for path in expected]}


def _compile(output: Path, has_citations: bool, *, runner: Callable | None = None,
             engine: str = 'pdflatex') -> dict[str, Any]:
    if engine not in {'pdflatex', 'xelatex'}:
        raise ReportExportError('Unsupported TeX engine')
    latex, bibtex = shutil.which(engine), shutil.which("bibtex")
    if not latex or (has_citations and not bibtex):
        return {"status": "exported", "compiled": False, "compile_note": "TeX unavailable; editable source retained."}
    env = {**os.environ, "openin_any": "p", "openout_any": "p", "shell_escape": "f"}
    command = [latex, "-no-shell-escape", "-halt-on-error", "-interaction=nonstopmode", "main.tex"]
    commands = [command, *([[bibtex, "main"]] if has_citations else []), command, command]
    logs: list[str] = []
    try:
        for argv in commands:
            logs.append((runner or _run)(argv, cwd=output, timeout=90, env=env))
    except (OSError, subprocess.SubprocessError, ReportExportError) as exc:
        logs.append(str(exc))
        return {"status": "compile_failed", "compiled": False, "compile_note": str(exc)}
    finally:
        (output / "build.log").write_text("\n".join(logs), encoding="utf-8")
    compiled = (output / "main.pdf").is_file()
    final_log = (output / "main.log").read_text(encoding="utf-8", errors="replace") if (output / "main.log").is_file() else ""
    warnings = re.findall(r"(?:Class \w+ Warning|LaTeX Warning|Package \w+ Warning):[^\n]*|Overfull \\[hv]box[^\n]*|Missing character:[^\n]*", final_log)
    return {"status": "compiled" if compiled else "compile_failed", "compiled": compiled,
            "compile_warnings": list(dict.fromkeys(warnings))}
