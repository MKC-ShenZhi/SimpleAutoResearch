from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from simple_ar.experiment.code_task_bridge import code_task_project_spec


SUBPROCESS_TIMEOUT_SEC = 45


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_cli_package_does_not_eagerly_import_entrypoint(self) -> None:
        probe = (
            "import sys; import simple_ar.cli; "
            "assert 'simple_ar.cli.main' not in sys.modules; "
            "from simple_ar.cli.main import main; "
            "import simple_ar.cli.parser; "
            "assert callable(main)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SEC,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_module_entrypoint_does_not_emit_duplicate_import_warning(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "simple_ar.cli.main", "--help"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SEC,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("RuntimeWarning", completed.stderr)

    def test_canonical_session_import_does_not_load_frozen_pipeline(self) -> None:
        probe = (
            "import sys; "
            "import simple_ar.app.research_application; "
            "import simple_ar.report.writing; "
            "assert not any(name.startswith('simple_ar.pipeline_stages') "
            "for name in sys.modules), sorted(name for name in sys.modules "
            "if name.startswith('simple_ar.pipeline_stages'))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SEC,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_canonical_research_modules_do_not_load_compatibility_layers(self) -> None:
        probe = (
            "import sys; "
            "import simple_ar.app.research_application; "
            "import simple_ar.research.sources.capability; "
            "import simple_ar.research.evidence.reader; "
            "import simple_ar.research.synthesis; "
            "forbidden = sorted(name for name in sys.modules "
            "if name.startswith('simple_ar.pipeline_stages') or "
            "name.startswith('simple_ar._legacy')); "
            "assert not forbidden, forbidden"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SEC,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_canonical_application_does_not_load_legacy_report_adapter(self) -> None:
        probe = (
            "import sys; "
            "import simple_ar.app.research_application; "
            "import simple_ar.report.projection; "
            "assert 'simple_ar.app.research_report' not in sys.modules"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SEC,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_embedded_code_task_spec_reads_large_edit_approval_from_toml(self) -> None:
        test_tmp_root = Path(__file__).resolve().parents[1] / ".tmp_tests"
        test_tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_tmp_root) as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            task = root / "task.md"
            task.write_text("# Task\n\nImprove the project.\n", encoding="utf-8")
            config = root / "code_task.toml"
            config.write_text(
                "\n".join(
                    (
                        "[code_task]",
                        f'code_root = "{project.as_posix()}"',
                        f'task_file = "{task.as_posix()}"',
                        "",
                        "[benchmark]",
                        'command = "python -c \'print(1)\'"',
                        "",
                        "[execute]",
                        "use_llm = true",
                        "allow_large_edits = true",
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            spec = code_task_project_spec({"code_task_config": str(config)})

            self.assertTrue(spec.allow_large_edits)
            self.assertEqual(spec.result_schema(), {'required_metrics': []})


if __name__ == "__main__":
    unittest.main()
