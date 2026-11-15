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


def account_standalone_task(action):
    """Give direct API callers the same ledger used by the CLI action."""
    @wraps(action)
    def run(run_dir, *args, **kwargs):
        client = kwargs.get('llm_client')
        if (kwargs.get('budget_ledger') is not None or getattr(client, '_budget_ledger', None) is not None
                or kwargs.get('process_scope') is not None):
            return action(run_dir, *args, **kwargs)
        with standalone_task_accounting(Path(run_dir), active=not kwargs.get('dry_run', False)) as accounting:
            if accounting:
                kwargs = {**accounting, **{key: value for key, value in kwargs.items()
                    if key not in accounting or value}}
            return action(run_dir, *args, **kwargs)
    return run
