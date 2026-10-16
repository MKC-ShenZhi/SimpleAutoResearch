from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from simple_ar.core.artifacts import append_jsonl, write_json
from simple_ar.integrations.llm import LLMClient, LLMError, LLMResponseError
from simple_ar.core.budget import BudgetLedger


MessageCallback = Callable[[str], None]
OutputSummaryCallback = Callable[[str, Mapping[str, Any]], dict[str, Any]]


def run_completed_response_corrections(
    *,
    client: LLMClient,
    operation: Callable[[int, LLMError | None], dict[str, Any]],
    retry_attempts: int,
    on_failure: Callable[[LLMError, int, bool], None],
) -> dict[str, Any]:
    """Own the correction boundary for decoded JSON and plan validation alike."""
    retry_attempts = max(1, int(retry_attempts or 1))
    last_error: LLMError | None = None
    for attempt in range(1, retry_attempts + 1):
        accounting_before = response_accounting_snapshot(client)
        try:
            return operation(attempt, last_error)
        except LLMError as exc:
            correct = (isinstance(exc, LLMResponseError) and attempt < retry_attempts
                       and response_accounting_known(client, accounting_before))
            on_failure(exc, attempt, correct)
            if not correct:
                raise
            last_error = exc
    raise AssertionError("Response correction loop exhausted without a result or error")


def run_json_agent_step(
    *,
    client: LLMClient,
    system: str,
    prompt: str,
    label: str,
    stage: str,
    attempt_index: int,
    retry_attempts: int,
    max_output_tokens: int | None,
    artifact_dir: Path | None = None,
    feedback: list[str] | None = None,
    output_summary_callback: OutputSummaryCallback | None = None,
    message_callback: MessageCallback | None = None,
) -> dict[str, Any]:
    """Run a structured step; retry_attempts bounds completed JSON corrections.

    LLMClient alone owns provider/transport retries and physical-call accounting.
    """

    retry_attempts = max(1, int(retry_attempts or 1))
    feedback_rows = list(feedback or [])

    def operation(attempt: int, last_error: LLMError | None) -> dict[str, Any]:
        call_label = label if attempt == 1 else f"{label}-retry-{attempt}"
        request_prompt = prompt + retry_suffix(last_error, attempt)

        def record(status: str, output: Mapping[str, Any]) -> None:
            record_agent_step(
                artifact_dir, stage=stage, attempt_index=attempt_index,
                retry_index=attempt, label=call_label, status=status,
                prompt=request_prompt, output=output, feedback=feedback_rows,
                output_summary_callback=output_summary_callback,
            )

        try:
            result = client.ask_json(
                system,
                request_prompt,
                label=call_label,
                max_output_tokens=max_output_tokens,
            )
            output = result if isinstance(result, dict) else {}
            record("passed", output)
            return output
        except LLMError as exc:
            if isinstance(exc, LLMResponseError) and artifact_dir is not None:
                write_json(artifact_dir / stage / f"attempt-{attempt_index:03d}-response-{attempt:03d}.json",
                    {"error": str(exc), "raw_response": exc.raw_response})
            record("failed", {"error": str(exc)})
            raise

    def on_failure(exc: LLMError, attempt: int, correct: bool) -> None:
        _emit(message_callback,
              f"Agent step `{label}` needs JSON correction ({attempt}/{retry_attempts}). {exc}"
              if correct else f"Agent step `{label}` failed after {attempt}/{retry_attempts} attempt(s). {exc}")

    return run_completed_response_corrections(
        client=client, operation=operation, retry_attempts=retry_attempts, on_failure=on_failure,
    )


def write_agent_step_artifact(
    artifact_dir: Path | None,
    *,
    stage: str,
    attempt_index: int,
    value: Mapping[str, Any],
) -> None:
    if artifact_dir is None:
        return
    stage_dir = artifact_dir / stage
    stage_dir.mkdir(parents=True, exist_ok=True)
    write_json(stage_dir / f"attempt-{attempt_index:03d}.json", dict(value))


def record_agent_step(
    artifact_dir: Path | None,
    *,
    stage: str,
    attempt_index: int,
    retry_index: int,
    label: str,
    status: str,
    prompt: str,
    output: Mapping[str, Any],
    feedback: list[str],
    output_summary_callback: OutputSummaryCallback | None = None,
) -> None:
    if artifact_dir is None:
        return
    artifact_dir.mkdir(parents=True, exist_ok=True)
    append_jsonl(
        artifact_dir / "agent_steps.jsonl",
        {
            "schema_version": "code_task_agent_step.v2",
            "stage": stage,
            "attempt_index": attempt_index,
            "retry_index": retry_index,
            "label": label,
            "status": status,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "prompt_chars": len(prompt),
            "feedback": feedback[-8:],
            "output_keys": sorted(str(key) for key in output.keys()),
            "output_summary": output_summary_callback(stage, output) if output_summary_callback else {},
        },
    )


def retry_suffix(error: LLMError | None, attempt: int) -> str:
    if error is None:
        return ""
    return (
        "\nPrevious attempt failed before attempt "
        f"{attempt}: {error}\n"
        "The completed response did not satisfy the JSON format. "
        "Return a smaller single JSON object only: no Markdown, no commentary, no trailing analysis.\n"
    )


@dataclass(frozen=True)
class ResponseAccountingSnapshot:
    """Call-local evidence; never grants or releases budget capacity."""

    ledger: BudgetLedger
    unknown_records: tuple[dict[str, Any], ...]


def response_accounting_snapshot(client: LLMClient) -> ResponseAccountingSnapshot | None:
    """Capture complete independent records before an operation/model call."""
    budget = getattr(client, '_budget_ledger', None)
    if not isinstance(budget, BudgetLedger):
        return None
    return ResponseAccountingSnapshot(budget, tuple(
        entry.to_dict() for entry in budget.entries if entry.status == 'unknown'))


def response_accounting_known(
    client: LLMClient, before: ResponseAccountingSnapshot | None = None,
) -> bool:
    """Allow only unchanged unknown history explicitly captured before the call.

    Without a snapshot retain the conservative whole-ledger check. Reserved
    entries (including older in-flight work) always block. Every prior unknown
    must still exist with its entire record unchanged, even if now settled.
    This is response-correction eligibility, not budget admission.
    """
    budget = getattr(client, '_budget_ledger', None)
    if not isinstance(budget, BudgetLedger):
        return before is None
    if before is not None and before.ledger is not budget:
        return False
    entries = {entry.reservation_id: entry.to_dict() for entry in budget.entries}
    retained = {row['reservation_id']: row for row in before.unknown_records} if before else {}
    if any(entries.get(key) != row for key, row in retained.items()):
        return False
    return all(row['status'] in ('settled', 'released') or (
        row['status'] == 'unknown' and retained.get(key) == row)
        for key, row in entries.items())


def _emit(callback: MessageCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)
