"""Native consumers of the existing deterministic table capability."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
from simple_ar.result_analysis.table import TableSpec, TABLE_CHOICES, preview_table_source, snapshot_table_capability, analyze_table_capability
from simple_ar.tools.specs import CommonToolSpec, ToolResult


class TableSourceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    path: str = Field(description='Authorized CSV/TSV or homogeneous JSON record table, up to 20 MB.')


class TableAnalysisRequest(TableSourceRequest):
    spec: TableSpec = Field(description='Existing analysis configuration. Establish row meaning, units and any pairing from evidence or user clarification, not filename guesses.')


def analysis_tool_specs() -> list[CommonToolSpec]:
    schema = TableAnalysisRequest.model_json_schema()
    properties = schema['$defs']['TableSpec']['properties']
    for name, choices in TABLE_CHOICES.items():
        properties[name]['enum'] = list(choices)
    return [CommonToolSpec(name='inspect_table', domain='analysis', permission_level='read_only', risk_level='low',
                input_schema=TableSourceRequest.model_json_schema(),
                description='Inspect a complete bounded table through the existing reader: exact row count, column names, raw-value cardinalities and bounded examples. Does not infer units, independent samples or pairing.'),
            CommonToolSpec(name='analyze_table', domain='analysis', permission_level='write_patch', risk_level='medium',
                input_schema=schema,
                description='Compute supported descriptive or explicitly paired statistics and figures with the existing table module; freeze source data and configuration, and deliver a portable analysis package for writing. No model calls, arbitrary code, network or significance claims. For multi-key transformations or unsupported methods use the existing code/execution tools instead of forcing the data into a preset.')]


class AnalysisToolGateway:
    def __init__(self, root: Path, workspace: Any) -> None:
        self.root, self.workspace = root, workspace

    def call(self, name: str, arguments: dict[str, Any], *, identity: str) -> ToolResult:
        try:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', identity):
                raise ValueError('Invalid analysis invocation')
            if name == 'inspect_table':
                request = TableSourceRequest.model_validate(arguments)
                path = self.workspace.resolve(request.path)
                return ToolResult(tool_name=name, data=preview_table_source(path, max_mb=20),
                    summary='Complete bounded source shape; row meaning and statistical assumptions are not verified.')
            if name != 'analyze_table':
                raise ValueError('Unknown analysis operation')
            # JSON validation preserves the dataclass's tuple fields without
            # maintaining another copy of its configuration/defaults.
            request = TableAnalysisRequest.model_validate_json(json.dumps(arguments))
            path = self.workspace.resolve(request.path)
            if request.spec.max_mb > 20 or request.spec.max_figures > 100 or request.spec.max_points > 10000:
                raise ValueError('Built-in analysis is bounded to 20 MB, 100 figures and 10000 points; use an explicitly resourced code task for larger work')
            binding = request.model_dump(mode='json')
            owner = ArtifactStore(self.root / 'tools/analysis' / identity)
            if owner.exists('request.json'):
                if owner.read_json('request.json') != binding:
                    raise ValueError('Analysis settings changed; preserve this snapshot and request a new version')
                if not owner.exists('result.json'):
                    raise ValueError('Incomplete analysis; inspect the frozen snapshot and partial outputs before retrying')
                saved = owner.read_json('result.json')
                hashes = saved['data']['artifact_sha256']
                if self.workspace.artifact_hashes(hashes) != hashes:
                    raise ValueError('Delivered analysis was modified; the retained snapshot is historical, not a new calculation')
                return ToolResult(tool_name=name, **saved)
            snapshot_store = ArtifactStore(owner.root / 'input')
            snapshot_store.root.mkdir(parents=True, exist_ok=True)
            frozen = snapshot_table_capability(context=CapabilityContext(snapshot_store, AttemptManifest(identity)),
                request={'file': str(path), **request.spec.to_config()})
            if frozen.status != 'completed':
                raise ValueError('Source snapshot did not complete')
            owner.write_json('request.json', binding)
            result_store = ArtifactStore(owner.root / 'computed')
            result_store.root.mkdir(parents=True, exist_ok=True)
            result = analyze_table_capability(context=CapabilityContext(result_store, AttemptManifest(identity),
                inputs=(frozen.artifacts[0],), input_store=snapshot_store), request=frozen.artifacts[0])
            if result.status != 'completed':
                raise ValueError('Analysis did not complete; retain the frozen input and diagnostics')
            prefix = f'workspace/analyses/{identity}'
            output = self.workspace.resolve(prefix, write=True)
            shutil.copytree(result_store.root, output)
            artifacts = [f'{prefix}/{file.relative_to(output).as_posix()}'
                for file in sorted(output.rglob('*')) if file.is_file()]
            hashes = self.workspace.artifact_hashes(artifacts)
            analysis = result_store.read_json('analysis.json')
            saved = {'artifacts': artifacts, 'data': {'analysis': f'{prefix}/analysis.json',
                'report': f'{prefix}/analysis.md', 'row_count': analysis['row_count'], 'artifact_sha256': hashes,
                'scope': 'Deterministically computed from frozen supplied data under the recorded configuration; input semantics, collection and inferential claims are not independently verified.'},
                'summary': 'Portable analysis, copied source and figures saved. Writing can consume analysis.md/analysis.json and existing figures; reuse valid results rather than rerunning to change prose.'}
            owner.write_json('result.json', saved)
            return ToolResult(tool_name=name, **saved)
        except Exception as exc:
            return ToolResult(tool_name=name, status='error', error=f'Analysis unavailable ({type(exc).__name__}: {exc}); original data and existing versions retained.')
