from __future__ import annotations

from enum import Enum
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# Saved method identities select behavior; loading never upgrades an old task.
GOAL_WORK_METHOD = 'goal.v2'
CLAIM_WORK_METHODS = frozenset({'execute.v2', GOAL_WORK_METHOD})
EXECUTABLE_WORK_METHODS = frozenset({'execute.v1', *CLAIM_WORK_METHODS})
NATIVE_WORK_METHODS = EXECUTABLE_WORK_METHODS | {'goal.v1'}
CURRENT_WORK_METHOD = GOAL_WORK_METHOD


def tool_result_payload(observation: dict) -> dict:
    """Separate common observation context from the unchanged physical payload.

    This projection verifies no receipt. Consumers must still compare the result
    with its original owner record, including unknown fields and all metadata.
    """
    context = {'record', 'arguments', 'current_request', 'request_count', 'contract_current', 'review_record'}
    return {key: value for key, value in observation.items() if key not in context}


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
    """Common result shape; it grants neither adoption nor authority.

    A nonempty field holds an optional DeliveryRequest in both arguments and
    result data. An empty field uses the whole object. Questions use the common
    question shape. Review uses the same claim shape without finishing a task.
    Closing offers must be safe without further model work.
    """

    model_config = ConfigDict(extra='forbid', strict=True)
    kind: Literal['delivery', 'question', 'review'] = 'delivery'
    field: str = Field(default='', max_length=200)
    closing: bool = False

    @model_validator(mode='after')
    def shape(self) -> ToolCompletion:
        if self.kind == 'question' and self.field:
            raise ValueError('Questions use the complete common question object')
        if self.kind == 'review' and self.closing:
            raise ValueError('Candidate review cannot close a task')
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

    def service_parameters_schema(self) -> dict[str, Any] | None:
        """Expose an operation without its optional task-completion effect.

        A required or whole-object completion is not a separable service.
        Owners must also reject completion arguments at dispatch; presentation
        never grants authority and the registered contract remains unchanged.
        """
        schema = deepcopy(self.parameters_schema())
        completion = self.completion
        if completion is not None:
            if not completion.field or completion.field in schema.get('required', []):
                return None
            schema.get('properties', {}).pop(completion.field, None)
        return schema


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
