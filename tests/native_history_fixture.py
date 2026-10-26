"""Writable native-tools-v1 history for loader/continuation compatibility tests.

This is a test-only wire fixture, separate from the research-v1 fixture. It
writes a declared historical config, empty journal and original budget, then
enters through load(). It never calls create(), _configuration(), a model or a
process, and never converts a newly created operations task into old history.
Callers supply historical contracts explicitly; current behavior uses create().
"""
from copy import deepcopy
import json
from pathlib import Path

from simple_ar.app.tool_application import ToolApplication, _runtime_bindings


def load_native_history(root: Path, goal: str, *, config: dict, limits: dict | None = None,
                        max_attempts: int = 300, services=None, absent_fields: tuple[str, ...] = ()):
    if 'control_protocol' in config:
        raise ValueError('Historical native fixture has no operation control protocol')
    return _load_history(root, goal, config=config, limits=limits, max_attempts=max_attempts,
        services=services, absent_fields=absent_fields)


def load_prepared_operation_history(root: Path, goal: str, *, config: dict,
                                    limits: dict, max_attempts: int = 300, services=None):
    """Load the saved prepared.v3/native.v1 operation contract, never create it."""
    return _load_history(root, goal, config={
        'control_protocol': 'operations.v1', 'source_generation_protocol': 'prepared.v3',
        'source_decision_protocol': 'native.v1', 'inspection_protocol': 'subjects.v3',
        'context_protocol': 'working.v1', 'tool_contracts': 'outcomes',
        'producer_completion': None, 'document_source_contract': None,
        **config}, limits=limits, max_attempts=max_attempts, services=services)


def _load_history(root: Path, goal: str, *, config: dict, limits: dict | None,
                  max_attempts: int, services, absent_fields: tuple[str, ...] = ()):
    root = Path(root).resolve()
    if root.exists():
        raise FileExistsError(root)
    saved = {
        'model': None, 'sources': {}, 'tool_contracts': 'on_demand',
        'model_connection': None, 'vision_connection': None, 'feasibility_connection': None,
        'project_python': None, 'allow_execution': False, 'allow_network': False,
        'allow_vision': False, 'scientific_tools': True, 'scientific_lineage': True,
        'execution_contract': None, 'research_policy': None, 'table_analysis': None,
        'max_images': 0, 'max_turns': 30, 'completion_policy': 'closing_decision.v2',
        'producer_completion': 'goal_findings.v1',
        'document_source_contract': 'document_source.v1',
        'publication_preparation': 'bound_candidate.v1',
        **deepcopy(config),
    }
    for field in absent_fields:
        saved.pop(field, None)
    saved['sources'] = {key: str(Path(path).resolve()) for key, path in saved['sources'].items()}
    if saved['project_python'] is not None:
        saved['project_python'] = str(saved['project_python'])
    if services is not None:
        # The injected transport is a test dependency, not a historical schema
        # serializer or an authorization to make a new request.
        saved['runtime_dependencies'] = _runtime_bindings(services)
    budget = {'llm_requests': 30, 'total_tokens': 600000,
        'process_invocations': 300, 'process_wall_seconds': 9000,
        'source_operations': 90, 'image_requests': 0, **(limits or {})}
    manifest = {
        'schema_version': 'session_manifest.v2', 'session_id': root.name, 'topic': goal,
        'profile': None, 'transition_recipe': 'native-tools-v1', 'status': 'created',
        'state_refs': {'actions': {'path': 'actions.json', 'kind': 'native_actions'}},
        'budget_ledger_ref': {'path': 'budget_ledger.json', 'kind': 'budget_ledger'},
        'budget': {'max_attempts': max_attempts, 'max_no_progress': 6,
            'attempts': 0, 'no_progress': 0, 'recorded_attempts': []},
        'decisions': [],
    }
    root.mkdir(parents=True)
    for name, payload in (
        ('session_manifest.json', manifest),
        ('actions.json', {'config': saved, 'requests': [goal], 'turns': [], 'pending': None}),
        ('budget_ledger.json', {'schema_version': 'budget_ledger.v1', 'limits': budget, 'entries': []}),
    ):
        (root / name).write_text(json.dumps(payload, indent=2), encoding='utf-8')
    return ToolApplication.load(root, services=services)
