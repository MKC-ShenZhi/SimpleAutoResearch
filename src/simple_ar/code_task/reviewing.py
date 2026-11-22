from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping

from simple_ar.integrations.usage import record_usage
from simple_ar.integrations.llm import LLMClient, LLMError, LLMResponseError
from simple_ar.reviewing.schema import ReviewFinding, normalize_review_findings, review_report
from simple_ar.reviewing.visual import _dedupe_findings, build_review_artifact, run_visual_review
from simple_ar.core.artifacts import write_json
from simple_ar.core.budget import BudgetLedger
from simple_ar.code_task.runtime.state import review_workspace_boundary
from simple_ar.code_task.review_pipeline import (
    build_review_index, build_review_clusters, compact_review_index, snippets_for_cluster,
)


MessageCallback = Callable[[str], None]

CODE_TASK_REVIEW_SYSTEM = (
    "You are a strict but practical senior code reviewer for an isolated code-task workspace. "
    "You cannot edit files. Review scope, runtime correctness, result validity, benchmark integrity, "
    "resource risk, and repair risk. Return only JSON."
)


def run_project_review(
    *, project_dir: Path, result_schema: Mapping[str, Any], contract: Mapping[str, Any],
    deterministic: list[ReviewFinding], reviewer: str, subject: str,
    prompt: Callable[[dict[str, Any], Mapping[str, Any], list[str]], str],
    metadata: Callable[[list[dict[str, Any]]], dict[str, Any]],
    request_options: dict[str, Any], total_findings: int, use_llm: bool,
    meta_dir: Path | None, artifact_suffix: str = '',
    cluster_metadata: dict[str, Any] | None = None,
    cluster_options: dict[str, Any] | None = None,
    snippet_options: dict[str, Any] | None = None,
    save_report: bool = False, before_save: Callable[[Path], None] | None = None,
    access_boundary: Path | None = None,
) -> dict[str, Any]:
    """Own the project-review lifecycle; callers supply domain facts and policy.

    Preserve each consumer's paths, scope, source windows and usage ownership.
    The total finding cap does not stop later clusters from being inspected.
    """
    boundary = review_workspace_boundary(project_dir, boundary=access_boundary)
    index = build_review_index(project_dir, result_schema=result_schema, contract=contract,
        access_boundary=boundary)
    clusters = build_review_clusters(index, deterministic_findings=deterministic,
                                    **(cluster_options or {}))
    if meta_dir is not None:
        if before_save is not None:
            before_save(meta_dir)
        write_json(meta_dir / f'review_index{artifact_suffix}.json', index)
        write_json(meta_dir / f'review_clusters{artifact_suffix}.json', {
            'schema_version': 'code_task_review_clusters.v1', **(cluster_metadata or {}),
            'cluster_count': len(clusters), 'clusters': clusters,
        })
    findings: list[ReviewFinding] = []
    if use_llm:
        compact_index = compact_review_index(index)
        for cluster in clusters:
            snippets = snippets_for_cluster(project_dir, cluster,
                **{**(snippet_options or {}), 'access_boundary': boundary})
            if not snippets:
                continue
            cluster_id = str(cluster.get('cluster_id') or 'cluster')
            options = {**request_options,
                'label': request_options['label'].replace('{cluster_id}', cluster_id),
                'source': request_options['source'].replace('{cluster_id}', cluster_id)}
            findings.extend(run_llm_review(meta_dir=meta_dir,
                prompt=prompt(compact_index, cluster, snippets), use_llm=True,
                allow_blocking=False, **options))
    report = build_review_artifact(reviewer=reviewer, subject=subject,
        findings=[*deterministic, *findings[:total_findings]], metadata=metadata(clusters))
    if save_report and meta_dir is not None:
        write_json(meta_dir / f'review_report{artifact_suffix}.json', report)
    return report


def run_llm_review(
    *,
    meta_dir: Path,
    prompt: str,
    label: str,
    source: str,
    default_category: str,
    default_evidence: list[str],
    model: str | None = None,
    use_llm: bool = True,
    client: LLMClient | None = None,
    record_injected_usage: bool = False,
    message_callback: MessageCallback | None = None,
    max_findings: int = 16,
    allow_blocking: bool = False,
    budget_ledger: BudgetLedger | None = None,
    session_id: str = "", attempt_id: str = "",
) -> list[ReviewFinding]:
    """Run the shared LLM reviewer and normalize its findings."""

    if not use_llm:
        return []
    try:
        _emit(message_callback, f"Calling LLM reviewer for {label}.")
        # Generated-project clients already own their task usage observer.
        # Existing-project review receives the unscoped session client.
        llm_client = client if client is not None and not record_injected_usage else LLMClient.for_task(
            client=client,
            model=model,
            budget_ledger=budget_ledger, session_id=session_id, attempt_id=attempt_id,
            usage_callback=lambda usage: record_usage(
                meta_dir,
                usage,
                stage="code_task.review",
                message_callback=message_callback,
            ),
        )
        response = llm_client.ask_json(CODE_TASK_REVIEW_SYSTEM, prompt, label=label)
    except LLMError as exc:
        _emit(message_callback, f"LLM reviewer unavailable; keeping deterministic review only. {exc}")
        return []
    findings = normalize_review_findings(
        response.get("findings"),
        source=source,
        default_category=default_category,
        default_evidence=default_evidence,
        max_findings=max_findings,
    )
    if allow_blocking:
        return findings
    return _downgrade_llm_blockers(findings)


def review_prompt(
    *,
    instructions: str,
    context: dict[str, Any],
    snippets: list[str],
) -> str:
    """Render a common JSON-review prompt from structured context and snippets."""

    return (
        "Return JSON with `findings`: a list of objects with fields "
        "`severity` (blocking|warning|info), `category`, `summary`, `evidence`, and `recommendation`.\n"
        "Use `blocking` only when the evidence clearly shows execution, validation, or result claims should not proceed.\n"
        "Prefer concrete, bounded findings over broad style feedback.\n\n"
        f"{instructions.strip()}\n\n"
        "Context JSON:\n"
        f"{json.dumps(context, indent=2, ensure_ascii=False)}\n\n"
        "Evidence snippets:\n"
        + ("\n\n".join(snippets) if snippets else "No source snippets available.")
    )




def _downgrade_llm_blockers(findings: list[ReviewFinding]) -> list[ReviewFinding]:
    """Keep LLM feedback visible without letting snippet-only review hard-block."""

    rows: list[ReviewFinding] = []
    for finding in findings:
        if finding.severity == "blocking":
            rows.append(finding.model_copy(update={"severity": "warning"}))
        else:
            rows.append(finding)
    return rows


def _emit(callback: MessageCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)
