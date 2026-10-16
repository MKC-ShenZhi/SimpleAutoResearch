"""Translate a bounded execution specification to the existing runner."""

from collections.abc import Mapping, Sequence
from dataclasses import fields
from pathlib import Path
from typing import Any

from simple_ar.experiment.execution.backend import RunRequest
from simple_ar.research.experiment import ExperimentRequest
from simple_ar.research.contracts import ResearchExperimentContract
from simple_ar.research.implementation import ImplementationRequest
from simple_ar.code_task.editing.budget import VALID_BUDGET_PROFILES
from simple_ar.code_task.runtime.config import CodeTaskInitOptions, CodeTaskExecuteOptions
from simple_ar.code_task.execution.environment import resolve_code_task_command
from simple_ar.result_analysis.metrics import normalize_direction


def code_task_execution_options(init: CodeTaskInitOptions, execute: CodeTaskExecuteOptions) -> dict[str, Any]:
    """Translate canonical options to the shared preparation/implementation contract."""
    return {
        'kind': init.kind, 'code_root': str(Path(init.code_root).resolve()) if init.code_root is not None else None,
        'workspace_mode': init.workspace_mode, 'max_file_bytes': init.max_file_bytes,
        'workspace_include': list(init.workspace_include), 'workspace_exclude': list(init.workspace_exclude),
        'workspace_reuse_source_venv': init.workspace_reuse_source_venv,
        'env_mode': init.env_mode, 'python_executable': init.python_executable,
        'approval_note': 'Auto-approved inside the isolated research-session workspace.',
        'max_repairs': execute.repair_rounds,
        'allowed_patterns': list(init.edit_scope_allowed_patterns),
        'protected_patterns': list(init.edit_scope_protected_patterns),
        'budget_profile': execute.budget_profile, 'edit_budget_overrides': execute.edit_budget_overrides,
        'allow_large_edits': execute.allow_large_edits,
    }


def analysis_contract_context(
    *, task_id: str, objective: str, contract: Mapping[str, Any],
    design: Mapping[str, Any], schema: Mapping[str, Any],
    hard_constraints: Sequence[str],
) -> dict[str, Any]:
    """Project selected, declared conditions without reading or persisting state."""
    context: dict[str, Any] = {
        "task_id": task_id, "title": objective, "research_question": objective,
    }
    contract = dict(contract)
    if contract:
        selected = design.get("selected_idea")
        selected_id = (
            selected.get("idea_id")
            if isinstance(selected, Mapping) and selected.get("idea_id")
            else contract.get("contract_id") or "research-hypothesis"
        )
        hypothesis = str(contract.get("hypothesis") or "").strip()
        if hypothesis:
            context["hypotheses"] = [{
                "id": str(selected_id),
                "statement": hypothesis,
                "metric_refs": list(contract.get("metrics") or []),
                "evidence": [
                    {"source": str(ref)}
                    for ref in contract.get("motivation_refs", [])
                    if str(ref).strip()
                ],
                "expected_outcome": str(contract.get("expected_outcome") or ""),
            }]
        if hard_constraints:
            contract["hard_constraints"] = list(hard_constraints)
        context["task_contract"] = contract

    names: list[str] = []
    primary = str(schema.get("primary_metric") or "").strip()
    if primary:
        names.append(primary)
    required = schema.get("required_metrics")
    if isinstance(required, (list, tuple)):
        names.extend(str(name).strip() for name in required if str(name).strip())
    for row in contract.get("metric_specs", []):
        if isinstance(row, Mapping) and str(row.get("name") or "").strip():
            names.append(str(row["name"]).strip())
    names.extend(str(name).strip() for name in contract.get("metrics", []) if str(name).strip())
    names = list(dict.fromkeys(names))
    raw_directions = schema.get("metric_directions")
    raw_directions = raw_directions if isinstance(raw_directions, Mapping) else {}
    if names:
        context["expected_metrics"] = [
            {
                "name": name,
                "direction": normalize_direction(raw_directions.get(name)),
            }
            for name in names
        ]
    directions = {
        str(name): normalize_direction(value)
        for name, value in raw_directions.items()
        if str(name).strip()
    }
    if directions:
        context["metric_directions"] = directions
    return context


def validate_execution_declaration(config: Mapping) -> tuple[tuple[Mapping, ...], str]:
    """Validate all supplied declarations without requiring or expanding argv.

    Return explicit pairs and the baseline policy; do not mutate configuration
    or expand seed_count. Explicit seeds retain precedence over a valid count.
    """
    if not isinstance(config, Mapping):
        raise ValueError("Provide execution with command (argv), absolute cwd and timeout_sec.")
    pairs = _validated_pairs(config.get("pairs", ()))
    if pairs and any(key in config for key in ("seeds", "seed_count", "seed_flag")):
        raise ValueError("Use execution.pairs or compact seed settings, not both.")
    if "seeds" in config:
        _seed_list(config["seeds"], "execution.seeds")
    protocol = config.get("protocol")
    comparison = protocol.get("comparison_conditions") if isinstance(protocol, Mapping) else None
    if isinstance(comparison, Mapping) and "seeds" in comparison:
        _seed_list(comparison["seeds"], "execution.protocol.comparison_conditions.seeds")
    if "seed_count" in config and (type(config["seed_count"]) is not int or config["seed_count"] < 1):
        raise ValueError("execution.seed_count must be a positive integer.")
    if "seed_flag" in config and (not isinstance(config["seed_flag"], str) or not config["seed_flag"].strip()):
        raise ValueError("execution.seed_flag must be a non-empty string.")
    if "baseline_ref" in config:
        refs = config['baseline_ref']
        refs = refs if isinstance(refs, (list, tuple)) else [refs]
        if not refs or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
            raise ValueError("execution.baseline_ref must be a non-empty artifact path or list of paths.")
    if "baseline_policy" in config:
        raw = config["baseline_policy"]
        if not isinstance(raw, str) or raw.strip().lower() not in {"run", "skip", "reuse"}:
            raise ValueError("execution.baseline_policy must be run, skip or reuse.")
        return pairs, raw.strip().lower()
    # Pairs or an explicit baseline are a comparison request; otherwise skip.
    return pairs, "run" if pairs or "baseline" in config else "skip"


def normalize_execution_config(
    config: Mapping,
    *,
    # Kept as a source-compatible no-op for callers that supplied the old
    # keyword. Natural-language interpretation belongs to planning/design, not
    # this deterministic argv adapter.
    task_text: str = "",
) -> dict[str, Any]:
    """Bind a compact declared protocol to literal execution pairs.

    This is the only expansion point for seed-aware execution. It binds a
    declared flag/value in an already validated argv; it never parses shell
    text or turns a model suggestion into an executable command.
    """

    pairs, baseline_policy = validate_execution_declaration(config)
    normalized = dict(config)

    del task_text
    seed_values, seed_reason = _declared_seed_values(normalized)
    if seed_values and not pairs:
        seed_flag = normalized.get("seed_flag", "")
        if not seed_flag:
            raise ValueError(
                "Seed-aware execution requires an explicit execution.seed_flag; "
                "the task text cannot authorize command inference."
            )
        command = _argv(normalized.get("command"), "execution.command")
        baseline = normalized.get("baseline")
        baseline_command = command
        if isinstance(baseline, Mapping) and "command" in baseline:
            baseline_command = _argv(baseline.get("command"), "execution.baseline.command")
        pairs = tuple(
            {
                "seed": seed,
                "baseline_command": _with_seed(baseline_command, seed_flag.strip(), seed),
                "candidate_command": _with_seed(command, seed_flag.strip(), seed),
            }
            for seed in seed_values
        )
        normalized["pairs"] = [dict(row) for row in pairs]
        normalized["protocol_seed_reason"] = seed_reason
        normalized["protocol_seed_flag"] = seed_flag.strip()
    elif pairs:
        normalized["pairs"] = [dict(row) for row in pairs]
        normalized.setdefault(
            "protocol_seed_reason",
            "Paired execution conditions were explicitly declared in the accepted protocol.",
        )
    else:
        normalized.pop("pairs", None)

    # A fixed command can declare its seed too; retaining the flag does not
    # authorize extra seed conditions or convert it into a paired experiment.
    if normalized.get("seed_flag"):
        normalized["protocol_seed_flag"] = normalized["seed_flag"]

    for key in ("seeds", "seed_count", "seed_flag"):
        normalized.pop(key, None)
    normalized["baseline_policy"] = baseline_policy
    if baseline_policy == "run" and not pairs and "baseline" not in normalized:
        # A resolved run decision needs an executable control.  When no
        # separate evaluator was supplied, the inspected/configured command
        # is the only authorized same-condition evaluator; use it before any
        # candidate implementation.  If it is missing or malformed, leave
        # the input missing so execution_request raises the normal boundary
        # error instead of inventing a command.
        command = normalized.get("command")
        if isinstance(command, (list, tuple)) and command and all(
            isinstance(arg, str) and arg for arg in command
        ):
            normalized["baseline"] = {
                "command": list(command),
                "label": "baseline",
            }
    if "protocol_seed_reason" not in normalized:
        normalized["protocol_seed_reason"] = seed_reason
    return normalized


def execution_pairs(config: Mapping, *, task_text: str = "") -> tuple[Mapping, ...]:
    """Return the bounded literal pairs consumed by the existing runner."""

    return tuple(normalize_execution_config(config, task_text=task_text).get("pairs", ()))


def _command_seed(command: list[str], flag: str) -> tuple[int | None, int | None]:
    """Read only the caller-declared seed flag, without executing or guessing argv."""
    indices = [i for i, arg in enumerate(command) if arg == flag or arg.startswith(flag + "=")]
    if not indices:
        return None, None
    if len(indices) != 1:
        raise ValueError(f"Ambiguous repeated seed flag: {flag}")
    index = indices[0]
    value = command[index].split("=", 1)[1] if command[index] != flag else (
        command[index + 1] if index + 1 < len(command) else ""
    )
    try:
        return index, int(value)
    except ValueError as exc:
        raise ValueError(f"Seed flag {flag} requires an integer value.") from exc


def _with_seed(command: list[str], flag: str, seed: int) -> list[str]:
    index, _ = _command_seed(command, flag)
    result = list(command)
    if index is None:
        return [*result, flag, str(seed)]
    if result[index] == flag:
        result[index + 1] = str(seed)
    else:
        result[index] = f"{flag}={seed}"
    return result


def _validated_pairs(rows: object) -> tuple[Mapping, ...]:
    """Validate explicit paired argv without interpreting command text."""

    if not isinstance(rows, (list, tuple)):
        raise ValueError("execution.pairs must be an explicit list.")
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"seed", "baseline_command", "candidate_command"}:
            raise ValueError("Each execution pair requires seed, baseline_command and candidate_command.")
        seed = row["seed"]
        if type(seed) is not int or seed in seen:
            raise ValueError("Execution pair seeds must be unique integers.")
        seen.add(seed)
        for name in ("baseline_command", "candidate_command"):
            _argv(row[name], name)
    return tuple(rows)


def _argv(value: object, name: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value or any(
        not isinstance(arg, str) or not arg for arg in value
    ):
        raise ValueError(f"{name} must be a non-empty argv list, not shell text.")
    return list(value)


def _declared_seed_values(config: Mapping) -> tuple[tuple[int, ...], str]:
    explicit = config.get("seeds")
    if explicit is not None:
        return _seed_list(explicit, "execution.seeds"), "Seeds were explicitly declared in execution.seeds."
    protocol = config.get("protocol")
    comparison = protocol.get("comparison_conditions") if isinstance(protocol, Mapping) else None
    if isinstance(comparison, Mapping) and "seeds" in comparison:
        values = _seed_list(comparison["seeds"], "execution.protocol.comparison_conditions.seeds")
        if config.get("seed_flag"):
            return values, "Seeds and their command binding were explicitly declared in the execution contract."
        if "seed_count" not in config:
            # Protocol describes experimental conditions, not how often to
            # invoke a process. A supplied evaluator may handle a whole seed
            # batch internally. Without a command binding, preserve literal argv.
            return (), "Protocol declares seed conditions; the fixed command is retained without per-seed expansion."
    count = config.get("seed_count")
    if count is not None:
        return tuple(range(count)), f"Seeds defaulted to 0..{count - 1} from execution.seed_count."
    return (), "No repeated seed condition was declared; the single configured execution is retained."


def _seed_list(value: object, name: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{name} must be a non-empty list of unique integers.")
    result: list[int] = []
    for item in value:
        if type(item) is not int or item in result:
            raise ValueError(f"{name} must contain unique integers.")
        result.append(item)
    return tuple(result)


def execution_protocol(config: Mapping, *, task_text: str = "") -> dict[str, Any]:
    """Return a small explainable protocol projection for work-plan views."""

    normalized = normalize_execution_config(config, task_text=task_text)
    pairs = tuple(normalized.get("pairs", ()))
    seeds = [int(row["seed"]) for row in pairs]
    if not seeds:
        flag = str(normalized.get("protocol_seed_flag") or "").strip()
        if flag:
            _, seed = _command_seed(_argv(normalized.get("command"), "execution.command"), flag)
            if seed is not None:
                seeds = [seed]
    if not seeds:
        protocol = normalized.get("protocol")
        comparison = protocol.get("comparison_conditions") if isinstance(protocol, Mapping) else None
        if isinstance(comparison, Mapping) and type(comparison.get("seed")) is int:
            seeds = [int(comparison["seed"])]
        elif isinstance(comparison, Mapping) and "seeds" in comparison:
            seeds = list(_seed_list(comparison["seeds"], "execution.protocol.comparison_conditions.seeds"))
    return {
        "condition_count": len(pairs) if pairs else 1,
        "seeds": seeds,
        "seed_reason": str(normalized.get("protocol_seed_reason") or "No repeated seed condition was declared."),
        "baseline_policy": str(normalized.get("baseline_policy") or "skip"),
        "paired": bool(pairs),
    }


def merge_execution_protocol(
    config: Mapping,
    protocol: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Apply a validated design proposal without replacing expert inputs.

    The design boundary may fill an omitted command, seed parameter or
    comparison policy. Explicit application configuration remains authoritative;
    this function only translates the small accepted protocol into the existing
    execution shape consumed by :func:`normalize_execution_config`.
    """

    merged = dict(config)
    if not isinstance(protocol, Mapping):
        return merged
    compact_keys = {"seeds", "seed_count", "seed_flag"}
    for key in (
        "command", "cwd", "timeout_sec", "result_schema", "pairs", "seeds",
        "seed_count", "seed_flag", "baseline_policy", "baseline_ref",
    ):
        if key == "pairs" and any(item in merged for item in compact_keys):
            continue
        if key in compact_keys and "pairs" in merged:
            continue
        if key not in merged and key in protocol:
            merged[key] = protocol[key]
    if "baseline" not in merged and "baseline_command" in protocol:
        merged["baseline"] = {
            "command": protocol["baseline_command"],
            "label": "baseline",
        }
    if "protocol" not in merged and isinstance(protocol.get("contract"), Mapping):
        merged["protocol"] = dict(protocol["contract"])
    return merged


def _merge_protocol_contract(
    configured: object,
    design: ResearchExperimentContract | Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Combine design facts with explicit execution protocol fields."""

    if design is None:
        return dict(configured) if isinstance(configured, Mapping) else configured
    if isinstance(design, ResearchExperimentContract):
        merged = design.to_row()
    elif isinstance(design, Mapping):
        merged = dict(design)
    else:
        raise ValueError("The research design contract must be an object.")
    if configured is not None:
        if not isinstance(configured, Mapping):
            raise ValueError("execution.protocol must be a research experiment contract object.")
        for key, value in configured.items():
            if (
                key in {"comparison_conditions", "split_spec", "resource_budget"}
                and isinstance(value, Mapping)
                and isinstance(merged.get(key), Mapping)
            ):
                merged[key] = {**dict(merged[key]), **dict(value)}
            else:
                merged[key] = value
    return merged


def execution_request(
    config: object,
    *,
    condition: str = "candidate",
    pair_index: int | None = None,
    task_text: str = "",
    contract: ResearchExperimentContract | Mapping[str, Any] | None = None,
) -> ExperimentRequest:
    """Build a runner request; do not infer shell commands or launch a process."""

    if not isinstance(config, Mapping):
        raise ValueError("Provide execution with command (argv), absolute cwd and timeout_sec.")
    config = normalize_execution_config(config, task_text=task_text)
    if "environment" in config:
        from simple_ar.research.project_environment import environment_profile
        environment_profile(config.pop("environment"), project=Path(str(config.get("cwd", ""))))
    pairs = tuple(config.get("pairs", ()))
    config.pop("pairs", None)
    code_task = config.pop("code_task", None)  # Consumed by the separate implementation action.
    environment = code_task if isinstance(code_task, Mapping) else None
    config.pop("baseline_policy", None)
    config.pop("baseline_ref", None)
    config.pop("protocol_seed_reason", None)
    seed_flag = str(config.pop("protocol_seed_flag", "") or "").strip()
    if "baseline" in config:
        baseline = config.pop("baseline")
        if not isinstance(baseline, Mapping) or "command" not in baseline:
            raise ValueError("execution.baseline must provide an explicit command.")
        if condition == "baseline":
            config.update(baseline)
            config["label"] = baseline.get("label", "baseline")
        else:
            config.setdefault("label", "candidate")
    if pairs:
        if pair_index is not None and not 0 <= pair_index < len(pairs):
            raise ValueError(f"Execution pair index {pair_index} is outside the accepted protocol.")
        pair = pairs[0 if pair_index is None else pair_index]
        role = "baseline" if condition == "baseline" else "candidate"
        config["command"] = pair[f"{role}_command"]
        config["label"] = f"{role}:seed={pair['seed']}"
        raw_protocol = config.get("protocol")
        if raw_protocol is not None and not isinstance(raw_protocol, Mapping):
            raise ValueError("execution.protocol must be a research experiment contract object.")
        protocol = dict(raw_protocol or {})
        comparison = protocol.get("comparison_conditions")
        if comparison is not None and not isinstance(comparison, Mapping):
            raise ValueError("execution.protocol.comparison_conditions must be an object.")
        protocol["comparison_conditions"] = {**dict(comparison or {}), "seed": pair["seed"]}
        config["protocol"] = protocol
    unknown = set(config) - {"command", "cwd", "timeout_sec", "result_schema", "label", "protocol"}
    if unknown:
        raise ValueError(f"Unsupported execution settings: {', '.join(sorted(unknown))}")
    command = config.get("command")
    if not isinstance(command, (list, tuple)) or not command or any(
        not isinstance(arg, str) or not arg for arg in command
    ):
        raise ValueError("execution.command must be a non-empty argv list, not shell text.")
    if environment is not None:
        command = resolve_code_task_command(
            command,
            env_mode=str(environment.get("env_mode") or "current"),
            python_executable=environment.get("python_executable"),
        )
    cwd = Path(str(config.get("cwd", "")))
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ValueError("execution.cwd must be an existing absolute directory.")
    timeout = config.get("timeout_sec")
    if type(timeout) is not int or timeout < 1:
        raise ValueError("execution.timeout_sec must be a positive integer.")
    schema = config.get("result_schema", {})
    if not isinstance(schema, Mapping):
        raise ValueError("execution.result_schema must be an object.")
    protocol = _merge_protocol_contract(config.get("protocol"), contract)
    if seed_flag:
        _, seed = _command_seed(list(command), seed_flag)
        if seed is not None:
            protocol = dict(protocol or {})
            conditions = dict(protocol.get("comparison_conditions") or {})
            if "seed" in conditions and conditions["seed"] != seed:
                raise ValueError("Declared protocol seed conflicts with the execution command.")
            protocol["comparison_conditions"] = {**conditions, "seed": seed}
    if protocol is not None and not isinstance(protocol, Mapping):
        raise ValueError("execution.protocol must be a research experiment contract object.")
    if protocol is not None:
        unsupported = set(protocol) - {item.name for item in fields(ResearchExperimentContract)}
        if unsupported:
            raise ValueError(f"Unsupported protocol fields: {', '.join(sorted(unsupported))}")
    try:
        parsed_contract = ResearchExperimentContract.from_row(protocol) if protocol is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid execution.protocol: {exc}") from exc
    return ExperimentRequest(
        run=RunRequest(list(command), cwd, timeout, label=str(config.get("label", "experiment"))),
        result_schema=dict(schema),
        experiment_contract=parsed_contract,
    )


def implementation_request(
    config: Mapping,
    client: object,
    *,
    validate: bool = False,
    task_text: str = "",
    revision_instruction: str = "",
    contract: ResearchExperimentContract | Mapping[str, Any] | None = None,
    require_validation: bool = False,
) -> ImplementationRequest:
    task = config.get("code_task")
    if not isinstance(task, Mapping) or set(task) - {
        "run_dir", "approval_note", "max_repairs", "budget_profile", "allow_large_edits", "edit_budget_overrides",
        "env_mode", "python_executable", "validation_command", "validation_timeout_sec",
    }:
        raise ValueError(
            "execution.code_task accepts run_dir, approval_note, max_repairs, "
            "budget_profile, edit_budget_overrides, allow_large_edits, env_mode, python_executable, validation_command and validation_timeout_sec."
        )
    repair_limit(config)
    run_dir = Path(str(task.get("run_dir", "")))
    note = task.get("approval_note")
    if not run_dir.is_absolute() or not (run_dir / "manifest.json").is_file():
        raise ValueError("code_task.run_dir must be an absolute, initialized CodeTask run.")
    if not isinstance(note, str) or not note.strip():
        raise ValueError("code_task.approval_note must explicitly authorize isolated edits.")
    budget_profile = task.get("budget_profile")
    if budget_profile is not None:
        if not isinstance(budget_profile, str) or budget_profile.strip().lower() not in VALID_BUDGET_PROFILES:
            raise ValueError("code_task.budget_profile must be normal, large or absolute.")
        budget_profile = budget_profile.strip().lower()
    allow_large_edits = task.get("allow_large_edits", False)
    from simple_ar.code_task.editing.budget import validated_edit_budget_overrides
    overrides = validated_edit_budget_overrides(task.get("edit_budget_overrides", {}))
    if not isinstance(allow_large_edits, bool):
        raise ValueError("code_task.allow_large_edits must be a boolean.")
    execution = execution_request(config, task_text=task_text, contract=contract)
    command, timeout = code_task_validation(config, required=require_validation)
    if command is None and validate:
        command, timeout = tuple(execution.run.command), execution.run.timeout_sec
    return ImplementationRequest(
        run_dir,
        execution.run.cwd,
        note,
        client,
        execution.normalized_experiment_contract(),
        validation_command=command,
        validation_timeout_sec=timeout,
        max_repairs=repair_limit(config) if command else 0,
        revision_instruction=revision_instruction.strip(),
        budget_profile=budget_profile,
        edit_budget_overrides=overrides,
        allow_large_edits=allow_large_edits,
    )


def code_task_validation(config: Mapping, *, required: bool = False) -> tuple[tuple[str, ...] | None, int | None]:
    task = config.get("code_task", {})
    if not isinstance(task, Mapping):
        raise ValueError("execution.code_task must be an explicit authorized mapping.")
    command = task.get("validation_command")
    timeout = task.get("validation_timeout_sec")
    if command is None and timeout is None and not required:
        return None, None
    if not isinstance(command, (list, tuple)) or not command or any(
        not isinstance(item, str) or not item.strip() for item in command
    ) or type(timeout) is not int or timeout <= 0:
        raise ValueError("CodeTask requires an independent validation_command argv and positive validation_timeout_sec.")
    policy = dict(env_mode=str(task.get("env_mode") or "current"), python_executable=task.get("python_executable"))
    command = tuple(resolve_code_task_command(command, **policy))
    if required:
        protocol = config.get("protocol")
        if not isinstance(protocol, Mapping) or any(not str(protocol.get(key) or "").strip()
                for key in ("hypothesis", "dataset", "expected_outcome")):
            raise ValueError("Reproduction preparation requires a user-confirmed fixed protocol.")
        if policy["env_mode"] not in {"current", "external"}:
            raise ValueError("Reproduction CodeTask requires current/external Python.")
        if "environment" in config:
            from simple_ar.research.project_environment import environment_profile
            profile = environment_profile(config["environment"])
            if profile["mode"] == "venv" and (policy["env_mode"] != "current" or task.get("python_executable")):
                raise ValueError("Reproduction installation requires an explicit venv and an initially current CodeTask interpreter.")
            if profile.get("check_command") and tuple(resolve_code_task_command(profile['check_command'], **policy)) == tuple(resolve_code_task_command(config['command'], **policy)):
                raise ValueError('Preparation checks must not run the formal scientific command.')
            if profile["mode"] == "venv" and any(argv[0] not in {"python", "python3"} for argv in (task["validation_command"], config["command"])):
                raise ValueError("CodeTask venv requires python/python3 aliases for both checker and formal command.")
        if command == tuple(resolve_code_task_command(config["command"], **policy)):
            raise ValueError("Reproduction validation_command must differ from the formal command.")
    return command, timeout


def repair_limit(config: Mapping) -> int:
    task = config.get("code_task", {})
    value = task.get("max_repairs", 0) if isinstance(task, Mapping) else 0
    if type(value) is not int or value < 0:
        raise ValueError("code_task.max_repairs must be a non-negative integer.")
    return value


__all__ = [
    "code_task_execution_options",
    "execution_pairs", "execution_protocol", "execution_request", "implementation_request", "code_task_validation",
    "merge_execution_protocol", "normalize_execution_config", "repair_limit",
]
