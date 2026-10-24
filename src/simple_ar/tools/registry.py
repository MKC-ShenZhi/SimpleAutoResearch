from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from simple_ar.tools.specs import CommonToolSpec, PermissionLevel, RiskLevel


@dataclass
class ToolRegistry:
    """In-memory registry that composes domain tools without owning their logic."""

    _specs: dict[str, CommonToolSpec] = field(default_factory=dict)
    _owners: dict[str, object] = field(default_factory=dict)
    _request_visible: set[str] = field(default_factory=set)

    def register_package(self, specs: Iterable[CommonToolSpec], owner: object, *,
                         request_visible: bool | Iterable[str] = False) -> None:
        """Bind contracts and dispatch together, for built-ins and local packages."""
        if not callable(getattr(owner, 'call', None)):
            raise ValueError('Package owner requires the common call contract')
        specs = list(specs)
        names = [spec.name for spec in specs]
        if len(set(names)) != len(names) or any(name in self._specs for name in names):
            raise ValueError('Package tool name collision')
        visible = set(names) if request_visible is True else set() if request_visible is False else set(request_visible)
        if not visible.issubset(names):
            raise ValueError('Request-visible tools must belong to this package')
        for spec in specs:
            if spec.domain in self._owners and self._owners[spec.domain] is not owner:
                raise ValueError('Package domain collision: ' + spec.domain)
        for spec in specs:
            self.register(spec)
            self._owners[spec.domain] = owner
        self._request_visible.update(visible)

    def domain_gateways(self) -> dict[str, object]:
        return dict(self._owners)

    def request_visible(self, name: str) -> bool:
        return name in self._request_visible

    def register(self, spec: CommonToolSpec, *, replace: bool = False) -> None:
        if spec.name in self._specs and not replace:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._specs[spec.name] = spec

    def completions(self) -> dict[str, dict]:
        """Freeze actual terminal contracts without a domain-name allowlist."""
        return {spec.name: spec.completion.model_dump(mode='json')
            for spec in self.list_specs() if spec.completion is not None}

    def register_many(self, specs: Iterable[CommonToolSpec], *, replace: bool = False) -> None:
        for spec in specs:
            self.register(spec, replace=replace)

    def get(self, name: str) -> CommonToolSpec | None:
        return self._specs.get(name)

    def list_specs(self, *, domain: str | None = None) -> list[CommonToolSpec]:
        specs = sorted(self._specs.values(), key=lambda item: (item.domain, item.name))
        if domain is None:
            return specs
        return [spec for spec in specs if spec.domain == domain]

    def to_json(self) -> list[dict[str, object]]:
        return [spec.model_dump(mode="json") for spec in self.list_specs()]


def default_tool_registry(
    *,
    include_report: bool = True,
    include_experiment: bool = True,
    include_code_task: bool = True,
) -> ToolRegistry:
    """Build the default registry from existing domain-level tool specs."""
    registry = ToolRegistry()
    if include_code_task:
        registry.register_many(_code_task_specs())
    if include_experiment:
        registry.register_many(_experiment_specs())
    if include_report:
        registry.register_many(_report_specs())
    return registry


def _code_task_specs() -> list[CommonToolSpec]:
    from simple_ar.code_task.tools.registry import default_code_task_tool_specs

    return default_code_task_tool_specs()


def _experiment_specs() -> list[CommonToolSpec]:
    from simple_ar.experiment.tools.registry import default_experiment_tool_specs

    specs: list[CommonToolSpec] = []
    for spec in default_experiment_tool_specs():
        specs.append(
            CommonToolSpec(
                name=spec.name,
                description=spec.description,
                input_schema=spec.input_schema,
                output_schema=spec.output_schema,
                permission_level=_permission_from_experiment(spec.permission),
                risk_level=_risk_from_permission(spec.permission),
                domain="experiment",
            )
        )
    return specs


def _report_specs() -> list[CommonToolSpec]:
    from simple_ar.report.tools import report_tool_specs

    specs: list[CommonToolSpec] = []
    for spec in report_tool_specs():
        permission = PermissionLevel.READ_ONLY if "read" in spec.permissions else PermissionLevel.PLAN
        specs.append(
            CommonToolSpec(
                name=spec.name,
                description=spec.description,
                input_schema=spec.input_schema,
                output_schema=spec.output_schema,
                permission_level=permission,
                risk_level=RiskLevel.LOW,
                domain="report",
                max_calls=spec.max_calls,
            )
        )
    return specs


def _permission_from_experiment(permission: str) -> PermissionLevel:
    if permission == "read_only":
        return PermissionLevel.READ_ONLY
    if permission == "write_patch":
        return PermissionLevel.WRITE_PATCH
    if permission == "execution":
        return PermissionLevel.EXECUTION
    if permission == "network":
        return PermissionLevel.NETWORK
    return PermissionLevel.PLAN


def _risk_from_permission(permission: str) -> RiskLevel:
    if permission == "read_only":
        return RiskLevel.LOW
    if permission == "write_patch":
        return RiskLevel.HIGH
    if permission in {"execution", "network"}:
        return RiskLevel.CRITICAL
    return RiskLevel.MEDIUM
