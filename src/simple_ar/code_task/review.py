from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from simple_ar.code_task.editing.scope import (
    allowed_patterns_from_manifest,
    is_edit_allowed_path,
    protected_patterns_from_manifest,
)
from simple_ar.code_task.generation.task_contract import load_task_contract
from simple_ar.code_task.memory import record_review_finding, task_memory_context
from simple_ar.code_task.analysis.interfaces import find_local_api_mismatches, project_api_contract
from simple_ar.code_task.runtime.state import code_task_paths, load_code_task_manifest
from simple_ar.core.artifacts import read_json, read_text
from simple_ar.reviewing.schema import ReviewFinding
from simple_ar.integrations.llm import LLMClient
from simple_ar.code_task.reviewing import review_prompt, run_project_review


MessageCallback = Callable[[str], None]

@dataclass(frozen=True, slots=True)
class CodeTaskReviewResult:
    run_dir: Path
    report_path: Path
    status: str
    blocking_count: int
    warning_count: int


def review_code_task_changes(
    run_dir: Path,
    *,
    phase: str = "post_apply",
    model: str | None = None,
    llm_client: LLMClient | None = None,
    use_llm: bool = True,
    max_source_chars_per_file: int = 3000,
    message_callback: MessageCallback | None = None,
) -> CodeTaskReviewResult:
    """Review the current code-task patch and write structured findings.

    Model findings remain advisory, even when overlapping source windows yield
    matching wording. Agreement is not an observed failure. Deterministic scope,
    interface and recorded validation defects still block execution. Findings
    remain in task memory for behavioral validation and later repair.
    """

    root = Path(run_dir)
    paths = code_task_paths(root)
    manifest = load_code_task_manifest(root)
    changed_files = _changed_files(manifest, paths)
    interface_mismatches = find_local_api_mismatches(paths.workspace_dir, relevant_paths=changed_files)
    deterministic = _deterministic_findings(root, manifest, changed_files, interface_mismatches=interface_mismatches)
    # A confirmed scope/API/validation failure already determines the next
    # action. Do not require a provider round-trip to persist that diagnosis.
    model_review = use_llm and not any(row.severity == "blocking" for row in deterministic)
    interface_paths = _dedupe(
        [*changed_files, *(str(row.get("target_path", "")) for row in interface_mismatches)]
    )[:20]
    local_api_contract = project_api_contract(paths.workspace_dir, relevant_paths=interface_paths) if model_review else {}
    suffix = "" if phase == "post_apply" else f"_{phase}"
    report = run_project_review(
        project_dir=paths.workspace_dir, result_schema=_result_schema_from_manifest(manifest),
        contract=_contract_from_run(paths), deterministic=deterministic,
        reviewer="code-task-reviewer", subject=phase, meta_dir=paths.meta_dir,
        artifact_suffix=suffix, cluster_metadata={"phase": phase}, save_report=True,
        cluster_options={"max_clusters": 4, "max_files_per_cluster": 5, "relevant_paths": changed_files},
        snippet_options={"chars_per_file": max_source_chars_per_file,
                         "patch_diff": _read_optional_text(paths.task_dir / "patch.diff")},
        use_llm=model_review, total_findings=16,
        request_options={
            "client": llm_client, "record_injected_usage": True, "model": model,
            "message_callback": message_callback, "max_findings": 5,
            "label": f"code-task-review-{phase}-{{cluster_id}}",
            "source": "code-task.llm-reviewer.{cluster_id}", "default_category": "llm_review",
            "default_evidence": ["code_task/patch.diff", _relative_meta_path(phase, "review_index")],
        },
        prompt=lambda index, cluster, snippets: _review_prompt(
            root, manifest=manifest, phase=phase, changed_files=changed_files,
            review_index=index, review_cluster=cluster, interface_mismatches=interface_mismatches,
            local_api_contract=local_api_contract, snippets=snippets),
        metadata=lambda clusters: {
            "phase": phase, "changed_files": changed_files,
            "changed_files_outside_review_clusters": [path for path in changed_files
                if not any(path in cluster.get("files", []) for cluster in clusters)],
            "patch_diff": "code_task/patch.diff" if (paths.task_dir / "patch.diff").is_file() else "",
            "review_mode": "deterministic_blocked" if use_llm and not model_review else "layered",
            "review_index": _relative_meta_path(phase, "review_index"),
            "review_clusters": _relative_meta_path(phase, "review_clusters"),
            "review_cluster_count": len(clusters),
        },
    )
    report_path = paths.meta_dir / f"review_report{suffix}.json"
    findings = report.get("findings", [])
    for row in findings:
        if not isinstance(row, dict):
            continue
        record_review_finding(
            root,
            {
                "key": row.get("key") or f"{phase}:{row.get('category', '')}:{str(row.get('summary', ''))[:40]}",
                "severity": row.get("severity", "info"),
                "category": row.get("category", "general"),
                "summary": row.get("summary", ""),
                "evidence": row.get("evidence", []),
                "recommendation": row.get("recommendation", ""),
                "source": row.get("source", "reviewer"),
            },
        )
    summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    return CodeTaskReviewResult(
        run_dir=root,
        report_path=report_path,
        status=str(report.get("status", "unknown")),
        blocking_count=int(summary.get("blocking_count", 0) or 0),
        warning_count=int(summary.get("warning_count", 0) or 0),
    )


def _deterministic_findings(root: Path, manifest: dict[str, Any], changed_files: list[str], *,
                            interface_mismatches: list[dict[str, Any]] | None = None) -> list[ReviewFinding]:
    paths = code_task_paths(root)
    findings: list[ReviewFinding] = []
    allowed = allowed_patterns_from_manifest(manifest)
    protected = protected_patterns_from_manifest(manifest)
    for path in changed_files:
        if not is_edit_allowed_path(path, allowed_patterns=allowed, protected_patterns=protected):
            findings.append(
                ReviewFinding(
                    key=f"scope:{path}",
                    severity="blocking",
                    category="scope",
                    summary=f"Changed file `{path}` is outside the configured editable scope.",
                    evidence=["code_task/patch.diff", "manifest.json"],
                    recommendation="Regenerate the proposal inside edit_scope or update the config deliberately.",
                    source="code-task.rule-review",
                )
            )
    if interface_mismatches is None:
        interface_mismatches = find_local_api_mismatches(paths.workspace_dir, relevant_paths=changed_files)
    for mismatch in interface_mismatches:
        caller = str(mismatch.get("caller", ""))
        target_path = str(mismatch.get("target_path", ""))
        target_module = str(mismatch.get("target_module", ""))
        missing_symbol = str(mismatch.get("missing_symbol", ""))
        available = ", ".join(mismatch.get("available_symbols", [])) or "none"
        findings.append(
            ReviewFinding(
                key=f"interface:{caller}:{mismatch.get('line', 0)}:{target_module}:{missing_symbol}",
                severity="blocking",
                category="interface_compatibility",
                summary=(
                    f"`{caller}` references missing local API `{target_module}.{missing_symbol}`; "
                    f"available symbols: {available}."
                ),
                evidence=[caller, target_path, "code_task/patch.diff"],
                recommendation="Align the caller with the actual local module API before running the benchmark.",
                source="code-task.rule-review",
            )
        )
    if not changed_files and _patch_status(manifest) == "applied":
        findings.append(
            ReviewFinding(
                key="patch:no-changed-files",
                severity="warning",
                category="patch",
                summary="Patch status is applied, but no changed files are recorded.",
                evidence=["manifest.json"],
                recommendation="Inspect applied_edits.json and patch.diff before trusting the run.",
                source="code-task.rule-review",
            )
        )
    validation = _read_optional_json(paths.meta_dir / "validation_report.json")
    if validation.get("status") == "failed":
        findings.append(
            ReviewFinding(
                key="validation:failed",
                severity="blocking",
                category="validation",
                summary="Static validation failed after applying the patch.",
                evidence=["code_task/meta/validation_report.json"],
                recommendation="Fix validation errors before treating benchmark results as meaningful.",
                source="code-task.rule-review",
            )
        )
    run_record = _run_record(manifest, "patched")
    if run_record and run_record.get("status") not in {"passed", "skipped"}:
        findings.append(
            ReviewFinding(
                key="benchmark:patched-failed",
                severity="warning",
                category="benchmark",
                summary=f"Patched benchmark status is `{run_record.get('status', 'unknown')}`.",
                evidence=["code_task/run/patched/report.md"],
                recommendation="Use failure analysis and repair memory before another proposal.",
                source="code-task.rule-review",
            )
        )
    if (paths.task_dir / "patch.diff").is_file() and (paths.task_dir / "patch.diff").stat().st_size == 0:
        findings.append(
            ReviewFinding(
                key="patch:empty-diff",
                severity="warning",
                category="patch",
                summary="patch.diff is empty even though review was requested.",
                evidence=["code_task/patch.diff"],
                recommendation="Verify whether the proposal was already applied or produced no effect.",
                source="code-task.rule-review",
            )
        )
    return findings





def _review_prompt(
    run_dir: Path,
    *,
    manifest: dict[str, Any],
    phase: str,
    changed_files: list[str],
    review_index: dict[str, Any],
    review_cluster: dict[str, Any],
    interface_mismatches: list[dict[str, Any]],
    local_api_contract: dict[str, list[str]],
    snippets: list[str],
) -> str:
    paths = code_task_paths(run_dir)
    return review_prompt(
        instructions=(
            "Review the applied patch for scope, interface compatibility, logic, tests, benchmark integrity, "
            "and repair risk. Use the full review index to understand project shape, but focus findings on "
            "the current review cluster and the supplied patch evidence. Do not request broad rewrites."
            " This review can precede validation. Missing test coverage or an unrun validation command "
            "belongs to category validation as an advisory request to run the authorized check, not "
            "evidence of a runtime defect. Distinguish absent results from recorded failed results."
        ),
        context={
            "phase": phase,
            "task": _read_optional_text(paths.task_dir / "task.md"),
            "task_memory": task_memory_context(run_dir),
            "changed_files": changed_files,
            "review_index": review_index,
            "review_cluster": review_cluster,
            "manifest_patch": manifest.get("patch", {}),
            "validation_report": _read_optional_json(paths.meta_dir / "validation_report.json"),
            "patched_run_record": _run_record(manifest, "patched"),
            "patch_diff": _clip(_read_optional_text(paths.task_dir / "patch.diff"), 9000),
            "local_api_contract": local_api_contract,
            "local_api_mismatches": interface_mismatches,
        },
        snippets=snippets,
    )





def _relative_meta_path(phase: str, kind: str) -> str:
    suffix = "" if phase == "post_apply" else f"_{phase}"
    return f"code_task/meta/{kind}{suffix}.json"


def _result_schema_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    benchmark = manifest.get("benchmark")
    benchmark = benchmark if isinstance(benchmark, dict) else {}
    primary = str(benchmark.get("primary_metric") or "score").strip() or "score"
    directions = benchmark.get("metric_directions")
    directions = directions if isinstance(directions, dict) else {}
    required = [primary]
    required.extend(str(key) for key in directions if str(key).strip() and str(key) not in required)
    return {
        "primary_metric": primary,
        "required_metrics": required,
        "metric_directions": {str(key): str(value) for key, value in directions.items()},
    }


def _contract_from_run(paths: Any) -> dict[str, Any]:
    contract = load_task_contract(paths.meta_dir)
    if contract:
        return contract
    task_text = _read_optional_text(paths.task_dir / "task.md")
    return {
        "objective": task_text[:2000],
        "task": task_text[:4000],
        "success_criteria": [],
    }


def _changed_files(manifest: dict[str, Any], paths: Any) -> list[str]:
    patch = manifest.get("patch")
    if isinstance(patch, dict):
        rows = [str(item) for item in patch.get("changed_files", []) if str(item).strip()]
        if rows:
            return _dedupe(rows)
    applied = _read_optional_json(paths.meta_dir / "applied_edits.json")
    return _dedupe(str(item) for item in applied.get("changed_files", []) if str(item).strip())


def _run_record(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    benchmark = manifest.get("benchmark")
    if not isinstance(benchmark, dict):
        return {}
    record = benchmark.get(name)
    return record if isinstance(record, dict) else {}


def _patch_status(manifest: dict[str, Any]) -> str:
    patch = manifest.get("patch")
    if isinstance(patch, dict):
        return str(patch.get("status", ""))
    return ""


def _read_optional_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = read_json(path)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _read_optional_text(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return read_text(path)
    except OSError:
        return ""


def _dedupe(values: Any) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).replace("\\", "/").strip()
        if text and text not in seen:
            seen.add(text)
            rows.append(text)
    return rows


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
