"""Pure candidate admission: compiler syntax and two Pyflakes name rules only.

No project imports, execution, dependency installation, model calls or retries.
The generation owner persists the report and owns any bounded correction.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Mapping


ADMISSION_CONTRACT = 'python_candidate_admission.v1'


class CandidateAdmissionError(ValueError):
    def __init__(self, report: dict) -> None:
        self.report = report
        super().__init__('Candidate admission rejected: ' + json.dumps(
            [row for row in report['issues'] if not row['baseline']], ensure_ascii=False))


def check_candidate_sources(sources: Mapping[str, str | bytes], *,
                            originals: Mapping[str, str | bytes] | None = None,
                            file_hashes: Mapping[str, str] | None = None) -> dict:
    """Check final merged bytes; byte-identical original defects are baselines.

    Modified files must pass. No heuristic matches between old and new errors,
    configurable globals, star-import guesses, style or type rules are applied.
    """
    from pyflakes import __version__
    from pyflakes.checker import Checker, builtin_vars, _MAGIC_GLOBALS
    from pyflakes.messages import UndefinedName, UndefinedLocal

    # Do not let PYFLAKES_BUILTINS / PYFLAKES_DOCTEST change product admission.
    # Reuse the official checker, with its official default Python globals.
    class CandidateChecker(Checker):
        builtIns = set(builtin_vars).union(_MAGIC_GLOBALS)

    def raw(value: str | bytes) -> bytes:
        return value.encode('utf-8') if isinstance(value, str) else value

    hashes = {name: hashlib.sha256(raw(text)).hexdigest() for name, text in sources.items()}
    if file_hashes is not None:
        if any(file_hashes.get(name) != digest for name, digest in hashes.items()):
            raise ValueError('Candidate admission bytes differ from their file hashes')
        hashes = dict(file_hashes)
    issues = []
    for name, text in sources.items():
        if Path(name).suffix.lower() != '.py':
            continue
        content = raw(text)
        baseline = name in (originals or {}) and raw(originals[name]) == content
        try:
            tree = ast.parse(content, filename=name)
            compile(tree, name, 'exec', dont_inherit=True)
        except (SyntaxError, UnicodeError, ValueError) as error:
            issues.append({'path': name, 'line': getattr(error, 'lineno', None),
                'column': getattr(error, 'offset', None), 'rule': 'SyntaxError',
                'message': getattr(error, 'msg', str(error)), 'baseline': baseline,
                'file_sha256': hashes[name]})
            continue
        for message in CandidateChecker(tree, filename=name, withDoctest=False).messages:
            if type(message) not in (UndefinedName, UndefinedLocal):
                continue
            issues.append({'path': name, 'line': message.lineno, 'column': message.col + 1,
                'rule': type(message).__name__, 'message': message.message % message.message_args,
                'baseline': baseline, 'file_sha256': hashes[name]})
    issues.sort(key=lambda row: (row['path'], row['line'] or 0, row['column'] or 0, row['rule']))
    blocking = [row for row in issues if not row['baseline']]
    report = {'contract': ADMISSION_CONTRACT, 'checker': {'name': 'pyflakes', 'version': __version__},
        'candidate_sha256': hashlib.sha256(json.dumps(hashes, sort_keys=True,
            separators=(',', ':')).encode('utf-8')).hexdigest(),
        'file_sha256': hashes, 'status': 'rejected' if blocking else 'accepted', 'issues': issues,
        'scope': 'Compiler syntax and Pyflakes UndefinedName/UndefinedLocal only; byte-identical '
            'original defects remain explicit baselines. Not dependency, runtime, type or scientific validation.'}
    if blocking:
        first = blocking[0]
        report.update(kind='python_syntax_failed' if first['rule'] == 'SyntaxError' else 'python_names_failed',
            filename=first['path'], line=first['line'], offset=first['column'], message=first['message'])
    return report
