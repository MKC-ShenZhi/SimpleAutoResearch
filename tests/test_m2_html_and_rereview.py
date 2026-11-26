"""Unit tests for v2.12 Milestone 2: pure-Python academic HTML export and incremental re-review dedup."""
from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from simple_ar.report.export import ReportExportError
from simple_ar.report.html_export import export_html_report
from simple_ar.tools.publication import ExportRequest, PublicationToolGateway
from simple_ar.tools.workspace import WorkspaceToolGateway


_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class M2HtmlExportTests(unittest.TestCase):
    def test_export_html_report_embeds_images_tables_and_math_without_pandoc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            out = Path(tmp) / "out"
            (src / "figures").mkdir(parents=True)
            (src / "figures" / "curve.png").write_bytes(_TINY_PNG)
            (src / "report.md").write_text(
                "# Empirical Study\n\n"
                "Inline math $x^2 + y^2 = z^2$ and **bold** text.\n\n"
                "$$\n\\mathcal{L} = \\sum_i \\ell_i\n$$\n\n"
                "| Method | Score |\n|---|---:|\n| Full | 0.91 |\n\n"
                "![Training curve](figures/curve.png)\n",
                encoding="utf-8",
            )
            manifest = export_html_report(src, out, include_katex_cdn=False)
            self.assertEqual(manifest["status"], "exported")
            self.assertEqual(manifest["format"], "html")
            self.assertFalse(manifest["compiled"])
            self.assertEqual(manifest["embedded_images"], ["figures/curve.png"])

            html_text = (out / "index.html").read_text(encoding="utf-8")
            self.assertIn("<title>Empirical Study</title>", html_text)
            self.assertIn("data:image/png;base64,", html_text)
            self.assertIn("<figcaption>Training curve</figcaption>", html_text)
            self.assertIn("<table><thead><tr><th>Method</th><th>Score</th></tr></thead>", html_text)
            self.assertIn("$x^2 + y^2 = z^2$", html_text)
            self.assertIn("\\mathcal{L} = \\sum_i \\ell_i", html_text)
            self.assertIn("@media print", html_text)

    def test_export_html_rejects_out_of_scope_image_and_pdf_only_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            (Path(tmp) / "outside.png").write_bytes(_TINY_PNG)
            (src / "report.md").write_text("![](../outside.png)\n", encoding="utf-8")
            with self.assertRaises(ReportExportError):
                export_html_report(src, Path(tmp) / "out")

        with self.assertRaises(ValueError):
            ExportRequest.model_validate({"markdown": "report.md", "format": "html", "render_preview": True})
        with self.assertRaises(ValueError):
            ExportRequest.model_validate({"markdown": "report.md", "format": "html", "layout": "article"})

    def test_publication_gateway_html_export_and_never_satisfies_pdf_obligation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "task"
            root.mkdir()
            workspace = WorkspaceToolGateway(root, {}, None)
            (workspace.workspace / "figs").mkdir()
            (workspace.workspace / "figs" / "plot.png").write_bytes(_TINY_PNG)
            (workspace.workspace / "report.md").write_text(
                "# Report\n\n![Plot](figs/plot.png)\n", encoding="utf-8"
            )
            gateway = PublicationToolGateway(root, workspace, None, owned_execution=True)
            args = {"markdown": "report.md", "format": "html", "compile_pdf": False}
            result = gateway.call("export_report", args, identity="html-exp-1")
            self.assertEqual(result.status, "ok", result.error)
            self.assertEqual(result.data["manifest"]["format"], "html")
            self.assertFalse(result.data["manifest"]["compiled"])
            self.assertTrue((workspace.workspace / "publications/html-exp-1/index.html").is_file())

            # An explicit PDF obligation MUST reject format="html".
            with self.assertRaisesRegex(ValueError, "PDF obligation"):
                gateway.check_report_delivery(
                    [],
                    None,
                    [],
                    formats=["pdf"],
                    manuscript="report.md",
                    proposed=args,
                )


class M2IncrementalReviewDedupTests(unittest.TestCase):
    def test_incremental_rereview_marks_unchanged_artifacts_and_guides_prompt(self) -> None:
        from simple_ar.tools.inspection import InspectionToolGateway
        from simple_ar.tools.inspection_contracts import InspectionRequest, _WORK_MATERIAL_POLICIES
        from simple_ar.tools.specs import GOAL_WORK_METHOD

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "task"
            root.mkdir()
            workspace = WorkspaceToolGateway(root, {}, None)
            p_changed = str((workspace.workspace / "report.md").resolve())
            p_unchanged = str((workspace.workspace / "metrics.json").resolve())
            p_added = str((workspace.workspace / "fig2.png").resolve())
            mat_policy = sorted(_WORK_MATERIAL_POLICIES)[0]

            gw = InspectionToolGateway(
                root,
                workspace,
                lambda _purpose: None,
                protocol="subjects.v3",
                coverage_policy="offered.v2",
                work_method=GOAL_WORK_METHOD,
                work_lifecycle="integrated.v3",
                material_policy=mat_policy,
                execution_protocol="shared.v2",
            )
            saved = {
                "identity": "rev-2",
                "prior_required": [{"concern": "Fix table 1"}],
                "execution_protocol": "shared.v2",
                "inspection_material_policy": mat_policy,
                "coverage_policy": "offered.v2",
                "delivery_context": {
                    "schema_version": "delivery_review.v3",
                    "scope_policy": "applicable.v1",
                    "purpose": "delivery",
                    "claim": {"summary": "Updated report"},
                    "artifacts": {
                        p_changed: "a" * 64,
                        p_unchanged: "b" * 64,
                        p_added: "c" * 64,
                    },
                },
                "revision_sources": [p_changed],
                "introduced_artifacts": [p_added],
                "removed_artifacts": [],
                "targets": [],
                "evidence": [],
            }
            req = InspectionRequest(goal="Re-review delivery", targets=["report.md"])
            view = gw._view(saved, req)
            rev = view["proposed_delivery"]["revision"]
            self.assertEqual(
                rev["unchanged_since_prior_review"],
                [{"artifact": "workspace/metrics.json", "sha256_prefix": "b" * 12}],
            )
            self.assertIn("unchanged_since_prior_review", rev["scope"])
            self.assertIn("avoid repeating environment/version probes", rev["scope"])

            sys_prompt = gw._system(saved)
            self.assertIn("incremental re-review", sys_prompt)
            self.assertIn("unchanged_since_prior_review", sys_prompt)

            # First-round review (no revision_sources) must NOT inject re-review guidance.
            saved_first = {k: v for k, v in saved.items() if k != "revision_sources"}
            self.assertNotIn("incremental re-review", gw._system(saved_first))


if __name__ == "__main__":
    unittest.main()
