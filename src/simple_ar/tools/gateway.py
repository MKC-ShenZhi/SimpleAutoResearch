from __future__ import annotations

from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

from simple_ar.tools.permissions import ToolPermissionPolicy
from simple_ar.tools.registry import ToolRegistry, default_tool_registry
from simple_ar.tools.specs import CommonToolSpec, ToolCall, ToolResult
from simple_ar.tools.trace import ToolTraceWriter, tool_timer
from simple_ar.core.capabilities import CapabilityContext

if TYPE_CHECKING:
    from simple_ar.report.schema import ReportContext


class CommonToolGateway:
    """Permissioned gateway that dispatches to existing domain tool gateways."""

    def __init__(
        self,
        run_dir: Path,
        *,
        registry: ToolRegistry | None = None,
        policy: ToolPermissionPolicy | None = None,
        report_context: ReportContext | None = None,
        trace_path: Path | None = None,
        debug_payloads: bool = False,
        domain_gateways: dict[str, Any] | None = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.registry = registry or default_tool_registry(include_report=report_context is not None)
        self.policy = policy or ToolPermissionPolicy.read_only()
        # Compatibility domains own their state only when actually dispatched.
        # A native registry must not create legacy code/experiment workspaces.
        self._code_task = None
        self._experiment = None
        self._report = None
        self._report_context = report_context
        self._trace = ToolTraceWriter(trace_path or self.run_dir / "tools" / "tool_trace.jsonl", debug_payloads=debug_payloads)
        self._domain_gateways = self.registry.domain_gateways()
        for domain, owner in (domain_gateways or {}).items():
            if domain in self._domain_gateways and self._domain_gateways[domain] is not owner:
                raise ValueError('Conflicting registered domain owner: ' + domain)
            self._domain_gateways[domain] = owner

    def list_specs(self) -> list[CommonToolSpec]:
        return self.registry.list_specs()

    def can_reconcile(self, call: ToolCall) -> bool:
        """Ask the registered owner whether it can recover without a new operation."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", call.trace_id):
            return False
        spec = self.registry.get(call.tool_name)
        if spec is None or not self.policy.is_allowed(spec)[0]:
            return False
        owner = self._domain_gateways.get(spec.domain)
        recoverable = getattr(owner, "can_reconcile", None)
        return bool(recoverable and recoverable(call.tool_name, call.trace_id))

    def has_started(self, call: ToolCall) -> bool:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", call.trace_id):
            return False
        spec = self.registry.get(call.tool_name)
        owner = self._domain_gateways.get(spec.domain) if spec else None
        started = getattr(owner, "has_started", None)
        return bool(started and started(call.trace_id))

    def can_observe_read_failure(self, call: ToolCall) -> bool:
        """Prove a read-only owner has no started operation; absence is not proof."""
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', call.trace_id):
            return False
        spec = self.registry.get(call.tool_name)
        if spec is None or spec.permission_level != 'read_only' or not self.policy.is_allowed(spec)[0]:
            return False
        owner = self._domain_gateways.get(spec.domain)
        started = getattr(owner, 'has_started', None)
        return callable(started) and started(call.trace_id) is False

    def session_owner(self, name: str) -> Any | None:
        """Look up a policy-authorized owner of the caller's actual attempt.

        Domain declarations replace per-tool execution lists in the controller;
        this grants neither process approval nor recovery eligibility.
        """
        spec = self.registry.get(name)
        if spec is None or not self.policy.is_allowed(spec)[0]:
            return None
        owner = self.domain_owner(name)
        return owner if name in getattr(owner, 'context_tools', ()) else None

    def domain_owner(self, name: str) -> Any | None:
        """Identify a receipt's owner without granting permission to run it."""
        spec = self.registry.get(name)
        if spec is None:
            return None
        return self._domain_gateways.get(spec.domain)

    def coordination_result(self, result: dict, record: str) -> dict:
        """Let a domain present its receipt without changing persisted evidence."""
        owner = self.domain_owner(result.get('tool_name', ''))
        present = getattr(owner, 'coordination_result', None)
        if present is None:
            return result
        # Validation detaches nested fields from the canonical caller object.
        from copy import deepcopy
        return present(ToolResult.model_validate(deepcopy(result)), receipt_path=record)

    def continuation_attempts(self, name: str, arguments: dict) -> int:
        """A target owner supplies its known admission floor, not authorization."""
        owner = self.domain_owner(name)
        required = getattr(owner, 'continuation_attempts', None)
        minimum = required(name, arguments) if required is not None else 1
        if type(minimum) is not int or minimum < 1:
            raise ValueError('Professional continuation requires a positive attempt floor')
        return minimum

    def professional_action(self, observed: dict, source: str, *, resume: bool,
                            consume_outcome: bool) -> tuple[Any, dict | None]:
        """Ask existing receipt owners; scheduling and authorization stay outside.

        The producing owner gets first refusal. Domain consumers may then accept
        its proven outcome. No tasks, model calls or state are created here.
        """
        owner = self.domain_owner(observed.get('tool_name', ''))
        continuation = getattr(owner, 'continuation', None)
        attempt_id = Path(source).parent.name
        action = None
        if continuation is not None:
            action = (getattr(owner, 'resume_after_budget', continuation) if resume else continuation)(
                observed, attempt_id)
        if action is None and consume_outcome:
            proposals = []
            seen = {id(owner)}
            for other in self._domain_gateways.values():
                if id(other) in seen:
                    continue
                seen.add(id(other))
                consumer = getattr(other, 'consume_outcome', None)
                # Consumers prove their own evidence and authority. Registration
                # alone grants neither adoption nor permission to execute.
                if consumer is not None and (proposal := consumer(observed, source)) is not None:
                    proposals.append((other, proposal))
            if len(proposals) > 1:
                return owner, None  # No unique handoff: retain the outcome for coordination.
            if proposals:
                return proposals[0]
        fallback = getattr(owner, 'stop_after_review', None)
        if action is None and fallback is not None:
            action = fallback(observed, attempt_id)
        return owner, action

    def _bound_context(self, call: ToolCall, context: Any) -> bool:
        """A path-shaped context is not evidence of controller ownership."""
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', call.trace_id)
                or not isinstance(context, CapabilityContext)
                or context.attempt.attempt_id != call.trace_id
                or context.store.root.resolve() != (self.run_dir / 'attempts' / call.trace_id).resolve()):
            return False
        try:
            saved = context.store.read_attempt_manifest()
            return (saved == context.attempt and saved.capability == 'native_tool'
                    and (saved.status == 'running' or self.can_reconcile(call)))
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def delivery_issues(self, observations: list[dict[str, Any]], *, proposed: ToolCall | None = None,
                        delivery: dict | None = None) -> list[str]:
        """Domains judge their accepted evidence; the gateway does not reinterpret it."""
        issues = []
        adoptions = []
        for owner in self._domain_gateways.values():
            adopt = getattr(owner, 'delivery_adoptions', None)
            if adopt is not None:
                adoptions.extend(adopt(observations, delivery, proposed))
        for owner in self._domain_gateways.values():
            check = getattr(owner, 'delivery_issues', None)
            if check is not None:
                preflight = getattr(owner, 'delivery_preflight', None)
                if proposed is not None and self.session_owner(proposed.tool_name) is owner and preflight:
                    issues.extend(preflight(observations, proposed.arguments))
                elif (adoption := getattr(owner, 'delivery_issues_for', None)) is not None:
                    issues.extend(adoption(observations, delivery, adoptions))
                else:
                    issues.extend(check(observations))
        return issues

    def call(self, call: ToolCall | str, arguments: dict[str, Any] | None = None,
             *, context: Any | None = None, approval: dict[str, Any] | None = None) -> ToolResult:
        if isinstance(call, str):
            call = ToolCall(tool_name=call, arguments=arguments or {})
        spec = self.registry.get(call.tool_name)
        if spec is None:
            result = ToolResult(tool_name=call.tool_name, status="blocked", error="Unknown tool.")
            self._trace.append(call=call, result=result, spec=None, duration_sec=0.0)
            return result
        allowed, reason = self.policy.is_allowed(spec)
        if not allowed:
            result = ToolResult(
                tool_name=call.tool_name,
                status="blocked",
                error=reason,
                metadata={"permission_level": spec.permission_level, "risk_level": spec.risk_level},
            )
            self._trace.append(call=call, result=result, spec=spec, duration_sec=0.0)
            return result
        with tool_timer() as timer:
            owner = self.session_owner(call.tool_name)
            if owner is not None:
                if not self._bound_context(call, context):
                    result = ToolResult(tool_name=call.tool_name, status='blocked',
                        error='This operation requires its actual authorized task attempt; no execution occurred.')
                else:
                    result = owner.execute_tool(call.tool_name, context, call.arguments, approval=approval)
            else:
                result = self._dispatch(spec, call)
        self._trace.append(call=call, result=result, spec=spec, duration_sec=timer.duration_sec)
        return result

    def _dispatch(self, spec: CommonToolSpec, call: ToolCall) -> ToolResult:
        if spec.domain in self._domain_gateways:
            return self._domain_gateways[spec.domain].call(call.tool_name, call.arguments, identity=call.trace_id)
        if spec.domain == "code_task":
            if self._code_task is None:
                from simple_ar.code_task.tools.gateway import LocalCodeTaskToolGateway

                self._code_task = LocalCodeTaskToolGateway(self.run_dir)
            return self._code_task.call(call.tool_name, call.arguments)
        if spec.domain == "experiment":
            if self._experiment is None:
                from simple_ar.experiment.tools.gateway import LocalExperimentToolGateway

                self._experiment = LocalExperimentToolGateway(self.run_dir)
            result = self._experiment.call(call.tool_name, call.arguments)
            return ToolResult(
                tool_name=result.name,
                status=_status(result.status),
                data=result.data,
                error=result.error,
            )
        if spec.domain == "report":
            if self._report_context is None:
                return ToolResult(tool_name=call.tool_name, status="blocked", error="Report context is not available.")
            from simple_ar.report.schema import ReportToolCall
            from simple_ar.report.tool_gateway import ReportToolGateway

            if self._report is None:
                self._report = ReportToolGateway(self._report_context)
            result = self._report.call(
                ReportToolCall(
                    tool_name=call.tool_name,
                    arguments=call.arguments,
                    caller=call.caller,
                    trace_id=call.trace_id,
                )
            )
            return ToolResult(
                tool_name=result.tool_name,
                status=_status(result.status),
                data=result.content,
                summary=result.summary,
                artifacts=list(result.source_handles),
                metadata=result.metadata,
            )
        return ToolResult(tool_name=call.tool_name, status="blocked", error=f"Unhandled tool domain: {spec.domain}")


def _status(status: str) -> str:
    if status in {"ok", "not_found", "blocked", "error"}:
        return status
    return "error"
