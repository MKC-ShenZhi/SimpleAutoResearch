"""Pure-Python self-contained academic HTML export for environments without Pandoc/LaTeX.

Renders workspace Markdown into a single standalone HTML file with Base64-embedded
local figures, print-ready academic typography (browser Print -> Save as PDF),
and preserved LaTeX math delimiters (with optional KaTeX auto-render).
Never claims or substitutes for compiled PDF output.
"""
from __future__ import annotations

import base64
import html
import mimetypes
from pathlib import Path
import re
from typing import Any, Callable

from simple_ar.core.artifacts import write_json
from simple_ar.report.export import ReportExportError, _asset_path

_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

_ACADEMIC_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  font-family: "Georgia", "Times New Roman", "Noto Serif SC", serif;
  line-height: 1.65;
  color: #1a1a1a;
  background: #ffffff;
  max-width: 860px;
  margin: 0 auto;
  padding: 2.5rem 2rem 4rem;
}
h1, h2, h3, h4, h5, h6 {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans SC", sans-serif;
  color: #111827;
  margin-top: 1.7em;
  margin-bottom: 0.6em;
  line-height: 1.3;
}
h1 { font-size: 1.95rem; border-bottom: 1.5px solid #d1d5db; padding-bottom: 0.45rem; }
h2 { font-size: 1.4rem; border-bottom: 1px solid #e5e7eb; padding-bottom: 0.25rem; }
h3 { font-size: 1.15rem; }
p { margin: 0.75em 0; }
a { color: #1d4ed8; text-decoration: none; }
a:hover { text-decoration: underline; }
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.9em;
  background: #f3f4f6;
  padding: 0.12em 0.35em;
  border-radius: 4px;
}
pre {
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  padding: 0.9rem 1rem;
  overflow-x: auto;
  line-height: 1.45;
}
pre code { background: transparent; padding: 0; font-size: 0.86em; }
blockquote {
  margin: 1em 0;
  padding: 0.4rem 1rem;
  border-left: 3.5px solid #64748b;
  background: #f8fafc;
  color: #334155;
}
table {
  border-collapse: collapse;
  width: 100%;
  margin: 1.2rem 0;
  font-size: 0.93em;
}
thead th {
  border-top: 2px solid #1f2937;
  border-bottom: 1.5px solid #1f2937;
  padding: 0.5rem 0.65rem;
  text-align: left;
  font-weight: 600;
}
tbody td {
  border-bottom: 1px solid #e5e7eb;
  padding: 0.45rem 0.65rem;
  vertical-align: top;
}
tbody tr:last-child td { border-bottom: 2px solid #1f2937; }
figure {
  margin: 1.4rem auto;
  text-align: center;
  page-break-inside: avoid;
}
figure img {
  max-width: 100%;
  height: auto;
  border: 1px solid #e5e7eb;
  border-radius: 4px;
}
figcaption {
  margin-top: 0.45rem;
  font-size: 0.88em;
  color: #4b5563;
}
hr { border: 0; border-top: 1px solid #d1d5db; margin: 1.8rem 0; }
@media print {
  body { max-width: none; padding: 0; font-size: 11pt; }
  pre, figure, table { break-inside: avoid; }
}
""".strip()

_KATEX_HEAD = """
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css" crossorigin="anonymous">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js" crossorigin="anonymous"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js" crossorigin="anonymous"
  onload="if (window.renderMathInElement) { renderMathInElement(document.body, { delimiters: [ {left: '$$', right: '$$', display: true}, {left: '\\\\[', right: '\\\\]', display: true}, {left: '$', right: '$', display: false}, {left: '\\\\(', right: '\\\\)', display: false} ], throwOnError: false }); }"></script>
""".strip()


def _data_uri_for_image(
    source_dir: Path,
    target: str,
    *,
    validate_asset: Callable[[Path], str | None] | None,
    resolve_image: Callable[[Path], Path] | None,
    embedded: list[str],
) -> str:
    cleaned = target.strip()
    if cleaned.startswith("<") and cleaned.endswith(">"):
        cleaned = cleaned[1:-1].strip()
    if re.match(r"^(?:https?:|data:|//)", cleaned, flags=re.IGNORECASE):
        raise ReportExportError(f"Unsupported remote or data URI image target in standalone HTML export: {cleaned}")
    path = _asset_path(source_dir, cleaned, resolve_image=resolve_image)
    if validate_asset is not None:
        validate_asset(path)
    ext = path.suffix.lower()
    mime = _IMAGE_MIME.get(ext) or mimetypes.guess_type(path.name)[0]
    if mime is None or not mime.startswith("image/"):
        raise ReportExportError(f"Unsupported image format for standalone HTML export: {path.name}")
    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    embedded.append(cleaned)
    return f"data:{mime};base64,{b64}"


def _render_inline(
    text: str,
    *,
    image_resolver: Callable[[str], str],
) -> str:
    placeholders: list[str] = []

    def stash(fragment: str) -> str:
        idx = len(placeholders)
        placeholders.append(fragment)
        return f"\x00PH{idx}\x00"

    # 1. Protect inline code spans first.
    def _code_repl(match: re.Match[str]) -> str:
        return stash(f"<code>{html.escape(match.group(1))}</code>")

    work = re.sub(r"`([^`\n]+)`", _code_repl, text)

    # 2. Protect inline/display math delimiters so markdown formatting does not corrupt LaTeX.
    def _math_repl(match: re.Match[str]) -> str:
        return stash(html.escape(match.group(0)))

    work = re.sub(r"\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|(?<!\\)\$(?!\s)([^$\n]+?)(?<!\s)\$", _math_repl, work)

    # 3. Images: ![alt](target)
    def _img_repl(match: re.Match[str]) -> str:
        alt = html.escape(match.group(1))
        uri = image_resolver(match.group(2))
        return stash(f'<img src="{uri}" alt="{alt}">')

    work = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _img_repl, work)

    # 4. Links: [label](href)
    def _link_repl(match: re.Match[str]) -> str:
        label = html.escape(match.group(1))
        href = match.group(2).strip()
        if not re.match(r"^(?:https?://|mailto:|#)", href, flags=re.IGNORECASE):
            return stash(label)
        return stash(f'<a href="{html.escape(href, quote=True)}" rel="noopener noreferrer">{label}</a>')

    work = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link_repl, work)

    # Escape remaining HTML, then apply simple bold/italic formatting.
    escaped = html.escape(work)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", escaped)

    for idx, fragment in enumerate(placeholders):
        escaped = escaped.replace(f"\x00PH{idx}\x00", fragment)
    return escaped


def _is_table_separator(line: str) -> bool:
    stripped = line.strip().strip("|")
    if not stripped or "-" not in stripped:
        return False
    return all(re.fullmatch(r":?-{2,}:?", cell.strip()) for cell in stripped.split("|"))


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _markdown_to_html_body(markdown_text: str, *, image_resolver: Callable[[str], str]) -> tuple[str, str]:
    lines = markdown_text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    inferred_title = ""
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # Fenced code block
        fence_match = re.match(r"^(`{3,}|~{3,})(.*)$", stripped)
        if fence_match:
            fence = fence_match.group(1)
            lang = html.escape(fence_match.group(2).strip())
            code_lines: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith(fence):
                code_lines.append(lines[i])
                i += 1
            if i < n:
                i += 1
            cls = f' class="language-{lang}"' if lang else ""
            out.append(f"<pre><code{cls}>{html.escape(chr(10).join(code_lines))}</code></pre>")
            continue

        # Display math block $$ ... $$
        if stripped.startswith("$$"):
            math_lines = [line]
            if stripped != "$$" and stripped.endswith("$$") and len(stripped) > 4:
                i += 1
            else:
                i += 1
                while i < n:
                    math_lines.append(lines[i])
                    if lines[i].strip().endswith("$$"):
                        i += 1
                        break
                    i += 1
            out.append(f'<div class="math-block">{html.escape(chr(10).join(math_lines))}</div>')
            continue

        # Headings
        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading_match:
            level = len(heading_match.group(1))
            raw_heading = heading_match.group(2).strip()
            if level == 1 and not inferred_title:
                inferred_title = raw_heading
            out.append(f"<h{level}>{_render_inline(raw_heading, image_resolver=image_resolver)}</h{level}>")
            i += 1
            continue

        # Horizontal rule
        if re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", stripped):
            out.append("<hr>")
            i += 1
            continue

        # Standalone figure image: ![caption](path)
        fig_match = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", stripped)
        if fig_match:
            caption = fig_match.group(1).strip()
            uri = image_resolver(fig_match.group(2))
            cap_html = f"<figcaption>{html.escape(caption)}</figcaption>" if caption else ""
            out.append(f'<figure><img src="{uri}" alt="{html.escape(caption)}">{cap_html}</figure>')
            i += 1
            continue

        # Pipe table
        if "|" in line and i + 1 < n and _is_table_separator(lines[i + 1]):
            headers = _split_table_row(line)
            i += 2
            rows: list[list[str]] = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(_split_table_row(lines[i]))
                i += 1
            th_html = "".join(f"<th>{_render_inline(cell, image_resolver=image_resolver)}</th>" for cell in headers)
            body_rows = []
            for row in rows:
                padded = row + [""] * max(0, len(headers) - len(row))
                tds = "".join(
                    f"<td>{_render_inline(cell, image_resolver=image_resolver)}</td>"
                    for cell in padded[: len(headers)]
                )
                body_rows.append(f"<tr>{tds}</tr>")
            out.append(f"<table><thead><tr>{th_html}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>")
            continue

        # Blockquote
        if stripped.startswith(">"):
            quote_lines: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(re.sub(r"^>\s?", "", lines[i].strip()))
                i += 1
            inner = "<br>".join(_render_inline(q, image_resolver=image_resolver) for q in quote_lines)
            out.append(f"<blockquote><p>{inner}</p></blockquote>")
            continue

        # Unordered / ordered list
        if re.match(r"^[-*+]\s+", stripped):
            items: list[str] = []
            while i < n and re.match(r"^\s*[-*+]\s+", lines[i]):
                item_text = re.sub(r"^\s*[-*+]\s+", "", lines[i]).strip()
                items.append(f"<li>{_render_inline(item_text, image_resolver=image_resolver)}</li>")
                i += 1
            out.append(f"<ul>{''.join(items)}</ul>")
            continue
        if re.match(r"^\d+\.\s+", stripped):
            items = []
            while i < n and re.match(r"^\s*\d+\.\s+", lines[i]):
                item_text = re.sub(r"^\s*\d+\.\s+", "", lines[i]).strip()
                items.append(f"<li>{_render_inline(item_text, image_resolver=image_resolver)}</li>")
                i += 1
            out.append(f"<ol>{''.join(items)}</ol>")
            continue

        # Paragraph
        para_lines = [stripped]
        i += 1
        while i < n:
            nxt = lines[i].strip()
            if (
                not nxt
                or nxt.startswith("#")
                or nxt.startswith(">")
                or nxt.startswith("```")
                or nxt.startswith("~~~")
                or nxt.startswith("$$")
                or re.match(r"^[-*+]\s+", nxt)
                or re.match(r"^\d+\.\s+", nxt)
                or ("|" in lines[i] and i + 1 < n and _is_table_separator(lines[i + 1]))
            ):
                break
            para_lines.append(nxt)
            i += 1
        out.append(f"<p>{_render_inline(' '.join(para_lines), image_resolver=image_resolver)}</p>")

    return "\n".join(out), inferred_title


def export_html_report(
    source_dir: Path,
    output_dir: Path,
    *,
    title: str | None = None,
    body_name: str = "report.md",
    validate_asset: Callable[[Path], str | None] | None = None,
    resolve_image: Callable[[Path], Path] | None = None,
    include_katex_cdn: bool = True,
) -> dict[str, Any]:
    """Export workspace Markdown + referenced images into a standalone academic HTML file."""
    source_dir = Path(source_dir).resolve()
    output_dir = Path(output_dir).resolve()
    source_path = source_dir / body_name
    if not source_path.is_file():
        raise ReportExportError(f"Markdown source not found: {source_path}")
    markdown_text = source_path.read_text(encoding="utf-8")

    embedded_images: list[str] = []

    def resolve_img(target: str) -> str:
        return _data_uri_for_image(
            source_dir,
            target,
            validate_asset=validate_asset,
            resolve_image=resolve_image,
            embedded=embedded_images,
        )

    body_html, inferred_title = _markdown_to_html_body(markdown_text, image_resolver=resolve_img)
    doc_title = (title or inferred_title or source_path.stem).strip()
    katex_block = _KATEX_HEAD if include_katex_cdn else ""
    document = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"  <title>{html.escape(doc_title)}</title>\n"
        f"  <style>\n{_ACADEMIC_CSS}\n  </style>\n"
        + (f"  {katex_block}\n" if katex_block else "")
        + "</head>\n"
        "<body>\n"
        "<article>\n"
        f"{body_html}\n"
        "</article>\n"
        "</body>\n"
        "</html>\n"
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "source.md").write_text(markdown_text, encoding="utf-8")
    (output_dir / "index.html").write_text(document, encoding="utf-8")
    manifest: dict[str, Any] = {
        "schema_version": "report_export_html.v1",
        "status": "exported",
        "format": "html",
        "compiled": False,
        "title": doc_title,
        "source": body_name,
        "html": "index.html",
        "embedded_images": list(dict.fromkeys(embedded_images)),
        "self_contained": True,
        "note": "Standalone academic HTML with Base64-embedded figures; does not substitute for compiled PDF.",
    }
    write_json(output_dir / "export.json", manifest)
    return manifest
