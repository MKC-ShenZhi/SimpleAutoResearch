"""Static project consumers must not inspect installed dependency trees."""
from pathlib import Path
import tempfile
import unittest

from simple_ar.code_task.analysis.entrypoints import analyze_entrypoint_debuggability
from simple_ar.code_task.analysis.interfaces import (
    find_local_api_mismatches, find_return_contract_mismatches, project_api_contract,
)
from simple_ar.code_task.analysis.resource_static import analyze_resource_risks
from simple_ar.code_task.execution.failure import _source_signal_files as failure_sources
from simple_ar.code_task.execution.failure_graph import _source_signal_files as graph_sources
from simple_ar.code_task.analysis.index import project_python_files
from simple_ar.code_task.generation.generated_project_repair import _compile_project


class ProjectSourceBoundaryTests(unittest.TestCase):
    def project(self, root):
        def write(relative, text):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        write('environment/domain.py', 'def transform(value):\n    return value\n')
        write('environment/consumer.py', 'from . import domain\ndomain.missing_attribute()\n')
        write('main.py', 'def main():\n    return "missing_attribute"\n')
        write('search.py', 'def run(model, values):\n    for value in values:\n        model.fit(value)\n')
        # An arbitrary name identifies the environment by its standard marker.
        write('previous-attempt/generated-runtime/pyvenv.cfg', 'home = /usr/bin\n')
        write('previous-attempt/generated-runtime/domain.py', 'def transform(value):\n    return value\n')
        write('previous-attempt/generated-runtime/consumer.py',
              'from . import domain\ndomain.missing_attribute()\n')
        write('previous-attempt/generated-runtime/main.py',
              'try:\n    run()\nexcept Exception:\n    print("suppressed")\n')
        write('previous-attempt/generated-runtime/search.py',
              'def run(model, values):\n    for value in values:\n        for other in values:\n            model.fit(other)\n')
        write('previous-attempt/generated-runtime/broken.py', 'def invalid(\n')
        return root

    def test_interfaces_ignore_dependencies_but_keep_real_source_named_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.project(Path(folder))
            contracts = project_api_contract(root)
            self.assertIn('environment/domain.py', contracts)
            self.assertFalse(any('generated-runtime/' in path for path in contracts))
            findings = find_local_api_mismatches(root)
            self.assertEqual({row['caller'] for row in findings}, {'environment/consumer.py'})
            selected = project_api_contract(root, relevant_paths=['environment/domain.py'])
            self.assertEqual(list(selected), ['environment/domain.py'])

    def test_entrypoint_and_resource_checks_do_not_blame_dependencies_or_spend_their_cap(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.project(Path(folder))
            self.assertEqual(analyze_entrypoint_debuggability(root)['findings'], [])
            result = analyze_resource_risks(root)
            self.assertEqual(result['total_fit_call_count'], 1)
            self.assertEqual(result['max_fit_loop_depth'], 1)
            self.assertEqual([row['path'] for row in result['files']], ['search.py'])
            self.assertEqual(analyze_resource_risks(root, max_files=4)['total_fit_call_count'], 1)

    def test_failure_ranking_and_generated_repair_keep_the_same_source_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.project(Path(folder))
            for search in (failure_sources, graph_sources):
                paths = search(root, "AttributeError: object has no attribute 'missing_attribute'")
                self.assertIn('environment/consumer.py', paths)
                self.assertFalse(any('generated-runtime/' in path for path in paths))
            paths = [path.relative_to(root).as_posix() for path in project_python_files(root)]
            self.assertIn('environment/domain.py', paths)
            self.assertFalse(any('generated-runtime/' in path for path in paths))
            self.assertEqual(_compile_project(root), [])

    def test_shared_ast_scan_retains_return_contract_and_open_export_behavior(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'producer.py').write_text('def produce() -> dict:\n    return {}\n')
            (root / 'consumer.py').write_text(
                'from producer import produce\ndef consume(rows: list):\n    return rows\n'
                'def run():\n    result = produce()\n    consume(result)\n')
            (root / 'open.py').write_text('from external import *\n')
            (root / 'uses_open.py').write_text('from open import unseen_export\n')
            self.assertEqual(find_local_api_mismatches(root), [])
            findings = find_return_contract_mismatches(root)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]['caller'], 'consumer.py')
            self.assertEqual(findings[0]['producer'], 'producer.produce')


if __name__ == '__main__':
    unittest.main()
