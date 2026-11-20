from __future__ import annotations

import importlib
import unittest


class PublicApiTests(unittest.TestCase):
    def test_documented_package_exports_resolve(self) -> None:
        for module_name in ("simple_ar.core", "simple_ar.research", "simple_ar.report"):
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
                for name in module.__all__:
                    with self.subTest(name=name):
                        self.assertIsNotNone(getattr(module, name))


if __name__ == "__main__":
    unittest.main()
