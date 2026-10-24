"""Explicit trusted local packages. No scanning, installer, sandbox or scheduler.

V1 accepts read/plan declarations; determinism is the author's responsibility.
Python has the host's OS privileges; declarations and services are not isolation.
"""
from __future__ import annotations

import hashlib
from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import re
import sys
from threading import RLock
import tomllib
from types import MappingProxyType, ModuleType
from typing import Any
from uuid import uuid4

from simple_ar.tools.specs import CommonToolSpec, ToolResult, ToolCompletion


def _local(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Extension resources require nonempty local paths')
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError('Extension resource must be a file inside its package: ' + value)
    return path


def _read(path: Path, limit: int) -> bytes:
    with path.open('rb') as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError('Extension resource exceeds size limit: ' + str(path))
    return value


def freeze_extensions(entries: list[Path]) -> list[dict[str, Any]]:
    """Freeze only explicitly selected descriptors, small instructions and code IDs."""
    if len(entries) > 16:
        raise ValueError('Select at most 16 local extension packages')
    frozen = []
    ids = set()
    for entry in entries:
        path = Path(entry).expanduser().resolve()
        if path.is_dir():
            path /= 'extension.toml'
        raw = _read(path, 65536)
        doc = tomllib.loads(raw.decode('utf-8'))
        if set(doc) - {'extension', 'tools'} or 'extension' not in doc:
            raise ValueError('Expected [extension] and optional [[tools]]')
        meta = doc['extension']
        if not isinstance(meta, dict):
            raise ValueError('Extension metadata must be a table')
        if set(meta) - {'id', 'version', 'description', 'scope', 'instructions', 'resources'}:
            raise ValueError('Unknown extension metadata')
        for key in ('id', 'version', 'description', 'scope'):
            if not isinstance(meta.get(key), str) or not meta[key].strip():
                raise ValueError('Extension requires ' + key)
        namespace = meta['id']
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,23}', namespace) or namespace in ids:
            raise ValueError('Invalid or duplicate extension namespace: ' + namespace)
        ids.add(namespace)
        files = {}
        instructions = ''
        resources = meta.get('resources', [])
        if not isinstance(resources, list) or any(not isinstance(p, str) for p in resources):
            raise ValueError('Extension resources must list local paths')
        selected = list(resources)
        if 'instructions' in meta and not isinstance(meta['instructions'], str):
            raise ValueError('Extension instructions must name a local file')
        if meta.get('instructions'):
            selected.append(meta['instructions'])
        tools = doc.get('tools', [])
        if not isinstance(tools, list) or len(tools) > 32:
            raise ValueError('Extension tools must be a list of at most 32 contracts')
        names = set()
        for tool in tools:
            if not isinstance(tool, dict) or set(tool) - {
                    'name', 'description', 'implementation', 'function', 'input_schema', 'permission_level', 'completion'}:
                raise ValueError('Unknown extension tool field')
            name = tool.get('name', '')
            if (not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,23}', name)
                    or name in names or name == 'instructions'):
                raise ValueError('Invalid or duplicate extension tool name: ' + str(name))
            names.add(name)
            if tool.get('permission_level') not in {'read_only', 'plan'}:
                raise ValueError('Local extension v1 supports only deterministic read_only/plan tools; use host tools for side effects')
            if (not isinstance(tool.get('function'), str)
                    or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', tool['function'])):
                raise ValueError('Extension function must be an explicit Python identifier')
            if (not isinstance(tool.get('description'), str) or not tool['description'].strip()
                    or not isinstance(tool.get('input_schema'), dict)):
                raise ValueError('Extension tool requires description and input_schema')
            _validate_schema(tool['input_schema'])
            if 'completion' in tool:
                completion = ToolCompletion.model_validate(tool['completion'])
                if completion.field and completion.field not in tool['input_schema'].get('properties', {}):
                    raise ValueError('Completion field must belong to the extension input contract')
            if tool['input_schema'].get('type') != 'object':
                raise ValueError('Extension input schema must describe an object')
            if _local(path.parent, tool.get('implementation')).suffix != '.py':
                raise ValueError('Extension implementation must be a .py source file')
            selected.append(tool.get('implementation'))
        if len(selected) > 64:
            raise ValueError('Extension has too many explicit resources')
        for resource in selected:
            if resource in files:
                continue
            limit = 16384 if resource == meta.get('instructions') else 1024 * 1024
            content = _read(_local(path.parent, resource), limit)
            files[resource] = hashlib.sha256(content).hexdigest()
            if resource == meta.get('instructions'):
                instructions = content.decode('utf-8')
        if not tools and not instructions:
            raise ValueError('Extension must provide instructions or tools')
        frozen.append({'path': str(path), 'descriptor_sha256': hashlib.sha256(raw).hexdigest(),
            'metadata': meta, 'tools': tools, 'files': files, 'instructions': instructions})
    return frozen


def _validate_schema(schema: dict, value: Any = None, *, check_value: bool = False) -> None:
    """Use standard schema validation without fetching external references."""
    from jsonschema import Draft202012Validator, SchemaError, ValidationError
    from referencing import Registry
    from referencing.exceptions import NoSuchResource, Unresolvable

    def no_retrieval(uri: str) -> Any:
        raise NoSuchResource(ref=uri)

    if not isinstance(schema, dict):
        raise ValueError('Extension schema must be an object')
    try:
        json.dumps(schema, allow_nan=False)
        Draft202012Validator.check_schema(schema)
        if check_value:
            json.dumps(value, allow_nan=False)
            Draft202012Validator(schema, registry=Registry(retrieve=no_retrieval)).validate(value)
    except (SchemaError, ValidationError) as exc:
        raise ValueError('Extension schema or arguments are invalid: ' + exc.message) from exc
    except Unresolvable as exc:
        raise ValueError('Extension schema reference cannot be resolved locally: ' + str(exc)) from exc


class LocalExtension:
    """A normal domain owner; results and recovery remain controller artifacts."""

    def __init__(self, frozen: dict, host_call: Any, *, host_protocol: str | None = 'nested_calls.v1') -> None:
        # Keep caller-owned configuration and published schemas out of the saved identity.
        frozen = deepcopy(frozen)
        self.frozen = frozen
        self.host_call = host_call
        if host_protocol not in {None, 'nested_calls.v1'}:
            raise ValueError('Unknown extension host-call protocol')
        self.host_protocol = host_protocol
        self._module_prefix = '_simple_ar_extension_' + uuid4().hex
        self._modules: dict[Path, ModuleType] = {}
        self._module_lock = RLock()
        namespace = frozen['metadata']['id']
        self.domain = 'extension:' + namespace
        self.tools = {namespace + '__' + tool['name']: tool for tool in frozen['tools']}
        self.instruction_name = namespace + '__instructions'

    def verify(self) -> None:
        try:
            current = freeze_extensions([Path(self.frozen['path'])])[0]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError('Saved extension unavailable; restore its exact package: ' + self.frozen['path']) from exc
        if current != self.frozen:
            raise ValueError('Saved extension identity changed; restore its exact version: ' + self.frozen['path'])

    def specs(self) -> list[CommonToolSpec]:
        meta = self.frozen['metadata']
        specs = [CommonToolSpec(name=name, domain=self.domain,
            description=tool['description'], input_schema=deepcopy(tool['input_schema']),
            permission_level=tool['permission_level'], risk_level='low',
            completion=tool.get('completion')) for name, tool in self.tools.items()]
        if self.frozen['instructions']:
            specs.append(CommonToolSpec(name=self.instruction_name, domain=self.domain,
                description=f"Load {meta['id']} {meta['version']} skill: {meta['description']}. Scope: {meta['scope']}. Instructions grant no permissions.",
                input_schema={'type': 'object', 'properties': {}, 'additionalProperties': False}))
        return specs

    def _module(self, implementation: str) -> ModuleType:
        """Load one verified standalone module once per owner, without bytecode caches."""
        path = _local(Path(self.frozen['path']).parent, implementation)
        with self._module_lock:
            source = _read(path, 1024 * 1024)
            if hashlib.sha256(source).hexdigest() != self.frozen['files'][implementation]:
                raise ValueError('Extension implementation changed before import')
            if path in self._modules:
                return self._modules[path]
            name = self._module_prefix + '_' + str(len(self._modules))
            # Even a file named __init__.py is standalone, not a package that can
            # import unchecked siblings through its submodule search path.
            spec = spec_from_file_location(name, path, submodule_search_locations=None)
            if spec is None or spec.loader is None:
                raise ValueError('Extension implementation must be a Python source module')
            module = module_from_spec(spec)
            # Registration before execution is required by dataclasses and annotation lookup.
            sys.modules[name] = module
            try:
                exec(compile(source, str(path), 'exec', dont_inherit=True), module.__dict__)
            except BaseException:
                sys.modules.pop(name, None)
                raise
            self._modules[path] = module
            return module

    def call(self, name: str, arguments: dict, *, identity: str) -> ToolResult:
        try:
            self.verify()  # Also reject edits after this process loaded the catalog.
            if name == self.instruction_name and self.frozen['instructions']:
                if arguments:
                    raise ValueError('Instruction loading accepts no arguments')
                return ToolResult(tool_name=name, data={'instructions': self.frozen['instructions'],
                    'extension': self.frozen['metadata']['id'], 'version': self.frozen['metadata']['version']})
            tool = self.tools[name]
            _validate_schema(tool['input_schema'], arguments, check_value=True)
            module = self._module(tool['implementation'])
            # Services deliberately expose only permissioned host reads/planning.
            ordinal = 0
            def call_tool(target: str, values: dict) -> ToolResult:
                nonlocal ordinal
                ordinal += 1
                # Stable across a retry of this same function, distinct for
                # each ordered host operation, and bounded for long parent IDs.
                # Old tasks keep their original identity; command reconciliation
                # still refuses a different operation at that historical slot.
                parent = hashlib.sha256(json.dumps([name, identity]).encode('utf-8')).hexdigest()[:32]
                child = f'ext-{parent}-{ordinal}' if self.host_protocol is not None else identity
                return self.host_call(target, values, child)
            data = getattr(module, tool['function'])(dict(arguments), MappingProxyType({'call_tool': call_tool}))
            if not isinstance(data, dict):
                raise ValueError('Extension function must return a JSON object')
            json.dumps(data, allow_nan=False)
            return ToolResult(tool_name=name, data=data)
        except Exception as exc:
            return ToolResult(tool_name=name, status='error', error=f'{type(exc).__name__}: {exc}')
