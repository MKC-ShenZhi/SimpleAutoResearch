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
New tasks → ToolApplication action loop → OperationTools → independent operations
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
New tasks use `operations.v1`: investigation, editing, execution, inspection and
publication are independently callable, rather than stages of an implicit handoff.
`OperationTools` composes the registry; it does not run another scheduler. Inspection
owns version-bound findings and publication owns rendering and export provenance.
New creation defaults to `tool_contracts="outcomes"`, with workspace editing and
permitted process operations available alongside inspection and publication. Ordinary
source work uses the shared action loop, without a mandatory preparation controller.
Saved preparation contracts retain their original presentation and executable recovery;
loading tools changes presentation, never authority.
Project reuse requires a terminal delivery and one explicit complete `copy_project`
inventory with matching current hashes. Portable file groups alone do not establish
a project root. Reuse grants no execution authority, environment or inherited validator;
ambiguous, incomplete, stale or unsettled work remains unavailable as `code_project`.
New tasks freeze `inspection_protocol=subjects.v3`: one source investigation can
inspect several targets, but each decision names its own supporting observations.
Each target's completeness, findings and evidence versions are evaluated separately;
cross-file support must be explicit. Investigation shares the task budget and reserves
capacity for its final opinion, without a separate six-round deadline. A decision can
explicitly remain incomplete; reading all target bytes alone does not complete a review.
Unfinished or failed reviews revoke an older pass and retain outstanding required findings;
only a conclusive complete reassessment can replace them. Saved protocols retain their
reading contracts and receipts; load and unrelated supplies do not upgrade them.
For a fresh inspection, whole-file targets that fit the current input budget are
supplied directly; large files retain bounded paging and explicit windows retain
their requested scope. This removes mandatory model requests just to fetch small
required inputs, without choosing the opinion. The common context selector bounds each presented view; original observations
and native call/result pairs remain recorded. Cumulative decoded byte ranges from the
same current file version can complete a target; gaps, changed bytes and binary reads
cannot. Owner-computed file digests are available for provenance checks, not scientific
certification. Saved `subjects.v1` keeps its original full-target presentation.
V3 derives eligible source IDs once for both the per-request tool schema and runtime
validation; observation IDs, window IDs and paths are distinct. An invalid final gets
one correction before the owner returns an incomplete record. This does not cap valid
source investigation or relax historical receipt recovery. Additional closing instructions
belong in the shared selector's `trailing_messages`, so their cost participates in material
selection rather than being appended afterward. Fresh final admission may bind one
omitted target identity only when the supplied decisions determine it uniquely;
explicit invalid IDs and ambiguous mappings remain errors. The raw response and
owner binding are recorded separately. Evidence errors identify offending IDs.
Exhausted finals retain a bounded, explicitly unadopted diagnostic and source trace;
these are not assessments or facts, and do not upgrade historical invalid receipts.
The shared material decoder also exposes inspection-owned raw windows, including
successful reads inside failed opinions. Supporting evidence can reuse these windows
only after the original receipt and current permission/version/range checks. Target
reading and verdicts are not inherited. Reused bodies are optional observations in
the same context selector, not another pinned input block or memory store.
Fresh inspections freeze the original user requests and feedback separately from the
local inspection goal. The shared context selector accounts for both. Requirements
are neither evidence nor permission; a narrow inspection does not certify the whole
task. Cached historical inspections keep their original input contract.

Saved `source_generation_protocol=prepared.v3` tasks retain their preparation and
approved-execution owner. Ordinary new tasks no longer select this protocol.
Explicit code tasks retain their exact edit, validation and execution contracts.
The saved preparation protocol separates up to 12 editable sources from immutable input copies
(64 explicit files, 256,000 bytes each, 3,072,000 bytes total). The existing owner
copies and version-binds those inputs; they do not need to pass through the model
as rewritten records. Feedback retains their immutable roles. Saved `prepared.v1`
and unmarked preparations keep their original combined admission limits.
New `prepared.v3` revisions accept complete content or exact replacements for a bound
editable original. Whole-file revision requires complete current-file observations;
inventory membership alone is insufficient. Immutable inputs, concurrent-change guards
and execution approval remain enforced. Saved `prepared.v2` retains replacement-only edits.
Formal delivery declares its manuscript and requested formats. Fixed code and
measurement contracts remain enforced by their original execution owners.
Historical native tasks without this protocol retain their saved professional
control through `load` and `supply`. New `ToolApplication.create` calls require
`operations.v1`; `None` and old professional-controller options fail before task
creation. Historical compatibility tests load explicit saved wire fixtures rather
than creating new legacy controllers. Never edit a saved task to upgrade it.

Delivery need not be a formal report. Plain explanations and Chinese Markdown
artifacts use the same delivery and feedback loop; publication is optional.
Explicit report claims and requested PDF/TeX retain their inspection/version checks.
Core retains attempts, artifacts, resource usage and process results through the same shared execution and records.

New operation tasks save `working.v2`. The current work owner maintains concise
understanding through accepted `update_work_plan.notes` receipts. The common
selector retains recent atomic conversation and supplies explicitly referenced
earlier observations, without a second summary author or lexical history backfill.
Original records remain readable; notes cannot certify sources, permission or
scientific correctness. Missing capacity and changed source versions remain visible.
Saved `working.v1` tasks retain their automatic checkpoints, original selection,
registered outputs and charges. Their summary failures and unknown usage still
follow the saved recovery contract; new work does not silently change old tasks.
An intact completed professional response can retain earlier failed physical attempts
of that same logical call when their full bounded reservations remain charged and a
successful response is actually settled. Source adoption, current inspection receipts
and accepted native results share the core accounting check. This grants no automatic
format correction, process replay or release of unknown costs; pending requests,
unbounded usage and unknown processes still stop.
Connection switching and explicit resource authorization share proof of local
operation closure, including professional model calls. This proof does not adopt
their outputs or settle provider charges. Explicit new allowances retain old
entries unchanged; admission uses the current allowance while accounting reports
all historical unknowns. Running or unaccepted operations and uncertain processes
cannot cross this boundary.
Fulfillment is checked after producer outputs are registered. A byte-bound
successful operation can return unmet delivery conditions to the same loop;
it is not replayed or silently marked delivered. Historical receipts remain
unchanged, and damaged bindings still stop. Publication owns its actual source
and export inventory; the coordinator does not reconstruct them.

Inspection owns bounded source investigation through native read-only tools,
then submits its structured opinion separately. Chat and Responses retain the
configured connection, paired calls/results and necessary reasoning items rather
than simulating reads in JSON prose. Its assessment and version rules are unchanged; partial reads and failed
responses remain in the same inspection records. Publication shares byte-bound
PDF metadata across checks and previews, forecasts remaining physical process
slots, and leaves actual charging to core. Sequential timeout sums are upper
bounds, not minimum wall-time costs.

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

New native tasks use `available_operations.v1`: operations remain available within
actual budget, attempt and approval limits. The last affordable decision is not
forced into a terminal-only reply; useful intermediate work can be retained.
Only explicit, validated delivery completes a request; exhaustion preserves work
without declaring success. Tool dispatch withholds estimated capacity for the caller's next
decision, except an explicit terminal batch, a finishing owner or caller takeover.
Inspection separately protects its final opinion while gathering; nested floors compose
on the same ledger. Token forecasts exclude future source growth and cannot guarantee
completion, especially with uncapped output. Unknown usage never creates capacity.
Retained finite charge bounds still withhold their full reservation; unrelated bounded
unknown billing is not unknown capacity. An inspector's own unconfirmed responses or
source executions remain unavailable for adoption.
For new publications, approval inventories potential images, while the existing renderer
records consumed presentation inputs in the byte-bound export manifest. Recovery,
inspection and delivery check those inputs and their producers. Historical manifests
keep their recorded inventory semantics; changing an unrelated preview cannot justify
accepting a stale consumed figure. This does not certify scientific attachment provenance.
Historical `protected_decision.v1`, `closing_decision.v2` and no-policy tasks
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
