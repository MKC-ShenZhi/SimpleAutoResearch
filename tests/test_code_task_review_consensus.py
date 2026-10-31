import unittest
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from simple_ar.code_task.reviewing import build_review_artifact
import simple_ar.code_task.reviewing as reviewing
from simple_ar.code_task import initialize_code_task
from simple_ar.core.artifacts import read_json
from simple_ar.reviewing.schema import ReviewFinding


def finding(cluster: str, summary: str, *, category: str = "logic") -> ReviewFinding:
    return ReviewFinding(
        severity="warning", category=category, summary=summary,
        evidence=["code_task/patch.diff"], source=f"code-task.llm-reviewer.{cluster}",
    )


def review_task(root: Path, main: str, helper: str = 'def present():\n    return 42\n') -> Path:
    source = root / 'source'
    source.mkdir()
    (source / 'main.py').write_text(main)
    (source / 'helper.py').write_text(helper)
    task = root / 'task.md'
    task.write_text('Preserve the public interface and check actual behavior.\n')
    return initialize_code_task(code_root=source, task_file=task, run_dir=root / 'run').run_dir


class ReviewConsensusTests(unittest.TestCase):
    def test_public_reviews_share_artifacts_empty_clusters_and_caps_without_merging_policy(self):
        from simple_ar.code_task.review import review_code_task_changes
        from simple_ar.code_task.generation.review import review_generated_project
        from simple_ar.code_task.runtime.state import code_task_workspace_boundary
        clusters = [{'cluster_id': 'empty', 'files': []}] + [
            {'cluster_id': str(i), 'files': ['main.py']} for i in range(5)]
        for phase, per_cluster, total in (('post_apply', 5, 16), ('post_repair', 5, 16),
                                           ('generated_project', 6, 24)):
            for enabled in (True, False):
                with self.subTest(phase=phase, enabled=enabled), tempfile.TemporaryDirectory() as folder:
                    run = review_task(Path(folder), 'print("observed")\n')
                    meta = run / 'code_task/meta'
                    suffix = '_post_repair' if phase == 'post_repair' else ''
                    requests = []
                    def capture(**options):
                        self.assertTrue((meta / f'review_index{suffix}.json').is_file())
                        self.assertTrue((meta / f'review_clusters{suffix}.json').is_file())
                        requests.append(options)
                        return [finding(options['label'], f"{options['label']}:{i}")
                                for i in range(options['max_findings'])]
                    with patch.object(reviewing, 'build_review_index', wraps=reviewing.build_review_index) as index, \
                         patch.object(reviewing, 'build_review_clusters', return_value=clusters) as cluster_builder, \
                         patch.object(reviewing, 'snippets_for_cluster', side_effect=lambda project, cluster, **opts:
                                      ['observed source'] if cluster['files'] else []) as windows, \
                         patch.object(reviewing, 'run_llm_review', side_effect=capture):
                        if phase == 'generated_project':
                            report = review_generated_project(project_dir=run / 'code_task/workspace',
                                access_boundary=code_task_workspace_boundary(run),
                                code_artifacts={}, result_schema={}, resource_plan={},
                                client=object(), meta_dir=meta, use_llm=enabled)
                        else:
                            result = review_code_task_changes(run, phase=phase, use_llm=enabled)
                            report = read_json(result.report_path)
                    self.assertEqual(index.call_count, 1)
                    self.assertEqual(windows.call_count, 6 if enabled else 0)
                    self.assertEqual(len(requests), 5 if enabled else 0)
                    model_rows = [row for row in report['findings'] if row['source'].startswith('code-task.llm-reviewer.')]
                    self.assertEqual(len(model_rows), total if enabled else 0)
                    self.assertTrue((meta / f'review_index{suffix}.json').is_file())
                    saved = read_json(meta / f'review_clusters{suffix}.json')
                    self.assertEqual(saved['clusters'], clusters)
                    self.assertEqual(saved.get('phase'), None if phase == 'generated_project' else phase)
                    self.assertEqual(report['metadata']['review_cluster_count'], 6)
                    self.assertEqual(cluster_builder.call_args.kwargs.get('max_clusters', 6),
                                     6 if phase == 'generated_project' else 4)
                    for options in requests:
                        self.assertEqual(options['max_findings'], per_cluster)
                        self.assertFalse(options['allow_blocking'])
                        self.assertEqual(options.get('record_injected_usage', False), phase != 'generated_project')
                        self.assertEqual(options['label'], ('greenfield-code-review-' if phase == 'generated_project'
                                         else f'code-task-review-{phase}-') + options['label'].rsplit('-', 1)[1])

    def test_phase_review_shares_one_interface_observation_with_all_clusters(self):
        import simple_ar.code_task.review as review
        for use_llm, broken in ((True, False), (True, True), (False, True)):
            with self.subTest(use_llm=use_llm, broken=broken), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                run = review_task(root, 'import helper\nhelper.' + ('missing' if broken else 'present') + '()\n')
                prompts = []
                def capture(**kwargs):
                    context = kwargs['prompt'].split('Context JSON:\n', 1)[1].split('\n\nEvidence snippets:', 1)[0]
                    prompts.append(json.loads(context))
                    return []
                with patch.object(review, 'find_local_api_mismatches', wraps=review.find_local_api_mismatches) as scan, \
                     patch.object(review, 'project_api_contract', wraps=review.project_api_contract) as contract, \
                     patch.object(reviewing, 'build_review_clusters', return_value=[{'cluster_id': 'first'}, {'cluster_id': 'second'}]), \
                     patch.object(reviewing, 'snippets_for_cluster', return_value=['observed source']), \
                     patch.object(reviewing, 'run_llm_review', side_effect=capture):
                    result = review.review_code_task_changes(run, use_llm=use_llm)
                self.assertEqual(scan.call_count, 1)
                self.assertEqual(contract.call_count, 1 if use_llm and not broken else 0)
                report = read_json(result.report_path)
                self.assertEqual(result.status, 'failed' if broken else 'passed')
                self.assertEqual(any(row['category'] == 'interface_compatibility' for row in report['findings']), broken)
                self.assertEqual(len(prompts), 2 if use_llm and not broken else 0)
                if use_llm and broken:
                    self.assertEqual(report['metadata']['review_mode'], 'deterministic_blocked')
                for context in prompts:
                    self.assertEqual(context['local_api_mismatches'], [])
                    self.assertIn('def present()', context['local_api_contract']['helper.py'])
                if prompts:
                    self.assertEqual(prompts[0]['local_api_mismatches'], prompts[1]['local_api_mismatches'])
                    self.assertEqual(prompts[0]['local_api_contract'], prompts[1]['local_api_contract'])

    def test_requests_for_downstream_validation_do_not_block_that_validation(self):
        rows = [finding(cluster, 'The applied fix has no recorded validation outcome', category='validation')
                for cluster in ('entrypoint', 'data_flow', 'core_logic', 'config_docs')]
        report = build_review_artifact(reviewer='code-task-reviewer', subject='post_apply', findings=rows)
        self.assertEqual(report['summary']['blocking_count'], 0)
        self.assertEqual(report['summary']['warning_count'], 1)

    def test_recorded_failed_validation_remains_a_deterministic_blocker(self):
        from simple_ar.code_task.review import _deterministic_findings
        from simple_ar.core.artifacts import write_json
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = review_task(root, 'print("old")\n')
            write_json(run / 'code_task/meta/validation_report.json', {'status': 'failed'})
            rows = _deterministic_findings(run, {}, [])
            self.assertTrue(any(row.key == 'validation:failed' and row.severity == 'blocking' for row in rows))

    def test_matching_model_concerns_leave_validation_reachable(self):
        from simple_ar.code_task import validate_code_task
        from simple_ar.code_task.review import review_code_task_changes
        from simple_ar.code_task.runtime.state import code_task_paths
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = review_task(root, "import helper\nassert helper.value() == 42\n",
                              "def value():\n    return 42\n")
            rows = [finding(cluster, "New path is unreachable: no caller.")
                    for cluster in ("entrypoint", "data_flow", "metrics")]
            with patch.object(reviewing, 'run_llm_review', return_value=rows):
                review = review_code_task_changes(run)
            self.assertEqual(review.blocking_count, 0)
            self.assertEqual(review.warning_count, 1)
            self.assertEqual(validate_code_task(run).status, "passed")
            report = read_json(review.report_path)
            self.assertFalse(any(row["source"] == "code-task.review-consensus" for row in report["findings"]))
            # Advisory does not mean correct: a real broken API still blocks.
            (code_task_paths(run).workspace_dir / "main.py").write_text("import helper\nhelper.absent()\n")
            with patch.object(reviewing, 'run_llm_review', return_value=rows) as request:
                failed = review_code_task_changes(run)
            self.assertEqual(failed.status, "failed")
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
