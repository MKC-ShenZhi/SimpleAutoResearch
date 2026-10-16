"""Replaceable report rendering ports.

Explicit renderers keep their public contract. Assembly opts out of historical
template inference; registered data/asset consumers remain the figure owners.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from simple_ar.report.figures import ReportFigureResult, maybe_add_report_figures
from simple_ar.report.schema import ReportDocumentPlan, ReportFigureConfig


@runtime_checkable
class FigureRenderer(Protocol):
    """Render optional figures into the citation-key body, before references."""

    name: str

    def render(
        self,
        *,
        report_markdown: str,
        report_dir: Path,
        config: ReportFigureConfig,
        template_name: str = "",
        document_plan: ReportDocumentPlan | None = None,
        emit: Callable[[str], None] | None = None,
    ) -> ReportFigureResult:
        """Return that body with any supported figure artifacts inserted."""


class DeterministicFigureRenderer:
    """SVG views with an isolated historical config/template selection mode."""

    name = "deterministic_svg"

    def __init__(self, *, legacy: bool = True, section_headings: dict[str, str] | None = None):
        self.legacy = legacy
        self.section_headings = section_headings

    def render(
        self,
        *,
        report_markdown: str,
        report_dir: Path,
        config: ReportFigureConfig,
        template_name: str = "",
        document_plan: ReportDocumentPlan | None = None,
        emit: Callable[[str], None] | None = None,
    ) -> ReportFigureResult:
        return maybe_add_report_figures(
            report_markdown=report_markdown,
            report_dir=report_dir,
            config=config,
            template_name=template_name,
            document_plan=document_plan,
            emit=emit,
            legacy=self.legacy,
            section_headings=self.section_headings,
        )


__all__ = ["DeterministicFigureRenderer", "FigureRenderer"]
