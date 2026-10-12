from __future__ import annotations

from importlib import resources
from pathlib import Path
import re

from simple_ar.report.schema import ReportMemory, ReportRuntimeConfig, ReportTemplateBundle


class ReportTemplateError(RuntimeError):
    """Raised when a report template or criteria file cannot be loaded."""


MATERIAL_REPORT_TEMPLATE = "material_report"
BUILTIN_TEMPLATE_NAMES = {"source_review", "survey", "survey_long", "experiment", "reproduction", "analysis_report", MATERIAL_REPORT_TEMPLATE}


def is_builtin_template(template: ReportTemplateBundle, config: ReportRuntimeConfig | None) -> bool:
    return (template.name in BUILTIN_TEMPLATE_NAMES
        and (config is None or config.template in {"", "auto", *BUILTIN_TEMPLATE_NAMES}))


def intended_use_guidance(template: ReportTemplateBundle) -> str:
    # Remove fallback chapter assignments, not genre-specific composition.
    # These blocks guide argument and prose without competing with the plan.
    return "\n\n".join(f"## {heading}\n\n{body.strip()}" for heading, body in
        re.findall(r"(?ims)^##\s+(Intended Use|Writing Principles)\s*$\n(.*?)(?=^##\s|\Z)",
                   template.template_markdown))


def planning_template_guidance(template: ReportTemplateBundle, config: ReportRuntimeConfig) -> str:
    """Adaptive planning needs genre, not the fallback's chapter assignments.

    Removing a second topology only after planning is too late: the planner
    can already have copied it. Custom and explicitly fixed templates keep
    their author's complete structure. Never infer blocks in legacy prose.
    """
    if config.outline_strategy == "template" or not is_builtin_template(template, config):
        return template.template_markdown
    return intended_use_guidance(template)


def _adapted_builtin_plan(
    template: ReportTemplateBundle, memory: ReportMemory, config: ReportRuntimeConfig | None = None,
) -> bool:
    return bool(memory.document_plan is not None and is_builtin_template(template, config)
        and memory.outline_planning.get("strategy") in {"evidence_organized_outline", "topic_specific_outline"}
        and memory.outline_planning.get("status") == "adapted")


def drafting_template_guidance(
    template: ReportTemplateBundle, memory: ReportMemory, config: ReportRuntimeConfig | None = None,
) -> str:
    """Do not reintroduce a built-in topology after evidence planning replaces it.

    The template's intended-use boundary remains relevant. Its old headings
    and draft order are not a second writing plan; the frozen document owns
    those responsibilities, including adapted surveys. Custom/unadapted paths
    remain unchanged.
    """
    if not _adapted_builtin_plan(template, memory, config):
        return template.template_markdown
    return intended_use_guidance(template)


def reviewing_template_guidance(
    template: ReportTemplateBundle, memory: ReportMemory, config: ReportRuntimeConfig | None,
) -> str:
    """Default structure is a fallback, not a second frozen document plan.

    Only the explicitly separated built-in structural block is projected out.
    Factual checks/output requirements remain; custom criteria and old unsplit
    snapshots are never guessed apart, modified or replaced with newer assets.
    """
    if (config is None or config.criteria not in {"", "auto", *BUILTIN_TEMPLATE_NAMES}
            or not _adapted_builtin_plan(template, memory, config)):
        return template.criteria_markdown
    return re.sub(r"(?ms)^## Default Structure\s*\n.*?(?=^## |\Z)", "",
                  template.criteria_markdown).strip() if "## Default Structure" in template.criteria_markdown else template.criteria_markdown


def resolve_research_only_delivery(config, *, source_count: int):
    """Choose a report shape that the available source set can support.

    A single source can support a critical source review, not a cross-paper
    taxonomy. An explicit template remains authoritative and is never changed.
    """
    if config.template not in {"", "auto"}:
        return config, {"template": config.template, "reason": "Use the explicitly requested research-only template."}
    if source_count <= 1:
        return config.model_copy(update={"template": "source_review"}), {
            "template": "source_review",
            "reason": "At most one citable source is available; do not imply a multi-source survey or method taxonomy.",
        }
    return config, {"template": "survey", "reason": "Multiple citable sources are available for a survey."}


def resolve_experiment_delivery(config, analysis, decision):
    """Select a delivery structure; never equate process success with science."""
    goal = dict(analysis.get("goal_assessment") or {})
    if config.template not in {"", "auto"}:
        template, reason = config.template, "Use the explicitly requested template without changing evidence."
    elif goal.get("requested_delivery") in {"paper", "analysis_report"}:
        template = "experiment" if goal["requested_delivery"] == "paper" else "analysis_report"
        reason = "Honor the explicit delivery requirement identified in the task; preserve negative and uncertain findings."
    elif goal.get("task_type") == "reproduction":
        template, reason = "reproduction", "Explain reproduction conditions and differences; improvement is not required."
    elif (goal.get("status") == "met" and goal.get("task_type") != "unknown"
          and goal.get("evidence_refs") and analysis.get("status") == "passed"
          and decision.get("disposition") != "deliver_with_limits"):
        template, reason = "experiment", "The analysis judges the stated goal met with referenced evidence."
    else:
        template, reason = "analysis_report", "The goal is unmet or uncertain; deliver observations, limitations and continuation options."
    delivery = {"template": template, "reason": reason, "goal_assessment": goal,
                "stop_reason": decision.get("decision_reason", ""),
                "continuation_options": decision.get("continuation_options", [])}
    return config.model_copy(update={"template": template}), delivery


def load_report_template_bundle(
    *,
    report_mode: str,
    config: ReportRuntimeConfig,
    project_root: Path | None = None,
) -> ReportTemplateBundle:
    """Load the Markdown report template and reviewer criteria.

    Args:
        report_mode: Resolved report mode from the pipeline.
        config: Report runtime config.
        project_root: Repository root. Defaults to current working directory.

    Returns:
        A template bundle containing writing and review protocols.
    """
    root = project_root or Path.cwd()
    template_root = _template_root(root)
    name = _resolve_template_name(report_mode, config.template)
    template_path = _resolve_markdown_path(
        value=config.template,
        default_path=template_root / f"{name}.md",
        root=root,
    )
    criteria_path = _resolve_markdown_path(
        value=config.criteria,
        default_path=template_root / "criteria" / f"{name}_review.md",
        root=root,
        auto_values={"", "auto"},
    )
    return ReportTemplateBundle(
        name=name,
        mode=report_mode,
        template_path=str(template_path),
        criteria_path=str(criteria_path),
        template_markdown=_read_markdown(template_path),
        criteria_markdown=_read_markdown(criteria_path),
    )


def _resolve_template_name(report_mode: str, value: str) -> str:
    text = str(value or "").strip()
    if text in {"", "auto"}:
        if report_mode == "supplied_materials":
            return MATERIAL_REPORT_TEMPLATE
        return "experiment" if report_mode == "experiment" else "survey"
    if text in BUILTIN_TEMPLATE_NAMES:
        return text
    path = Path(text)
    if path.suffix.lower() in {".md", ".markdown"}:
        return path.stem
    return text


def _template_root(root: Path) -> Path:
    local = root / "templates" / "report"
    if local.exists():
        return local
    packaged = resources.files("simple_ar").joinpath("report_templates")
    if packaged.is_dir():
        return Path(str(packaged))
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "templates" / "report"


def _resolve_markdown_path(
    *,
    value: str,
    default_path: Path,
    root: Path,
    auto_values: set[str] | None = None,
) -> Path:
    auto = auto_values or {"", "auto"}
    text = str(value or "").strip()
    if text in auto or text in BUILTIN_TEMPLATE_NAMES:
        path = default_path
    else:
        raw = Path(text)
        path = raw if raw.is_absolute() else root / raw
    if not path.exists():
        raise ReportTemplateError(f"Report template file not found: {path}")
    if path.suffix.lower() not in {".md", ".markdown"}:
        raise ReportTemplateError(f"Report template must be Markdown: {path}")
    return path


def _read_markdown(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ReportTemplateError(f"Report template file is empty: {path}")
    return text
