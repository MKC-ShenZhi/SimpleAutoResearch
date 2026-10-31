import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from importlib.metadata import PackageNotFoundError

from simple_ar.cli.main import main
from simple_ar.research.preparation import inspect_execution_entry, inspect_project_preparation, project_preparation_markdown
from simple_ar.research.preparation import _dependency_probe


class ProjectPreparationTests(unittest.TestCase):
    def test_asset_public_download_receipt_and_budget_resume_without_requests(self):
        import httpx
        import socket
        import zipfile
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.research.preparation_assets import acquire_asset
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("snapshot/README.md", "Public setup instructions, not commands to execute.")
        payload = archive.getvalue()
        original_client = httpx.Client
        calls = []
        timeouts = []
        def serve(request):
            calls.append(str(request.url))
            timeouts.append(request.extensions["timeout"]["read"])
            if request.url.host == "api.github.com":
                return httpx.Response(302, headers={"location": "https://public.example/snapshot.zip"})
            return httpx.Response(200, stream=httpx.ByteStream(payload))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ledger = BudgetLedger({"download_requests": None}, storage_path=root / "ledger.json")
            with patch("simple_ar.research.preparation_assets.socket.getaddrinfo",
                       return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]), \
                    patch("simple_ar.research.preparation_assets.httpx.Client",
                          side_effect=lambda **kw: original_client(transport=httpx.MockTransport(serve), **kw)) as client:
                receipt = acquire_asset(root=root / "repo", role="project", url="https://github.com/owner/repo", ledger=ledger)
                self.assertEqual(receipt["status"], "completed")
                self.assertEqual(receipt["url"], "https://github.com/owner/repo")
                self.assertTrue((Path(receipt["path"]) / "README.md").is_file())
                self.assertEqual(receipt["provenance"], "default_branch_snapshot_not_fixed_commit")
                self.assertEqual(ledger.entries[0].reserved["download_requests"], 4)
                self.assertEqual(ledger.entries[0].reservation_id, f"asset:{(root / 'repo').absolute()}")
                self.assertEqual(ledger.entries[0].actual, {"download_requests": 2, "download_bytes": len(payload)})
                self.assertIsNone(ledger.limits["download_requests"])
                self.assertFalse(client.call_args.kwargs["trust_env"])
                self.assertFalse(client.call_args.kwargs["follow_redirects"])
                count = len(calls)
                self.assertEqual(acquire_asset(root=root / "repo", role="project", url=receipt["url"],
                    ledger=BudgetLedger.load(root / "ledger.json")), receipt)
                self.assertEqual(len(calls), count)
                data = acquire_asset(root=root / "data", role="data", url="https://public.example/dataset.ZIP", ledger=ledger,
                                     max_download_mb=32)
                self.assertEqual(data["status"], "completed")
                self.assertEqual(data["limits"]["download_bytes"], 32 * 1024 * 1024)
                self.assertEqual(data["limits"]["expanded_bytes"], 128 * 1024 * 1024)
                self.assertEqual(ledger.entries[-1].reserved["download_bytes"], 32 * 1024 * 1024)
                self.assertEqual(Path(data["path"]).name, "download.zip")
                self.assertEqual(Path(data["path"]).read_bytes(), payload)
                self.assertFalse((root / "data" / "project").exists())
                direct = acquire_asset(root=root / "direct", role="project",
                    url="https://public.example/export/version-123", ledger=ledger, max_download_mb=32)
                self.assertEqual(direct["status"], "completed")
                self.assertTrue((Path(direct["path"]) / "README.md").is_file())
                self.assertEqual(timeouts, [30] * len(calls))
                retained_count = len(calls)
                restored = acquire_asset(root=root / "data", role="data", url=data["url"],
                                         ledger=BudgetLedger.load(root / "ledger.json"))
                self.assertEqual(restored, data)  # Saved capacity survives the old default.
                self.assertEqual(len(calls), retained_count)
                for invalid in (0, -1, True, 1.5):
                    with self.subTest(capacity=invalid), self.assertRaises(ValueError):
                        acquire_asset(root=root / "invalid", role="data", url=data["url"],
                                      ledger=ledger, max_download_mb=invalid)
                    self.assertFalse((root / "invalid").exists())
                count = len(calls)
                wrong_role = acquire_asset(root=root / "repo", role="data", url=receipt["url"], ledger=ledger)
                self.assertEqual(wrong_role["reason"], "AssetRootRequestConflict")
                project = Path(receipt["path"])
                project.rename(project.with_name("removed_snapshot"))
                missing = acquire_asset(root=root / "repo", role="project", url=receipt["url"], ledger=ledger)
                self.assertEqual(missing["status"], "failed")
                self.assertEqual(missing["reason"], "completed_asset_path_missing_or_wrong_role")
                self.assertEqual(len(calls), count)

    def test_asset_failed_private_redirect_and_interruption_never_resend(self):
        import httpx
        import socket
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.research.preparation_assets import acquire_asset
        original_client = httpx.Client
        for failure in ("private_redirect", "interrupted", "empty", "http_error"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                ledger = BudgetLedger(storage_path=root / "ledger.json")
                def serve(request):
                    if failure == "interrupted":
                        raise KeyboardInterrupt()
                    if failure == "empty":
                        return httpx.Response(200, stream=httpx.ByteStream(b""))
                    if failure == "http_error":
                        return httpx.Response(403, stream=httpx.ByteStream(b"secret server body"))
                    return httpx.Response(302, headers={"location": "https://127.0.0.1/data.csv"})
                with patch("simple_ar.research.preparation_assets.socket.getaddrinfo",
                           return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]), \
                        patch("simple_ar.research.preparation_assets.httpx.Client",
                              side_effect=lambda **kw: original_client(transport=httpx.MockTransport(serve), **kw)) as client:
                    receipt = acquire_asset(root=root / "asset", role="data", url="https://public.example/data.csv", ledger=ledger)
                    self.assertEqual(receipt["status"], "unknown" if failure == "interrupted" else "failed")
                    self.assertEqual(receipt["reason"], {"private_redirect": "nonpublic_address", "empty": "empty_download",
                        "interrupted": "KeyboardInterrupt", "http_error": "HTTPStatusError"}[failure])
                    if failure == "empty":
                        self.assertEqual(Path(receipt["path"]).stat().st_size, 0)
                    self.assertEqual(ledger.entries[0].status, "unknown")
                    self.assertEqual(receipt["get_requests"], 1)
                    count = client.call_count
                    self.assertEqual(acquire_asset(root=root / "asset", role="data", url=receipt["url"], ledger=ledger), receipt)
                    self.assertEqual(client.call_count, count)
                    saved = json.loads((root / "asset" / "receipt.json").read_text())
                    saved["status"] = "started"
                    (root / "asset" / "receipt.json").write_text(json.dumps(saved))
                    self.assertEqual(acquire_asset(root=root / "asset", role="data", url=receipt["url"], ledger=ledger)["status"], "unknown")
                    self.assertEqual(client.call_count, count)

    def test_asset_zip_safety_and_download_limit_preserve_failed_receipts(self):
        import httpx
        import socket
        import zipfile
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.research.preparation_assets import acquire_asset, LIMITS
        original_client = httpx.Client
        for name in ("../outside", "/absolute", "bad\\path", "symlink", "duplicate", "oversize", "nonzip"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                archive = io.BytesIO()
                with zipfile.ZipFile(archive, "w") as bundle:
                    entry = zipfile.ZipInfo(name)
                    if name == "symlink":
                        entry.create_system, entry.external_attr = 3, 0o120777 << 16
                    bundle.writestr(entry, "asset")
                    if name == "duplicate":
                        bundle.writestr("DUPLICATE", "second")
                payload = b"<html>Not a project archive</html>" if name == "nonzip" else archive.getvalue()
                headers = {"content-length": str(LIMITS["download_bytes"] + 1)} if name == "oversize" else {}
                with patch("simple_ar.research.preparation_assets.socket.getaddrinfo",
                           return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]), \
                        patch("simple_ar.research.preparation_assets.httpx.Client", side_effect=lambda **kw: original_client(
                            transport=httpx.MockTransport(lambda r: httpx.Response(200, headers=headers, stream=httpx.ByteStream(payload))), **kw)):
                    receipt = acquire_asset(root=root / "asset", role="project", url="https://public.example/project.zip",
                                            ledger=BudgetLedger(storage_path=root / "ledger.json"))
                self.assertEqual(receipt["status"], "failed")
                self.assertEqual(receipt["reason"], "download_limit" if name == "oversize" else
                                 "BadZipFile" if name == "nonzip" else "unsafe_zip_entry")
                self.assertTrue((root / "asset" / "receipt.json").is_file())
                self.assertFalse((root / "outside").exists())

    def test_standard_venv_marker_prunes_arbitrary_dependency_directory_not_source_name(self):
        from simple_ar.code_task.analysis.index import build_codebase_index
        from simple_ar.code_task.review_pipeline import build_review_index
        from simple_ar.code_task.analysis.source_context import source_file_inventory
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dependency = root / 'previous-attempt/generated-runtime'
            dependency.mkdir(parents=True)
            (dependency / 'pyvenv.cfg').write_text('home = /usr/bin\n')
            (dependency / 'third_party.py').write_text('raise RuntimeError("not project code")\n')
            source = root / 'environment'
            source.mkdir()
            (source / 'domain.py').write_text('VALUE = 1\n')
            (root / 'run.py').write_text('if __name__ == "__main__":\n    print("METRIC score=1")\n')
            for builder in (build_codebase_index, build_review_index, source_file_inventory):
                options = {'access_boundary': root} if builder is build_review_index else {}
                paths = {row['path'] for row in builder(root, **options)['files']}
                self.assertIn('environment/domain.py', paths)
                self.assertIn('run.py', paths)
                self.assertNotIn('previous-attempt/generated-runtime/third_party.py', paths)
                self.assertNotIn('previous-attempt/generated-runtime/pyvenv.cfg', paths)
            facts = inspect_project_preparation(root)
            self.assertIn('environment/domain.py', facts['source_file_paths'])
            self.assertFalse(any('generated-runtime/' in path for path in facts['source_file_paths']))

    def test_probe_distinguishes_versions_markers_urls_extras_and_unknowns(self):
        def version(name):
            if name == "absent":
                raise PackageNotFoundError(name)
            return "2.0"
        requirements = ["present>=1", "present>=3", "absent", "unused; python_version<'1'",
                        "present[feature]>=1", "present @ https://example.org/source.whl", "not a valid requirement"]
        with patch("simple_ar.research.preparation.metadata.version", side_effect=version) as lookup:
            probe = _dependency_probe({"requires-python": ">=1", "dependencies": requirements})
        self.assertEqual([row["status"] for row in probe["packages"]], ["version_matches", "version_mismatch",
            "distribution_not_found_here", "marker_not_applicable_here", "version_matches",
            "installed_source_not_verified", "unresolved_declaration"])
        self.assertEqual(probe["packages"][4]["unverified_extras"], ["feature"])
        self.assertTrue(probe["python_requirement"]["matches_inspecting_python"])
        self.assertNotIn("unused", [call.args[0] for call in lookup.call_args_list])
        self.assertFalse(probe["project_imported"])

    def test_probe_never_infers_dynamic_or_malformed_dependencies_as_ready(self):
        self.assertEqual(_dependency_probe({})["status"], "no_static_dependency_list")
        result = _dependency_probe({"requires-python": "not a specifier", "dependencies": [None]})
        self.assertEqual(result["python_requirement"]["status"], "invalid_declaration")
        self.assertEqual(result["packages"][0]["status"], "unresolved_declaration")

    def test_requirement_files_keep_line_identity_and_leave_installer_operations_unresolved(self):
        text = ('# heading\npresent>=1, \\\n <3 # compatible\n'
                'unused; python_version < "1"\n'
                'present @ https://example.org/pkg.whl#archive-fragment\n'
                '-r ../private.txt\n-c https://example.org/constraints.txt\n-e .\n'
                '--index-url https://example.org\n'
                'present @ https://example.org/${TOKEN}/pkg.whl\n'
                'present==2 --hash=sha256:unverified\n./local.whl\n')
        with patch('simple_ar.research.preparation.metadata.version', return_value='2.0') as lookup:
            result = _dependency_probe({}, requirements_excerpts=({'path': 'requirements.txt',
                'text': text, 'truncated': False},))
        self.assertEqual(result['status'], 'metadata_inspected')
        rows = result['packages']
        self.assertEqual(rows[0]['source_lines'], [2, 3])
        self.assertEqual(rows[0]['status'], 'version_matches')
        self.assertEqual(rows[1]['status'], 'marker_not_applicable_here')
        self.assertEqual(rows[2]['status'], 'installed_source_not_verified')
        self.assertTrue(rows[2]['declared'].endswith('#archive-fragment'))
        self.assertEqual([row['status'] for row in rows[3:7]], ['pip_directive_not_processed'] * 4)
        self.assertEqual(rows[7]['status'], 'environment_reference_not_expanded')
        self.assertEqual([row['status'] for row in rows[8:]], ['unresolved_declaration'] * 2)
        self.assertEqual(lookup.call_count, 2)

    def test_requirement_tail_never_becomes_a_complete_version_requirement(self):
        for text in ('present>=', 'present>=1, \\\n', 'present>=1, \\\n <'):
            with self.subTest(text=text), patch('simple_ar.research.preparation.metadata.version',
                    side_effect=AssertionError('partial line must not be inspected')):
                result = _dependency_probe({}, requirements_excerpts=({'path': 'requirements.txt',
                    'text': text, 'truncated': True},))
                self.assertEqual(result['packages'][0]['status'], 'incomplete_requirement_line')
        with patch('simple_ar.research.preparation.metadata.version', return_value='2.0'):
            result = _dependency_probe({}, requirements_excerpts=({'path': 'requirements.txt',
                'text': 'present>=1\n', 'truncated': True},))
        self.assertEqual(result['packages'][0]['status'], 'version_matches')
        self.assertTrue(result['requirements_sources'][0]['truncated'])

    def test_preparation_observes_separate_requirements_sources_without_following_includes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.project(root)
            (root / 'requirements.txt').write_text('present>=1\n-r /private/deps.txt\n')
            (root / 'requirements-dev.txt').write_text('present>=3\n')
            with patch('subprocess.run', side_effect=AssertionError('read only')), \
                    patch('simple_ar.research.preparation.metadata.version', return_value='2.0'):
                facts = inspect_project_preparation(root)
            rows = facts['dependency_probe']['packages']
            self.assertEqual(rows[0]['source_path'], 'pyproject.toml')
            versions = [row for row in rows if row.get('name') == 'present']
            self.assertEqual({row['source_path']: row['status'] for row in versions},
                {'requirements.txt': 'version_matches', 'requirements-dev.txt': 'version_mismatch'})
            self.assertEqual(next(row for row in rows if row['declared'].startswith('-r'))['status'],
                'pip_directive_not_processed')
            self.assertIn('requirements.txt:1–1', project_preparation_markdown(facts))

    def project(self, root):
        (root / 'README.md').write_text('# Setup\nRun python main.py --data input.csv.\n')
        (root / 'main.py').write_text('if __name__ == "__main__":\n    raise RuntimeError("must not execute")\n')
        (root / 'pyproject.toml').write_text('[project]\nname="demo"\nrequires-python=">=3.11"\ndependencies=["numpy"]\n[project.scripts]\ndemo="main:main"\n')
        (root / 'uv.lock').write_text('lock body must not enter model preparation context')

    def test_readonly_inventory_has_provenance_and_declared_not_installed_dependencies(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.project(root)
            with patch('subprocess.run', side_effect=AssertionError('must not execute')):
                facts = inspect_project_preparation(root, data_paths=(Path('missing.csv'),))
            self.assertEqual(facts['declared_project']['dependencies'], ['numpy'])
            self.assertIn('main.py', facts['entrypoint_candidates'])
            self.assertFalse(facts['data_paths'][0]['available'])
            self.assertIn('uv.lock', facts['document_paths'])
            self.assertFalse(any(row['path'] == 'uv.lock' for row in facts['excerpts']))
            self.assertIn('uv.lock', facts['source_file_paths'])
            explicit = inspect_project_preparation(root, read_paths=('uv.lock',))
            self.assertTrue(any(row['path'] == 'uv.lock' and 'lock body' in row['text']
                                for row in explicit['excerpts']))
            self.assertIn('project-authored', project_preparation_markdown(facts))
            self.assertIn('> Run python main.py', project_preparation_markdown(facts))
            self.assertNotIn('installed_dependencies', facts)

    def test_declared_console_entry_reads_src_layout_without_main_guard_or_import(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'pyproject.toml').write_text('[project.scripts]\nsimulate="study.cli:main"\n')
            (root / 'src/study').mkdir(parents=True)
            source = root / 'src/study/cli.py'
            source.write_text('raise RuntimeError("must not import")\n'
                              'def main():\n    print("METRIC score=1")\n')
            with patch('subprocess.run', side_effect=AssertionError('read only')):
                facts = inspect_project_preparation(root)
            self.assertIn('src/study/cli.py', facts['entrypoint_candidates'])
            self.assertEqual(facts['declared_entrypoints'][0]['source_candidates'], ['src/study/cli.py'])
            self.assertEqual(next(row['text'] for row in facts['excerpts']
                                  if row['path'] == 'src/study/cli.py'), source.read_text())
            self.assertIn('bindings and callables unverified', project_preparation_markdown(facts))

    def test_console_source_ambiguity_and_invalid_references_do_not_invent_bindings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'pyproject.toml').write_text('[project.scripts]\nrun="study:main"\n'
                'external="unavailable.cli:main"\nbad="../outside:main"\nmalformed=42\n')
            (root / 'study.py').write_text('def main():\n    pass\n')
            (root / 'src/study').mkdir(parents=True)
            (root / 'src/study/__init__.py').write_text('def main():\n    pass\n')
            facts = inspect_project_preparation(root)
            entries = {row['name']: row for row in facts['declared_entrypoints']}
            self.assertEqual(entries['run']['source_candidates'], ['study.py', 'src/study/__init__.py'])
            for key in ('external', 'bad', 'malformed'):
                self.assertEqual(entries[key]['source_candidates'], [])
            self.assertTrue(all(path in facts['source_file_paths'] for path in facts['entrypoint_candidates']))

    def test_declared_entries_share_existing_read_allowance_and_dependency_exclusions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'pyproject.toml').write_text('[project.scripts]\n' +
                ''.join(f'job{i}="job{i}:main"\n' for i in range(8)))
            for i in range(8):
                (root / f'job{i}.py').write_text('def main():\n    pass\n')
            facts = inspect_project_preparation(root)
            self.assertEqual(len(facts['entrypoint_candidates']), 8)
            self.assertEqual(len([row for row in facts['excerpts'] if row['role'] == 'entry_source']), 5)
            self.assertNotIn('job7.py', facts['reading_coverage'])
            extra = inspect_project_preparation(root, read_paths=('job7.py',))
            self.assertIn('job7.py', extra['reading_coverage'])

    def test_review_api_uses_observed_source_without_second_file_read(self):
        from simple_ar.code_task.review_pipeline import build_review_index
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'module.py').write_text('def visible(value):\n    return value\n')
            original = Path.read_bytes
            reads = []
            def observe(path, *args, **kwargs):
                reads.append(path)
                return original(path, *args, **kwargs)
            with patch.object(Path, 'read_bytes', observe):
                index = build_review_index(root, access_boundary=root)
            self.assertEqual(reads, [root / 'module.py'])
            self.assertIn('def visible(value)', index['files'][0]['public_api'])

    def test_root_manifest_retains_priority_with_many_readmes_in_same_document_allowance(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for i in range(20):
                (root / f'README-{i:02d}.md').write_text('Project documentation\n')
            (root / 'pyproject.toml').write_text('[project.scripts]\nrun="study:main"\n')
            (root / 'study.py').write_text('def main():\n    pass\n')
            facts = inspect_project_preparation(root, read_paths=('pyproject.toml',))
            self.assertIn('study.py', facts['entrypoint_candidates'])
            documents = [row for row in facts['excerpts'] if row['role'] in
                         {'dependency_declaration', 'project_instructions'}]
            self.assertEqual(len(documents), 12)
            self.assertEqual(sum(row['path'] == 'pyproject.toml' for row in facts['excerpts']), 1)

    def test_design_entry_uses_the_same_fact_owner_without_new_authority(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.project(root)
            facts = inspect_execution_entry({'code_task': {'code_root': str(root)}})
            self.assertEqual(facts['preparation']['project'], str(root.resolve()))
            self.assertFalse(any('pip' in prefix for prefix in facts['authorized_argv_prefixes']))

    def test_cli_emits_reusable_notes_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.project(root)
            output = root / 'inspection'
            arguments = ['project-info', '--project', str(root), '--data-path', 'missing.csv', '--output', str(output)]
            with contextlib.redirect_stdout(io.StringIO()):
                main(arguments)
            self.assertTrue((output / 'preparation.md').is_file())
            self.assertEqual(json.loads((output / 'preparation.json').read_text())['schema_version'], 'project_preparation.v1')
            with self.assertRaisesRegex(SystemExit, 'not overwritten'):
                main(arguments)

    def test_long_or_invalid_manifest_preserves_unread_or_unparsed_status(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.project(root)
            (root / 'README.md').write_text('a' * 18000)
            (root / 'pyproject.toml').write_text('[invalid')
            facts = inspect_project_preparation(root)
            self.assertTrue(next(row for row in facts['excerpts'] if row['path'] == 'README.md')['truncated'])
            self.assertEqual(facts['declared_project'], {})
            self.assertTrue(any('could not be parsed' in note for note in facts['notes']))

    def test_valid_toml_with_invalid_project_shape_preserves_other_preparation_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.project(root)
            (root / 'pyproject.toml').write_text('project = 42\n')
            (root / 'requirements.txt').write_text('present>=1\n')
            with patch('simple_ar.research.preparation.metadata.version', return_value='2.0'):
                facts = inspect_project_preparation(root)
            self.assertEqual(facts['declared_project'], {})
            self.assertTrue(any('not a table' in note for note in facts['notes']))
            self.assertEqual(facts['dependency_probe']['packages'][0]['status'], 'version_matches')

    def test_legacy_saved_probe_without_source_path_is_still_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.project(root)
            facts = inspect_project_preparation(root)
            for row in facts['dependency_probe']['packages']:
                row.pop('source_path', None)
            self.assertIn('declaration', project_preparation_markdown(facts))

    def test_notebook_candidates_reuse_index_without_reading_outputs_or_dependency_trees(self):
        from simple_ar.code_task.review_pipeline import build_review_index, read_code_task_text
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.project(root)
            notebook = root / 'experiment.ipynb'
            notebook.write_text('untrusted saved outputs' * 60000)
            dependency = root / '.venv' / 'large-library'
            dependency.mkdir(parents=True)
            (dependency / 'main.py').write_text('not project code')
            checkpoint = root / '.ipynb_checkpoints'
            checkpoint.mkdir()
            (checkpoint / 'copy.ipynb').write_text('old outputs')
            with patch('simple_ar.code_task.review_pipeline.read_code_task_text', wraps=read_code_task_text) as read:
                index = build_review_index(root, access_boundary=root)
                facts = inspect_project_preparation(root, index=index)
            self.assertFalse(any(call.args[0] == notebook for call in read.call_args_list))
            self.assertFalse(any(call.args[0].is_relative_to(root / '.venv')
                or call.args[0].is_relative_to(checkpoint) for call in read.call_args_list))
            self.assertFalse(any(row['path'].startswith(('.venv/', '.ipynb_checkpoints/'))
                for row in index['files']))
            self.assertEqual([row['path'] for row in index['notebooks']], ['experiment.ipynb'])
            self.assertEqual([row['path'] for row in facts['notebook_candidates']], ['experiment.ipynb'])
            self.assertEqual(facts['notebook_candidates'][0]['inspection'], 'path_only_cells_and_outputs_unread')
            self.assertIn('Notebook candidates', project_preparation_markdown(facts))
            self.assertNotIn('experiment.ipynb', facts['entrypoint_candidates'])

    def test_entry_detection_is_python_structure_not_one_quote_style_or_documentation_text(self):
        from simple_ar.code_task.review_pipeline import _is_entrypoint_candidate
        self.assertTrue(_is_entrypoint_candidate('run.py', "if __name__ == '__main__':\n    main()\n"))
        self.assertTrue(_is_entrypoint_candidate('run.py', 'if "__main__" == __name__ and ready:\n    main()\n'))
        self.assertFalse(_is_entrypoint_candidate('notes.py', '\'\'\'Example: if __name__ == "__main__"\'\'\'\n'))
        self.assertFalse(_is_entrypoint_candidate('library.py', "if __name__ != '__main__':\n    imported()\n"))

    def test_requested_configuration_is_readonly_bounded_and_uses_review_index(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.project(root)
            (root / 'README.rst').write_text('Project setup and test instructions in reStructuredText.\n')
            (root / 'configuration.py').write_text('value = 1\n' * 2000)
            (root / 'justfile').write_text('run:\n    python main.py --trials 10000\n')
            (root / 'run.sh').write_text('python main.py --trials 10000\n')
            (root / 'recipe.custom').write_text('trials = 10000\n')
            (root / 'image.png').write_bytes(b'\x89PNG\x00binary')
            (root / '.env').write_text('SECRET=not-source\n')
            (root / '.env.local').write_text('SECRET=not-source\n')
            (root / 'uv.lock').write_text('generated lock data\n')
            with patch('subprocess.run', side_effect=AssertionError('read only')):
                facts = inspect_project_preparation(root, read_paths=('configuration.py', 'justfile', 'run.sh', 'recipe.custom'))
            row = next(row for row in facts['excerpts'] if row['path'] == 'configuration.py')
            self.assertIn('README.rst', facts['document_paths'])
            self.assertIn('reStructuredText', next(row['text'] for row in facts['excerpts'] if row['path'] == 'README.rst'))
            self.assertEqual(len(row['text']), 8000)
            self.assertTrue(row['truncated'])
            self.assertIn('configuration.py', facts['source_file_paths'])
            for name in ('justfile', 'run.sh', 'recipe.custom'):
                self.assertIn(name, facts['source_file_paths'])
                self.assertIn('10000', next(row['text'] for row in facts['excerpts'] if row['path'] == name))
            for name in ('image.png', '.env', '.env.local'):
                self.assertNotIn(name, facts['source_file_paths'])
            self.assertIn('up to 8000 characters', project_preparation_markdown(facts))
            with self.assertRaisesRegex(ValueError, 'indexed'):
                inspect_project_preparation(root, read_paths=('../outside.py',))

    def test_preparation_continues_entry_tail_even_when_only_one_character_is_unread(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.project(root)
            (root / 'main.py').write_text('#' + 'x' * 7999 + '\n')
            initial = inspect_project_preparation(root)
            self.assertTrue(next(row for row in initial['excerpts'] if row['path'] == 'main.py')['has_unread_tail'])
            facts = inspect_project_preparation(root, read_paths=('main.py',))
            rows = [row for row in facts['excerpts'] if row['path'] == 'main.py']
            self.assertEqual(len(rows), 2)
            self.assertFalse(rows[-1]['has_unread_tail'])
            self.assertTrue(rows[-1]['text'].endswith('\n'))
            self.assertFalse(facts['reading_coverage']['main.py']['has_unread_tail'])


class ProjectDataPreparationTests(unittest.TestCase):
    def prepare_execution(self, root, project, policy, *, data_paths=()):
        from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
        from simple_ar.experiment.execution.backend import RunRequest
        from simple_ar.research.preparation import PreparationRequest, run_preparation_capability

        context = CapabilityContext(ArtifactStore(root), AttemptManifest('prepare-policy'))
        config = {'command': ['python', 'main.py'], 'cwd': str(project), 'timeout_sec': 5,
            'code_task': {'code_root': str(project), 'approval_note': 'Fixture isolated edits', **policy}}
        before = json.loads(json.dumps(config))
        result = run_preparation_capability(context=context, request=PreparationRequest(
            config, 'Check workspace policy', RunRequest(['python', 'main.py'], project, timeout_sec=5),
            data_paths=data_paths))
        self.assertEqual(result.status, 'completed', result.diagnostics)
        self.assertEqual(config, before)  # Preparation must not mutate the caller's declaration.
        payload = context.store.read_json('execution.json')
        for name in ('code_root', 'workspace_mode', 'max_file_bytes', 'workspace_include',
                     'workspace_exclude', 'workspace_reuse_source_venv'):
            self.assertNotIn(name, payload['execution']['code_task'])
        from simple_ar.app.research_execution import implementation_request
        implementation_request(payload['execution'], object(), validate=True)
        return payload

    def test_sparse_preparation_filters_caps_and_reload_keeps_selected_source_venv(self):
        from simple_ar.app.research_application import ResearchApplicationServices, create_session, load_session
        from simple_ar.app.research_execution import execution_request, implementation_request
        from simple_ar.core.artifacts import read_json
        from simple_ar.research.workflow_contracts import ResearchBrief

        with tempfile.TemporaryDirectory() as folder:
            root, project = Path(folder), Path(folder) / 'project'
            (project / 'src/private').mkdir(parents=True)
            (project / 'main.py').write_text('print("fixture")\n')
            (project / 'check.py').write_text('pass\n')
            (project / 'src/keep.py').write_text('VALUE = 1\n')
            (project / 'src/private/hidden.py').write_text('PRIVATE = 1\n')
            (project / 'src/large.py').write_bytes(b'x' * 65)
            (project / 'src/exact.py').write_bytes(b'#' * 64)
            (project / 'notes.txt').write_text('outside include')
            (project / '.env').write_text('fixture only')
            # Detection needs an interpreter file, not a created or executed venv.
            source_python = project / '.venv/Scripts/python.exe'
            source_python.parent.mkdir(parents=True)
            source_python.write_bytes(b'fixture interpreter; never execute')
            source_python = source_python.resolve()
            original = {p.relative_to(project).as_posix(): p.read_bytes()
                        for p in project.rglob('*') if p.is_file() and '.venv' not in p.parts}
            execution = {'command': ['python', 'main.py'], 'cwd': str(project), 'timeout_sec': 5,
                'code_task': {'code_root': str(project), 'approval_note': 'Fixture isolated edits',
                    'workspace_mode': 'sparse_copy', 'max_file_bytes': 64,
                    'workspace_include': ['main.py', 'check.py', 'src/**', '.env'],
                    'workspace_exclude': ['src/private/**'], 'workspace_reuse_source_venv': True,
                    'env_mode': 'current', 'validation_command': ['python', 'check.py'],
                    'validation_timeout_sec': 5}}
            before = json.loads(json.dumps(execution))
            services = ResearchApplicationServices(config={'research_task_kind': 'bug_fix',
                'execution': execution}, budget_limits={'process_invocations': 0})
            app = create_session(ResearchBrief(request_text='Check isolated workspace',
                requested_outputs=('bug_fix',)), root=root / 'session', services=services)
            # Only suppress the version subprocess: workspace creation, environment
            # selection, persistence and downstream argv resolution remain real.
            with patch('simple_ar.code_task.execution.environment._python_version_for_policy',
                       return_value='fixture'):
                self.assertTrue(app._run_action('prepare_execution'), app.controller.manifest.status_reason)
            ref = app._active_preparation_ref()
            payload = app.controller.store.read_json(ref)
            prepared, workspace = payload['execution'], Path(payload['workspace'])
            self.assertEqual(payload['workspace_info']['selected_mode'], 'sparse_copy')
            self.assertEqual((workspace / 'src/keep.py').read_text(), 'VALUE = 1\n')
            self.assertEqual((workspace / 'src/exact.py').stat().st_size, 64)
            for name in ('src/private/hidden.py', 'src/large.py', 'notes.txt', '.env', '.venv'):
                self.assertFalse((workspace / name).exists(), name)
            skips = {row['path']: row['reason'] for row in payload['copy_report']['skipped']}
            self.assertEqual(skips['src/large.py'], 'file_too_large')
            manifest_path = Path(prepared['code_task']['run_dir']) / 'manifest.json'
            manifest = read_json(manifest_path)
            selected = manifest['environment']['policy']
            self.assertEqual(selected['mode'], 'external')
            self.assertEqual(Path(selected['python_executable']), source_python)
            self.assertEqual(manifest['copy']['max_file_bytes'], 64)
            self.assertEqual(payload['workspace_info']['environment_mapping']['python_executable'], str(source_python))
            for name in ('code_root', 'workspace_mode', 'max_file_bytes', 'workspace_include',
                         'workspace_exclude', 'workspace_reuse_source_venv'):
                self.assertNotIn(name, prepared['code_task'])
            impl = implementation_request(prepared, object())
            self.assertEqual(impl.validation_command, (str(source_python), 'check.py'))
            self.assertEqual(execution_request(prepared).run.command, [str(source_python), 'main.py'])
            (workspace / 'src/keep.py').write_text('VALUE = 2\n')
            (project / 'src/keep.py').write_text('VALUE = 3\n')
            saved = manifest_path.read_bytes()
            # Real reload, with a tripwire only on the forbidden reinitialization.
            with patch('simple_ar.research.preparation.initialize_code_task',
                       side_effect=AssertionError('reload must not recopy the source')):
                recovered = load_session(root / 'session', services=services)
                effective = recovered._effective_config()['execution']
                self.assertEqual(recovered._active_preparation_ref(), ref)
                self.assertEqual(effective, prepared)
                self.assertEqual(implementation_request(effective, object()).validation_command,
                                 (str(source_python), 'check.py'))
                self.assertEqual(execution_request(effective).run.command, [str(source_python), 'main.py'])
            self.assertEqual(manifest_path.read_bytes(), saved)
            self.assertEqual((workspace / 'src/keep.py').read_text(), 'VALUE = 2\n')
            self.assertEqual(services.config['execution'], before)
            original['src/keep.py'] = b'VALUE = 3\n'
            self.assertEqual(original, {p.relative_to(project).as_posix(): p.read_bytes()
                for p in project.rglob('*') if p.is_file() and '.venv' not in p.parts})

    def test_preparation_copy_policy_keeps_declared_data_at_protected_relative_path(self):
        from simple_ar.code_task.editing.scope import is_edit_allowed_path
        with tempfile.TemporaryDirectory() as folder:
            root, project = Path(folder), Path(folder) / 'project'
            (project / 'data').mkdir(parents=True)
            (project / 'main.py').write_text('pass\n')
            data = project / 'data/input[1].bin'
            data.write_bytes(b'd' * 65)
            (project / 'unrelated.bin').write_bytes(b'x' * 65)
            (project / 'notes.txt').write_text('copy keeps this')
            external = root / 'external.bin'
            external.write_bytes(b'e' * 65)
            payload = self.prepare_execution(root / 'attempt', project,
                {'workspace_mode': 'copy', 'max_file_bytes': 64,
                 'workspace_include': ['main.py'], 'workspace_exclude': ['notes.txt'],
                 'workspace_reuse_source_venv': False}, data_paths=(data, external))
            workspace = Path(payload['workspace'])
            self.assertEqual((workspace / 'data/input[1].bin').read_bytes(), data.read_bytes())
            self.assertFalse((workspace / 'unrelated.bin').exists())
            self.assertFalse((workspace / 'external.bin').exists())
            self.assertEqual((workspace / 'notes.txt').read_text(), 'copy keeps this')
            self.assertEqual(payload['workspace_info']['patterns']['data_inputs'], ['data/input[1].bin'])
            manifest = json.loads((Path(payload['execution']['code_task']['run_dir']) / 'manifest.json').read_text())
            self.assertFalse(is_edit_allowed_path('data/input[1].bin',
                protected_patterns=manifest['edit_scope']['protected_patterns']))
            self.assertNotIn('data/input[1].bin', {row['path'] for row in payload['copy_report']['skipped']})
            self.assertEqual(data.read_bytes(), b'd' * 65)
            self.assertEqual(external.read_bytes(), b'e' * 65)

    def test_preparation_default_and_zero_file_caps_remain_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            root, project = Path(folder), Path(folder) / 'project'
            project.mkdir()
            (project / 'main.py').write_text('pass\n')
            (project / 'large.bin').write_bytes(b'x' * 2_000_001)
            for label, policy, copied in (
                ('legacy', {}, False),
                ('unlimited', {'workspace_mode': 'copy', 'max_file_bytes': 0}, True),
            ):
                with self.subTest(policy=label):
                    payload = self.prepare_execution(root / label, project, policy)
                    self.assertEqual((Path(payload['workspace']) / 'large.bin').exists(), copied)
                    mapping = payload['workspace_info']['environment_mapping']
                    self.assertFalse(mapping['reuse_source_venv'])
                    self.assertEqual(mapping['python_executable'], '')

    def test_preparation_rejects_invalid_workspace_policy_and_sparse_declared_data(self):
        from simple_ar.code_task.workspace.modes import WorkspaceModeError
        with tempfile.TemporaryDirectory() as folder:
            root, project = Path(folder), Path(folder) / 'project'
            project.mkdir()
            (project / 'main.py').write_text('pass\n')
            invalid = ({'max_file_bytes': -1}, {'max_file_bytes': '64'},
                {'max_file_bytes': True}, {'max_file_bytes': 1.5},
                {'workspace_include': 'main.py'}, {'workspace_exclude': [1]},
                {'workspace_reuse_source_venv': 'false'})
            for index, policy in enumerate(invalid):
                with self.subTest(policy=policy), self.assertRaises(ValueError):
                    self.prepare_execution(root / f'invalid-{index}', project, {'workspace_mode': 'copy', **policy})
            for index, policy in enumerate(({'workspace_mode': 'sparse_copy'},
                    {'kind': 'greenfield', 'workspace_mode': 'empty'})):
                with self.subTest(policy=policy), self.assertRaises(WorkspaceModeError):
                    self.prepare_execution(root / f'data-{index}', project, policy,
                                           data_paths=(project / 'main.py',))

    def test_preparation_rejects_source_venv_reuse_with_managed_environment(self):
        from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
        from simple_ar.experiment.execution.backend import RunRequest
        from simple_ar.research.preparation import PreparationRequest, run_preparation_capability
        with tempfile.TemporaryDirectory() as folder:
            root, project = Path(folder), Path(folder) / 'project'
            project.mkdir()
            (project / 'main.py').write_text('pass\n')
            (project / 'check.py').write_text('pass\n')
            config = {'command': ['python', 'main.py'], 'cwd': str(project), 'timeout_sec': 5,
                'protocol': {'hypothesis': 'Fixture', 'dataset': 'Fixed fixture', 'expected_outcome': 'Pass'},
                'environment': {'mode': 'venv', 'timeout_sec': 10},
                'code_task': {'code_root': str(project), 'approval_note': 'Fixture isolated edits',
                    'workspace_mode': 'copy', 'workspace_reuse_source_venv': True, 'env_mode': 'current',
                    'validation_command': ['python', 'check.py'], 'validation_timeout_sec': 5}}
            context = CapabilityContext(ArtifactStore(root / 'attempt'), AttemptManifest('conflict'))
            with self.assertRaisesRegex(ValueError, 'source-venv|source.*venv'):
                run_preparation_capability(context=context, request=PreparationRequest(config, 'Check policy',
                    RunRequest(['python', 'check.py'], project, timeout_sec=5)))
            self.assertFalse((context.store.root / 'execution.json').exists())
            self.assertFalse((context.store.root / 'project_run').exists())

    def initialize(self, root, **kwargs):
        from simple_ar.code_task.orchestration.workflow import initialize_code_task
        task = root / 'task.md'
        task.write_text('Check project behavior')
        return initialize_code_task(run_dir=root / 'run', code_root=root / 'project', task_file=task, **kwargs)

    def test_large_named_inputs_are_copied_and_edit_protected_without_broadening_discovery(self):
        from simple_ar.code_task.editing.scope import is_edit_allowed_path
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / 'project'
            (project / 'data').mkdir(parents=True)
            (project / 'main.py').write_text('VALUE = 1\n')
            data = project / 'data/input[1].bin'
            data.write_bytes(b'x' * 2_000_001)
            (project / 'unrelated.bin').write_bytes(b'y' * 2_000_001)
            initialized = self.initialize(root, data_inputs=('data/input[1].bin',))
            copied = initialized.workspace_dir / 'data/input[1].bin'
            self.assertEqual(copied.read_bytes(), data.read_bytes())
            self.assertFalse((initialized.workspace_dir / 'unrelated.bin').exists())
            manifest = json.loads(initialized.manifest_path.read_text())
            self.assertFalse(is_edit_allowed_path('data/input[1].bin',
                protected_patterns=manifest['edit_scope']['protected_patterns']))
            skipped = {row['path'] for row in initialized.copy_report.skipped}
            self.assertNotIn('data/input[1].bin', skipped)
            self.assertIn('unrelated.bin', skipped)
            copied.write_bytes(b'changed only in the copy')
            self.assertEqual(data.stat().st_size, 2_000_001)

            # An allow pattern alone never creates a file.
            from simple_ar.code_task.orchestration.workflow import initialize_code_task, INITIAL_ADAPTER_SOURCE
            task = root / 'task.md'
            untouched = initialize_code_task(run_dir=root / 'allow-only', code_root=project,
                task_file=task, edit_scope_allowed_patterns=('adapter.py',))
            self.assertFalse((untouched.workspace_dir / 'adapter.py').exists())
            created = initialize_code_task(run_dir=root / 'explicit', code_root=project,
                task_file=task, edit_scope_allowed_patterns=('adapters/export.py',),
                initial_files=('adapters/export.py',))
            self.assertEqual((created.workspace_dir / 'adapters/export.py').read_text(), INITIAL_ADAPTER_SOURCE)
            self.assertFalse((project / 'adapters').exists())
            rejected = ('main.py', 'tests/check.py', 'data/new.py', '../escape.py',
                        '/absolute.py', 'C:/escape.py', 'adapter*.py', 'adapter.txt')
            for i, name in enumerate(rejected):
                with self.subTest(initial_file=name), self.assertRaises(ValueError):
                    initialize_code_task(run_dir=root / f'rejected-{i}', code_root=project,
                        task_file=task, edit_scope_allowed_patterns=(name,), initial_files=(name,))
            with self.assertRaises(ValueError):
                initialize_code_task(run_dir=root / 'no-exact-allow', code_root=project,
                    task_file=task, edit_scope_allowed_patterns=('*.py',), initial_files=('adapter.py',))
            with self.assertRaises(ValueError):
                initialize_code_task(run_dir=project / 'forbidden-run', code_root=project,
                    task_file=task, edit_scope_allowed_patterns=('adapter.py',), initial_files=('adapter.py',))
            self.assertFalse((project / 'forbidden-run').exists())
            with self.assertRaises(ValueError):
                initialize_code_task(run_dir=root / 'duplicate', code_root=project, task_file=task,
                    edit_scope_allowed_patterns=('adapter.py',), initial_files=('adapter.py', 'adapter.py'))
            self.assertFalse((root / 'duplicate/code_task/workspace/adapter.py').exists())

    def test_explicit_directory_retains_relative_paths_but_does_not_copy_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / 'project/data/nested'
            data.mkdir(parents=True)
            (data / 'observations.csv').write_text('value\n7\n')
            (data / '.env').write_text('private')
            initialized = self.initialize(root, max_file_bytes=1, data_inputs=('data', 'data/nested/observations.csv'))
            self.assertTrue((initialized.workspace_dir / 'data/nested/observations.csv').is_file())
            self.assertFalse((initialized.workspace_dir / 'data/nested/.env').exists())
            self.assertEqual(initialized.copy_report.files_copied, 1)

    def test_restored_input_updates_total_even_when_skip_details_were_capped(self):
        from simple_ar.code_task.workspace.copy import CopyReport, copy_workspace_inputs
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'workspace'
            workspace.mkdir()
            (root / 'data.csv').write_text('value\n7\n')
            report = CopyReport(1, 10, 300, ({'path': 'other.bin', 'kind': 'file', 'reason': 'file_too_large'},))
            updated = copy_workspace_inputs(root, workspace, ('data.csv',), report)
            self.assertEqual(updated.skipped_count, 299)
            self.assertEqual(updated.files_copied, 2)
            self.assertEqual(updated.skipped, report.skipped)

    def test_declared_input_cannot_bypass_secret_or_path_boundaries(self):
        from simple_ar.code_task.workspace.copy import copy_workspace_inputs, empty_copy_report
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'workspace'
            workspace.mkdir()
            (root / '.env').write_text('private')
            for name in ('.env', '../outside', str(root / '.env'), '.'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    copy_workspace_inputs(root, workspace, (name,), empty_copy_report())
            self.assertEqual(list(workspace.iterdir()), [])

    def test_insufficient_disk_capacity_is_checked_before_any_data_overlay(self):
        from simple_ar.code_task.workspace.copy import copy_workspace_inputs, empty_copy_report
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / 'workspace'
            workspace.mkdir()
            (root / 'data.csv').write_text('value\n7\n')
            with patch('simple_ar.code_task.workspace.copy.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
                with self.assertRaises(OSError):
                    copy_workspace_inputs(root, workspace, ('data.csv',), empty_copy_report())
            self.assertEqual(list(workspace.iterdir()), [])

    def test_source_and_destination_symlinks_are_not_followed(self):
        from simple_ar.code_task.workspace.copy import copy_workspace_inputs, empty_copy_report
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, workspace = root / 'source', root / 'workspace'
            source.mkdir()
            workspace.mkdir()
            (source / 'data.csv').write_text('value\n7\n')
            outside = root / 'outside.csv'
            outside.write_text('unchanged')
            try:
                (source / 'linked.csv').symlink_to(outside)
                (workspace / 'data.csv').symlink_to(outside)
            except OSError:
                self.skipTest('symlinks unavailable')
            for name in ('linked.csv', 'data.csv'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    copy_workspace_inputs(source, workspace, (name,), empty_copy_report())
            self.assertEqual(outside.read_text(), 'unchanged')
            from simple_ar.code_task.orchestration.workflow import initialize_code_task
            task = root / 'task.md'
            task.write_text('Initialize only a confirmed adapter')
            (source / 'linked').symlink_to(workspace, target_is_directory=True)
            from simple_ar.code_task.analysis.index import build_codebase_index
            original_open = Path.open
            def bounded_open(path, *args, **kwargs):
                self.assertFalse(path.is_symlink(), 'must not open a link target')
                self.assertTrue(path.resolve().is_relative_to(source.resolve()))
                return original_open(path, *args, **kwargs)
            with patch.object(Path, 'open', new=bounded_open):
                inventory = build_codebase_index(source)
            self.assertEqual([row['path'] for row in inventory['files']], ['data.csv'])
            with self.assertRaises(ValueError):
                initialize_code_task(run_dir=root / 'adapter-run', code_root=source,
                    task_file=task, edit_scope_allowed_patterns=('linked/adapter.py',),
                    initial_files=('linked/adapter.py',))
            self.assertFalse((workspace / 'adapter.py').exists())

    def test_worktree_can_materialize_untracked_named_data(self):
        import subprocess
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / 'project'
            project.mkdir()
            (project / 'main.py').write_text('VALUE = 1\n')
            subprocess.run(['git', 'init', '-q', str(project)], check=True)
            subprocess.run(['git', '-C', str(project), 'add', 'main.py'], check=True)
            subprocess.run(['git', '-C', str(project), '-c', 'user.email=fixture@example.org',
                            '-c', 'user.name=Fixture', 'commit', '-qm', 'baseline'], check=True)
            (project / 'data.csv').write_text('value\n7\n')
            initialized = self.initialize(root, workspace_mode='git_worktree', data_inputs=('data.csv',),
                edit_scope_allowed_patterns=('adapter.py',), initial_files=('adapter.py',))
            self.assertEqual(initialized.workspace.selected_mode, 'git_worktree')
            self.assertEqual((initialized.workspace_dir / 'data.csv').read_text(), 'value\n7\n')
            from simple_ar.code_task.orchestration.workflow import INITIAL_ADAPTER_SOURCE
            self.assertEqual((initialized.workspace_dir / 'adapter.py').read_text(), INITIAL_ADAPTER_SOURCE)
            self.assertFalse((project / 'adapter.py').exists())

    def test_preparation_maps_original_data_into_candidate_revision_without_reimporting_original(self):
        from simple_ar.app.research_execution import execution_request, implementation_request
        from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
        from simple_ar.experiment.execution.backend import RunRequest
        from simple_ar.research.preparation import (
            PreparationRequest, revision_preparation_config, run_preparation_capability,
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = root / 'original'
            original.mkdir()
            (original / 'data.csv').write_text('original')
            (original / 'main.py').write_text('VALUE = 1\n')
            (original / 'ignored.py').write_text('IGNORED = True\n')
            source_python = original / '.venv/Scripts/python.exe'
            source_python.parent.mkdir(parents=True)
            source_python.write_bytes(b'fixture interpreter; never execute')
            source_python = source_python.resolve()
            policy = {'workspace_mode': 'sparse_copy', 'max_file_bytes': 64,
                      'workspace_include': ['main.py', 'data.csv'], 'workspace_exclude': ['ignored.py'],
                      'workspace_reuse_source_venv': True, 'env_mode': 'current'}
            original_bytes = {p.relative_to(original).as_posix(): p.read_bytes()
                              for p in original.rglob('*') if p.is_file()}
            with patch('simple_ar.code_task.execution.environment._python_version_for_policy',
                       return_value='fixture'):
                prepared = self.prepare_execution(root / 'source-attempt', original, policy)
            candidate = Path(prepared['workspace'])
            self.assertFalse((candidate / '.venv').exists())
            (candidate / 'data.csv').write_text('accepted candidate input')
            (candidate / 'main.py').write_text('VALUE = 2\n')
            (candidate / 'accepted.py').write_text('ACCEPTED = True\n')
            external = root / 'external.csv'
            external.write_text('external')
            context = CapabilityContext(ArtifactStore(root / 'attempt'), AttemptManifest('prepare-1'))
            declared = {'command': ['python', 'main.py'], 'cwd': str(original), 'timeout_sec': 5,
                'code_task': {'code_root': str(original), 'approval_note': 'isolated edits', **policy}}
            before = json.loads(json.dumps(declared))
            config = revision_preparation_config(declared, prepared, source_root=candidate)
            self.assertEqual(config['code_task']['max_file_bytes'], 64)
            self.assertEqual(config['code_task']['workspace_mode'], 'copy')
            for name in ('workspace_include', 'workspace_exclude', 'workspace_reuse_source_venv'):
                self.assertNotIn(name, config['code_task'])
            self.assertEqual(config['code_task']['env_mode'], 'external')
            self.assertEqual(Path(config['code_task']['python_executable']), source_python)
            baseline = revision_preparation_config(declared, prepared, source_root=original)
            self.assertEqual(baseline['code_task']['env_mode'], 'current')
            self.assertNotIn('python_executable', baseline['code_task'])
            self.assertEqual(baseline['code_task']['max_file_bytes'], 64)
            for name in ('workspace_mode', 'workspace_include', 'workspace_exclude', 'workspace_reuse_source_venv'):
                self.assertEqual(baseline['code_task'][name], policy[name])
            self.assertEqual(declared, before)
            baseline_context = CapabilityContext(ArtifactStore(root / 'baseline-attempt'), AttemptManifest('baseline'))
            with patch('simple_ar.code_task.execution.environment._python_version_for_policy', return_value='fixture'):
                baseline_result = run_preparation_capability(context=baseline_context,
                    request=PreparationRequest(baseline, 'Reimport declared baseline', execution_request(baseline).run,
                                               source_project=original))
            self.assertEqual(baseline_result.status, 'completed')
            baseline_payload = baseline_context.store.read_json('execution.json')
            baseline_workspace = Path(baseline_payload['workspace'])
            self.assertEqual(baseline_payload['workspace_info']['selected_mode'], 'sparse_copy')
            self.assertEqual((baseline_workspace / 'main.py').read_text(), 'VALUE = 1\n')
            self.assertFalse((baseline_workspace / 'ignored.py').exists())
            self.assertFalse((baseline_workspace / 'accepted.py').exists())
            with patch('simple_ar.code_task.execution.environment._python_version_for_policy',
                       return_value='fixture'):
                result = run_preparation_capability(context=context, request=PreparationRequest(config, 'Check data',
                    execution_request(config).run, source_project=original / '../original',
                    data_paths=(original / 'data.csv', external)))
            self.assertEqual(result.status, 'completed')
            payload = context.store.read_json('execution.json')
            workspace = Path(payload['workspace'])
            self.assertEqual((workspace / 'data.csv').read_text(), 'accepted candidate input')
            self.assertEqual((workspace / 'main.py').read_text(), 'VALUE = 2\n')
            self.assertEqual((workspace / 'accepted.py').read_text(), 'ACCEPTED = True\n')
            self.assertEqual(execution_request(payload['execution']).run.command,
                             [str(source_python), 'main.py'])
            self.assertEqual(implementation_request(payload['execution'], object(), validate=True).validation_command,
                             (str(source_python), 'main.py'))
            self.assertFalse((workspace / 'external.csv').exists())
            self.assertEqual(payload['workspace_info']['patterns']['data_inputs'], ['data.csv'])
            self.assertEqual((original / 'data.csv').read_text(), 'original')
            # An accepted edit may grow past the original cap. Preserve that cap
            # and reject an incomplete revision instead of silently dropping it.
            (candidate / 'main.py').write_text('VALUE = 2\n' + '# accepted edit\n' * 8)
            accepted_bytes = {p.relative_to(candidate).as_posix(): p.read_bytes()
                              for p in candidate.rglob('*') if p.is_file()}
            prior_bytes = {p.relative_to(workspace).as_posix(): p.read_bytes()
                           for p in workspace.rglob('*') if p.is_file()}
            saved_execution = (context.store.root / 'execution.json').read_bytes()
            rejected = CapabilityContext(ArtifactStore(root / 'rejected-revision'), AttemptManifest('too-large'))
            limited = revision_preparation_config(declared, prepared, source_root=candidate)
            self.assertEqual(limited['code_task']['max_file_bytes'], 64)
            with self.assertRaisesRegex(ValueError, 'Candidate revision copy omitted'):
                with patch('simple_ar.code_task.execution.environment._python_version_for_policy',
                           return_value='fixture'):
                    run_preparation_capability(context=rejected, request=PreparationRequest(limited, 'Keep accepted edit',
                        execution_request(limited).run, source_project=original,
                        data_paths=(original / 'data.csv',)))
            self.assertFalse((rejected.store.root / 'execution.json').exists())
            self.assertEqual(original_bytes, {p.relative_to(original).as_posix(): p.read_bytes()
                for p in original.rglob('*') if p.is_file()})
            self.assertEqual(accepted_bytes, {p.relative_to(candidate).as_posix(): p.read_bytes()
                for p in candidate.rglob('*') if p.is_file()})
            self.assertEqual(prior_bytes, {p.relative_to(workspace).as_posix(): p.read_bytes()
                for p in workspace.rglob('*') if p.is_file()})
            self.assertEqual((context.store.root / 'execution.json').read_bytes(), saved_execution)
            self.assertEqual(declared, before)
            new_context = CapabilityContext(ArtifactStore(root / 'new-attempt'), AttemptManifest('prepare-new'))
            # Formal execution retains its own explicit limit; the preparation
            # probe's RunRequest is not authorization for a later measurement.
            new_config = {'command': ['python', 'generated_project/main.py'], 'timeout_sec': 5, 'code_task': {'kind': 'greenfield', 'code_root': None, 'workspace_mode': 'empty',
                'approval_note': 'Create in isolated empty workspace', 'allowed_patterns': ['generated_project/**']}}
            new_result = run_preparation_capability(context=new_context, request=PreparationRequest(new_config,
                'Create a reusable program', RunRequest(['python', 'generated_project/main.py'], root, timeout_sec=5)))
            self.assertEqual(new_result.status, 'completed')
            new_payload = new_context.store.read_json('execution.json')
            self.assertIsNone(new_payload['source_project'])
            self.assertFalse((Path(new_payload['workspace']) / 'main.py').exists())
            from simple_ar.core.artifacts import read_json
            manifest = read_json(Path(new_payload['execution']['code_task']['run_dir']) / 'manifest.json')
            self.assertEqual(manifest['code_task']['kind'], 'greenfield')
            from simple_ar.app.research_execution import implementation_request
            implementation_request(new_payload['execution'], client=object(), task_text='Create a program', validate=True)

    def test_code_task_venv_preparation_routes_checker_and_formal_command(self):
        import sys
        from dataclasses import replace
        from types import SimpleNamespace
        from simple_ar.app.research_application import ResearchApplicationServices, create_session, load_session
        from simple_ar.app.research_execution import execution_request, implementation_request
        from simple_ar.code_task.editing.patching import apply_patch_edits
        from simple_ar.code_task.orchestration.workflow import INITIAL_ADAPTER_SOURCE
        from simple_ar.code_task.runtime.state import load_code_task_manifest
        from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
        from simple_ar.experiment.execution.backend import LocalExecutionBackend
        from simple_ar.research.implementation import run_implementation_capability
        from simple_ar.research.workflow_contracts import ResearchBrief

        # Real stdlib venv and checker/formal processes; pip alone is mocked,
        # so this fixture neither contacts an index nor asserts real installs.
        class SetupBackend:
            def __init__(self, ledger, failure=None):
                self.local = LocalExecutionBackend(budget_ledger=ledger)
                self.requests, self.failure = [], failure
            def run(self, request):
                self.requests.append(request)
                argv = request.command
                if argv[1:3] == ['-m', 'venv']:
                    argv = [*argv, '--without-pip']
                elif argv[1:3] == ['-m', 'pip']:
                    argv = [argv[0], '-c', "print('mock pip fixture')"]
                result = self.local.run(replace(request, command=argv))
                if request.command[1:3] == ['-m', 'pip']:
                    if self.failure == 'changed':
                        (request.cwd / 'check.py').write_text('raise RuntimeError("changed")\n')
                    if self.failure == 'install':
                        result = replace(result, returncode=1)
                return replace(result, command=request.command)

        for failure in (None, 'install', 'changed'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                root, project = Path(folder), Path(folder) / 'author'
                project.mkdir()
                (project / 'author.py').write_text('def value():\n    return 7\n')
                (project / 'requirements.txt').write_text('# No dependencies in this fixture\n')
                (project / 'check.py').write_text(
                    "import sys\nfrom adapter import value\nassert value() == 7\n"
                    "assert sys.prefix != sys.base_prefix\nprint(sys.executable)\n")
                (project / 'formal.py').write_text(
                    "import json,sys\nfrom pathlib import Path\nfrom adapter import value\n"
                    "Path(sys.argv[1]).write_text(json.dumps({'value':value(),'python':sys.executable}))\n")
                original = {p.name: p.read_bytes() for p in project.iterdir()}
                execution = {'command': ['python', 'formal.py', '{output_dir}/raw.json'],
                    'cwd': str(project), 'timeout_sec': 10,
                    'protocol': {'hypothesis': 'Fixture only', 'dataset': 'Fixed fixture', 'expected_outcome': 'value=7'},
                    'environment': {'mode': 'venv', 'requirements': ['requirements.txt'], 'timeout_sec': 60},
                    'code_task': {'code_root': str(project), 'workspace_mode': 'copy', 'env_mode': 'current',
                        'allowed_patterns': ['adapter.py'], 'initial_files': ['adapter.py'],
                        'protected_patterns': ['check.py', 'formal.py'], 'approval_note': 'Fixture adapter only',
                        'validation_command': ['python', 'check.py'], 'validation_timeout_sec': 10}}
                paper = root / 'paper.md'
                paper.write_text('Constructed integration fixture, not a paper claim.')
                services = ResearchApplicationServices(config={'research_task_kind': 'reproduction',
                    'research_plan_mode': 'deterministic', 'research_materials_only': True,
                    'research_local_documents': [str(paper)], 'execution': execution},
                    budget_limits={'process_invocations': 5, 'process_wall_seconds': 200})
                app = create_session(ResearchBrief(request_text='Connect fixed fixture results',
                    requested_outputs=('experiments',)), root=root / 'session', services=services)
                backend = SetupBackend(app.budget_ledger, failure)
                with patch('simple_ar.app.research_application.LocalExecutionBackend', return_value=backend):
                    succeeded = app._run_action('prepare_execution')
                self.assertEqual(succeeded, failure is None, app.controller.manifest.status_reason)
                self.assertEqual(original, {p.name: p.read_bytes() for p in project.iterdir()})
                self.assertFalse((project / 'adapter.py').exists())
                if failure:
                    self.assertIsNone(app._active_preparation_ref())
                    self.assertFalse(list((root / 'session').rglob('execution.json')))
                    continue
                ref = app._active_preparation_ref()
                payload = app.controller.store.read_json(ref)
                prepared, workspace = payload['execution'], Path(payload['workspace'])
                self.assertEqual(payload['source_project'], str(project.resolve()))
                self.assertIn('copy_report', payload)
                self.assertIn('workspace_info', payload)
                self.assertIn('setup_ref', payload['environment'])
                self.assertTrue(all(r.cwd == workspace for r in backend.requests))
                self.assertEqual(app.budget_ledger.remaining('process_invocations'), 2)
                python, run_dir = prepared['command'][0], Path(prepared['code_task']['run_dir'])
                self.assertNotEqual(python, sys.executable)
                self.assertEqual(load_code_task_manifest(run_dir)['environment']['policy']['python_executable'], python)
                recovered = load_session(root / 'session', services=services)
                self.assertEqual(recovered._effective_config()['execution'], prepared)
                self.assertEqual(len(backend.requests), 3)  # Recovery performs no setup.
                impl = replace(implementation_request(prepared, object(), require_validation=True),
                    budget_ledger=app.budget_ledger)
                self.assertEqual(impl.validation_command, (python, 'check.py'))
                proposal = run_dir / 'code_task/meta/fixture_edits.json'
                proposal.write_text(json.dumps({'edits': [{'path': 'adapter.py', 'old': INITIAL_ADAPTER_SOURCE,
                    'new': 'from author import value\n'}]}))
                def implement(*args, **kwargs):
                    apply_patch_edits(run_dir, edits_file=proposal, allow_unapproved_plan=True)
                    return SimpleNamespace(stop_reason='stop_point', next_action='Validate', steps=())
                context = CapabilityContext(ArtifactStore(root / 'implementation'), AttemptManifest('implement-fixture'),
                    inputs=app._input_refs('brief', 'runtime_config'), input_store=app.controller.store)
                with patch('simple_ar.research.implementation.implement_code_task', side_effect=implement):
                    result = run_implementation_capability(context=context, request=impl)
                self.assertEqual(result.status, 'completed', result.diagnostics)
                formal = execution_request(prepared).run
                observed = backend.local.run(replace(formal, output_dir=root / 'measurement'))
                self.assertEqual(observed.status, 'passed', observed.stderr)
                measured = json.loads(next((root / 'measurement').rglob('raw.json')).read_text())
                self.assertEqual(measured, {'value': 7, 'python': python})
                self.assertEqual(app.budget_ledger.remaining('process_invocations'), 0)
                self.assertEqual(original, {p.name: p.read_bytes() for p in project.iterdir()})


if __name__ == '__main__':
    unittest.main()
