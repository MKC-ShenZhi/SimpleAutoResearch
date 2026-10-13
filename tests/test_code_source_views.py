"""Exact source windows and their planning, review and repair consumers."""
import copy
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from simple_ar.code_task import initialize_code_task
from simple_ar.code_task.analysis.context import read_source_snippets
from simple_ar.code_task.analysis.interfaces import (
    render_source_snippets, snippet_api_contract, source_snippet_views,
)
from simple_ar.code_task.analysis.source_context import source_context_for_files
from simple_ar.code_task.execution.repair import _repair_prompt, _repair_source_anchors
import simple_ar.code_task.review as review
from simple_ar.code_task import build_code_task_context_pack
from simple_ar.code_task.analysis.context import (
    LoadedCodeTaskContextPack, _named_definition_offset,
    load_latest_code_task_context_pack, planned_context_paths,
)
from simple_ar.code_task.analysis.index import build_codebase_index
from simple_ar.code_task.analysis.source_context import inferred_source_request, requested_source_context
from simple_ar.code_task.editing.planning import _context_pack_snippets, generate_patch_plan
from simple_ar.code_task.editing.work_plan import generate_code_task_work_plan
from simple_ar.code_task.editing.patching import (
    _selected_context_files, _context_pack_editable_snippets, _context_pack_reference_snippets,
)
from simple_ar.code_task.orchestration.execute import _ensure_context_pack_for_current_batch
from simple_ar.core.artifacts import read_json, read_jsonl, write_json, write_text
from simple_ar.code_task.review_pipeline import build_review_clusters, snippets_for_cluster


def large_source(root, name='helper.py'):
    text = ''.join(f'# preceding line {i:04d}\n' for i in range(350))
    text += 'def changed_behavior(value):\n    return value + 1\n'
    text += ''.join(f'# following line {i:04d}\n' for i in range(350))
    (root / name).write_text(text, encoding='utf-8')
    return text


class SourceViewTests(unittest.TestCase):
    def rows(self, source, spans, role='editable'):
        return [dict(path='src/example.py', access_role=role, text=source[start:end],
                     source_offset=start, source_chars=len(source), start_line=source.count('\n', 0, start) + 1,
                     truncated=start > 0 or end < len(source), has_unread_tail=end < len(source))
                for start, end in spans]

    def test_full_contiguous_overlapping_observations_form_exact_complete_source(self):
        source = 'def first(value):\n    return value + 1\n\ndef second(value):\n    return first(value)\n'
        rows = self.rows(source, [(0, 24), (24, 59), (50, len(source))])
        del rows[-1]['source_chars']  # Old saved follow-up rows can omit this metadata.
        before = copy.deepcopy(rows)
        views = source_snippet_views(rows)
        self.assertEqual(rows, before)
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0]['text'], source)
        self.assertFalse(views[0]['truncated'])
        self.assertFalse(views[0]['has_unread_tail'])
        self.assertEqual(views[0]['end_line'], source.rstrip('\n').count('\n') + 1)
        self.assertEqual(snippet_api_contract(rows)['src/example.py'], ['def first(value)', 'def second(value)'])
        prompt = render_source_snippets(rows)
        self.assertIn('complete source', prompt)
        self.assertNotIn('partial source', prompt)
        self.assertIn(source, prompt)
        self.assertIn(f'chars [0, {len(source)})', prompt)

    def test_unread_gap_is_not_filled_or_called_complete(self):
        rows = self.rows('abcdefghij', [(0, 3), (7, 10)])
        views = source_snippet_views(rows)
        self.assertEqual([row['text'] for row in views], ['abc', 'hij'])
        self.assertTrue(all(row['truncated'] for row in views))
        self.assertIn('chars [7, 10)', render_source_snippets(rows))
        self.assertNotIn('complete source', render_source_snippets(rows))

    def test_duplicate_and_nested_observations_do_not_duplicate_text(self):
        rows = self.rows('abcdefghij', [(5, 10), (0, 7), (1, 4), (0, 7)])
        views = source_snippet_views(rows)
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0]['text'], 'abcdefghij')
        self.assertFalse(views[0]['truncated'])

    def test_conflicting_overlaps_keep_original_observations_not_false_full_version(self):
        rows = self.rows('abcdefghij', [(0, 5), (3, 8), (7, 10)])
        rows[-1]['text'] = 'XYZ'
        before = copy.deepcopy(rows)
        views = source_snippet_views(rows)
        self.assertEqual([row['text'] for row in views], [row['text'] for row in before])
        self.assertTrue(all(row['coverage_conflict'] and row['truncated'] for row in views))
        self.assertIn('conflicting observations', render_source_snippets(rows))
        self.assertEqual(rows, before)

    def test_unknown_length_and_conflicting_totals_do_not_claim_complete(self):
        rows = self.rows('abcdefghij', [(0, 5), (5, 10)])
        for row in rows:
            del row['source_chars']
        self.assertTrue(source_snippet_views(rows)[0]['truncated'])
        rows[0]['source_chars'] = 10
        rows[1]['source_chars'] = 12
        self.assertTrue(all(row['coverage_conflict'] for row in source_snippet_views(rows)))

    def test_roles_never_merge_and_legacy_complete_source_still_works(self):
        rows = self.rows('abcdefghij', [(0, 5)]) + self.rows('abcdefghij', [(5, 10)], 'reference')
        views = source_snippet_views(rows)
        self.assertEqual(len(views), 2)
        self.assertEqual([row['access_role'] for row in views], ['editable', 'reference'])
        self.assertTrue(all(row['truncated'] for row in views))
        legacy = [dict(path='src/example.py', text='def legacy():\n    pass\n')]
        self.assertFalse(source_snippet_views(legacy)[0]['truncated'])

    def test_partial_parsed_contracts_accumulate_without_last_fragment_erasure(self):
        first, second = 'def first():\n    pass\n', 'def second():\n    pass\n'
        rows = [dict(path='src/example.py', text=first, source_offset=0, source_chars=200, truncated=True),
                dict(path='src/example.py', text=second, source_offset=100, source_chars=200, truncated=True),
                dict(path='src/example.py', text='    unfinished(', source_offset=150, source_chars=200, truncated=True)]
        self.assertEqual(snippet_api_contract(rows)['src/example.py'], ['def first()', 'def second()'])

    def test_invalid_coordinates_are_not_used_for_complete_coverage(self):
        row = dict(path='x.py', source_offset='wrong', text='pass', source_chars=4)
        view = source_snippet_views([row])[0]
        self.assertTrue(view['coverage_conflict'])
        self.assertTrue(view['truncated'])
        self.assertIn('unknown range', render_source_snippets([row]))
        del row['source_chars']
        self.assertTrue(source_snippet_views([row])[0]['coverage_conflict'])

    def test_index_fallback_is_exact_bounded_and_keeps_caller_roles(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'workspace'
            workspace.mkdir()
            source = 'def transform(value):\n    return value + 1\n' + '# detail\n' * 60
            (workspace / 'main.py').write_text(source)
            (workspace / 'tests.py').write_text('assert transform(1) == 2\n')
            (workspace / '.env').write_text('PRIVATE=value')
            (root / 'outside.py').write_text('private = True')
            rows = read_source_snippets(workspace,
                ['main.py', 'tests.py', '.env', '../outside.py', str(root / 'outside.py'), 'missing.py'],
                max_chars_per_file=200, editable_files=['main.py'])
            self.assertEqual([row['path'] for row in rows], ['main.py', 'tests.py'])
            self.assertEqual([row['access_role'] for row in rows], ['editable', 'read_only'])
            self.assertEqual(rows[0]['text'], source[:200])
            self.assertEqual(rows[0]['source_chars'], len(source))
            self.assertEqual(rows[0]['source_offset'], 0)
            self.assertTrue(rows[0]['has_unread_tail'])
            self.assertTrue(rows[0]['truncated'])
            self.assertEqual(rows[1]['text'], 'assert transform(1) == 2\n')
            self.assertFalse(rows[1]['truncated'])
            prompt = render_source_snippets(rows)
            self.assertIn('partial source', prompt)
            self.assertIn('read_only; complete source', prompt)
            self.assertNotIn('... [truncated]', rows[0]['text'])


class ReviewSourceContextTests(unittest.TestCase):
    def assert_views(self, blocks, source, budget):
        self.assertTrue(blocks)
        count = 0
        for block in blocks:
            span = re.search(r'chars \[(\d+), (\d+)\)', block)
            self.assertIsNotNone(span, block[:200])
            lo, hi = map(int, span.groups())
            excerpt = block.split('```text\n', 1)[1].rsplit('\n```', 1)[0]
            self.assertEqual(excerpt, source[lo:hi])
            self.assertIn('read_only', block)
            count += len(excerpt)
        self.assertLessEqual(count, budget)

    def test_changed_support_file_precedes_role_clusters_within_same_limits(self):
        rows = [{'path': f'main{i}.py', 'role': 'entrypoint', 'line_count': 1000}
                for i in range(7)]
        rows += [{'path': 'helper.py', 'role': 'support', 'line_count': 30},
                 {'path': 'model.py', 'role': 'model', 'line_count': 100}]
        index = {'files': rows}
        unchanged = build_review_clusters(index, max_clusters=2, max_files_per_cluster=3)
        self.assertNotIn('helper.py', [p for c in unchanged for p in c['files']])
        clusters = build_review_clusters(index, relevant_paths=['helper.py'],
                                         max_clusters=2, max_files_per_cluster=3)
        self.assertEqual(clusters[0]['files'], ['helper.py'])
        self.assertEqual(len(clusters), 2)
        self.assertTrue(all(len(c['files']) <= 3 for c in clusters))
        self.assertEqual(sum(c['files'].count('helper.py') for c in clusters), 1)

    def test_large_changeset_does_not_expand_cluster_or_file_limits(self):
        paths = [f'helper{i}.py' for i in range(9)]
        index = {'files': [{'path': p, 'role': 'support', 'line_count': 10} for p in paths]}
        clusters = build_review_clusters(index, relevant_paths=[*paths, paths[0], 'missing.py'],
                                         max_clusters=2, max_files_per_cluster=3)
        self.assertEqual([p for c in clusters for p in c['files']], paths[:6])
        self.assertEqual(build_review_clusters(index, relevant_paths=paths, max_clusters=0), [])

    def test_local_review_follows_imports_not_unrelated_data_or_role_names(self):
        from simple_ar.code_task.review_pipeline import build_review_index
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'pkg').mkdir()
            (root / 'pkg/helper.py').write_text('from .dependency import value\nVALUE = value\n')
            (root / 'pkg/dependency.py').write_text('value = 2\n')
            (root / 'pkg/consumer.py').write_text('def use():\n    from .helper import VALUE\n    return VALUE\n')
            (root / 'pkg/unrelated_model.py').write_text('value = 99\n')
            (root / 'large_data.txt').write_text('Unrelated measurements\n' * 100)
            (root / 'README.md').write_text('Unrelated project overview\n')
            index = build_review_index(root)
            clusters = build_review_clusters(index, relevant_paths=['pkg/helper.py'])
            selected = {path for cluster in clusters for path in cluster['files']}
            self.assertEqual(selected, {'pkg/helper.py', 'pkg/dependency.py', 'pkg/consumer.py'})
            self.assertEqual(index['file_count'], 6)
            broad = build_review_clusters(index)
            self.assertIn('large_data.txt', {path for cluster in broad for path in cluster['files']})
            non_python = build_review_clusters(index, relevant_paths=['README.md'])
            self.assertIn('large_data.txt', {path for cluster in non_python for path in cluster['files']})

    def test_review_windows_share_limits_and_exact_source(self):
        cases = [
            ('+++ b/helper.py\n@@ -351,2 +351,2 @@\n', 700, None),
            ('+++ b/helper.py\n@@ -3,1 +3,1 @@\n@@ -351,2 +351,2 @@\n', 1000, 2),
            ('', 80, None),
        ]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = large_source(root)
            for diff, budget, count in cases:
                with self.subTest(diff=diff, budget=budget):
                    blocks = snippets_for_cluster(root, {'files': ['helper.py']},
                        chars_per_file=budget, patch_diff=diff)
                    self.assert_views(blocks, source, budget)
                    self.assertIn('partial source', blocks[0])
                    if diff:
                        self.assertIn('def changed_behavior', '\n'.join(blocks))
                    else:
                        self.assertIn(source[-40:], '\n'.join(blocks))
                        self.assertNotIn('middle omitted', '\n'.join(blocks))
                    if count is not None:
                        self.assertEqual(len(blocks), count)

    def test_small_file_is_complete_and_no_scope_escape_is_read(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'workspace'
            workspace.mkdir()
            source = 'def main():\n    return 42\n'
            (workspace / 'main.py').write_text(source)
            (workspace / '.env').write_text('SECRET=value')
            (root / 'outside.py').write_text('private = True')
            cluster = {'files': ['main.py', '.env', '../outside.py', str(root / 'outside.py')]}
            blocks = snippets_for_cluster(workspace, cluster, chars_per_file=100,
                                          patch_diff='+++ b/main.py\n@@ -1,1 +1,1 @@\n')
            self.assertEqual(len(blocks), 1)
            self.assert_views(blocks, source, 100)
            self.assertIn('complete source', blocks[0])

    def test_normal_review_receives_changed_source_not_only_patch_diff(self):
        from simple_ar.core.artifacts import read_json, write_json
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source_root = root / 'source'
            source_root.mkdir()
            source = large_source(source_root)
            (source_root / 'main.py').write_text('print("entrypoint")\n')
            task = root / 'task.md'
            task.write_text('Review the actual changed behavior.')
            run = initialize_code_task(code_root=source_root, task_file=task, run_dir=root / 'run').run_dir
            manifest = read_json(run / 'manifest.json')
            manifest['patch'] = {'status': 'applied', 'changed_files': ['helper.py']}
            write_json(run / 'manifest.json', manifest)
            (run / 'code_task/patch.diff').write_text('+++ b/helper.py\n@@ -351,2 +351,2 @@\n')
            prompts = []
            def capture(**kwargs):
                prompts.append(kwargs['prompt'])
                return []
            with patch('simple_ar.code_task.reviewing.run_llm_review', side_effect=capture):
                result = review.review_code_task_changes(run, use_llm=True, max_source_chars_per_file=700)
            self.assertTrue(prompts)
            context = json.loads(prompts[0].split('Context JSON:\n', 1)[1].split('\n\nEvidence snippets:', 1)[0])
            self.assertIn('helper.py', context['review_cluster']['files'])
            evidence = prompts[0].split('\n\nEvidence snippets:', 1)[1]
            self.assertIn('def changed_behavior', evidence)
            self.assertIn('partial source', evidence)
            report = read_json(result.report_path)
            self.assertEqual(report['metadata']['changed_files_outside_review_clusters'], [])
            self.assertEqual(source, (run / 'code_task/workspace/helper.py').read_text())


class RepairSourceContextTests(unittest.TestCase):
    def assert_exact(self, rows, source, budget):
        self.assertTrue(rows)
        self.assertLessEqual(sum(len(row['text']) for row in rows), budget)
        for row in rows:
            lo = row['source_offset']
            self.assertEqual(row['text'], source[lo:lo + len(row['text'])])
            self.assertEqual(row['source_chars'], len(source))
            self.assertNotIn('[truncated]', row['text'])

    def test_current_diff_reads_late_changed_code_not_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = large_source(root, 'large.py')
            rows = source_context_for_files(root, ['large.py'], max_chars_per_file=1000,
                anchors=_repair_source_anchors('', '--- a/large.py\n+++ b/large.py\n@@ -351,2 +351,2 @@\n', {}))
            self.assert_exact(rows, source, 1000)
            self.assertIn('def changed_behavior', rows[0]['text'])
            self.assertGreater(rows[0]['source_offset'], 0)
            self.assertTrue(rows[0]['truncated'])

    def test_traceback_takes_priority_over_earlier_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = large_source(root, 'module.ts')
            rows = source_context_for_files(root, ['module.ts'], max_chars_per_file=1800,
                anchors=_repair_source_anchors('File "C:\\project\\module.ts", line 351, in target',
                    '+++ b/module.ts\n@@ -2,1 +2,1 @@\n', {}))
            self.assert_exact(rows, source, 1800)
            self.assertIn('def changed_behavior', rows[0]['text'])
            self.assertEqual(len(rows), 2)

    def test_overlapping_anchors_share_budget_and_do_not_repeat_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = large_source(root, 'large.py')
            rows = source_context_for_files(root, ['large.py'], max_chars_per_file=2000,
                anchors=_repair_source_anchors('File "large.py", line 351, in changed_behavior',
                    '+++ b/large.py\n@@ -352,1 +352,1 @@\n', {}))
            self.assert_exact(rows, source, 2000)
            self.assertEqual(''.join(row['text'] for row in rows).count('def changed_behavior'), 1)

    def test_static_validation_line_and_stale_anchor_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = large_source(root, 'large.py')
            rows = source_context_for_files(root, ['large.py'], max_chars_per_file=700,
                anchors=_repair_source_anchors('', '', {'issues': [{'path': 'large.py', 'line': 351}]}))
            self.assert_exact(rows, source, 700)
            self.assertIn('def changed_behavior', rows[0]['text'])
            rows = source_context_for_files(root, ['large.py'], max_chars_per_file=700,
                anchors=_repair_source_anchors('File "large.py", line 999999, in missing', '', {}))
            self.assert_exact(rows, source, 700)
            self.assertEqual(rows[0]['source_offset'], 0)

    def test_small_file_remains_complete_and_protected_source_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = 'assert result == expected\n'
            (root / 'test_contract.py').write_text(source, encoding='utf-8')
            rows = source_context_for_files(root, ['test_contract.py'], max_chars_per_file=500,
                anchors=_repair_source_anchors('File "test_contract.py", line 1, in check', '', {}), editable_files=[])
            self.assert_exact(rows, source, 500)
            self.assertFalse(rows[0]['truncated'])
            self.assertEqual(rows[0]['access_role'], 'read_only')
            prompt = _repair_prompt(task_text='repair production', patch_plan='initial diagnosis',
                patch_diff='', memory_context='', failure_analysis='failed', execution_report={},
                validation_report={}, snippets=rows, read_only_context=['test_contract.py'])
            self.assertIn('read_only; complete source', prompt)
            self.assertIn('assert result == expected', prompt)
            self.assertIn('marked editable', prompt)
            self.assertIn('disproven plan', prompt)
            from simple_ar.code_task.editing.planning import patch_plan_context
            for task in ('Full request with final constraint: preserve input units.',
                         '完整要求\n\n## Run Context\n用户自己的标题不能被截断。'):
                plan = f'# Patch Plan\n\n## Task\n\n{task}\n\n## Run Context\n\ncommand\n\n## Risks\nkeep contract'
                compact = patch_plan_context(plan, task)
                self.assertNotIn(task, compact)
                self.assertIn('keep contract', compact)
                self.assertEqual(patch_plan_context(plan, 'Different task'), plan)
                self.assertEqual(patch_plan_context(plan, ''), plan)
                revised = _repair_prompt(task_text=task, patch_plan=plan, patch_diff='',
                    memory_context='', failure_analysis='failed', execution_report={},
                    validation_report={}, snippets=rows, read_only_context=['test_contract.py'])
                self.assertEqual(revised.count(task), 1)
                self.assertIn('assert result == expected', revised)

    def test_outside_workspace_and_secrets_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'
            workspace.mkdir()
            (root / 'outside.py').write_text('secret = True', encoding='utf-8')
            (workspace / '.env').write_text('KEY=secret', encoding='utf-8')
            rows = source_context_for_files(workspace, ['../outside.py', '.env'], max_chars_per_file=500)
            self.assertEqual(rows, [])


class CodeContextTargetTests(unittest.TestCase):
    def test_loaded_context_owns_order_limits_and_manifest_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root.parent / 'outside-context.txt'
            pack = LoadedCodeTaskContextPack(run_dir=root, context_pack_path=root / 'pack.json',
                prompt_context_path=root / 'prompt.md', snippets_path=outside,
                context_pack={'budget': {'used_chars': 7}}, snippets=(), selected_files=('a.py', 'a.py', 'b.py'))
            self.assertEqual(pack.selected_paths(max_files=2), ['a.py', 'b.py'])
            self.assertEqual(pack.selected_paths(max_files=0), ['a.py'])
            expected = {'path': 'pack.json', 'prompt_context': 'prompt.md', 'snippets': str(outside),
                'selected_files': ['a.py', 'a.py', 'b.py'], 'budget': {'used_chars': 7}}
            self.assertEqual(pack.manifest_reference(root), expected)
            pack.context_pack['budget'] = None
            self.assertEqual(pack.manifest_reference(root)['budget'], {})
            self.assertEqual(pack.selected_files, ('a.py', 'a.py', 'b.py'))

    def test_target_section_not_issue_environment_or_validation_paths(self):
        plan = ('## Task\r\n`noise.py`\r\n## Files To Modify\r\n'
                '- `target.py` (bug_fix): nested conversion\r\n- `unknown.py`\r\n'
                '- `../outside.py`\r\n## Validation\r\n`tests/test_target.py`\r\n')
        self.assertEqual(planned_context_paths(plan, {'noise.py', 'target.py', 'tests/test_target.py'}), ['target.py'])
        self.assertEqual(planned_context_paths('## Manual plan\n`target.py`', {'target.py'}), [])

    def setup_project(self, root):
        project = root / 'project'
        project.mkdir()
        write_text(project / 'noise.py', 'def json_numpy_handler():\n    return "old"\n')
        write_text(project / 'target.py', 'def convert_nested(data):\n    return data\n')
        write_text(project / 'tests' / 'test_target.py', 'def test_conversion():\n    pass\n')
        write_text(project / 'config' / 'active.toml', 'seed = 0\n')
        write_text(root / 'task.md', 'Fix json numpy handler in noise.py.')
        run = root / 'run'
        initialize_code_task(run_dir=run, code_root=project, task_file=root / 'task.md',
            benchmark_command='python -m unittest discover -s tests',
            edit_scope_allowed_patterns=('*.py',),
            edit_scope_protected_patterns=('tests/**', 'config/**'))
        return run

    def test_saved_target_precedes_noise_while_required_read_only_config_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.setup_project(Path(tmp))
            write_text(run / 'code_task' / 'patch_plan.md', '## Files To Modify\n- `target.py`: conversion\n')
            manifest = read_json(run / 'manifest.json')
            manifest['context_requirements'] = {'read_only_paths': ['config/active.toml']}
            write_json(run / 'manifest.json', manifest)
            pack = build_code_task_context_pack(run, max_files=2, max_source_chars_per_file=100, max_total_chars=200)
            self.assertEqual(pack.selected_files, ('target.py', 'config/active.toml'))
            rows = read_jsonl(pack.snippets_path)
            self.assertEqual(rows[0]['access_role'], 'editable')
            self.assertEqual(rows[1]['access_role'], 'read_only_evidence')
            self.assertLessEqual(read_json(pack.context_pack_path)['budget']['used_chars'], 200)

    def test_plan_cannot_promote_protected_file_to_editable(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.setup_project(Path(tmp))
            pack = build_code_task_context_pack(run, max_files=3, preferred_paths=('tests/test_target.py', 'target.py'))
            row = next(row for row in read_jsonl(pack.snippets_path) if row['path'] == 'tests/test_target.py')
            self.assertNotEqual(row['access_role'], 'editable')
            self.assertIn('target.py', pack.selected_files)

    def test_cached_pack_without_new_plan_target_rebuilds_once_then_reuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.setup_project(Path(tmp))
            old = build_code_task_context_pack(run, max_files=1)
            self.assertNotIn('target.py', old.selected_files)
            write_text(run / 'code_task' / 'patch_plan.md', '## Files To Modify\n- `target.py`: fix\n')
            _ensure_context_pack_for_current_batch(run, max_files=1, max_source_chars_per_file=200, message_callback=None)
            manifest = read_json(run / 'manifest.json')
            self.assertEqual(manifest['context_pack']['selected_files'], ['target.py'])
            self.assertNotEqual(manifest['context_pack']['latest'], str(old.context_pack_path.relative_to(run)))
            with patch('simple_ar.code_task.orchestration.execute.build_code_task_context_pack') as build:
                _ensure_context_pack_for_current_batch(run, max_files=1, max_source_chars_per_file=200, message_callback=None)
                build.assert_not_called()

    def test_fallback_editor_reads_current_target_before_old_planning_inputs(self):
        selected = _selected_context_files({'plan': {'selected_files': ['noise.py']}},
            {'files': [{'path': 'noise.py'}, {'path': 'target.py'}]}, task_text='noise handler',
            patch_plan='## Files To Modify\n- `target.py`: fix\n## Validation\n`noise.py`', max_files=1)
        self.assertEqual(selected, ['target.py'])


class SymbolWindowTests(unittest.TestCase):
    def test_named_read_only_collaborator_reaches_editor_without_edit_authority(self):
        from simple_ar.code_task import propose_patch_edits, record_plan_decision
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = 'def compute(value):\n    return value + 1\n'
            dependency = '# unrelated\n' * 100 + 'class MeasurementAdapter:\n    scale = 2\n'
            write_text(root / 'project/engine.py', source)
            write_text(root / 'project/support.py', dependency)
            write_text(root / 'task.md', 'Correct compute in engine.py; keep support.py read-only.')
            run = root / 'run'
            initialize_code_task(run_dir=run, code_root=root / 'project', task_file=root / 'task.md',
                                 edit_scope_allowed_patterns=('engine.py',))
            generate_patch_plan(run, use_llm=False)
            record_plan_decision(run, decision='approve')
            labels = []
            class Client:
                def ask_json(self, system, prompt, *, label=''):
                    labels.append(label)
                    if label == 'code-task-propose-edits':
                        return {'edits': [], 'summary': 'Need the MeasurementAdapter definition from the collaborator.'}
                    if label != 'code-task-propose-edits-context':
                        raise AssertionError('Resolve the unique owner rather than repeat the prompt')
                    if 'class MeasurementAdapter:' not in prompt or 'scale = 2' not in prompt:
                        raise AssertionError('The actual collaborator definition must be visible')
                    return {'edits': [
                        {'path': 'engine.py', 'old': 'return value + 1', 'new': 'return value * 2', 'reason': 'Use observed contract.'},
                        {'path': 'support.py', 'old': 'scale = 2', 'new': 'scale = 3', 'reason': 'Must remain unauthorized.'}]}
            with patch('simple_ar.code_task.editing.patching.LLMClient.for_task', return_value=Client()):
                result = propose_patch_edits(run, use_llm=True, max_files=1, max_source_chars_per_file=160)
            self.assertEqual(labels, ['code-task-propose-edits', 'code-task-propose-edits-context'])
            proposal = read_json(result.proposal_path)
            self.assertEqual([row['path'] for row in proposal['edits']], ['engine.py'])
            evidence = read_json(run / 'code_task/meta/edit_context_followup.json')
            self.assertEqual(evidence['request']['files'], ['support.py'])
            self.assertEqual(evidence['snippets'][0]['access_role'], 'read_only')
            self.assertLessEqual(len(evidence['snippets'][0]['text']), 160)
            self.assertEqual((run / 'code_task/workspace/support.py').read_text(), dependency)
            self.assertEqual((root / 'project/engine.py').read_text(), source)

    def test_ambiguous_collaborator_is_not_guessed_from_global_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('left.py', 'right.py'):
                write_text(root / name, 'class SharedAdapter:\n    pass\n')
            index = build_codebase_index(root)
            proposal = {'summary': 'SharedAdapter implementation has not been supplied.'}
            missing = inferred_source_request([], [], 2, index=index, proposal=proposal)
            self.assertEqual(missing['files'], [])
            visible = [{'path': 'right.py', 'text': 'class SharedAdapter:', 'source_offset': 0}]
            selected = inferred_source_request(visible, [], 2, index=index, proposal=proposal)
            self.assertEqual(selected['files'], ['right.py'])
            self.assertEqual(selected['symbols'], ['SharedAdapter'])

    def test_unseen_named_collaborator_precedes_more_padding_in_visible_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_text(root / 'engine.py', 'class Engine:\n    pass\n' + '# padding\n' * 100)
            write_text(root / 'support.py', 'class Adapter:\n    pass\n')
            snippet = {'path': 'engine.py', 'text': 'class Engine:\n    pass\n',
                       'source_offset': 0, 'source_chars': 1023, 'truncated': True}
            request = inferred_source_request([snippet], ['engine.py'], 1, index=build_codebase_index(root),
                proposal={'summary': 'Engine is visible, but Adapter is needed to understand its contract.'})
            self.assertEqual(request['files'], ['support.py'])
            self.assertEqual(request['symbols'], ['Adapter'])

    def test_read_only_lookup_respects_shared_character_budget_and_actual_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = '# prefix\n' * 30 + 'class ExternalStage:\n    def run(self):\n        return 1\n'
            write_text(root / 'support.py', text)
            index = build_codebase_index(root)
            request = inferred_source_request([], [], 1, index=index,
                proposal={'summary': 'ExternalStage body needed; UnknownStage is not indexed.'})
            self.assertEqual(request['symbols'], ['ExternalStage'])
            self.assertEqual(requested_source_context(root, index, request, supplied=[],
                max_files=1, max_chars=200, max_total_chars=0), [])
            rows = requested_source_context(root, index, request, supplied=[],
                max_files=1, max_chars=200, max_total_chars=40)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['text'], text[rows[0]['source_offset']:][:40])
            self.assertEqual(rows[0]['access_role'], 'read_only')
            write_text(root / 'support.py', '# stale index, no definition\n')
            self.assertEqual(requested_source_context(root, index, request, supplied=[],
                max_files=1, max_chars=200), [])

    def test_unseen_subtraction_handles_overlapping_observations_and_short_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = '# source\n' * 100
            write_text(root / 'source.py', text)
            seen = [(0, 120), (90, 180), (200, len(text))]
            supplied = [{'path': 'source.py', 'text': text[lo:hi], 'source_offset': lo,
                         'source_chars': len(text), 'truncated': True} for lo, hi in seen]
            rows = requested_source_context(root, build_codebase_index(root), {'files': ['source.py']},
                supplied=supplied, max_files=1, max_chars=500, max_total_chars=500)
            self.assertEqual([(row['source_offset'], len(row['text'])) for row in rows], [(180, 20)])
            self.assertEqual(rows[0]['text'], text[180:200])

    def test_proposal_reads_named_gap_without_repeating_the_unchanged_prompt(self):
        from simple_ar.code_task import propose_patch_edits, record_plan_decision
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = '# unrelated context\n' * 100 + (
                'class RuntimeStage:\n    def run(self, value):\n        return value + 1\n')
            write_text(root / 'project/engine.py', source)
            write_text(root / 'task.md', 'Correct the computation in engine.py.')
            run = root / 'run'
            initialize_code_task(run_dir=run, code_root=root / 'project', task_file=root / 'task.md',
                                 edit_scope_allowed_patterns=('engine.py',))
            generate_patch_plan(run, use_llm=False)
            record_plan_decision(run, decision='approve')
            labels = []
            class Client:
                def ask_json(self, system, prompt, *, label=''):
                    labels.append(label)
                    if label == 'code-task-propose-edits':
                        return {'edits': [], 'summary': 'The RuntimeStage body is outside the visible prefix.'}
                    if label != 'code-task-propose-edits-context':
                        raise AssertionError('Do not repeat the same missing-source prompt')
                    if 'class RuntimeStage:' not in prompt or 'return value + 1' not in prompt:
                        raise AssertionError('The actual definition body must reach the editor')
                    return {'edits': [{'path': 'engine.py', 'old': 'return value + 1',
                                      'new': 'return value * 2', 'reason': 'Use the observed body.'}]}
            with patch('simple_ar.code_task.editing.patching.LLMClient.for_task', return_value=Client()):
                result = propose_patch_edits(run, use_llm=True, max_files=1, max_source_chars_per_file=160)
            self.assertEqual(result.edit_count, 1)
            self.assertEqual(labels, ['code-task-propose-edits', 'code-task-propose-edits-context'])
            followup = read_json(run / 'code_task/meta/edit_context_followup.json')
            self.assertEqual(followup['request']['symbols'], ['RuntimeStage'])
            self.assertEqual((run / 'code_task/workspace/engine.py').read_text(), source)

    def test_requested_definitions_take_precedence_over_earlier_mentions_and_adjacent_padding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = 'class FirstStage:\n    def run(self):\n        return 1\n'
            second = 'class SecondStage:\n    def run(self):\n        return 2\n'
            text = '# FirstStage and SecondStage are referenced here.\n' + '# unrelated\n' * 120
            text += first + '# unrelated\n' * 150 + second + '# tail\n' * 80
            write_text(root / 'workflow.py', text)
            rows = requested_source_context(root, build_codebase_index(root),
                {'files': ['workflow.py'], 'symbols': ['FirstStage', 'SecondStage'], 'query': ''},
                supplied=[], max_files=2, max_chars=120, max_total_chars=240, max_windows_per_file=2)
            self.assertEqual([row['source_offset'] for row in rows],
                             [text.index('class FirstStage'), text.index('class SecondStage')])
            self.assertTrue(rows[0]['text'].startswith(first))
            self.assertTrue(rows[1]['text'].startswith(second))
            self.assertLessEqual(sum(len(row['text']) for row in rows), 240)

    def test_fallback_only_uses_indexed_names_in_partial_allowed_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = '# padding\n' * 80 + 'class ReadyStage:\n    pass\n'
            write_text(root / 'src/engine.py', source)
            write_text(root / 'protected.py', 'class SecretStage:\n    pass\n')
            index = build_codebase_index(root)
            snippets = [{'path': 'src/engine.py', 'text': '# padding\n', 'source_chars': len(source),
                         'source_offset': 0, 'truncated': True},
                        {'path': 'protected.py', 'text': 'class SecretStage:', 'source_chars': 30,
                         'source_offset': 0, 'truncated': True}]
            request = inferred_source_request(snippets, ['src/engine.py'], 1, index=index,
                proposal={'summary': 'ReadyStage, UnknownStage and SecretStage bodies were not supplied.'})
            self.assertEqual(request['files'], ['src/engine.py'])
            self.assertEqual(request['symbols'], ['ReadyStage'])
            self.assertIn('proposal explanation', request['reason'])
            ordinary = inferred_source_request(snippets, ['src/engine.py'], 1, index=index,
                proposal={'summary': 'No precise name available.', 'validation': None})
            self.assertEqual(ordinary['symbols'], [])
            self.assertEqual(ordinary['files'], ['src/engine.py'])

    def test_unseen_window_does_not_repeat_the_previously_supplied_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = '# prefix\n' * 70 + '# observed tail\n' * 30
            write_text(root / 'notes.py', text)
            boundary = text.index('# observed tail')
            old = {'path': 'notes.py', 'text': text[boundary:], 'source_offset': boundary,
                   'source_chars': len(text), 'truncated': True}
            rows = requested_source_context(root, build_codebase_index(root),
                {'files': ['notes.py'], 'symbols': [], 'query': ''}, supplied=[old],
                max_files=1, max_chars=800, max_total_chars=800)
            self.assertEqual(len(rows), 1)
            self.assertLessEqual(rows[0]['source_offset'] + len(rows[0]['text']), boundary)
            self.assertNotIn('# observed tail', rows[0]['text'])
            self.assertEqual(rows[0]['text'], text[rows[0]['source_offset']:boundary])

    def test_fresh_staged_planning_reads_named_body_and_retains_context_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = '# unrelated prefix\n' * 300 + 'def calibrate_observation(x):\n    return x * 2\n'
            write_text(root / 'project/science.py', text)
            write_text(root / 'task.md', 'Fix calibrate_observation in science.py.')
            run = root / 'run'
            initialize_code_task(run_dir=run, code_root=root / 'project', task_file=root / 'task.md')
            captured = []
            def answer(client, **kwargs):
                captured.extend(kwargs['snippets'])
                return {'summary': 'Use observed source.', 'items': [{'id': 'W1',
                    'objective': 'Fix the observed definition.', 'target_files': ['science.py']}]}
            with patch('simple_ar.code_task.editing.work_plan.LLMClient.for_task', return_value=object()), \
                    patch('simple_ar.code_task.editing.work_plan._ask_llm_for_work_plan', side_effect=answer):
                generate_code_task_work_plan(run, llm_client=object(), max_files=1, max_source_chars_per_file=200)
            self.assertTrue(captured[0]['text'].startswith('def calibrate_observation'))
            self.assertGreater(captured[0]['source_offset'], 0)
            self.assertEqual(captured[0]['text'], text[captured[0]['source_offset']:][:200])
            manifest = read_json(run / 'manifest.json')
            loaded = load_latest_code_task_context_pack(run)
            self.assertIsNotNone(loaded)
            self.assertEqual(manifest['work_plan']['context_pack']['path'], manifest['context_pack']['latest'])
            self.assertLessEqual(loaded.context_pack['budget']['used_chars'], 200)
            self.assertEqual((root / 'project/science.py').read_text(), text)

    def test_staged_planning_reuses_saved_coordinates_without_rebuilding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = '# prefix\n' * 100 + 'def calibrate_observation(x):\n' + '    x += 1\n' * 40 + '    return x\n'
            write_text(root / 'project/science.py', text)
            write_text(root / 'task.md', 'Fix calibrate_observation in science.py.')
            run = root / 'run'
            initialize_code_task(run_dir=run, code_root=root / 'project', task_file=root / 'task.md')
            pack = build_code_task_context_pack(run, max_files=1, max_source_chars_per_file=500, max_total_chars=500)
            before = pack.context_pack_path.read_bytes()
            captured = []
            def answer(client, **kwargs):
                captured.extend(kwargs['snippets'])
                return {'summary': 'Use saved source.', 'items': [{'id': 'W1',
                    'objective': 'Fix observed source.', 'target_files': ['science.py']}]}
            with patch('simple_ar.code_task.analysis.context.build_code_task_context_pack') as build, \
                    patch('simple_ar.code_task.editing.work_plan.LLMClient.for_task', return_value=object()), \
                    patch('simple_ar.code_task.editing.work_plan._ask_llm_for_work_plan', side_effect=answer):
                generate_code_task_work_plan(run, llm_client=object(), max_files=1, max_source_chars_per_file=200)
                build.assert_not_called()
            self.assertGreater(captured[0]['source_offset'], 0)
            self.assertEqual(captured[0]['text'], text[captured[0]['source_offset']:][:200])
            self.assertNotIn('[truncated]', captured[0]['text'])
            self.assertEqual(pack.context_pack_path.read_bytes(), before)

    def test_fresh_model_planning_uses_bounded_definition_and_preserves_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'project'
            text = '# unrelated prefix\n' * 300 + 'def calibrate_observation(x):\n    return x * 2\n'
            write_text(project / 'science.py', text)
            write_text(root / 'task.md', 'Fix calibrate_observation in science.py.')
            run = root / 'run'
            initialize_code_task(run_dir=run, code_root=project, task_file=root / 'task.md')
            captured = []
            def answer(client, **kwargs):
                captured.extend(kwargs['snippets'])
                return {'summary': 'Use the observed definition.', 'files_to_modify': []}
            with patch('simple_ar.code_task.editing.planning.LLMClient.for_task', return_value=object()), \
                    patch('simple_ar.code_task.editing.planning._ask_llm_for_plan', side_effect=answer):
                generate_patch_plan(run, llm_client=object(), max_files=1, max_source_chars_per_file=180)
            self.assertEqual(len(captured), 1)
            self.assertTrue(captured[0]['text'].startswith('def calibrate_observation'))
            self.assertGreater(captured[0]['source_offset'], 0)
            manifest = read_json(run / 'manifest.json')
            loaded = load_latest_code_task_context_pack(run)
            self.assertIsNotNone(loaded)
            self.assertEqual(manifest['plan']['context_pack']['path'], manifest['context_pack']['latest'])
            self.assertLessEqual(loaded.context_pack['budget']['used_chars'], 180)
            before = loaded.context_pack_path.read_bytes()
            with patch('simple_ar.code_task.analysis.context.build_code_task_context_pack') as build, \
                    patch('simple_ar.code_task.editing.planning.LLMClient.for_task', return_value=object()), \
                    patch('simple_ar.code_task.editing.planning._ask_llm_for_plan', side_effect=answer):
                generate_patch_plan(run, force=True, llm_client=object(), max_files=1, max_source_chars_per_file=180)
                build.assert_not_called()
            self.assertEqual(loaded.context_pack_path.read_bytes(), before)

    def test_offline_planning_keeps_existing_no_retrieval_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_text(root / 'project' / 'science.py', 'VALUE = 1\n')
            write_text(root / 'task.md', 'Inspect science.py.')
            run = root / 'run'
            initialize_code_task(run_dir=run, code_root=root / 'project', task_file=root / 'task.md')
            with patch('simple_ar.code_task.analysis.context.build_code_task_context_pack') as build:
                generate_patch_plan(run, use_llm=False)
                generate_code_task_work_plan(run, use_llm=False)
                build.assert_not_called()
            self.assertIsNone(load_latest_code_task_context_pack(run))

    def test_large_file_starts_at_exact_named_definition_not_its_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'project'
            text = '# unrelated introduction\n' * 500 + (
                'def transform_measurement(value):\n    """Scientific definition.\n' + '    protocol explanation\n' * 8
                + '    """\n    scale = 2\n    return value * scale\n') + '# unrelated tail\n' * 100
            write_text(project / 'science.py', text)
            write_text(root / 'task.md', 'Fix science.transform_measurement() for invalid input.')
            run = root / 'run'
            initialize_code_task(run_dir=run, code_root=project, task_file=root / 'task.md')
            pack = build_code_task_context_pack(run, max_files=1, max_source_chars_per_file=500, max_total_chars=500)
            row = read_jsonl(pack.snippets_path)[0]
            self.assertTrue(row['text'].startswith('def transform_measurement'))
            self.assertIn('return value * scale', row['text'])
            self.assertEqual(row['source_offset'], text.index('def transform_measurement'))
            self.assertEqual(row['text'], text[row['source_offset']:row['source_offset'] + row['chars']])
            self.assertLessEqual(row['chars'], 500)
            self.assertEqual(row['start_line'], 501)
            self.assertTrue(row['has_unread_tail'])
            self.assertEqual((project / 'science.py').read_text(), text)

    def test_projection_and_symbol_continuation_keep_nonzero_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'project'
            text = '# preface\n' * 150 + 'def process_measurement(value):\n' + ('    value += 1\n' * 70) + '    return value\n'
            write_text(project / 'science.py', text)
            write_text(root / 'task.md', 'Investigate process_measurement')
            run = root / 'run'
            initialize_code_task(run_dir=run, code_root=project, task_file=root / 'task.md')
            build_code_task_context_pack(run, max_files=1, max_source_chars_per_file=500, max_total_chars=500)
            loaded = load_latest_code_task_context_pack(run)
            for projected in (
                _context_pack_snippets(loaded, max_files=1, max_chars_per_file=220, include_read_only=True),
                _context_pack_editable_snippets(loaded, selected_files=['science.py'], max_chars_per_file=220),
                _context_pack_reference_snippets(loaded, excluded_files=[], max_files=1, max_chars_per_file=220),
            ):
                row = projected[0]
                offset = row['source_offset']
                self.assertGreater(offset, 0)
                self.assertEqual(row['text'], text[offset:offset + 220])
                self.assertTrue(row['truncated'])
                self.assertEqual(row['end_line'], row['start_line'] + row['text'][:-1].count('\n'))
                index = build_codebase_index(project)
                extra = requested_source_context(project, index,
                    {'files': ['science.py'], 'symbols': ['process_measurement'], 'query': ''},
                    supplied=projected, max_files=1, max_chars=250, max_total_chars=250)
                self.assertEqual(extra[0]['source_offset'], offset + len(row['text']))
                self.assertEqual(extra[0]['text'], text[offset + 220:offset + 470])

    def test_ambiguous_methods_substrings_and_stale_anchors_are_not_guessed(self):
        text = ('# preface\n' * 10 + 'class First:\n    def evaluate(self):\n        return 1\n'
            'class Second:\n    def evaluate(self):\n        return 2\n')
        symbols = [
            {'path': 'a.py', 'name': 'evaluate', 'qualified_name': 'First.evaluate', 'line_start': 12},
            {'path': 'a.py', 'name': 'evaluate', 'qualified_name': 'Second.evaluate', 'line_start': 15},
        ]
        for query in ('evaluate', 'reevaluate', 'unknown'):
            self.assertEqual(_named_definition_offset(text, 'a.py', query, symbols), 0)
        second = _named_definition_offset(text, 'a.py', 'Fix Second.evaluate()', symbols)
        self.assertEqual(second, text.index('    def evaluate', text.index('class Second')))
        self.assertEqual(_named_definition_offset(text, 'a.py', 'Second.evaluate',
            [dict(symbols[1], line_start=14)]), 0)
        self.assertEqual(_named_definition_offset(text, 'other.py', 'Second.evaluate', symbols), 0)

    def test_protected_definition_keeps_its_read_only_role(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'project'
            write_text(project / 'entry.py', 'VALUE = 1\n')
            write_text(project / 'evidence.py', '# preface\n' * 50 + 'def evaluate_protocol():\n    return "reference"\n')
            write_text(root / 'task.md', 'Compare evaluate_protocol')
            run = root / 'run'
            initialize_code_task(run_dir=run, code_root=project, task_file=root / 'task.md',
                edit_scope_protected_patterns=('evidence.py',))
            pack = build_code_task_context_pack(run, preferred_paths=('entry.py', 'evidence.py'),
                max_files=2, max_source_chars_per_file=200, max_total_chars=400)
            row = next(row for row in read_jsonl(pack.snippets_path) if row['path'] == 'evidence.py')
            self.assertEqual(row['access_role'], 'read_only_evidence')
            self.assertTrue(row['text'].startswith('def evaluate_protocol'))
            captured = []
            def answer(client, **kwargs):
                captured.extend(kwargs['snippets'])
                return {'summary': 'Respect source and evidence roles.', 'items': [{'id': 'W1',
                    'objective': 'Update the editable source, not the reference.',
                    'target_files': ['entry.py'], 'read_only_evidence': ['evidence.py']}]}
            before = pack.context_pack_path.read_bytes()
            with patch('simple_ar.code_task.analysis.context.build_code_task_context_pack') as build, \
                    patch('simple_ar.code_task.editing.work_plan.LLMClient.for_task', return_value=object()), \
                    patch('simple_ar.code_task.editing.work_plan._ask_llm_for_work_plan', side_effect=answer):
                result = generate_code_task_work_plan(run, llm_client=object(), max_files=2,
                    max_source_chars_per_file=200)
                build.assert_not_called()
            reference = next(row for row in captured if row['path'] == 'evidence.py')
            self.assertEqual(reference['access_role'], 'read_only_evidence')
            self.assertGreater(reference['source_offset'], 0)
            self.assertEqual(pack.context_pack_path.read_bytes(), before)
            plan = read_json(result.work_plan_path)
            self.assertEqual(plan['items'][0]['target_files'], ['entry.py'])
            self.assertEqual(plan['items'][0]['read_only_evidence'], ['evidence.py'])


if __name__ == '__main__':
    unittest.main()
