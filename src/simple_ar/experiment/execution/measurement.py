"""Measured-run identity and declared comparison-protocol compatibility."""

import hashlib
import json
from collections.abc import Mapping
from typing import Any, TYPE_CHECKING
from pathlib import Path

if TYPE_CHECKING:
    from simple_ar.research.experiment import ExperimentRequest


def representative_measurement_execution(contract: Mapping[str, Any]) -> Mapping[str, Any]:
    """Read one candidate execution for code preparation/generation, not a copy of its contract."""
    if 'conditions' not in contract:
        return contract
    candidates = [row['execution'] for row in contract['conditions'] if row['role'] == 'candidate']
    if not candidates or any(row.get('result_schema') != candidates[0].get('result_schema') for row in candidates):
        raise ValueError('A condition set needs candidates with one shared result_schema for code generation')
    return candidates[0]


def measurement_reuse_matches(
    payload: Mapping[str, Any], expected: "ExperimentRequest", *,
    same_session_result: bool = False,
    actual_source: Mapping[str, Any] | None = None,
    current_source: Mapping[str, Any] | None = None,
) -> bool:
    """Match canonical evidence to one declared request without owning its lifecycle.

    Callers verify result/attempt and preparation-reference ownership. This
    function checks their loaded domain facts and reads only declared protected
    files. Same-session reuse without those snapshots retains its limitations;
    it is not upgraded to independently verified asset integrity.
    """
    try:
        if not isinstance(payload, Mapping) or str(payload.get("status") or "").lower() != "passed":
            return False
        if (list(payload.get("command") or ()) != list(expected.run.command)
                or dict(payload.get("result_schema") or {}) != dict(expected.result_schema)):
            return False
        expected_contract = expected.normalized_experiment_contract()
        actual_contract = payload.get("experiment_contract")
        integrity = (payload.get("measurement") or {}).get("asset_integrity")
        if (not isinstance(expected_contract, Mapping) or not isinstance(actual_contract, Mapping)
                or comparable_protocol(actual_contract, observed_assets=integrity)
                != comparable_protocol(expected_contract, observed_assets=integrity)):
            return False
        preparation = payload.get("preparation")
        if current_source is not None:
            if (not isinstance(preparation, Mapping) or not isinstance(preparation.get("source_ref"), Mapping)
                    or not isinstance(actual_source, Mapping) or not isinstance(current_source, Mapping)):
                return False
            actual_project, current_project = actual_source.get("source_project"), current_source.get("source_project")
            if not isinstance(actual_project, str) or not isinstance(current_project, str):
                return False
            actual_project, current_project = Path(actual_project).resolve(), Path(current_project).resolve()
            if not actual_project.is_dir() or actual_project != current_project:
                return False
        elif preparation is not None or actual_source is not None:
            return False  # A claimed producing preparation cannot lose its current lineage.
        if (payload.get("validity_status") == "invalid"
                or isinstance(integrity, Mapping) and integrity.get("status") == "changed"):
            return False
        protected = expected_contract.get("protected_assets")
        if same_session_result and (not isinstance(protected, list) or not protected):
            return True
        if (not isinstance(protected, list) or not protected or not isinstance(integrity, Mapping)
                or integrity.get("status") != "observed_unchanged"):
            return False
        before = integrity.get("before")
        if not isinstance(before, Mapping) or not before:
            return False
        current = snapshot_protocol_assets(expected_contract, expected.run.cwd)
        # Copies may have different physical paths; protected identities and
        # original observed content, not those paths, bind this narrow check.
        return set(before) == set(current) and all(
            isinstance(before.get(asset_id), Mapping)
            and before[asset_id].get("sha256") == current[asset_id].get("sha256")
            for asset_id in before
        )
    except (OSError, KeyError, TypeError, ValueError, AttributeError, RuntimeError):
        return False


def snapshot_protocol_assets(contract: Mapping[str, Any] | None, cwd: Path) -> dict[str, dict[str, str]]:
    """Hash only explicitly named files; never recurse through a data directory."""
    snapshots = {}
    for asset in (contract or {}).get("protected_assets", []):
        asset_id, locator = asset.get("asset_id"), asset.get("path")
        if not isinstance(asset_id, str) or not asset_id or asset_id in snapshots:
            raise ValueError("Protected assets need unique non-empty asset_id values.")
        if not isinstance(locator, str) or not locator:
            raise ValueError(f"Protected asset {asset_id} requires a file path.")
        path = Path(locator)
        # Keep the named path so a symlink retarget is observed on the second
        # read, rather than checking only its original resolved target.
        path = (cwd / path).absolute() if not path.is_absolute() else path
        snapshots[asset_id] = {"path": str(path), "sha256": _file_hash(path)}
    return snapshots


def reconcile_protocol_assets(before: dict[str, dict[str, str]]) -> dict[str, Any]:
    after, errors = {}, {}
    for asset_id, snapshot in before.items():
        try:
            after[asset_id] = _file_hash(Path(snapshot["path"]))
        except OSError as exc:
            errors[asset_id] = str(exc)
    changed = [asset_id for asset_id, snapshot in before.items()
               if after.get(asset_id) != snapshot["sha256"]]
    fingerprint = hashlib.sha256(json.dumps(
        {key: value["sha256"] for key, value in before.items()}, sort_keys=True,
    ).encode()).hexdigest() if before else None
    return {"status": "changed" if changed else "observed_unchanged" if before else "not_checked",
            "before": before, "after": after, "errors": errors,
            "changed_assets": changed, "content_fingerprint": fingerprint}


def _file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def comparable_protocol(contract: Mapping[str, Any], *, observed_assets: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Project comparison conditions, excluding candidate identity and research prose."""
    protocol = {key: contract.get(key) for key in (
        "protocol_revision", "dataset_refs", "split_spec", "metric_specs",
        "comparison_conditions", "protected_assets",
    )}
    assets = protocol.get('protected_assets') or []
    if (observed_assets and observed_assets.get('status') == 'observed_unchanged'
            and observed_assets.get('content_fingerprint')
            and {row.get('asset_id') for row in assets} == set(observed_assets.get('before', {}))):
        # File locations bind physical execution; logical IDs and independently
        # observed content bind comparability across isolated candidate copies.
        protocol['protected_assets'] = [{key: value for key, value in row.items() if key != 'path'} for row in assets]
    return protocol


def _protocol_fingerprint(contract: Mapping[str, Any], result_schema: Mapping[str, Any],
                          *, observed_assets: Mapping[str, Any] | None = None) -> str | None:
    protocol = comparable_protocol(contract, observed_assets=observed_assets)
    if not all(protocol.get(key) for key in (
        "dataset_refs", "split_spec", "metric_specs", "comparison_conditions",
    )):
        return None
    return hashlib.sha256(json.dumps(
        {"protocol": protocol, "result_schema": dict(result_schema)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()


def measurement_record(
    run: Any, contract: Mapping[str, Any] | None, result_schema: Mapping[str, Any],
) -> dict[str, Any]:
    process = getattr(run, "process_record", {})
    protocol = dict(contract or {})
    fingerprint = _protocol_fingerprint(protocol, result_schema)
    return {
        "schema_version": "experiment_measurement.v1",
        "measurement_id": process.get("invocation_id"),
        "condition_id": getattr(run, "label", "experiment"),
        "source_kind": "measured" if process.get("invocation_id") else "unverified_backend",
        "protocol_id": protocol.get("contract_id"),
        "protocol_revision": protocol.get("protocol_revision"),
        "protocol_fingerprint": fingerprint,
        "protocol_status": "declared" if fingerprint else "incomplete",
        "seed": protocol.get("comparison_conditions", {}).get("seed"),
        "limitations": ["Protocol identity reflects declared settings, not verified dataset or evaluator contents."],
    }


def comparison_compatibility(baseline: Mapping, candidate: Mapping) -> tuple[str, str]:
    first, second = baseline.get("measurement"), candidate.get("measurement")
    if first is None and second is None:
        return "legacy_unverified", "Historical results have no measurement protocol metadata."
    if not isinstance(first, Mapping) or not isinstance(second, Mapping):
        return "unknown", "One result lacks measurement protocol metadata."
    a, b = first.get("protocol_fingerprint"), second.get("protocol_fingerprint")
    if not a or not b:
        return "unknown", "Comparison protocol is incomplete."
    # Re-project persisted contracts so old whole-contract fingerprints and new
    # condition-only fingerprints can be compared without rewriting history.
    contracts = (baseline.get("experiment_contract"), candidate.get("experiment_contract"))
    if any(isinstance(contract, Mapping) for contract in contracts):
        if not all(isinstance(contract, Mapping) for contract in contracts):
            return "unknown", "One result lacks its persisted comparison contract."
        a = _protocol_fingerprint(contracts[0], baseline.get("result_schema") or {}, observed_assets=first.get('asset_integrity'))
        b = _protocol_fingerprint(contracts[1], candidate.get("result_schema") or {}, observed_assets=second.get('asset_integrity'))
        if not a or not b:
            return "unknown", "Comparison protocol is incomplete."
    if a != b:
        return "mismatched", "Results use different declared protocols; deltas are descriptive only."
    placeholder_reason = _unverified_protocol_reason(baseline, candidate)
    if placeholder_reason:
        return "unknown", placeholder_reason
    if first.get("source_kind") != "measured" or second.get("source_kind") != "measured":
        return "unknown", "At least one result is not executor-observed measurement."
    if first.get("measurement_id") == second.get("measurement_id"):
        return "unknown", "The same physical measurement cannot be its own independent control."
    x, y = first.get("asset_integrity"), second.get("asset_integrity")
    if x or y:
        if not isinstance(x, Mapping) or not isinstance(y, Mapping):
            return "unknown", "Only one measurement has asset-integrity observations."
        if x["status"] == "changed" or y["status"] == "changed":
            return "mismatched", "A declared protected asset changed during execution."
        if x["content_fingerprint"] != y["content_fingerprint"]:
            return "mismatched", "Declared protected files differ between measurements."
        if x["status"] == y["status"] == "observed_unchanged":
            return "declared_match", "Declared protocols and explicitly checked file contents match at the observed boundaries."
    return "declared_match", "Declared protocols match; independent asset integrity is not yet verified."


def _unverified_protocol_reason(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any],
) -> str | None:
    """Reject framework placeholders as proof that two measurements are comparable.

    Prepared execution can supply a command boundary without declaring the
    dataset or split used by that command.  The resulting fingerprint is
    useful for lineage, but it cannot establish a scientific comparison.
    Explicit contracts with concrete split/dataset facts remain eligible even
    when no protected file was named.
    """

    for result in (baseline, candidate):
        contract = result.get("experiment_contract")
        if not isinstance(contract, Mapping):
            continue
        split = contract.get("split_spec")
        if isinstance(split, Mapping) and str(split.get("status") or "").strip().lower() == "not_declared_by_framework":
            return "Comparison protocol contains an execution-boundary placeholder split; comparability is unverified."
    return None
