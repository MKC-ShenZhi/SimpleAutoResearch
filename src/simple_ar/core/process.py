"""Bounded local process I/O shared by experiment and CodeTask execution.

This controls process lifetime and output, not CPU, GPU or network isolation.
Callers own environment policy, metrics and experiment authorization.
"""

from __future__ import annotations

import codecs
import math
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from simple_ar.core.artifacts import read_json, write_json
from simple_ar.core.budget import BudgetConflictError, BudgetLedger


@dataclass(frozen=True)
class ProcessSpec:
    argv: list[str]
    cwd: Path
    timeout_sec: float
    env: dict[str, str] | None = None
    output_dir: Path | None = None
    tail_bytes: int = 200_000
    log_bytes: int = 2_000_000  # Per stream; keep draining after this limit.
    invocation_id: str = field(default_factory=lambda: uuid4().hex)
    session_id: str = ""
    attempt_id: str = ""
    input_binding: dict | None = None


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stop_reason: str | None
    stdout: str
    stderr: str
    duration_sec: float
    record: dict


def with_process_outputs(spec: ProcessSpec) -> ProcessSpec:
    """Bind argv and environment to the same invocation-owned output directory."""
    if spec.output_dir is None:
        if any("{output_dir}" in arg for arg in spec.argv):
            raise ValueError("Command contains {output_dir} without a process output directory")
        return spec
    invocation_dir = (spec.output_dir / spec.invocation_id).absolute()
    outputs_dir = invocation_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ if spec.env is None else spec.env)
    environment["SIMPLE_AR_OUTPUT_DIR"] = str(outputs_dir)
    return replace(spec, output_dir=invocation_dir, env=environment,
                   argv=[arg.replace("{output_dir}", str(outputs_dir)) for arg in spec.argv])


class _Capture:
    def __init__(self, spec: ProcessSpec, name: str) -> None:
        self.tail = bytearray()
        self.total = 0
        self.written = 0
        self.spec = spec
        self.path = spec.output_dir / f"{name}.log" if spec.output_dir else None

    def drain(self, stream, callback: Callable[[str], None] | None) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        log = None
        try:
            log = self.path.open("wb") if self.path else None
            while raw := stream.read(8192):
                self.total += len(raw)
                self.tail.extend(raw)
                del self.tail[: max(0, len(self.tail) - self.spec.tail_bytes)]
                if log is not None:
                    chunk = raw[: max(0, self.spec.log_bytes - self.written)]
                    log.write(chunk)
                    log.flush()
                    self.written += len(chunk)
                if callback is not None:
                    callback(decoder.decode(raw))
            if callback is not None:
                callback(decoder.decode(b"", final=True))
        finally:
            stream.close()
            if log is not None:
                log.close()

    def metadata(self) -> dict:
        return {
            "bytes_seen": self.total,
            "tail_bytes_discarded": max(0, self.total - len(self.tail)),
            "log_bytes_discarded": max(0, self.total - self.written) if self.path else None,
            "log_path": str(self.path) if self.path else None,
            "observation_source": "final_tail",
            "observation_bytes_discarded": max(0, self.total - len(self.tail)),
            "final_tail_available": True,
        }


def process_observation(record: dict, output_dir: Path, *, tail_bytes: int = 12000) -> dict:
    """Project finalized output without executing or rewriting an invocation.

    New receipts own the exact decoded final tails. Legacy logs contain a
    bounded prefix, so their last bytes cannot stand in for a lost final tail.
    """
    if record["status"] not in {"finished", "stopped"}:
        raise ValueError("Only finalized process records have recoverable output")
    if tail_bytes < 1:
        raise ValueError("A positive legacy tail limit is required")
    streams = {name: dict(record.get("streams", {}).get(name, {}))
               for name in ("stdout", "stderr")}
    if "output" in record:
        output = {name: record["output"][name] for name in streams}
    else:
        output = {}
        for name, metadata in streams.items():
            with (output_dir / f"{name}.log").open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - tail_bytes))
                raw = handle.read(tail_bytes)
            output[name] = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
            seen = metadata.get("bytes_seen")
            metadata.update(
                observation_source="legacy_log_tail", final_tail_available=False,
                observation_bytes_discarded=max(0, seen - len(raw)) if seen is not None else None,
            )
            # Missing historical measurements are unknown, never zero omissions.
            metadata.setdefault("log_bytes_discarded", None)
            metadata.setdefault("tail_bytes_discarded", None)
    return {"returncode": record["returncode"], "stop_reason": record.get("stop_reason"),
            **output, "streams": streams}


def run_process(
    spec: ProcessSpec,
    *,
    output_callback: Callable[[str, str], None] | None = None,
    cancel: threading.Event | None = None,
    budget_ledger: BudgetLedger | None = None,
    output_complete_callback: Callable[[], None] | None = None,
) -> ProcessResult:
    """Execute one invocation, optionally reserving and settling shared budget.

    Ledger-backed calls require persistent output. Reusing an invocation ID never
    starts another process; recovery settles its existing record explicitly.
    """
    if budget_ledger is None:
        return _run_process(spec, output_callback=output_callback, cancel=cancel,
                            output_complete_callback=output_complete_callback)
    if spec.output_dir is None:
        raise ValueError("Ledger-backed execution requires a persistent output_dir")
    reservation = f"process-{spec.invocation_id}"
    if any(entry.reservation_id == reservation for entry in budget_ledger.entries):
        raise BudgetConflictError("Invocation already reserved; reconcile its record instead of rerunning it")
    budget_ledger.reserve(
        reservation, {"process_invocations": 1, "process_wall_seconds": spec.timeout_sec},
        session_id=spec.session_id, attempt_id=spec.attempt_id,
        logical_call_id=spec.invocation_id, purpose="local_process",
    )
    try:
        result = _run_process(spec, output_callback=output_callback, cancel=cancel,
                              output_complete_callback=output_complete_callback)
    except BaseException:
        record_path = spec.output_dir / "invocation.json"
        if record_path.is_file():
            record = read_json(record_path)
            if record["invocation_id"] != spec.invocation_id or record["status"] == "launch_failed":
                budget_ledger.release(reservation, reason="Process was not launched")
            elif record["status"] in {"finished", "stopped"}:
                settle_process_record(record, budget_ledger)
            else:
                budget_ledger.mark_unknown(reservation, reason="Process supervision interrupted; inspect invocation before continuing")
        else:
            budget_ledger.release(reservation, reason="Preflight failed before invocation record was written")
        raise
    settle_process_record(result.record, budget_ledger)
    return result


def verify_process_accounting(record: dict, ledger: BudgetLedger, *, session_id: str,
                              attempt_id: str, timeout_sec: int) -> None:
    """Read-only verification of a finalized physical invocation and its ledger."""
    invocation, duration = record['invocation_id'], record['duration_sec']
    if (not isinstance(invocation, str) or not re.fullmatch(r'[0-9a-f]{32}', invocation)
            or record['status'] not in {'finished', 'stopped'}
            or record['session_id'] != session_id or record['attempt_id'] != attempt_id
            or record['timeout_sec'] != timeout_sec or type(duration) not in (int, float)
            or not math.isfinite(duration) or duration < 0):
        raise ValueError('Invalid finalized process identity or wall-clock observation')
    entries = [entry for entry in ledger.entries if entry.reservation_id == f'process-{invocation}']
    if len(entries) != 1:
        raise ValueError('Process lacks its unique physical reservation')
    entry = entries[0]
    if (entry.logical_call_id != invocation or entry.attempt_id != attempt_id
            or entry.session_id != session_id or entry.status != 'settled'
            or entry.purpose != 'local_process' or entry.actual_source != 'executor_wall_clock'
            or entry.reserved != {'process_invocations': 1, 'process_wall_seconds': timeout_sec}
            or entry.actual != {'process_invocations': 1, 'process_wall_seconds': duration}
            or type(entry.actual.get('process_invocations')) is not int):
        raise ValueError('Physical process accounting differs from its reservation and observed wall clock')


def settle_process_record(record: dict, ledger: BudgetLedger) -> None:
    """Idempotently reconcile a finalized executor record without running code."""
    if record["status"] not in {"finished", "stopped"}:
        raise ValueError("Only finalized process records can settle measured consumption")
    ledger.settle(
        f"process-{record['invocation_id']}",
        {"process_invocations": 1, "process_wall_seconds": record["duration_sec"]},
        actual_source="executor_wall_clock",
    )


def _run_process(
    spec: ProcessSpec,
    *,
    output_callback: Callable[[str, str], None] | None,
    cancel: threading.Event | None,
    output_complete_callback: Callable[[], None] | None = None,
) -> ProcessResult:
    """Run argv without a shell; return bounded tails and observed execution facts.

    Output callbacks receive decoded chunks. A callback error ends the process and
    is raised in the caller; it must not silently turn into a successful experiment.
    Windows uses a Job Object attached immediately after spawn. The small attachment
    window is recorded explicitly; this is not a hostile-code sandbox.
    """
    if not spec.argv or spec.timeout_sec <= 0 or spec.tail_bytes < 1 or spec.log_bytes < 0:
        raise ValueError("argv, positive timeout/tail limit and nonnegative log limit are required")
    if not spec.cwd.is_dir():
        raise ValueError(f"Working directory not found: {spec.cwd}")
    if cancel is not None and cancel.is_set():
        raise ValueError("Process cancelled before launch")
    if spec.output_dir:
        spec.output_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": "process_invocation.v1",
        "invocation_id": spec.invocation_id,
        "session_id": spec.session_id,
        "attempt_id": spec.attempt_id,
        "argv": list(spec.argv),
        "cwd": str(spec.cwd.resolve()),
        "status": "starting",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "timeout_sec": spec.timeout_sec,
        "environment_policy": "inherited" if spec.env is None else "caller_supplied",
        "resource_enforcement": {"cpu": "not_enforced", "gpu": "not_enforced", "memory": "not_enforced"},
    }
    if spec.input_binding is not None:
        # The caller owns this authority/source identity; retain it with the
        # actual expanded argv/environment before launch, in the same record.
        record.update(input_binding=spec.input_binding, environment=spec.env)

    def save() -> None:
        if spec.output_dir:
            write_json(spec.output_dir / "invocation.json", record)

    save()
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            spec.argv, cwd=spec.cwd, env=spec.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
    except OSError as exc:
        record.update(status="launch_failed", error=str(exc))
        save()
        raise
    job = None
    captures = {name: _Capture(spec, name) for name in ("stdout", "stderr")}
    errors: list[Exception] = []
    failed = threading.Event()

    def drain(name: str) -> None:
        try:
            callback = (lambda chunk: output_callback(name, chunk)) if output_callback else None
            captures[name].drain(getattr(process, name), callback)
        except Exception as exc:
            errors.append(exc)
            failed.set()

    threads = [threading.Thread(target=drain, args=(name,), daemon=True) for name in captures]
    stop_reason = None
    try:
        if os.name == "nt":
            from simple_ar.core.process_windows import ProcessJob

            job = ProcessJob(process)
        record.update(
            pid=process.pid, status="running",
            process_tree="windows_job_after_spawn" if job else "posix_process_group",
        )
        save()
        for thread in threads:
            thread.start()
        while process.poll() is None:
            if failed.is_set():
                stop_reason = "output_error"
                break
            if cancel is not None and cancel.is_set():
                stop_reason = "cancelled"
                break
            if time.monotonic() - started >= spec.timeout_sec:
                stop_reason = "timeout"
                break
            failed.wait(0.05)
    except Exception as exc:
        record.update(status="execution_error", error=str(exc))
        save()
        raise
    finally:
        # Also close descendants after a successful parent exit: otherwise an
        # inherited output pipe can keep readers (and GPU workers) alive.
        if job is not None:
            job.close()
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=5)
        for name, thread in zip(captures, threads):
            if thread.ident is None:
                getattr(process, name).close()
        if any(thread.is_alive() for thread in threads):
            raise RuntimeError("Process descendants retained output pipes after termination")
    if output_complete_callback is not None:
        output_complete_callback()
    duration = time.monotonic() - started
    if errors:
        stop_reason = "output_error"
    record.update(
        status="stopped" if stop_reason else "finished",
        stop_reason=stop_reason, returncode=process.returncode,
        finished_at=datetime.now(timezone.utc).isoformat(), duration_sec=duration,
        streams={name: capture.metadata() for name, capture in captures.items()},
        output={name: capture.tail.decode("utf-8", errors="replace").replace("\r\n", "\n")
                for name, capture in captures.items()},
    )
    save()
    if errors:
        raise errors[0]
    return ProcessResult(
        returncode=process.returncode, stop_reason=stop_reason,
        stdout=record["output"]["stdout"],
        stderr=record["output"]["stderr"],
        duration_sec=duration, record=record,
    )
