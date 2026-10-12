# Development Guide

[中文版本](DEVELOPMENT_zh.md)

This document is for contributors who want to extend SimpleAutoResearch. For command details, see [CLI Reference](CLI_REFERENCE.md). For TOML schema details, see [Configuration Reference](CONFIG_REFERENCE.md). For setup walkthroughs, see [Usage And Configuration](USAGE.md). For workflow concepts and artifacts, see [Workflows And Artifacts](WORKFLOWS.md).

## Project Shape

SimpleAutoResearch is file-first with persisted session state:

- capabilities read and write concrete artifacts;
- session state is visible in `session_manifest.json`, attempts, and `ArtifactRef` handoffs;
- tests verify contracts/artifacts instead of hidden in-memory state;
- code changes happen in isolated editable workspaces, not the original project.

The main responsibilities are:

```text
src/simple_ar/
  cli/              user input, configuration and command dispatch
  app/              task coordination and next-action decisions
  core/             sessions, attempts, artifacts, budgets and processes
  research/         documents, reading, synthesis, design and analysis contracts
  code_task/        project inspection, scoped edits, validation and repair
  result_analysis/  deterministic table analysis and data figures
  report/           writing, review, assembly, references, audit and export
  integrations/     model and service adapters
  agent_backends/   scoped handoff and backend interfaces
```

Use `simple_ar.cli.main:main` for the console entrypoint and
`from simple_ar.cli.main import main` for Python callers. After an entrypoint
change, reinstall before using `--no-sync`; do not recreate package-level forwarding
that depends on child-module import order.

Retained-passage ranking uses Python's `sqlite3` with FTS5/BM25. A custom Python
runtime must include FTS5. Its lexical table exists only in memory for the lookup
and is closed afterwards; this does not introduce database files or an index service.

## Engineering Principles And Code Review Standard

Code prompts share `scope.prompt_file_inventory`: all indexed paths retain their
edit/evidence roles; detailed descriptions are limited to editable or selected
files. `interfaces.source_snippet_views` merges matching adjacent/overlapping
observations for prompt rendering and API extraction, never unread gaps or access
roles. Complete coverage requires the known whole-file length; conflicting ranges
remain separate. This is an immutable projection, not another source index/cache.
Source inventories also prune directories with the standard `pyvenv.cfg`
marker before traversal, rather than relying only on `.venv`/`venv` names. Task
environments nested in a project must not become project source or setup context;
an ordinary source folder named `environment` remains discoverable.

`execution/repair` uses the same source-window reader and renderer. Traceback and
static-validation lines precede current diff hunk anchors; at most two locations
share the original per-file character allowance. Small files stay whole; stale
coordinates fall back to an explicitly partial current view. Read-only evidence
can inform a repair, but cannot acquire edit permission. Prior plans are hypotheses,
not mandatory diagnoses. This improves information supply, not proof of correct repair.

The primary principle is: **provide a reliable path for the current real task; preserve evidence and continue reasoning when the research is uncertain; block only when an explicit execution boundary is reached.**

SimpleAutoResearch must avoid two forms of drift: adding general architecture with no current consumer, and adding so many checks, fallbacks, and defensive branches that the actual research path becomes difficult to understand. These standards apply to new features, refactors, compatibility layers, and tests, not only to domain code.

### Changes, Structure, And Abstraction

1. **Every change must answer a concrete problem.** State which input or operation exposes the current problem and what observable behavior should change. If the only explanation is “we may need it later” or “it is more complete,” defer it; the same rule applies to abstractions, configuration, and checks.
2. **The normal path should be readable.** A reader should be able to follow the path from research input to the next action through a small number of application functions. Use the existing capability registry for explicit capability boundaries, but do not register every internal helper or route an ordinary call through layers of factories, managers, gateways, and adapters merely for replaceability.
3. **Reuse concrete code before extracting an abstraction.** Extract a shared function when two behaviors are genuinely the same; allow a little duplication when they only look similar. Abstractions should come from an observed common need, not every imagined future implementation. Before converging the CodeTask bridge and independent entrypoint, compare defaults, authorization, and failure semantics rather than only renaming functions.
4. **Every configuration option needs a real use case.** Do not add a switch for every `if`, or require users to understand internal stages to run a task. Make budgets, edit scope, and research objectives explicit when they affect user decisions; use sensible defaults for internal choices.

Prefer the standard library, then an existing dependency, before adding a new
library. State the concrete consumer, which custom implementation it replaces,
behavior/serialization compatibility, license and installation cost. Declare
direct dependencies explicitly; do not quietly rely on a transitive dependency.
Developer tools need not become runtime dependencies. A mature library is useful
when it removes maintenance responsibility, not merely a few lines of code.

### Contracts, Truthfulness, And Errors

5. **Validate necessary boundaries, then trust the internal contract.** Validate user input, model output, external files, and process results. Once inside a typed boundary, do not repeat checks for emptiness, dictionaries, and fields at every layer. In particular, do not let chains of `.get(..., default)` silently turn missing experiment evidence into zero or an empty result.
6. **Scientific uncertainty is not a system error.** An incomplete abstract, unknown novelty, or non-improving experiment should produce a qualified result. Pause an action only when that action lacks a required condition; missing data may block an experiment without blocking literature analysis that does not depend on it.
7. **Fallbacks must preserve meaning.** When a model is unavailable, a deterministic status summary is acceptable but must not be labeled as model-generated analysis. When only an abstract is available, it may be analyzed but must not be called full-text reading. When an experiment fails, existing evidence may be retained but a fixed metric must never be invented to make the workflow pass. Every fallback must state what it actually completed.
8. **Handle an exception once, where it can be handled.** The provider layer handles recoverable network errors; the application boundary records execution failure and recovery position. Do not catch `Exception` at every layer and return an empty object, default success, or vague string. Preserve the cause and diagnostic context so real defects surface early.
9. **Each fact has one owner.** The experiment executor owns measured results, research owns interpretation, the application owns next-step decisions, and the report owns expression. CodeTask, session, and Writer must not maintain contradictory budgets or success states. An external Agent cannot override framework observations by reporting success.

### Tests, Compatibility, And Delivery

10. **Test user outcomes and important boundaries.** Prioritize behavior such as not starting a process for a no-experiment request, not retraining during recovery, not entering infinite repair after a negative result, and matching metrics to conditions. Test fewer private call counts and internal object counts; mocks must not replace a small real execution, and every small change need not trigger an expensive validation.
11. **Compatibility layers have boundaries and exit conditions.** Preserve an old entrypoint when its usage still matters, but do not let it carry new business logic. Record its consumers, replacement, and deletion condition. Do not maintain two complete orchestrators indefinitely, and do not break valid historical reads merely to remove an old directory.
12. **Each batch should be small and complete.** A batch should solve one visible problem and include the necessary implementation, validation, and explanation. Avoid changing directories, interfaces, behavior, dependency versions, and output formats all at once; remove locally superseded code when the migration is complete instead of only adding more code.

For a localized change, run the affected capability and its direct consumers, then check a
real user outcome. Expand coverage for shared configuration, session, execution or recovery
changes; reserve full regression for release freezing or broad refactors. Record untested
boundaries instead of treating test count as quality. Keep costly model runs, dependency
installation and training on the chosen compute host. On a shared `uv` environment, use
`uv sync --inexact` to retain extra scientific packages; benchmark dependencies belong in a
separate environment, not the application's environment.

### Fixed Review Questions

Every review should answer at least:

- Which confirmed problem does this change solve?
- Is the normal path easier to understand?
- Did it add duplicate state, hidden fallback, or unnecessary configuration?
- Can unaffected work still proceed when information is incomplete?
- What practical evidence demonstrates that it works?

Locks, budgets, migration, and recovery in the implementation blueprint should be the smallest reliable versions required by the current path. The blueprint is not a checklist saying that every piece of infrastructure and protection must be complete before any user feature can ship. Its example limits are adjustable engineering starting points, not reasons to avoid real tasks. The full architecture and construction order live in the project's local `MDfiles/` planning notes; that directory is intentionally excluded from GitHub, so this document is the public contributor standard.

### Current research-session extension boundaries

These are structured research development contracts, not claims that every provider, project, or
scientific loop has passed live acceptance.

`research-session` interprets task/assets/constraints into an accepted bounded plan and connects
it to typed capabilities. Design and execution reuse the supplied entrypoint, protocol, CodeTask
edit scope and process budget. Model output may propose conditions only inside inspected and
authorized boundaries; it cannot grant itself a cwd, installer, repair limit, or process permission.
Baseline behavior is `run`, `skip`, or same-condition `reuse`. A saved measurement is reused only
when its command, schema, protocol, preparation lineage and protected assets still match.
Analysis, implementation, experiment, report writing and audit own different facts. A plan is not
execution, a zero exit code is not scientific success, and report prose does not create a measured
result. Recovery follows saved attempt/state references and does not silently repeat a completed
side effect; provider failures, failed processes and negative scientific findings remain distinct.

- Keep one execution chain: task/assets → near-term plan → typed capability request →
  SessionController → actual artifacts → reconsider only when needed.
- Build basic research memory from the task, plan, relevant experience and artifact references,
  not a second fact store. Read constraints, versions and measurements from their original records;
  model summaries cannot overwrite them. Separate technical failures from negative research results.
- Prompts own stable responsibilities and output contracts. Dynamic inputs distinguish user
  requirements, observations, proposals and unknowns. Fix missing input transmission before adding
  more instructions; correct output formats at the parsing boundary, not through layered fallbacks.
- Reuse CapabilityResult/ArtifactRef for observations, binding actual conditions, code versions and
  input provenance. A plan is not execution, a zero exit code is not scientific success, and reports
  do not create measured facts.
- Continue an accepted plan while its premises hold; reconsider when evidence changes the decision.
  Basic recovery is required from the first working path.

Do not document a future capability as accepted merely because its schema or fixture exists.
Live provider, project, GPU, retrieval, and report-quality evidence must be named separately from
offline parser or behavior coverage. New work should extend the current application/capability
path and remain bounded until a real consumer justifies a larger interface.

### Compatibility Audit

Ordinary new tasks use the native goal-driven entrypoint; exact structured
protocols, segmented commands and historical recovery retain their consumers.
The old eight-stage runner has been removed:

```text
start (ordinary native goals and adaptable creation preferences)
  -> ToolApplication -> CommonToolGateway -> shared domain modules
  -> SessionController -> ArtifactStore

research-session (exact structured protocols and saved-session recovery)
  -> ResearchApplication
  -> typed research capabilities -> SessionController -> ArtifactStore

research-brief (segmented/development)
  -> typed research capabilities -> SessionController -> ArtifactStore

simple-ar status / inspect / search-artifacts
  -> historical artifact readers (no old workflow execution)
```

`start` sends ordinary goals and adaptable creation preferences to `ToolApplication`,
which combines investigation, implementation, analysis, writing, visual/image work
and publication through existing domain tools. `cli/start.py` adapts inputs; it
has no execution ownership. Exact table columns, fixed edit/validation scope and
reproduction protocols retain the TOML/`research-session` path through
`ResearchApplication`. Saved tasks recover through their original owner and protocol;
structured execution is not an implicit fallback after native failure. Goals and
preferences grant no permissions: mounts remain read-only, access and resources
are explicit, and process commands require exact approval. This is not an OS sandbox
or proof of delivery reliability or scientific quality. The
segmented commands remain useful for
development, diagnostics, and persisted handoff continuation, but are not a
parallel product workflow. `simple-ar run/resume` is retired; its flags are not
silently translated. New domain work belongs under `research/`, `code_task/`,
`experiment/`, `result_analysis/` or `report/`. The old stage layer is deleted; historical consumers are read-only
and current experiment/report behavior is owned by the modules described below.

The current tree uses read cards and `evidence_pack_from_read()` for the shared
Read-to-Synthesis handoff and does not add a second planning or lifecycle store.
CodeTask's external CLI support is also kept as a disabled/explicit backend because the current experiment path
uses its provider factory; it is not a research-session workflow controller.

Historical readers and compatibility facades are retained only where their
consumers or old formats still exist. Before deleting one, search imports, CLI
dispatch, docs, fixtures, and historical readers, then preserve the old-format
regression while migrating its real consumer. Delete a facade or projection
once its consumers are gone instead of keeping a complete legacy entrypoint
for hypothetical future use.

### Delivery review checklist

Before calling a change ready, review the actual user path and its evidence:

1. Trace the input from the public command to the owning capability and artifact.
2. Check ordinary, failed, resumed, and no-execution paths with focused tests.
3. Compare generated claims with original measurements, protocol, sources and figures.
4. State provider, environment, data and live-project limitations instead of weakening gates.
5. Remove replaced branches and duplicate ownership; do not add a new lifecycle to preserve an old one.

The public “one entrypoint” rule does not remove internal modularity. Capabilities
remain independently testable and composable for developers, recovery, and
future workflows.

### Cleanup Policy

Keep a generated artifact when it is a declared handoff, an audit record, a
portable user-facing output, or a compatibility input. Rebuildable caches may
be removed only through the explicit `simple-ar clean` command; the pipeline
must not silently delete them. Before removing code, search imports, CLI
dispatch, documentation, fixtures, and historical readers, then add a focused
regression for the replacement path. Prefer removing dead imports or a proven
duplicate branch over splitting a large but cohesive adapter into more layers.

Remove code when its production consumer is gone; do not split cohesive code
merely to reduce file size. The old research facade and stage aliases are deleted.
Remaining report, experiment and application lifecycle duplication still needs
retirement; shared projections alone do not establish one execution owner.

## Ownership Map

CLI/TOML input validation belongs to `cli.research_config.validate_session_arguments`;
report option projection belongs to `report_settings` in the same module, using
the existing `FIELDS["report"]` mapping for creation and explicit resume overrides.
Omitted resume options do not overwrite saved values; explicit false, zero and
empty mappings are retained. Template selection remains task policy in the CLI.
The application owns action dispatch, execution decisions and persistence. Ordinary
action handlers remain in that same owner. Bounded report prompt views belong to
`report.narrative`, not a second report memory. Editable-context selection belongs
to `code_task.editing.scope` and is shared by initial edits and repair. Moving pure
views does not authorize changing checkpoint, budget or adopted-draft semantics.

Reading lookup context is owned by `research.evidence.reader.reading_followup_context`.
Synthesis consumes the adopted note, its original lookup passages and remaining
questions, not nested superseded notes. Full diagnostic history stays in the
Read artifact; report projection shares the same lookup view without copying it.

Use this map when deciding where a change belongs. The stable entry is the
small public boundary a new caller may depend on; the final column is equally
important because it prevents domain policy from leaking into the core.

| Area | Stable entry | Owns | Does not own |
| --- | --- | --- | --- |
| Core runtime | `simple_ar.core` | artifact references, attempt lineage, bounded decisions, profiles, transition validation, and the shared resource ledger | domain schemas, provider calls, code edits, retries, or selecting the best result |
| Sources, documents, and evidence | `research.sources`, `research.documents`, `research.evidence` | provider/parser ports, document bundles, cards, chunks, and provenance-aware handoffs | workflow scheduling, provider-specific policy in core, or copying full text into every handoff |
| Saved-text retrieval | `research.store.retrieval` | shared lexical ranking, source order when known, actual excerpt windows | semantic truth, paper identity or a new index; report imports remain thin compatibility |
| Synthesis | `research.synthesis`, `research.brief` | evidence-derived directions, research contracts, and the smallest literature-to-idea composition | claiming novelty, choosing an experiment automatically, or calling a model implicitly |
| Experiment and analysis | `research.experiment`, `research.analysis`, existing `experiment.execution` | explicit run requests, canonical results, metric comparison, and result evidence status | code generation, repair policy, retry policy, or deciding the next research stage |
| Supplied tables and data figures | `result_analysis.table`, `result_analysis.figures` | shared parsing/preflight, descriptive arithmetic, declared coordinate rendering and rebuildable packages | inferring row meaning, scientific validity or arbitrary statistical/code execution |
| Report and audit | `report.projection`, `report.capability`, `report.audit`, `report.writing` | evidence projection, explicit section assembly, optional figure rendering, citation/metric audit, and legacy report compatibility | hiding missing evidence, inventing figures, or replacing the legacy writer/reviewer without a migration contract |
| Writing context and revision | `report.narrative`, existing `report.agent`/`editor` | adopted-prose projections, original correction contracts, candidate verification and existing checkpoint/allowance consumption | a second memory store, rejected prose as adopted fact, or unlimited re-review |
| Application and benchmarks | `app`, `cli`, `code_task`, and benchmark adapters | user-facing orchestration, legacy projections, code-task policy, and external evaluator integration | becoming a dependency of the core runtime or changing canonical capability semantics for one benchmark |

When a feature appears to span two rows, keep the coordination in the
application or an explicit adapter and pass declared `ArtifactRef` inputs. Do
not make the lower row import private files from the upper row. A new class or
artifact is justified only when an existing boundary cannot express a real
consumer's input, output, or failure state; otherwise add a function, adapter,
or fixture at the existing boundary.

## Capability Boundary For New Modules

New replaceable modules may use the small capability boundary in
`src/simple_ar/core/` without changing the existing pipeline. `ArtifactRef`
identifies a declared artifact, `ArtifactStore` provides run-relative and
attempt-local file access, `CapabilityContext` passes registered inputs and a
profile, and `CapabilityResult` returns status, output references, diagnostics,
and provenance. `CapabilityRegistry` uses explicit registrations; it does not
scan the repository or dynamically import arbitrary providers. When a
controller-managed capability returns an `available` output reference whose
file is absent from its attempt store, the controller marks that ref
`missing`, adds a diagnostic, and downgrades a claimed `completed` result to
`partial`. It performs this check only on declared outputs; it does not scan
the attempt or calculate file hashes. Explicit `missing`, `not_rendered`, and
`failed` artifact statuses remain unchanged.

The built-in research adapters can be registered with
`research.register_research_capabilities(registry, names=...)`. This helper
loads only when called, accepts an explicit subset, and supports replacing a
selected implementation. It does not register the legacy eight-stage handlers
or create a workflow scheduler. The deterministic `plan` adapter is included
because it reuses the existing question, query, and source-budget builders and
writes a `research_plan.v1` handoff without an LLM call. Domain-specific
`design`, `code`, and `run` implementations remain owned by the application
until their contracts are ready.
`research.planning.search_request_from_plan()` is the corresponding in-memory
handoff to `SearchRequest`; it does not invoke a provider or own search policy.

`SessionController` owns physical attempts and persistence; ResearchApplication
chooses the next research action. Neither requires a second eight-stage runner.
The old runner and `core/stage_results.py` collector have been deleted.
The old Context, stage-contract table and unused console event framework are
retired. Capabilities use the artifact/session APIs above; archived document
reading is a small read-only adapter, not a runtime lifecycle.
Direct `execute_attempt()` calls resolve the requested handler before creating an
attempt, so a misspelled or unregistered capability cannot consume budget or
leave a synthetic failure attempt.
`execute_attempt()` is the corresponding physical-attempt entry for the newer
application layer: it persists the running/result manifests and updates the
bounded counter but does not choose a transition or append a `DecisionRecord`.
The old `execute()` entry and its fixed-stage decision execution were removed.
`execute_attempt()` is the sole physical execution path; it does not enforce a
second research sequence. Explicit interrupted recovery also returns execution
facts without appending research decisions; historical decision records remain readable.
CodeTask usage callbacks share `integrations.usage.record_usage()` for logs,
batch projections and display summaries. This function does not settle the
BudgetLedger; do not add independent usage writers to planning, editing or repair.
New capability tests use `execute_attempt()` and validate actual results and
handoffs; application tests own research completion and retry decisions.
The new entry may omit `attempt_id`; the controller then uses a persisted
monotonic sequence to generate a readable id. Gaps after a crash or preflight
failure are acceptable, but an id that may contain evidence is never reused.
`SessionManifest` now persists `revision`, `status_reason`,
`next_attempt_sequence`, state references, and an optional ledger reference, and
writes `session_manifest.v2`; loading still accepts v1. A v1 session is
read-only so inspection cannot rewrite history. `research-session-migrate`
imports supported evidence into a new application session; it does not upgrade
the historical manifest in place.
After checking delivery conditions, a new application may call
`pause(reason)`, `complete(reason)`, or `continue_with_revision(reason)`. Pause
blocks another physical attempt; explicit continuation increments a revision
without resetting the previous attempt budget. Core does not judge whether a
paper or experiment is scientifically sufficient; the application owns that
delivery check. Status inspection and recovery do not start new work implicitly.

`DecisionRecord` and the manifest's recipe label are retained as historical data,
not active policy. `status_snapshot()` reports execution counts, budget and the
last recorded decision without deriving permitted next steps. Attempts are
persisted as running before the handler starts.
After a process-level interruption, a caller may load the session and call
`reconcile_attempt()` when the capability result is already on disk. It closes
a running attempt or repairs accounting for an already finalized attempt,
idempotently. ResearchApplication also restores its missing state reference
before advancing, so the same completed action is not invoked twice.
If no result exists, the caller must confirm the interruption and
call `recover_interrupted()`, which writes an explicit failed capability result.
Neither operation retries, overwrites an existing result envelope, or chooses
the next domain operation.
While any attempt is still marked `running`, a new attempt is rejected until
that explicit recovery is performed. This preserves the one-active-attempt
lineage without silently creating a second branch. A caller that intentionally
wants to compare an alternative from an earlier node may pass
`parent_attempt_id` to `execute_attempt()`. The parent must be an existing completed or
failed attempt. The application owns the research route. The default remains the persisted current
attempt, so ordinary linear runs are unchanged; this option is an explicit
lineage branch, not a graph scheduler or automatic retry.
Use `attempt_lineage()` when a caller needs the root-to-node chain for a
comparison or recovery view. It reads attempt manifests only, does not merge
artifacts, choose a best result, or schedule work; missing parents and cycles
are reported explicitly.

The application layer owns the ordered capability sequence and calls
`SessionController.execute_attempt()` explicitly. The structured
`simple_ar.app.research_application.ResearchApplication` entry persists a
`ResearchBrief` and normalized assets, then advances the
visible sequence `plan -> search -> document_ingest -> read -> synthesize ->
summarize` one bounded action at a time. It records the accepted output in
`SessionManifest.state_refs`, so a reload can continue without reconstructing
earlier in-memory objects. `ResearchApplicationServices` supplies only the
LLM client, provider registry, and small resource settings that this path
uses; it is not a general service registry.

The entry completes an evidence-backed summary and supports assessment/design.
Explicit experiment requests continue through execution and analysis; an
explicitly initialized CodeTask can implement a candidate between measurements;
existing source projects and the bounded CSV text baseline can be prepared
through the same application boundary; report-only requests also use the shared
Writer/assembly/audit lifecycle. `advance_session()` and `load_session()` are
library helpers for the same path, and the formal `research-session` CLI uses
it directly. The controller
still preflights registered handlers, input artifacts, and budgets before an
attempt is created. A higher-level workflow must inspect the result before
constructing a bounded continuation; every supplied input must be an existing
session artifact.

The application exposes one derived `WorkPlan`, persisted under `planning/`
as JSON and Markdown. It contains delivery status, gaps and the next action;
the redundant readiness view/file is retired. Available research is still
reported as partial progress when execution is blocked. When an execution or idea-assessment deliverable is requested,
the application also runs the bounded `assess_ideas` capability after
synthesis. Its JSON and Markdown artifacts record evidence resolution,
similarity risk, unknowns, and a readiness-oriented recommendation; they do
not claim novelty or authorize execution.

The initial LLM task plan receives a shared planning-boundary view. Suggested
default rows help describe the task but do not grant process authority. Before
`research_design` accepts an execution protocol, configured preparation,
implementation, baseline, candidate, and repair actions are reported as
deferred; survey and unconfigured research routes report them as unauthorized.
After design, the accepted protocol's existing execution-step materializer is
the sole source for executable process actions and their conditions. This
keeps the design checkpoint explicit without adding a second lifecycle or
permission store.

The application maintains one capability-output contract table for registration,
normal execution and recovery. A shared binding step resolves all declared
references before updating state. Attempt triggers retain the exact baseline,
candidate or repair role; recovery does not need another output mapping.

With an injected LLM client, assessment compares candidates against a common
source context sampled round-robin across documents. It records the text actually
sent, truncation, response and recommendation. Candidate IDs and citations are
validated at this boundary; failures retain `deterministic_fallback` assessments.
The application saves its summary before assessment, then reuses the existing
`research_design` capability for design/execution requests without another model
selection call. Abstention pauses design; `config.research_selected_idea_id` can
provide an explicit choice. Design does not authorize experiment execution.

For an existing executable experiment, request `experiment` or `experiments`
and supply `services.config["execution"]` with `command` (an argv list), `cwd`
(an existing absolute directory), `timeout_sec`, and optional `result_schema`
and `label`. Also set finite `process_invocations` and `process_wall_seconds`
in `budget_limits`. The command is user supplied, never inferred from LLM text;
configuration alone does not launch it for a summary-only request. Canonical
CodeTask repair/retest is bounded, and a simple explicit technical failure can
be retried by an explicit caller decision without rebuilding research evidence;
scientific negative results are not silently retried.
Results and deterministic analysis are separate persisted attempts. Failed
executions retain their status and can still deliver diagnostic analysis; a
completed application means requested artifacts exist, not that the experiment
succeeded. Saved physical results are reused after reload, including interruption
between attempt finalization and application-reference persistence. Named asset checks and report lifecycle behavior are described below;
workflow completion does not certify scientific validity or prose quality.

Execution configuration also accepts `protocol`, using the existing
`ResearchExperimentContract`: `protocol_revision`, `dataset_refs`, `split_spec`,
`metric_specs` and `comparison_conditions` retain comparison settings. Unknown
protocol fields are rejected instead of silently ignoring unsupported constraints.
Canonical results attach the process invocation ID as `measurement_id`, the run
label as `condition_id`, and a declared-protocol/metric-schema fingerprint.
`compare_experiment_results` permits descriptive deltas but returns `inconclusive`
for new results with incomplete/mismatched protocols or the same measurement ID.
Matching declarations are labeled `declared_match`, not independently verified:
only explicitly named files can be checked (below); access enforcement remains pending. Older
results without measurement metadata retain compatibility behavior with the
explicit `legacy_unverified` label.

For a paired experiment, add `execution.baseline` with its explicit `command`.
It inherits shared cwd, timeout, protocol and metric settings; supplied baseline
fields override those defaults. The top-level command is the candidate. The
application schedules baseline and candidate as separate uses of the same
experiment capability, then analysis writes `comparison.json` with both artifact
references. `application:baseline` in the attempt trigger preserves the role
across a crash before state-reference persistence. A valid regression finishes
with `metric_below_target` analysis and does not request automatic retraining.
This is one explicit pair, not a research iteration loop.

To modify a candidate, add `execution.code_task` with an absolute initialized
`run_dir` and an explicit `approval_note`; candidate `cwd` must be that isolated
workspace. The original CodeTask task remains the user requirements; the selected
design, declared execution protocol and actual baseline metrics are appended before
planning using the research-handoff renderer.
`research_handoff.json` freezes the consumed context and original task, and the
attempt records the resulting task Markdown. Changed research inputs cannot reuse
an already planned CodeTask run; prepare a fresh run for that revision.
The `implement` action reuses CodeTask planning, proposal, editing and validation,
without running its benchmark or hidden baseline. Its model calls share the session budget and attempt ID;
declared workspace protocol assets join the existing protected edit patterns.
Implementation records reference the design, baseline (when present), patch and
validation evidence. Recovery accepts completed implementation without repeating
edits. Start from a fresh initialized workspace for an unchanged baseline;
automatic workspace preparation, source provenance checks, full
execution-bundle portability, and broad research-direction revision remain
unfinished.

The old segmented research-code-task creator and its bridge executor have been
retired. `validate_repair_patch` retains review/static validation without measuring;
the canonical experiment action owns remeasurement. Standalone CodeTask keeps its
explicit repair-proposal approval flow. Do not reintroduce a combined repair/run
loop behind the implementation boundary.

Generated-project review repair must not guess implementation intent. Missing
entrypoints, configuration, documentation or public APIs remain review findings;
the framework no longer synthesizes fixed replacements or empties invalid package
code. Model-proposed repairs reuse snapshots and edit validation. Without a model,
the original files and failed review remain unchanged, and no experiment is started.

Runtime repair follows the same rule: a matching exception string does not
authorize a guessed module rename, global import rewrite or results-path
substitution. The observed failure goes to the existing bounded model repair
path with snapshots and validation; unavailable model repair leaves it unresolved.

Repair localization prioritizes failure-graph paths, explicitly implicated files
and source matches. Remaining project files provide bounded context; names such
as `runner`, `data` or `artifact` are not treated as evidence of responsibility.

Repair actions and whole-file content share the action applicator. Content becomes
a rewrite action; rejected actions cannot trigger a second overwrite. Partial
rejection rolls back the target file; accepted edits retain observed hashes and APIs.

Repair records describe attempted edits, not scientific or execution success.
Review/run repair counts share one accounting function. Follow-up review owns
its result; it does not rewrite the repair record as effectively recovered or
copy repair status into implementation state. Historical status fields remain readable.

`propose_repair_edits(..., failure_evidence=RepairEvidence(...))` accepts the
measurement owner's explicit failure report and analysis without discovering or
creating a legacy CodeTask benchmark record. It snapshots the supplied evidence
beside the proposal and does not apply edits or launch another measurement.
External evidence must describe a failed/timed-out execution, not a scientific
regression from a successfully executed experiment. Omission retains existing
standalone failure discovery.

An initialized `execution.code_task` can explicitly authorize automatic technical
repair rounds with `max_repairs` (non-negative integer, default `0`). After a
failed/timed-out candidate, the application records a separate implementation
attempt, then a canonical experiment attempt; it never remeasures the baseline.
All rounds also obey the existing attempt, LLM and process budgets. Original
`experiment` evidence remains unchanged; `repair_N` and `experiment_repair_N` state
refs retain each round, and analysis compares the last measurement with baseline.
Reaching the limit delivers the remaining failure rather than looping. An invalid
proposal/review/validation pauses before remeasurement. Persisted completed repair
results recover without repeating edits; interruption inside patch application
still needs interrupted-attempt inspection, not an automatic blind replay.
This is bounded technical repair, not model-directed research revision or complete
report generation. The default attempt cap may need an explicit increase for a
longer authorized workflow; a repair limit does not enlarge other budgets.

The same explicit repair limit applies after a **single-condition research
revision** fails at execution: the accepted plan can schedule
`repair_candidate` then `retest_candidate`, and always reaches `reanalysis`
after an observed result, including a final failure. The repair consumes the
failed run's immutable artifact and the original revision handoff; retesting
uses the unchanged protocol. Paired follow-up repairs are not yet supported,
and a missing/invalid repair proposal pauses before remeasurement. For report
provenance, the Writer receives all completed plan-order measurements, while
failed runs contribute status and captured diagnosis rather than fabricated
metric values. The analysis handoff requires the producing implementation ref
for the measured revision, not the globally latest patch.

`ResearchApplication.latest_experiment_ref()` selects the last recorded candidate
from the application action sequence. Analysis, experiment deliverable references
and the exported snapshot use this same selection; they do not treat the initial
failed candidate as the final result after a repair. Before remeasurement exists,
the previous recorded result remains selected. Historical refs are never rewritten.

For an existing baseline, `execution.code_task.code_root` can replace `run_dir`.
Supply an absolute source directory and `approval_note`, plus execution argv,
timeout and protocol. Cwd may be omitted or equal code_root. `prepare_execution`
reuses the CodeTask copy initializer inside its attempt, records copied/skipped
files, and hands off isolated cwd/run_dir. The original project is not edited or
measured. Preparation does not install, download or run setup hooks/benchmarks.
Baseline inherits the isolated cwd; an explicit baseline cwd equal to code_root
is remapped. Completed preparation recovers without reinitialization; interrupted
partial initialization still needs inspection. Existing copy limits apply, so keep
large datasets as explicit shared assets.

For a data-only text baseline, execution accepts `dataset` (absolute UTF-8 CSV
path) and `timeout_sec`, without a user command or CodeTask. Required columns are
`text,label,split`; split values must be `train` or `eval`, both must exist and
training must contain at least two labels. Current limits are 10 MB / 10,000 rows;
exceeding them fails preparation instead of silently sampling. Normalized-text
overlap is recorded as possible leakage, not hidden or treated as a system crash.
Preparation saves source hash, inspection, normalized data and the generated
`csv_text_classification` script. The explicit protocol protects data/evaluator.
The ordinary experiment action then trains CountVectorizer + LogisticRegression
on train only (one BLAS/OpenMP thread) and measures accuracy/macro-F1 on eval;
it uses the same process budget and analysis path, not another runner. This is a
single baseline, not automatic candidate generation, language adaptation or paper
reproduction. The existing project-preparation path remains available for other methods.

Prepared execution is a declared experiment input. Its reference and limitations
are retained in the measured result under `preparation`, then carried into the
analysis audit and Markdown. This preserves issues such as split leakage through
the report-facing handoff without changing observed metrics or process status.

`ResearchApplication.report_inputs()` projects completed canonical artifacts into
existing `ReportContext`/`ReportMemory`; the old session adapter uses the same
`build_research_report_inputs()` constructor. It retains separate baseline,
candidate and comparison sources, latest analyzed measurements and limitations.
The measured execution protocol takes precedence over proposed design. This is
a read-only projection: experiment requests use the completed experiment/analysis
artifacts, while report-only requests use the available literature evidence. The
projection itself is not the report snapshot or Writer/audit execution boundary;
those are created by the report capabilities below.

When both experiment and report/paper outputs are requested, the application now
runs `report_write -> report -> report_audit`. The Writer runs inside a controller
attempt, after a content-identified snapshot of context, memory, configuration,
template and source references has been saved. Assembly and audit reuse that
snapshot and the Writer's memory, not a refreshed live context. Existing agent,
assembler and audit implementations are reused. Default total attempt allowance
is 16; resource budgets remain independent. A Writer failure pauses with its
snapshot retained; explicit continuation retries writing without research reruns.
A completed Writer can recover into assembly without another model call. Section
checkpoints are persisted at the Writer boundary, and the canonical application
also supports report-only/no-experiment requests. The legacy report entry still
has its old execution order.

Report `MetricSource` retains measurement ID, protocol ID/revision/fingerprint,
condition, unit and source kind. Baseline and candidate use their own metric schemas
and protocol units; old records remain `legacy_unverified` with missing identity,
and comparison deltas are marked derived. The metric appendix displays unit,
condition and origin. These are traceable declarations, not proof that arbitrary
prose comparisons or scientific conclusions have passed semantic review.

Implementation evidence (patch, validation, available review, work plan and research
handoff) is copied into its attempt and registered as capability outputs.
`implementation.json.artifact_refs` resolves relative to that attempt, as indicated
by `artifact_base: "attempt"`. CodeTask/workspace absolute paths are provenance,
not the evidence lookup mechanism. Copying a session therefore preserves these
records without the external CodeTask directory; it does not bundle datasets,
dependencies, checkpoints or all source files needed to rerun the experiment.

`execution.protocol.protected_assets` accepts explicit `{asset_id, path}` file
entries for data, split indices or evaluators. Relative paths resolve against
the execution cwd. Required files are hashed before launch and checked again
afterward; there is no recursive directory scan. Results retain both observations
under `measurement.asset_integrity`. A changed/deleted file sets `validity_status`
to `invalid` and the overall result to failed while retaining `execution_status`,
return code and metrics. The guard reports `protected_asset_changed`. Matching
protocol declarations do not permit comparisons across different observed file
contents. These checks are audits of named files, not OS write protection, access
isolation or detection of changes restored before the final snapshot. Hashing is
streamed and is not included in the child-process wall-time budget.

New application sessions also persist a session-level `BudgetLedger` and its
manifest reference. A standard `LLMClient` passed to the application is copied
with that ledger attached, without introducing a separate accounting path.

`SessionController.mutation_scope()` is the public grouping boundary for an
application mutation. It keeps a capability result, its state reference, and
the enclosing manifest under one process/OS lock. It does not turn the
application into a scheduler or hold a second copy of domain state.
While holding the lock, the controller checks that the saved manifest still
matches the version it loaded. A stale writer must reload; it cannot overwrite
another writer's budget or accepted references. Reinjecting services on reload
preserves persisted numeric settings, and unreadable runtime configuration is
reported rather than replaced with defaults.

For a timed-out LLM request, the ledger records the known request count. With
an output cap it retains the token reservation as a conservative estimate,
keeps actual token usage marked unknown, and permits retries within the
remaining budget. Unbounded unknown usage still blocks a finite token budget.

Attempt outputs are local to their attempt directory. Use
`SessionController.attempt_output_refs()` when a later capability should read
an earlier declared output: it returns session-root references such as
`attempts/attempt-001/result.json` without copying files or selecting a best
attempt. This keeps cross-capability handoff explicit and prevents a relative
artifact path from being resolved against the wrong store.
When a capability emits multiple domain outputs, use
`attempt_output_ref(..., kind=..., schema=...)` to require one unambiguous
artifact instead of relying on output order; ambiguous kinds fail explicitly.

`LifecycleProfile` provides four optional, built-in capability scopes:
`research_brief`, `survey`, `experiment`, and `full_research`.
When a session uses one of these names, the controller rejects a capability
outside its allow-list before execution. This is a scope check, not
an automatic workflow or a mandatory start point. Unrecognized profile names
remain unscoped for compatibility with older callers and experiments.
When a new session uses a recognized profile without an explicit `BudgetState`,
its default attempt budget is the number of named capabilities plus two bounded
recovery attempts. An explicit budget always wins; legacy manifests keep their
stored counters and limits; v1 stays read-only. Explicit migration creates a
separate application session and preserves the historical directory.
An attempt may inherit the session profile or omit it; it cannot replace a
scoped session with another profile.
The application exposes its current next action; Core does not infer one.
The built-in capability names are the actual stage boundaries: `plan`, `search`,
`document_ingest`, `read`, `synthesize`, `research_design`, `experiment`,
`analysis`, `report`, and `report_audit`. `analyze` remains a legacy alias for
`analysis`. `research_brief` is an application/profile name, not a hidden
composite capability; arbitrary caller-chosen names remain suitable only for an
unscoped or legacy session.

The ordered capability sequence is documented by the application workflow and
is not an implicit scheduler. Callers still provide capability inputs and may
choose a permitted backtrack explicitly.

The smallest end-to-end reference is
`tests/fixtures/capability_package_minimal/`. Run `uv run simple-ar-checks core` to
verify the boundary offline. New capability work should begin from this
contract and keep domain-specific request/result schemas outside the core.

Each controller-managed attempt records its capability in the attempt manifest
and stores one `capability_result.json`
containing only the `CapabilityResult` status, output references, diagnostics,
usage, and provenance. This preserves the result boundary after the process
ends without copying full text or raw logs; the legacy eight-stage artifact
layout is unchanged.

The old research facade and `pipeline_stages/` source package have been deleted.
Research domain modules own research behavior; historical readers remain separate.

CLI code is split by responsibility:

```text
src/simple_ar/cli/
  parser.py  argparse command and option declarations
  main.py    command dispatch and user-facing output
```

The old stage registry, handlers, common helpers and import aliases are retired.

Top-level implementation modules have been collapsed into domain packages.
Prefer direct imports from `core/*`, `app/*`, `integrations/*`, `research/*`,
`experiment/*`, `report/*`, or `code_task/*`. Do not reintroduce broad
compatibility facades for new code.

Research code is grouped by evidence lifecycle:

```text
src/simple_ar/research/
  planning/    research questions and executable query plans
  sources/     source plan contracts plus connector-neutral query objects
  connectors/  OpenAlex, Semantic Scholar, arXiv, and local-file adapters
  documents/   document records, full-text hints, parser/extractor helpers
  store/       chunks and local index backends
  evidence/    retrieval screening, coverage, optional debug evidence cards
  outputs/     search-stage artifact writers
```

Keep new retrieval/evidence work inside these packages instead of returning to
the old flat `research/*.py` layout.

### Replacing A Search Provider

`research.sources.base` defines the small provider port:
`SearchQuery -> SearchResponse`. `research.sources.registry.SearchProviderRegistry`
owns explicit connector factories, while the search stage keeps query planning,
deduplication, caching, and artifact projection. The default pipeline remains
backward compatible, but library callers can pass `provider_registry=` to
`execute_search` or register a new source name without changing those policies.
Provider implementations should stay focused on source access and return
normalized `Paper` objects; they should not write run artifacts or decide
research coverage.

### Using The Standalone Search Boundary

`research.sources.capability` provides the smallest multi-source search entry
point for library callers:

```python
from simple_ar.research.sources import (
    SearchRequest,
    default_search_provider_registry,
    search_sources,
)

result = search_sources(
    SearchRequest(
        queries=("research topic",),
        providers=("openalex", "arxiv"),
        max_results_per_query=5,
    ),
    registry=default_search_provider_registry(),
)
```

The result preserves one response for each provider/query pair and uses
`completed`, `partial`, `empty`, or `failed` to distinguish usable results,
source failures, and successful empty searches. This boundary does not write
stage files, select candidates, deduplicate papers, or download full text;
those remain policies of the existing Search stage and its callers.
When a caller needs a session handoff, `run_search_capability()` persists the
same normalized paper rows and provider/query response metadata as one
attempt-local `search_result.json`. It reports partial or empty searches as
non-complete outcomes and does not add retry or selection policy.
`SearchResult.from_handoff_dict()` restores that persisted `search_handoff.v1`
without network access, while retaining diagnostics for response rows that
refer to missing paper metadata.

### Reusing Document Ingest

`research.documents.ingest.build_document_bundle()` is the narrow composition
boundary for document metadata, permitted full-text handling, sections, and
chunks. It reuses the existing research records without calling an LLM or
writing stage artifacts. Search keeps ownership of index persistence and
legacy JSON/JSONL projections. Current callers pass the typed bundle or use its
handoff representation. `_legacy.documents.load_search_document_bundle(search_dir)`
reads archived Search JSON/JSONL from an explicit directory without creating a
runtime Context or advancing a historical run.

`research.documents.ports` provides the small `DocumentResolver` and
`DocumentParser` ports used after a manifest has selected a local resource.
`build_local_document_bundle()` is the direct local-document entry point; it
reuses the existing bundle, section, chunk, and Read logic without running
Search. The default resolver and parser preserve the existing local/cache
behavior, while callers can inject a resolver or parser for another storage or
document service.
`research.documents.LocalDocumentParser` is the reusable default implementation
for the existing plain-text, HTML, optional-PDF, and `unstructured` paths. The
legacy extraction helper still delegates to it, so selecting a different parser
does not require changing bundle construction or the old Search projection.
`DocumentBundle.to_handoff_dict()` and `from_handoff_dict()` define the
restorable `document_bundle.v1` representation. For a session-owned ingest,
`run_document_ingest_capability()` writes that bundle once as
`document_bundle.json` and exposes it through the attempt manifest; Read can
then be run in a later process by explicitly loading the bundle, without
re-fetching or duplicating it into another stage artifact.

### Reusing The Read Boundary

`research.evidence.reader.ReadRequest` accepts a `DocumentBundle` and optional
document or paper identifiers. `read_documents()` returns typed evidence cards
and diagnostics without writing files. It is deterministic by default; explicit
`use_llm=True` with a client enables bounded screening, notes and gap reading. The existing
`write_read_card_artifacts()` function remains a compatibility projection over
that boundary, so stage artifact paths and legacy callers stay unchanged.
For a session-owned attempt, `run_read_capability()` persists the same cards and
source locations as one `read_result.json` handoff; it does not copy chunk text,
fetch documents, or expand the selection.
Both generated and restored Read results validate declared `evidence_refs`
against the chunks in the same `DocumentBundle`; an unresolved reference is
recorded as a diagnostic and downgrades the result to `partial`. The same
side-effect-free check is available as `validate_read_evidence()`. It validates
explicit references only; it does not scan files or judge semantic correctness.
`query_evidence()` is the P05a source-resolution boundary: given a document or
explicit chunk IDs, it returns `EvidenceRef` rows with the source identity,
content revision, exact location, extraction status, target text, and real
same-document neighboring context. Unknown IDs are errors; an adjacent chunk
is never used as a substitute for a missing target.
Read handoffs consume this projection for `source_spans`, excluding raw text
so the document bundle remains the source of truth. Model notes retain bounded
overview coverage, gap queries and returned source windows, not a full-reading certificate.
Mixed-case lettered appendices are split from references when freshly parsing
retained text; prose cross-references/citation initials are not headings. Shared
`research.store.retrieval` ranks body terms and retained section headings for
navigation, and selects exact windows using nearby distinct terms with word/number
boundaries. Explicit captions, headings and phrases remain navigation cues, not
support judgments. Old bundles/notes are immutable; reparsing creates new artifacts,
and better lexical access does not establish source identity or semantic correctness.
Basic PDF parsing defaults to all pages; an explicit positive page limit remains
authoritative. Optional `ParsedDocument.coverage` feeds existing record/extraction
metadata, Reader diagnostics and Writer limits. A successful parse can still be
partial or have empty-text pages. External two-argument parsers and historical
bundles keep unknown coverage; no second state model or inferred retroactive proof.

### Reusing The Synthesis Boundary

`research.synthesis.SynthesisRequest` accepts the expanded evidence pack already
assembled by the research pipeline. `synthesize_evidence()` returns bounded
`IdeaCandidate`, `NoveltyCheck`, and optional `ExperimentContract` objects plus
an evidence-gap summary. It is deterministic by default and does not write
files. A caller may explicitly provide an LLM client through
`SynthesisRequest(use_llm=True, llm_client=...)`; that keeps the structured
derivation and adds model-generated, evidence-grounded prose. The stage-level
policy still owns persistence and any broader writing workflow.
The existing synthesis artifact writer uses this facade for its structured
evidence derivation, while legacy artifact paths remain unchanged. Compact
persisted packs contain card references, so callers should hydrate the card
rows before invoking the boundary.
For a session-owned attempt, `run_synthesis_capability()` persists the complete
bounded direction handoff as `synthesis_result.json`. It accepts an expanded
pack supplied by the caller and does not read private stage paths or decide
whether an experiment should run.
`SynthesisResult.from_handoff_dict()` restores that `synthesis_result.v1` handoff
without network or LLM access, including its idea rows, novelty checks, and
optional research-level experiment contract.

The research-level `ExperimentContract` in `research.contracts` describes a
grounded hypothesis and proposed change. The unused legacy design-package
builder, runtime-config converter and domain profiles have been removed.
Execution settings belong to the current experiment request; implementation
requirements belong to the CodeTask contract, not a second design package.
`ResearchExperimentContract.from_row()` restores the research-level handoff,
and `ExperimentRequest` accepts either that typed object or the historical
mapping form; canonical execution results preserve the contract without
reconstructing the retired design package.
In the vertical fixture, the restored contract is passed into the explicit
`ExperimentRequest`, so the execution result records the research-to-experiment
handoff rather than reconstructing the hypothesis from a private stage path.
If a typed research contract is supplied without an execution result schema,
its declared metric names form a minimal expected-metric view for downstream
analysis; an explicit execution schema always takes precedence. Historical
mapping inputs retain their previous behavior.
When that handoff should feed the standalone Experiment boundary, use
`experiment_request_from_synthesis()`. It restores `synthesis_result.v1`,
transfers only its existing research-level contract, and requires the caller
to provide an explicit `RunRequest`; it does not approve `needs_review`, choose
a command, execute, retry, or select a next stage merely because a contract is
present.

### Composing A Research Brief

`research.brief.build_research_brief()` is a small in-memory convenience: it
calls the Read boundary and passes the resulting evidence cards to the
Synthesis boundary. It accepts a Search-produced `DocumentBundle`, cached
documents, or a local-document bundle and returns `ready`, `partial`,
`needs_review`, or `empty`. Metadata-only input is not reported as sufficient
evidence. It does not search or write files; synthesis is deterministic by
default, while `ResearchBriefRequest(use_llm=True, llm_client=...)` explicitly
enables the shared LLM for grounded prose.

The user-facing `research-brief` and `research-session` applications do not
hide this composition anymore: they persist separate `read-001` and
`synthesize-001` attempts. The aggregate helper remains available for library
callers and old `research_brief.v1` handoffs, but it is not another lifecycle
stage.

There is intentionally no default session adapter for this aggregate. A
session must persist the `read` and `synthesize` attempts separately; callers
that only need an in-memory value can use `build_research_brief()`. Historical
`research_brief.v1` files remain readable, but they are not a second executable
lifecycle.

For a multi-attempt composition, restore a persisted Read result with
`ReadResult.from_handoff_dict(..., bundle=...)` and use
`evidence_pack_from_read()` to form the minimal Synthesis input. Keeping the
bundle explicit is intentional: it preserves one owner for source text while
leaving selection and sequencing to the caller.
The same rule applies downstream: restore the persisted analysis handoff and
derive report sections from its observed result data before passing them to the
standalone report assembler. The assembler does not infer or invent analysis
values from an input reference.

### Reusing The Experiment Boundary

`research.experiment.ExperimentRequest` wraps the existing execution
`RunRequest` with optional result-schema, contract, artifact, comparison, and
guard metadata. `run_experiment()` accepts the existing `ExecutionBackend`
protocol, defaults to `LocalExecutionBackend`, and returns the existing
`RunResult` together with canonical normalized results. It does not write files
or decide how an experiment is analyzed. `run_and_analyze()` is the small
composition when a caller wants both operations: it copies an
`AnalysisContext`, adds the observed metrics and canonical execution record,
and delegates to the existing result-analysis service. It preserves failed and
timed-out executions as analysis inputs, performs no retry or repair, and only
persists analysis artifacts when an output directory is supplied. The code-task
implementation therefore remains a backend, not a second experiment API.
When the request carries primary or required metrics, the composition exposes
those requirements to analysis without requiring callers to duplicate them in
the context.

Analysis owns its evidence and audit artifacts, not CodeTask repair state. The
unused `record_result_analysis_memory` bridge has been removed. CodeTask summaries
display recorded outcomes and repair notes; they do not infer extra blockers
from a negative comparison, an old failure file, or a missing memory event.

`research.experiment.run_experiment_capability()` is the opt-in session adapter
for execution. It registers under a caller-chosen name, writes the existing
canonical result as `results.json`, and stores the captured stdout/stderr under
the same attempt as declared `execution/stdout.txt` and `execution/stderr.txt`
artifacts. The canonical `passed`, `failed`, or `timed_out` status remains in
the result, while the raw streams remain available for diagnosis and alternate
downstream capabilities. Every non-passed execution maps to a failed
capability result, so the session layer cannot mistake a timeout for a
successful experiment. Analysis remains a separate capability and
`research.analysis.analyze_experiment_capability()` can consume the declared
result reference explicitly, writing a single `analysis.json` handoff with a
pointer back to the execution artifact. It returns `completed` only when the
analysis status is `passed`; missing evidence maps to `partial`, while explicit
failure and blocking remain visible to the session controller. Consumers that
cross a process boundary can use `AnalysisHandoff.from_handoff_dict()` to
restore the execution reference, observed execution status, and
`AnalysisResult` without rerunning or copying execution artifacts.
The adapter also reuses the existing result guard and diagnosis functions. It
writes `guard_report.json`, `diagnosis.json`, and a compact `diagnosis.md` in
the same attempt; a guard error fails the capability while the canonical result
keeps the underlying execution status. It does not retry, repair, or choose a
research transition.

### Reusing The Result-Analysis Boundary

`research.analysis.AnalysisRequest` and `analyze_results()` provide the
standalone analysis entry point. They reuse the existing metric normalization,
claim grounding, and audit implementation; deterministic analysis is the
default and persistence is opt-in through `output_dir`. The boundary does not
invent metrics, run code, or decide a research transition.

When a caller has two canonical execution results, use
`research.analysis.compare_experiment_results()` to produce the compact
`experiment_comparison.v1` mapping. It compares shared numeric metrics using
explicit directions (or directions embedded in the result schema), preserves
execution-status changes, and returns `inconclusive` when the required evidence
is missing or non-directional. The mapping can be supplied through
`ExperimentRequest.comparisons`; the helper does not retry, select a winner, or
choose a session transition.

`AnalysisResult.status` is the corresponding evidence-state summary. It is
derived only from an explicit canonical execution record, its guard, required
metrics, and explicit comparison verdicts: `passed`, `failed`, `blocked`,
`incomplete`, or `metric_below_target`. A standalone analysis without an
execution record remains `incomplete`; the status never chooses a retry or a
research transition. When persistence is requested, the same small handoff is
also written to `analysis_status.json`.

The application consumes typed analysis results and execution evidence directly.
There is no separate adapter into a Core research policy.

### Reusing The Report Figure Port

`report.ports.FigureRenderer` is the small substitution point for report
visuals. `DeterministicFigureRenderer` wraps the existing SVG implementation
and remains the default for report assembly. A future image or chart
backend can implement the same render method and consume the existing
`ReportDocumentPlan`, `ReportFigureConfig`, and `ReportFigureResult`; it does
not need to change writer, citation audit, or report assembly code. Callers
that own report orchestration can pass another renderer to
`assemble_report_document(..., figure_renderer=...)` or the corresponding
`run_report_capability()` entry. The old stage-level service is retired.

### Using The Report Assembly Boundary

`report.capability.ReportAssemblyRequest` and `run_report_capability()` expose
the downstream report boundary for callers that already have section drafts.
The adapter reuses `assemble_report_sections()`, the existing heading
numbering policy, and the `FigureRenderer` port, then writes one attempt-local
`report.md`. Each renderer-reported figure is also declared as a local
`figure` output ref; the optional figure manifest remains an index for readers.
If a renderer reports a file that is not present, the capability returns
`partial` with a `missing` ref instead of claiming completion. It does not
choose an outline, call an LLM, revise prose, or audit citations; those remain
separate concerns. This makes the report-to-audit handoff explicit without
introducing a second writer implementation or changing the legacy report stage.

`report.audit.ReportAuditRequest` and `audit_report()` provide the corresponding
side-effect-free audit boundary. They reuse the existing citation, metric, and
claim checks; report writing and revision remain orchestration concerns.
`ReportAuditCapabilityRequest` and `run_report_audit_capability()` are the
optional session adapter: callers pass explicit report/body artifact refs plus
the typed report context and memory, and receive one `report_audit.json`
artifact in the attempt. A warning maps to a partial capability result and a
failed audit maps to failed; the adapter never retries, rewrites the report, or
searches for an implicit “latest” artifact.

## Adding A Canonical Capability

New research work belongs to the capability/session path, not to the historical
eight-stage implementation. Add a capability in this order:

1. Define a typed request/result and its stable handoff schema in the owning
   domain package.
2. Keep domain behavior independent of CLI arguments, `Context`, and run-folder
   scanning; put external effects behind an explicit adapter or port.
3. Add the session adapter that writes one attempt-local artifact and maps
   domain status to `CapabilityResult`.
4. Register it in the relevant capability registry (for example
   `research.registry`) and compose it in an `app/` use case with explicit
   inputs, outputs, budget, and transition.
5. Add contract, application, failure/recovery, and CLI/example coverage as
   appropriate.

Canonical capabilities use explicit artifact references and compact handoffs.
Do not reintroduce the retired stage registry or a second lifecycle. `Stage` and
implicit directory lookup remain historical compatibility mechanisms.

## Experiment Preparation And Execution

Prepared-code execution uses `research.experiment` and `experiment.execution.backend`;
CodeTask owns isolated edits and validation. Do not add new Context-based design,
code or run stages: those executors and their stage-archive helpers are retired.

`research.preparation` reuses the CSV text-classification template for its supported
small-data baseline. New preparation support must have a concrete input contract
and a bounded executable example; do not add a general project generator merely
to populate all possible experiment types. Keep unknown measurements unknown.

Historical experiment readers and the current bridge remain available. Their
remaining duplicate lifecycle logic is cleanup debt, not an extension point.

## Extending Code Task

The code-task workflow is grouped by lifecycle rather than being a flat folder:

```text
src/simple_ar/code_task/
  runtime/        config and path/manifest helpers
  workspace/      copy, git worktree, sparse-copy preparation
  analysis/       codebase index, repo map, locate, context packs
  editing/        work plan, patch plan, edit budgets, patch proposal/application
  execution/      environment probe, validation, benchmark, comparison, repair
  orchestration/  init and execute flows that compose the lower-level modules
```

New code should go into the lifecycle package that owns the behavior. Avoid
adding more flat files directly under `code_task/` unless the file is a public
facade or a genuinely cross-cutting boundary.

When adding a new code-task feature:

- keep original source directories read-only;
- write artifacts under `code_task/meta`, `code_task/run`, or `code_task/repairs`;
- keep CLI steps explicit until the underlying behavior is stable;
- add tests that exercise both the library function and CLI path when useful;
- prefer small composable functions over a single agent loop.

Metric comparison should stay conservative. Unknown numeric metrics may be
recorded as deltas, but they should not decide an improved/regressed verdict
unless their direction is known from explicit manifest configuration or a
simple local heuristic. Add new default heuristics only when the metric naming
convention is common enough to be unsurprising.

## Extending Report And Audit

`report/survey.py` retains source routing and consumption of existing survey
section metadata. The old service's unused contract builder, taxonomy generator,
planning-file writer and separate coverage audits are retired. Extend the active
Writer/document-plan and report/audit boundaries instead of restoring that second
pipeline. Historical `survey_contract` context remains readable; the builder-only
runtime toggle and longform `planning_artifacts` option no longer exist.

The report system is the outlet for research-only surveys, experiment reports,
and embedded code-task results. Keep it template-driven and
evidence-aware rather than turning it back into a single prompt or a single
large service file.

```text
src/simple_ar/report/
  schema.py        Pydantic models for context, memory, tools, drafts, reviews
  projection.py    project persisted research and measurement evidence into report inputs
  templates.py     load Markdown templates and reviewer criteria
  memory.py        compact section plan, evidence handles, claims, limitations
  tools.py         report tool schema definitions
  tool_gateway.py  bounded read-only tool execution
  retrieval.py     source-handle backtracking
  agent.py         Writer/Reviewer orchestration
  citations.py     citation key mapping, display labels, and citation cleanup
  audit.py         citation, metric, claim, and reviewer audit aggregation
  assembler.py     section drafts to final Markdown
  writing.py       controller-owned Writer execution and checkpoints
  capability.py    report assembly and artifact packaging
```

When adding report behavior:

- put schemas in `schema.py`, not ad hoc dictionaries in the writer;
- put new source projection or backtracking logic in `projection.py`,
  `retrieval.py`, or `tool_gateway.py`;
- put Writer/Reviewer loop behavior in `agent.py`;
- put citation mapping, display conversion, and citation cleanup in
  `citations.py`;
- put mechanical checks in `audit.py`;
- keep templates and criteria in `templates/report/`, not hard-coded prompt
  strings;
- keep Writer execution in `writing.py` and assembly in `capability.py`.

Source lookup is retrieval, not a semantic verdict. A passing review with pending
context requests must consume the fetched results and be rejudged before adoption;
unresolved requests remain a visible verification gap. Preserve tool results in
checkpoints and format-recovery prompts, prioritize recent evidence when bounding
context, and do not introduce a separate source index or task-specific paper rules.

`app/research_report.py` now only reads historical evidence; it no longer runs a
Writer or appends fixed report/audit attempts. `app/research_application.py`
owns session decisions and `cli/main.py` owns user-facing dispatch. Keep those
responsibilities distinct and remove redundant state or execution ownership
rather than distributing the same complexity across more files.

## Extending Tools And External Agent Backends

The common tool and handoff layer provides an opt-in boundary without replacing
the existing domain implementations:

```text
src/simple_ar/tools/
  specs.py        CommonToolSpec, ToolCall, ToolResult, permission/risk enums
  registry.py     compose report and experiment tools into one registry
  gateway.py      permissioned local dispatch and compact trace writing
  permissions.py  read/write/execution/network policy checks
  openai_schema.py / mcp_schema.py
                  schema export only; no server is started by default
  mcp_server.py   explicit stdio MCP server for read-only run-local tools

src/simple_ar/agent_backends/
  base.py         AgentBackend protocol and run result models
  policy.py       external-agent permission policy serialized into handoff
  handoff.py      workspace-scoped handoff package and untrusted output ingestion
  factory.py      provider selection for fake/local_llm/Codex/Claude/OpenCode
  fake.py         deterministic backend for integration tests and dry-runs
  local_llm.py    LLM-backed bounded reviewer/planner backend
  external_cli.py subprocess wrapper with cwd, timeout, env allowlist, and logs
  profiles/       Codex / Claude Code / OpenCode profile Markdown
```

The common layer is intentionally thin. `experiment/tools/` and
`report/tool_gateway.py` still own their business logic; `tools/` only provides
one audited surface for future OpenAI tool calling, MCP adapters, and external
agent backends.

Rules for new tool/backend work:

- register real tools only; do not add MCP/OpenAI schemas for stub tools;
- experiment-domain tools currently read archived numbered-stage directories;
  they are not the canonical session result API. Unimplemented run/repair/apply
  stubs and the duplicate experiment-specific OpenAI exporter are retired;
- keep write, shell, network, and secret access disabled unless a config and
  approval path explicitly enables them;
- write external-agent context into `agent_handoff/<name>/`, never into a
  user's global tool directory by default;
- treat external-agent outputs as untrusted. Ingest them under
  `agent_outputs/<name>/`, then route them through the existing patch,
  result-guard, report-audit, or code-task validation paths;
- keep external CLI providers opt-in. `fake` and `local_llm` are useful for
  tests and local review; `codex`, `claude_code`, `opencode`, and
  `external_cli` must stay disabled until the config explicitly allows them;
- keep trace rows compact by default. Raw prompts, raw outputs, or large
  payloads belong behind debug settings.

### Code-Task Environment Policy

The current code-task runner has workspace isolation through `copy`,
`git_worktree`, or experimental `sparse_copy`, command timeouts, optional
streamed benchmark output, captured stdout/stderr, a restricted environment
map, and an explicit execution interpreter policy. It supports `current` and
`external` modes, but it does not yet create or install into a separate Python
environment. Unless a future feature explicitly changes this, do not install
user project dependencies into SimpleAutoResearch's own `.venv` by default.

Environment support should evolve in layers:

- `current`: run with the current SimpleAutoResearch Python. This is simple and
  useful for demos, but it is not dependency isolation. Supported now.
- `external`: run with a user-provided Python or Conda interpreter. This should
  be the first practical escape hatch for real projects that already have an
  environment. Supported now.
- `project-venv`: create a per-run environment under
  `code_task/.venv/`. This isolates well but can waste disk space. Planned.
- `shared-env-cache`: create or reuse environments under a cache directory such
  as `.simple_ar_cache/envs/<env-hash>/`, keyed by OS, Python version, and
  dependency files. This is the preferred long-term default. Planned.
- `docker`: run inside a container for stronger isolation. Keep this separate
  from the Python runner because Windows, GPU, and image-build behavior need
  careful handling. Planned.

Future environment creation or dependency installation must be explicit,
auditable, and recorded in artifacts. A safe implementation should record the
selected mode, interpreter path, dependency files, install commands, exit codes,
and warnings in `code_task/meta/environment_report.json` or a dedicated
environment artifact.

## Documentation Rules

Use the docs this way:

- `README.md`: project entry, setup, quickstart, workflow overview, links.
- `docs/USAGE.md`: installation, env configuration, and workflow walkthroughs.
- `docs/CLI_REFERENCE.md`: command groups and option tables.
- `docs/CONFIG_REFERENCE.md`: TOML schema and configuration examples.
- `docs/WORKFLOWS.md`: what each workflow/stage does and what files it produces.
- `docs/DEVELOPMENT.md`: contributor guidance.
- `CHANGELOG.md`: dated, user-visible changes and compatibility notes in reverse chronological order.
- `MDfiles/`: private or learning-heavy planning notes, usually ignored from GitHub.

Update the relevant section in its existing style instead of appending a progress note.
Keep commands, worked examples and interface contracts where readers need them;
conciseness alone is not a reason to remove them. CHANGELOG has one heading per date,
with related changes merged under that date. Internal debugging and acceptance details
belong in maintainer records. Keep English and Chinese instructions aligned.

## Tests

Use layered checks during development:

```bash
uv run simple-ar-checks --list
uv run simple-ar-checks quick
uv run simple-ar-checks intake
uv run simple-ar-checks data
uv run simple-ar-checks code-task
uv run --extra examples simple-ar-checks pipeline
uv run simple-ar-checks research
uv run --extra examples simple-ar-checks code-task-examples
uv run simple-ar-checks core
```

The same runner can be called without the console script:

```bash
uv run python scripts/run_checks.py code-task
```

Recommended validation layers:

| Change area | Suggested check |
| --- | --- |
| Docs only | `git diff --check` plus manual link review. |
| Small parser, prompt, config, metric, or CLI changes | `uv run simple-ar-checks quick`. |
| Code-task internals, workspace, repo-map, patching, validation, runner, repair | `uv run simple-ar-checks code-task`. |
| Bundled code-task examples or benchmark examples | `uv run --extra examples simple-ar-checks code-task-examples`. |
| Experiment templates and their execution | `uv run --extra examples simple-ar-checks pipeline`. |
| Literature, retrieval, evidence ledger, report generation, LLM adapter | `uv run simple-ar-checks research`. |
| Core capability boundary, registry, attempt store, and package example | `uv run simple-ar-checks core`. |
| Intake, application state, candidate assessment | `uv run simple-ar-checks application`. |
| LLM transport and accounting | `uv run simple-ar-checks llm`. |
| Report changes only | `uv run simple-ar-checks report`. |
| Guided/chat setup and shared CLI/TOML inputs | `uv run simple-ar-checks intake`. |
| Table analysis, figures and writing handoff | `uv run simple-ar-checks data`. |
| Local process control and execution results | `uv run --extra examples simple-ar-checks execution`. |
| Shared-interface/architecture checkpoint or release candidate | `uv run --extra examples simple-ar-checks all`. |

For a small change, select the affected unittest module or method directly.
Combining check groups deduplicates modules; `all` supersedes other groups.
Do not rerun the full suite merely because a commit is being made. Preserve
useful regression tests; remove one when its behavior is obsolete or demonstrably
covered by another retained test. A green mock suite does not replace real execution.

Groups are smoke selections, not exhaustive coverage of a domain. Run a changed
module directly, for example `uv run python -m unittest tests.test_report_source_backtracking`.
The `code-task` group covers execution in `test_code_task` and context selection,
named windows, review and repair consumers in `test_code_source_views`. Keep
these context scenarios together rather than creating a module per retrieval
fix. Execution scenarios share a temporary-root fixture, not task policy.
Whole-document drafting, review and joint revision belong to
`test_report_document_first`; adopted-note history and persisted input identity
belong to `test_report_checkpoints`. Keep their scenario-specific clients and
assertions explicit; do not add a module for each corrective iteration.
Data tests separate responsibilities: `test_table_analysis` owns the five-plot
CLI/freeze/recovery/relocation/import lifecycle and distribution-package checks; `test_table_figures` owns
distributions, matrices and exports; `test_coordinate_figures` owns coordinate
and grouped-series behavior, association evidence and recovery; `test_report_data_delivery` owns writing projection.
Do not repeat a complete CLI fixture for each new plot. The product follows the
same boundary: `table._write_analysis_delivery` emits packages for analysis,
writing imports and explicit rebuild; validation and completed recovery stay separate.
Shared fixtures belong to the `tests` package; neither named tests nor full
discovery should require adding `tests/` to `PYTHONPATH`. Discovery uses
`python -m unittest discover -s tests -t .`. Prefer outcome and side-effect
assertions to exact private call counts or wording. For optional static cleanup,
use `uvx ruff check src/simple_ar tests --select F401,F811` and review each finding;
do not auto-delete public re-exports or imports with required side effects.

Experiment and CodeTask use `core/process.py` for bounded output capture and
process lifetime. Each stream keeps a 200 KB memory tail and, when an output
directory is supplied, a 2 MB log prefix; discarded-byte counts remain visible.
Metrics parsed from stdout therefore cover the retained tail only. Dedicated
metric-file consumption remains follow-up work. Canonical experiment attempts
declare invocation/log artifacts; CodeTask history references its invocation.
The Windows Job Object is attached after spawn, leaving a startup attachment
window; POSIX uses a process group. Neither mechanism is a hostile-code sandbox.
CPU, GPU and memory enforcement are explicitly unimplemented.
`LocalExecutionBackend(budget_ledger=...)` and `execute_code_task(..., budget_ledger=...)`
can share the application's ledger for benchmark calls, with `session_id` and
`attempt_id` linking actual invocations. The process boundary reserves
`process_invocations` and `process_wall_seconds`, then settles observed wall time
(not GPU utilization or GPU hours). `settle_process_record` reuses finalized
records without executing code. Environment probes/setup helpers are not yet
included in process accounting; they remain explicit preflight inputs rather
than hidden experiment actions. ResearchApplication execution actions use this
process boundary.
`execute_code_task(..., llm_client=...)` forwards the injected client through
existing-project planning/edit/review/repair and greenfield generation/repair.
`LLMClient.for_task` retains provider settings, the session ledger and attempt
identity while adding the owning task's usage observer; model overrides change
the role model, not the original client. Existing generation review clients
already own their usage observer and must not register a duplicate one.
The legacy experiment bridge now delegates preparation through validation to
the official executor and retains only its explicit approval/stop mapping and
artifact projection for those steps. Baseline process failure stops preparation
before planning; it is not equivalent to a valid negative research result.
Its bounded verification/auto-repair sequence remains a compatibility concern.
The canonical application now calls this CodeTask preparation path and keeps
implementation separate from measured experiment actions. The legacy bridge's
remaining behavior still needs to be reduced before it can be removed.

Run the full test suite directly when needed:

```bash
uv run --extra examples python -m unittest discover -s tests
```

Run the realistic code-task example:

```bash
uv run --extra examples python -m unittest tests.test_code_task_examples
```

Run the execution-boundary tests (output capture, timeout, and a small template):

These call `LocalExecutionBackend` directly; the unused `experiment.runner`
script wrapper and its `ExperimentRunError`/`ExperimentRunResult` aliases are retired.

```bash
uv run --extra examples python -m unittest tests.test_experiment_runner
```

Run config and public example config loading tests:

```bash
uv run python -m unittest tests.test_run_config
```

## Preview Completion Priorities

The preview is not a completed V2.9 release. The next acceptance work should:

- Complete a paper-to-reproduction task, including preparation, measured results,
  a substantive feedback revision and a portable delivery. Prepared commands or
  an initial PDF alone do not close this requirement.
- Check conceptual-image feedback against the requested meaning and layout,
  and validate cross-module use on one installation candidate. Earlier successful
  reports or figures on other candidates remain evidence with their original scope.
- Retire replaced implementation, templates and duplicate tests together after
  checking public and historical consumers. Keep permissions, observed execution,
  unknown usage and recovery; moving code between files is not simplification.

Named-connection authentication is only a deployment check, not evidence of
long-stream reliability or task quality. Freeze against complete user outcomes,
not test counts, compilation success or a collection of demonstrations.

## Git Hygiene

- Keep unrelated refactors out of feature commits.
- Do not commit `.env`, run outputs, caches, or private learning notes.
- Keep README concise; move detailed behavior into docs.
- Update `CHANGELOG.md` when user-facing commands, artifacts, or workflow behavior changes.
