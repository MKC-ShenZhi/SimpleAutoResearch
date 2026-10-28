# CLI Reference

[中文版本](CLI_REFERENCE_zh.md) · [Usage](USAGE.md) · [Configuration](CONFIG_REFERENCE.md)

Look up syntax, parameters and recovery limits here. Prefix examples with `uv run` from a checkout. SESSION and RUN_DIR mean full saved directory paths; replace uppercase placeholders. Quote paths containing spaces. All commands accept `-h / --help`. Put research/start `--command ARGV...` last; subsequent tokens belong to the child argv without shell interpretation.

## Everyday commands

| Need | Command |
| --- | --- |
| Start a goal-driven task, continue saved work or give feedback | [`simple-ar start`](#simple-ar-start) |
| List current reusable delivery files, limitations and continuation commands without executing work. | [`simple-ar results`](#simple-ar-results) |
| Read saved progress, stop conditions and resource use without model calls | [`simple-ar status`](#simple-ar-status) |
| Inspect local connections, routes and credential presence without API calls or provider tests. | [`simple-ar models`](#simple-ar-models) |

## Standalone tools

| Need | Command |
| --- | --- |
| Generate or edit one concept image with a named Images profile; keep each version separately. | [`simple-ar image`](#simple-ar-image) |
| Review rendered figures with a vision model and save feedback without editing originals | [`simple-ar image-review`](#simple-ar-image-review) |
| Read project instructions, dependencies, entry candidates and named data locations without installation or execution. | [`simple-ar project-info`](#simple-ar-project-info) |
| Export an assembled report to editable LaTeX, optionally compiling PDF; no model or experiment calls. | [`simple-ar report-export`](#simple-ar-report-export) |
| Write an artifact index with metadata and bounded previews, not full-text reading. | [`simple-ar inspect`](#simple-ar-inspect) |
| Search artifacts lexically with a fresh index; operational metadata is excluded unless requested. | [`simple-ar search-artifacts`](#simple-ar-search-artifacts) |
| Preview and remove recognized rebuildable caches, retaining the run, evidence and deliveries | [`simple-ar clean`](#simple-ar-clean) |

## Advanced and compatibility interfaces

| Need | Command |
| --- | --- |
| Use exact structured inputs or continue saved research work | [`simple-ar research-session`](#simple-ar-research-session) |
| Generate or refresh a report for a saved legacy session with ready analysis | [`simple-ar research-report`](#simple-ar-research-report) |
| Compatibility spelling for a new summary-only session; inherits research-session parameters with the limits below. | [`simple-ar research-brief`](#simple-ar-research-brief) |
| Run one corrected explicit experiment after a failed/timed-out simple experiment in a saved canonical session. | [`simple-ar research-session-continue`](#simple-ar-research-session-continue) |
| Create a successor from a read-only v1 session, importing only named small artifacts without changing the source. | [`simple-ar research-session-migrate`](#simple-ar-research-session-migrate) |
| Prepare an isolated workspace for an explicit existing-project or greenfield contract. | [`simple-ar code-task init`](#simple-ar-code-task-init) |
| Continue bounded planning, review, edits and benchmarks for a saved CodeTask. | [`simple-ar code-task execute`](#simple-ar-code-task-execute) |
| Record a human decision on the current patch plan. | [`simple-ar code-task decide-plan`](#simple-ar-code-task-decide-plan) |
| Inspect runtime/environment without installing dependencies or running benchmarks. | [`simple-ar code-task probe`](#simple-ar-code-task-probe) |
| Rebuild the workspace repository map and index. | [`simple-ar code-task map`](#simple-ar-code-task-map) |
| Rank likely editable files and read-only evidence. | [`simple-ar code-task locate`](#simple-ar-code-task-locate) |
| Build a bounded prompt context pack from located files. | [`simple-ar code-task context`](#simple-ar-code-task-context) |
| Generate a batch-oriented implementation work plan. | [`simple-ar code-task work-plan`](#simple-ar-code-task-work-plan) |
| Create an attempt/batch for one work-plan item. | [`simple-ar code-task batch`](#simple-ar-code-task-batch) |
| Generate a patch plan for human review. | [`simple-ar code-task plan`](#simple-ar-code-task-plan) |
| Propose controlled old/new text edits without applying them. | [`simple-ar code-task propose-edits`](#simple-ar-code-task-propose-edits) |
| Apply a reviewed edit proposal inside the allowed workspace scope. | [`simple-ar code-task apply-edits`](#simple-ar-code-task-apply-edits) |
| Run lightweight static validation; passing is not runtime proof. | [`simple-ar code-task validate`](#simple-ar-code-task-validate) |
| Run the saved benchmark before edits. | [`simple-ar code-task baseline`](#simple-ar-code-task-baseline) |
| Run the saved benchmark after edits and compare available baseline metrics. | [`simple-ar code-task run`](#simple-ar-code-task-run) |
| Summarize the latest validation/benchmark failure deterministically. | [`simple-ar code-task analyze-failure`](#simple-ar-code-task-analyze-failure) |
| Propose bounded repair edits; review and apply separately. | [`simple-ar code-task repair`](#simple-ar-code-task-repair) |
| Export registered read-only tool schemas to stdout or a file. | [`simple-ar tools schema`](#simple-ar-tools-schema) |
| Call one registered read-only tool with a JSON object and save its trace. | [`simple-ar tools call`](#simple-ar-tools-call) |
| Serve registered read-only run tools over MCP stdio; no shell, write, network or installation capability is granted. | [`simple-ar tools serve-mcp`](#simple-ar-tools-serve-mcp) |

---

<a id="guided-entry"></a>
<a id="native-task-entry-experimental"></a>
<a id="native-recovery"></a>
<a id="resume-and-feedback"></a>
<a id="add-remaining-allowance"></a>
<a id="switch-a-configured-connection"></a>
<a id="retry-a-failed-model-decision"></a>
<a id="supply-missing-or-revised-contracts"></a>
<a id="structured-compatibility-selection"></a>
<a id="existing-data-no-api"></a>
<a id="goal-driven-code-analysis-and-figures"></a>

### `simple-ar start`

Start a goal-driven task, continue saved work or give feedback. Bare start asks for a goal; ordinary --kind selects an outcome preference.

```text
simple-ar start [REQUEST] [OPTIONS]
simple-ar start --resume-task SESSION [FEEDBACK] [OPTIONS]
```

#### Goal, materials and creation

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `REQUEST` | New task goal, or feedback on resume; do not also pass --goal. | Unset |
| `--task-config PATH` | Load editable [task] TOML to create a new native task; CLI overrides it. | Unset |
| `--write-task-config PATH` | Export annotated native creation TOML and exit; does not revise existing tasks. | Unset |
| `--source PATH` | Read-only local file/directory mount; repeatable. Resume confirms added materials. | [] |
| `--guided` | Interactively select an outcome preference for a native task. | Off |
| `--kind CHOICE` | Outcome preference; exact protocol options can select structured compatibility. Does not grant access. Choices: `survey`, `bug_fix`, `reproduction`, `writing`, `data_analysis`, `figure`, `code`. | Unset |
| `--from-session PATH` | Reuse deliveries from this session; pair with --reuse. | Unset |
| `--reuse NAME` | Registered result name shown by results; repeatable. code_project retains its edit scope/checker. | [] |
| `--scripted` | With --kind data_analysis, use native code/analysis; commands still need individual approval. | Off |
| `--goal TEXT` | Goal text, instead of the positional request. | Unset |
| `--document PATH` | Local reference paper; repeatable. Ordinary native input rejects URLs; reproduction --chat can acquire public HTTPS papers after confirmation. | [] |
| `--material PATH` | Notes, drafts or an analysis package; repeatable. Ordinary materials are unverified, not bibliographic papers. | [] |
| `--project PATH` | Existing project; ordinary goals mount it read-only, explicit repair/reproduction uses an isolated candidate. | Unset |
| `--project-python PATH` | Existing execution interpreter; reproduction venv uses it as base Python. | Unset |
| `--model SELECTOR` | Model selector: env, profile:NAME or route:NAME. | env |
| `--output-root PATH` | Parent directory for a new task/run. | runs/assistant |
| `--prepare-only` | Save/preview without model/process calls; new native tasks still check credentials. | Off |
| `--yes` | Accept displayed creation/access/resource summary; never approves a pending native process. | Off |

#### Access and resource allowances

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--max-processes N` | Cumulative native process count, including search backends; zero disables processes. | 10 × max-turns unless a contract sets it |
| `--max-process-seconds N` | Cumulative process wall time in seconds; not a per-command timeout. | 300 × max-turns unless a contract sets it |
| `--allow-execution` | Allow process proposals; each command still needs exact approval. Execution is not an OS sandbox. | Off |
| `--allow-network` | Authorize public-source search/acquisition; not package installation or arbitrary commands. | Off |
| `--allow-vision` | Send mounted rendered PNG/JPEG/WebP to the vision route; shares the model budget. | Off |
| `--max-images N` | Image generation/edit submissions through the image route; separate from visual inspection. | 0 |
| `--max-turns N` | Cumulative native model-request allowance, including feedback. | 30 |
| `--max-tokens N` | Cumulative native model-token allowance, including feedback. | 600000 |

#### Explicit structured compatibility

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--chat` | Model-assisted setup for explicit structured protocols; requires a terminal and model. | Off |
| `--resume-setup PATH` | Continue saved structured setup/accounting; not task execution recovery. | Unset |
| `--with-report` | Explicitly request a model-authored report; data analysis otherwise delivers analysis only. | Off |
| `--asset-max-mb N` | Chat per-project/data acquisition capacity; ZIP expansion limited to four times this. Not download permission. | 20 in chat setup |
| `--template NAME_OR_PATH` | Report template name or Markdown path. | Unset |
| `--sources CHOICE` | Structured survey policy: supplied documents only or online sources. Choices: `materials`, `search`. | Unset |
| `--fulltext` | Structured full-text/PDF permission; reproduction --chat URLs still require confirmation. | Off |
| `--web-extract-backend CHOICE` | Public-page extraction backend; separate from PDF acquisition. Choices: `direct`, `tavily_basic`. | Unset |
| `--max-cited-sources N` | Maximum distinct cited sources in the report. | Unset |
| `--validate VALUE` | Explicit validator; strict reproduction keeps validation distinct from formal measurement. | Unset |
| `--allow VALUE` | Editable project-relative path/glob; repeatable. | [] |
| `--hypothesis TEXT` | Published conclusion to check in prepared reproduction. | Unset |
| `--dataset TEXT` | Data description and accepted adaptation. | Unset |
| `--expected-outcome TEXT` | Declared comparison criterion, not an invented result. | Unset |
| `--metric NAME` | Required emitted metric name; repeatable. | [] |
| `--output-files JSON` | JSON name-to-relative-file map under SIMPLE_AR_OUTPUT_DIR; no output-path inference. | {} |
| `--metric-sources JSON` | Explicit JSON/CSV selectors for registered output metrics. | {} |
| `--cwd PATH` | Declared command working directory. | Current directory for prepared reproduction |
| `--timeout-sec N` | Per-process time limit in seconds. | 300 for prepared reproduction |
| `--environment CHOICE` | Use current environment or explicitly create a task-local reproduction venv. Choices: `current`, `venv`. | current |
| `--requirements PATH` | Project-relative requirements file; repeatable. Venv defaults to requirements.txt if present, otherwise a bare venv. | [] |
| `--install-project` | Install reproduction project in its task venv; needs packaging metadata and can execute build code/network. | Off |
| `--check-argv JSON` | Explicit short pre-measurement environment check as JSON argv; not scientific evidence. | Unset |
| `--interaction CHOICE` | Design/protocol confirmation policy; new native tasks have no stage/delivery milestone dialogs. Never grants process permission. Choices: `assisted`, `checkpoints`, `autonomous`. | checkpoints |
| `--command ARGV...` | Explicit argv; put last because all following tokens belong to the command. Research/start argv has no shell interpretation. | Unset |

#### Continue, approve and recover

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--resume-task PATH` | Resume a saved native task; with --tasks-in, also accepts an exact displayed name. | Unset |
| `--tasks-in PATH` | List native tasks here and in immediate children, at most 256 entries; no recursive search. | Unset |
| `--select-task` | Interactively select from --tasks-in; blank input cancels. | Off |
| `--supply-task PATH` | On resume, record TOML contracts or first model binding without execution; replace_existing=true revises contracts, retaining evidence/accounting. | Requires --resume-task; separate operation |
| `--retry-model` | On resume, retry a terminated failed model decision with remaining budget; optional --model changes to a compatible Chat connection. | Off |
| `--switch-model profile:NAME` | On resume, record profile:NAME or route:NAME text connection change; requires switch ID/reason. Records only, no execution. | Unset |
| `--switch-id NAME` | Stable idempotency key for --switch-model. | Unset |
| `--switch-reason TEXT` | Human-readable reason for --switch-model. | Unset |
| `--authorize-remaining DIMENSION=AMOUNT` | New remaining allowance as DIMENSION=AMOUNT; repeatable. Retains prior usage/unknown reservations. | Unset |
| `--authorization-id NAME` | Stable idempotency key; identical ID/terms do not replenish spent capacity. | Unset |
| `--authorization-reason TEXT` | Human-readable reason for remaining-allowance changes. | Unset |
| `--approve-execution VALUE` | Exact printed token for the pending command; requires --resume-task. | Requires --resume-task and exact token |
| `--decision-id NAME` | Exact pending design/protocol decision ID; native replies require --resume-task. | Unset |
| `--decision-response CHOICE` | Reply to the pending decision; accepting a design does not approve a process. Choices: `accept`, `reject`, `revise`. | Unset |
| `--decision-guidance TEXT` | Facts or revised direction with --decision-response revise. | Unset |

#### Explicit table analysis

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--data-file PATH` | UTF-8 CSV/TSV or JSON records. Explicit table selectors choose the no-API shortcut; --kind data_analysis alone does not. | Unset |
| `--data-attribution TEXT` | User-declared source/credit/version; retained without independent verification. | Empty |
| `--value-column NAME` | Explicit numeric column; repeat for multiple metrics or matrix/coordinate columns. | [] |
| `--group-column NAME` | Category/group column; values bar/heatmap needs unique row labels. | Empty |
| `--observation-unit TEXT` | What one row represents; required for the table shortcut. | Empty |
| `--value-unit TEXT` | Selected value columns' unit; omission is unknown. | Empty |
| `--paired-baseline NAME` | Explicit same-row baseline among selected observation columns sharing a quantity/unit. | Empty |
| `--data-association CHOICE` | Opt-in descriptive Pearson r for line/scatter pairs; no significance/causal inference. Choices: `none`, `pearson`. | none |
| `--data-mode CHOICE` | observations aggregates rows; values retains supplied numbers without inferred aggregation. Choices: `observations`, `values`. | observations |
| `--data-missing CHOICE` | Reject missing values or explicitly omit them; missing y breaks lines. Choices: `reject`, `omit`. | reject |
| `--figure-width CHOICE` | Generic column (3.5 inch) or wide (7 inch) figure width. Choices: `column`, `wide`. | wide |
| `--data-max-mb N` | Physical input size limit in MiB. | 20 |
| `--data-max-figures N` | Physical SVG page limit; excess fails without dropping categories. | 100 |
| `--data-plot CHOICE` | bar: means/values; box: observation quartiles/min-max; line/scatter: explicit coordinates; heatmap: supplied matrix. Choices: `bar`, `box`, `line`, `scatter`, `heatmap`. | bar |
| `--x-column NAME` | Numeric x for values-mode line/scatter; distinct from value/group columns. | Empty |
| `--x-unit TEXT` | Declared x-coordinate unit; omission is unknown. | Empty |
| `--data-max-points N` | Physical coordinate points per figure; excess fails without sampling. | 10000 |
| `--series-layout CHOICE` | Separate axes, or shared axes for line/scatter columns with a common declared unit. Choices: `separate`, `shared`. | separate |
| `--data-path PATH` | Existing data file/directory; repeatable. Does not rewrite commands or automatically copy external data. | [] |

Ordinary goals, --guided and ordinary --kind preferences use native tasks. Explicit protocol controls, --chat and explicit table selectors choose supported structured paths. --kind data_analysis alone remains model-driven; --value-column/--x-column/--paired-baseline explicitly chooses the deterministic table shortcut unless --scripted is used. The shortcut requires --data-file, --value-column and --observation-unit; it makes no task API calls unless a report is requested.

New tasks read .env from the current working directory. Source mounts reject symlinks and private/runtime paths; native data-file preferences retain a 20 MiB bound. Put public URLs in the goal with network authorization rather than passing ordinary document URLs.

Resume retains model, interpreter and cumulative budgets. Use --authorize-remaining, not creation limits, to add resources. Supported dimensions include llm_requests, total_tokens, process_invocations, process_wall_seconds and image_requests. Stable ID/reason are required; prior charges and unknown reservations remain. This grants capacity, not access or command approval. Confirm added access/resources separately from --approve-execution.

Keep --retry-model separate from feedback, access/resource grants and command approval. It retries only a terminated failed decision without an accepted reply and with sufficient saved finite capacity; it does not replay tools. Cached replies use ordinary resume. In-flight requests, unbounded charges and unknown process side effects require reconciliation. --switch-model is record-only and separate from feedback, retry, supply, --model and grants.

For new native tasks, assisted/checkpoints wait for design/protocol replies; autonomous can accept valid designs within saved limits. There are no extra stage/delivery milestone dialogs. Design acceptance does not approve a process. Exact pending command tokens are required even with --yes.

Examples:

```bash
simple-ar start "Explain these results" --source ./results --model profile:daily
simple-ar start --write-task-config task.toml
simple-ar start --task-config task.toml --yes
simple-ar start --resume-task SESSION "Shorten the explanation"
simple-ar start --resume-task SESSION --approve-execution TOKEN
simple-ar start --resume-task SESSION --authorize-remaining llm_requests=10 --authorize-remaining total_tokens=100000 --authorization-id continuation-1 --authorization-reason "Finish the saved goal" --yes
simple-ar start --kind data_analysis --data-file measurements.csv --value-column score --observation-unit "one independent run" --yes
```

#### Supply missing task settings (advanced)

Use this only when an existing task is missing settings. It records them without running the task. Example for a first model binding:

```toml
supply_id = "first-model"
reason = "Continue retained work"

[changes]
model = "profile:daily"

[resource_allowances]
llm_requests = 20
total_tokens = 600000
```

Supply requires supply_id, reason and [changes]; source aliases resolve from the supply file. By default it fills missing contracts. Explicit replace_existing = true revises saved contracts; replacing sources requires the complete alias map. Evidence and charges remain. New protocols may need fresh acceptance; processes need separate approval. Keep supply separate from feedback, retry, CLI grants and command approval.

```bash
simple-ar start --resume-task SESSION --supply-task supply.toml --prepare-only
simple-ar start --resume-task SESSION --supply-task supply.toml --yes
simple-ar start --resume-task SESSION
```


<a id="reuse-deliveries"></a>


### `simple-ar results`

List current reusable delivery files, limitations and continuation commands without executing work.

```text
simple-ar results SESSION [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `SESSION` | Session directory whose current deliveries are listed. | Required |

Examples:

```bash
simple-ar results SESSION
simple-ar start --from-session SESSION --reuse code_project --goal "Revise the figure legend"
```


### `simple-ar status`

Read saved progress, stop conditions and resource use without model calls. A resume hint does not mean a worker is running.

```text
simple-ar status RUN_DIR [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |

Examples:

```bash
simple-ar status SESSION
```


### `simple-ar models`

Inspect local connections, routes and credential presence without API calls or provider tests.

```text
simple-ar models [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--config PATH` | This command's TOML configuration; explicit CLI options take precedence. | SIMPLE_AR_MODELS_CONFIG, then user models.toml |
| `--profile NAME` | Inspect one named connection without exposing credentials. | Unset |
| `--purpose CHOICE` | Capability for selecting --profile; listing profiles does not probe providers. Choices: `text`, `code`, `writing`, `vision`, `image`. | text |

Examples:

```bash
simple-ar models
simple-ar models --config models.toml --profile daily --purpose text
```

<a id="concept-images"></a>

### `simple-ar image`

Generate or edit one concept image with a named Images profile; keep each version separately.

```text
simple-ar image --prompt TEXT --output PATH [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--prompt TEXT` | Image description or edit instructions. | Required |
| `--input PATH` | Source image to edit or rendered image to review; originals retained. | Unset |
| `--output PATH` | This command's output directory/file. | Required |
| `--config PATH` | This command's TOML configuration; explicit CLI options take precedence. | Unset |
| `--model SELECTOR` | Model selector: env, profile:NAME or route:NAME. | Configured image route |

Use a new output directory for each changed request. Identical saved completed requests recover without another submission; unknown requests are not automatically replayed. Each output has a one-image-request ledger beside it. Requires a named Images connection/image route. Raster output is not measured data or editable vector layers.

Examples:

```bash
simple-ar image --prompt "Concept sketch" --output figures/v1 --model profile:illustration
simple-ar image --prompt "Larger labels" --input SOURCE_IMAGE --output figures/v2
```

<a id="inspect-a-rendered-figure"></a>

### `simple-ar image-review`

Review rendered figures with a vision model and save feedback without editing originals. Model opinion is not scientific verification.

```text
simple-ar image-review --input PATH --goal TEXT --output PATH [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--input PATH` | Source image to edit or rendered image to review; originals retained. Repeatable. | Required |
| `--goal TEXT` | Goal text, instead of the positional request. | Required |
| `--output PATH` | This command's output directory/file. | Required |
| `--config PATH` | This command's TOML configuration; explicit CLI options take precedence. | Unset |
| `--model SELECTOR` | Model selector: env, profile:NAME or route:NAME. | Configured vision route |

Repeat --input for up to four PNG/JPEG/WebP images. Changed inputs need a new review directory; identical completed inputs recover without another call. Each output saves a budget of one model request and 40000 tokens.

Examples:

```bash
simple-ar image-review --input figure.png --goal "Check labels and hierarchy" --output reviews/v1
```

<a id="project-preparation"></a>

### `simple-ar project-info`

Read project instructions, dependencies, entry candidates and named data locations without installation or execution.

```text
simple-ar project-info --project PATH --output PATH [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--project PATH` | Existing project; ordinary goals mount it read-only, explicit repair/reproduction uses an isolated candidate. | Required |
| `--data-path PATH` | Existing data file/directory; repeatable. Does not rewrite commands or automatically copy external data. | [] |
| `--output PATH` | This command's output directory/file. | Required |

--output must not exist. Data paths resolve relative to the project and their contents are not read. Writes preparation.md and preparation.json, reusable as writing materials.

Examples:

```bash
simple-ar project-info --project ./project --data-path ./data --output preparation/v1
```


### `simple-ar report-export`

Export an assembled report to editable LaTeX, optionally compiling PDF; no model or experiment calls.

```text
simple-ar report-export --report-dir PATH --output PATH [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--report-dir PATH` | Assembled report with report_body.md and bibliography for citations. | Required |
| `--output PATH` | This command's output directory/file. | Required |
| `--title TEXT` | Reader-facing publication title. | Unset |
| `--citation-style CHOICE` | Numeric natbib display or template defaults; source citation keys unchanged. Choices: `numeric`, `template`. | Unset |
| `--latex-template PATH` | Trusted Pandoc LaTeX template relative to --report-dir; otherwise ACM. | Unset |
| `--template-asset NAME` | Flat companion filename beside template; repeatable; requires --latex-template. | [] |
| `--tex-engine CHOICE` | Installed engine for custom template; requires --latex-template; BibTeX bibliography. Choices: `pdflatex`, `xelatex`. | Unset |
| `--compile` | Compile with installed TeX/BibTeX; retain source/logs on failure. | Off |

Output must be new. Custom templates need $body$ in the document and $for(header-includes)$$header-includes$$endfor$ in the preamble. Companions must be flat filenames beside the template. Dependencies are not discovered automatically; custom build commands are not run. Compilation needs installed TeX/BibTeX; failures retain source/logs and return failure.

Examples:

```bash
simple-ar report-export --report-dir REPORT_DIR --output publication/v1 --compile
simple-ar report-export --report-dir REPORT_DIR --output publication/v2 --latex-template layout/article.latex --citation-style numeric --compile
```

<a id="artifact-tools"></a>

### `simple-ar inspect`

Write an artifact index with metadata and bounded previews, not full-text reading.

```text
simple-ar inspect RUN_DIR [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |

Does not follow symlinks/special files or hash full files by default. Writes artifact_index.json.

Examples:

```bash
simple-ar inspect RUN_DIR
```


### `simple-ar search-artifacts`

Search artifacts lexically with a fresh index; operational metadata is excluded unless requested.

```text
simple-ar search-artifacts RUN_DIR QUERY [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |
| `QUERY` | Text query for run artifacts. | Required |
| `--top-k N` | Maximum returned search results. | 8 |
| `--include-operational` | Also search operational metadata such as manifests. | Off |

Writes artifact_index.json, artifact_chunks.jsonl and artifact_search_results.json. Binary files are not chunked; oversized files are reported rather than treated as fully searched.

Examples:

```bash
simple-ar search-artifacts RUN_DIR "validation results" --top-k 5
```


### `simple-ar clean`

Preview and remove recognized rebuildable caches, retaining the run, evidence and deliveries. Shared cleanup affects multiple runs.

```text
simple-ar clean [RUN_DIR] [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required unless shared cleanup selected |
| `--yes` | Accept displayed creation/access/resource summary; never approves a pending native process. | Off; confirms deletion only after preview |
| `--all-caches` | Include all recognized rebuildable run caches/index/context; retain evidence/deliveries. | Off |
| `--shared-index` | Clear shared research index across runs. | Off |
| `--shared-cache` | Clear shared indexes, literature-provider cache and retained external-agent transcripts. | Off |
| `--index-root PATH` | Shared index root for shared cleanup. | SIMPLE_AR_RESEARCH_INDEX_ROOT or .simple_ar_cache/research_index |
| `--literature-cache-root PATH` | Literature-provider cache root for --shared-cache. | .simple_ar_cache/literature |
| `--allow-external-index-root` | Allow shared cleanup outside the current workspace. | Off |

Without --yes, deletion requires confirmation after preview. Omit RUN_DIR only for shared cleanup. Current native evidence/documents/full text/attempts and unrecognized files remain. --all-caches also includes artifact indexes/chunks; recognized legacy rebuildable targets can be broader. Shared cleanup leaves run-local deliveries but can require new provider queries.

Examples:

```bash
simple-ar clean RUN_DIR
simple-ar clean RUN_DIR --all-caches
simple-ar clean --shared-cache
```

<a id="research-commands"></a>
<a id="report-controls"></a>
<a id="interaction-policies-and-pending-decisions"></a>
<a id="prepared-reproduction"></a>
<a id="simple-ar-research-session-canonical-task-driven-entry"></a>
<a id="simple-ar-research-session-structured-and-compatibility-entry"></a>

### `simple-ar research-session`

Use exact structured inputs or continue saved research work. New tasks share the native task owner; follow the printed continuation command.

```text
simple-ar research-session --topic TEXT [OPTIONS] [--command ARGV...]
```

#### Goal, materials and creation

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--config PATH` | This command's TOML configuration; explicit CLI options take precedence. | Unset |
| `--interaction CHOICE` | Design/protocol confirmation policy; new native tasks have no stage/delivery milestone dialogs. Never grants process permission. Choices: `assisted`, `checkpoints`, `autonomous`. | checkpoints for creation; saved policy on resume |
| `--topic TEXT` | Research goal; required for creation unless supplied by TOML; optional on resume. | Required for creation unless in TOML; optional on resume |
| `--task-kind CHOICE` | Explicit structured task category. Choices: `auto`, `survey`, `bug_fix`, `measurement`, `reproduction`, `writing`, `data_analysis`. | auto |
| `--outputs CHOICE ...` | Requested outputs; cannot combine with --with-report or --no-report. Choices: `summary`, `report`, `experiments`, `bug_fix`, `code_task`, `data_analysis`. | Unset |
| `--material PATH` | Notes, drafts or an analysis package; repeatable. Ordinary materials are unverified, not bibliographic papers. | [] |
| `--model SELECTOR` | Model selector: env, profile:NAME or route:NAME. | Direct CLI: none; TOML normally env; resume keeps model |
| `--feasibility-review-model SELECTOR` | Independent model for source-backed feasibility review. | Unset |
| `--output-root PATH` | Parent directory for a new task/run. | runs/research-session |
| `--cache-dir PATH` | Optional shared full-text cache location. | Session-local cache |
| `--local-document PATH` | Local Markdown/text reference; repeatable. | [] |
| `--query TEXT` | Override search query; repeatable. | [] |
| `--provider NAME` | Override search provider; repeatable. | [] |
| `--max-results N` | Search result count limit. | 10 |
| `--max-chunks N` | Ingestion chunk count limit. | 300 |
| `--max-fulltext-documents N` | Remote full-text document count cap. | No count cap if omitted |
| `--max-pdf-mb N` | Per-fetched-PDF size cap in MiB. | No size cap if omitted |
| `--web-extract-backend CHOICE` | Public-page extraction backend; separate from PDF acquisition. Choices: `direct`, `tavily_basic`. | Unset |
| `--idea-limit N` | Structured idea candidate limit. | 3 |
| `--max-research-iterations N` | Additional rounds after first analysis; zero stops after first analysis. | 1 |
| `--cwd PATH` | Declared command working directory. | . |
| `--timeout-sec N` | Per-process time limit in seconds. | Unset |
| `--label NAME` | Declared experiment label. | research-session |
| `--primary-metric NAME` | Primary comparison metric. | Unset |
| `--metric NAME` | Required emitted metric name; repeatable. | [] |
| `--metric-direction NAME=DIRECTION` | NAME=DIRECTION; repeatable. Directions include higher, lower, resource and ignore. | [] |
| `--code-task-config PATH` | Explicit CodeTask TOML for implementing the selected direction. | Unset |
| `--command ARGV...` | Explicit argv; put last because all following tokens belong to the command. Research/start argv has no shell interpretation. | Unset |

#### Access and resource allowances

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--total-tokens N` | Shared API token budget; resume retains accounting. | No cap if omitted on direct CLI; config/resume may set one |
| `--llm-requests N` | Shared API request budget. | No cap if omitted on direct CLI; config/resume may set one |
| `--max-output-tokens N` | Per-model-response output cap. | Unset |
| `--process-invocations N` | Cumulative process invocation allowance; explicit zero is respected. | Owner computes if omitted; zero is respected |
| `--process-wall-seconds N` | Cumulative process wall-time allowance in seconds. | Owner computes if omitted; zero is respected |

#### Continue, approve and recover

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--session-root PATH` | Saved research session; follow its printed resume command. Common tasks resume with start --resume-task. | Unset |
| `--recover-interrupted` | Legacy only: confirm prior worker stopped before closing its running attempt without a result. | Off |
| `--reanalyze` | Legacy: reconsider saved measurements, separately from revisions/allowances/report requests. | Off |
| `--authorization-id NAME` | Stable idempotency key; identical ID/terms do not replenish spent capacity. | Unset |
| `--authorization-reason TEXT` | Human-readable reason for remaining-allowance changes. | Unset |
| `--authorize-remaining DIMENSION=AMOUNT` | New remaining allowance as DIMENSION=AMOUNT; repeatable. Retains prior usage/unknown reservations. | Unset |
| `--additional-attempts N` | Increase legacy total attempt cap without erasing prior attempts. | 0 |
| `--additional-no-progress N` | Increase legacy consecutive no-progress cap. | 0 |
| `--decision-id NAME` | Exact pending design/protocol decision ID; native replies require --resume-task. | Unset |
| `--decision-response CHOICE` | Reply to the pending decision; accepting a design does not approve a process. Choices: `accept`, `reject`, `revise`. | Unset |
| `--decision-guidance TEXT` | Facts or revised direction with --decision-response revise. | Unset |

#### Explicit table analysis

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--data-file PATH` | UTF-8 CSV/TSV or JSON records. Explicit table selectors choose the no-API shortcut; --kind data_analysis alone does not. | Unset |
| `--data-attribution TEXT` | User-declared source/credit/version; retained without independent verification. | Empty |
| `--value-column NAME` | Explicit numeric column; repeat for multiple metrics or matrix/coordinate columns. | [] |
| `--group-column NAME` | Category/group column; values bar/heatmap needs unique row labels. | Empty |
| `--observation-unit TEXT` | What one row represents; required for the table shortcut. | Empty |
| `--value-unit TEXT` | Selected value columns' unit; omission is unknown. | Empty |
| `--paired-baseline NAME` | Explicit same-row baseline among selected observation columns sharing a quantity/unit. | Empty |
| `--data-association CHOICE` | Opt-in descriptive Pearson r for line/scatter pairs; no significance/causal inference. Choices: `none`, `pearson`. | none |
| `--data-mode CHOICE` | observations aggregates rows; values retains supplied numbers without inferred aggregation. Choices: `observations`, `values`. | observations |
| `--data-missing CHOICE` | Reject missing values or explicitly omit them; missing y breaks lines. Choices: `reject`, `omit`. | reject |
| `--figure-width CHOICE` | Generic column (3.5 inch) or wide (7 inch) figure width. Choices: `column`, `wide`. | wide |
| `--data-max-mb N` | Physical input size limit in MiB. | 20 |
| `--data-max-figures N` | Physical SVG page limit; excess fails without dropping categories. | 100 |
| `--data-plot CHOICE` | bar: means/values; box: observation quartiles/min-max; line/scatter: explicit coordinates; heatmap: supplied matrix. Choices: `bar`, `box`, `line`, `scatter`, `heatmap`. | bar |
| `--x-column NAME` | Numeric x for values-mode line/scatter; distinct from value/group columns. | Empty |
| `--x-unit TEXT` | Declared x-coordinate unit; omission is unknown. | Empty |
| `--data-max-points N` | Physical coordinate points per figure; excess fails without sampling. | 10000 |
| `--series-layout CHOICE` | Separate axes, or shared axes for line/scatter columns with a common declared unit. Choices: `separate`, `shared`. | separate |
| `--data-path PATH` | Existing data file/directory; repeatable. Does not rewrite commands or automatically copy external data. | [] |

#### Report controls

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--report-writing-method CHOICE` | New reports use article; explicit sectional controls select sections. Resume retains the saved method. Choices: `article`, `sections`. | New article; sections with explicit sectional controls |
| `--max-section-tokens N` | Output cap per writing unit; zero/unset omits this extra cap. | Unset |
| `--with-report` | Explicitly request a model-authored report; data analysis otherwise delivers analysis only. | Off |
| `--no-report` | Skip report writing in a model-backed session. | Off |
| `--report-template NAME_OR_PATH` | Report structure name or Markdown path. | auto |
| `--report-reviewer CHOICE` | Report reviewer; disabled retains deterministic audit. Choices: `llm`, `disabled`. | llm |
| `--max-review-iterations N` | Report revision/correction bound; failed proposals count too. | 1 |
| `--report-document-review / --no-report-document-review` | Enable/disable bounded whole-document review/revision. | Off for sections; retained on resume |
| `--report-review-scope CHOICE` | section or document; document requires document-review. Choices: `section`, `document`. | section for sections; retained on resume |
| `--report-draft-scope CHOICE` | section, joint document or auto (joint up to 2000 planned words); document/auto requires document review and scope. Choices: `section`, `document`, `auto`. | section for sections; retained on resume |
| `--max-document-review-prompt-chars N` | Full review request character ceiling; zero uses client/session capacity without truncating evidence. | Unset |
| `--report-outline-strategy CHOICE` | Template outline or evidence-organized outline planning. Choices: `auto`, `template`, `adaptive`. | auto for sections; retained on resume |
| `--report-data-tables CHOICE` | Link complete copied analysis records, or also repeat full tables in the report. Choices: `linked`, `full`. | linked for sections; retained on resume |

Explicit CLI options override research TOML. A direct new command without --model uses deterministic collection/analysis; research TOML normally defaults the model to env. Collection without a model is not completion of requested experiments/reports. Supply missing contracts/model authorization and continue with the printed start --resume-task command. Model failure does not silently fall back.

Model-backed sessions normally request a report, except data analysis needs an explicit request. --outputs cannot combine with --with-report/--no-report. Detailed paired protocols, output-file selectors and environment preparation belong in research TOML, not invented CLI flags. Exact process contracts do not establish scientific success.

New tasks use start decision/approval rules. Legacy sessions retain saved milestone policies: assisted confirms protocol/delivery/follow-ups; checkpoints confirms first protocol/candidate revisions/delivery changes while allowing bounded same-protocol supplements; autonomous chooses within saved limits. --session-root preserves evidence/accounting. Use --recover-interrupted only after the old worker stops, separately from input revisions. --reanalyze is separate from revisions/authorization/report refresh; only compatible measurements are reused.

Omitted report options on resume retain saved choices. New article reports use complete-article work units; explicit sectional controls select sections. Section defaults are section drafting/review, document review off, auto outline and linked tables. Joint drafting needs document review/scope and compatible full-source settings. Disabled review retains audit. Authorize resources separately before report refresh; authorization alone does not reopen completed work.

Examples:

```bash
simple-ar research-session --config research.toml
simple-ar research-session --config examples/data-curves/research.toml
simple-ar research-session --topic "Summarize local sources" --local-document paper.md --outputs summary
```


### `simple-ar research-report`

Generate or refresh a report for a saved legacy session with ready analysis. Common native tasks request writing through start feedback or supply.

```text
simple-ar research-report --session-root PATH --model SELECTOR [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--session-root PATH` | Saved research session; follow its printed resume command. Common tasks resume with start --resume-task. | Required |
| `--refresh` | New report/audit attempts from saved evidence; keeps old reports without repeating experiments. | Off |
| `--model SELECTOR` | Model selector: env, profile:NAME or route:NAME. | Required |
| `--template NAME_OR_PATH` | Report template name or Markdown path. | experiment |
| `--reviewer CHOICE` | Report reviewer; disabled retains audit. Choices: `llm`, `disabled`. | llm |
| `--max-review-iterations N` | Report revision/correction bound; failed proposals count too. | 1 |
| `--document-review / --no-document-review` | Enable/disable bounded whole-document review/revision. | Off for sections; retained on resume |
| `--review-scope CHOICE` | section or document; document requires document-review. Choices: `section`, `document`. | section for sections; retained on resume |
| `--draft-scope CHOICE` | section, joint document or auto (joint up to 2000 planned words); document/auto requires document review and scope. Choices: `section`, `document`, `auto`. | section for sections; retained on resume |
| `--max-document-review-prompt-chars N` | Full review request character ceiling; zero uses client/session capacity without truncating evidence. | Unset |
| `--outline-strategy CHOICE` | Template outline or evidence-organized outline planning. Choices: `auto`, `template`, `adaptive`. | auto for sections; retained on resume |
| `--data-tables CHOICE` | Link complete copied analysis records, or also repeat full tables in the report. Choices: `linked`, `full`. | linked for sections; retained on resume |
| `--max-section-tokens N` | Output cap per writing unit; zero/unset omits this extra cap. | Unset |

Requires a ready-for-report legacy session; v1 is read-only. Existing reports are idempotent unless --refresh is supplied. Omitted options retain saved settings. Refresh consumes saved attempt/API budgets without rerunning experiments; authorize resources separately if exhausted. Disabled reviewer retains audit.

Examples:

```bash
simple-ar research-report --session-root SESSION --model profile:daily --refresh
```

<a id="historical-and-development-commands"></a>
<a id="simple-ar-research-brief-compatibility-alias"></a>

### `simple-ar research-brief`

Compatibility spelling for a new summary-only session; inherits research-session parameters with the limits below.

```text
simple-ar research-brief --topic TEXT [OPTIONS]
```

#### Goal, materials and creation

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--config PATH` | This command's TOML configuration; explicit CLI options take precedence. | Unset |
| `--interaction CHOICE` | Design/protocol confirmation policy; new native tasks have no stage/delivery milestone dialogs. Never grants process permission. Choices: `assisted`, `checkpoints`, `autonomous`. | autonomous |
| `--topic TEXT` | Research goal; required for creation unless supplied by TOML; optional on resume. | Required for creation unless in TOML; optional on resume |
| `--task-kind CHOICE` | Explicit structured task category. Choices: `auto`, `survey`, `bug_fix`, `measurement`, `reproduction`, `writing`, `data_analysis`. | auto |
| `--outputs CHOICE ...` | Requested outputs; cannot combine with --with-report or --no-report. Choices: `summary`, `report`, `experiments`, `bug_fix`, `code_task`, `data_analysis`. | summary only |
| `--material PATH` | Notes, drafts or an analysis package; repeatable. Ordinary materials are unverified, not bibliographic papers. | [] |
| `--model SELECTOR` | Model selector: env, profile:NAME or route:NAME. | Direct CLI: none; TOML normally env; resume keeps model |
| `--feasibility-review-model SELECTOR` | Independent model for source-backed feasibility review. | Unset |
| `--output-root PATH` | Parent directory for a new task/run. | runs/research-brief |
| `--cache-dir PATH` | Optional shared full-text cache location. | Session-local cache |
| `--local-document PATH` | Local Markdown/text reference; repeatable. | [] |
| `--query TEXT` | Override search query; repeatable. | [] |
| `--provider NAME` | Override search provider; repeatable. | [] |
| `--max-results N` | Search result count limit. | 10 |
| `--max-chunks N` | Ingestion chunk count limit. | 300 |
| `--max-fulltext-documents N` | Remote full-text document count cap. | No count cap if omitted |
| `--max-pdf-mb N` | Per-fetched-PDF size cap in MiB. | No size cap if omitted |
| `--web-extract-backend CHOICE` | Public-page extraction backend; separate from PDF acquisition. Choices: `direct`, `tavily_basic`. | Unset |
| `--idea-limit N` | Structured idea candidate limit. | 3 |
| `--max-research-iterations N` | Additional rounds after first analysis; zero stops after first analysis. | 1 |
| `--cwd PATH` | Declared command working directory. | . |
| `--timeout-sec N` | Per-process time limit in seconds. | Unset |
| `--label NAME` | Declared experiment label. | research-session |
| `--primary-metric NAME` | Primary comparison metric. | Unset |
| `--metric NAME` | Required emitted metric name; repeatable. | [] |
| `--metric-direction NAME=DIRECTION` | NAME=DIRECTION; repeatable. Directions include higher, lower, resource and ignore. | [] |
| `--code-task-config PATH` | Explicit CodeTask TOML for implementing the selected direction. | Unset |
| `--command ARGV...` | Explicit argv; put last because all following tokens belong to the command. Research/start argv has no shell interpretation. | Unset |

#### Access and resource allowances

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--total-tokens N` | Shared API token budget; resume retains accounting. | No cap if omitted on direct CLI; config/resume may set one |
| `--llm-requests N` | Shared API request budget. | No cap if omitted on direct CLI; config/resume may set one |
| `--max-output-tokens N` | Per-model-response output cap. | Unset |
| `--process-invocations N` | Cumulative process invocation allowance; explicit zero is respected. | Owner computes if omitted; zero is respected |
| `--process-wall-seconds N` | Cumulative process wall-time allowance in seconds. | Owner computes if omitted; zero is respected |

#### Continue, approve and recover

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--session-root PATH` | Saved research session; follow its printed resume command. Common tasks resume with start --resume-task. | Rejected by this alias |
| `--recover-interrupted` | Legacy only: confirm prior worker stopped before closing its running attempt without a result. | Off |
| `--reanalyze` | Legacy: reconsider saved measurements, separately from revisions/allowances/report requests. | Off |
| `--authorization-id NAME` | Stable idempotency key; identical ID/terms do not replenish spent capacity. | Unset |
| `--authorization-reason TEXT` | Human-readable reason for remaining-allowance changes. | Unset |
| `--authorize-remaining DIMENSION=AMOUNT` | New remaining allowance as DIMENSION=AMOUNT; repeatable. Retains prior usage/unknown reservations. | Unset |
| `--additional-attempts N` | Increase legacy total attempt cap without erasing prior attempts. | 0 |
| `--additional-no-progress N` | Increase legacy consecutive no-progress cap. | 0 |
| `--decision-id NAME` | Exact pending design/protocol decision ID; native replies require --resume-task. | Unset |
| `--decision-response CHOICE` | Reply to the pending decision; accepting a design does not approve a process. Choices: `accept`, `reject`, `revise`. | Unset |
| `--decision-guidance TEXT` | Facts or revised direction with --decision-response revise. | Unset |

#### Explicit table analysis

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--data-file PATH` | UTF-8 CSV/TSV or JSON records. Explicit table selectors choose the no-API shortcut; --kind data_analysis alone does not. | Unset |
| `--data-attribution TEXT` | User-declared source/credit/version; retained without independent verification. | Empty |
| `--value-column NAME` | Explicit numeric column; repeat for multiple metrics or matrix/coordinate columns. | [] |
| `--group-column NAME` | Category/group column; values bar/heatmap needs unique row labels. | Empty |
| `--observation-unit TEXT` | What one row represents; required for the table shortcut. | Empty |
| `--value-unit TEXT` | Selected value columns' unit; omission is unknown. | Empty |
| `--paired-baseline NAME` | Explicit same-row baseline among selected observation columns sharing a quantity/unit. | Empty |
| `--data-association CHOICE` | Opt-in descriptive Pearson r for line/scatter pairs; no significance/causal inference. Choices: `none`, `pearson`. | none |
| `--data-mode CHOICE` | observations aggregates rows; values retains supplied numbers without inferred aggregation. Choices: `observations`, `values`. | observations |
| `--data-missing CHOICE` | Reject missing values or explicitly omit them; missing y breaks lines. Choices: `reject`, `omit`. | reject |
| `--figure-width CHOICE` | Generic column (3.5 inch) or wide (7 inch) figure width. Choices: `column`, `wide`. | wide |
| `--data-max-mb N` | Physical input size limit in MiB. | 20 |
| `--data-max-figures N` | Physical SVG page limit; excess fails without dropping categories. | 100 |
| `--data-plot CHOICE` | bar: means/values; box: observation quartiles/min-max; line/scatter: explicit coordinates; heatmap: supplied matrix. Choices: `bar`, `box`, `line`, `scatter`, `heatmap`. | bar |
| `--x-column NAME` | Numeric x for values-mode line/scatter; distinct from value/group columns. | Empty |
| `--x-unit TEXT` | Declared x-coordinate unit; omission is unknown. | Empty |
| `--data-max-points N` | Physical coordinate points per figure; excess fails without sampling. | 10000 |
| `--series-layout CHOICE` | Separate axes, or shared axes for line/scatter columns with a common declared unit. Choices: `separate`, `shared`. | separate |
| `--data-path PATH` | Existing data file/directory; repeatable. Does not rewrite commands or automatically copy external data. | [] |

#### Report controls

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--report-writing-method CHOICE` | New reports use article; explicit sectional controls select sections. Resume retains the saved method. Choices: `article`, `sections`. | New article; sections with explicit sectional controls |
| `--max-section-tokens N` | Output cap per writing unit; zero/unset omits this extra cap. | Unset |
| `--with-report` | Explicitly request a model-authored report; data analysis otherwise delivers analysis only. | Off |
| `--no-report` | Skip report writing in a model-backed session. | Off |
| `--report-template NAME_OR_PATH` | Report structure name or Markdown path. | auto |
| `--report-reviewer CHOICE` | Report reviewer; disabled retains deterministic audit. Choices: `llm`, `disabled`. | llm |
| `--max-review-iterations N` | Report revision/correction bound; failed proposals count too. | 1 |
| `--report-document-review / --no-report-document-review` | Enable/disable bounded whole-document review/revision. | Off for sections; retained on resume |
| `--report-review-scope CHOICE` | section or document; document requires document-review. Choices: `section`, `document`. | section for sections; retained on resume |
| `--report-draft-scope CHOICE` | section, joint document or auto (joint up to 2000 planned words); document/auto requires document review and scope. Choices: `section`, `document`, `auto`. | section for sections; retained on resume |
| `--max-document-review-prompt-chars N` | Full review request character ceiling; zero uses client/session capacity without truncating evidence. | Unset |
| `--report-outline-strategy CHOICE` | Template outline or evidence-organized outline planning. Choices: `auto`, `template`, `adaptive`. | auto for sections; retained on resume |
| `--report-data-tables CHOICE` | Link complete copied analysis records, or also repeat full tables in the report. Choices: `linked`, `full`. | linked for sections; retained on resume |

The entry forces summary-only output, defaults to runs/research-brief and autonomous interaction, and rejects --session-root or outputs other than summary. The table lists inherited parser parameters; execution/report/recovery options still undergo research-session validation. Resume using the printed start --resume-task command.

Examples:

```bash
simple-ar research-brief --topic "Summarize local sources" --local-document paper.md
```


### `simple-ar research-session-continue`

Run one corrected explicit experiment after a failed/timed-out simple experiment in a saved canonical session.

```text
simple-ar research-session-continue --session-root PATH [OPTIONS] --command ARGV...
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--session-root PATH` | Saved research session; follow its printed resume command. Common tasks resume with start --resume-task. | Required |
| `--parent-attempt-id NAME` | Failed experiment to branch from; latest canonical experiment, or experiment-001 for old layouts. | Latest experiment; experiment-001 for old layouts |
| `--model SELECTOR` | Model selector: env, profile:NAME or route:NAME. | Unset |
| `--cwd PATH` | Declared command working directory. | . |
| `--timeout-sec N` | Per-process time limit in seconds. | 300 |
| `--label NAME` | Declared experiment label. | research-session-recovery |
| `--primary-metric NAME` | Primary comparison metric. | Unset |
| `--metric NAME` | Required emitted metric name; repeatable. | [] |
| `--metric-direction NAME=DIRECTION` | NAME=DIRECTION; repeatable. Directions include higher, lower, resource and ignore. | [] |
| `--command ARGV...` | Explicit argv; put last because all following tokens belong to the command. Research/start argv has no shell interpretation. | Required |

Only a corrected simple explicit experiment is supported. Scientific negative results are not technical failures. Paired/prepared-data/CodeTask failures use their own continuation path. Retains parent evidence/schema unless explicitly overridden. V1 cannot execute; native tasks use start --resume-task.

Examples:

```bash
simple-ar research-session-continue --session-root SESSION --metric accuracy --command python experiment.py
```


### `simple-ar research-session-migrate`

Create a successor from a read-only v1 session, importing only named small artifacts without changing the source.

```text
simple-ar research-session-migrate --source-root PATH --destination-root PATH [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--source-root PATH` | Read-only historical session containing session_manifest.v1. | Required |
| `--destination-root PATH` | New empty successor directory, outside source root. | Required |
| `--artifact NAME` | Explicitly named small artifact to import; repeatable. | [] |
| `--requested-output NAME` | Requested successor output; repeatable. | Legacy brief or research_summary |
| `--max-artifact-bytes N` | Byte limit per explicitly imported artifact. | 2097152 |

Only explicitly named, size-bounded artifacts are copied. Skipped facts and unknown historical budget remain disclosed, never guessed or reset.

Examples:

```bash
simple-ar research-session-migrate --source-root OLD_SESSION --destination-root NEW_SESSION --artifact research_summary.md
```

<a id="code-task-commands"></a>
<a id="high-level-orchestration"></a>
<a id="low-level-primitives"></a>

### `simple-ar code-task init`

Prepare an isolated workspace for an explicit existing-project or greenfield contract.

```text
simple-ar code-task init [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--config PATH` | TOML config for init settings. CLI flags override config values. | Unset |
| `--code-root PATH` | Source project. Required for `existing_project`; optional scaffold/source root for `greenfield`. | Unset |
| `--kind CHOICE` | `existing_project` for patching an existing codebase, or `greenfield` for from-scratch generation. Choices: `existing_project`, `greenfield`. | Unset |
| `--task-file PATH` | Markdown/text task description. Required unless set in config. | Unset |
| `--output-root PATH` | Directory where the code-task run is created. | Unset |
| `--name NAME` | Run name suffix. | Unset |
| `--benchmark-command VALUE` | Command run inside the workspace before and after edits. | Unset |
| `--primary-metric NAME` | Primary metric for before/after verdicts. | Unset |
| `--metric-direction METRIC=DIRECTION` | Metric direction: `higher`, `lower`, `resource`, or `ignore`. Repeatable. | [] |
| `--env-mode CHOICE` | `current` or `external`. Choices: `current`, `external`. | Saved/config policy; current if unset |
| `--python PATH` | Python executable for `--env-mode external`. | Saved/config interpreter; external policy needs one |
| `--workspace-mode CHOICE` | `auto`, `copy`, `git_worktree`, `sparse_copy`. Greenfield defaults to empty through TOML; CLI does not accept empty. Choices: `auto`, `copy`, `git_worktree`, `sparse_copy`. | Unset |
| `--workspace-include VALUE` | Include pattern for `sparse_copy`. Repeatable. | Unset |
| `--workspace-exclude VALUE` | Additional exclude pattern for `sparse_copy`. Repeatable. | Unset |
| `--workspace-reuse-source-venv` | Reuse a detected source `.venv` Python as external execution policy. | Unset |
| `--max-file-bytes N` | Maximum copied file size in copy/sparse modes. Use `0` to disable. | Unset |

Examples:

```bash
simple-ar code-task init --config code-task.toml
```


### `simple-ar code-task execute`

Continue bounded planning, review, edits and benchmarks for a saved CodeTask.

```text
simple-ar code-task execute RUN_DIR [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |
| `--config PATH` | Optional TOML for model routing, budget, and runtime settings. | Unset |
| `--to-step CHOICE` | Stop no later than `probe`, `baseline`, `work-plan`, `batch`, `plan`, `propose-edits`, `apply-edits`, `review`, `validate`, `run`, `analyze-failure`, or `repair`. Choices: `probe`, `baseline`, `work-plan`, `batch`, `plan`, `propose-edits`, `apply-edits`, `review`, `validate`, `run`, `analyze-failure`, `repair`. | Config or run |
| `--dry-run` | Print the next action without writing artifacts. | Off |
| `--model SELECTOR` | Model override for LLM-backed steps. | Unset |
| `--no-llm` | Use deterministic fallbacks where possible. | Off |
| `--timeout N` | Benchmark timeout. | 60 |
| `--yes` | Auto-approve inline review gates in normal execute mode; with `--interactive`, auto-continue primitive prompts. Use only after you are comfortable approving the reviewed plan/proposal. | Off |
| `--interactive` | Debug mode: confirm each primitive step instead of running continuously to the next review gate. | Off |
| `--no-review-inline` | Disable inline review prompts and stop at review gates instead. | Off |
| `--skip-validation` | Run benchmark even when static validation has not passed. | Off |
| `--strict-validation` | Treat higher-risk validation warnings as errors. | Off |
| `--validation-max-file-bytes N` | Max file size scanned by static validation. | 500000 |
| `--apply-proposed-edits` | Apply reviewed `proposed_edits.json` after plan approval. | Off |
| `--allow-large-edits` | Allow a reviewed proposal that exceeds the normal edit budget. | Off |
| `--allow-planning-fallback` | Allow offline plans after known complete response-format failures only. | Off |
| `--planning-mode CHOICE` | Greenfield architecture planning; tool_agent is a deprecated alias for compact. Choices: `tool_agent`, `compact`. | Config or compact |
| `--llm-retry-attempts N` | Completed-response correction bound including the first attempt; transport retries use model settings. | Config or 3 |
| `--planning-review-rounds N` | Deprecated; nonzero only emits a notice, granting no review calls. | Config or 0; retired |
| `--repair-rounds N` | Number of bounded repair proposals after failure. | Config or 0 |
| `--reuse-planning-from PATH` | Import immutable accepted greenfield planning after task-contract hash checks; no generated code, memory, findings or repair history. | Unset |
| `--review-gate CHOICE` | Strict blocks all blocking review findings; runtime blocks safety/direct executability, retaining packaging/report gaps. Choices: `strict`, `runtime`. | Config or strict |
| `--baseline-policy CHOICE` | Existing-project baseline handling: `auto`, `run`, `skip`, `provided`, or `none`. Use `skip`/`none` for expensive baselines, or `provided` with a metrics file. Choices: `auto`, `run`, `skip`, `provided`, `none`. | Config or auto |
| `--baseline-metrics-file PATH` | JSON or metric-line file used when `--baseline-policy provided`. | Unset |
| `--max-files N` | Context file budget for LLM steps. | 8 |
| `--max-source-chars-per-file N` | Per-file source context budget. | 4000 |
| `--env-mode CHOICE` | `current` or `external`. Choices: `current`, `external`. | Saved/config policy; current if unset |
| `--python PATH` | Python executable for external env mode. | Saved/config interpreter; external policy needs one |

Execution stops at review gates. --yes can approve inline CodeTask gates; native start process approvals use different rules. --no-review-inline stops at gates; --interactive asks before primitive steps. Applying edits requires plan approval plus --apply-proposed-edits; larger edits need separate authorization. Offline planning fallback is limited to known complete response-format failures, never transport/budget/deadline/partial-stream failures. Response corrections include the first attempt; transport retries belong to the model connection. Benchmark completion does not prove objective improvement.

Examples:

```bash
simple-ar code-task execute RUN_DIR --config code-task.toml --dry-run
simple-ar code-task execute RUN_DIR --config code-task.toml
```


### `simple-ar code-task decide-plan`

Record a human decision on the current patch plan.

```text
simple-ar code-task decide-plan RUN_DIR --decision CHOICE [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |
| `--decision CHOICE` | `approve`, `reject`, or `revise`. Required. Choices: `approve`, `reject`, `revise`. | Required |
| `--note TEXT` | Optional review note. | Empty |
| `--reviewer VALUE` | Reviewer label. | user |

Examples:

```bash
simple-ar code-task decide-plan RUN_DIR --decision approve
```


### `simple-ar code-task probe`

Inspect runtime/environment without installing dependencies or running benchmarks.

```text
simple-ar code-task probe RUN_DIR [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |
| `--env-mode CHOICE` | `current` or `external`. Choices: `current`, `external`. | Saved/config policy; current if unset |
| `--python PATH` | Python executable for external env mode. | Saved/config interpreter; external policy needs one |

Examples:

```bash
simple-ar code-task probe RUN_DIR
```


### `simple-ar code-task map`

Rebuild the workspace repository map and index.

```text
simple-ar code-task map RUN_DIR [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |
| `--no-refresh-index` | Reuse the existing `codebase_index.json`. | Off |
| `--show-summary` | Print `repo_map_summary.md`. | Off |

Examples:

```bash
simple-ar code-task map RUN_DIR
```


### `simple-ar code-task locate`

Rank likely editable files and read-only evidence.

```text
simple-ar code-task locate RUN_DIR [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |
| `--query TEXT` | Locate query. Defaults to `code_task/task.md`. | Unset |
| `--top-k N` | Candidate count per group. | 8 |
| `--refresh-map` | Rebuild index and repo map before ranking. | Off |
| `--no-read-only` | Omit protected read-only evidence files. | Off |
| `--show-summary` | Print `locate_results.md`. | Off |

Examples:

```bash
simple-ar code-task locate RUN_DIR
```


### `simple-ar code-task context`

Build a bounded prompt context pack from located files.

```text
simple-ar code-task context RUN_DIR [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |
| `--query TEXT` | Locate query. Defaults to `task.md`. | Unset |
| `--top-k N` | Locate candidate budget. | 8 |
| `--max-files N` | Maximum snippets included. | 8 |
| `--max-source-chars-per-file N` | Per-file snippet budget. | 4000 |
| `--max-total-chars N` | Total snippet budget. | 20000 |
| `--refresh-map` | Rebuild map before packing context. | Off |
| `--show-prompt` | Print `prompt_context.md`. | Off |

Examples:

```bash
simple-ar code-task context RUN_DIR
```


### `simple-ar code-task work-plan`

Generate a batch-oriented implementation work plan.

```text
simple-ar code-task work-plan RUN_DIR [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |
| `--model SELECTOR` | Model override. | Unset |
| `--no-llm` | Use fallback planning. | Off |
| `--force` | Regenerate existing work-plan artifacts. | Off |
| `--allow-planning-fallback` | Allow offline plans after known complete response-format failures only. | Off |
| `--llm-retry-attempts N` | Completed-response correction bound including the first attempt; transport retries use model settings. | 1 |
| `--max-files N` | Planning context file budget. | 8 |
| `--max-source-chars-per-file N` | Per-file source context budget. | 2500 |

Examples:

```bash
simple-ar code-task work-plan RUN_DIR
```


### `simple-ar code-task batch`

Create an attempt/batch for one work-plan item.

```text
simple-ar code-task batch RUN_DIR --work-item NAME [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |
| `--work-item NAME` | Work-plan item id, such as `W1`. Required. | Required |
| `--attempt-id NAME` | Optional attempt id, such as `attempt-001`. | Unset |
| `--force` | Create a new batch even when one already exists. | Off |

Examples:

```bash
simple-ar code-task batch RUN_DIR --work-item W1
```


### `simple-ar code-task plan`

Generate a patch plan for human review.

```text
simple-ar code-task plan RUN_DIR [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |
| `--model SELECTOR` | Model override. | Unset |
| `--no-llm` | Use fallback plan. | Off |
| `--force` | Regenerate existing plan. | Off |
| `--allow-planning-fallback` | Allow offline plans after known complete response-format failures only. | Off |
| `--llm-retry-attempts N` | Completed-response correction bound including the first attempt; transport retries use model settings. | 1 |
| `--max-files N` | Context file budget. | 8 |
| `--max-source-chars-per-file N` | Per-file source context budget. | 2500 |

Examples:

```bash
simple-ar code-task plan RUN_DIR
```


### `simple-ar code-task propose-edits`

Propose controlled old/new text edits without applying them.

```text
simple-ar code-task propose-edits RUN_DIR [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |
| `--model SELECTOR` | Model override. | Unset |
| `--no-llm` | Write deterministic empty proposal. | Off |
| `--force` | Regenerate existing proposal. | Off |
| `--max-files N` | Editable context file budget. | 8 |
| `--max-source-chars-per-file N` | Per-file source context budget. | 4000 |
| `--allow-large-edits` | Accept a large but bounded proposal after review. | Off |

Examples:

```bash
simple-ar code-task propose-edits RUN_DIR
```


### `simple-ar code-task apply-edits`

Apply a reviewed edit proposal inside the allowed workspace scope.

```text
simple-ar code-task apply-edits RUN_DIR [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |
| `--edits-file PATH` | Specific proposal file to apply. | Unset |
| `--allow-unapproved-plan` | Bypass plan approval for local tests/demos. | Off |
| `--allow-large-edits` | Apply a reviewed proposal that requires large-edit approval. | Off |

Checks paths, scope, unique old text and edit budgets. Repairs are not automatically applied: review and pass their --edits-file. --allow-unapproved-plan bypasses human approval and is intended only for local tests/demos.

Examples:

```bash
simple-ar code-task apply-edits RUN_DIR --edits-file PROPOSAL_JSON
```


### `simple-ar code-task validate`

Run lightweight static validation; passing is not runtime proof.

```text
simple-ar code-task validate RUN_DIR [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |
| `--strict` | Treat higher-risk warnings as errors. | Off |
| `--max-file-bytes N` | Maximum file size scanned. | 500000 |

Examples:

```bash
simple-ar code-task validate RUN_DIR
```


### `simple-ar code-task baseline`

Run the saved benchmark before edits.

```text
simple-ar code-task baseline RUN_DIR [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |
| `--command VALUE` | Quoted benchmark command string overriding the saved command. | Unset |
| `--timeout N` | Benchmark timeout. | 60 |
| `--skip-validation` | Run even when static validation has not passed. | Off |
| `--env-mode CHOICE` | `current` or `external`. Choices: `current`, `external`. | Saved/config policy; current if unset |
| `--python PATH` | Python executable for external env mode. | Saved/config interpreter; external policy needs one |

Examples:

```bash
simple-ar code-task baseline RUN_DIR
```


### `simple-ar code-task run`

Run the saved benchmark after edits and compare available baseline metrics.

```text
simple-ar code-task run RUN_DIR [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |
| `--command VALUE` | Quoted benchmark command string overriding the saved command. | Unset |
| `--timeout N` | Benchmark timeout. | 60 |
| `--skip-validation` | Run even when static validation has not passed. | Off |
| `--env-mode CHOICE` | `current` or `external`. Choices: `current`, `external`. | Saved/config policy; current if unset |
| `--python PATH` | Python executable for external env mode. | Saved/config interpreter; external policy needs one |

Examples:

```bash
simple-ar code-task run RUN_DIR
```


### `simple-ar code-task analyze-failure`

Summarize the latest validation/benchmark failure deterministically.

```text
simple-ar code-task analyze-failure RUN_DIR [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |

Examples:

```bash
simple-ar code-task analyze-failure RUN_DIR
```


### `simple-ar code-task repair`

Propose bounded repair edits; review and apply separately.

```text
simple-ar code-task repair RUN_DIR [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |
| `--model SELECTOR` | Model override. | Unset |
| `--no-llm` | Write deterministic empty repair proposal. | Off |
| `--max-files N` | Repair context file budget. | 8 |
| `--max-source-chars-per-file N` | Per-file source context budget. | 4000 |

Writes a proposal only; review before applying with code-task apply-edits --edits-file.

Examples:

```bash
simple-ar code-task repair RUN_DIR
```

<a id="tools-and-mcp"></a>

### `simple-ar tools schema`

Export registered read-only tool schemas to stdout or a file.

```text
simple-ar tools schema [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `--format CHOICE` | Schema export format. Choices: `mcp`, `openai`. | mcp |
| `--output PATH` | This command's output directory/file. | stdout |

Examples:

```bash
simple-ar tools schema --format mcp --output schemas.json
```


### `simple-ar tools call`

Call one registered read-only tool with a JSON object and save its trace.

```text
simple-ar tools call RUN_DIR TOOL_NAME [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |
| `TOOL_NAME` | Registered read-only run tool name. | Required |
| `--args-json VALUE` | Tool arguments as a JSON object; --args-file avoids shell quoting issues. | {} |
| `--args-file PATH` | Read argument JSON object from file instead of --args-json. | Unset |
| `--debug-payloads` | Retain larger payloads in tool traces. | Off |

Arguments must be a JSON object. --args-file takes precedence. Appends RUN_DIR/tools/tool_trace.jsonl.

Examples:

```bash
simple-ar tools call RUN_DIR TOOL_NAME --args-file arguments.json
```


### `simple-ar tools serve-mcp`

Serve registered read-only run tools over MCP stdio; no shell, write, network or installation capability is granted.

```text
simple-ar tools serve-mcp RUN_DIR [OPTIONS]
```

| Parameter | Meaning | Default or conditions |
| --- | --- | --- |
| `RUN_DIR` | Existing run/task directory. | Required |
| `--debug-payloads` | Retain larger payloads in tool traces. | Off |

Examples:

```bash
simple-ar tools serve-mcp RUN_DIR
```

Compatibility note: `simple-ar run`, `simple-ar resume`, `simple-ar research-experiment` and `simple-ar research-code-task` are retired and reject execution. Use start for goals or research-session/code-task for explicit contracts; status can inspect supported historical results. Old stage/synthesis-file options are not translated.
