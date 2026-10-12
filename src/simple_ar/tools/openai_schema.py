from __future__ import annotations

from simple_ar.tools.registry import ToolRegistry, default_tool_registry
from simple_ar.tools.permissions import ToolPermissionPolicy


def export_openai_tool_schemas(
    registry: ToolRegistry | None = None,
    *,
    read_only_only: bool = True,
    policy: ToolPermissionPolicy | None = None,
    names: set[str] | None = None,
) -> list[dict[str, object]]:
    """Export registered tools in OpenAI-compatible function-tool format."""
    registry = registry or default_tool_registry()
    tools: list[dict[str, object]] = []
    for spec in registry.list_specs():
        if names is not None and spec.name not in names:
            continue
        if read_only_only and spec.permission_level not in {"read_only", "plan"}:
            continue
        if policy is not None and not policy.is_allowed(spec)[0]:
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters_schema(),
                },
            }
        )
    return tools
