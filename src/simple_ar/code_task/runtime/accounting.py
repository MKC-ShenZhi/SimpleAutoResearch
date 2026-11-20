"""Use the saved task ledger at standalone CodeTask entry points."""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from simple_ar.core.artifacts import read_json
from simple_ar.core.budget import BudgetLedger
from simple_ar.core.locking import SessionFileLock
from simple_ar.integrations.llm import task_budget_identity


@contextmanager
def standalone_task_accounting(run_dir: Path, *, active: bool = True):
    """Open an existing standalone ledger; historical and embedded tasks keep their owner."""
    root = Path(run_dir)
    manifest_path = root / 'manifest.json'
    if not active or not manifest_path.is_file():
        yield {}
        return
    task = read_json(manifest_path).get('code_task', {})
    policy = task.get('accounting', task.get('cli_accounting'))
    if policy is None:
        yield {}
        return
    if policy != 'standalone.v1':
        raise ValueError('Unknown standalone CodeTask accounting contract')
    with SessionFileLock(root / 'code_task' / 'meta' / 'budget.lock'):
        yield {'budget_ledger': BudgetLedger.load(root / 'budget_ledger.json'),
               'session_id': str(root.resolve()), 'attempt_id': 'standalone'}


def account_code_task(action):
    """Use the enclosing scope or the saved standalone ledger for a task action."""
    @wraps(action)
    def run(run_dir, *args, **kwargs):
        client = kwargs.get('llm_client')
        supplied = kwargs.get('budget_ledger')
        scope = kwargs.get('process_scope')
        ledger, session_id, attempt_id = task_budget_identity(client, supplied,
            session_id=kwargs.get('session_id', ''), attempt_id=kwargs.get('attempt_id', ''))
        if scope is not None:
            if (ledger is not None and ledger is not scope.ledger
                    or kwargs.get('session_id') and kwargs['session_id'] != scope.session_id
                    or kwargs.get('attempt_id') and kwargs['attempt_id'] != scope.attempt_id):
                raise ValueError('Scoped code execution must use its task ledger and identity')
            ledger, session_id, attempt_id = scope.ledger, scope.session_id, scope.attempt_id
        dry_run = kwargs.get('dry_run', False)
        # A client supplied at the standalone entry may hold an earlier copy
        # of that task's ledger. An explicitly passed parent ledger is live.
        reopen = (scope is None and supplied is None and ledger is not None
            and ledger.storage_path is not None and callable(getattr(client, 'with_budget', None))
            and ledger.storage_path.resolve() == (Path(run_dir) / 'budget_ledger.json').resolve())
        with standalone_task_accounting(run_dir, active=not dry_run and (ledger is None or reopen)) as accounting:
            if accounting:
                ledger = accounting['budget_ledger']
                session_id = kwargs.get('session_id') or accounting['session_id']
                attempt_id = kwargs.get('attempt_id') or accounting['attempt_id']
                if reopen:
                    kwargs['llm_client'] = client.with_budget(ledger, session_id=session_id, attempt_id=attempt_id)
            if ledger is not None:
                kwargs.update(budget_ledger=ledger, session_id=session_id, attempt_id=attempt_id)
            return action(run_dir, *args, **kwargs)
    return run
