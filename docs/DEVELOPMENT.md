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

Technical greenfield tasks may start from an empty workspace or a mounted scaffold (`copy`/`sparse_copy`) through the same native preparation and feedback owner. Structured scientific tasks still own their measurement protocol: planning and dispatch share `task_plan.action_binding`, but this identity mapping never grants permissions or substitutes a validator for a scientific measurement. Saved actions and ledgers retain their original meaning.

Both paths have real consumers today; do not describe their migration as already complete:

```text
Ordinary goal / migrated exact requests
  → ToolApplication → CommonToolGateway → professional tools

Remaining scientific protocols / saved structured sessions
  → ResearchApplication → typed capabilities

Shared: SessionController, artifact records, budgets and execution primitives
```

The ordinary entry chooses actions from goals and observations. Task names do not grant permissions.
Structured inputs for tables, material reports and research summaries now project frozen
domain contracts into the common owner. Some mixed research/execution contracts and saved
structured sessions still use ResearchApplication; it is not a silent fallback for failed native tasks.

Native measurement can select a completed implementation receipt rather than a global active candidate. The implementation owner verifies its actual preparation, independent validator and shared process accounting; scientific execution uses that candidate's interpreter/environment and retains its producing references. Original sources remain pinned, and added source/dependency files require validation. Physical validator verification belongs to CodeTask and is shared with interruption recovery, not copied into each consumer. Formal measurement is a separate physical invocation from validation, even when an ordinary benchmark uses the same argv. Strict reproduction retains its distinct-checker contract.

Caller-frozen single-command and declared multi-condition measurements use the same owner with or without a model. Their exact cwd/argv/environment policy is not a writable workspace mount or an OS sandbox. Each literal initial caller condition authorizes one execution; model follow-up is not preapproved. Compatible conditions share receipt-verified environment preparation. Historical baseline reuse and mixed research/design/execution creation still need consumer migration.

Exact CodeTask and a flat formal measurement contract can coexist. Preparation stores both declarations; native approval binds the scientific contract, and the common source owner verifies the actual independent validator before measurement. Project argv relocation belongs to CodeTask's environment owner and is shared by execution and provenance, not reimplemented in the CLI. Independent validation does not require already-produced scientific artifacts; whole-project/API checks remain, and the separate formal measurement owns its runtime result schema. Greenfield planning consumes the declared formal entrypoint/metrics rather than mistaking the validator for the experiment. Report projection consumes receipt-verified measurements for the accepted code lineage. Frozen measurement obligations survive report-only feedback; unrelated old candidate output bytes are not a report dependency. Reuse reads the delivered formal argv template, never a completed invocation's physical output directory or old allowance.

Validated candidate inputs stay pinned. A measurement may declare new candidate-relative `workspace_outputs`; approval binds the exact paths and inventory, and physical execution receipts pin the observed output bytes. Undeclared additions, changes to prior inputs or outputs, and new source/dependency files do not inherit validation. Delivery and feedback use this same ownership check. Derived data can be retained/copied but are not newly validated code: another measurement requires a feedback candidate with its full inventory independently revalidated. Prefer attempt-local `{output_dir}` for successive results; existing candidate files cannot be overwritten as new outputs. Missing declared outputs force disclosed partial delivery, including the separate final-delivery path. Final delivery may reference registered domain outputs, not arbitrary observations or ledgers.

Start reading here:

- [CLI dispatch](../src/simple_ar/cli/main.py) and [start input adaptation](../src/simple_ar/cli/start.py).
- [Native task application](../src/simple_ar/app/tool_application.py) and [tool gateway](../src/simple_ar/tools/gateway.py).
- [Structured research application](../src/simple_ar/app/research_application.py) and [task planning](../src/simple_ar/research/task_plan.py).
- [Session control](../src/simple_ar/core/session.py), [artifacts](../src/simple_ar/core/artifacts.py) and [budgets](../src/simple_ar/core/budget.py).

The old eight-stage executor has been retired. `run`, `resume`, `research-code-task`
and `research-experiment` no longer launch it.
`research-brief` is a summary-only compatibility spelling of `research-session`, not another application. Its CLI consumer no longer uses the removed `app.research_brief` session request/result wrapper; library integrations should use the existing research capability registry and typed `research.brief` results, or the structured application's services for custom providers. Historical application loading/continuation is unchanged.
Distinguish historical read-only access from structured sessions that can still continue execution.

## Place changes with their owner

| Change | Preferred owner | Avoid |
| --- | --- | --- |
| User parameters and defaults | CLI and existing configuration parser | Separate conversions for every entry |
| Next actions and feedback | app | Domain modules owning another scheduler or task state |
| Source retrieval and text selection | research | Reimplementing retrieval in each report entry |
| Code creation, changes and validation | code_task / corresponding tools | Restricting abilities by task name |
| Numerical computation and data figures | result_analysis and mature libraries | Treating model prose as computation |
| Prose, citations and assembly | report | Application code reinterpreting the same facts |
| Connections and accounting | integrations and shared budget | Independent Writer/CodeTask ledgers |

Source material, measurements and adopted drafts have distinct owners.
Summaries are views, not replacements for original records.
Reports express results; they do not create them.
Do not persist competing state solely for temporary context.

Structured scientific actions select their protocol and lineage, while the shared dispatch boundary binds the local backend, finite measurement budgets and attempt-scoped implementation services. Do not rebuild these in each baseline, supplement, revision or retry branch. Work-plan consumers use `task`, `steps`, `accepted_refs` and the session's attempts/budget; the unused nested `research_task_context.v1` duplicate is no longer emitted. Existing saved records are not rewritten. Experiment reports extend the literature projection's source identities rather than numbering citations or reinterpreting source metadata independently.

### Add or change a capability

Trace one real consumer through input, execution and output before choosing the smallest change:

1. Define inputs, outputs and failure meaning in the responsible domain.
2. Reuse existing capability/tool boundaries; not every internal function needs registration or a factory.
3. Validate at user, model, file and process boundaries; reuse established typed contracts internally.
4. Save results through existing session/artifact mechanisms. Let the application choose continuation, clarification or delivery. A verification-only `review_document` may carry an explicit `delivery`, like other final-work tools: validate and bind declared existing artifacts before inference, include the actual review output, and use the same terminal commit. `needs_revision` forces unresolved partial delivery; no manuscript is corrected or scientifically certified. Without this field, inspection remains nonterminal, including historical requests.

For material directories, writing owns evidence investigation rather than asking the coordinator to guess filenames. It can batch existing `read_file` windows, inspect continuations and select the exact observed windows. Six decisions and 96,000 observed characters bound this professional work within the same task budget; they do not certify exhaustive coverage. Read versions, complete responses and selected ranges are retained. Old selections keep their binding, and interrupted unknown requests are not replayed automatically.
5. Cover consumers and failure/recovery behavior, then retire the replaced implementation in the same package.

Native tool recovery asks the registered domain's `can_reconcile` before dispatch. That method admits only retained work; the domain still checks request and output bindings and must never start a replacement operation. Process/publication owners also expose whether execution has started. The application does not inspect their receipt filenames. Core commits saved attempt outcomes in one place; research continuation decisions belong to the application, including programmatic callers.

Code patches and generated repair actions use the shared atomic file writer. File snapshots own rollback and byte verification: failure to restore one file does not prevent attempts to restore the others, and restoration errors remain alongside the original failure. CodeTask consumes the core process record directly; watchdog and scientific quality judgments are domain results, not rewrites of physical execution facts.

CodeTask also owns completed-proposal correction and authorized technical verification/repair. Research callers supply approval, protocol-asset guards and the shared ledger, then retain the returned evidence; they do not run another repair loop. Proposal-only public execution still waits for approval. Failed restoration, changed protected assets or insufficient verification allowance stop further automatic work; scientific measurement remains a separate responsibility.

The report projection owns measurement-to-text evidence joins. Applications select accepted records; comparisons must reference those measurements, and implementation history stops at the measured revision. Later patches and superseded measurements remain history, not evidence for the current result. Source retrieval uses canonical document chunks and in-memory ranking; the retired persistent-index API fails explicitly without modifying historical stores.

See [capability contracts](../src/simple_ar/core/capabilities.py),
[tool specifications](../src/simple_ar/tools/specs.py) and
[registration](../src/simple_ar/tools/registry.py).
Do not register unimplemented tools merely to expose a schema or MCP interface.

### Permissions, execution and recovery

- Models may propose actions, not expand edit scope, network access or resource authorization.
- Mounted originals are read-only. Commands require the relevant approval. Workspaces and virtual environments are not OS sandboxes.
- Commands, exit status, logs and accounting come from execution, not model self-reports.
- Distinguish model failure, process failure and valid negative experimental results.
- Recovery preserves the ledger, failures and unknown usage; it must not blindly replay operations with possible side effects.
- A core-confirmed interrupted native tool can restore its missing failure observation from the actual failed result. This neither re-executes the tool nor changes its core result to success. Without confirmation or a retained result, restoration still refuses blind replay.
- SDK failure diagnostics retain only safe transport events and cause types. A later redirected TLS failure cannot erase an earlier HTTP send; old unknown reservations remain unchanged.
- External agent output is untrusted input. Apply existing edit, validation and artifact rules before acceptance; do not execute it implicitly.

See [permissions](../src/simple_ar/tools/permissions.py),
[process control](../src/simple_ar/core/process.py) and
[model catalogs](../src/simple_ar/integrations/model_profiles.py).

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

[CI](../.github/workflows/ci.yml) currently runs its listed groups and builds a wheel.
This is neither full discovery nor live API acceptance.
Check selection when introducing a shared boundary.

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
