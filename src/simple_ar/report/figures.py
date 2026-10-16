from __future__ import annotations

import html
import re
from dataclasses import dataclass
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, Field

from simple_ar.core.artifacts import write_text
from simple_ar.report.document_plan import visual_plan_for_renderer
from simple_ar.report.schema import ReportDocumentPlan, ReportFigureConfig
# Retain the old import surface; data rendering no longer depends on reporting.
from simple_ar.result_analysis.figures import render_table_figures as render_table_figures
from simple_ar.result_analysis.figures import render_measurement_pairs


_DEFAULT_PAIRED_FIGURE_LIMIT = 4


class ReportFigureRecord(BaseModel):
    """One deterministic report figure artifact."""

    figure_id: str
    title: str
    path: str
    anchor: str
    caption: str = ""
    source_artifacts: list[str] = Field(default_factory=list)
    exports: dict[str, str] = Field(default_factory=dict, exclude_if=lambda value: not value)


class ReportFigureResult(BaseModel):
    """Report markdown plus generated figure metadata."""

    report_markdown: str
    figures: list[ReportFigureRecord] = Field(default_factory=list)


@dataclass(frozen=True)
class _FigureSpec:
    figure_id: str
    title: str
    anchor_patterns: tuple[str, ...]
    section_keywords: tuple[str, ...]
    caption: str


_FIGURE_SPECS: tuple[_FigureSpec, ...] = (
    _FigureSpec(
        figure_id="taxonomy-map",
        title="Conceptual taxonomy map",
        anchor_patterns=("foundations and taxonomy", "taxonomy", "organizing axes"),
        section_keywords=("taxonomy", "axis", "family", "foundation", "class"),
        caption="Figure: a compact taxonomy map derived from the survey structure and comparison tables.",
    ),
    _FigureSpec(
        figure_id="system-construction-flow",
        title="System components overview",
        anchor_patterns=("system construction", "method", "architecture", "construction"),
        section_keywords=("role", "coordination", "grounding", "retrieval", "verification", "memory"),
        caption="Figure: components named in this section; placement does not establish execution order or causal relationships.",
    ),
    _FigureSpec(
        figure_id="evaluation-landscape",
        title="Evaluation landscape",
        anchor_patterns=("evaluation and benchmarks", "evaluation", "benchmarks"),
        section_keywords=("benchmark", "metric", "dataset", "baseline", "cost", "failure"),
        caption="Figure: an evaluation landscape distilled from the benchmark and metric discussion.",
    ),
    _FigureSpec(
        figure_id="challenge-roadmap",
        title="Challenges and future directions",
        anchor_patterns=("challenges and open problems", "future directions", "open problems"),
        section_keywords=("challenge", "gap", "limitation", "risk", "future", "direction"),
        caption="Figure: challenge and future-direction map grounded in the survey's open-problem sections.",
    ),
)


def add_paired_measurement_figures(*, report_markdown: str, report_dir: Path,
                                 comparisons: list[dict], summaries: list[dict], config: ReportFigureConfig,
                                 anchors: dict[str, str] | None = None) -> ReportFigureResult:
    """Plot declared-comparable measured pairs, not values extracted from prose."""
    if not config.enabled or config.mode == "off":
        return ReportFigureResult(report_markdown=report_markdown)
    groups = defaultdict(list)
    membership = {(source["baseline_ref"]["path"], source["candidate_ref"]["path"], summary["metric"]):
                  (summary["group_id"], summary["metric"], summary.get("unit", ""))
                  for summary in summaries for source in summary["sources"]}
    for comparison in comparisons:
        if comparison["comparability"] != "declared_match" or any(comparison[role]["status"] != "passed" for role in ("baseline", "candidate")):
            continue
        for metric in comparison["metrics"]:
            group = membership.get((comparison["baseline_ref"]["path"], comparison["candidate_ref"]["path"], metric["name"]))
            if group is not None:
                groups[group].append((comparison, metric))
    figures, blocks = [], []
    placed_blocks = defaultdict(list)
    body = report_markdown
    ordered_groups = list(groups.items())
    if config.max_figures <= 0:
        # Keep the evidence producer's metric order; no domain-specific metric
        # names should displace the actual task's measurements.
        limit = min(len(ordered_groups), _DEFAULT_PAIRED_FIGURE_LIMIT)
    else:
        limit = config.max_figures
    for index, ((group_id, name, unit), pairs) in enumerate(ordered_groups):
        if index >= limit:
            break
        title = f"{name} ({unit or 'unit not recorded'}) — condition group {group_id}"
        rendered = render_measurement_pairs(
            [{"seed": comparison["seed"], "baseline": metric["baseline"], "candidate": metric["candidate"]}
             for comparison, metric in pairs], title=title, unit=unit, output_dir=report_dir,
            filename=f"figures/paired-{index + 1}.svg")
        sources = [comparison[f"{role}_ref"]["path"] for comparison, _ in pairs for role in ("baseline", "candidate")]
        record = ReportFigureRecord(figure_id=f"paired-{index + 1}", title=title, path=rendered["path"],
            anchor=(anchors or {}).get(name, "Measured comparisons"), caption=rendered["caption"], source_artifacts=list(dict.fromkeys(sources)),
            exports=rendered["exports"])
        figures.append(record)
        block = f"![{name.replace(']', '')}]({record.path})\n\n*{record.caption}*"
        if name in (anchors or {}):
            placed_blocks[record.anchor].append(block)
        else:
            blocks.append(block)
    for anchor, owned_blocks in placed_blocks.items():
        body = _insert_after_heading(body, f"## {anchor}", "\n\n" + "\n\n".join(owned_blocks) + "\n\n")
    if blocks:
        block = "\n\n## Measured comparisons\n\n" + "\n\n".join(blocks) + "\n"
        marker = re.search(r"^## References\s*$", body, re.MULTILINE)
        body = body[:marker.start()] + block + body[marker.start():] if marker else body + block
    return ReportFigureResult(report_markdown=body, figures=figures)


def maybe_add_report_figures(
    *,
    report_markdown: str,
    report_dir: Path,
    config: ReportFigureConfig,
    template_name: str = "",
    document_plan: ReportDocumentPlan | None = None,
    emit=None,
    legacy: bool = True,
    section_headings: dict[str, str] | None = None,
) -> ReportFigureResult:
    """Render declared views; retain unplanned text inference for old contracts.

    Direct calls preserve the historical config/template contract. Assembly
    disables that contract for new work: a quota is not a request for a view.
    """

    if not config.enabled or config.mode == "off":
        return ReportFigureResult(report_markdown=report_markdown)
    if config.format != "svg":
        return ReportFigureResult(report_markdown=report_markdown)
    if document_plan is None and not legacy:
        return ReportFigureResult(report_markdown=report_markdown)
    existing_images = set(re.findall(r"!\[[^\]]*\]\(([^)]+)\)", report_markdown))
    if document_plan is None and existing_images:
        return ReportFigureResult(report_markdown=report_markdown)

    planned_figures = visual_plan_for_renderer(document_plan)
    automatic = len(planned_figures) if document_plan is not None else (3 if template_name.strip().lower() == "survey_long" else 0)
    max_figures = config.max_figures if config.max_figures > 0 else automatic
    if max_figures <= 0:
        return ReportFigureResult(report_markdown=report_markdown)

    sections = _markdown_sections(report_markdown)
    figures_dir = report_dir / "figures"
    generated: list[ReportFigureRecord] = []
    updated = report_markdown

    specs = {spec.figure_id: spec for spec in _FIGURE_SPECS}
    # Legacy reports without an agent-resolved plan preserve the previous
    # conservative renderer behavior. A resolved plan with no visual intent is
    # an explicit decision to render no figures.
    requested = planned_figures if document_plan is not None else list(specs.values())
    sections_by_heading = {section["heading"]: section for section in sections}
    sections_by_id = ({key: sections_by_heading.get(f"## {heading}")
                      for key, heading in section_headings.items()} if section_headings is not None
                     else {_section_id(section["heading"]): section for section in sections})
    for item in requested:
        if len(generated) >= max_figures:
            break
        if isinstance(item, _FigureSpec):
            spec = item
            section = _best_section(sections, spec)
            title = spec.title
        else:
            spec = specs.get(item.view)
            if spec is None:
                continue
            section = sections_by_id.get(item.section_id)
            title = item.title
        if not section:
            continue
        rel_path = f"figures/{spec.figure_id}.svg"
        if rel_path in existing_images:
            continue
        # The chosen section/view already carries the professional intent.
        # Keyword ranking is retained only for the old renderer contract.
        items = _figure_items(section["body"], spec if legacy else None)
        if len(items) < 3:
            if emit is not None:
                emit(f"Skipped {title}: fewer than three grounded labels in the section.")
            continue
        path = figures_dir / f"{spec.figure_id}.svg"
        write_text(path, _render_svg(title, items[:8]))
        image_block = f"\n![{title}]({rel_path})\n\n*{spec.caption}*\n"
        updated = _insert_after_heading(updated, section["heading"], image_block)
        generated.append(
            ReportFigureRecord(
                figure_id=spec.figure_id,
                title=title,
                path=rel_path,
                anchor=section["heading"],
                caption=spec.caption,
                source_artifacts=[] if isinstance(item, _FigureSpec) else list(item.evidence_handles),
            )
        )

    if generated and emit is not None:
        emit(f"Generated {len(generated)} report figure(s).")
    return ReportFigureResult(report_markdown=updated, figures=generated)


def _markdown_sections(markdown: str) -> list[dict[str, str]]:
    lines = markdown.splitlines()
    sections: list[dict[str, str]] = []
    current_heading = ""
    current_lines: list[str] = []
    for line in lines:
        if re.match(r"^##\s+", line):
            if current_heading:
                sections.append({"heading": current_heading, "body": "\n".join(current_lines)})
            current_heading = line.strip()
            current_lines = []
        elif current_heading:
            current_lines.append(line)
    if current_heading:
        sections.append({"heading": current_heading, "body": "\n".join(current_lines)})
    return sections


def _best_section(sections: list[dict[str, str]], spec: _FigureSpec) -> dict[str, str] | None:
    for pattern in spec.anchor_patterns:
        for section in sections:
            if pattern in _normalize_heading(section["heading"]):
                return section
    best: dict[str, str] | None = None
    best_score = 0
    for section in sections:
        text = f"{section['heading']}\n{section['body']}".lower()
        score = sum(text.count(keyword) for keyword in spec.section_keywords)
        if score > best_score:
            best = section
            best_score = score
    return best if best_score > 0 else None


def _normalize_heading(heading: str) -> str:
    text = re.sub(r"^#+\s*", "", heading).strip().lower()
    text = re.sub(r"^\d+(?:\.\d+)*\s+", "", text)
    return text


def _section_id(heading: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _normalize_heading(heading)).strip("_")[:60]


def _figure_items(
    section_body: str,
    spec: _FigureSpec | None,
) -> list[str]:
    candidates = [*_table_first_column_items(section_body),
        *re.findall(r"^###\s+(.+)$", section_body, re.MULTILINE),
        *re.findall(r"^\s*[-*]\s+(?:\*\*)?([^:.;\n*]+)", section_body, re.MULTILINE)]
    unique: dict[str, str] = {}
    for candidate in candidates:
        clean = _clean_item(candidate)
        if clean:
            unique.setdefault(clean.lower(), clean)
    labels = list(unique.values())
    if spec is None:
        return labels[:8]
    matched = [label for label in labels if any(word in label.lower() for word in spec.section_keywords)][:8]
    if len(matched) < 3:
        for label in labels:
            if label not in matched:
                matched.append(label)
            if len(matched) >= 6:
                break
    return matched


def _table_first_column_items(text: str) -> list[str]:
    items: list[str] = []
    in_body = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            in_body = False
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            in_body = True
            continue
        if in_body and cells and cells[0]:
            items.append(cells[0])
    return items


def _clean_item(item: str) -> str:
    text = re.sub(r"\[[^\]]+\]\([^)]+\)", "", item)
    text = re.sub(r"\[[\d,\s]+\]", "", text)
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -:;.,")
    if not text or len(text) > 70:
        return ""
    if text.lower() in {"what it foregrounds", "representative framing", "boundary condition"}:
        return ""
    return text


def _insert_after_heading(markdown: str, heading: str, block: str) -> str:
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == heading.strip():
            insert_at = index + 1
            while insert_at < len(lines) and lines[insert_at].strip() == "":
                insert_at += 1
            return "\n".join(lines[:insert_at]) + block + "\n".join(lines[insert_at:]) + "\n"
    return markdown.rstrip() + "\n" + block


def _render_svg(title: str, items: list[str]) -> str:
    width = 1120
    card_w = 250
    card_h = 82
    gap_x = 28
    gap_y = 32
    columns = 3 if len(items) > 4 else 2
    rows = (len(items) + columns - 1) // columns
    height = 120 + rows * card_h + max(0, rows - 1) * gap_y + 30
    start_x = (width - (columns * card_w + (columns - 1) * gap_x)) // 2
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">',
        "<style>text{font-family:Arial,Helvetica,sans-serif}.title{font-size:28px;font-weight:700;fill:#172033}.node{fill:#f8fafc;stroke:#475569;stroke-width:1.5}.label{font-size:17px;fill:#172033}.index{font-size:13px;fill:#64748b}</style>",
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text class="title" x="{width // 2}" y="48" text-anchor="middle">{html.escape(title)}</text>',
    ]
    for idx, item in enumerate(items):
        row = idx // columns
        col = idx % columns
        x = start_x + col * (card_w + gap_x)
        y = 86 + row * (card_h + gap_y)
        cx = x + card_w // 2
        parts.append(f'<rect class="node" x="{x}" y="{y}" rx="10" ry="10" width="{card_w}" height="{card_h}"/>')
        parts.append(f'<text class="index" x="{x + 18}" y="{y + 24}">{idx + 1:02d}</text>')
        for line_no, line in enumerate(_wrap_text(item, max_chars=26)[:2]):
            parts.append(
                f'<text class="label" x="{cx}" y="{y + 42 + line_no * 22}" text-anchor="middle">{html.escape(line)}</text>'
            )
    # A list of labels is not evidence for arrows or a causal/temporal chain.
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _wrap_text(text: str, *, max_chars: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word[:max_chars]
    if current:
        lines.append(current)
    return lines or [text[:max_chars]]
