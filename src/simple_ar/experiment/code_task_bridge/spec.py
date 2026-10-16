from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from simple_ar.code_task import load_code_task_init_options
from simple_ar.code_task.runtime.config import (
    DEFAULT_MAX_FILE_BYTES,
    CodeTaskConfigError,
    code_task_metric_schema,
    load_code_task_execute_options,
)


CODE_TASK_TOY_SPAM_TEMPLATE = "llm_code_task_toy_spam"
CODE_TASK_PROJECT_TEMPLATE = "code_task_project"


@dataclass(frozen=True)
class CodeTaskExperimentSpec:
    """Source and runtime configuration for an embedded code-task experiment."""

    template: str
    code_root: Path | None
    task_file: Path | None
    benchmark_command: str | None
    primary_metric: str | None = None
    metric_directions: dict[str, str] = field(default_factory=dict)
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    workspace_mode: str = "auto"
    workspace_include: tuple[str, ...] = ()
    workspace_exclude: tuple[str, ...] = ()
    workspace_reuse_source_venv: bool = False
    env_mode: str = "current"
    python_executable: str | None = None
    edit_scope_mode: str | None = None
    edit_scope_allowed_patterns: tuple[str, ...] = ()
    edit_scope_protected_patterns: tuple[str, ...] = ()
    config_path: str | None = None
    name: str | None = None
    allow_test_changes: bool = False
    allow_large_edits: bool = False
    approval_note: str = "Auto-approved inside the isolated research-session workspace."
    kind: str = "existing_project"

    def result_schema(self) -> dict[str, object]:
        """Return the metric contract enforced by the Code-Task configuration."""
        return code_task_metric_schema(self.primary_metric, self.metric_directions)




def is_code_task_experiment_template(template: object) -> bool:
    return str(template) in {CODE_TASK_TOY_SPAM_TEMPLATE, CODE_TASK_PROJECT_TEMPLATE}




def code_task_project_spec(
    config: dict[str, object],
    *,
    task_file_override: Path | None = None,
    allow_greenfield: bool = False,
) -> CodeTaskExperimentSpec:
    """Resolve a generic user-project code-task spec from pipeline config."""

    config_path = _config_string(config.get("code_task_config"))
    try:
        options = load_code_task_init_options(
            config_path=config_path,
            code_root=_config_string(config.get("code_task_code_root")),
            task_file=_config_string(config.get("code_task_task_file")),
            output_root=None,
            name=_config_string(config.get("code_task_name")),
            benchmark_command=_config_string(config.get("code_task_benchmark_command")),
            max_file_bytes=_config_int(config.get("code_task_max_file_bytes")),
            workspace_mode=_config_string(config.get("code_task_workspace_mode")),
            workspace_reuse_source_venv=_config_bool(
                config.get("code_task_workspace_reuse_source_venv")
            ),
            env_mode=_config_string(config.get("code_task_env_mode")),
            python_executable=_config_string(config.get("code_task_python_executable")),
            primary_metric=_config_string(config.get("code_task_primary_metric")),
            metric_directions=_config_metric_directions(config.get("code_task_metric_directions")),
            require_task_file=False,
        )
    except CodeTaskConfigError as exc:
        raise RuntimeError(f"Invalid code-task experiment configuration: {exc}") from exc

    configured_allow_large_edits = _config_bool(config.get("safety_allow_large_edits"))
    if configured_allow_large_edits is None and config_path:
        try:
            configured_allow_large_edits = load_code_task_execute_options(
                config_path=config_path
            ).allow_large_edits
        except CodeTaskConfigError as exc:
            raise RuntimeError(f"Invalid code-task execute configuration: {exc}") from exc

    if options.kind == "greenfield" and not allow_greenfield:
        raise RuntimeError("Greenfield code projects use the shared research-session or standalone CodeTask, not the legacy experiment bridge.")
    task_file = task_file_override
    if task_file is None and options.task_file:
        task_file = _resolve_user_path(options.task_file)
    return CodeTaskExperimentSpec(
        template=CODE_TASK_PROJECT_TEMPLATE,
        code_root=_resolve_user_path(options.code_root) if options.code_root else None,
        kind=options.kind,
        task_file=task_file,
        benchmark_command=options.benchmark_command,
        primary_metric=options.primary_metric,
        metric_directions=options.metric_directions,
        max_file_bytes=options.max_file_bytes,
        workspace_mode=options.workspace_mode,
        workspace_include=options.workspace_include,
        workspace_exclude=options.workspace_exclude,
        workspace_reuse_source_venv=options.workspace_reuse_source_venv,
        env_mode=options.env_mode,
        python_executable=options.python_executable,
        edit_scope_mode=options.edit_scope_mode,
        edit_scope_allowed_patterns=options.edit_scope_allowed_patterns,
        edit_scope_protected_patterns=options.edit_scope_protected_patterns,
        config_path=options.config_path,
        name=options.name,
        allow_test_changes=False,
        allow_large_edits=configured_allow_large_edits or False,
    )




def _resolve_user_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (Path.cwd() / path)


def _config_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _config_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _config_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _config_metric_directions(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, str] = {}
    for key, direction in value.items():
        name = str(key).strip()
        if name:
            result[name] = str(direction)
    return result

