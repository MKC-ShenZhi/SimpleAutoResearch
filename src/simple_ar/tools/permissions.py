from __future__ import annotations

from pydantic import BaseModel, Field

from simple_ar.tools.specs import CommonToolSpec, PermissionLevel, RiskLevel


class ToolPermissionPolicy(BaseModel):
    """Small explicit policy for tool execution and external-agent handoff."""

    allowed_permissions: set[PermissionLevel] = Field(default_factory=lambda: {PermissionLevel.READ_ONLY, PermissionLevel.PLAN})
    blocked_tools: set[str] = Field(default_factory=set)
    allowed_tools: set[str] = Field(default_factory=set)
    max_risk_level: RiskLevel = RiskLevel.MEDIUM
    allow_debug_payloads: bool = False

    def is_allowed(self, spec: CommonToolSpec) -> tuple[bool, str]:
        """Return whether *spec* is allowed and a human-readable reason."""
        if self.allowed_tools and spec.name not in self.allowed_tools:
            return False, f"Tool `{spec.name}` is not in allowed_tools."
        if spec.name in self.blocked_tools:
            return False, f"Tool `{spec.name}` is explicitly blocked."
        permission = PermissionLevel(spec.permission_level)
        if permission not in self.allowed_permissions:
            return False, f"Permission `{permission.value}` is not allowed by policy."
        if _risk_rank(RiskLevel(spec.risk_level)) > _risk_rank(self.max_risk_level):
            return False, f"Risk `{spec.risk_level}` exceeds max_risk_level `{self.max_risk_level}`."
        return True, "allowed"

    @classmethod
    def read_only(cls) -> "ToolPermissionPolicy":
        return cls(allowed_permissions={PermissionLevel.READ_ONLY, PermissionLevel.PLAN})

    @classmethod
    def allow_write_preview(cls) -> "ToolPermissionPolicy":
        return cls(
            allowed_permissions={PermissionLevel.READ_ONLY, PermissionLevel.PLAN, PermissionLevel.WRITE_PATCH},
            max_risk_level=RiskLevel.HIGH,
        )


def _risk_rank(level: RiskLevel) -> int:
    return {
        RiskLevel.LOW: 0,
        RiskLevel.MEDIUM: 1,
        RiskLevel.HIGH: 2,
        RiskLevel.CRITICAL: 3,
    }[level]
