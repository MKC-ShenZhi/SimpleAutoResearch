from __future__ import annotations

import sys
import unittest
from pathlib import Path

from simple_ar.app.dev_checks import CHECK_GROUPS, build_unittest_command, parse_args


class DevChecksTests(unittest.TestCase):
    def test_check_groups_are_layered_and_documented(self) -> None:
        self.assertIn("quick", CHECK_GROUPS)
        self.assertIn("core", CHECK_GROUPS)
        self.assertIn("code-task", CHECK_GROUPS)
        self.assertIn("pipeline", CHECK_GROUPS)
        self.assertIn("research", CHECK_GROUPS)
        self.assertIn("intake", CHECK_GROUPS)
        self.assertIn("data", CHECK_GROUPS)
        self.assertIn("all", CHECK_GROUPS)
        for group in CHECK_GROUPS.values():
            self.assertTrue(group.description)
            self.assertTrue(group.targets)
        self.assertIn('tests.test_architecture_boundaries', CHECK_GROUPS['quick'].targets)
        self.assertIn('tests.test_tool_agent_handoff', CHECK_GROUPS['application'].targets)

    def test_named_check_targets_exist(self) -> None:
        tests_dir = Path(__file__).parent
        for name, group in CHECK_GROUPS.items():
            if name == "all":
                continue
            for target in group.targets:
                self.assertTrue(
                    (tests_dir / f"{target.removeprefix('tests.')}.py").is_file(),
                    f"{name} references missing test module {target}",
                )

    def test_builds_unittest_command_for_group(self) -> None:
        command = build_unittest_command("code-task", verbose=True, failfast=True)

        self.assertEqual(command[:3], [sys.executable, "-m", "unittest"])
        self.assertIn("-v", command)
        self.assertIn("-f", command)
        self.assertIn("tests.test_code_task", command)
        self.assertIn("tests.test_code_source_views", command)

    def test_full_group_uses_discover(self) -> None:
        command = build_unittest_command("all")

        self.assertEqual(command[:3], [sys.executable, "-m", "unittest"])
        self.assertEqual(command[3:], ["discover", "-s", "tests", "-t", "."])

    def test_overlapping_groups_run_each_module_once(self) -> None:
        command = build_unittest_command(["llm", "research", "llm"])
        self.assertEqual(command.count("tests.test_llm"), 1)
        self.assertEqual(command.count("tests.test_budget"), 1)
        self.assertEqual(
            build_unittest_command(["quick", "all", "execution"]),
            build_unittest_command("all"),
        )

    def test_default_group_is_optional(self) -> None:
        args = parse_args([])

        self.assertEqual(args.groups, [])
        self.assertFalse(args.list)


if __name__ == "__main__":
    unittest.main()
