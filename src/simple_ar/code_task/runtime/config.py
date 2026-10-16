from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from simple_ar.code_task.execution.baseline_policy import normalize_baseline_policy
from simple_ar.code_task.execution.comparison import normalize_metric_direction


DEFAULT_OUTPUT_ROOT = "runs"
DEFAULT_MAX_FILE_BYTES = 2_000_000
DEFAULT_ENV_MODE = "current"
DEFAULT_WORKSPACE_MODE = "auto"
DEFAULT_GREENFIELD_WORKSPACE_MODE = "empty"
CODE_TASK_KIND_EXISTING = "existing_project"
CODE_TASK_KIND_GREENFIELD = "greenfield"
CODE_TASK_KINDS = {CODE_TASK_KIND_EXISTING, CODE_TASK_KIND_GREENFIELD}
EXECUTE_STEPS = (
    "probe",
    "baseline",
    "work-plan",
    "batch",
    "plan",
    "propose-edits",
    "apply-edits",
    "review",
    "validate",
    "run",
    "analyze-failure",
    "repair",
)


class CodeTaskConfigError(RuntimeError):
    """Raised when a code-task config file or override is invalid."""


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CodeTaskSection(_ConfigModel):
    kind: str | None = None
    code_root: str | None = None
    task_file: str | None = None
    output_root: str | None = None
    name: str | None = None
    max_file_bytes: int | None = None


class BenchmarkSection(_ConfigModel):
    command: str | None = None
    timeout: int | None = None
    primary_metric: str | None = None
    metric_directions: dict[str, str] = Field(default_factory=dict)


class MetricsSection(_ConfigModel):
    primary: str | None = None
    primary_metric: str | None = None
    directions: dict[str, str] = Field(default_factory=dict)
    metric_directions: dict[str, str] = Field(default_factory=dict)


class EnvironmentSection(_ConfigModel):
    mode: str | None = None
    python: str | None = None
    python_executable: str | None = None
    required_paths: list[str] = Field(default_factory=list)


class WorkspaceSection(_ConfigModel):
    model_config = ConfigDict(extra="forbid")
    mode: str | None = None
    include: list[str] | None = None
    exclude: list[str] | None = None
    reuse_source_venv: bool | None = None


class SafetySection(_ConfigModel):
    max_file_bytes: int | None = None
    validation_max_file_bytes: int | None = None


class EditScopeSection(_ConfigModel):
    mode: str | None = None
    allowed_patterns: list[str] | None = None
    protected_patterns: list[str] | None = None


class ExecuteSection(_ConfigModel):
    to_step: str | None = None
    model: str | None = None
    use_llm: bool | None = None
    timeout_sec: int | None = None
    timeout: int | None = None
    skip_validation: bool | None = None
    strict_validation: bool | None = None
    validation_max_file_bytes: int | None = None
    stream_benchmark_output: str | bool | None = None
    baseline_policy: str | None = None
    baseline_metrics_file: str | None = None
    apply_proposed_edits: bool | None = None
    allow_large_edits: bool | None = None
    allow_planning_fallback: bool | None = None
    planning_mode: str | None = None
    planning_review_rounds: int | None = None
    llm_retry_attempts: int | None = None
    repair_rounds: int | None = None
    budget_profile: str | None = None
    max_batches: int | None = None
    cost_cap_usd: float | None = None
    max_files: int | None = None
    max_source_chars_per_file: int | None = None
    max_generated_lines: int | None = None
    ablation: "ExecuteAblationSection" = Field(default_factory=lambda: ExecuteAblationSection())


class ExecuteAblationSection(_ConfigModel):
    model_config = ConfigDict(extra="forbid")
    review_gate: str | None = None


class ImplementationSection(_ConfigModel):
    provider: str | None = None
    agent_mode: str | None = None
    allow_external_agent: bool | None = None
    agent_model: str | None = None
    agent_binary: str | None = None
    agent_args: list[str] | None = None
    agent_timeout_sec: int | None = None


class ResourceSection(_ConfigModel):
    max_runtime_sec: int | None = None
    max_files: int | None = None
    max_generated_lines: int | None = None
    max_memory_mb: int | None = None
    allow_gpu: bool | None = None


class LLMSection(_ConfigModel):
    enabled: bool | None = None
    model: str | None = None


class CodeTaskModelsSection(_ConfigModel):
    @model_validator(mode="before")
    @classmethod
    def reject_retired_stage_profiles(cls, value: Any) -> Any:
        if isinstance(value, dict) and set(value) & {
            "requirements", "architecture", "interfaces", "file_plan", "planning_review",
            "stages", "stage_profiles", "stage_routes",
        }:
            raise ValueError("Greenfield stage model routes are retired. Select one existing "
                             "connection with [models.code_task] planner = 'profile:NAME'; "
                             "remove stage selectors and set [execute] planning_mode = 'compact'.")
        return value

    default: str | None = None
    planner: str | None = None
    writer: str | None = None
    reviewer: str | None = None
    editor: str | None = None
    repair: str | None = None
    summarizer: str | None = None


class ModelsSection(_ConfigModel):
    default: str | None = None
    code_task: CodeTaskModelsSection = Field(default_factory=CodeTaskModelsSection)


class BudgetSection(BaseModel):
    model_config = ConfigDict(extra="allow")

    profile: str | None = None
    max_batches: int | None = None
    cost_cap_usd: float | None = None
    max_files: int | None = None
    max_edits: int | None = None
    max_old_chars: int | None = None
    max_new_chars: int | None = None
    max_total_edit_chars: int | None = None
    max_proposal_chars: int | None = None


class CodeTaskConfig(_ConfigModel):
    code_task: CodeTaskSection = Field(default_factory=CodeTaskSection)
    benchmark: BenchmarkSection = Field(default_factory=BenchmarkSection)
    metrics: MetricsSection = Field(default_factory=MetricsSection)
    environment: EnvironmentSection = Field(default_factory=EnvironmentSection)
    workspace: WorkspaceSection = Field(default_factory=WorkspaceSection)
    safety: SafetySection = Field(default_factory=SafetySection)
    edit_scope: EditScopeSection = Field(default_factory=EditScopeSection)
    execute: ExecuteSection = Field(default_factory=ExecuteSection)
    implementation: ImplementationSection = Field(default_factory=ImplementationSection)
    resource: ResourceSection = Field(default_factory=ResourceSection)
    llm: LLMSection = Field(default_factory=LLMSection)
    models: ModelsSection = Field(default_factory=ModelsSection)
    budget: BudgetSection = Field(default_factory=BudgetSection)


def code_task_metric_schema(primary_metric: str | None, metric_directions: dict[str, str]) -> dict[str, object]:
    """Project the declared metric contract once for current and compatibility consumers."""
    primary = str(primary_metric or '').strip()
    directions = {str(name): str(direction) for name, direction in metric_directions.items() if str(name).strip()}
    required = list(dict.fromkeys(([primary] if primary else []) + list(directions)))
    schema: dict[str, object] = {'required_metrics': required}
    if primary:
        schema['primary_metric'] = primary
    if directions:
        schema['metric_directions'] = directions
    return schema


@dataclass(frozen=True)
class CodeTaskInitOptions:
    """Resolved options for ``code-task init`` after config/CLI merging."""

    kind: str
    code_root: str | None
    task_file: str | None
    output_root: str
    name: str | None
    benchmark_command: str | None
    max_file_bytes: int
    workspace_mode: str
    workspace_include: tuple[str, ...]
    workspace_exclude: tuple[str, ...]
    workspace_reuse_source_venv: bool
    env_mode: str
    python_executable: str | None
    primary_metric: str | None
    metric_directions: dict[str, str]
    edit_scope_mode: str | None
    edit_scope_allowed_patterns: tuple[str, ...]
    edit_scope_protected_patterns: tuple[str, ...]
    config_path: str | None


@dataclass(frozen=True)
class CodeTaskExecuteOptions:
    """Resolved options for ``code-task execute`` from a TOML config.

    The config is intentionally optional. It keeps the common long-tail knobs
    out of the CLI happy path while preserving explicit CLI overrides in
    ``cli.py``.
    """

    to_step: str
    model: str | None
    planner_model: str | None
    writer_model: str | None
    reviewer_model: str | None
    editor_model: str | None
    repair_model: str | None
    summarizer_model: str | None
    use_llm: bool
    timeout_sec: int
    skip_validation: bool
    strict_validation: bool
    validation_max_file_bytes: int
    stream_benchmark_output: str
    baseline_policy: str
    baseline_metrics_file: str | None
    apply_proposed_edits: bool
    allow_large_edits: bool
    allow_planning_fallback: bool
    planning_mode: str
    planning_review_rounds: int
    llm_retry_attempts: int
    repair_rounds: int
    budget_profile: str | None
    edit_budget_overrides: dict[str, Any]
    max_batches: int | None
    cost_cap_usd: float | None
    max_files: int
    max_source_chars_per_file: int
    max_generated_lines: int
    review_gate: str
    implementation_provider: str
    implementation_agent_mode: str
    implementation_allow_external_agent: bool
    implementation_agent_model: str
    implementation_agent_binary: str
    implementation_agent_args: tuple[str, ...]
    implementation_agent_timeout_sec: int
    env_mode: str | None
    python_executable: str | None
    config_path: str | None


def parse_metric_direction_arg(value: str) -> tuple[str, str]:
    """Parse and normalize ``METRIC=DIRECTION`` strings."""
    separator = "=" if "=" in value else ":"
    if separator not in value:
        raise CodeTaskConfigError(
            "Expected METRIC=DIRECTION, for example accuracy=higher"
        )
    name, direction = value.split(separator, 1)
    name = name.strip()
    direction = direction.strip()
    if not name or not direction:
        raise CodeTaskConfigError("Expected non-empty METRIC and DIRECTION values")
    try:
        normalized = normalize_metric_direction(direction)
    except ValueError as exc:
        raise CodeTaskConfigError(str(exc)) from exc
    return name, normalized


def load_code_task_execute_options(
    *,
    config_path: str | None = None,
) -> CodeTaskExecuteOptions:
    """Resolve optional ``code-task execute`` settings from TOML.

    Supported sections:
    - ``[execute]`` for orchestration knobs.
    - ``[models.code_task]`` for planner/editor/repair/summarizer models.
    - ``[budget]`` plus optional ``[budget.normal]``/``[budget.large]`` for
      edit budget profile and caps.
    - ``[environment]`` for env mode and Python executable.
    """

    config = _load_toml_config(config_path)
    execute = config.execute
    ablation = execute.ablation
    benchmark = config.benchmark
    llm = config.llm
    models = config.models
    code_task_models = config.models.code_task
    budget = config.budget
    environment = config.environment
    safety = config.safety
    implementation = config.implementation
    resource = config.resource

    to_step = _config_string(execute.to_step) or "run"
    if to_step not in EXECUTE_STEPS:
        raise CodeTaskConfigError(
            "Unsupported [execute].to_step. Expected one of: "
            + ", ".join(EXECUTE_STEPS)
        )
    budget_profile = (
        _config_string(execute.budget_profile)
        or _config_string(budget.profile)
    )
    max_batches = _config_int(execute.max_batches)
    if max_batches is None:
        max_batches = _config_int(budget.max_batches)
    cost_cap = _config_float(execute.cost_cap_usd)
    if cost_cap is None:
        cost_cap = _config_float(budget.cost_cap_usd)

    return CodeTaskExecuteOptions(
        to_step=to_step,
        model=(
            _config_string(execute.model)
            or _config_string(code_task_models.default)
            or _config_string(models.default)
            or _config_string(llm.model)
        ),
        planner_model=_config_string(code_task_models.planner),
        writer_model=_config_string(code_task_models.writer),
        reviewer_model=_config_string(code_task_models.reviewer),
        editor_model=_config_string(code_task_models.editor),
        repair_model=_config_string(code_task_models.repair),
        summarizer_model=_config_string(code_task_models.summarizer),
        use_llm=_resolve_bool(
            override=None,
            value=execute.use_llm if execute.use_llm is not None else llm.enabled,
            default=True,
        ),
        timeout_sec=_positive_int(
            _config_int(execute.timeout_sec)
            or _config_int(execute.timeout)
            or _config_int(benchmark.timeout)
            or _config_int(resource.max_runtime_sec),
            60,
        ),
        skip_validation=_resolve_bool(
            override=None,
            value=execute.skip_validation,
            default=False,
        ),
        strict_validation=_resolve_bool(
            override=None,
            value=execute.strict_validation,
            default=False,
        ),
        validation_max_file_bytes=_positive_int(
            _config_int(execute.validation_max_file_bytes)
            or _config_int(safety.validation_max_file_bytes),
            500_000,
        ),
        stream_benchmark_output=_stream_output_mode(execute.stream_benchmark_output),
        baseline_policy=_baseline_policy(execute.baseline_policy),
        baseline_metrics_file=_config_string(execute.baseline_metrics_file),
        apply_proposed_edits=_resolve_bool(
            override=None,
            value=execute.apply_proposed_edits,
            default=False,
        ),
        allow_large_edits=_resolve_bool(
            override=None,
            value=execute.allow_large_edits,
            default=False,
        ),
        allow_planning_fallback=_resolve_bool(
            override=None,
            value=execute.allow_planning_fallback,
            default=False,
        ),
        planning_mode=_planning_mode(execute.planning_mode),
        planning_review_rounds=_non_negative_int(
            _config_int(execute.planning_review_rounds),
            0,
        ),
        llm_retry_attempts=_positive_int(_config_int(execute.llm_retry_attempts), 3),
        repair_rounds=_non_negative_int(_config_int(execute.repair_rounds), 0),
        budget_profile=budget_profile,
        edit_budget_overrides=_edit_budget_overrides(budget, budget_profile),
        max_batches=max_batches if max_batches and max_batches > 0 else None,
        cost_cap_usd=cost_cap if cost_cap is not None and cost_cap >= 0 else None,
        max_files=_positive_int(
            _config_int(execute.max_files)
            or _config_int(resource.max_files)
            or _config_int(budget.max_files),
            8,
        ),
        max_source_chars_per_file=_positive_int(
            _config_int(execute.max_source_chars_per_file),
            4000,
        ),
        max_generated_lines=_positive_int(
            _config_int(execute.max_generated_lines)
            or _config_int(resource.max_generated_lines),
            1600,
        ),
        review_gate=_review_gate_mode(ablation.review_gate),
        implementation_provider=_config_string(implementation.provider) or "local",
        implementation_agent_mode=_config_string(implementation.agent_mode),
        implementation_allow_external_agent=_resolve_bool(
            override=None,
            value=implementation.allow_external_agent,
            default=False,
        ),
        implementation_agent_model=_config_string(implementation.agent_model),
        implementation_agent_binary=_config_string(implementation.agent_binary),
        implementation_agent_args=tuple(
            str(item).strip()
            for item in (implementation.agent_args or [])
            if str(item).strip()
        ),
        implementation_agent_timeout_sec=_positive_int(
            _config_int(implementation.agent_timeout_sec),
            600,
        ),
        env_mode=_config_string(environment.mode),
        python_executable=(
            _config_string(environment.python)
            or _config_string(environment.python_executable)
        ),
        config_path=config_path,
    )


def load_code_task_init_options(
    *,
    config_path: str | None = None,
    kind: str | None = None,
    code_root: str | None = None,
    task_file: str | None = None,
    output_root: str | None = None,
    name: str | None = None,
    benchmark_command: str | None = None,
    max_file_bytes: int | None = None,
    workspace_mode: str | None = None,
    workspace_include: list[str] | tuple[str, ...] | None = None,
    workspace_exclude: list[str] | tuple[str, ...] | None = None,
    workspace_reuse_source_venv: bool | None = None,
    env_mode: str | None = None,
    python_executable: str | None = None,
    primary_metric: str | None = None,
    metric_directions: list[tuple[str, str]] | dict[str, str] | None = None,
    require_task_file: bool = True,
) -> CodeTaskInitOptions:
    """Resolve ``code-task init`` options from TOML config plus CLI overrides.

    Explicit arguments take precedence over the config file. The resulting
    object is intentionally small and maps directly to ``initialize_code_task``.
    ``require_task_file`` stays true for standalone code-task runs; embedded
    pipeline runs may generate a task file from earlier research artifacts.
    """
    config = _load_toml_config(config_path)
    code_task = config.code_task
    benchmark = config.benchmark
    metrics = config.metrics
    environment = config.environment
    workspace = config.workspace
    safety = config.safety
    edit_scope = config.edit_scope

    resolved_kind = _normalize_code_task_kind(kind or code_task.kind)
    resolved_code_root = _portable_path_string(
        _config_string(code_root) or _config_string(code_task.code_root)
    )
    resolved_task_file = _portable_path_string(
        _config_string(task_file) or _config_string(code_task.task_file)
    )
    if resolved_kind == CODE_TASK_KIND_EXISTING and not resolved_code_root:
        raise CodeTaskConfigError(
            "Missing code root. Pass --code-root or set [code_task].code_root."
        )
    if require_task_file and not resolved_task_file:
        raise CodeTaskConfigError(
            "Missing task file. Pass --task-file or set [code_task].task_file."
        )

    resolved_output_root = _portable_path_string(
        _config_string(output_root)
        or _config_string(code_task.output_root)
        or DEFAULT_OUTPUT_ROOT
    )
    resolved_name = _config_string(name) or _config_string(code_task.name)
    resolved_benchmark_command = _config_string(benchmark_command) or _config_string(
        benchmark.command
    )
    resolved_primary_metric = (
        _config_string(primary_metric)
        or _config_string(benchmark.primary_metric)
        or _config_string(metrics.primary)
        or _config_string(metrics.primary_metric)
    )
    resolved_env_mode = (
        _config_string(env_mode)
        or _config_string(environment.mode)
        or DEFAULT_ENV_MODE
    )
    resolved_python = _portable_path_string(
        _config_string(python_executable)
        or _config_string(environment.python)
        or _config_string(environment.python_executable)
    )
    resolved_workspace_mode = (
        _config_string(workspace_mode)
        or _config_string(workspace.mode)
        or (
            DEFAULT_GREENFIELD_WORKSPACE_MODE
            if resolved_kind == CODE_TASK_KIND_GREENFIELD
            else DEFAULT_WORKSPACE_MODE
        )
    )
    if resolved_kind == CODE_TASK_KIND_GREENFIELD and resolved_workspace_mode == "auto":
        resolved_workspace_mode = DEFAULT_GREENFIELD_WORKSPACE_MODE
    if resolved_kind == CODE_TASK_KIND_GREENFIELD and resolved_workspace_mode not in {
        "empty",
        "copy",
        "sparse_copy",
    }:
        raise CodeTaskConfigError(
            "Greenfield code-task runs support workspace.mode = empty, copy, or sparse_copy. "
            "Use empty unless you deliberately provide a scaffold code_root."
        )
    if resolved_kind == CODE_TASK_KIND_GREENFIELD and resolved_workspace_mode != "empty" and not resolved_code_root:
        raise CodeTaskConfigError(
            "Greenfield workspace modes other than empty require [code_task].code_root as a scaffold/source root."
        )
    resolved_reuse_source_venv = _resolve_bool(
        override=workspace_reuse_source_venv,
        value=workspace.reuse_source_venv,
        default=False,
    )
    resolved_workspace_include = _resolve_string_list(
        override=workspace_include,
        value=workspace.include,
    )
    resolved_workspace_exclude = _resolve_string_list(
        override=workspace_exclude,
        value=workspace.exclude,
    )

    return CodeTaskInitOptions(
        kind=resolved_kind,
        code_root=resolved_code_root,
        task_file=resolved_task_file,
        output_root=resolved_output_root,
        name=resolved_name,
        benchmark_command=resolved_benchmark_command,
        max_file_bytes=_resolve_max_file_bytes(
            override=max_file_bytes,
            code_task=code_task,
            safety=safety,
        ),
        workspace_mode=resolved_workspace_mode,
        workspace_include=resolved_workspace_include,
        workspace_exclude=resolved_workspace_exclude,
        workspace_reuse_source_venv=resolved_reuse_source_venv,
        env_mode=resolved_env_mode,
        python_executable=resolved_python,
        primary_metric=resolved_primary_metric,
        metric_directions=_merge_metric_directions(
            config=config,
            cli_values=metric_directions,
        ),
        edit_scope_mode=_config_string(edit_scope.mode),
        edit_scope_allowed_patterns=_config_string_list(edit_scope.allowed_patterns),
        edit_scope_protected_patterns=_config_string_list(edit_scope.protected_patterns),
        config_path=config_path,
    )


def _normalize_code_task_kind(value: object) -> str:
    text = (_config_string(value) or CODE_TASK_KIND_EXISTING).lower().replace("-", "_")
    aliases = {
        "existing": CODE_TASK_KIND_EXISTING,
        "existing_code": CODE_TASK_KIND_EXISTING,
        "existing_project": CODE_TASK_KIND_EXISTING,
        "project": CODE_TASK_KIND_EXISTING,
        "patch": CODE_TASK_KIND_EXISTING,
        "greenfield": CODE_TASK_KIND_GREENFIELD,
        "from_scratch": CODE_TASK_KIND_GREENFIELD,
        "new_project": CODE_TASK_KIND_GREENFIELD,
    }
    normalized = aliases.get(text, text)
    if normalized not in CODE_TASK_KINDS:
        raise CodeTaskConfigError(
            "[code_task].kind must be one of: "
            + ", ".join(sorted(CODE_TASK_KINDS))
        )
    return normalized


def _load_toml_config(path: str | None) -> CodeTaskConfig:
    if not path:
        return CodeTaskConfig()
    config_path = Path(_portable_path_string(path) or path)
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise CodeTaskConfigError(f"Config file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise CodeTaskConfigError(
            f"Could not parse TOML config {config_path}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise CodeTaskConfigError(f"Expected TOML table in config file: {config_path}")
    try:
        config = CodeTaskConfig.model_validate(data)
    except ValidationError as exc:
        raise CodeTaskConfigError(f"Invalid code-task config {config_path}: {exc}") from exc
    _resolve_local_references(config, config_path)
    return config


def _resolve_local_references(config: CodeTaskConfig, config_path: Path) -> None:
    """Expand case-local paths without changing legacy relative paths."""

    fields = (
        (config.code_task, "code_root"),
        (config.code_task, "task_file"),
        (config.code_task, "output_root"),
        (config.environment, "python"),
        (config.environment, "python_executable"),
        (config.benchmark, "command"),
        (config.execute, "baseline_metrics_file"),
    )
    case_dir = config_path.resolve().parent.as_posix()

    def expand(value: str) -> str:
        resolved = value.replace("{config_dir}", case_dir)
        if "${" in resolved:
            raise CodeTaskConfigError(
                f"Task resource paths must be declared in {config_path}; "
                "use {config_dir} or an explicit path, not .env variables."
            )
        return resolved

    for section, name in fields:
        value = getattr(section, name)
        if value is not None:
            setattr(section, name, expand(value))
    config.environment.required_paths = [expand(value) for value in config.environment.required_paths]
    missing_paths = [
        str(Path(value).expanduser().resolve())
        for value in config.environment.required_paths
        if not Path(value).expanduser().exists()
    ]
    if missing_paths:
        raise CodeTaskConfigError(
            f"Required path(s) not found for {config_path}: " + ", ".join(missing_paths)
        )


def _config_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _portable_path_string(value: object) -> str | None:
    """Normalize user-facing path separators without touching artifact IDs.

    Python on Windows accepts forward slashes, but POSIX systems treat
    backslashes as literal filename characters. Normalizing config/CLI path
    inputs here lets examples copied from Windows shells still resolve on
    Ubuntu, while all internal artifact paths remain POSIX-style elsewhere.
    """

    if value is None:
        return None
    return str(value).replace("\\", "/")


def _config_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _config_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _resolve_bool(*, override: bool | None, value: object, default: bool) -> bool:
    if override is not None:
        return override
    if isinstance(value, bool):
        return value
    return default


def _stream_output_mode(value: object) -> str:
    """Resolve benchmark output relay mode from TOML.

    ``true`` remains supported and maps to ``auto`` so older configs gain
    carriage-return progress support without changing their files.
    """

    if value is None:
        return "off"
    if isinstance(value, bool):
        return "auto" if value else "off"
    text = _config_string(value)
    if text is None:
        return "off"
    aliases = {
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
    normalized = aliases.get(text.lower())
    if normalized is None:
        raise CodeTaskConfigError(
            "Unsupported [execute].stream_benchmark_output. Expected boolean "
            "or one of: off, line, auto, summary."
        )
    return normalized


def _baseline_policy(value: str | None) -> str:
    try:
        return normalize_baseline_policy(_config_string(value))
    except ValueError as exc:
        raise CodeTaskConfigError(
            "Unsupported [execute].baseline_policy. Expected one of: "
            "auto, none, provided, run, skip."
        ) from exc


def _planning_mode(value: str | None) -> str:
    text = (_config_string(value) or "compact").lower().replace("-", "_")
    aliases = {
        "tool": "tool_agent",
        "tools": "tool_agent",
        "tool_agent": "tool_agent",
        "agent_tools": "tool_agent",
        "compact": "compact",
        "single": "compact",
        "single_call": "compact",
        "legacy": "compact",
    }
    normalized = aliases.get(text)
    if normalized is None:
        raise CodeTaskConfigError(
            "Unsupported [execute].planning_mode. Expected `tool_agent` or `compact`."
        )
    return normalized






def _review_gate_mode(value: str | None) -> str:
    try:
        return normalize_review_gate(value)
    except ValueError as exc:
        raise CodeTaskConfigError(
            "Unsupported [execute.ablation].review_gate. Expected `strict` or `runtime`."
        ) from exc


def normalize_review_gate(value: str | None) -> str:
    """One interpretation for configuration files and direct execution calls."""
    text = (value or "strict").strip().lower().replace("-", "_") or "strict"
    if text in {"strict", "full", "quality"}:
        return "strict"
    if text in {"runtime", "run", "execution", "safety", "safety_only", "runtime_only", "soft", "nonblocking"}:
        return "runtime"
    raise ValueError("review_gate must be `strict` or `runtime`")


def _resolve_string_list(
    *,
    override: list[str] | tuple[str, ...] | None,
    value: object,
) -> tuple[str, ...]:
    if override is not None:
        return _config_string_list(override)
    return _config_string_list(value)


def _config_string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value:
        text = _config_string(item)
        if text:
            result.append(text)
    return tuple(dict.fromkeys(result))


def _resolve_max_file_bytes(
    *,
    override: int | None,
    code_task: CodeTaskSection,
    safety: SafetySection,
) -> int:
    if override is not None:
        return override
    configured = _config_int(code_task.max_file_bytes)
    if configured is not None:
        return configured
    safety_value = _config_int(safety.max_file_bytes)
    if safety_value is not None:
        return safety_value
    return DEFAULT_MAX_FILE_BYTES


def _positive_int(value: int | None, default: int) -> int:
    if isinstance(value, int) and value > 0:
        return value
    return default


def _non_negative_int(value: int | None, default: int) -> int:
    if isinstance(value, int) and value >= 0:
        return value
    return default


def _edit_budget_overrides(
    budget: BudgetSection,
    profile: str | None,
) -> dict[str, Any]:
    """Return numeric edit budget overrides for the selected profile."""

    keys = {
        "max_files",
        "max_edits",
        "max_old_chars",
        "max_new_chars",
        "max_total_edit_chars",
        "max_proposal_chars",
    }
    values: dict[str, Any] = {}
    budget_data = budget.model_dump()
    budget_extra = dict(budget.model_extra or {})
    for key in keys:
        value = budget_data.get(key)
        if value is not None:
            values[key] = value
    selected_profile = (profile or _config_string(budget.profile) or "normal").strip().lower()
    profile_table = budget_extra.get(selected_profile)
    profile_data = profile_table if isinstance(profile_table, dict) else {}
    for key in keys:
        if key in profile_data:
            values[key] = profile_data[key]
    return values


def _merge_metric_directions(
    *,
    config: CodeTaskConfig,
    cli_values: list[tuple[str, str]] | dict[str, str] | None,
) -> dict[str, str]:
    directions: dict[str, str] = {}
    for table in (
        config.benchmark.metric_directions,
        config.metrics.directions,
        config.metrics.metric_directions,
    ):
        if isinstance(table, dict):
            directions.update(_normalize_direction_table(table))
    directions.update(_normalize_cli_directions(cli_values))
    return directions


def _normalize_direction_table(table: dict[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in table.items():
        name = str(key).strip()
        if not name:
            continue
        try:
            result[name] = normalize_metric_direction(str(value))
        except ValueError as exc:
            raise CodeTaskConfigError(str(exc)) from exc
    return result


def _normalize_cli_directions(
    value: list[tuple[str, str]] | dict[str, str] | None,
) -> dict[str, str]:
    if not value:
        return {}
    if isinstance(value, dict):
        return _normalize_direction_table(value)
    return _normalize_direction_table(dict(value))
