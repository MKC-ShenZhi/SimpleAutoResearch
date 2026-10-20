from __future__ import annotations

from copy import deepcopy
from typing import Any

from simple_ar.tools.registry import ToolRegistry, default_tool_registry
from simple_ar.tools.permissions import ToolPermissionPolicy


def compact_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Copy a display schema without titles or unreachable root definitions.

    Walk schema positions only: property/definition names and instance values
    (including defaults, examples, const and enum objects) are never rewritten.
    """
    result = deepcopy(schema)

    def strip(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                strip(child)
        elif isinstance(node, dict):
            node.pop('title', None)
            for key in ('properties', 'patternProperties', '$defs', 'definitions', 'dependentSchemas', 'dependencies'):
                for child in node.get(key, {}).values():
                    strip(child)
            for key in ('items', 'additionalItems', 'additionalProperties', 'contains', 'propertyNames',
                        'not', 'if', 'then', 'else', 'unevaluatedItems', 'unevaluatedProperties',
                        'contentSchema', 'allOf', 'anyOf', 'oneOf', 'prefixItems'):
                if key in node:
                    strip(node[key])

    strip(result)
    definitions = result.get('$defs', {})
    needed: set[str] = set()
    pending = [{key: value for key, value in result.items() if key != '$defs'}]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(node)
        elif isinstance(node, dict):
            for key in ('$ref', '$dynamicRef', '$recursiveRef'):
                reference = node.get(key)
                if isinstance(reference, str) and reference.startswith('#/$defs/'):
                    name = reference[len('#/$defs/'):].split('/', 1)[0].replace('~1', '/').replace('~0', '~')
                    if name in definitions and name not in needed:
                        needed.add(name)
                        pending.append(definitions[name])
                elif isinstance(reference, str) and not reference.startswith('#/'):
                    # Anchors/URI scopes may resolve inside definitions. Retain
                    # them conservatively instead of changing ref resolution.
                    pending.extend(value for name, value in definitions.items() if name not in needed)
                    needed.update(definitions)
            pending.extend(node.values())
    if '$defs' in result:
        if needed:
            result['$defs'] = {key: value for key, value in definitions.items() if key in needed}
        else:
            result.pop('$defs')
    return result


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
