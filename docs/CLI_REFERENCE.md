# CLI Reference

[中文版本](CLI_REFERENCE_zh.md) · [Usage](USAGE.md) · [Configuration](CONFIG_REFERENCE.md)

Use this page for syntax, options and recovery rules. Prefix examples with `uv run` when running from a checkout. `SESSION` is the printed full session path; `RUN_DIR` is a run directory. Put `--command ARGV...` last: everything after it belongs to the child command, without shell interpretation.

## Command Overview

| Need | Command |
| --- | --- |
| Start a goal or resume native work | [start](#guided-entry) |
| Inspect model settings without API calls | [models](#simple-ar-models) |
| Read saved status / deliveries | [status and results](#simple-ar-status) |
| Reuse a registered result in a new task | [Reuse deliveries](#reuse-deliveries) |
| Generate/edit a raster concept or inspect pixels | [image / image-review](#concept-images) |
| Inspect project preparation without execution | [project-info](#project-preparation) |
| Run an exact research protocol | [research-session](#research-commands) |
| Add/refresh a report or export an ACM project | [research-report](#simple-ar-research-report), [report-export](#simple-ar-report-export) |
| Edit an isolated code project | [code-task](#code-task-commands) |
| Index/search artifacts or remove caches | [Artifact tools](#artifact-tools) |
| Export schemas, call tools or serve MCP | [Tools and MCP](#tools-and-mcp) |
| Inspect native filesystem/evidence/recovery contracts | [Advanced native contracts](#advanced-native-contracts) |

`simple-ar --help` and command `--help` describe the installed version. Native tools and isolated workspaces do not certify scientific correctness or provide an OS sandbox.

## Guided entry

~~~bash
simple-ar start
simple-ar start --guided
simple-ar start "Explain these results" --source results/ --model profile:daily
simple-ar start --kind writing --material notes.md --prepare-only
~~~

Bare start asks for a goal. A positional request or ordinary `--goal` enters the native action loop. `--guided` and ordinary kinds select an outcome preference for that same loop; they do not grant access.

### Native task entry (experimental)

| Option | Meaning / default |
| --- | --- |
| `REQUEST`, `--goal TEXT` | Natural-language goal |
| `--guided`, `--kind KIND` | Preferences: survey, bug_fix, reproduction, writing, data_analysis, figure, code |
| `--model SELECTOR` | env by default; catalog or legacy connection. Native decisions require Chat |
| `--output-root PATH` | Default runs/assistant |
| `--source PATH` | Repeatable read-only regular file/directory mount |
| `--document PATH`, `--material PATH`, `--project PATH`, `--data-path PATH`, `--data-file PATH` | Ordinary preference inputs become read-only mounts with declared roles |
| `--project-python PATH` | Existing process interpreter; no installation or environment creation |
| `--allow-network` | Public source search/acquisition; separate from process access |
| `--allow-execution` | Allow process proposals; every command still needs approval |
| `--allow-vision` | Send mounted PNG/JPEG/WebP to configured vision route |
| `--max-images N` | Generation/edit submission allowance; default 0. Does not disable separately authorized vision |
| `--max-turns N`, `--max-tokens N` | Saved task-wide allowances: 30 model requests, 600000 tokens |
| `--prepare-only` | Save/preview without calls or execution; new native tasks still preflight connection/credentials |
| `--yes` | Accept displayed task/access/resource summary, not command approval |
| `--resume-task SESSION` | Resume saved native state, optionally with feedback |
| `--tasks-in PATH`, `--select-task` | Read-only native task listing in a directory and direct children; select interactively or use `--resume-task EXACT_NAME` |
| `--write-task-config PATH`, `--task-config PATH` | Export annotated native creation TOML and exit; load after review, with explicit CLI values taking precedence; not a saved-task reset |
| `--max-processes N`, `--max-process-seconds N` | Cumulative native creation process count/wall-second caps, including search backends; zero is respected; resume keeps its ledger |
| `--supply-task PATH` | With native resume, confirm a TOML supply of missing contracts; records only, no work is executed |
| `--approve-execution TOKEN` | Exact current printed approval token; requires native resume |
| `--decision-id ID`, `--decision-response accept\|reject\|revise`, `--decision-guidance TEXT` | Reply to a frozen research design on resume; guidance accompanies revise. Not process approval |
| `--retry-model` | Explicitly retry a closed failed model decision; see recovery below |
| `--authorize-remaining DIMENSION=AMOUNT` | Repeatable new remaining allowance on resume |
| `--authorization-id ID`, `--authorization-reason TEXT` | Required stable ID/reason for allowance changes |

New source mounts reject symlinks and private/runtime paths. Outputs belong in `workspace/`. Data-file preference inputs retain a 20 MiB bound; use source mounts for larger bounded investigation. Public document URLs are refused by the ordinary preference adapter: put URLs in the goal and grant network acquisition separately.

### Goal-driven code, analysis and figures

~~~bash
simple-ar start --kind figure --document method.md --goal "Draw observations → comparison → evidence" --allow-execution
simple-ar start --kind code --material requirements.md --goal "Build a reusable CSV validator" --allow-execution
simple-ar start --kind data_analysis --scripted --data-file measurements.csv --goal "Compare cost and quality" --allow-execution
~~~

Figure work requests layout/code, PNG, editable SVG and explanation. Scripted analysis requests runnable source, computed results and explanation. Preferences do not restrict tools or plot types, grant permissions, manufacture data or prove completion. Explicit column/unit/pairing/presentation declarations are retained in the goal; unprovided preset defaults do not restrict native work. `--scripted` requires data_analysis.

### Native recovery

To add missing conditions to a caller-directed native task, use a supply file rather than model retry:

```toml
supply_id = "first-model"
reason = "Continue from the retained materials"

[changes]
model = "profile:daily"
allow_execution = true

[resource_allowances]
llm_requests = 20
total_tokens = 600000
process_invocations = 8
process_wall_seconds = 600
```

```bash
simple-ar start --resume-task SESSION --supply-task supply.toml --prepare-only
simple-ar start --resume-task SESSION --supply-task supply.toml --yes
simple-ar start --resume-task SESSION
```

Supply uses the same validation as native creation. It can add `sources`, `execution_contract`, `code_task`, `research_policy`, `table_analysis`, `report_requirement` and `max_output_tokens` under `changes`. By default it only fills missing contracts. To change an existing native contract, set top-level `replace_existing = true` and use a new ID and reason. Only fields named in `changes` are replaced; when `sources` is present in replacement mode, it is the complete new alias map, not a merge. Old records and charges remain history, not approval or completed evidence for new conditions. Report-only changes retain valid measurements. Model connection changes still use the separate retry contract.

Source paths resolve from the supply file; execution cwd must be an existing absolute directory and argv remains literal. Changed protocols need fresh acceptance when confirmation is required; actual execution always needs its own exact approval. First model binding requires both positive request and token allowances; resource continuation alone cannot grant model access.

The same ID and terms are idempotent, including recovery after a grant was recorded but the config commit was interrupted. An interrupted supply blocks work until that supply is completed. In-flight/unknown operations must first be reconciled. Supply is separate from feedback, retry, resource flags and command approval; it does not modify saved legacy ResearchApplication protocols.

~~~bash
simple-ar status SESSION
simple-ar results SESSION
simple-ar start --resume-task SESSION
simple-ar start --resume-task SESSION "Shorten the explanation without rerunning"
simple-ar start --resume-task SESSION --approve-execution TOKEN
simple-ar start --resume-task SESSION --source notes/ --allow-network --yes
simple-ar start --resume-task SESSION --retry-model
simple-ar start --resume-task SESSION --authorize-remaining llm_requests=10 --authorize-remaining total_tokens=100000 --authorization-id continuation-1 --authorization-reason "Finish the saved goal" --yes
~~~

Ordinary resume consumes saved boundaries. A completed task with no pending work returns terminal state without replay. Feedback shares the original budget and cancels only unstarted proposals; attempted/unknown operations must first be reconciled. An old delivery during a paused revision is not completion of the new request.

Adding sources/network/execution/vision needs confirmation. Invalid additions leave mounts, permissions and ledger unchanged. Existing model/interpreter/work/budgets remain. On resume, prepare-only previews without applying access or resources. Confirm grants separately from command approval.

New remaining allowances are future capacity, not historical totals. ID/reason without allowances fails. Replaying an ID does not replenish spent capacity; ordinary max-turns/max-tokens cannot add resources on resume. History and bounded unknown charges remain reserved. In-flight calls, unbounded unknown use or unconfirmed side effects require reconciliation before continuation.

Retry-model permits a new decision only after a terminated failure without an accepted reply, using saved finite output cap and remaining capacity. It does not replay tools or whole tasks. Cached replies use ordinary recovery; in-flight requests, unconfirmed reservations, unbounded charges and pending tools refuse retry. Use it separately from feedback, grants and command approval.

For that closed failure only, add `--model profile:NAME` or `route:NAME` to record a replacement Chat connection with the same finite output cap. Text/code/writing bindings and transition are saved together; vision retains its binding. Missing required reasoning history is not reconstructed; choose a compatible non-thinking profile. Cached replies cannot switch. A different explicit model without retry is rejected; omission retains the saved selector. Prepare-only makes no retry or mutation.

### Structured compatibility selection

Most explicit protocol controls select structured input adaptation, even if an explicitly supplied value equals a parser default; new tasks still use the common task owner. Source/retrieval policies, report templates/limits, reproduction commands/metrics/environment, table physical limits and interaction settings retain exact semantics and cannot mix with native access/budget controls. Figure/scripted requests with incompatible strict controls fail before creation. Creation preferences cannot change a resumed native task.

New `--kind bug_fix` with `--project`, `--allow` and `--validate` together uses the native session and the exact CodeTask owner. The work, original source, validator and environment require explicit approval; generic workspace writes/processes are unavailable under this frozen contract. Feedback may reference a validated candidate for a separately approved revision. Additional unmapped protocol controls are rejected, not ignored. Registered legacy project reuse retains its original editing/budget policy.

`--chat` requires a terminal/model and uses structured technical setup; `--resume-setup PATH` resumes its saved draft, not execution. Do not combine chat and prepare-only. Setup may inspect materials and propose settings but does not execute or install. Explicit user options remain authoritative.

| Structured start options | Meaning |
| --- | --- |
| `--sources materials\|search`, `--document PATH`, `--fulltext`, `--web-extract-backend direct\|tavily_basic`, `--max-cited-sources N` | Source policy, repeatable papers, full text, page acquisition backend, final citation cap |
| `--material PATH`, `--template NAME_OR_PATH` | Repeatable notes/drafts/results and report form |
| `--project PATH`, `--allow PATTERN`, `--validate TEXT` | Project, repeatable exact edit scope, independent validator |
| `--data-path PATH`, `--project-python PATH` | Repeatable execution input locations, existing interpreter |
| `--hypothesis TEXT`, `--dataset TEXT`, `--expected-outcome TEXT`, `--metric NAME` | Prepared reproduction conclusion/conditions/criteria and repeatable metrics |
| `--cwd PATH`, `--timeout-sec N` | Reproduction directory, per-process timeout (default 300) |
| `--output-files JSON`, `--metric-sources JSON` | Registered producer files and explicit JSON/unique-row numeric selectors |
| `--environment current\|venv`, `--requirements PATH`, `--install-project` | Explicit dependency preparation; repeatable requirements |
| `--check-argv JSON` | Independent short premeasurement check; no inferred command or scientific metrics |
| `--command ARGV...` | Formal command, last option |
| `--interaction assisted\|checkpoints\|autonomous` | Protocol/design confirmation policy for new tasks; legacy milestones follow the saved owner |
| `--with-report` | Data/repair report from saved results; requires model |
| `--asset-max-mb N` | Chat project/data download capacity; default 20 MiB, ZIP expansion 4×; not download permission |

Scoped reproduction adaptation requires project + allow + independent validate. Validator must differ from formal command after interpreter resolution; cwd must match project. Protected tests/secrets/data remain read-only, edits occur in isolated worktree/copy, and validation precedes measurement. Use current/existing Python or explicit task venv preparation; installation failure stops work. Do not add a second check-argv beside CodeTask validation. No scientific method/split/condition change is authorized.

Chat may propose exact new Python adapter paths and a source-backed validator; confirmation saves them. Place outputs outside the author project. Without a creation declaration, allow paths must already exist. Project-python selects an existing interpreter, or the venv base when explicit preparation is chosen; setup cannot select an interpreter from untrusted source instructions.

Chat acquisition proposals require separate asset confirmation and inspected literal URLs. Only public HTTPS is accepted; userinfo/fragments/credential-bearing or signed links are refused. Project assets support direct ZIP or GitHub default-branch snapshot, not pinned commits. Data remains one file without unpacking. Default limits: 3 assets, 20 MiB each, project expansion 80 MiB/5000 entries. ZIP format is checked from bytes; configured limits persist for recovery.

Supporting material uses one public GET without redirects and separate PDF permission. Preview is 8000 characters/three PDF pages; expanded local extraction needs confirmation (≤40 PDF pages/20 MiB). Question reads send bounded excerpts. Setup discovery permits two rounds, two queries per round, five results per query/provider; Tavily+OpenAlex with Tavily key, otherwise arXiv+OpenAlex. Responses/acquisition receipts are reused; unknown interruptions are not resent. A failed paper may be explicitly skipped while its receipt/unknown consumption remain. accept-proposal adopts displayed assumptions, not missing facts or execution permission.

### Existing data (no API)

~~~bash
simple-ar start --kind data_analysis --data-file observations.csv --value-column score --observation-unit "one run"
~~~

Explicit value-column/x-column/paired-baseline without scripted selects deterministic TableSpec analysis; a path alone remains a native preference. Add with-report and model for writing. These options are also available on research-session.

| Option | Default / purpose |
| --- | --- |
| `--data-file PATH` | UTF-8 CSV/TSV or JSON records |
| `--value-column NAME`, `--group-column NAME` | Repeatable numeric columns; optional grouping |
| `--observation-unit TEXT`, `--value-unit TEXT`, `--data-attribution TEXT` | Required row meaning, declared units/source |
| `--data-mode observations\|values`, `--data-plot bar\|box\|heatmap\|line\|scatter` | observations, bar |
| `--data-missing reject\|omit` | reject |
| `--paired-baseline COLUMN` | Same-row candidate-minus-baseline comparison |
| `--x-column NAME`, `--x-unit TEXT`, `--data-association none\|pearson` | Coordinates; association default none |
| `--series-layout separate\|shared`, `--figure-width column\|wide` | separate, wide |
| `--data-max-mb N`, `--data-max-points N`, `--data-max-figures N` | 20 MiB, 10000 positions/cells, 100 figures; no sampling on overflow |

Report context uses material/document on start, material/local-document on research-session. Analysis-only ignores model settings. [Transformation constraints](CONFIG_REFERENCE.md#existing-data-descriptive-analysis) define valid combinations and statistical meaning.

### `simple-ar models`

~~~bash
simple-ar models --config models.toml
simple-ar models --config models.toml --profile daily --purpose writing
~~~

This command inspects named connections locally without API calls or exposing credentials.

| Option | Purpose |
| --- | --- |
| `--config PATH` | Catalog TOML; otherwise use `SIMPLE_AR_MODELS_CONFIG` or the user configuration |
| `--profile NAME` | Inspect one named profile |
| `--purpose text\|code\|writing\|vision\|image` | Connection purpose; default `text` |

See [named model connections](CONFIG_REFERENCE.md#named-model-connections) for catalog fields and routing rules.

## Reuse deliveries

~~~bash
simple-ar results SESSION
simple-ar start --from-session SESSION --reuse data_analysis --goal "Explain the trade-off"
simple-ar start --from-session SESSION --reuse report --goal "Reorganize the discussion"
simple-ar start --from-session SESSION --reuse code_project --goal "Revise the implementation and validate it"
~~~

Repeat reuse selectors from data_analysis, report, summary, code_analysis. Ordinary writing creates a new native task with registered packages, not the prior ledger. Original delivery remains; valid measurements are not rerun. Reports retain source passages/citation identities/evidence, with old prose separately treated as a draft. Analysis packages recheck copied data. Metadata is not primary evidence or independent verification.

code_analysis packages retain scripts/data/results/PNG/vector exports; analysis.py is the entry, src helpers/layout sources travel with it. Results may register figures with package-relative path/caption and citation_map.v1. Authored captions/known references are retained; missing bibliography is not invented. Legacy deliveries without packages keep text/JSON reuse.

code_project starts an isolated revision, preserving registered scope, protected files, validator, interpreter and patch policy. Ordinary revisions use the native task owner, without intermediate TOML or the old research plan; commands still need approval. Both structured results and current formally delivered native CodeTask projects with a fixed scope and validator can be selected. Changed/incomplete candidates are not offered; the original task and ledger remain unchanged. Do not mix it with writing selectors or override project/scope/validator.

A validated single-command reproduction project can be selected with kind reproduction, document and goal to repeat its saved protocol/interpreter/output selectors; confirm execution. Scientific reproduction and explicit technical controls retain their structured contract. Paired/extended protocols stay on original resume.

Survey follow-up with kind survey + reuse report uses its original source bundle, excludes drafts/derived attachments and defaults to online search. Text versions are not merged across versions; sources still need new relevance checks. Reuse is distinct from interrupted execution recovery.

## Concept images

~~~bash
simple-ar image --config models.toml --prompt "A method overview" --output figures/v1
simple-ar image --config models.toml --input figures/v1/image.png --prompt "Keep layout; use green" --output figures/v2
~~~

| image option | Purpose |
| --- | --- |
| `--prompt TEXT`, `--output PATH` | Required prompt and new version directory |
| `--input PATH` | Optional edit source; original preserved |
| `--config PATH`, `--model SELECTOR` | Catalog / explicit profile; default image route |

Generation/edit capabilities must match. Identical completed requests restore without calls; failed/unknown submissions are not resent. openai_images requires base64 output, rejects URL-only. Explicit cctq_images_async saves task ID and recovers the same-origin files/0 through authenticated GET, without another POST or API/provider fallback. Completed IDs download directly; timeout/HTTP settings may change for recovery. Unknown dispatch without ID is not resubmitted. Raster concepts are not measurements or editable vectors.

### Inspect a rendered figure

~~~bash
simple-ar image-review --config models.toml --input figures/v1/image.png --goal "Check labels and arrows" --output figures/review-v1
~~~

Required input (repeat up to four), goal and output; optional config/model selects vision route. Static PNG/JPEG/WebP limits: combined 20 MiB, each ≤4,194,304 pixels. Review uses connection cap or 3000-token fallback, records absent usage as unknown and saves originals/raw response/review.json/feedback.md. Invalid responses stay in rejected-response.json. Identical completed requests reuse; raw responses can normalize offline; changed inputs need new version. Interrupted requests without response are not resent. Review does not edit or certify figures.

## Project preparation

`simple-ar project-info --project DIR [--data-path PATH ...] --output NEW_DIR` saves preparation.json/.md: indexed entries/dependencies/excerpts/declared data locations. Relative data paths use project root. It neither imports/runs project code nor installs/downloads; inspecting-interpreter observations do not prove target readiness. Use an unused output directory.

## Research Commands

### `simple-ar research-session` (canonical task-driven entry)

~~~bash
simple-ar research-session --config research.toml
simple-ar research-session --config research.toml --session-root SESSION --model env
~~~

This is the exact structured interface. Explicit CLI overrides research TOML; omitted model on a direct new command is deterministic, while file config defaults env. Model failures remain failures. All new tasks use the common action owner, combining materials, summary, code and experiments within their declared contracts. Reading sources does not itself require a scientific design. Unsupported declarations fail before creation, without an old-controller fallback. Tables can run without a model; authored reports require one. A no-model experiment request can retain materials and pause with requested work incomplete. Continue that same task with the printed `start --resume-task SESSION` and [explicit supply](#native-recovery) for missing contracts or model authorization; collection is not experiment/report completion. Saved historical sessions retain their original owner.

Research design replies use `start --resume-task SESSION --decision-id ID --decision-response accept|reject|revise`; add `--decision-guidance TEXT` for revise. The ID binds actual design/protocol bytes. Confirmation does not run or authorize a process. Resume keeps frozen contracts; use ordinary native feedback for text changes, not creation flags. Intact table results can be reused for report feedback without recomputation. See [interaction policies](#interaction-policies-and-pending-decisions) for new and legacy confirmation scope.

| Option group | Options / defaults |
| --- | --- |
| Task | `--config PATH`, `--topic TEXT`, `--task-kind KIND` (auto), `--outputs OUTPUT ...`, `--output-root PATH` (runs/research-session) |
| Models / API budget | `--model SELECTOR`, `--feasibility-review-model NAME`, `--total-tokens N`, `--llm-requests N`, `--max-output-tokens N` |
| Process budget | `--process-invocations N`, `--process-wall-seconds N` |
| Sources | Repeat `--local-document PATH`, `--material PATH`, `--data-path PATH`, `--query TEXT`, `--provider NAME`; `--cache-dir PATH` |
| Retrieval bounds | `--max-results N` (10), `--max-chunks N` (300), `--idea-limit N` (3), `--max-fulltext-documents N`, `--max-pdf-mb N`, `--web-extract-backend direct\|tavily_basic` |
| Research | `--max-research-iterations N` (1), `--interaction POLICY` (checkpoints) |
| Execution | `--cwd PATH` (.), `--timeout-sec N`, `--label TEXT` (research-session), `--primary-metric NAME`, repeat `--metric NAME` and `--metric-direction NAME=DIRECTION`; `--code-task-config PATH` or final `--command ARGV...` |
| Reporting | `--with-report` / `--no-report`; explicit outputs cannot combine with either. Model-backed tasks normally request report, except data analysis needs explicit request |
| Resume / decisions | `--session-root PATH`, `--recover-interrupted`, `--reanalyze`, `--decision-id ID`, `--decision-response accept\|reject\|revise`, `--decision-guidance TEXT` |
| Continuation | `--authorization-id ID`, `--authorization-reason TEXT`, repeat `--authorize-remaining DIMENSION=AMOUNT`, `--additional-attempts N`, `--additional-no-progress N` |

Data options are shared with the table above. Full-text permissions, page extraction, paired protocols, output files and environment preparation are available through [research TOML](CONFIG_REFERENCE.md#research-toml-sections-and-defaults), not invented CLI flags.

Historical structured sessions retain their bounded plan correction and attempts/plan-*/task_plan_proposals.json records. New tasks use the common action journal instead. CodeTask retains its configured project/workspace/benchmark/scope; reviewed large edits require execute.allow_large_edits=true. The actual execution owner computes omitted process limits, including preparation and repair/retests; explicit limits, including zero, win.

For saved legacy sessions owned by ResearchApplication, resume preserves history and reuses measurements only with matching command/schema/protocol/preparation/protected assets. Explicit goal/outputs/local-paper/execution revisions replan dependent work; task-kind changes need a new session. Incompatible search/ingestion changes fail. Omitted report settings stay saved; report changes rebuild report outputs only.

For those legacy sessions, recover-interrupted confirms the old worker has stopped and closes a running attempt lacking result within the same ledger. Apply input revisions separately. Persisted API failures use ordinary resume. Reanalyze is separate from revisions/authorizations: it reconsiders saved measurements and may lead to authorized future experiments, without requesting a report. Pending follow-ups must continue first. New common tasks use [native recovery](#native-recovery).

### Report controls

| research-session | research-report | Meaning |
| --- | --- | --- |
| `--report-writing-method article\|sections` | Saved method retained | New article; explicit sectional controls select sections; incompatible article options fail |
| `--report-template NAME_OR_PATH` | `--template NAME_OR_PATH` | Structure, session default auto; omitted report-resume option keeps saved choice |
| `--report-reviewer llm\|disabled` | `--reviewer llm\|disabled` | Document review/scope enables article inspection; zero rounds inspect only, positive rounds permit bounded complete candidates |
| `--max-review-iterations N` | Same | Candidate/correction limit; CLI 1 when requested; failed proposals still count |
| `--max-section-tokens N` | Same | 0/unset no per-call cap; positive bounds current writing unit |
| `--report-document-review` / `--no-report-document-review` | `--document-review` / `--no-document-review` | Whole-document review |
| `--report-review-scope section\|document` | `--review-scope section\|document` | Review timing |
| `--report-draft-scope section\|document\|auto` | `--draft-scope section\|document\|auto` | Section/joint/≤2000 planned words joint |
| `--report-outline-strategy auto\|template\|adaptive` | `--outline-strategy auto\|template\|adaptive` | Outline selection |
| `--report-data-tables linked\|full` | `--data-tables linked\|full` | Linked records or tables too |
| `--max-document-review-prompt-chars N` | Same | Optional complete review-request ceiling |

Sections default to section drafting/review, no document review, auto outline, linked tables, no extra review ceiling. Joint drafting requires document review/scope and full source strategy. Historical methods/checkpoints remain. Article defaults to deterministic assembly/audit; explicit document inspection and bounded corrections use the common writing owner. [Report contracts](CONFIG_REFERENCE.md#writing-from-supplied-material).

#### Interaction policies and pending decisions

For new common tasks, `interaction` controls protocol/design confirmation only. `assisted` and `checkpoints` wait for a reply to a generated design; `autonomous` accepts a valid generated design within the saved contract. It does not add stage or delivery milestones or grant process permission. Missing facts, assets or permissions still block the affected work.

Fixed-measurement creation requests protocol confirmation when `--interaction checkpoints` or `assisted` is explicitly supplied. The common task freezes the caller's actual protocol and prints its decision ID. Resume with the printed `start --resume-task ... --decision-id ... --decision-response accept`, then separately approve the pending process with its exact token. Acceptance retains the queued commands; it does not run them or fabricate a research design. Identical caller replies after delivery are idempotent.

Saved legacy sessions continuing under ResearchApplication retain their milestone policy: `assisted` confirms protocol, delivery and supplementary/revision research choices; `checkpoints` confirms the first protocol, candidate revisions and delivery changes while permitting bounded same-protocol supplements; `autonomous` selects valid options within saved limits. The following legacy reply examples and delivery/mode-change rules apply to those sessions:

~~~bash
simple-ar research-session --session-root SESSION --model env --decision-id ID --decision-response accept
simple-ar research-session --session-root SESSION --model env --decision-id ID --decision-response revise --decision-guidance "Revised direction"
~~~

Rich displays decisions but does not wait for stdin. Delivery revise also needs report setting. Answers and proposals persist separately; exact replay restores saved interrupted revisions, changed terms/inputs refuse old decisions. Mode changes must be separate from reanalyze/report-only refresh, except a delivery answer may include report/mode changes.

Legacy continuation allowances need stable ID/reason; prior usage/unknown charges/counters remain. Same ID/terms are idempotent and cannot replenish spent capacity. Attempt/no-progress additions raise caps. Authorization alone does not reopen completed work; authorize before report refresh (normally writer/assembly/audit: three attempts, failures extra). Report-only needs no process expansion. Do not edit ledger/manifest.

### Prepared reproduction

Use task-kind reproduction with local sources and fixed protocol; measurement executes caller-declared command(s) and can optionally author a report with a configured model. Neither implies arbitrary-paper environment discovery or scientific success. Configuration defines independent validation, adapter creation, interpreter and metric selectors. [Exact contracts](CONFIG_REFERENCE.md#execution-and-report).

An explicit single-candidate reproduction with CodeTask, a distinct declared validator and a flat formal command now uses the common task owner, including an optional frozen report. The validator, formal metrics/protocol and actual candidate remain separately bound; internal absolute project paths target the isolated candidate, while external inputs and the selected interpreter retain their identity. Feedback can revise only the report without repeating measurement. A delivered compatible single-command protocol can be reused with `--reuse code_project --kind reproduction`; it does not import the old ledger. Explicit environment setup and adapter files use this same owner; choose auto rather than the strict reproduction shortcut for comparisons. Ordinary open-ended tasks are not reduced to this expert contract.

A new `measurement` request without CodeTask uses the common task owner for a flat command or an explicit baseline-run comparison (`pairs`, or compact seeds with a declared seed flag). Each literal caller condition authorizes one initial execution; cwd, argv and inherited environment are preserved unless an explicit task-venv profile selects its actual interpreter. Protocol seeds without an argv binding describe an internal batch, not extra process runs. The frozen set cannot be silently reduced or re-paired: complete delivery requires the actual conditions and their matching deterministic analysis; missing/failed or incomparable evidence remains disclosed.

Ordinary CodeTask benchmarks also use the common owner when validator and formal argv match; they still require separate physical calls and ledger records, and validation is not measurement. Strict reproduction retains its distinct-command requirement. Required new Python files and one explicit current/venv preparation can accompany declared conditions without changing the task category; exact scope and source checks still apply. Default budgets include preparation, never override explicit limits, and a managed venv preserves Python aliases until its owner selects the actual interpreter. Greenfield `copy`/`sparse_copy` with a real source project can measure source baselines (`source="preparation"`, `before_implementation=true`): prepare, finish all declared baselines, then call `implement(prepared_result)` with actual successful baseline facts. Failed/missing baselines prevent generation. An empty/source-less project cannot invent an unchanged-source baseline; supply real source or independently verified external evidence.

Exact argv's existing external file operands are retained as individual read-only inputs and hash-bound during approval, including `--option=FILE`. This does not mount their parent directories or infer directory access; supply data/material directories explicitly. Restoring a missing domain summary reuses an intact saved observation after artifact/candidate checks, without rerunning the model or validator.

With a model, interpretation and follow-up tools share this task and ledger and require a Chat profile with finite request/token allowances. Without one, declared measurements and deterministic analysis can run through caller actions, while missing model/design/report work remains pending. Resume using the printed start --resume-task command; unknown operations are never replayed.

For measurement plus writing, use `--task-kind measurement --outputs experiments report --model SELECTOR` and the usual report controls. No code modification is required: the writer consumes actual measurements and their matching deterministic analysis. Include supplied references as local documents/materials. Feedback needs a new canonical composition and preserves the original results, manuscript and ledger without rerunning measurements. Missing analysis or a stale manuscript cannot count as completed report delivery.

A measurement may include `execution.environment`: a current-environment check or task venv, declared requirements/project installation and short check. For a frozen multi-condition set, one actual attempt owns preparation; all conditions must share the declared cwd/profile and compatible interpreter. Later conditions reuse its verified receipt, not another installation. The full set plus preparation must fit the finite process allowance before setup; explicit limits, including zero, take precedence. Changed sources/preparation, interrupted or unaccounted processes block reuse. Setup metrics are not formal measurements. Installation may run build code/network and is not an OS sandbox; a CodeTask-owned environment cannot be replaced here.

### `simple-ar research-report`

For saved ResearchApplication sessions, `simple-ar research-report --session-root SESSION --model SELECTOR [--refresh]` adds a missing report to a ready analysis handoff. Report controls above apply. Omitted options retain saved settings. Existing completed reports are idempotent; refresh creates new report/audit attempts from saved evidence without rerunning measurements/analysis. Exhausted attempt/no-progress caps require explicit session authorization. Disabled reviewer does not disable audit. Legacy v1 records remain read-only; invalid sessions do not fall back to another executor. New common tasks request writing through [native feedback or supply](#native-recovery) and continue under the same owner.

### `simple-ar report-export`

~~~bash
simple-ar report-export --report-dir REPORT_ATTEMPT --output NEW_DIR --compile
~~~

Required `--report-dir` and `--output`; optional `--title TEXT` and `--compile`. Input needs report_body.md and bibliography when cited. Pandoc converts existing text without model/experiment calls. Local figures and registered vector variants are copied, SVG-to-TeX needs rsvg-convert; remote images are not fetched. External evidence links become labels with targets recorded in export.json, not automatic dataset copying.

Output is movable single-column acmart source: main.tex/body.tex/source.md/bibliography/figures/export.json and code/block-*.txt for literal code. Preserve the whole directory. Recorded figure captions bind exports; only exact adjacent duplicates are removed, while no-manifest captions remain. Figures use htbp, not forced placement.

Compile needs pdflatex/bibtex/acmart/fvextra/seqsplit/etoolbox and scalable fallback fonts (full cm-super on Debian/Ubuntu). Missing/failed compilation returns nonzero, preserves source and writes diagnostics/build.log when applicable. pdfLaTeX may require engine/font adaptation for non-Latin text. This demonstration does not select conference submission options or certify quality/page count. Choose new output directories to retain prior exports.

## Code Task Commands

CodeTask prepares an isolated workspace and never directly edits the source project. Existing default auto prefers detached worktree and records copy fallback; explicit git_worktree fails instead. Greenfield starts empty and generates there. Isolation is not an OS sandbox.

### High-Level Orchestration

Existing-project execute normally uses one patch plan. Work-plan/batch is opt-in via to-step or saved work plan. Interactive terminals ask at plan/proposal/large-edit gates; noninteractive runs stop unless yes is intentional. Execute's yes auto-approves review gates, unlike native start's yes. No-review-inline stops at gates; interactive confirms primitive steps for debugging.

Planning failures preserve artifacts; complete response corrections are bounded, and offline fallback requires explicit no-llm/allow-planning-fallback. Transport/unknown/deadline failures are not fallback success. Greenfield compact replaces retired stages/review; tool_agent and nonzero planning-review-rounds emit migration notices without extra calls. Retired model-stage routes fail. Generated projects/immutable accepted planning are reused without generation; old plans/failures remain in planning/method_migration.json and retired_method.

Dependency advice is inspection only, never installation. Post-apply and post-run reviews record findings under memory; review is not acceptance. Review contract changes preserve history. Bounded repairs check inputs/bytes, keep snapshots/rollback and code_artifacts.json synchronized; interrupted candidates are not blindly replayed. Offline scaffolds fail before emitting fake metrics.

#### `simple-ar code-task init`

create a code-task run, prepare the editable workspace, and build the first code index.

```bash
uv run simple-ar code-task init --config examples/code_task_medium_review/configs/code_task.toml
uv run simple-ar code-task init --code-root path/to/project --task-file task.md --benchmark-command "python main.py"
uv run simple-ar code-task init --kind greenfield --task-file task.md --benchmark-command "python generated_project/main.py"
```

| Option | Type | Description |
| --- | --- | --- |
| `--config PATH` | path | TOML config for init settings. CLI flags override config values. |
| `--kind MODE` | enum | `existing_project` for patching an existing codebase, or `greenfield` for from-scratch generation. |
| `--code-root DIR` | path | Source project. Required for `existing_project`; optional scaffold/source root for `greenfield`. |
| `--task-file PATH` | path | Markdown/text task description. Required unless set in config. |
| `--output-root DIR` | path | Directory where the code-task run is created. |
| `--name TEXT` | string | Run name suffix. |
| `--benchmark-command TEXT` | string | Command run inside the workspace before and after edits. |
| `--primary-metric NAME` | string | Primary metric for before/after verdicts. |
| `--metric-direction NAME=DIRECTION` | repeatable | Metric direction: `higher`, `lower`, `resource`, or `ignore`. |
| `--env-mode MODE` | enum | `current` or `external`. |
| `--python PATH` | path | Python executable for `--env-mode external`. |
| `--workspace-mode MODE` | enum | `auto`, `copy`, `git_worktree`, `sparse_copy`. Greenfield defaults to empty through TOML; CLI does not accept empty. |
| `--workspace-include GLOB` | repeatable | Include pattern for `sparse_copy`. |
| `--workspace-exclude GLOB` | repeatable | Additional exclude pattern for `sparse_copy`. |
| `--workspace-reuse-source-venv` | flag | Reuse a detected source `.venv` Python as external execution policy. |
| `--max-file-bytes N` | int | Maximum copied file size in copy/sparse modes. Use `0` to disable. |

Outputs: `code_task/manifest.json`; `code_task/task.md`; `code_task/workspace/`; `code_task/meta/codebase_index.json`; `code_task/meta/repo_map.json`; `code_task/meta/repo_map_summary.md`.

#### `simple-ar code-task execute`

advance a code-task run to the next safe stop based on current artifacts.

```bash
uv run simple-ar code-task execute runs/<run-id> --config examples/code_task_medium_review/configs/code_task.toml
uv run simple-ar code-task execute runs/<run-id> --to-step propose-edits
uv run simple-ar code-task execute runs/<run-id> --apply-proposed-edits --timeout 60
```

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--config PATH` | path | Optional TOML for model routing, budget, and runtime settings. |
| `--to-step STEP` | enum | Stop no later than `probe`, `baseline`, `work-plan`, `batch`, `plan`, `propose-edits`, `apply-edits`, `review`, `validate`, `run`, `analyze-failure`, or `repair`. |
| `--dry-run` | flag | Print the next action without writing artifacts. |
| `--model NAME` | string | Model override for LLM-backed steps. |
| `--no-llm` | flag | Use deterministic fallbacks where possible. |
| `--timeout N` | int | Benchmark timeout. |
| `--baseline-policy MODE` | enum | Existing-project baseline handling: `auto`, `run`, `skip`, `provided`, or `none`. Use `skip`/`none` for expensive baselines, or `provided` with a metrics file. |
| `--baseline-metrics-file PATH` | path | JSON or metric-line file used when `--baseline-policy provided`. |
| `--reuse-planning-from PATH` | path | Import immutable accepted greenfield planning after task-contract hash checks; no generated code, memory, findings or repair history. |
| `--review-gate strict\|runtime` | enum | Strict blocks all blocking review findings; runtime blocks safety/direct executability, retaining packaging/report gaps. |
| `--planning-review-rounds N` | int | Deprecated; use 0. Nonzero grants no calls. |
| `--planning-mode MODE` | enum | Greenfield complete architecture work unit; default `compact`. `tool_agent` is a deprecated alias with a visible method migration notice. |
| `--yes` | flag | Auto-approve inline review gates in normal execute mode; with `--interactive`, auto-continue primitive prompts. Use only after you are comfortable approving the reviewed plan/proposal. |
| `--interactive` | flag | Debug mode: confirm each primitive step instead of running continuously to the next review gate. |
| `--no-review-inline` | flag | Disable inline review prompts and stop at review gates instead. |
| `--skip-validation` | flag | Run benchmark even when static validation has not passed. |
| `--strict-validation` | flag | Treat higher-risk validation warnings as errors. |
| `--validation-max-file-bytes N` | int | Max file size scanned by static validation. |
| `--apply-proposed-edits` | flag | Apply reviewed `proposed_edits.json` after plan approval. |
| `--allow-large-edits` | flag | Allow a reviewed proposal that exceeds the normal edit budget. |
| `--allow-planning-fallback` | flag | Allow explicit offline planning or fallback after known, complete response-format failures; not after provider, budget, deadline or partial-stream failures. |
| `--llm-retry-attempts N` | int | Bound completed-response corrections for work/patch planning and greenfield generation, including the first attempt. Transport retries belong to the shared model connection; unreconciled operations prevent correction and fallback. |
| `--repair-rounds N` | int | Number of bounded repair proposals after failure. |
| `--max-files N` | int | Context file budget for LLM steps. |
| `--max-source-chars-per-file N` | int | Per-file source context budget. |
| `--env-mode MODE` | enum | `current` or `external`. |
| `--python PATH` | path | Python executable for external env mode. |

Outputs: `code_task/work_plan.md` and `work_plan.json`; `code_task/attempts/attempt-*/batches/batch-*/batch_state.json`; `code_task/patch_plan.md`; `code_task/meta/proposed_edits.json`; `code_task/meta/applied_edits.json`; `code_task/meta/review_report.json` and `review_report_post_run.json`; `code_task/meta/validation_report.json`; `code_task/meta/resource_probe.json` and `resource_decision.json` after `probe`; `code_task/memory/task_memory.md`, `compressed_memory.md`, and `review_findings.jsonl`; `code_task/run/baseline/`, `code_task/run/patched/`, `code_task/run/comparison.json`; `code_task/summary.md`.

#### `simple-ar code-task decide-plan`

record a human decision for the current patch plan.

```bash
uv run simple-ar code-task decide-plan runs/<run-id> --decision approve
```

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--decision VALUE` | enum | `approve`, `reject`, or `revise`. Required. |
| `--note TEXT` | string | Optional review note. |
| `--reviewer TEXT` | string | Reviewer label. Default: `user`. |

Outputs: updates plan decision state in `manifest.json`.

### Low-Level Primitives

These commands accept RUN_DIR and are normally managed by execute. Planning/proposal commands share model, no-llm and bounded context settings; force regenerates their saved proposal where supported. Benchmark success differs from objective improvement.

#### `simple-ar code-task map`

rebuild repo-map artifacts from the editable workspace.

```bash
uv run simple-ar code-task map runs/<run-id> --show-summary
```

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--no-refresh-index` | flag | Reuse the existing `codebase_index.json`. |
| `--show-summary` | flag | Print `repo_map_summary.md`. |

Outputs: `code_task/meta/codebase_index.json`; `code_task/meta/repo_map.json`; `code_task/meta/repo_map_summary.md`.

#### `simple-ar code-task locate`

rank likely editable files and read-only evidence from the repo map.

```bash
uv run simple-ar code-task locate runs/<run-id> --query "improve classifier"
```

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--query TEXT` | string | Locate query. Defaults to `code_task/task.md`. |
| `--top-k N` | int | Candidate count per group. Default: `8`. |
| `--refresh-map` | flag | Rebuild index and repo map before ranking. |
| `--no-read-only` | flag | Omit protected read-only evidence files. |
| `--show-summary` | flag | Print `locate_results.md`. |

Outputs: `code_task/meta/locate_results.json`; `code_task/meta/locate_results.md`.

#### `simple-ar code-task context`

build a bounded prompt-ready context pack.

```bash
uv run simple-ar code-task context runs/<run-id> --max-files 8 --max-total-chars 20000
```

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--query TEXT` | string | Locate query. Defaults to `task.md`. |
| `--top-k N` | int | Locate candidate budget. |
| `--max-files N` | int | Maximum snippets included. |
| `--max-source-chars-per-file N` | int | Per-file snippet budget. |
| `--max-total-chars N` | int | Total snippet budget. |
| `--refresh-map` | flag | Rebuild map before packing context. |
| `--show-prompt` | flag | Print `prompt_context.md`. |

Outputs: `code_task/context_packs/context-NNN/context_pack.json`; `code_task/context_packs/context-NNN/prompt_context.md`; `code_task/context_packs/context-NNN/selected_snippets.jsonl`.

#### `simple-ar code-task probe`

inspect runtime and project environment signals.

```bash
uv run simple-ar code-task probe runs/<run-id>
```

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--env-mode MODE` | enum | `current` or `external`. |
| `--python PATH` | path | Python executable for external env mode. |

Outputs: `code_task/meta/environment_report.json`; `code_task/meta/resource_probe.json`; `code_task/meta/resource_decision.json`.

#### `simple-ar code-task baseline`

run the recorded benchmark before applying edits.

```bash
uv run simple-ar code-task baseline runs/<run-id> --timeout 60
```

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--command TEXT` | string | Override the benchmark command for this run. |
| `--timeout N` | int | Benchmark timeout. |
| `--skip-validation` | flag | Run even when static validation has not passed. |
| `--env-mode MODE` | enum | `current` or `external`. |
| `--python PATH` | path | Python executable for external env mode. |

Outputs: `code_task/run/baseline/execution_report.json`; `code_task/run/baseline/stdout.txt`; `code_task/run/baseline/stderr.txt`; `code_task/run/baseline/metrics.json`.

#### `simple-ar code-task work-plan`

generate a batch-oriented implementation work plan.

```bash
uv run simple-ar code-task work-plan runs/<run-id>
```

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--model NAME` | string | Model override. |
| `--no-llm` | flag | Use fallback planning. |
| `--force` | flag | Regenerate existing work-plan artifacts. |
| `--allow-planning-fallback` | flag | Allow fallback after known, complete work-plan response-format failures, not transport failures. |
| `--llm-retry-attempts N` | int | Completed-response work-plan correction bound, including the first attempt; no additional transport retry layer. |
| `--max-files N` | int | Planning context file budget. |
| `--max-source-chars-per-file N` | int | Per-file source context budget. |

Outputs: `code_task/work_plan.json`; `code_task/work_plan.md`.

#### `simple-ar code-task batch`

create an attempt/batch state directory for one work-plan item.

```bash
uv run simple-ar code-task batch runs/<run-id> --work-item W1
```

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--work-item ID` | string | Work-plan item id, such as `W1`. Required. |
| `--attempt-id ID` | string | Optional attempt id, such as `attempt-001`. |
| `--force` | flag | Create a new batch even when one already exists. |

Outputs: `code_task/attempts/attempt-NNN/attempt_state.json`; `code_task/attempts/attempt-NNN/batches/batch-NNN/batch_state.json`.

#### `simple-ar code-task plan`

generate a human-reviewable patch plan for the active batch.

```bash
uv run simple-ar code-task plan runs/<run-id>
```

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--model NAME` | string | Model override. |
| `--no-llm` | flag | Use fallback plan. |
| `--force` | flag | Regenerate existing plan. |
| `--allow-planning-fallback` | flag | Allow fallback after known, complete patch-plan response-format failures, not transport failures. |
| `--llm-retry-attempts N` | int | Completed-response patch-plan correction bound, including the first attempt; no additional transport retry layer. |
| `--max-files N` | int | Context file budget. |
| `--max-source-chars-per-file N` | int | Per-file source context budget. |

Outputs: `code_task/patch_plan.md`.

#### `simple-ar code-task propose-edits`

ask the model to produce controlled old/new text edits.

```bash
uv run simple-ar code-task propose-edits runs/<run-id>
```

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--model NAME` | string | Model override. |
| `--no-llm` | flag | Write deterministic empty proposal. |
| `--force` | flag | Regenerate existing proposal. |
| `--max-files N` | int | Editable context file budget. |
| `--max-source-chars-per-file N` | int | Per-file source context budget. |
| `--allow-large-edits` | flag | Accept a large but bounded proposal after review. |

Outputs: `code_task/meta/proposed_edits.json`; `code_task/meta/proposal_warnings.json`; per-batch proposal artifacts when an active batch exists.

#### `simple-ar code-task apply-edits`

safely apply controlled old/new text edits inside the workspace.

```bash
uv run simple-ar code-task apply-edits runs/<run-id>
uv run simple-ar code-task apply-edits runs/<run-id> --edits-file runs/<run-id>/code_task/repairs/repair-001/proposed_edits.json
```

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--edits-file PATH` | path | Specific proposal file to apply. |
| `--allow-unapproved-plan` | flag | Bypass plan approval for local tests/demos. |
| `--allow-large-edits` | flag | Apply a reviewed proposal that requires large-edit approval. |

Outputs: modifies files under `code_task/workspace`; `code_task/meta/applied_edits.json`; `code_task/patch.diff`.

#### `simple-ar code-task validate`

run lightweight static validation over the workspace.

```bash
uv run simple-ar code-task validate runs/<run-id> --strict
```

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--strict` | flag | Treat higher-risk warnings as errors. |
| `--max-file-bytes N` | int | Maximum file size scanned. |

Outputs: `code_task/meta/validation_report.json`.

#### `simple-ar code-task run`

run the benchmark after edits.

```bash
uv run simple-ar code-task run runs/<run-id> --timeout 60
```

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--command TEXT` | string | Override recorded benchmark command. |
| `--timeout N` | int | Benchmark timeout. |
| `--skip-validation` | flag | Run even when static validation has not passed. |
| `--env-mode MODE` | enum | `current` or `external`. |
| `--python PATH` | path | Python executable for external env mode. |

Outputs: `code_task/run/patched/execution_report.json`; `code_task/run/patched/stdout.txt`; `code_task/run/patched/stderr.txt`; `code_task/run/patched/metrics.json`; `code_task/run/comparison.json` when baseline metrics also exist.

#### `simple-ar code-task analyze-failure`

summarize the latest failed validation or benchmark result.

```bash
uv run simple-ar code-task analyze-failure runs/<run-id>
```

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |

Outputs: `code_task/run/patched/failure_analysis.md` or `code_task/meta/failure_analysis.md`.

#### `simple-ar code-task repair`

propose bounded repair edits from the latest failure context.

```bash
uv run simple-ar code-task repair runs/<run-id>
```

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--model NAME` | string | Model override. |
| `--no-llm` | flag | Write deterministic empty repair proposal. |
| `--max-files N` | int | Repair context file budget. |
| `--max-source-chars-per-file N` | int | Per-file source context budget. |

Outputs: `code_task/repairs/repair-NNN/proposed_edits.json`; updates `code_task/summary.md`.


Apply-edits checks paths, scope, unique old text and budgets before writing. Empty old creates an absent allowed file, never replaces existing; new files require explicit scope/batch target and share snapshots/rollback. Repair proposals are not applied automatically; review then apply with edits-file. Static validation is conservative, not runtime proof. Probe installs nothing and runs no benchmark. See [configuration scope](CONFIG_REFERENCE.md#edit-scope-behavior).

## Artifact Tools

### `simple-ar inspect`

`simple-ar inspect RUN_DIR` writes artifact_index.json with metadata/bounded previews. It does not follow symlinks or special files, and defaults sha256 to null; Python callers can explicitly enable hash_files. This is not full-text reading.

### `simple-ar search-artifacts`

`simple-ar search-artifacts RUN_DIR QUERY [--top-k N] [--include-operational]` builds its own current index; top-k defaults 8. Operational files are excluded unless requested. It prints chunks/paths and writes artifact_index.json, artifact_chunks.jsonl, artifact_search_results.json. Binary files are not chunked; >8 MiB files are reported file_too_large rather than searched as a complete prefix.

### `simple-ar clean`

~~~bash
simple-ar clean RUN_DIR
simple-ar clean RUN_DIR --all-caches
simple-ar clean --shared-index
simple-ar clean --shared-cache
~~~

| Option | Effect |
| --- | --- |
| `RUN_DIR` | Optional only for shared cleanup |
| `--yes` | Delete previewed targets without interactive yes |
| `--all-caches` | Broader run-local rebuildable caches/index/context |
| `--shared-index` | Clear historical shared SQLite/LanceDB stores across runs |
| `--shared-cache` | Also clear literature provider cache and agent_handoff_archives |
| `--index-root PATH` | Environment index root or .simple_ar_cache/research_index |
| `--literature-cache-root PATH` | Default .simple_ar_cache/literature |
| `--allow-external-index-root` | Permit shared cleanup outside workspace |

Cleanup previews targets and retains the run itself/reports/manifests/evidence. Current v2 removes recognized cache/literature query metadata and artifact_search_results; all-caches additionally removes artifact indexes/chunks. Attempts/outputs/documents/full text/unrecognized files remain; custom external caches are not followed. Query metadata >1 MiB remains uninspected, never deleted by filename alone. Workspace-local shared SQLite rows may be removed through index_meta.

Historical layouts remove recognized fulltext/extracted caches; all-caches can remove portable research_index/chunks.jsonl plus repo-map/locate/context caches, while retaining final reports/metadata/benchmark/audit artifacts. Shared cleanup leaves run-local artifacts. Historical persistent indexes no longer serve source queries; clearing provider caches may require new queries. Shared-cache also deletes retained external-agent transcripts.

### `simple-ar status`

`simple-ar status RUN_DIR` and `simple-ar results SESSION` read saved state without model calls. Status handles native/research/CodeTask and historical layouts. Canonical v2 includes state/current attempt/budget/counters/last decision; v1 is read-only. Handoff/continuation hints do not mean a background worker is running. Results lists current delivery files/limitations/resume command; a paused feedback revision does not present old delivery as current completion.

## Tools And MCP

### `simple-ar tools schema`

`simple-ar tools schema [--format mcp|openai] [--output PATH]` exports registered schemas; default mcp, stdout if output omitted.

### `simple-ar tools call`

`simple-ar tools call RUN_DIR TOOL_NAME [--args-json JSON | --args-file PATH] [--debug-payloads]` calls a registered read-only run tool. Arguments are a JSON object; args-file avoids shell quoting problems. Prints JSON and writes RUN_DIR/tools/tool_trace.jsonl; debug retains larger payloads.

### `simple-ar tools serve-mcp`

`simple-ar tools serve-mcp RUN_DIR [--debug-payloads]` serves read-only registered experiment tools over stdio. Methods: initialize, ping, tools/list, tools/call. It enables no write/shell/network/install capability.

## Advanced native contracts

### Files, project copies and delivery

File tools accept authorized source0/... and workspace/... locators or observed absolute paths. Mount names are not real directories and embedded code/argv strings are not rewritten. Private-name filtering is case-insensitive throughout listing/search/inspection; absolute paths never widen roots or source permissions.

| Tool / field | Contract |
| --- | --- |
| list_directory: recursive, file_glob | Paginated filenames without bodies; traversal ≤4096 entries, inventory_truncated means incomplete inventory |
| read_file: byte offset, start_line, max_lines, max_bytes | One-based lines for files ≤256000 bytes; larger files use bytes. Both limits apply; continue at next_offset_bytes, not decoded-text length |
| read_file: full_digest=true | Stream full digest for regular file ≤64 MiB, return only requested window |
| write_file / edit_file | Expected digest, backup and atomic write; replacements must match uniquely and not overlap. Legacy old_text/new_text single replacement remains |
| Version conflict | Authorized writable target returns current/expected digests without body/write; new explicit matching expectation required. Private/read-only targets reveal no conflict digest |
| copy_file | Binary/text bytes ≤64 MiB, source digest required; existing destination also needs observed digest |
| search_files | Literal rg search within authorized roots; paginated scanned-file coverage. Consumes process receipt/allowance even though read-only |
| inspect_project: data_paths | Read-only dependency/entry/test/document/coverage observation and full attempt; no project import or execution |
| prepare_implementation: goal, project_path, files | Optional project selection request binds originals before generation. Unchanged selected files may return empty replacements. One complete-contract correction permitted; transport/unknown/Python-syntax failures are not replayed |
| update_work_plan | Current-request plan persists through compaction/resume; no separate planner/scheduler. Step claims neither verify work nor authorize execution |

Copy-project takes source directory and nonexistent workspace path. It copies an editable subset without execution/network, skipping caches/builds/symlinks/private paths, .git/.aws/.ssh/.codex/.agents/venvs/all .env* (including .env.example). Oversized files are skipped; empty source/existing destination/count or total overflow refuse publication. Inspect omissions before treating the package as complete.

Copy limits: max_files=1000 (1–4096), max_total_bytes=134217728 (1–536870912), max_file_bytes=2000000 (1–67108864). Returned absolute_path/source/copy/files include relative paths and SHA256. Optional simple .zip archive_name packages exactly copied files inside the copy; conflicting names fail. Request/output hashes persist in attempts/IDENTITY/project-copy.json before publication. Same-ID recovery checks current file/ZIP hashes and does not recopy/repack changed contents.

Write-file and run-process can explicitly declare delivery with summary/artifacts/unresolved. Final write must include its written file; other artifacts already exist in writable workspace. Process delivery requires successful unstopped exit and artifacts afterward; declaration is part of exact approval. Request/output hash binding recovers without rerunning; changed/missing/unbound outputs, failed commands or invalid terminal ordering block delivery.

Directory artifacts expand to explicit snapshots of ≤200 accessible files per directory, excluding private paths/venvs/bytecode/symlinks. Empty/oversized selections refuse rather than truncate. A manifest proves file presence, not correctness, portability or automatic archival.

### Sources, writing and publication

Allow-network independently authorizes arXiv/OpenAlex/Semantic Scholar/optional Tavily search and original acquisition. Evidence stays read-only; each acquisition currently bounds one document, three fetch attempts, 8 MiB, 60 PDF pages. Source_operations counts provider/adapter operations, not HTTP calls/credits. Question lookup is local and retains offsets/truncation; omit find_source_passages query to inspect identity/coverage first.

Original retained document_bundle.v1 records can be supplied read-only without network. Resolved ownership rejects workspace-generated documents and attempt history as original evidence. Bibliographic gaps may be filled from retained front matter in the same response with exact quotation checks; supplied metadata is retained, and this is not independent publication verification.

Compose-document takes selected source chunks/material windows/known bibliography/figures/prior draft and saves manuscript.md with publication citation keys/references, preserving old drafts. New article work uses one logical complete-body request with shared transport/accounting and no additional format correction/redrafting loop; directory selection consumes separate shared capacity. It does no independent search/experiment/review.

Optional `required_headings` specifies ordered article-level section titles, ignoring case and presentation numbering; `max_cited_sources` sets a distinct body-source ceiling (0 means no cap). These are checked requirements, not advisory prose. Violations retain the original response and stop publication without another writing request. Reference lists and fenced examples do not count toward the ceiling.

Material-roots performs bounded private-filtered version-bound selection and reports read coverage/omissions. It can combine with sources/figures/prior draft, but not explicit materials windows; locators validate before paid selection. Original bundles retain identities, while drafts/derived results remain materials. Selection is not exhaustive reading or visual/statistical verification.

A complete input-bound writing response can resume assembly/missing publication files without calls; failed/partial/unknown writing is not resent. Recovery verifies original byte windows/manuscript/references/figures; changed partial files remain and fail. Missing/edited outputs or legacy records lacking byte bindings need inspection/new feedback version. Historical checkpoint/assembly forms remain; authored heading hierarchy is retained.

`review_document` independently inspects an existing `prior_draft` using the same selected-source/material contract, without invoking the Writer or changing prose. Its `review.json` contains `review_status` (`reviewed` or `needs_revision`) and validated findings/quotes. Without independent evidence it can inspect prose, not certify facts. Complete responses recover without another call; rejected/unknown responses stop. A feedback rewrite uses a new composition identity, retaining the old draft and review. Rendered-image quality requires separate visual inspection.

Export-report converts workspace Markdown/assets to ACM/optional PDF through shared process receipts and exact approval. Reuse requires bound output digests; changed/missing files cannot inherit compilation success, and unbound interrupted exports are not replayed. Workspace captions own revised prose; figure metadata only fills missing captions/provides variants. Compilation is neither scientific review nor page-count acceptance.

### Images, execution and history

Inspect-images needs allow-vision and a compatible bound vision connection; missing route fails before sending. It shares model requests/tokens, preserves originals/feedback and reuses saved responses. Unknown interruptions/rejected responses are not resent or accepted as findings. Generation uses max-images and image route; create-image makes versions, resume-image queries/downloads known IDs or reuses completions, never resets persistent image allowance.

Project-python preserves an existing virtualenv entry and is retained on resume. Its directory leads filtered PATH; scripts should use sys.executable for children. Approval binds resolved argv/environment; changes need approval again. It installs nothing. Existing receipts recover without execution replay. New tasks allow ten process invocations per model-turn allowance; saved older limits and total process wall budget remain.

Context_input_tokens (default 24000) is a soft input target including schemas/history. Remaining token capacity subtracts effective output reservation. The current complete operation is never split; earlier complete assistant/tool blocks form a contiguous retained suffix marked retained_turn_start. If the current unit exceeds target, optional earlier blocks/indexes/windows are omitted.

History_archive indexes omitted accepted turns/failure counts, not missing evidence or resolved failures. Recent observations are bounded to 2000 estimated tokens, with version-checked source windows. Read_history pages records locally; pin returned end_turn and use next_offset_bytes. Attempt IDs are not turn indices. Latest actual process result/latest nonzero diagnosis remain available unless already read back; cancelled/blocked proposals do not replace execution facts. Historical identities do not certify current file bytes. Raw records remain; estimates are not exact tokenizer guarantees or budget bypass.

## Historical and development commands

### `simple-ar research-brief` (compatibility alias)

`research-brief` is a summary-only compatibility alias, retaining its default `runs/research-brief` output directory and optional model. It creates a common task, retaining original documents, reading/synthesis and summary artifacts without a second controller or a mandatory planning call. Missing coverage stays qualified. Model omission is deterministic, not model-failure fallback. Resume with the printed `start --resume-task SESSION`; `research-session --session-root SESSION` also selects the saved owner. Historical attempts and ledgers remain intact.

### `simple-ar research-session-continue`

`simple-ar research-session-continue --session-root SESSION [--parent-attempt-id ID] [--model SELECTOR] [--cwd PATH] [--timeout-sec N] [--label TEXT] [--primary-metric NAME] [--metric NAME ...] [--metric-direction NAME=DIRECTION ...] --command ARGV...`

This compatibility command uses the saved ResearchApplication owner (canonical v2), permitting one corrected simple explicit experiment after a failed/timed_out technical candidate; scientific negative results are evidence. Cwd defaults ., timeout 300, label research-session-recovery. Latest candidate/schema retained unless explicitly overridden. Parent attempt/literature remain; deterministic analysis reruns. Paired/prepared-data/CodeTask failures use their own bounded paths. V1 execution is retired; new common tasks use native recovery.

### `simple-ar research-session-migrate`

`simple-ar research-session-migrate --source-root OLD --destination-root NEW [--artifact NAME ...] [--requested-output NAME ...] [--max-artifact-bytes N]` creates a common-tool successor without changing v1. Default import cap 2 MiB; only explicitly named small files copied. Destination must be empty/outside old root. Parent/imported/skipped counts and unknown_not_imported historical budget are recorded, never guessed.

### Retired `simple-ar research-experiment`

Use research-session; old synthesis-file is not translated. Historical artifacts remain readable, library capabilities composable.

### Retired segmented CodeTask command

research-code-task and its independent executor are retired. Use research-session with code-task-config or standalone code-task. Historical results remain readable.
