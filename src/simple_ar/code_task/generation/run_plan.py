"""Validated execution proposals; execution and approval belong to the caller."""
from __future__ import annotations

from pathlib import Path

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

    def validate_command_identity(self, *, candidate_root: Path, source_binding: dict,
                                  candidate_paths: list[str]) -> None:
        """Keep directly named copied members in this candidate's identity.

        This is a literal argv contract, not interpreter parsing or dependency
        discovery. Uncopied external materials and executables remain legal.
        Older preparations retain their originally approved path semantics.
        """
        semantics = source_binding.get('argv_semantics')
        if semantics in {None, 'candidate_relative.v1'}:
            return
        if semantics != 'candidate_relative.v2':
            raise ValueError('Unknown retained command path semantics')
        copied = {}
        prior = source_binding.get('prior_candidate', {})
        inventory = prior.get('inventory', {})
        root = prior.get('project_root') or inventory.get('project_path')
        if root:
            for name in candidate_paths:
                path = str(Path(root) / name)
                if path in inventory.get('file_hashes', {}):
                    copied[Path(path).resolve()] = name
        snapshot = source_binding.get('project_snapshot', {})
        if snapshot.get('contract') == 'ordinary_project.v1':
            for name in snapshot['files']:
                copied[(Path(snapshot['project_root']) / name).resolve()] = name
        for bindings in (source_binding.get('original_files', {}),
                         source_binding.get('preserved_files', {}),
                         prior.get('preserved_files', {})):
            for name, binding in bindings.items():
                copied[Path(binding['path']).resolve()] = name
        candidate_root = candidate_root.resolve()
        for argument in self.argv:
            value = argument.split('=', 1)[1] if argument.startswith('-') and '=' in argument else argument
            try:
                path = (candidate_root / value).resolve()
            except (OSError, ValueError):
                continue  # Inline text need not be a filesystem path.
            if path in copied and path != (candidate_root / copied[path]).resolve():
                raise ValueError('Execution argv references a copied source member outside this candidate: '
                    f'{argument!r}; use candidate-relative {copied[path]!r}')

    @staticmethod
    def _check_conflicts(outputs: list[str], paths: list[str], *, same_set: bool = False) -> None:
        for index, output in enumerate(outputs):
            for path in paths[index + 1:] if same_set else paths:
                left, right = output.casefold(), path.casefold()
                if left == right or left.startswith(right + '/') or right.startswith(left + '/'):
                    raise ValueError('Execution outputs must be distinct and cannot overlap planned file paths')
