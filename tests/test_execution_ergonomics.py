"""Unit tests for v2.11 execution ergonomics, GPU sandbox passthrough, and clear stop reasons."""

from __future__ import annotations

from io import StringIO
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
from simple_ar.cli.main import _render_native_result
from simple_ar.core.process_sandbox import ProcessSandbox, sandbox_command


class _FakeWorkspace:
    def __init__(self, root: Path) -> None:
        self.workspace = root / "workspace"


class _FakeStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def exists(self, name: str) -> bool:
        return False


class _FakeSession:
    def __init__(self, root: Path) -> None:
        self.store = _FakeStore(root)


class _FakeLedger:
    limits = {"llm_requests": 30, "total_tokens": 1200000}
    entries: list = []

    def snapshot(self) -> dict:
        return {"limits": self.limits, "settled": {}, "reserved": {}, "unknown": []}


class _FakeApp:
    def __init__(self, root: Path) -> None:
        self.session = _FakeSession(root)
        self.workspace = _FakeWorkspace(root)
        self.state = {"pending": {}, "config": {"allow_execution": True}}
        self.ledger = _FakeLedger()


class ExecutionErgonomicsTests(unittest.TestCase):
    def test_token_allowance_exceeded_stop_reason_is_not_withheld(self) -> None:
        root = Path("/tmp/fake-session")
        app = _FakeApp(root)
        status = {
            "status": "paused",
            "status_reason": "ValueError: Current working view exceeds the remaining token allowance; no request sent",
            "current_attempt": "native-turn-0136",
            "accounting": {},
            "retained_versions": [],
        }
        buf = StringIO()
        console = Console(file=buf, force_terminal=False, width=120)
        with patch("simple_ar.core.console.make_console", return_value=console):
            _render_native_result(app, status)
        rendered = buf.getvalue()
        self.assertIn("Insufficient remaining budget before dispatch", rendered)
        self.assertNotIn("Saved stop detail withheld", rendered)

    def test_sandbox_command_binds_gpu_devices_only_when_allow_gpu_enabled(self) -> None:
        workspace = Path("/tmp/ws")
        sandbox_off = ProcessSandbox(workspace=workspace, writable=workspace, python=Path("/usr/bin/python3"))
        sandbox_on = ProcessSandbox(workspace=workspace, writable=workspace, python=Path("/usr/bin/python3"), allow_gpu=True)
        fake_devs = ["/dev/dri", "/dev/nvidia0", "/dev/nvidiactl", "/dev/nvidia-uvm"]
        with patch("simple_ar.core.process_sandbox.shutil.which", return_value="/usr/bin/bwrap"), \
             patch("simple_ar.core.process_sandbox._gpu_device_paths", return_value=fake_devs):
            cmd_off = sandbox_command(sandbox_off, workspace, ["python3", "-V"])
            cmd_on = sandbox_command(sandbox_on, workspace, ["python3", "-V"])
        self.assertIn("--unshare-all", cmd_off)
        self.assertNotIn("/dev/nvidia0", cmd_off)
        self.assertIn("--unshare-all", cmd_on)
        for dev in fake_devs:
            self.assertIn(dev, cmd_on)
            idx = cmd_on.index(dev)
            self.assertEqual(cmd_on[idx - 1], "--dev-bind-try")

    def test_cli_start_parser_registers_sandbox_gpu_and_auto_approve(self) -> None:
        from simple_ar.cli.main import build_parser
        parser = build_parser()
        parsed = parser.parse_args(["start", "--allow-execution", "--auto-approve-execution", "--sandbox-execution", "--sandbox-gpu", "test goal"])
        self.assertTrue(parsed.auto_approve_execution)
        self.assertTrue(parsed.sandbox_gpu)


if __name__ == "__main__":
    unittest.main()
