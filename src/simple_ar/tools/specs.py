from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PermissionLevel(str, Enum):
    """Common permission categories for local tools and external backends."""

    READ_ONLY = "read_only"
    PLAN = "plan"
    WRITE_PATCH = "write_patch"
    EXECUTION = "execution"
    NETWORK = "network"
    SECRET = "secret"


class RiskLevel(str, Enum):
    """Risk level used by previews, approval gates, and traces."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ToolCompletion(BaseModel):
    """Optional common terminal shape; it grants neither adoption nor authority.

    A nonempty field holds an optional DeliveryRequest in both arguments and
    result data. An empty field uses the whole object. Questions use the common
    question shape. Closing offers must be safe without further model work.
    """

    model_config = ConfigDict(extra='forbid', strict=True)
    kind: Literal['delivery', 'question'] = 'delivery'
    field: str = Field(default='', max_length=200)
    closing: bool = False

    @model_validator(mode='after')
    def shape(self) -> ToolCompletion:
        if self.kind == 'question' and self.field:
            raise ValueError('Questions use the complete common question object')
        return self


def tool_completion(name: str, saved: dict[str, Any] | None) -> ToolCompletion | None:
    """Decode one frozen contract; absent maps retain the historical v1 rules."""
    if saved is not None:
        value = saved.get(name)
        return ToolCompletion.model_validate(value) if value is not None else None
    # Historical request/receipt compatibility belongs here, not in each
    # application, reader and future domain. New tasks freeze declared specs.
    if name in {'deliver_result', 'ask_user'}:
        return ToolCompletion(kind='question' if name == 'ask_user' else 'delivery', closing=True)
    if name in {'write_file', 'export_report', 'run_process', 'execute_implementation',
                'review_document', 'measure_experiment', 'analyze_measurements'}:
        return ToolCompletion(field='delivery', closing=name in {'write_file', 'export_report', 'run_process'})
    return None


class CommonToolSpec(BaseModel):
    """Tool contract shared by local, OpenAI-style, and MCP-style adapters."""

    model_config = ConfigDict(use_enum_values=True)

    schema_version: str = "common_tool_spec.v1"
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    permission_level: PermissionLevel = PermissionLevel.READ_ONLY
    risk_level: RiskLevel = RiskLevel.LOW
    domain: str = "core"
    artifact_outputs: list[str] = Field(default_factory=list)
    max_calls: int = 8
    completion: ToolCompletion | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode='after')
    def completion_input(self) -> CommonToolSpec:
        if (self.completion is not None and self.completion.field
                and self.completion.field not in self.parameters_schema().get('properties', {})):
            raise ValueError('Completion field must belong to the declared input contract')
        return self

    def parameters_schema(self) -> dict[str, Any]:
        """Return an object JSON schema suitable for tool-call APIs."""
        if self.input_schema:
            return self.input_schema
        return {"type": "object", "properties": {}, "additionalProperties": False}


class ToolCall(BaseModel):
    """One tool invocation request."""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    caller: str = "simple_ar"
    trace_id: str = ""


class ToolResult(BaseModel):
    """Common result returned by all local tool gateways."""

    tool_name: str
    status: Literal["ok", "not_found", "blocked", "error"] = "ok"
    data: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    error: str = ""
    artifacts: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"
