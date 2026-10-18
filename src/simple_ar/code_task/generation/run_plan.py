"""Validated execution proposals; execution and approval belong to the caller."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from simple_ar.code_task.generation.common import safe_relative_path


class ImplementationRunPlan(BaseModel):
    """One argv executed from the candidate root, with literal expected files."""

    model_config = ConfigDict(extra='forbid', strict=True)

    argv: list[str] = Field(min_length=1, max_length=40)
    outputs: list[str] = Field(min_length=1, max_length=24)
    timeout_sec: int = Field(default=120, gt=0, le=300)

    @field_validator('argv')
    @classmethod
    def validate_argv(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or '\0' in item for item in value):
            raise ValueError('Execution argv requires nonempty strings without NUL')
        return value

    @field_validator('outputs')
    @classmethod
    def validate_outputs(cls, value: list[str]) -> list[str]:
        # Reuse the workspace gateway's private/runtime rules without adding a
        # second policy or importing the tools package during module loading.
        from simple_ar.tools.workspace import _private_name

        for path in value:
            if (not path or safe_relative_path(path) != path
                    or any(char in path for char in '\\:*?[]')
                    or any(ord(char) < 32 for char in path)
                    or any(_private_name(part) for part in path.split('/'))):
                raise ValueError('Execution outputs require safe literal candidate-relative file paths')
        cls._check_conflicts(value, value, same_set=True)
        return value

    def validate_planned_paths(self, planned: list[str]) -> None:
        """Reject output files that would replace or contain generated files."""
        self._check_conflicts(self.outputs, planned)

    @staticmethod
    def _check_conflicts(outputs: list[str], paths: list[str], *, same_set: bool = False) -> None:
        for index, output in enumerate(outputs):
            for path in paths[index + 1:] if same_set else paths:
                left, right = output.casefold(), path.casefold()
                if left == right or left.startswith(right + '/') or right.startswith(left + '/'):
                    raise ValueError('Execution outputs must be distinct and cannot overlap planned file paths')
