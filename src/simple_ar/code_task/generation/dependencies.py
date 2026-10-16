"""Observe dependency metadata; task requirements, not keywords, choose methods."""
from __future__ import annotations

import importlib.metadata as importlib_metadata
import os
from pathlib import Path
import re
import sys
from typing import Any


def build_dependency_advice(task_text: str, *, python_executable: str | None = None) -> dict[str, Any]:
    """Read the inspecting interpreter only, without importing or executing code.

    A different task Python is not inspected by proxy. Dependency acquisition
    and import/runtime checks require their own authorized execution evidence.
    Old v1 advice remains readable; new advice does not recommend packages,
    algorithms, substitute datasets or install commands from topic keywords.
    """
    target = os.path.abspath(python_executable or sys.executable)
    inspecting = os.path.abspath(sys.executable)
    same = target == inspecting  # Do not resolve a venv symlink to its host binary.
    packages = _scan_installed_packages() if same else {}
    rows = []
    for package in packages.values():
        names = [package['package'], *package['import_names']]
        matches = [name for name in dict.fromkeys(names) if _term_in_text(name, task_text)]
        if matches:
            rows.append({**package, 'status': 'installed', 'priority': 'observed',
                'role': 'Distribution metadata explicitly named in task text; suitability and imports unverified.',
                'matched_terms': matches, 'match_reason': 'explicit_name_not_semantic_recommendation',
                'impact_if_missing': ''})
    return {
        'schema_version': 'code_task_dependency_advice.v1',
        'policy': 'advice_only_no_auto_install',
        'selection_policy': 'observed_metadata_and_explicit_names',
        'requirements_status': 'not_assessed',
        'environment_scope': 'inspecting_interpreter_metadata' if same else 'target_not_inspected',
        'inspecting_python_executable': inspecting, 'target_python_executable': target,
        'target_matches_inspecting_interpreter': same,
        'environment_package_count': len(packages) if same else None,
        'environment_packages': list(packages.values()),
        'installed_packages': [row['package'] for row in rows],
        # Retained v1 keys describe no inferred dependency requirement.
        'missing_required': [], 'missing_recommended': [], 'missing_optional': [],
        'risky_packages': [], 'install_command': '', 'ephemeral_install_command': '',
        'notes': [
            'Distribution metadata is not proof of import, binary compatibility or runtime availability.',
            'Only the inspecting Python is observed; a different task interpreter has no inferred inventory.',
            'Explicit-name matches are navigation aids, not a library whitelist or a task-solving algorithm.',
            'Required/missing dependencies are not inferred; empty missing_* lists do not certify readiness.',
            'Choose dependencies from actual requirements, project declarations and official interfaces.',
            'If requirements cannot be met, investigate or request authorized preparation; do not silently '
            'replace scientific methods, datasets or evaluation conditions.',
            'No package imports, installs, subprocesses or network requests are performed by this observation.',
        ],
        'packages': rows,
    }


def _scan_installed_packages() -> dict[str, dict[str, Any]]:
    packages = {}
    for distribution in importlib_metadata.distributions():
        try:
            name = str(distribution.metadata.get('Name') or '').strip()
            version = str(distribution.version or '').strip()
            top_level = distribution.read_text('top_level.txt') or ''
            names = [line.strip() for line in top_level.splitlines() if line.strip().isidentifier()]
            # RECORD/file metadata can name real imports when top_level.txt is absent.
            if not names:
                for file in distribution.files or ():
                    parts = file.parts
                    if not parts:
                        continue
                    first = parts[0]
                    candidate = Path(first).stem if len(parts) == 1 and first.endswith('.py') else first if len(parts) > 1 else ''
                    if candidate.isidentifier():
                        names.append(candidate)
        except (OSError, ValueError, TypeError, UnicodeError):
            continue  # An unreadable declaration is not a usable package fact.
        if name:
            key = re.sub(r'[-_.]+', '-', name.lower()).strip('-')
            packages.setdefault(key, {'package': name, 'version': version,
                'import_names': sorted(set(names)), 'import_name': names[0] if names else '',
                'importable': None})
    return dict(sorted(packages.items()))


def _term_in_text(term: str, text: str) -> bool:
    spelling = r'[-_.]+'.join(re.escape(part) for part in re.split(r'[-_.]+', term))
    return bool(term and re.search(r'(?<![\w-])' + spelling + r'(?![\w-])', text, re.IGNORECASE))


def _join_or_none(value: Any) -> str:
    return ', '.join(str(item) for item in value) if isinstance(value, (list, tuple)) and value else '(none)'


def render_dependency_advice_markdown(advice: dict[str, Any]) -> str:
    """Render new observations and historical v1 advice without reinterpreting it."""
    observed = advice.get('selection_policy') == 'observed_metadata_and_explicit_names'
    lines = ['# Dependency Advice', '', f"- Policy: `{advice.get('policy', 'unknown')}`",
        f"- Selection policy: `{advice.get('selection_policy', 'unknown')}`",
        f"- Observation scope: `{advice.get('environment_scope', 'historical_unspecified')}`",
        f"- Inspecting Python: `{advice.get('inspecting_python_executable', 'not recorded')}`",
        f"- Task Python: `{advice.get('target_python_executable', 'not recorded')}`",
        '- Automatic install: `disabled`', '', '## Summary', '',
        f"- Environment packages observed: `{advice.get('environment_package_count', 'not recorded')}`"]
    for label, key in ((('Explicitly named installed distributions' if observed else 'Recorded installed distributions'), 'installed_packages'),
            ('Missing required', 'missing_required'), ('Missing recommended', 'missing_recommended'),
            ('Missing optional', 'missing_optional'), ('Risky/task-sensitive', 'risky_packages')):
        if advice.get(key):
            lines.append(f'- {label}: ' + _join_or_none(advice[key]))
    if advice.get('install_command'):
        lines.extend(['', '## Historical install suggestion (not authorization)', '',
            '```bash\n' + str(advice['install_command']) + '\n```'])
    lines.extend(['', '## Observed package metadata' if observed else '## Recorded dependency details', ''])
    for row in advice.get('packages', []):
        if isinstance(row, dict):
            lines.extend([f"### {row.get('package', '')}", '',
                '- Imports: ' + _join_or_none(row.get('import_names') or ([row['import_name']] if row.get('import_name') else [])),
                *[f"- {label}: {row.get(key, 'not recorded')}" for label, key in (
                    ('Version', 'version'), ('Status', 'status'), ('Priority', 'priority'), ('Role', 'role'),
                    ('Recorded import probe', 'importable'))],
                *(['- Original impact advice: ' + str(row['impact_if_missing'])] if row.get('impact_if_missing') else []), ''])
    lines.extend(['## Limitations', '', *['- ' + str(note) for note in advice.get('notes', [])],
        '', 'Full observed distribution metadata is retained in dependency_advice.json.'])
    return '\n'.join(lines).rstrip() + '\n'


def dependency_advice_messages(advice: dict[str, Any]) -> tuple[str, ...]:
    return (
        f"Dependency observation: {advice.get('environment_scope', 'historical_unspecified')}; "
        f"packages observed: {advice.get('environment_package_count', 'not recorded')}.",
        'Explicitly named distributions: ' + _join_or_none(advice.get('installed_packages')),
        'Dependency metadata does not authorize installation or task/method substitution.',
        'Dependency advice artifact: code_task/meta/dependency_advice.json',
    )
