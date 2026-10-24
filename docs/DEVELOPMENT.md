# Development guide

[中文](DEVELOPMENT_zh.md) · [Get started](USAGE.md) · [Configuration](CONFIG_REFERENCE.md)

For contributors: locate the owner of a change, then verify behavior and compatibility.
See [task guides](WORKFLOWS.md) for usage and the [CLI reference](CLI_REFERENCE.md) for options.

## Set up a development environment

Use Python 3.12+, Git, uv and ripgrep. From the repository root:

```bash
uv sync --locked --extra examples
uv run simple-ar --help
uv run simple-ar-checks --list
```

The `examples` extra supports execution cases. Offline checks do not require model credentials;
live model and network tasks need separate configuration and authorization.
For a shared scientific environment whose additional packages must remain installed,
use `uv sync --inexact --extra examples`.
Keep large project dependencies and benchmark environments separate from the framework environment.

## Find the code

| Directory | Responsibility |
| --- | --- |
| `src/simple_ar/cli/` | Commands, input adaptation, CLI/TOML merging |
| `app/` | Action coordination, continuation, interaction and recovery |
| `core/` | Sessions, artifacts, budgets, capability contracts and process lifecycle |
| `tools/` | Callable tools, argument contracts, permissions and domain bridges |
| `research/` | Sources, documents, reading, comparisons and research contracts |
| `code_task/` | Project investigation, creation/editing, validation and bounded repair |
| `result_analysis/` | Deterministic tables, data figures and reconstruction packages |
| `report/` | Writing, revision, assembly, citations and publication |
| `integrations/` | Model catalogs and provider connections |
| `experiment/` | Experiment execution and compatibility consumers |
| `tests/` | Behavioral tests and shared test support |
| `examples/` | Readable, runnable cases |
| `templates/report/` | Packaged report templates |
| `benchmark/` | External evaluation adapters, not claims of benchmark results |

The paths from `app/` through `experiment/` are under `src/simple_ar/`.
Runtime outputs belong in ignored directories such as `runs/`, not source.

## Understand the two entry paths

Ordinary goals, new structured CLI tasks and `create_research_task` use one action
backbone. Structured inputs supply domain contracts; they do not create a separate
scheduler or grant permissions.

```text
New tasks → ToolApplication → CommonToolGateway → professional tools
Explicit compatibility APIs / saved ResearchApplication sessions
          → ResearchApplication → typed capabilities
Shared    → SessionController, artifacts, budget and process records
```

Read [CLI dispatch](../src/simple_ar/cli/main.py),
[start input adaptation](../src/simple_ar/cli/start.py),
[common research creation](../src/simple_ar/app/research_intake.py),
[ToolApplication](../src/simple_ar/app/tool_application.py) and
[the gateway](../src/simple_ar/tools/gateway.py) in that order for a new-task change.
The application owns cross-domain decisions, task feedback and terminal state.
Professional owners perform bounded investigation, implementation, repair and checks without coordinator-side file-by-file wiring.
Core retains attempts, artifacts, resource usage and process results through the same shared execution and records.

New native tasks expose workspace tools and an authorized capability catalog;
`load_tools` loads the contracts needed for the next action through the same registry.
Contract presentation options and exact defaults belong in the
[configuration reference](CONFIG_REFERENCE.md), not a second dispatch path.

Compatibility remains deliberate. Explicit `create_session` and
`ResearchApplication.create` consumers retain their return types and continuation
contracts; see [ResearchApplication](../src/simple_ar/app/research_application.py).
Saved sessions resume under their recorded owner and policy. Read-only legacy v1
records differ from saved ResearchApplication sessions that can still execute.
The retired eight-stage executor is no longer launched by `run`, `resume`,
`research-code-task` or `research-experiment`; this does not mean ResearchApplication
has been deleted. `research-brief` remains the summary-only compatibility spelling
of `research-session`. Unsupported new declarations fail before creation rather
than silently falling back to compatibility execution.

For input supply, approvals and continuation, use the
[CLI recovery contract](CLI_REFERENCE.md#native-recovery). Preserve saved contracts,
prior observations and charges when adding inputs or allowances. Do not silently
migrate historical tasks to new defaults.

## Place changes with their owner

| Change | Preferred owner | Avoid |
| --- | --- | --- |
| User parameters and defaults | CLI and existing configuration parser | Separate conversions for every entry |
| Cross-domain actions and feedback | app | A second global scheduler or task state |
| Source retrieval and text selection | research | Reimplementing retrieval in each report entry |
| Code creation, changes and validation | code_task / corresponding tools | Restricting abilities by task name |
| Numerical computation and data figures | result_analysis and mature libraries | Treating model prose as computation |
| Prose, citations and assembly | report | Application code reinterpreting the same facts |
| Connections and accounting | integrations and shared budget | Independent Writer/CodeTask ledgers |

Source material, measurements and adopted drafts have distinct owners.
Summaries are views of original records; reports express results rather than create them.
Keep action state in the application and execution facts in the shared ledger.
Nested model calls, retries and professional work use that same accounting;
do not add parallel task state, fee counters or global schedulers.

New native tasks protect an affordable follow-up decision in the shared budget
(`protected_decision.v1`). This temporary spending floor is not an extra charge or allowance.
Accepted local work remains subject to actual budget, attempt and approval limits.
Only explicit, validated delivery completes a request; exhaustion preserves work
without declaring success. Historical `closing_decision.v2` and no-policy tasks
retain their saved behavior. See [budget ownership](../src/simple_ar/core/budget.py)
and [configuration](CONFIG_REFERENCE.md) for policy details.

### Add or change a capability

Trace one real consumer from input through execution to output:

1. Define inputs, outputs and failure meaning in the responsible domain.
2. Reuse existing capability/tool boundaries; register only implemented operations.
3. Validate user, model, file and process boundaries; reuse typed contracts internally.
4. Save results through existing session/artifact mechanisms and return evidence to the application.
5. Cover direct consumers and failure/recovery behavior, then retire the replaced implementation.

Start with [capability contracts](../src/simple_ar/core/capabilities.py),
[tool specifications](../src/simple_ar/tools/specs.py) and
[registration](../src/simple_ar/tools/registry.py). Professional work may use a necessary bounded local loop, but must not create a second global task runtime, approval mechanism or ledger.

### Material and candidate ownership

Keep read-only materials separate from editable projects. Preparation produces a
candidate; a runnable proposal is still neither permission to execute nor evidence
of success. Execution consumes the registered preparation receipt and approved
command, environment, input versions and expected outputs. Feedback creates a new
candidate while preserving the original.

Validation, physical execution and scientific measurement answer different questions.
CodeTask owns technical validation and bounded repair; research owns measurement
protocols; report projection joins accepted measurements to the measured code version.
A passing validator, citation marker or compiled report does not establish scientific
quality. Later patches and superseded measurements remain history.

Bind delivery to registered outputs and actual bytes. Changed source, dependencies or
inputs invalidate reuse; generated data do not automatically become validated source.
Missing required outputs and unresolved review findings must remain visible in partial
delivery. Use existing workspace snapshots and atomic writers for edits and rollback,
retaining restoration failures alongside the original error.

Exact preparation, execution and output parameters belong in
[configuration](CONFIG_REFERENCE.md) and [CLI contracts](CLI_REFERENCE.md);
follow the [CodeTask owner](../src/simple_ar/code_task/) and
[report owner](../src/simple_ar/report/) for implementation details.

### Permissions, execution and recovery

- Models may propose actions, not expand edit scope, network access or resource authorization.
- Mounted originals are read-only. Protocol acceptance and process approval are distinct authorities; initial caller authorization does not preapprove model follow-up.
- Workspaces and virtual environments are not OS sandboxes. Treat external agent output as untrusted input.
- Commands, exit status, logs and usage come from execution. Distinguish model failure, process failure and valid negative experimental results.
- Preserve reservations, failures and unknown usage on recovery. A later transport error does not prove that an earlier request was never sent.
- Reconcile retained results through the domain owner; never blindly replay an operation with possible side effects. A core-confirmed interruption can restore a missing failure observation without re-execution or changing failure to success.
- Input supply must preserve prior charges and apply each authorized allowance once; interrupted supply blocks work until reconciled.

See [session control](../src/simple_ar/core/session.py),
[artifacts](../src/simple_ar/core/artifacts.py),
[permissions](../src/simple_ar/tools/permissions.py),
[process control](../src/simple_ar/core/process.py) and
[model catalogs](../src/simple_ar/integrations/model_profiles.py).

### Add a trusted local extension

For a local skill or custom read/plan operation, start with
[LOCAL_EXTENSIONS](LOCAL_EXTENSIONS.md) and
[the example package](../examples/local_extension/).
Select packages explicitly through the task configuration; they use the existing
catalog, gateway, attempts and receipts. The implemented v1 supports instructions
and read-only/plan Python tools, not arbitrary write, execution or network permissions.

Trusted Python runs with the host process's OS privileges. Restricted host services
and permission declarations are **not a sandbox**; load only trusted packages.
Saved tasks verify package identity, so keep selected code and declared resources
unchanged for continuation. Do not add a private scheduler, billing or recovery layer.

On the development branch, optional common delivery/question contracts are
frozen for new tasks. Application completion and retained-result readers use
the same contract, without adding extension names to core closing rules.
Declarations grant neither permission nor acceptance; closing offers require
an already visible operation suitable for direct completion. Simple producers
can hand off to one proven consumer without implementing a work loop. Shared
stops bind the original receipt and retain the same ledger and recovery.
See [completion declarations](LOCAL_EXTENSIONS.md#optional-explicit-completion).

See [the loader](../src/simple_ar/tools/extensions.py) and
[extension tests](../tests/test_local_extensions.py). The extension guide contains
the focused verification commands and recovery limitations. This is an implemented,
bounded local path; it does not establish completion of the broader persistent-project
workflow. Checked scope and remaining limits belong in
[development evidence](EVALUATION.md#v210-development-evidence).

## Refactor and reduce complexity

The unit of refactoring is **replacing and retiring a responsibility**, not file count or relocation.
For each package, explain:

- The concrete user or maintenance benefit.
- Which consumers switch to the replacement.
- Which branches, conversions, templates or tests are removed.
- How permissions, results and recovery semantics remain intact.

Before deletion, inspect imports, commands, registrations, configuration, documentation, tests and
historical readers. Supported new tasks and resumable sessions must not disappear merely because
their implementation is old. Remove consumer-free code rather than keeping it for hypothetical use.

Prefer the standard library, existing dependencies, then a suitable mature library.
A new dependency should replace actual maintenance responsibilities; check licensing, installation
cost and format compatibility. Do not indefinitely maintain both implementations behind another wrapper.
Avoid broad exceptions that return default success, repeated defensive checks and a configuration switch for every branch.

Code-generation dependency advice observes distribution metadata, not task semantics. It does not choose
libraries or substitute algorithms/data from keywords, and never treats the framework interpreter's
packages as a different task interpreter's inventory. Import/runtime checks and installation need their
own authorized evidence; an empty list of inferred missing dependencies is not proof of readiness.

## Verify a change

Start with affected behavior and direct consumers. Run full checks for release freezes or broad
shared-interface changes. Test counts are not completion criteria; private helpers do not each need a test.

```bash
uv run simple-ar-checks --list
uv run simple-ar-checks intake
uv run simple-ar-checks code-task
uv run simple-ar-checks report
uv run --extra examples simple-ar-checks all
```

| Changed area | Possible groups |
| --- | --- |
| CLI, configuration, setup | `quick`, `intake` |
| Sessions, application, recovery | `core`, `application`, plus affected tests |
| Sources and research | `research` |
| Code and patches | `code-task`; `code-task-examples` for cases |
| Tables and figures | `data` |
| Writing and assembly | `report` |
| Model transport/accounting | `llm` |
| Real processes and templates | `execution`, `pipeline` (historical group name) |
| Shared-boundary refactors, release candidates | `all` |

Groups are selected smoke coverage, not every test in a domain.
Direct selection shortens feedback cycles:

```bash
uv run python -m unittest tests.test_research_config
uv run --extra examples python -m unittest discover -s tests -t .
```

Use `-t .` for full discovery and import shared fixtures from the `tests` package.
Consolidate duplicate fixtures and repeated behavioral coverage, without packing unrelated behaviors
into giant test methods. Delete tests only when behavior has retired or retained tests cover it.

[CI configuration](../.github/workflows/ci.yml) defines grouped offline checks and a wheel build, not full discovery or live API acceptance. Whether automatic runs are enabled is managed separately by the maintainers.
Check test selection when introducing a shared boundary; documentation updates do not authorize enabling workflows.

Live task acceptance separately inspects content, numbers, figures, feedback and reconstruction.
Mocks, process exit codes and PDF compilation alone do not establish scientific quality.
Run expensive models, dependency installation and training on the designated compute server
with explicit call/resource scope.

## Build and submit

```bash
git diff --check
uv build
```

Packaging is defined in [pyproject.toml](../pyproject.toml).
The wheel contains application source and report templates, not the entire development repository.
Installed commands must not depend on root `scripts/` or private planning files.

Before submitting, confirm:

- Public commands, configuration and direct consumers agree.
- Normal, failed and resumed behavior has appropriate evidence; untested boundaries are explicit.
- Replaced code, tests, templates and dependencies have been addressed.
- No credentials, runtime outputs, caches or private records are included.
- Documentation matches behavior without presenting plans as implemented features.

Licensing is not yet settled; discuss code contributions first.

## Maintain documentation

| File | Primary purpose |
| --- | --- |
| README | Project value, quick entry and navigation |
| USAGE | First installation, configuration, task and continuation |
| WORKFLOWS | Recipes organized by user goals |
| CLI_REFERENCE | Command and option lookup |
| CONFIG_REFERENCE | Configuration locations, fields, defaults and combination rules |
| LOCAL_EXTENSIONS | Trusted local package setup, examples and limits |
| DEVELOPMENT | Architecture entry points, development, verification and maintenance |
| CHANGELOG | Implemented user-visible changes and migration notes |

Edit the relevant section instead of repeatedly prepending updates.
Keep one authoritative home for each rule; other pages provide necessary examples and links,
not another copy of the protocol. Tutorials should help users finish work before explaining advanced boundaries.

Use one heading per changelog date, newest first. Combine related changes around user impact.
Internal call counts, candidate identifiers, test details and construction diaries do not belong there.
Historical entries may be condensed while retaining dates and necessary migration notes.

Keep English and Chinese structure and facts aligned. Check incoming anchors after renaming headings.
Public documentation must not depend on the ignored `MDfiles/` directory to be usable.
