"""Use the saved task ledger at standalone CodeTask entry points."""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from simple_ar.core.artifacts import read_json
from simple_ar.core.budget import BudgetLedger
from simple_ar.core.locking import SessionFileLock


@contextmanager
def standalone_task_accounting(run_dir: Path, *, active: bool = True):
    """Open an existing standalone ledger; historical and embedded tasks keep their owner."""
    manifest_path = Path(run_dir) / 'manifest.json'
    if not active or not manifest_path.is_file():
        yield {}
        return
    policy = read_json(manifest_path).get('code_task', {}).get('cli_accounting')
    if policy is None:
        yield {}
        return
    if policy != 'standalone.v1':
        raise ValueError('Unknown standalone CodeTask accounting contract')
    root = Path(run_dir)
    with SessionFileLock(root / 'code_task' / 'meta' / 'budget.lock'):
        yield {'budget_ledger': BudgetLedger.load(root / 'budget_ledger.json'),
               'session_id': str(root.resolve()), 'attempt_id': 'standalone'}


def account_code_task(action):
    """Use the enclosing scope or the saved standalone ledger for a task action."""
    @wraps(action)
    def run(run_dir, *args, **kwargs):
        client = kwargs.get('llm_client')
        client_ledger = getattr(client, '_budget_ledger', None)
        scope = kwargs.get('process_scope')
        if scope is not None:
            if (kwargs.get('budget_ledger') is not None and kwargs['budget_ledger'] is not scope.ledger
                    or isinstance(client_ledger, BudgetLedger) and client_ledger is not scope.ledger
                    or kwargs.get('session_id') and kwargs['session_id'] != scope.session_id
                    or kwargs.get('attempt_id') and kwargs['attempt_id'] != scope.attempt_id):
                raise ValueError('Scoped code execution must use its task ledger and identity')
            kwargs = {**kwargs, 'budget_ledger': scope.ledger,
                      'session_id': scope.session_id, 'attempt_id': scope.attempt_id}
            return action(run_dir, *args, **kwargs)
        if kwargs.get('budget_ledger') is not None or isinstance(client_ledger, BudgetLedger):
            return action(run_dir, *args, **kwargs)
        with standalone_task_accounting(Path(run_dir), active=not kwargs.get('dry_run', False)) as accounting:
            if accounting:
                kwargs = dict(kwargs)
                for key, value in accounting.items():
                    if not kwargs.get(key):
                        kwargs[key] = value
            return action(run_dir, *args, **kwargs)
    return run
