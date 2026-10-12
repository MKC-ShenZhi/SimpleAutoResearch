from __future__ import annotations

import re

from simple_ar.report.schema import (
    ClaimEvidenceRecord,
    ReportContext,
    ReportMemory,
    ReportSectionPlan,
    ReportTemplateBundle,
)
from simple_ar.report.survey import enrich_survey_sections, route_section_sources


SECTION_PATTERN = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
DRAFT_ORDER_PATTERN = re.compile(r"(?im)^draft\s+order\s*:\s*(.+?)\s*$")
NON_DRAFT_SECTION_NAMES = {
    "intended use",
    "output expectations",
    "review goal",
    "required checks",
    "writing workflow",
    "writing principles",
    "writing order",
    "generation strategy",
    "references",
}


def initialize_report_memory(
    *,
    context: ReportContext,
    template: ReportTemplateBundle,
) -> ReportMemory:
    """Create compact report memory from template and stage context."""
    sections = enrich_survey_sections(
        _section_plan(template.template_markdown, context),
        context=context,
    )
    claims = _initial_claims(context)
    limitations = _initial_limitations(context)
    return ReportMemory(
        objective=context.problem_markdown.strip()[:1200] or context.topic,
        template=template.name,
        report_mode=context.report_mode,
        survey_contract=context.survey_contract,
        section_plan=sections,
        claims_evidence_matrix=claims,
        source_handles=context.source_handles,
        metric_sources=context.metric_sources,
        limitations=limitations,
        key_decisions=[
            "Report generation must use only current-run evidence, metrics, and paper ids.",
            "Unsupported claims should be weakened or moved to limitations/future work.",
        ],
    )


def _section_plan(template_markdown: str, context: ReportContext) -> list[ReportSectionPlan]:
    matches = list(SECTION_PATTERN.finditer(template_markdown))
    headings = [match.group(1).strip() for match in matches
                if match.group(1).strip().lower() not in NON_DRAFT_SECTION_NAMES]
    guidance = {
        _heading_key(match.group(1)): template_markdown[match.end():
            matches[index + 1].start() if index + 1 < len(matches) else len(template_markdown)].strip()
        for index, match in enumerate(matches)
    }
    if not headings:
        headings = _fallback_headings(context.report_mode)
    evidence_handles = _section_evidence_handles(context)
    draft_order_map = _template_draft_order(template_markdown, headings)
    sections: list[ReportSectionPlan] = []
    for index, heading in enumerate(headings, start=1):
        section_id = _slug(heading) or f"section_{index}"
        goal = _section_goal(heading, context.report_mode)
        # Do not discard per-section template instructions and replace them
        # with the title heuristic. Preserve the latter's evidence routing;
        # the user-authored purpose remains explicit in the frozen plan.
        section_guidance = guidance.get(_heading_key(heading), "")
        if section_guidance:
            goal += "\nTemplate section guidance: " + section_guidance[:800]
            if len(section_guidance) > 800:
                goal += f"\n({len(section_guidance) - 800} guidance characters omitted; full template remains available.)"
        section_handles = evidence_handles
        if context.report_mode == "experiment" and context.max_section_sources > 0:
            execution_handles = [h.handle for h in context.source_handles if h.kind == "experiment"]
            budget = context.max_section_sources
            section_handles = execution_handles[:budget]
            # Evidence-card IDs are created as <paper_id>#claim-N / #method-N.
            # Keep the selected design's actual sources ahead of lexical ranking.
            motivation_papers = {
                ref.partition("#")[0] for ref in context.experiment_plan.get("motivation_refs", [])
            }
            motivated = [h.handle for h in context.source_handles
                         if h.kind == "paper" and h.paper_id in motivation_papers]
            ranked = route_section_sources(
                context=context, heading=heading,
                goal=f"{context.hypothesis_markdown} {goal}",
                contract={}, budget=budget,
            )
            # A small window must not starve source attribution. Method/background
            # sections need the paper first; results need the measured artifact
            # first. Verified local metrics remain available separately in prompts.
            paper_first = goal.startswith("Ground prose")
            papers = list(dict.fromkeys([*motivated, *ranked]))
            ordered = ([*papers[:1], *section_handles, *papers[1:]] if paper_first
                       else [*section_handles, *papers])
            section_handles = list(dict.fromkeys(ordered))[:budget]
        draft_order = draft_order_map.get(_heading_key(heading), index)
        sections.append(
            ReportSectionPlan(
                section_id=section_id,
                heading=heading,
                goal=goal,
                evidence_handles=section_handles,
                min_citations=int(goal.startswith("Ground prose") and any(
                    handle.handle in section_handles and handle.citation_key
                    for handle in context.source_handles
                )),
                final_order=index,
                draft_order=draft_order,
            )
        )
    return sections


def _section_evidence_handles(context: ReportContext) -> list[str]:
    """Choose a bounded source set for each writer call.

    Paper handles are prioritized because they carry citation keys and concise
    metadata. Extra non-paper handles fill remaining slots only when the paper
    set is smaller than the configured section-source budget.
    """
    if context.report_mode == "supplied_materials":
        # Writing starts from supplied notes/results. Bibliographic sources
        # must not fill the whole window and silently hide those primary inputs.
        materials = [row.handle for row in context.source_handles if row.kind == "material"]
        papers = [row.handle for row in context.source_handles if row.kind == "paper"]
        remaining = [row.handle for row in context.source_handles
                     if row.kind not in {"material", "paper", "chunk", "prior_draft"}]
        ordered = []
        for index in range(max(len(materials), len(papers))):
            if index < len(materials):
                ordered.append(materials[index])
            if index < len(papers):
                ordered.append(papers[index])
        ordered = list(dict.fromkeys([*ordered, *remaining]))
        return ordered[:context.max_section_sources] if context.max_section_sources > 0 else ordered
    experiment_handles = [
        handle.handle for handle in context.source_handles if handle.kind == "experiment"
    ]
    paper_handles = [handle.handle for handle in context.source_handles if handle.kind == "paper"]
    if context.max_section_sources <= 0:
        if context.report_mode == "experiment":
            return [*experiment_handles, *paper_handles] or [
                handle.handle for handle in context.source_handles if handle.kind not in {"chunk", "prior_draft"}
            ]
        if paper_handles:
            return paper_handles
        return [handle.handle for handle in context.source_handles if handle.kind not in {"chunk", "prior_draft"}]

    budget = max(1, context.max_section_sources)
    selected = experiment_handles[:budget] if context.report_mode == "experiment" else []
    selected.extend(path for path in paper_handles if path not in selected)
    selected = selected[:budget]
    if len(selected) >= budget:
        return selected
    selected_set = set(selected)
    for handle in context.source_handles:
        if handle.handle in selected_set or handle.kind == "prior_draft":
            continue
        selected.append(handle.handle)
        selected_set.add(handle.handle)
        if len(selected) >= budget:
            break
    return selected


def _initial_claims(context: ReportContext) -> list[ClaimEvidenceRecord]:
    claims: list[ClaimEvidenceRecord] = []
    for metric in context.metric_sources[:8]:
        claims.append(
            ClaimEvidenceRecord(
                claim_id=f"claim:{_slug(metric.metric_id)}",
                claim=f"Report may discuss metric `{metric.name}` only with value from `{metric.artifact}`.",
                status="supported",
                metric_ids=[metric.metric_id],
                notes="Auto-created metric provenance guard.",
            )
        )
    for handle in context.source_handles[:8]:
        if handle.kind in {"paper", "paper_brief", "chunk"}:
            claims.append(
                ClaimEvidenceRecord(
                    claim_id=f"claim:{handle.handle}",
                    claim=f"Report may cite `{handle.title or handle.handle}` for claims supported by this handle.",
                    status="partially_supported",
                    evidence_handles=[handle.handle],
                    citation_ids=[handle.paper_id] if handle.paper_id else [],
                    notes="Auto-created source provenance guard.",
                )
            )
    return claims


def _initial_limitations(context: ReportContext) -> list[str]:
    limitations: list[str] = []
    if not context.papers:
        limitations.append("No paper metadata was available for citation-backed claims.")
    if context.report_mode == "research_only" and not context.results:
        limitations.append("No experiment results are available; empirical claims must be avoided.")
    if context.search_meta.get("source") == "fixture" or "fixture" in str(context.search_meta.get("status", "")):
        limitations.append("Literature evidence came from fixture/fallback metadata.")
    guard = context.results.get("guard") if isinstance(context.results, dict) else {}
    if isinstance(guard, dict) and guard.get("status") in {"warning", "failed"}:
        limitations.append(
            f"Experiment result guard status is `{guard.get('status')}`; claims must be qualified."
        )
    code_review = context.results.get("code_review") if isinstance(context.results, dict) else {}
    if isinstance(code_review, dict) and code_review.get("status") in {"warning", "failed"}:
        limitations.append(
            f"Generated code review status is `{code_review.get('status')}`; implementation risk must be disclosed."
        )
    recovery = (
        context.results.get("review_failure_recovery")
        if isinstance(context.results, dict)
        else {}
    )
    if isinstance(recovery, dict) and recovery:
        limitations.append(
            "Initial generated code failed review and was replaced by a bounded fallback; "
            "treat implementation claims as recovered rather than fully autonomous."
        )
    return limitations


def _fallback_headings(report_mode: str) -> list[str]:
    if report_mode == "experiment":
        return [
            "Abstract",
            "Introduction",
            "Related Work",
            "Method",
            "Experimental Setup",
            "Results",
            "Discussion",
            "Limitations",
            "Conclusion",
        ]
    return [
        "Abstract",
        "Introduction And Scope",
        "Method Families",
        "Evaluation And Benchmarks",
        "Design Patterns And Failure Modes",
        "Research Gaps And Opportunities",
        "Limitations",
        "Conclusion",
    ]


def _section_goal(heading: str, report_mode: str) -> str:
    lowered = heading.lower()
    if report_mode == "supplied_materials":
        return ("Use supplied papers and materials, attributing source assertions and distinguishing "
                "checked computation from unverified conditions. Do not imply new local experiments.")
    # Mixed headings such as "Target Method And Claimed Result" describe the
    # source method, not exclusively a locally measured result.
    if any(term in lowered for term in ("method", "related", "background", "introduction")):
        return "Ground prose in paper ids, briefs, chunks, and synthesis evidence."
    if "result" in lowered or "experiment" in lowered:
        return "Use only recorded metrics and experiment artifacts; avoid unstaged performance claims."
    if "related" in lowered or "evidence" in lowered or "method" in lowered or "benchmark" in lowered:
        return "Ground prose in paper ids, briefs, chunks, and synthesis evidence."
    if "limitation" in lowered:
        return "State evidence gaps and runtime boundaries clearly."
    if report_mode == "research_only":
        return "Summarize literature evidence without implying experiment execution."
    return "Write this section with traceable evidence and conservative claims."


def _template_draft_order(template_markdown: str, headings: list[str]) -> dict[str, int]:
    """Parse an optional ``Draft order: A -> B`` directive from the template.

    Writing order belongs to the template because different report forms should
    be free to draft sections differently. Unknown names are ignored and
    undeclared headings keep their final display order.
    """
    match = DRAFT_ORDER_PATTERN.search(template_markdown)
    if not match:
        return {}
    heading_positions = {_heading_key(heading): index for index, heading in enumerate(headings, start=1)}
    requested = [
        _heading_key(item)
        for item in re.split(r"\s*(?:->|,|\|)\s*", match.group(1).strip())
        if item.strip()
    ]
    draft_order: dict[str, int] = {}
    order = 1
    for key in requested:
        if key not in heading_positions or key in draft_order:
            continue
        draft_order[key] = order
        order += 1
    for key, final_order in heading_positions.items():
        draft_order.setdefault(key, len(headings) + final_order)
    return draft_order


def _heading_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    return slug[:60]
