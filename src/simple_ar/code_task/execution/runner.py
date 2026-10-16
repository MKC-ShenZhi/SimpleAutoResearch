from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from simple_ar.core.artifacts import read_json, write_json, write_text
from simple_ar.core.process import ProcessResult, ProcessSpec, run_process, with_process_outputs
from simple_ar.core.budget import BudgetLedger
from simple_ar.code_task.editing.attempts import (
    load_latest_code_task_batch,
    update_code_task_batch_state,
)
from simple_ar.code_task.execution.comparison import CodeTaskComparisonResult, compare_code_task_runs
from simple_ar.code_task.execution.environment import (
    apply_code_task_environment_policy,
    ensure_code_task_environment_policy,
    policy_python_executable,
)
from simple_ar.code_task.execution.artifact_contract import (
    compact_artifact_scan,
    expected_artifact_row,
    has_artifact_path_mismatch,
    scan_required_artifacts,
)
from simple_ar.code_task.execution.run_history import archive_run_attempt
from simple_ar.code_task.runtime.state import (
    code_task_paths,
    load_code_task_manifest,
    manifest_section,
    native_workspace_binding,
    native_preparation_processes,
    save_code_task_manifest,
    utcnow_iso,
)
from simple_ar.code_task.execution.summary import write_code_task_summary
from simple_ar.code_task.execution.validation import validate_code_task
from simple_ar.experiment.metrics import parse_metric_lines


CONTROL_TOKENS = {"|", "||", "&&", ";", "<", ">", ">>", "2>", "2>>"}
OUTPUT_CHAR_LIMIT = 200_000
WATCHDOG_OUTPUT_CHAR_LIMIT = 1_000_000
WATCHDOG_WARNING_LINE_LIMIT = 300
WATCHDOG_REPEATED_LINE_LIMIT = 160
WATCHDOG_MIN_ELAPSED_SEC = 1.0
WATCHDOG_WARNING_TOKENS = (
    "warning",
    "convergencewarning",
    "runtimewarning",
    "total no. of iterations",
    "failed to converge",
    "maximum iterations",
)
OutputCallback = Callable[[str, str], None]
StreamOutputMode = str
STREAM_OUTPUT_ALIASES = {
    "0": "off",
    "1": "auto",
    "false": "off",
    "no": "off",
    "off": "off",
    "none": "off",
    "true": "auto",
    "yes": "auto",
    "on": "auto",
    "line": "line",
    "lines": "line",
    "auto": "auto",
    "tqdm": "auto",
    "progress": "auto",
    "summary": "summary",
    "tail": "summary",
}
PROGRESS_RELAY_MIN_INTERVAL_SEC = 0.5
SUMMARY_TAIL_LINES = 12


class CodeTaskRunError(RuntimeError):
    """Raised when a code-task benchmark cannot be launched safely."""


VALID_RUN_LABELS = {"baseline", "patched"}


class _OutputWatchdog:
    """Detect obvious output floods while preserving normal long runs."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.total_chars = 0
        self.line_count = 0
        self.warning_line_count = 0
        self.repeated_lines: dict[str, int] = {}
        self.samples: list[str] = []
        self.triggered: dict[str, Any] | None = None

    def observe(self, stream_name: str, text: str) -> None:
        if self.triggered is not None:
            return
        display = _display_stream_text(text)
        if not display:
            return
        self.total_chars += len(text)
        self.line_count += 1
        if len(self.samples) < 12:
            self.samples.append(f"{stream_name}: {display[:240]}")
        normalized = _normalize_watchdog_line(display)
        if normalized:
            if len(self.repeated_lines) >= 2048 and normalized not in self.repeated_lines:
                self.repeated_lines.pop(next(iter(self.repeated_lines)))
            self.repeated_lines[normalized] = self.repeated_lines.get(normalized, 0) + 1
        lowered = display.lower()
        if any(token in lowered for token in WATCHDOG_WARNING_TOKENS):
            self.warning_line_count += 1
        elapsed = time.monotonic() - self.started
        if elapsed < WATCHDOG_MIN_ELAPSED_SEC:
            return
        if self.warning_line_count >= WATCHDOG_WARNING_LINE_LIMIT:
            self._trigger(
                "warning_flood",
                stream_name=stream_name,
                elapsed_sec=elapsed,
                detail=f"{self.warning_line_count} warning-like lines observed.",
            )
        elif normalized and self.repeated_lines.get(normalized, 0) >= WATCHDOG_REPEATED_LINE_LIMIT:
            self._trigger(
                "repeated_output_flood",
                stream_name=stream_name,
                elapsed_sec=elapsed,
                detail=f"Line repeated {self.repeated_lines[normalized]} times.",
            )
        elif self.total_chars >= WATCHDOG_OUTPUT_CHAR_LIMIT:
            self._trigger(
                "output_volume_limit",
                stream_name=stream_name,
                elapsed_sec=elapsed,
                detail=f"Captured output exceeded {WATCHDOG_OUTPUT_CHAR_LIMIT} characters.",
            )

    def _trigger(self, reason: str, *, stream_name: str, elapsed_sec: float, detail: str) -> None:
        self.triggered = {
            "schema_version": "code_task_runtime_watchdog.v1",
            "reason": reason,
            "stream": stream_name,
            "elapsed_sec": round(elapsed_sec, 3),
            "line_count": self.line_count,
            "warning_line_count": self.warning_line_count,
            "total_chars": self.total_chars,
            "detail": detail,
            "sample_lines": self.samples[-12:],
        }


def _normalize_watchdog_line(text: str) -> str:
    text = " ".join(text.strip().split())
    if not text:
        return ""
    # Keep the semantic shape while ignoring counters and timings.
    text = re.sub(r"\d+(?:\.\d+)?", "<num>", text.lower())
    return text[:240]


@dataclass(frozen=True)
class CodeTaskRunResult:
    """Captured result from running a code-task benchmark.

    Args:
        run_dir: Code-task run directory.
        label: Execution label, usually ``baseline`` or ``patched``.
        report_path: Structured execution report.
        stdout_path: Captured stdout path.
        stderr_path: Captured stderr path.
        metrics_path: Parsed metrics path.
        status: ``passed``, ``failed``, ``timed_out``, or
            ``blocked_by_validation``.
        returncode: Subprocess return code, or ``None`` for timeout/blocking.
        timed_out: Whether execution timed out.
        metrics: Parsed ``name: value`` metric lines from stdout.
    """

    run_dir: Path
    label: str
    report_path: Path
    stdout_path: Path
    stderr_path: Path
    metrics_path: Path
    status: str
    returncode: int | None
    timed_out: bool
    metrics: dict[str, float] = field(default_factory=dict)


def run_code_task_benchmark(
    run_dir: Path,
    *,
    command: str | None = None,
    timeout_sec: int = 60,
    skip_validation: bool = False,
    run_label: str = "patched",
    env_mode: str | None = None,
    python_executable: str | Path | None = None,
    stream_output: bool | str = False,
    output_callback: OutputCallback | None = None,
    budget_ledger: BudgetLedger | None = None,
    session_id: str = "",
    attempt_id: str = "",
) -> CodeTaskRunResult:
    """Run the recorded benchmark command inside the copied workspace.

    Args:
        run_dir: Code-task run directory.
        command: Optional command override. When omitted, the command recorded
            during ``code-task init`` is used.
        timeout_sec: Maximum runtime in seconds.
        skip_validation: Run even when static validation has not passed.
        run_label: Result slot under ``code_task/run``. Use ``baseline`` before
            patching and ``patched`` after edits have been applied.
        env_mode: Optional execution environment mode override.
        python_executable: External interpreter path or executable name when
            ``env_mode`` is ``external``.
        stream_output: Whether and how to relay subprocess output while it
            runs. ``True`` is treated as ``"auto"`` for progress-bar friendly
            output; supported string modes are ``off``, ``line``, ``auto``,
            and ``summary``.
        output_callback: Callback receiving ``("stdout"|"stderr", line)`` when
            streaming is enabled.

    Returns:
        Execution artifacts and status.

    Raises:
        CodeTaskRunError: If the command is missing, unsafe, or timeout is
            invalid.
    """
    if timeout_sec < 1:
        raise CodeTaskRunError("timeout_sec must be at least 1")
    label = _normalize_run_label(run_label)

    manifest = load_code_task_manifest(run_dir)
    paths = code_task_paths(run_dir)
    if not paths.workspace_dir.is_dir():
        raise FileNotFoundError(f"Missing code-task workspace: {paths.workspace_dir}")
    paths.run_artifact_dir.mkdir(parents=True, exist_ok=True)

    command_text = command or _benchmark_command(manifest)
    if not command_text:
        raise CodeTaskRunError(
            "No benchmark command recorded. Pass --command or rerun code-task init "
            "with --benchmark-command."
        )
    environment_policy = ensure_code_task_environment_policy(
        run_dir,
        manifest,
        env_mode=env_mode,
        python_executable=python_executable,
    )
    command_args = _split_command(command_text, environment_policy=environment_policy)

    if not skip_validation:
        validation = validate_code_task(run_dir)
        if validation.error_count:
            return _write_blocked_result(
                run_dir,
                command_text=command_text,
                command_args=command_args,
                environment_policy=environment_policy,
                timeout_sec=timeout_sec,
                reason="Static validation reported errors.",
                run_label=label,
            )
        manifest = load_code_task_manifest(run_dir)

    started = time.monotonic()
    watchdog = _OutputWatchdog()
    mode = normalize_stream_output_mode(stream_output)
    binding = manifest.get('code_task', {}).get('native_binding')
    recovered = recover_implementation_process(run_dir, budget_ledger=budget_ledger,
        session_id=session_id, attempt_id=attempt_id) if binding is not None else None
    if binding is not None:
        from simple_ar.code_task.execution.environment import native_validation_command
        expected = _split_command(
            _argv_command(native_validation_command(manifest, paths.workspace_dir)), environment_policy=environment_policy)
        if (command_args != expected or timeout_sec != binding['binding']['contract']['timeout_sec']
                or environment_policy.get('python_executable') != binding['binding']['python_executable']):
            raise CodeTaskRunError('Validator command, interpreter or timeout differs from the original approval')
        from simple_ar.code_task.runtime.state import implementation_file_hashes, required_new_file_issues
        missing = required_new_file_issues(manifest, paths.workspace_dir)
        if missing and recovered is None:
            return _write_blocked_result(run_dir, command_text=command_text, command_args=command_args,
                environment_policy=environment_policy, timeout_sec=timeout_sec, run_label=label,
                reason='Create required nonempty project files before validation: ' + ', '.join(missing),
                report_metadata={'native_binding': binding,
                    'preflight': {'reason': 'required_new_files', 'missing_files': missing},
                    'implementation_file_hashes': implementation_file_hashes(run_dir)})
    process = recovered or _run_command(
        command_args,
        cwd=paths.workspace_dir,
        timeout_sec=timeout_sec,
        stream_output=mode,
        output_callback=output_callback,
        output_dir=paths.run_artifact_dir / label / "process",
        budget_ledger=budget_ledger, session_id=session_id, attempt_id=attempt_id,
        **({'environment': native_workspace_binding(manifest, paths.workspace_dir)['environment'],
            'input_binding': {'native_binding': binding,
                'implementation_file_hashes': implementation_file_hashes(run_dir),
                **({'required_new_file_issues': required_new_file_issues(manifest, paths.workspace_dir)}
                    if binding['binding']['contract'].get('new_files') else {})}}
            if binding is not None else {}),
        watchdog=watchdog,
    )
    if recovered is not None:
        watchdog.triggered = recovered.record['input_binding']['runtime_watchdog']
    stderr_text = process.stderr
    timed_out = process.stop_reason == "timeout"
    returncode = None if timed_out else process.returncode
    status = "timed_out" if timed_out else "passed" if returncode == 0 and not process.stop_reason else "failed"
    report_metadata: dict[str, Any] = {"process": process.record}
    if binding is not None:
        from simple_ar.code_task.runtime.state import implementation_file_hashes
        report_metadata['native_binding'] = binding
        report_metadata['implementation_file_hashes'] = implementation_file_hashes(run_dir)
    run_metadata: dict[str, Any] = {}
    if timed_out:
        if stderr_text:
            stderr_text += "\n"
        stderr_text += f"Timed out after {timeout_sec} seconds."
    else:
        if watchdog.triggered is not None:
            returncode = 124
            status = "failed"
            detail = watchdog.triggered["detail"]
            stderr_text = stderr_text.rstrip() + f"\nRuntime output watchdog aborted benchmark: {detail}\n"
            report_metadata["runtime_watchdog"] = watchdog.triggered
            run_metadata["runtime_watchdog"] = watchdog.triggered
        if mode == "summary" and output_callback is not None:
            _emit_output_summary(process.stdout, stderr_text, output_callback)
    duration_sec = round(process.duration_sec if binding is not None else time.monotonic() - started, 3)
    stdout, stdout_truncated = _clip_output(process.stdout)
    stderr, stderr_truncated = _clip_output(stderr_text)
    metrics = parse_metric_lines(stdout)
    stdout_truncated |= process.record["streams"]["stdout"]["tail_bytes_discarded"] > 0
    stderr_truncated |= process.record["streams"]["stderr"]["tail_bytes_discarded"] > 0
    if not timed_out:
        artifact_scan: dict[str, Any] | None = None
        if (_is_greenfield_manifest(manifest)
                and not (binding and binding['binding'].get('measurement_contract') is not None)):
            artifact_scan = scan_required_artifacts(run_dir, manifest)
            artifact_scan_path = _write_runtime_artifact_scan(run_dir, label, artifact_scan)
            artifact_scan_rel = _relative_to_run(run_dir, artifact_scan_path)
            artifact_scan_summary = compact_artifact_scan(artifact_scan)
            report_metadata.update(
                {
                    "artifact_scan": artifact_scan_rel,
                    "artifact_scan_summary": artifact_scan_summary,
                }
            )
            run_metadata.update(
                {
                    "artifact_scan": artifact_scan_rel,
                    "artifact_scan_summary": artifact_scan_summary,
                }
            )
        quality_guard = _greenfield_result_quality_guard(
            run_dir,
            manifest,
            status=status,
            metrics=metrics,
            artifact_scan=artifact_scan,
            required_file_issues=(process.record.get('input_binding') or {}).get('required_new_file_issues'),
        )
        if quality_guard is not None:
            status = "failed"
            guard_message = str(quality_guard.get("message") or "Generated benchmark quality guard failed.")
            stderr_prefix = stderr.rstrip()
            stderr_text = f"{stderr_prefix}\n{guard_message}\n" if stderr_prefix else guard_message + "\n"
            stderr, stderr_truncated = _clip_output(stderr_text)
            report_metadata["quality_guard"] = quality_guard
            run_metadata["quality_guard"] = quality_guard
    return _write_execution_result(
        run_dir,
        manifest=manifest,
        command_text=command_text,
        command_args=command_args,
        environment_policy=environment_policy,
        timeout_sec=timeout_sec,
        duration_sec=duration_sec,
        status=status,
        returncode=returncode,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
        metrics=metrics,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        run_label=label,
        report_metadata=report_metadata,
        run_metadata=run_metadata or None,
    )


def run_code_task_baseline(
    run_dir: Path,
    *,
    command: str | None = None,
    timeout_sec: int = 60,
    skip_validation: bool = False,
    env_mode: str | None = None,
    python_executable: str | Path | None = None,
    stream_output: bool | str = False,
    output_callback: OutputCallback | None = None,
    budget_ledger: BudgetLedger | None = None,
    session_id: str = "",
    attempt_id: str = "",
) -> CodeTaskRunResult:
    """Run the recorded benchmark as the pre-patch baseline.

    Args:
        run_dir: Code-task run directory.
        command: Optional command override.
        timeout_sec: Maximum runtime in seconds.
        skip_validation: Run even when static validation has not passed.
        env_mode: Optional execution environment mode override.
        python_executable: External interpreter path or executable name when
            ``env_mode`` is ``external``.
        stream_output: Whether and how to relay subprocess output while it
            runs. ``True`` is treated as ``"auto"``.
        output_callback: Callback receiving ``("stdout"|"stderr", line)`` when
            streaming is enabled.

    Returns:
        Baseline execution artifacts and status.
    """
    return run_code_task_benchmark(
        run_dir,
        command=command,
        timeout_sec=timeout_sec,
        skip_validation=skip_validation,
        run_label="baseline",
        budget_ledger=budget_ledger, session_id=session_id, attempt_id=attempt_id,
        env_mode=env_mode,
        python_executable=python_executable,
        stream_output=stream_output,
        output_callback=output_callback,
    )


def record_provided_code_task_baseline(
    run_dir: Path,
    *,
    metrics: dict[str, float],
    source_path: str,
    env_mode: str | None = None,
    python_executable: str | Path | None = None,
) -> CodeTaskRunResult:
    """Record user-provided baseline metrics without running the benchmark.

    This is for expensive existing-project tasks where the unchanged baseline
    has already been measured elsewhere. The artifact is deliberately explicit:
    reports and manifests mark the baseline as provided so downstream summaries
    do not confuse it with a fresh subprocess run.
    """

    numeric_metrics = {
        str(name): float(value)
        for name, value in metrics.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    if not numeric_metrics:
        raise CodeTaskRunError("Provided baseline metrics must include at least one numeric metric.")

    manifest = load_code_task_manifest(run_dir)
    environment_policy = ensure_code_task_environment_policy(
        run_dir,
        manifest,
        env_mode=env_mode,
        python_executable=python_executable,
    )
    return _write_execution_result(
        run_dir,
        manifest=manifest,
        command_text="provided-baseline-metrics",
        command_args=[],
        environment_policy=environment_policy,
        timeout_sec=0,
        duration_sec=0.0,
        status="passed",
        returncode=None,
        timed_out=False,
        stdout=f"Baseline metrics were provided by the user from {source_path}.\n",
        stderr="",
        metrics=numeric_metrics,
        stdout_truncated=False,
        stderr_truncated=False,
        run_label="baseline",
        report_metadata={
            "provided_baseline": True,
            "provided_source": source_path,
        },
        run_metadata={
            "provided": True,
            "provided_source": source_path,
            "baseline_policy": "provided",
        },
    )


def _argv_command(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv) if os.name == 'nt' else shlex.join(argv)


def native_validation_process(
    run_dir: Path, *, budget_ledger: BudgetLedger, session_id: str, attempt_id: str,
    invocation_id: str, file_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Verify one physical validator for recovery and downstream provenance.

    No process, reconciliation, ledger settlement or report mutation occurs.
    The implementation owner may pin its validated source inventory while
    allowing subsequently produced non-source artifacts.
    """
    from simple_ar.code_task.runtime.state import implementation_file_hashes
    paths = code_task_paths(run_dir)
    manifest = load_code_task_manifest(run_dir)
    binding = manifest.get('code_task', {}).get('native_binding')
    if binding is None or not re.fullmatch(r'[0-9a-f]{32}', invocation_id):
        raise CodeTaskRunError('Native validation needs an original binding and physical invocation identity')
    invocation = paths.run_artifact_dir / 'patched' / 'process' / invocation_id / 'invocation.json'
    if (not invocation.resolve().is_relative_to((paths.run_artifact_dir / 'patched' / 'process').resolve())
            or not invocation.is_file()):
        raise CodeTaskRunError('Original validator invocation is missing or outside its owner')
    record = read_json(invocation)
    expected_env = {**native_workspace_binding(manifest, paths.workspace_dir)['environment'],
        'SIMPLE_AR_OUTPUT_DIR': str(invocation.parent.absolute() / 'outputs')}
    contract = binding['binding']['contract']
    from simple_ar.core.process import verify_process_accounting
    try:
        verify_process_accounting(record, budget_ledger, session_id=session_id or record.get('session_id'),
            attempt_id=attempt_id, timeout_sec=contract['timeout_sec'])
    except (KeyError, TypeError, ValueError) as exc:
        raise CodeTaskRunError('Original validator physical accounting cannot be reconciled') from exc
    from simple_ar.code_task.execution.environment import native_validation_command
    expected_argv = _split_command(_argv_command(native_validation_command(manifest, paths.workspace_dir)),
        environment_policy=manifest['environment']['policy'])
    expected_argv = [arg.replace('{output_dir}', expected_env['SIMPLE_AR_OUTPUT_DIR']) for arg in expected_argv]
    original = record.get('input_binding', {})
    expected_files = implementation_file_hashes(run_dir) if file_hashes is None else file_hashes
    if (record.get('invocation_id') != invocation_id
            or record.get('argv') != expected_argv or record.get('cwd') != str(paths.workspace_dir.resolve())
            or record.get('environment_policy') != 'caller_supplied' or record.get('environment') != expected_env
            or record.get('timeout_sec') != contract['timeout_sec']
            or manifest['environment']['policy'].get('python_executable') != binding['binding']['python_executable']
            or original.get('native_binding') != binding or 'runtime_watchdog' not in original
            or contract.get('new_files') and original.get('required_new_file_issues') != []
            or original.get('implementation_file_hashes') != expected_files
            or not isinstance(record.get('output'), dict)):
        raise CodeTaskRunError('Original validator argv/cwd/environment/source/ledger binding cannot be reconciled')
    return record


def recover_implementation_process(
    run_dir: Path, *, budget_ledger: BudgetLedger | None, session_id: str, attempt_id: str,
) -> ProcessResult | None:
    """Read one original finalized validator; never execute or settle new work."""
    paths = code_task_paths(run_dir)
    manifest = load_code_task_manifest(run_dir)
    binding = manifest.get('code_task', {}).get('native_binding')
    if binding is None:
        return None
    if budget_ledger is None or not attempt_id:
        raise CodeTaskRunError('Bound validation recovery requires its original attempt and ledger')
    try:
        preparation_ids = native_preparation_processes(run_dir, ledger=budget_ledger,
            session_id=session_id, attempt_id=attempt_id)
    except (OSError, KeyError, IndexError, TypeError, ValueError, RuntimeError) as exc:
        raise CodeTaskRunError('Original preparation evidence cannot be reconciled; no replay') from exc
    entries = [entry for entry in budget_ledger.entries
        if entry.attempt_id == attempt_id and entry.purpose == 'local_process' and entry.status != 'released'
        and entry.logical_call_id not in preparation_ids]
    invocations = list((paths.run_artifact_dir / 'patched' / 'process').glob('*/invocation.json'))
    owned_ids = {row.logical_call_id for row in budget_ledger.entries
        if row.attempt_id == attempt_id and row.purpose == 'local_process'
        and row.logical_call_id not in preparation_ids}
    if any(path.parent.name not in owned_ids for path in invocations):
        raise CodeTaskRunError('Validator invocation has no matching original ledger owner; no replay')
    if not entries:
        if invocations:
            raise CodeTaskRunError('Validator invocation lacks settled original consumption; no replay')
        return None
    latest = paths.run_artifact_dir / 'patched' / 'execution_report.json'
    entry = entries[-1]
    if any(row.status != 'settled' or (session_id and row.session_id != session_id) for row in entries):
        raise CodeTaskRunError('Validator invocation is pending or unknown; no replay')
    reports = [latest, *(paths.run_artifact_dir / 'patched' / 'attempts').glob('*/execution_report.json')]
    projected = {read_json(path).get('process', {}).get('invocation_id') for path in reports if path.is_file()}
    if any(row.logical_call_id not in projected for row in entries[:-1]):
        raise CodeTaskRunError('Multiple unprojected validator invocations require inspection; no replay')
    if latest.is_file():
        report = read_json(latest)
        if report.get('process', {}).get('invocation_id') == entry.logical_call_id:
            if (binding['binding']['contract'].get('new_files')
                    and report['process'].get('input_binding', {}).get('required_new_file_issues') != []):
                raise CodeTaskRunError('Original validator lacks a passed creation preflight; no replay')
            return None  # Verification consumes this already projected report.
        if (report.get('status') == 'blocked_by_validation' and report.get('native_binding') == binding
                and report.get('preflight', {}).get('reason') == 'required_new_files'
                and entry.logical_call_id in projected):
            return None  # A newer source preflight is not an unprojected old process.
    record = native_validation_process(run_dir, budget_ledger=budget_ledger,
        session_id=session_id, attempt_id=attempt_id, invocation_id=entry.logical_call_id)
    invocation = paths.run_artifact_dir / 'patched' / 'process' / entry.logical_call_id / 'invocation.json'
    from simple_ar.core.process import process_observation
    observed = process_observation(record, invocation.parent)
    return ProcessResult(record['returncode'], record.get('stop_reason'), observed['stdout'],
        observed['stderr'], record['duration_sec'], record)


def _run_command(
    command_args: list[str],
    *,
    cwd: Path,
    timeout_sec: int,
    stream_output: bool | str,
    output_callback: OutputCallback | None,
    output_dir: Path | None = None,
    budget_ledger: BudgetLedger | None = None,
    session_id: str = "",
    attempt_id: str = "",
    watchdog: _OutputWatchdog | None = None,
    environment: dict[str, str] | None = None,
    input_binding: dict[str, Any] | None = None,
) -> ProcessResult:
    mode = normalize_stream_output_mode(stream_output)
    if watchdog is None:
        watchdog = _OutputWatchdog()
    cancel = threading.Event()
    callback = output_callback if mode not in {"off", "summary"} else None
    relays = {
        name: _OutputRelay(name, callback, mode, watchdog, cancel)
        for name in ("stdout", "stderr")
    }
    spec = ProcessSpec(command_args, cwd, timeout_sec, env=environment if environment is not None else _safe_env(cwd), output_dir=output_dir,
                       session_id=session_id, attempt_id=attempt_id, input_binding=input_binding)
    spec = with_process_outputs(spec)

    def finish_output() -> None:
        for relay in relays.values():
            relay.finish()
        if input_binding is not None:
            input_binding['runtime_watchdog'] = watchdog.triggered

    result = run_process(
        spec,
        output_callback=lambda name, chunk: relays[name].feed(chunk),
        cancel=cancel,
        budget_ledger=budget_ledger,
        **({'output_complete_callback': finish_output} if input_binding is not None else {}),
    )
    if input_binding is None:
        finish_output()
    return result


def normalize_stream_output_mode(value: bool | str) -> StreamOutputMode:
    """Normalize benchmark output relay mode.

    ``True`` maps to ``auto`` so existing configs keep working while gaining
    carriage-return progress support for tools such as tqdm.
    """

    if isinstance(value, bool):
        return "auto" if value else "off"
    text = str(value).strip().lower()
    if text in STREAM_OUTPUT_ALIASES:
        return STREAM_OUTPUT_ALIASES[text]
    raise ValueError(
        "stream_output must be one of: false, true, off, line, auto, summary"
    )


class _OutputRelay:
    """Keep CodeTask's line/progress presentation separate from process control."""

    def __init__(self, name, callback, mode, watchdog, cancel) -> None:
        self.name, self.callback, self.mode = name, callback, mode
        self.watchdog, self.cancel = watchdog, cancel
        self.buffer: list[str] = []
        self.pending = ""
        self.last_progress = ""
        self.last_progress_time = 0.0

    def emit(self, text: str, *, progress: bool = False, force: bool = False) -> bool:
        display = _display_stream_text(text)
        if self.callback is None or not display:
            return False
        if progress:
            now = time.monotonic()
            if not force and (
                display == self.last_progress
                or now - self.last_progress_time < PROGRESS_RELAY_MIN_INTERVAL_SEC
            ):
                return False
            self.last_progress, self.last_progress_time = display, now
        self.callback(self.name, display)
        return True

    def flush(self, *, progress: bool = False) -> None:
        text = "".join(self.buffer)
        self.buffer.clear()
        self.watchdog.observe(self.name, text)
        if self.watchdog.triggered is not None:
            self.cancel.set()
        if progress:
            if not self.emit(text, progress=True):
                self.pending = text
        else:
            if self.pending and _display_stream_text(self.pending) != _display_stream_text(text):
                self.emit(self.pending, progress=True, force=True)
            self.pending = ""
            self.emit(text, force=True)

    def feed(self, chunk: str) -> None:
        for char in chunk:
            if char == "\n":
                self.flush()
            elif char == "\r" and self.mode == "auto":
                self.flush(progress=True)
            else:
                self.buffer.append(char)
                if len(self.buffer) >= 8192:
                    self.flush()

    def finish(self) -> None:
        if self.buffer:
            self.flush()
        elif self.pending:
            self.emit(self.pending, progress=True, force=True)
            self.pending = ""


def _display_stream_text(text: str) -> str:
    """Return a terminal-friendly representation of captured stream text."""

    return text.replace("\r", "").strip()


def _emit_output_summary(
    stdout: str,
    stderr: str,
    callback: OutputCallback,
) -> None:
    for stream_name, text in (("stdout", stdout), ("stderr", stderr)):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            continue
        omitted = max(0, len(lines) - SUMMARY_TAIL_LINES)
        if omitted:
            callback(stream_name, f"... {omitted} earlier line(s) omitted ...")
        for line in lines[-SUMMARY_TAIL_LINES:]:
            callback(stream_name, line)


def _write_blocked_result(
    run_dir: Path,
    *,
    command_text: str,
    command_args: list[str],
    environment_policy: dict[str, Any],
    timeout_sec: int,
    reason: str,
    run_label: str,
    report_metadata: dict[str, Any] | None = None,
) -> CodeTaskRunResult:
    manifest = load_code_task_manifest(run_dir)
    return _write_execution_result(
        run_dir,
        manifest=manifest,
        command_text=command_text,
        command_args=command_args,
        environment_policy=environment_policy,
        timeout_sec=timeout_sec,
        duration_sec=0.0,
        status="blocked_by_validation",
        returncode=None,
        timed_out=False,
        stdout="",
        stderr=reason + "\n",
        metrics={},
        stdout_truncated=False,
        stderr_truncated=False,
        run_label=run_label,
        report_metadata=report_metadata,
    )


def _write_execution_result(
    run_dir: Path,
    *,
    manifest: dict[str, Any],
    command_text: str,
    command_args: list[str],
    environment_policy: dict[str, Any],
    timeout_sec: int,
    duration_sec: float,
    status: str,
    returncode: int | None,
    timed_out: bool,
    stdout: str,
    stderr: str,
    metrics: dict[str, float],
    stdout_truncated: bool,
    stderr_truncated: bool,
    run_label: str,
    report_metadata: dict[str, Any] | None = None,
    run_metadata: dict[str, Any] | None = None,
) -> CodeTaskRunResult:
    paths = code_task_paths(run_dir)
    run_dir_for_label = _run_label_dir(paths.run_artifact_dir, run_label)
    run_dir_for_label.mkdir(parents=True, exist_ok=True)
    stdout_path = run_dir_for_label / "stdout.txt"
    stderr_path = run_dir_for_label / "stderr.txt"
    metrics_path = run_dir_for_label / "metrics.json"
    report_path = run_dir_for_label / "execution_report.json"
    rel_base = f"code_task/run/{run_label}"

    write_text(stdout_path, stdout or "")
    write_text(stderr_path, stderr or "")
    write_json(metrics_path, metrics)
    report = {
        "schema_version": 1,
        "generated_at": utcnow_iso(),
        "status": status,
        "label": run_label,
        "command_text": command_text,
        "command": command_args,
        "cwd": str(paths.workspace_dir),
        "environment": _execution_environment_record(environment_policy),
        "timeout_sec": timeout_sec,
        "duration_sec": duration_sec,
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout": f"{rel_base}/stdout.txt",
        "stderr": f"{rel_base}/stderr.txt",
        "metrics": f"{rel_base}/metrics.json",
        "metric_values": metrics,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }
    if report_metadata:
        report.update(report_metadata)
    history_record = archive_run_attempt(
        run_dir,
        run_label=run_label,
        stdout=stdout or "",
        stderr=stderr or "",
        metrics=metrics,
        execution_report=report,
    )
    report["history_attempt"] = history_record["id"]
    report["history_report"] = history_record["execution_report"]
    write_json(report_path, report)
    _update_manifest_after_run(
        run_dir,
        manifest,
        status=status,
        command_text=command_text,
        environment_policy=environment_policy,
        returncode=returncode,
        timed_out=timed_out,
        metric_values=metrics,
        run_label=run_label,
        run_metadata=run_metadata,
        history_record=history_record,
    )
    comparison = _maybe_compare_runs(run_dir)
    if run_label == "patched":
        _update_latest_batch_after_benchmark(run_dir, report_path, status)
        _update_manifest_after_patched_outcome(
            run_dir,
            status=status,
            comparison_verdict=comparison.verdict if comparison is not None else "",
        )
    write_code_task_summary(run_dir)
    return CodeTaskRunResult(
        run_dir=paths.run_dir,
        label=run_label,
        report_path=report_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        metrics_path=metrics_path,
        status=status,
        returncode=returncode,
        timed_out=timed_out,
        metrics=metrics,
    )


def _maybe_compare_runs(run_dir: Path) -> CodeTaskComparisonResult | None:
    paths = code_task_paths(run_dir)
    baseline_report = paths.run_artifact_dir / "baseline" / "execution_report.json"
    patched_report = paths.run_artifact_dir / "patched" / "execution_report.json"
    if baseline_report.exists() and patched_report.exists():
        return compare_code_task_runs(run_dir)
    return None


def _update_latest_batch_after_benchmark(run_dir: Path, report_path: Path, status: str) -> None:
    batch = load_latest_code_task_batch(run_dir)
    if batch is None:
        return
    update_code_task_batch_state(
        run_dir,
        batch.batch_state_path,
        state="completed" if status == "passed" else "failed",
        artifacts={"benchmark_run": _relative_to_run(run_dir, report_path)},
        detail=f"Patched benchmark {status}.",
        extra={"benchmark_status": status},
    )


def _update_manifest_after_patched_outcome(
    run_dir: Path,
    *,
    status: str,
    comparison_verdict: str,
) -> None:
    manifest = load_code_task_manifest(run_dir)
    if status == "passed":
        if comparison_verdict:
            manifest["objective"] = {
                "status": comparison_verdict,
                "source": "code_task/run/comparison.json",
                "updated_at": utcnow_iso(),
            }
            if comparison_verdict == "improved":
                manifest["status"] = "objective_improved"
            elif comparison_verdict in {"regressed", "mixed"}:
                manifest["status"] = "objective_" + comparison_verdict
            else:
                manifest["status"] = "objective_inconclusive"
        _mark_failure_resolved(manifest)
        _mark_repair_benchmark_resolved(manifest)
    save_code_task_manifest(run_dir, manifest)


def _mark_failure_resolved(manifest: dict[str, Any]) -> None:
    failure = manifest.get("failure_analysis")
    if not isinstance(failure, dict) or not failure:
        return
    if failure.get("status") != "no_failure":
        failure["status"] = "resolved"
        failure["resolved_at"] = utcnow_iso()
        manifest["failure_analysis"] = failure


def _mark_repair_benchmark_resolved(manifest: dict[str, Any]) -> None:
    repair = manifest.get("repair")
    if not isinstance(repair, dict) or not repair:
        return
    if str(repair.get("status", "")).startswith("repair_"):
        repair["status"] = "benchmark_passed"
        repair["resolved_at"] = utcnow_iso()
        manifest["repair"] = repair


def _relative_to_run(run_dir: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(Path(run_dir).resolve()).as_posix()
    except ValueError:
        return str(path)


def _normalize_run_label(value: str) -> str:
    label = value.strip().lower().replace("_", "-")
    if label not in VALID_RUN_LABELS:
        raise CodeTaskRunError(
            "run_label must be one of: " + ", ".join(sorted(VALID_RUN_LABELS))
        )
    return label


def _run_label_dir(run_artifact_dir: Path, label: str) -> Path:
    return run_artifact_dir / label


def _split_command(command_text: str, *, environment_policy: dict[str, Any]) -> list[str]:
    try:
        args = shlex.split(command_text, posix=os.name != "nt")
    except ValueError as exc:
        raise CodeTaskRunError(f"Could not parse benchmark command: {exc}") from exc
    if not args:
        raise CodeTaskRunError("Benchmark command is empty")
    if any(token in CONTROL_TOKENS for token in args):
        raise CodeTaskRunError(
            "Shell control operators are not supported in benchmark commands. "
            "Use a direct command such as `python -m unittest discover -s tests`."
        )
    if os.name == "nt":
        # Non-POSIX shlex preserves grouping quotes. Popen receives argv, not
        # shell text: keeping them makes Python -c execute a string literal.
        args = [token[1:-1] if len(token) >= 2 and token[0] in {"'", '"'}
                and token[-1] == token[0] else token for token in args]
    return apply_code_task_environment_policy(args, environment_policy)


def _execution_environment_record(environment_policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": environment_policy.get("mode", "current"),
        "python_executable": policy_python_executable(environment_policy),
        "python_version": environment_policy.get("python_version"),
        "allow_dependency_install": bool(
            environment_policy.get("allow_dependency_install", False)
        ),
        "dependency_install": environment_policy.get("dependency_install", "disabled"),
    }


def _safe_env(workspace_dir: Path) -> dict[str, str]:
    allowed = {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    # Execute against the task's sources and selected interpreter, not packages
    # injected into the controller (including an installed framework candidate).
    python_paths = [str(workspace_dir), str(workspace_dir / "src")]
    env["PYTHONPATH"] = os.pathsep.join(python_paths)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["SIMPLE_AR_CODE_TASK"] = "1"
    return env


def _clip_output(text: str) -> tuple[str, bool]:
    if len(text) <= OUTPUT_CHAR_LIMIT:
        return text, False
    suffix = "\n... [truncated by SimpleAutoResearch]\n"
    return text[: OUTPUT_CHAR_LIMIT - len(suffix)] + suffix, True


def _benchmark_command(manifest: dict[str, Any]) -> str:
    benchmark = manifest.get("benchmark", {})
    if isinstance(benchmark, dict):
        command = benchmark.get("command")
        return str(command) if command else ""
    return ""


def _is_greenfield_manifest(manifest: dict[str, Any]) -> bool:
    code_task = manifest.get("code_task")
    return isinstance(code_task, dict) and str(code_task.get("kind") or "") == "greenfield"


def _write_runtime_artifact_scan(run_dir: Path, label: str, scan: dict[str, Any]) -> Path:
    paths = code_task_paths(run_dir)
    run_dir_for_label = _run_label_dir(paths.run_artifact_dir, label)
    run_dir_for_label.mkdir(parents=True, exist_ok=True)
    path = run_dir_for_label / "artifact_scan.json"
    write_json(path, scan)
    return path


def _greenfield_result_quality_guard(
    run_dir: Path,
    manifest: dict[str, Any],
    *,
    status: str,
    metrics: dict[str, float],
    artifact_scan: dict[str, Any] | None = None,
    required_file_issues: list[str] | None = None,
) -> dict[str, Any] | None:
    if status != "passed":
        return None
    from simple_ar.code_task.runtime.state import required_new_file_issues
    missing = list(dict.fromkeys([*(required_file_issues or []),
        *required_new_file_issues(manifest, code_task_paths(run_dir).workspace_dir)]))
    if missing:
        return {'status': 'failed', 'reason': 'required_new_files',
            'message': 'Required new files were not created as nonempty regular files: ' + ', '.join(missing)}
    if not _is_greenfield_manifest(manifest):
        return None
    if manifest.get('code_task', {}).get('native_binding', {}).get('binding', {}).get('measurement_contract') is not None:
        # This physical invocation is the independently approved validator.
        # Source/API review remains mandatory; scientific runtime artifacts
        # are checked by the separate formal measurement owner/result schema.
        return None
    paths = code_task_paths(run_dir)
    if artifact_scan and has_artifact_path_mismatch(artifact_scan):
        return {
            "status": "failed",
            "reason": "artifact_path_mismatch",
            "message": (
                "Generated benchmark quality guard failed: a required artifact was "
                "written to a same-name path outside the expected generated-project "
                "artifact location. See the run artifact_scan.json."
            ),
            "artifact_scan": compact_artifact_scan(artifact_scan),
        }
    results_row = expected_artifact_row(artifact_scan or {}, "artifacts/results.json")
    results_rel = (
        str(results_row.get("expected_path", "generated_project/artifacts/results.json"))
        if isinstance(results_row, dict)
        else "generated_project/artifacts/results.json"
    )
    results_path = paths.workspace_dir / results_rel
    results = None
    if isinstance(results_row, dict):
        parse_status = str(results_row.get("parse_status") or "missing")
        if parse_status in {"missing", "empty", "invalid_json", "unreadable"}:
            reason = "missing_results_artifact" if parse_status == "missing" else f"{parse_status}_results_artifact"
            return {
                "status": "failed",
                "reason": reason,
                "message": (
                    "Generated benchmark quality guard failed: expected "
                    f"{results_rel} is {parse_status}. Successful completion requires "
                    "all configured runtime artifacts."
                ),
                "artifact": _relative_to_run(run_dir, results_path),
                "artifact_scan": compact_artifact_scan(artifact_scan or {}),
            }
        try:
            results = json.loads(results_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "status": "failed",
                "reason": "unreadable_results_artifact",
                "message": (
                    "Generated benchmark quality guard failed: "
                    f"could not read {results_rel} ({exc})."
                ),
                "artifact": _relative_to_run(run_dir, results_path),
                "artifact_scan": compact_artifact_scan(artifact_scan or {}),
            }

    if isinstance(results, dict):
        record_count = _count_result_records(results.get("raw_records"))
        condition_summary_count = _mapping_size(results.get("condition_summaries"))
        dataset_summary_count = _mapping_size(results.get("dataset_comparisons"))
        non_resource_metrics = _non_resource_metric_names(manifest, metrics)
        all_non_resource_zero = bool(non_resource_metrics) and all(
            abs(float(metrics.get(name, 0.0))) <= 1e-12 for name in non_resource_metrics
        )
        no_structured_evidence = (
            record_count == 0 and condition_summary_count == 0 and dataset_summary_count == 0
        )
        if no_structured_evidence and all_non_resource_zero:
            return {
                "status": "failed",
                "reason": "empty_greenfield_evidence",
                "message": (
                    "Generated benchmark quality guard failed: artifacts/results.json contains "
                    "no condition-level records or summaries, and all non-resource metrics are "
                    "zero. Treating this run as failed instead of benchmark_passed."
                ),
                "artifact": _relative_to_run(run_dir, results_path),
                "record_count": record_count,
                "condition_summary_count": condition_summary_count,
                "dataset_summary_count": dataset_summary_count,
                "non_resource_metrics": non_resource_metrics,
            }

    unusable_artifacts = [
        row
        for row in (artifact_scan or {}).get("artifacts", [])
        if isinstance(row, dict)
        and str(row.get("parse_status", "")) in {"missing", "empty", "invalid_json", "unreadable"}
        and not str(row.get("expected_path", "")).replace("\\", "/").endswith("artifacts/results.json")
    ]
    if unusable_artifacts:
        row = unusable_artifacts[0]
        parse_status = str(row.get("parse_status") or "missing")
        expected_path = str(row.get("expected_path") or row.get("contract_path") or "required artifact")
        return {
            "status": "failed",
            "reason": f"{parse_status}_required_artifact",
            "message": (
                "Generated benchmark quality guard failed: expected "
                f"{expected_path} is {parse_status}. Successful completion requires "
                "all configured runtime artifacts."
            ),
            "artifact": _relative_to_run(run_dir, paths.workspace_dir / expected_path),
            "artifact_scan": compact_artifact_scan(artifact_scan or {}),
        }
    return None


def _count_result_records(value: object) -> int:
    if isinstance(value, list):
        total = 0
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("records"), list):
                total += len(item["records"])
            elif isinstance(item, dict):
                total += 1
            else:
                total += _count_result_records(item)
        return total
    if isinstance(value, dict):
        records = value.get("records")
        if isinstance(records, list):
            return len(records)
        return sum(_count_result_records(item) for item in value.values())
    return 0


def _mapping_size(value: object) -> int:
    return len(value) if isinstance(value, dict) else 0


def _non_resource_metric_names(manifest: dict[str, Any], metrics: dict[str, float]) -> list[str]:
    benchmark = manifest.get("benchmark")
    directions = benchmark.get("metric_directions") if isinstance(benchmark, dict) else {}
    directions = directions if isinstance(directions, dict) else {}
    names: list[str] = []
    for name in metrics:
        direction = str(directions.get(name, "")).strip().lower()
        lowered = name.lower()
        if direction == "resource" or "runtime" in lowered or lowered.endswith("_sec"):
            continue
        names.append(name)
    return names


def _greenfield_results_artifact_path(workspace_dir: Path) -> Path:
    candidates = [
        workspace_dir / "generated_project" / "artifacts" / "results.json",
        workspace_dir / "artifacts" / "results.json",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def _update_manifest_after_run(
    run_dir: Path,
    manifest: dict[str, Any],
    *,
    status: str,
    command_text: str,
    environment_policy: dict[str, Any],
    returncode: int | None,
    timed_out: bool,
    metric_values: dict[str, float],
    run_label: str,
    run_metadata: dict[str, Any] | None = None,
    history_record: dict[str, Any] | None = None,
) -> None:
    layout = manifest_section(manifest, "layout")
    layout["run"] = "code_task/run"
    layout[f"{run_label}_execution_report"] = f"code_task/run/{run_label}/execution_report.json"
    layout["latest_execution_report"] = f"code_task/run/{run_label}/execution_report.json"
    benchmark = manifest_section(manifest, "benchmark")
    runs = benchmark.get("runs", {})
    if not isinstance(runs, dict):
        runs = {}
    run_record = {
        "label": run_label,
        "status": status,
        "run_at": utcnow_iso(),
        "execution_report": f"code_task/run/{run_label}/execution_report.json",
        "stdout": f"code_task/run/{run_label}/stdout.txt",
        "stderr": f"code_task/run/{run_label}/stderr.txt",
        "metrics": f"code_task/run/{run_label}/metrics.json",
        "returncode": returncode,
        "timed_out": timed_out,
        "metric_values": metric_values,
        "environment": _execution_environment_record(environment_policy),
    }
    existing_record = runs.get(run_label)
    previous_attempts = (
        existing_record.get("attempts", []) if isinstance(existing_record, dict) else []
    )
    previous_attempt_count = (
        int(existing_record.get("attempt_count", 0) or 0) if isinstance(existing_record, dict) else 0
    )
    attempts = [row for row in previous_attempts if isinstance(row, dict)]
    new_history = history_record and not any(row.get('id') == history_record['id'] for row in attempts)
    if history_record:
        if new_history:
            attempts.append(dict(history_record))
        run_record["latest_attempt"] = history_record["id"]
        run_record["latest_attempt_dir"] = str(history_record["execution_report"]).rsplit("/", 1)[0]
    if attempts:
        run_record["attempts"] = attempts[-50:]
        run_record["attempt_count"] = max(previous_attempt_count + (1 if new_history else 0), len(attempts))
    if run_metadata:
        run_record.update(run_metadata)
    runs[run_label] = run_record
    benchmark.update(
        {
            "command": command_text,
            "executed": True,
            "latest_label": run_label,
            "last_status": status,
            "last_run_at": run_record["run_at"],
            "execution_report": run_record["execution_report"],
            "stdout": run_record["stdout"],
            "stderr": run_record["stderr"],
            "metrics": run_record["metrics"],
            "returncode": returncode,
            "timed_out": timed_out,
            "metric_values": metric_values,
            "runs": runs,
        }
    )
    if run_label == "baseline":
        policy = "provided" if run_record.get("provided") else "run"
        benchmark["baseline_policy"] = {
            "policy": policy,
            "status": "recorded",
            "source": run_record.get("provided_source") or "benchmark_run",
            "updated_at": run_record["run_at"],
        }
    manifest["layout"] = layout
    manifest["benchmark"] = benchmark
    if run_label == "baseline":
        if status == "passed":
            manifest["status"] = "baseline_passed"
        elif status == "blocked_by_validation":
            manifest["status"] = "baseline_blocked"
        else:
            manifest["status"] = "baseline_failed"
    elif status == "passed":
        manifest["status"] = "benchmark_passed"
    elif status == "blocked_by_validation":
        manifest["status"] = "benchmark_blocked"
    else:
        manifest["status"] = "benchmark_failed"
    save_code_task_manifest(run_dir, manifest)
