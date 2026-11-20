"""Result attachments cross the existing execution/writing boundary safely."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from simple_ar.core.capabilities import ArtifactRef, ArtifactStore, AttemptManifest, CapabilityContext
from simple_ar.experiment.execution.backend import RunRequest
from simple_ar.experiment.execution.outputs import (
    MAX_OUTPUT_BYTES, capture_outputs, output_files, read_output_window,
    metric_sources, extract_file_metrics,
)
from simple_ar.research.experiment import ExperimentRequest, run_experiment_capability
from simple_ar.report.execution_evidence import report_execution_evidence
from simple_ar.report.projection import _qualified_outputs, _output_handles, attach_experiment_history
from simple_ar.report.schema import ReportContext, ReportMemory, ReportRuntimeConfig, ReportToolCall
from simple_ar.report.templates import load_report_template_bundle
from simple_ar.report.tool_gateway import ReportToolGateway
from simple_ar.report.writing import ReportWritingRequest, run_report_writing_capability


class ExperimentOutputTests(unittest.TestCase):
    def test_file_metric_selectors_validate_before_execution(self):
        schema = {"output_files": {"raw": "raw.json", "table": "raw.csv"}}
        valid = {"coverage": {"output": "raw", "path": ["rows", 0, "coverage"]},
                 "error": {"output": "table", "column": "error", "match": {"method": "RCP"}}}
        self.assertEqual(metric_sources({**schema, "metric_sources": valid}), valid)
        self.assertEqual(metric_sources(schema), {})
        invalid = [[], {"output": "missing", "path": ["x"]},
                   {"output": "raw", "path": []}, {"output": "raw", "path": "coverage"},
                   {"output": "raw", "path": [True]}, {"output": "raw", "path": [-1]},
                   {"output": "raw", "path": ["x"], "column": "x"},
                   {"output": "table", "column": "error"},
                   {"output": "table", "column": "error", "match": {"x": None}},
                   {"output": "table", "column": "error", "match": {"x": float("nan")}}]
        with tempfile.TemporaryDirectory() as directory:
            context = CapabilityContext(ArtifactStore(Path(directory)), AttemptManifest("a"))
            for source in invalid:
                request = ExperimentRequest(RunRequest(["unused"], Path(directory), 5),
                    result_schema={**schema, "metric_sources": {"coverage": source}})
                with self.subTest(source=source), patch("simple_ar.research.experiment.run_experiment") as run:
                    with self.assertRaises(ValueError):
                        run_experiment_capability(context=context, request=request)
                    run.assert_not_called()

    def test_file_metric_extraction_is_explicit_finite_and_unambiguous(self):
        cases = [
            ("json", '{"rows":[{"coverage":0.9}]}', {"path": ["rows", 0, "coverage"]}, 0.9),
            ("csv", 'method,error\nother,2\nRCP,0.2\n', {"column": "error", "match": {"method": "RCP"}}, 0.2),
            ("tsv", 'seed\terror\n1\t0.3\n', {"column": "error", "match": {"seed": 1}}, 0.3),
            ("json", '{"x":true}', {"path": ["x"]}, None),
            ("json", '{"x":"0.2"}', {"path": ["x"]}, None),
            ("json", '{"x":NaN}', {"path": ["x"]}, None),
            ("json", '{"x":Infinity}', {"path": ["x"]}, None),
            ("json", '{"x":1e999}', {"path": ["x"]}, None),
            ("json", '{"x":1,"nested":{"a":1,"a":2}}', {"path": ["x"]}, None),
            ("json", '{"x":[]}', {"path": ["x", 0]}, None),
            ("json", '{"x":1}', {"path": ["absent"]}, None),
            ("csv", 'method,error\nRCP,1\nRCP,2\n', {"column": "error", "match": {"method": "RCP"}}, None),
            ("csv", 'method,error\nother,1\n', {"column": "error", "match": {"method": "RCP"}}, None),
            ("csv", 'error,error\n1,2\n', {"column": "error", "match": {}}, None),
            ("csv", 'error\nInf\n', {"column": "error", "match": {}}, None),
            ("csv", 'error\nNaN\n', {"column": "error", "match": {}}, None),
            ("csv", 'error\ntrue\n', {"column": "error", "match": {}}, None),
            ("csv", 'method,error\nRCP\n', {"column": "error", "match": {}}, None),
        ]
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            for suffix, text, selector, expected in cases:
                with self.subTest(text=text):
                    filename = f"raw.{suffix}"
                    (store.root / filename).write_text(text, encoding="utf-8")
                    schema = {"output_files": {"raw": filename},
                              "metric_sources": {"metric": {"output": "raw", **selector}}}
                    rows, _ = capture_outputs(store, store.root, output_files(schema))
                    metrics, locations, issues = extract_file_metrics(store, rows, metric_sources(schema), {})
                    if expected is None:
                        self.assertEqual(metrics, {})
                        self.assertEqual(issues[0]["severity"], "error")
                        self.assertEqual(locations["metric"]["status"], "failed")
                    else:
                        self.assertEqual(metrics, {"metric": expected})
                        self.assertEqual(issues, [])
                        self.assertEqual(locations["metric"]["artifact"], filename)
                        if suffix != "json":
                            self.assertGreaterEqual(locations["metric"]["data_row"], 1)
            sources = {"metric": {"output": "raw", "path": ["x"]}}
            (store.root / "raw.json").write_text('{"padding":"' + "x" * 3000 + '","x":0.7}')
            rows, _ = capture_outputs(store, store.root, {"raw": "raw.json"})
            self.assertTrue(rows[0]["preview"]["truncated"])
            self.assertEqual(extract_file_metrics(store, rows, sources, {})[0], {"metric": 0.7})
            self.assertEqual(extract_file_metrics(store, rows, sources, {"metric": 0.7})[2], [])
            self.assertEqual(extract_file_metrics(store, rows, sources, {"metric": 1})[0], {})
            self.assertTrue(extract_file_metrics(store, rows, sources, {"metric": 1})[2])
            for payload in (None, b"x" * (MAX_OUTPUT_BYTES + 1), b"\xff"):
                path = store.root / "raw.json"
                path.unlink(missing_ok=True)
                if payload is not None:
                    path.write_bytes(payload)
                rows, _ = capture_outputs(store, store.root, {"raw": "raw.json"})
                self.assertTrue(extract_file_metrics(store, rows, sources, {})[2])

    def test_native_file_metrics_reach_canonical_guard_without_stdout_or_failure_promotion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "raw.json").write_text('{"coverage":123}')  # Never read cwd/previous output.
            for label, stdout, exitcode, write, expected in (
                    ("native", "", 0, True, "completed"),
                    ("agreement", "coverage=0.9", 0, True, "completed"),
                    ("conflict", "coverage=0.1", 0, True, "failed"),
                    ("failed_process", "", 2, True, "failed"),
                    ("missing_file", "coverage=0.9", 0, False, "failed")):
                with self.subTest(label=label):
                    store = ArtifactStore(root / label)
                    script = ("import os,sys; from pathlib import Path; "
                              "p=Path(os.environ['SIMPLE_AR_OUTPUT_DIR']); "
                              "p.mkdir(parents=True,exist_ok=True); "
                              + ("(p/'raw.json').write_text('{\"coverage\":0.9}'); " if write else "")
                              + (f"print({stdout!r}); " if stdout else "")
                              + f"sys.exit({exitcode})")
                    request = ExperimentRequest(RunRequest([sys.executable, "-c", script], root, 5),
                        result_schema={"primary_metric": "coverage", "required_metrics": ["coverage"],
                            "output_files": {"raw": "raw.json"},
                            "metric_sources": {"coverage": {"output": "raw", "path": ["coverage"]}}})
                    result = run_experiment_capability(
                        context=CapabilityContext(store, AttemptManifest(label)), request=request)
                    self.assertEqual(result.status, expected, result.diagnostics)
                    canonical = store.read_json(next(ref for ref in result.artifacts if ref.kind == "experiment_result"))
                    self.assertEqual(result.usage["metric_count"], len(canonical["metrics"]))
                    self.assertEqual(canonical["measurement"]["metric_count"], len(canonical["metrics"]))
                    if expected == "completed":
                        self.assertEqual(canonical["metrics"], {"coverage": 0.9})
                        self.assertEqual(canonical["guard"]["status"], "passed")
                        self.assertEqual(canonical["guard"]["summary"]["metric_count"], 1)
                        self.assertIn("execution/process/", canonical["metric_sources"]["coverage"]["artifact"])
                        if label == "native":
                            self.assertEqual(canonical["execution"]["stdout_chars"], 0)
                    if label == "conflict":
                        self.assertEqual(canonical["metrics"]["coverage"], 0.1)
                        self.assertEqual(canonical["guard"]["status"], "failed")
                    if label == "failed_process":
                        self.assertEqual(canonical["metrics"], {"coverage": 0.9})
                        self.assertEqual(canonical["execution_status"], "failed")

    def test_small_json_object_overview_preserves_all_fields_including_late_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            payload = {'metrics': {f'metric_{i}': i / 100 for i in range(55)},
                       'runtime': {'python': '3.12', 'device': 'cpu'}, 'nullable': None}
            path = store.root / 'summary.json'
            path.write_text(json.dumps(payload, indent=6), encoding='utf-8')
            self.assertGreater(path.stat().st_size, 1200)
            rows, _ = capture_outputs(store, store.root, {'summary': path.name})
            view = rows[0]['preview']
            self.assertEqual(json.loads(view['text']), payload)
            self.assertFalse(view['truncated'])
            self.assertEqual(view['next_offset'], len(path.read_text()))
            self.assertEqual(view['view_kind'], 'complete_json_object_whitespace_compacted')
            self.assertEqual(read_output_window(store.root, path)['text'], path.read_text()[:2400])

    def test_overview_does_not_select_array_rows_or_collapse_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            path = store.root / 'raw.json'
            for text in (json.dumps([{'row': i} for i in range(200)], indent=2),
                         '{"metric":1,"metric":2}', '{"metric":NaN}'):
                path.write_text(text)
                rows, _ = capture_outputs(store, store.root, {'raw': path.name})
                self.assertEqual(rows[0]['preview']['text'], text[:1200])
                self.assertNotIn('view_kind', rows[0]['preview'])

    def test_large_object_overview_lists_late_fields_without_pretending_they_were_read(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            path = store.root / 'large.json'
            payload = {'long_text': 'x' * 12000, 'runtime': {'python': '3.12'},
                       **{f'field_{i}': i for i in range(80)}}
            path.write_text(json.dumps(payload))
            rows, _ = capture_outputs(store, store.root, {'large': path.name})
            view = rows[0]['preview']
            self.assertTrue(view['truncated'])
            self.assertIn('runtime', view['object_fields'])
            self.assertNotIn('3.12', view['text'])
            self.assertEqual(view['object_fields_omitted'], 18)
            self.assertEqual(view['structure_scope'], 'top_level_keys_only_not_field_contents')
            runtime = read_output_window(store.root, path, query='"runtime"')
            self.assertTrue(runtime['query_matched'])
            self.assertIn('3.12', runtime['text'])

    def test_contract_preserves_declared_files_and_rejects_escape(self):
        for value in ({"x": "../x"}, {"x": "/etc/passwd"}, {"x": "C:/x"},
                      {"x": "a\\x"}, {"x": "./x"}, {"x": "a//b"},
                      {"x": 1}, {"": "x"}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                output_files({"output_files": value})
        self.assertEqual(output_files({}), {})
        declared = {f"result_{i}": f"tables/result_{i}.csv" for i in range(12)}
        self.assertEqual(output_files({"output_files": declared}), declared)
        self.assertEqual(output_files({"output_files": {"raw": "nested/raw.json"}}), {"raw": "nested/raw.json"})

    def test_live_process_two_layouts_registers_only_declared_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename in ("raw.json", "tables/seed values.csv"):
                with self.subTest(filename=filename):
                    store = ArtifactStore(root / filename.replace("/", "-"))
                    script = ("import os; from pathlib import Path; "
                              "p=Path(os.environ['SIMPLE_AR_OUTPUT_DIR']); "
                              f"f=p/{filename!r}; f.parent.mkdir(parents=True, exist_ok=True); "
                              "f.write_text('seed,value\\n0,0.4\\n', encoding='utf-8'); "
                              "(p/'unrelated.txt').write_text('not requested'); print('METRIC accuracy=0.4')")
                    request = ExperimentRequest(run=RunRequest([sys.executable, "-c", script], root, 5),
                        result_schema={"output_files": {"observations": filename, "optional": "absent.json"}})
                    result = run_experiment_capability(context=CapabilityContext(store, AttemptManifest("experiment-1")), request=request)
                    self.assertEqual(result.status, "completed", result.diagnostics)
                    canonical = store.read_json(next(ref for ref in result.artifacts if ref.kind == "experiment_result"))
                    rows = canonical["output_evidence"]
                    self.assertEqual([row["status"] for row in rows], ["available", "missing"])
                    refs = [ref for ref in result.artifacts if ref.kind == "experiment_output"]
                    self.assertEqual(len(refs), 1)
                    self.assertEqual(refs[0].path, rows[0]["artifact"])
                    self.assertNotIn("unrelated", json.dumps(rows))
                    self.assertIn("0,0.4", rows[0]["preview"]["text"])

    def test_invalid_contract_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            request = ExperimentRequest(run=RunRequest([sys.executable, "-c", "raise Exception('ran')"], Path(directory), 5),
                result_schema={"output_files": {"raw": "../outside"}})
            with patch("simple_ar.research.experiment.run_experiment") as run, self.assertRaises(ValueError):
                run_experiment_capability(context=CapabilityContext(ArtifactStore(Path(directory)), AttemptManifest("a")), request=request)
            run.assert_not_called()

    def test_preview_windows_unicode_and_move_do_not_invent_complete_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ArtifactStore(root / "source")
            file = store.root / "outputs" / "raw.txt"
            file.parent.mkdir(parents=True)
            text = "µ测量🙂" * 900
            file.write_text(text, encoding="utf-8")
            rows, refs = capture_outputs(store, file.parent, {"raw": "raw.txt"})
            self.assertTrue(rows[0]["preview"]["truncated"])
            self.assertEqual(rows[0]["preview"]["total_characters"], len(text))
            shutil.copytree(store.root, root / "moved")
            moved = ArtifactStore(root / "moved")
            view = read_output_window(moved.root, moved.resolve(refs[0]), offset=1200, limit=2400)
            self.assertEqual(view["text"], text[1200:3600])
            self.assertFalse(view["has_more"])
            self.assertTrue(view["truncated"])

    def test_unreadable_and_oversize_files_keep_distinct_attachment_status(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            (store.root / "big").write_bytes(b"x" * (MAX_OUTPUT_BYTES + 1))
            (store.root / "binary").write_bytes(b"\xff\x00")
            (store.root / "folder").mkdir()
            rows, refs = capture_outputs(store, store.root, {name: name for name in ("big", "binary", "folder", "absent")})
            self.assertEqual([row["status"] for row in rows], ["unreadable"] * 3 + ["missing"])
            self.assertEqual(refs, [])

    def test_literal_search_finds_late_evidence_without_guessing_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "raw.json"
            text = "prefix\n" * 1800 + '\n{"seed": 9, "value": 0.42}\n' + "suffix\n" * 800
            file.write_text(text, encoding="utf-8")
            result = read_output_window(root, file, query='"seed": 9')
            self.assertTrue(result["query_matched"])
            self.assertIn('"value": 0.42', result["text"])
            self.assertGreater(result["offset"], 10000)
            self.assertEqual(result["text"], text[result["offset"]:result["next_offset"]])
            missing = read_output_window(root, file, query="no such phrase")
            self.assertFalse(missing["query_matched"])
            self.assertEqual(missing["text"], "")
            with self.assertRaises(ValueError):
                read_output_window(root, file, query="x" * 201)

    def test_producer_attribution_is_not_a_fabricated_paper_citation(self):
        from simple_ar.report.narrative import _prompt_handle_view
        handle = _output_handles([{"handle": "output:raw", "status": "available", "artifact": "raw.csv"}])[0]
        view = _prompt_handle_view(handle)
        self.assertIn("not a literature source", view["attribution_guidance"])
        self.assertNotIn("cite_as", view)

    def test_exact_record_selection_handles_json_csv_and_tsv_without_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [{"condition": i, "method": method, "value": i / 100}
                    for i in range(10) for method in ("A", "B")]
            for suffix in ("json", "csv", "tsv"):
                file = root / f"rows.{suffix}"
                if suffix == "json":
                    file.write_text(json.dumps(rows), encoding="utf-8")
                else:
                    import csv
                    with file.open("w", newline="", encoding="utf-8") as stream:
                        writer = csv.DictWriter(stream, fieldnames=rows[0], delimiter="\t" if suffix == "tsv" else ",")
                        writer.writeheader()
                        writer.writerows(rows)
                result = read_output_window(root, file, record_match={"condition": 9})
                selected = json.loads(result["text"])
                self.assertEqual(result["source_records"], 20)
                self.assertEqual(result["matched_records"], 2)
                self.assertEqual([row["method"] for row in selected], ["A", "B"])
                self.assertFalse(result["truncated"])
                self.assertEqual(read_output_window(root, file, record_match={"unknown": 9})["matched_records"], 0)
                with self.assertRaises(ValueError):
                    read_output_window(root, file, query="9", record_match={"condition": 9})

    def test_record_selection_retains_truncation_and_does_not_treat_boolean_as_number(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "raw.json"
            file.write_text(json.dumps([{"flag": True, "payload": "x" * 3000}, {"flag": 1}]))
            result = read_output_window(root, file, record_match={"flag": True})
            self.assertEqual(result["matched_records"], 1)
            self.assertTrue(result["truncated"])
            self.assertEqual(result["view_kind"], "selected_producer_records")
            file.write_text('{"summary": 1}')
            with self.assertRaises(ValueError):
                read_output_window(root, file, record_match={"summary": 1})

    def test_symlink_and_escape_do_not_read_external_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ArtifactStore(root / "store")
            store.root.mkdir()
            outside = root / "outside"
            outside.write_text("never expose")
            try:
                (store.root / "linked").symlink_to(outside)
                (store.root / "nested").symlink_to(root, target_is_directory=True)
            except OSError:
                self.skipTest("Symlinks unavailable")
            for file in (store.root / "linked", store.root / "nested" / "outside", store.root / ".." / "outside"):
                with self.subTest(file=file), self.assertRaises(ValueError):
                    read_output_window(store.root, file)
            if hasattr(os, "mkfifo"):
                os.mkfifo(store.root / "fifo")
                with self.assertRaises(ValueError):
                    read_output_window(store.root, store.root / "fifo")

    def test_projection_keeps_current_and_history_measurements_distinct(self):
        result = {"output_evidence": [{"name": "raw", "status": "available", "artifact": "outputs/raw.json",
                                       "preview": {"text": "producer", "truncated": False}}]}
        result["output_evidence"].extend({"name": f"observation-{index}", "status": "available",
            "artifact": f"outputs/observation-{index}.csv", "preview": {"text": "value\n0.2", "truncated": False}}
            for index in range(11))
        ref = ArtifactRef("attempts/experiment-2/results.json")
        qualified = _qualified_outputs(result, ref)
        self.assertEqual(len(qualified), 12)
        context = ReportContext(topic="Measurements", report_mode="experiment", results={"output_evidence": qualified})
        memory = ReportMemory()
        before = json.dumps(result)
        context, memory = attach_experiment_history(context, memory,
            [("initial", ArtifactRef("attempts/experiment-1/results.json"), result), ("repair", ref, result)], current_ref=ref)
        view = report_execution_evidence(context)
        self.assertEqual(len(view["output_evidence"]), 24)
        self.assertEqual(view["output_evidence"][0]["artifact"], "attempts/experiment-2/outputs/raw.json")
        self.assertEqual(json.dumps(result), before)
        self.assertTrue(all(handle.kind == "experiment_output" for handle in _output_handles(qualified)))

    def test_saved_measurement_preview_is_refreshed_read_only_for_current_report(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            ref = ArtifactRef('attempts/run/results.json')
            output = store.resolve('attempts/run/outputs/summary.json')
            output.parent.mkdir(parents=True)
            output.write_text(json.dumps({'metrics': [0.1] * 100, 'runtime': {'python': '3.12'}}, indent=4))
            result = {'output_evidence': [{'name': 'summary', 'status': 'available',
                'artifact': 'outputs/summary.json', 'preview': {'text': 'old truncated', 'truncated': True}}]}
            original = json.dumps(result)
            context = ReportContext(topic='Reproduction', report_mode='experiment',
                results={'output_evidence': _qualified_outputs(result, ref)})
            context, _ = attach_experiment_history(context, ReportMemory(), [('current', ref, result)],
                current_ref=ref, output_store=store)
            evidence = report_execution_evidence(context)['output_evidence']
            self.assertEqual(len(evidence), 1)
            self.assertIn('3.12', evidence[0]['preview']['text'])
            self.assertFalse(evidence[0]['preview']['truncated'])
            self.assertEqual(json.dumps(result), original)

    def test_gateway_does_not_guess_paths_or_read_without_registration(self):
        context = ReportContext(topic="Measurements", report_mode="experiment", source_handles=_output_handles([{"handle": "output:registered", "status": "available", "artifact": "raw.json"}]))
        gateway = ReportToolGateway(context)
        for handle in ("output:registered", "raw.json", "../../outside"):
            result = gateway.call(ReportToolCall(tool_name="get_code_task_result", arguments={"output_handle": handle}))
            self.assertEqual(result.status, "not_found")
        result = gateway.call(ReportToolCall(tool_name="get_code_task_result", arguments={"output_handle": "output:registered", "limit": 5000}))
        self.assertEqual(result.status, "error")

    def test_registered_gateway_selects_rows_and_reports_missing_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "raw.json"
            file.write_text(json.dumps([{"condition": 7, "value": 0.42}]))
            context = ReportContext(topic="Measurements", report_mode="experiment",
                source_handles=_output_handles([{"handle": "output:raw", "status": "available", "artifact": "raw.json"}]))
            def read(path, offset, limit, query, record_match):
                return read_output_window(root, root / path, offset=offset, limit=limit,
                                          query=query, record_match=record_match)
            gateway = ReportToolGateway(context, output_reader=read)
            for condition, expected in ((7, "ok"), (8, "not_found")):
                result = gateway.call(ReportToolCall(tool_name="get_code_task_result", arguments={
                    "output_handle": "output:raw", "record_match": {"condition": condition}}))
                self.assertEqual(result.status, expected)
                self.assertEqual(result.content["matched_records"], int(condition == 7))
                self.assertEqual(result.content["search_scope"], "registered_record_array")

    def test_writing_reader_uses_registered_inputs_and_recovers_after_move(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = ArtifactStore(root / "session")
            raw = source.write_text("attempts/experiment-1/outputs/raw.json", "measured facts" * 300,
                                    kind="experiment_output", schema="text.v1")
            context = ReportContext(topic="Measurements", report_mode="experiment", source_handles=_output_handles([{"handle": "output:raw", "status": "available", "artifact": raw.path}]))
            config = ReportRuntimeConfig()
            request = ReportWritingRequest(context, ReportMemory(), config,
                load_report_template_bundle(report_mode="experiment", config=config), object())
            shutil.copytree(source.root, root / "moved")
            for location in (source.root, root / "moved"):
                store = ArtifactStore(location)
                capability = CapabilityContext(ArtifactStore(location / "writer"), AttemptManifest("w"), inputs=(raw,), input_store=store)
                def inspect(**kwargs):
                    result = kwargs["gateway"].call(ReportToolCall(tool_name="get_code_task_result",
                        arguments={"output_handle": "output:raw", "offset": 1200, "limit": 1200}))
                    self.assertEqual(result.status, "ok", result.summary)
                    self.assertEqual(result.content["text"], ("measured facts" * 300)[1200:2400])
                    return None
                with patch("simple_ar.report.writing.run_report_agent", side_effect=inspect):
                    run_report_writing_capability(context=capability, request=request)
                with patch("simple_ar.report.writing.run_report_agent", return_value=None) as writer:
                    run_report_writing_capability(context=replace(capability, inputs=()), request=request)
                    result = writer.call_args.kwargs["gateway"].call(ReportToolCall(tool_name="get_code_task_result",
                        arguments={"output_handle": "output:raw"}))
                    self.assertEqual(result.status, "error")

if __name__ == "__main__":
    unittest.main()
