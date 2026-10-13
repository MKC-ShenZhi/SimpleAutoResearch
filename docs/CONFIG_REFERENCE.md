# Configuration Reference

[中文版本](CONFIG_REFERENCE_zh.md)

This is the field reference for task configuration. Start with [Usage](USAGE.md) if you do not need expert settings; commands are listed in [CLI Reference](CLI_REFERENCE.md).

## Guided setup

Ordinary natural-language `start`, `--guided`, and outcome preferences without exact protocol controls
create a saved native session, rather than an editable research TOML. The model catalog TOML below
still owns named connections. Startup arguments provide the goal, mounts, authorization and allowances;
resume retains saved values. Only explicit structured protocols save research TOML and, when needed,
a referenced CodeTask TOML. `--chat` uses this structured setup serializer; a setup draft is not an execution session.
Follow the [complete ordinary workflow](USAGE.md#a-copyable-ordinary-user-workflow).

| Setting | Location |
| --- | --- |
| Named provider connections and routing | Model catalog TOML (optional) |
| Provider secrets / legacy single connection | Environment or private `.env` |
| Native goal, read-only materials, access and resource limits | `start` arguments; resume retains the saved session |
| Exact protocol goal, inputs, delivery and resource limits | Structured research TOML |
| Structured editable project, protection and project interpreter | Referenced CodeTask TOML |

Structured research precedence is built-in defaults → TOML → explicit CLI options. CLI lists replace file lists.
Paths resolve from the configuration location where documented; command argv remains literal.
Confirmed chat settings cannot silently replace explicit user choices. This is not the native resume override rule:
resume retains the model, interpreter and allowances; adding materials/access or resources uses explicit continuation options.

### Named model connections

Copy [the model catalog example](../examples/model-profiles/models.toml) to a private location.
Set `SIMPLE_AR_MODELS_CONFIG` to its absolute path and put `CCTQ_API_KEY` (or your chosen variable)
in the environment/private `.env`. Alternatively use `~/.config/simple-ar/models.toml`.
Only one catalog is loaded; files are not merged. Secrets never belong in the TOML.

Interactive `start` lists text connections before model-assisted work and lets you choose a name or number.
It shows the model, endpoint, streaming, timeout and credential availability, then saves `profile:NAME`
in the task configuration. An explicit `--model` is retained; descriptive analysis does not require a model.
With a catalog, do not duplicate model or endpoint settings in `.env`: keep only the referenced secrets
and, when not using the global path, `SIMPLE_AR_MODELS_CONFIG`. Without a catalog, legacy fallback is
explicitly labelled. Structured `--prepare-only` can save TOML before credentials are set.
Native `--prepare-only` preflights the connection and credentials before saving a session, without model requests or process execution.
`start`, `models` and the model client read only the command's current-directory `.env`, preserving existing environment variables.
After changing directories, ensure the catalog and credentials remain discoverable.

```bash
simple-ar models                       # local inspection, no API calls
simple-ar models --config /path/models.toml
simple-ar start "Explain these materials" --source materials/ --model profile:daily
```

`--config` on `models` inspects a file; it does not activate it for later commands.
`--model env` uses the `text` route, then `default`; CodeTask selects `code`, then `default`.
`--model profile:NAME` selects a complete connection; `route:NAME` selects a route.
Existing task TOML `[model].name` accepts these selectors too. Nested code tasks use the code route
unless their own model selector is explicit. A selected code connection survives its child calls.
Native `compose_document` uses an optional `writing` route for material selection,
composition and feedback, sharing the original task ledger. It requires `text`
capability, not a new model capability. Without this route the selected task
connection is retained, including an explicit profile. This permits inexpensive
coordination and a separately configured scientific writer; it does not guarantee
correct interpretation. Named task snapshots pin an explicit writing route, so
changing its model/endpoint requires a new task rather than a silent switch.
The same optional route is available through `focus_work` with `purpose="writing"`
and a concrete goal, for investigation, revision and delivery in the existing action
loop. This consumer requires `openai_chat`; a Responses writing profile can still
serve `compose_document`, but cannot be selected for native work. No route change
automatically increases reasoning effort, tools or the shared budget.

New whole-article composition makes one logical `ask_json` call using the selected
connection's shared bounded retries, API mode and JSON policy. Physical attempts
use the original ledger and allowance. Invalid JSON or article content is retained
and fails without automatic redrafting; recovery uses frozen inputs and saved
responses, never blindly resubmits. Historical Writer checkpoints keep their contract.

Each profile requires `api`, `base_url`, `model`, `api_key_env` and `capabilities`.
Text adapters are `openai_chat` and `openai_responses`; Chat supports `stream=true`.
Optional `http2=true` uses the SDK's HTTP/2 negotiation; install with
`pip install 'simple-autoresearch[http2]'` (source checkout: `uv sync --extra http2`).
It is disabled by default, never changes providers, and is not a guarantee against gateway stalls.
Test longer input/output on the execution host before selecting it. Changing it on a saved named
session is a connection change; old HTTP/1.1 bindings remain compatible when it is omitted/false.
Supported optional fields: `request_timeout_sec`, `max_output_tokens`, `context_input_tokens`, `retry_attempts`,
`retry_base_delay_sec`, `retry_max_delay_sec`, `reasoning_effort`, `thinking_mode`,
`reasoning_output_tokens`, `json_response_format`, `chat_token_limit_param`,
`input_price_per_million`, and `output_price_per_million`.
Timeouts/delays and supplied token caps are positive; retry count includes the first request.
Per-call output limits still apply. Provider support for reasoning/JSON parameters must be checked.
`context_input_tokens` is the native action-loop input working-set target (default
24,000), including schemas, reasoning history and observations. It is a local
setting, not a provider request parameter or certified model context window.
For longer work, a profile may set e.g. `context_input_tokens = 64000`, after
checking the provider's combined input/output limit and the task's total budget.
The active work connection owns this target; remaining tokens minus the output
reservation can reduce it. Larger inputs increase repeated input costs and do not
guarantee better decisions. Saved named tasks may retune it without changing
their model identity, permissions or ledger; exhausted contracts remain exhausted.
`reasoning_effort="low"` reduces effort; it does not disable provider thinking.
Effort labels are not portable levels: [DeepSeek maps `medium` to `high`](https://api-docs.deepseek.com/guides/thinking_mode/).
Its thinking-mode tool calls also carry prior `reasoning_content` into subsequent
inputs. Higher effort can therefore increase both reasoning output and later input
costs; do not assume it improves every inventory, edit or verification decision.
Use separate named connections for distinct work when useful, and evaluate a whole
delivery plus feedback before promoting a setting to the default.
For a Chat provider supporting the switch, use `thinking_mode="disabled"` with
empty effort or `reasoning_effort="none"`; active effort with disabled thinking is
rejected. Explicit caller/profile output caps take precedence. The optional
`reasoning_output_tokens` fallback applies only to active effort, not `none` or
disabled thinking. Providers may count both reasoning and final text against the
output cap: `finish_reason="length"` with reasoning but no final text is not a
completed result. Check the provider's semantics rather than extending timeouts.
For finite-timeout SDK Chat streaming, the timeout also bounds submission and
stream consumption by elapsed time, followed by at most two seconds of local
cleanup. A deadline without a completion marker retains unknown usage. Capped
text requests may retry on the same API within `retry_attempts`, retaining the
full failed-attempt reservation; uncapped and visual requests stop. This does
not prove provider cancellation or no charge, and insufficient budget stops retries.
`retry_attempts = 1` disables automatic retries; increasing the timeout alone
does not change that policy. The example text profile uses a 600-second deadline
and three total attempts. Choose an explicit provider-supported output cap when
uncertain text retries must fit a finite task allowance; do not copy single-attempt
image settings into text profiles without intending that restriction.
Nonstreaming, Responses and LiteLLM retain their transport timeout semantics.
For SDK requests with a positive finite timeout, TCP/TLS connection waiting is
limited to the smaller of 20 seconds and that timeout; read, write and pool waits
keep the configured value. A 600-second generation deadline therefore does not
allow a single connection attempt to stall for ten minutes. SDK internal retries
remain disabled; the existing `retry_attempts` budget governs new requests.
Synchronous finite-timeout streaming inside an existing asyncio loop requires
`await asyncio.to_thread(client.ask, ...)`, rather than nesting another loop.

`json_response_format="off"` omits the provider-native JSON parameter; `ask_json`
still parses and validates JSON. Use it for a gateway that does not support the
parameter, not as a cure for overload. A stream can carry an upstream error even
after HTTP connection success. Increasing the timeout does not cure an immediate
`overloaded` response. Bounded retries consume separate physical attempts and retain
unknown usage; they do not reset a saved task's allowance. Compare SDK and framework
requests on the execution host, including a longer response, before changing routes.
Stream diagnostics retain SDK exception type, available HTTP status and allowlisted
protocol error codes/types, not arbitrary provider text, request IDs or URLs. A
missing code is unknown, not proof of timeout; incomplete output is not adopted.
Timeout, output cap and retry tuning can be changed when resuming a saved task;
model, endpoint and code-route identity remain pinned. Old snapshots are not rewritten.

Capabilities are declarations, not probe results. `simple-ar image` uses an `openai_images`
profile for generation/editing; optional `image_size` and `image_quality` are passed to the
provider. Provider support must be verified.
An Images profile cannot be used as a text client.
Image profiles do not accept text stream/token/JSON or token-price settings.
Images with `http2=true` require the `simple-autoresearch[http2]` extra in the
actual execution environment. Missing support is rejected before a reservation
or submission record; intact completed images can still be restored offline.
Older uncertain submissions are not reclassified or automatically resent.

`simple-ar image-review` uses a separate `vision` route with an explicit
`openai_chat` or `openai_responses` profile and `vision` capability. It sends
actual image pixels, not filenames; automatic API-mode fallback is disabled.
Inspection accepts up to four static PNG/JPEG/WebP images, at most 20 MiB combined
and 4,194,304 pixels per image. Provider usage is recorded when supplied; absent
usage remains unknown, not zero. Feedback describes visible issues, not scientific validity.
Local image admission failures report the applicable limits; resize a review copy
while preserving the original. Review uses the named connection's output cap,
falling back to a bounded 3,000-token default when none is configured. Unparseable
final responses stay in the review directory's `rejected-response.json`, not in
published findings, and are not automatically resent.

In a native task, `--allow-vision` authorizes `inspect_images`; it consumes the
shared model-call and token allowances, not the image-generation allowance.
`--max-images 0` therefore disables generation/editing, not authorized inspection.
The task context distinguishes availability from budget and provider health.

For cctq's documented asynchronous image-task protocol, explicitly select
`api = "cctq_images_async"`; `openai_images` remains the generic synchronous API.
The async path uses the configured origin, saves one task ID, polls that task and
authenticates its same-origin `files/0` download. It does not switch hosts/providers
or fall back to another API. `request_timeout_sec` bounds the whole async invocation,
including submission, polling and download, with at most two seconds of local cleanup.
A saved task ID remains recoverable by GET only; interrupted submissions without an ID
are not replayed. Saved-task recovery and one feedback edit were
verified through a caller-configured proxy route; direct server transport remains
unreliable. The prior synchronous timeout remains a failed request, not a success.

Optional `proxy_env = "RESEARCH_API_PROXY"` references an HTTP(S) proxy URL in
that environment variable. It affects only this SDK/image connection, not source
search or other profiles, and is not a provider fallback. The current option
rejects URL userinfo, query and fragment; missing/invalid values fail before a
paid call. Proxy values are not saved in task snapshots or provider payloads.
Without this option, standard client environment routing remains unchanged.

Linked public-document downloads can use an explicit deployment route without
changing model connections. Set `SIMPLE_AR_DOCUMENT_ROUTE_HOSTS` to comma-separated
exact hostnames, `SIMPLE_AR_DOCUMENT_PROXY_ENV` to the name of an environment
variable containing an HTTP(S) proxy URL, and optionally
`SIMPLE_AR_DOCUMENT_CA_BUNDLE` to an existing trusted CA file. For example, route
`github.com` only; wildcards are not accepted. Unlisted hosts remain direct.
TLS verification stays enabled. These options apply to linked supporting-material
downloads, not literature search, model calls, paper-PDF transport or ZIP acquisition.
Without them, this downloader ignores ambient proxies. Configure the execution
host explicitly; a model proxy alone does not make author repositories accessible.

Literature connectors use their own optional `OPENALEX_API_KEY` and
`SEMANTIC_SCHOLAR_API_KEY` environment variables, not model-profile credentials.
They send keys in authorization headers; absent keys retain anonymous access with
provider quotas. Supplying a key does not certify source coverage or remove rate limits.
Anonymous quotas may be shared by users of the same server IP. On HTTP 429,
the current search batch stops live requests to that provider, still checks each
query's opted-in cache and continues other configured providers. Cached results
retain their provenance; this does not expand source coverage or obtain credentials.

For project documentation, repositories and dataset webpages, explicitly add
`"web"` to `research_sources` (or repeat `--provider web` alongside paper providers).
This optional connector uses `TAVILY_API_KEY` from the execution host's `.env`,
not a model profile. It uses Tavily basic search without automatic upgrades,
generated answers or automatic retries; provider-reported credits are recorded
in the search response. Missing credentials fail clearly without falling back.
Existing default providers and saved source plans are unchanged.
Web hits retain their URLs and `source="web"`; excerpts are discovery material,
not full text, verified paper metadata or proof that a project runs. Original-page
acquisition uses the existing document path. Explicit publication-year restrictions
still exclude undated pages; no year is inferred from retrieval time.

Original HTML/text pages can explicitly use Tavily basic extraction with
`[research] web_extract_backend = "tavily_basic"`, or
`start --fulltext --web-extract-backend tavily_basic`. This is independent of
search providers and requires full-text acquisition permission. The default
`direct` and PDF download permissions are unchanged; PDFs still use the existing
download path. Extracted text, its original URL, provider request ID and reported
usage are cached separately from search excerpts. Cache reuse makes no new
extraction request. Empty or failed URL results remain failures; there is no
automatic upgrade or fallback. Provider-extracted pages are not verified paper
full text, and a reported zero credit count is not a promise of free extraction.

With a catalog, legacy model/URL/key environment settings do not override profile values.
Missing profiles, credentials or incompatible capabilities fail without switching providers.
A different bare model override is rejected: define another profile instead of mixing connections.
Without a catalog, existing environment configuration works unchanged; no `.env` is deleted or migrated.

New research sessions and chat drafts save non-secret connection bindings in their existing snapshots.
Resume pins the model, endpoint and nested code route; timeout, output cap and retry
settings may be retuned. Rotate the value behind the same credential variable freely.
Restore the old connection or start a new task to change providers.
Adding an unrelated image profile does not invalidate a text session. Legacy sessions and standalone
CodeTask operations do not gain retrospective connection pinning. Keep their catalog fixed during a run.

Migration is opt-in: copy the example, register current non-secret settings, keep keys in `.env`,
inspect with `models`, and test a small new task before removing any unused legacy variables.
There is no interactive registration wizard or automatic migration command yet.

For repair, `--project-python PATH` writes `[environment] mode="external"` and `python="..."` in CodeTask TOML. Omission keeps current. This selects an existing interpreter, without installing dependencies.

By default, reproduction `--project` supplies read-only preparation and the command cwd. `--data-path` registers data; `--output-files JSON` maps producer files to `execution.output_files`. These alone do not authorize edits or infer results from arbitrary paths.

Explicit `--project` + `--allow` + independent `--validate` additionally opts into
scoped CodeTask adapter preparation. Current/external CodeTask Python applies to
validation and formal measurement; `--project-python` selects an existing interpreter.
Explicit task-venv preparation may precede adaptation in the same isolated workspace:
approved requirements/project installation uses the shared process budget, then the
prepared interpreter serves CodeTask, its independent validator and formal measurement.
Do not combine this with an external interpreter or a second `check_command`.
Installation failure stops implementation and measurement; completed recovery does not
reinstall. See the reproduction contract below.

Optional dependency preparation for a single Python command uses:

```toml
[execution.environment]
mode = "venv"
requirements = ["requirements.txt"] # relative to execution.cwd
install_project = false
timeout_sec = 300
# python_executable = "/path/to/base/python"
```

The default is current execution without installation. Preparation uses the shared process budget/logs and stops measurement on failure. Build code/network access is possible; this is not an OS sandbox or general provisioning. See [workflow boundaries](WORKFLOWS.md#explicit-task-dependency-preparation).

An explicitly chosen short runtime check can use the current environment:

```toml
[execution.environment]
mode = "current"
check_command = ["python", "check_inputs.py"] # your project's real check, not a built-in script
timeout_sec = 30
```

Guided reproduction accepts `--check-argv '["python", "check_inputs.py"]'`
before the final `--command` option. A venv profile can also include
`check_command`; bare Python aliases then use the prepared interpreter.
An explicit baseline command's bare Python alias uses that same task venv;
its arguments and any explicitly selected non-alias interpreter are retained.
Current mode retains both commands as supplied and does not install or change
interpreters. A nonzero exit or timeout stops measurement and preserves logs;
check outputs never become scientific metrics. Success proves only that the
chosen check exited successfully, not that the full experiment will succeed.
Expert configurations must budget this additional process; guided setup counts it.
Reproduction chat may include the optional check in its displayed execution
proposal using inspected project instructions. Confirmation saves it through the
same serializer; setup does not execute either command or replace explicit argv.

For source access, `materials_only` excludes online search; `--fulltext` permits best-effort remote retrieval for an online survey. Unavailable or truncated material stays explicit.

## Writing from supplied material

Put length and genre requirements in the task goal, not additional environment
variables. New adaptive article planning distinguishes "about 1,200 words" (a
soft target) from "1,000–1,200 words" (hard bounds). It does not invent a tolerance,
convert pages into words, or silently alter saved plans. Targets guide composition;
they do not make a short or incomplete report acceptable. Review still checks the
original request, evidence and complete canonical delivery. No new TOML field is
required for this distinction.
Specify whether the count includes the full delivery, excludes only references,
or also excludes title and section headings. These are separate count scopes;
saved plans keep their recorded scope rather than reinterpreting old tasks.

`start --kind writing --goal "Explain my existing results" --material notes.md --prepare-only`
saves ordinary TOML: `task.kind = "writing"`, `task.outputs = ["report"]`,
`assets.materials = ["notes.md"]`, `model.name = "env"`.
`--document`/`assets.papers` identifies a bibliographic source, not a draft;
repeat `--material` for additional drafts, notes or result descriptions. Ordinary JSON
objects/arrays are accepted as unverified supplied text, not computed analysis packages.
Malformed JSON fails before guided setup saves the task; a declared `table_analysis.*`
package must pass the existing loader, never silently fall back to unverified text.
Paths resolve from the configuration directory. Supported inputs are Markdown, text, HTML/HTM and PDF;
`assets.materials` also accepts completed `table_analysis.v1` `analysis.json` files with
their adjacent copied data. They use the same config field, not a second task/runtime.
Arithmetic is rechecked and SVG/PDF/PNG figures regenerated through the same renderer. The complete data package stays
beside the report. By default, `[report].data_tables = "linked"` links its full numerical
records rather than repeating every row table in the prose; `"full"` includes those
tables too. Neither setting samples or drops data. Figures and data links enter the
uniquely owning frozen-plan section when one is known; ambiguous ownership uses a
separate descriptive-data section, not a guessed scientific interpretation.
Opt-in adaptive planning can also assign one explicit owner to an already imported
figure package, even when other sections cite the same source. This is saved in
the existing document plan; it does not create new data or image paths. Invalid
sources and conflicting owners use the existing bounded outline correction.
CLI equivalents are `research-session --report-data-tables linked|full` and
`research-report --data-tables linked|full`. Completed historical reports are unchanged.
Native experiment reports use the same option: `linked` adds a compact record link;
`full` also embeds recorded summary tables. Both preserve `experiment_evidence.json`,
its readable `.md` companion and explicitly registered source copies beside the report.
No measurements are sampled. Assembly preview and delivery share this text; the body
still needs its required results. Attachment audit checks projected values, companion
text and declared copy presence, not scientific validity or later byte-level tampering.
ACM export carries this native package; arbitrary linked files are not discovered or copied.
Analysis-package imports cap each JSON/input at 20 MiB; incomplete/stale packages fail before writing.
This verifies the calculation, not data collection or scientific claims.
Raw tables are not automatically treated as verified experiment results. Execution configuration
is rejected. Extracted text is persisted and reused on resume; search, synthesis and experiments
are not prerequisites. Supplied results remain unverified external assertions.
New `research-session` reports select `[report].writing_method = "article"`
unless explicit section/planning/review controls require `"sections"`. Article
writing uses the shared complete-article Writer, then the existing deterministic
assembly and audit; it does not claim independent model review or backtracking.
`max_section_tokens` bounds that complete response when positive. A saved report
retains its method and checkpoint owner; resume cannot silently change methods.
Explicit incompatible controls with `writing_method = "article"` are rejected.
Explicit refresh or goal revision supplies the last accepted article as editable
prose, separately from evidence; earlier drafts, responses and charges remain.

The structured guided compatibility path selects `report.template = "material_report"`
and `report.document_review = true`, retaining the controlled section Writer.
Experts can add `review_scope = "document"` to draft the multi-section body before
the existing whole-document inspection/revision, avoiding mandatory preliminary
section reviews. It requires `document_review = true`; default `section` retains
the previous sequence, and single-section documents retain section inspection.
The same checkpoint, evidence tools and per-section revision limits apply; saved
drafts are not certified as individually reviewed. Change saved report settings
through the supported explicit report-refresh path, not by editing a checkpoint.
Joint drafting is independent of review timing. Newly guided data reports
(`start --kind data_analysis --with-report`, also via chat) explicitly select
joint drafting and complete inspection. New guided surveys and finite reproductions
use the same established defaults. Automatic draft granularity remains an explicit
experimental choice, not a new entry default. Existing configs and custom writing templates
retain their choices; expert configs can choose the same settings:

```toml
[report]
document_review = true
review_scope = "document"
draft_scope = "document" # Explicit experimental alternative: "auto"
```

`auto` drafts jointly when every section has a positive planned word count and
the full plan totals at most 2,000 words. Longer or unsized plans save sections
individually before the same complete-document review. This is a drafting heuristic,
not a token guarantee or a change to output limits. Choose `document` to always
compose remaining sections together using the frozen plan and shared evidence.
The default source
strategy `full` is required; `batch_refine` is not compatible. Default
`draft_scope = "section"` preserves separate draft calls and historical checkpoint
identity. The existing `max_section_tokens` is a per-call cap, so a positive value
bounds the whole joint answer. A validated complete section set is saved in the
same checkpoint; resuming a saved body does not regenerate it. In this explicit
mode and in `auto`, cross-section corrections still form one complete candidate;
automatic initial drafting does not split a large joint revision. One complete
candidate inspection checks outstanding issues and the current manuscript before
adoption; unsuccessful corrections retain the original sections. The existing
`max_review_iterations` bounds joint candidate rounds. Previously consumed
per-section rounds and pending legacy corrections are retained on recovery;
upgrading does not grant fresh correction or model budgets. Candidate drafts and
  their original correction requests share the existing iteration/checkpoint trace.
  With `allow_source_backtracking = true`, a joint Writer may request one optional
  read-only batch before each initial composition or joint correction. The batch
  is bounded by `max_backtracking_calls` and the existing six-result prompt window;
  it shares the gateway's per-tool limits with review. Requests/results are saved
  before and after reading; recovery does not replay allocated reads. Setting the
  option to false also disables these Writer requests. No live search, arbitrary
  path reads or new execution authority is introduced.
This does not certify scientific quality or eliminate provider failures.
This explains supplied material without requiring an experiment or a failed objective.
`analysis_report` remains available for unmet/uncertain experiment goals. Explicit saved
templates are retained; a started writing task with an automatic default reuses its
resolved template from the existing input snapshot on resume.
Use `template = "experiment"` for an honest paper-style draft; missing evidence remains explicit.
Local bibliographic metadata may be incomplete; authors, dates and venues must not be invented.

Optional `[report].outline_strategy = "adaptive"` organizes built-in non-survey templates
around the supplied purpose, claims, measurements and limitations before drafting. `auto`
keeps their existing template path; `template` disables adaptive planning. Custom Markdown
templates keep their topology. New `start --kind writing` tasks using built-in templates
explicitly save `adaptive`; this does not change `auto` in existing configuration files.
This adds one planning call (at most one correction), not new
research or measurements. Invalid source pointers are rejected; template fallback requires
the existing explicit fallback setting. Started drafts retain their checkpoint plan on resume.
After successful non-survey planning, writers retain the built-in template's intended-use
boundary, not its old section list or draft order alongside the frozen plan.
Section prompts reuse those frozen responsibilities rather than duplicating them;
this reduces repeated context, not saved evidence or review requirements.
CLI equivalents are `research-session --report-outline-strategy adaptive` and
`research-report --outline-strategy adaptive` for a requested new report.
An organized plan is not a factual certificate or a promise of publication-quality prose.

## Existing data: descriptive analysis

```toml
[task]
goal = "Describe supplied measurements"
kind = "data_analysis"
outputs = ["data_analysis"]

[analysis]
file = "observations.csv"
value_columns = ["score"]
group_column = "method"
observation_unit = "one run"
value_unit = "seconds"
```

`file` resolves relative to this TOML; `value_columns` and nonempty `observation_unit` are required.
`group_column` and `value_unit` default empty (ungrouped / unknown unit). Optional fields:
`attribution = "Dataset title, release, credit or public URL"` preserves a user-declared
source in the frozen analysis and downstream writing. It does not fetch that URL,
verify collection or invent bibliographic metadata. Omit it when unknown; it is not required.
`mode = "observations"` aggregates rows into group count/mean/sample std and empirical quartiles. `"values"` preserves supplied numeric values:
bars require unique labels; line/scatter retains every complete coordinate pair, including raw individual observations.
These mode names describe transformations, not whether the source data are observational.
`missing = "reject"` or explicitly `"omit"`; `width = "wide"` or `"column"`;
physical input/output limits `max_mb = 20`, `max_figures = 100` (positive, adjustable).
Missing values are never zero-filled; nonfinite/nonnumeric values fail. No inferred error bars or
scientific verdict. The task requires `data_analysis` output and optionally `report`,
not execution or online research. Set `outputs = ["data_analysis", "report"]`
and `[model].name = "env"` to analyze, plot and write in the same session.
The completed package feeds the existing material writer and audit. With a report,
optional `[assets].materials = ["README.md"]`
adds local data documentation/notes and `[assets].papers = ["reference.pdf"]`
adds reference sources. Use text/Markdown/HTML/PDF, not another raw table or JSON
analysis package. Retained material passages and recomputed results remain
distinct; documentation does not override `[analysis]` or authorize search.
Analysis-only reads only the table and ignores model settings; adding a report
requires a model and retains the same session budget.
`plot` defaults to `"bar"` (means). `plot = "box"` requires observation rows and
shows Q1–Q3, median and observed min–max, not uncertainty or Tukey outlier cutoffs.
Quartiles interpolate at `(n−1)p`; all valid observations contribute. Labels include
counts/omissions, and pages for one column share a scale. Supplied summaries cannot
recover quartiles; box plots do not accept coordinate or Pearson options.
`plot = "heatmap"` requires `mode = "values"` and a unique row-label `group_column`.
Selected columns must express a user-declared compatible quantity/unit; their raw
values share one global color scale, including across row/column pages. No normalization,
sorting, clustering or correlation calculation is implied. Explicit `missing = "omit"`
keeps absent cells gray/NA, including an entirely missing column; an entirely missing
matrix cannot be colored. `max_points` limits cells per SVG page, including missing cells,
and `max_figures` limits pages, without sampling. Full labels and values remain in the
analysis package and downstream writing. Heatmaps do not accept coordinate, Pearson,
paired-baseline or shared-coordinate-layout settings.

`plot = "line"` or `"scatter"` requires `mode = "values"`
and a numeric `x_column`; `x_unit` defaults empty (unknown). Optional `group_column`
identifies distinct series, not paired observations or replicate aggregation.
Each selected value column gets its own axis by default. Explicit `series_layout = "shared"`
puts selected line/scatter series on common axes with a legend, using their declared common
`value_unit`; unit compatibility is not independently checked. It does not normalize values.
The shared panel counts all selected series' positions towards `max_points`; no series is dropped.
Lines require unique x within each group, sort by x and break at missing y.
Scatter retains duplicate x; explicit `missing = "omit"` retains rows with missing
x/y in the package but does not plot those positions. Lines still require complete x.
Category legends paginate without dropping groups; all pages of a coordinate panel use the same axes.
`max_points = 10000` is a positive, adjustable coordinate-output limit; overflow
fails without sampling. Coordinates are not aggregated or fitted. See the
[complete example](../examples/data-curves/README.md).
Model connections are not used or required. Setup checks shape and explicit column names
within `max_mb` before persisting a new task; numeric validation happens at ingestion.
Settings and input bytes freeze at ingest; changing them requires a new task, while resume
reuses completed snapshots without rereading the original file. See [workflow](WORKFLOWS.md).

For an explicit same-row comparison, set `paired_baseline = "baseline"` and select
that column plus candidate columns in `value_columns`. This requires
`mode = "observations"`, `plot = "bar"` or `"box"`, and a common quantity/unit declared by the
user. Each row must represent a matched pair; grouping remains optional. The result
retains candidate-minus-baseline differences, pair/missing-pair counts, mean difference,
sample standard deviation and standard error. `missing = "omit"` omits a pair if
either value is absent; it does not subtract independently filtered column means.
Difference plots show ±1 standard error for at least two pairs, assuming independent
pairs. This is not a confidence interval, significance test or automatic improvement
verdict. Saved packages recompute paired values when imported into writing.
With `plot = "box"`, method panels show their marginal distributions on a common
scale; a separate panel still shows matched mean differences and SE. Marginal
distributions retain each column's nonmissing values, while differences use complete
pairs only. Figure metadata records what is actually drawn; computed SD is not
an uncertainty bar unless the renderer explicitly draws it.

For a different question—linear association of two supplied measurements—set
`association = "pearson"` on a line/scatter task with explicit numeric `x_column`
and selected y columns. Default `"none"` does not compute correlation. Each
declared group/column uses only its jointly present coordinate pairs; no groups
are pooled and no coordinates or figures are changed. The analysis package and
writing show Pearson r, complete/missing-pair counts and undefined-value reasons.
Constant axes or fewer than two pairs have no coefficient; with two nonconstant
pairs r is necessarily ±1. This is descriptive linear association, not a paired
mean difference, regression, significance test, confidence interval or causal
effect. A small r does not rule out nonlinear association. Coordinate mode
`"values"` means supplied x/y values are retained, not that every row is an
independent experiment. There is no extra model or dependency.

## Global `.env`

`.env` is loaded by the LLM integration for credentials, endpoint, model and
transport settings. It is ignored by Git and must not contain task assets,
source roots, datasets, or interpreter paths. Task-specific paths and execution
conditions belong to the research TOML or its referenced CodeTask TOML.

## Research TOML: sections and defaults

`report.template` defaults to `auto`: literature tasks with at most one citable
source use a concise single-source evidence review, while multi-source tasks
use a survey. Supplied-material writing automatically selects `material_report`,
with the continuation semantics described above. An explicit `source_review`,
`survey`, `material_report`, or custom template wins;
automatic selection never invents a comparison absent from the sources. Experimental
tasks select a paper, reproduction report or concise analysis report using the
analysis goal judgment. Execution `passed` is not scientific success. Unmet or
uncertain goals default to an analysis report; explicit templates still win and
cannot override measured facts. Saved session templates are preserved on resume.
Use the existing report configuration revision to opt an old session into `auto`.
Automatic structure selection does not certify the scientific judgment or prose.

Run `simple-ar research-session --config examples/survey/research.toml`.
The [survey case](../examples/survey/README.md) is a complete literature task.
Additional configuration options are documented below, not separate user workflows.
Precedence is built-in defaults, TOML, then explicit CLI options. CLI lists replace file lists.
File-relative paths resolve from the TOML directory; command argv remains literal.

### Task, model and budget

| Section | Fields | Default / requirement / condition |
| --- | --- | --- |
| `[task]` | `goal`, `kind`, `outputs`, `output_root`, `selected_idea_id` | New sessions require `goal`. Kinds: `auto` (default), `survey`, `bug_fix`, `measurement`, `reproduction`, `writing`, `data_analysis`. `measurement` requires `["experiments"]`; `reproduction` includes `experiments` and optionally `report`; `writing` only accepts `report`; `data_analysis` requires its same-name output and optionally `report`. Other outputs: `summary`, `report`, `experiments`, `bug_fix`. `output_root` defaults `runs/research-session`. Optional `selected_idea_id` selects an existing grounded candidate. |
| `[model]` | `name`, `feasibility_review_model`, `max_output_tokens` | A file config defaults `name` to `env`, which reads `SIMPLE_AR_MODEL`; `name = ""` selects deterministic processing. Optional `feasibility_review_model` routes only the source-backed implementation feasibility audit to another model on the same provider and session budget; it is saved for resume and cannot be changed within an existing session. `max_output_tokens` is optional. Credentials stay in the environment. |
| `[budget]` | `total_tokens`, `llm_requests`, `process_invocations`, `process_wall_seconds` | Token/request caps are optional for a new session (omitted means no cap for that dimension). Process values default from the task shape; set them explicitly when execution is requested. Resume keeps the saved ledger and does not reset usage. |

An independent feasibility review can challenge a candidate's mechanism before
CodeTask or training. It is still model judgment, not an executable proof of
the method or an improvement claim. If it is omitted, the main model performs
the review. Use the same `feasibility_review_model` on resume (or omit it to
reuse the saved choice).

### Research inputs and behavior

| Section | Fields | Default / requirement / condition |
| --- | --- | --- |
| `[research]` | `providers`, `queries`, `max_results`, `max_chunks`, `max_pdf_pages`, `read_max_shortlist`, `idea_limit`, `cache_dir` | Lists are optional; CLI defaults are `max_results = 10`, `max_chunks = 300`, `idea_limit = 3`. Omit `max_pdf_pages` to extract all PDF pages; a supplied positive integer explicitly limits extraction and records truncation. Changing it requires a new session because existing extracted evidence is frozen. `read_max_shortlist` is optional (default: all papers up to 24); explicitly supplied papers are retained within this reading limit, and an over-limit request fails visibly. `cache_dir` is optional and is not a safe resume-change because it is not persisted. |
| `[research]` | `use_fulltext`, `allow_pdf_download`, `keep_raw_pdf`, `max_fulltext_documents`, `max_pdf_mb`, `materials_only` | These switches default false and optional caps are unset. `materials_only = true` consumes supplied local inputs (`assets.papers`, or writing `assets.materials`) and disables search; it does not disable model reading. Writing always uses local-only scope. Guided `start --fulltext --sources search` enables PDF cache retention with a 6-document, 20 MiB-per-resource limit; expert TOML may adjust the positive caps. Automatic evidence followup reserves initial acquisition slots within these totals, not extra downloads. Remote PDFs require both PDF permission and cache retention. Full-text retrieval remains best-effort and unavailable/abstract-only states are retained honestly. |
| `[research]` | `max_iterations`, `interaction` | `max_iterations` defaults to `1`; `0` stops after the first analysis. `interaction` defaults to `checkpoints` for a new CLI session and accepts `assisted`, `checkpoints`, or `autonomous`. Critical facts and permissions block every mode. |
| `[assets]` | `papers`, `materials`, `data` | Paths resolve from the TOML directory. `papers` identifies bibliographic sources; `materials` is for `writing` drafts, notes, result descriptions or completed `table_analysis.v1` packages with copied data, not independently measured experiment metrics. Writing requires at least one input and rejects duplicate/dual-role files. `data` names execution inputs; isolated CodeTask preparation copies declared project data, while direct/external inputs remain in place. No split verification or automatic argv substitution. |

In model-planned searches, explicit configured queries remain seeds; otherwise
the planner supplies focused topic queries rather than searching the full task
description. Supporting abstract conditions guide screening and reading, not a
mandatory conjunction of every condition. The arXiv connector joins plain
keywords with `AND`; an explicitly supplied advanced connector expression is
passed through. Retrieval is not semantic confirmation of relevance or coverage.
The document limit covers different focused queries as well as required facets,
before remaining slots are filled by lexical rank.

Explicitly supplied local PDFs are parsed best-effort (all pages by default,
or the explicitly configured `max_pdf_pages`) even when `use_fulltext = false`; that flag governs remote
full-text retrieval. `allow_pdf_download = false` does not exclude a PDF already
on disk. Observed total/extracted pages, truncation and empty-text pages travel
with extraction provenance. Reading windows remain bounded independently;
extracting all pages is not full understanding or OCR. Old bundles without
coverage cannot be retrospectively certified. Parser failures remain diagnostics,
not invented paper content.
The page ceiling is implemented by the basic PDF parser. The optional
`unstructured` backend rejects an explicit PDF ceiling it cannot enforce;
external parsers must declare their own observed coverage.

### Execution and report

| Section | Fields | Default / requirement / condition |
| --- | --- | --- |
| `[execution]` | `command`, `cwd`, `timeout_sec`, `code_task_config` | Normally choose literal argv `command` plus an existing absolute `cwd`, or a CodeTask TOML reference. Reproduction permits both for explicitly authorized adapter preparation followed by the separate formal command. Omit both for literature-only work. `timeout_sec` is optional and defaults at the CLI/application boundary. |
| `[execution.environment]` | `mode`, `requirements`, `install_project`, `python_executable`, `timeout_sec`, `check_command` | Optional single-command preparation: `venv` creates a task environment; `current` requires an explicit check argv and does not install or override Python. Requirements default empty, project installation false, base venv Python this runtime, per-step timeout 300. Optional check executes after setup. Omission keeps ordinary current execution. Expert TOML budgets preparation explicitly; guided start includes it. |
| `[execution]` | `primary_metric`, `metrics`, `metric_directions` | Optional measurement schema; directions use `higher`, `lower`, `resource`, or `ignore`. |
| `[execution]` | `output_files` | Optional mapping of attachment names to relative POSIX files under the process-owned `SIMPLE_AR_OUTPUT_DIR`. Only declared UTF-8 regular files up to 2 MiB each receive bounded previews and registered read handles; missing/unreadable attachments are recorded separately from execution success. |
| `[execution]` | `pairs`, `seeds`, `seed_flag`, `seed_count` | Optional explicit comparison inputs. `pairs` contains unique integer `seed` plus literal `baseline_command` and `candidate_command`; compact seed expansion requires a literal command and explicit seed flag/count. Natural-language seed requests are not parsed. |
| `[execution]` | `baseline_policy`, `baseline_ref`, `protocol` | Policy is `run`, `skip`, or `reuse`; `reuse` requires a passed current-session artifact whose command, schema, protocol conditions, protected assets and preparation lineage match. `protocol` uses the existing experiment contract and does not certify data contents. |
| `[report]` | `writing_method` | New structured reports use `article` unless explicit section/review controls select `sections`. Article drafting shares the complete-body Writer; assembly and deterministic audit remain separate. Explicit `sections` retains planning, inspection and partial checkpoints. Historical sessions without this field remain sectional; saved owners do not switch on resume. |
| `[report]` | `template`, `reviewer`, `max_review_iterations`, `document_review`, `max_section_tokens`, `max_cited_sources`, `figures` | `template` defaults to `auto`. The controlled `sections` method defaults to `reviewer = "llm"`, with CLI review iterations `1`; `article` disables model inspection. Optional `document_review = true` selects the controlled method and adds cross-section review: argument-led plans can correct their frozen sections; legacy plans retain two targets. Each target has at most `max_review_iterations` corrections; rejected candidates consume the allowance and recovery does not reset it. `max_section_tokens = 0` omits a per-call output cap. Positive `max_cited_sources` selects controlled writing and bounds distinct final citations without truncating the reading pool; final audit records excess. Omit it for no source-count cap. Figures are deterministic by default; set `[report.figures].enabled = false` or `mode = "off"` for text-only output. |
| `[report]` | `max_document_review_prompt_chars` | Optional expert complete-request character cap; default `0` adds no hidden assembled-prompt cap. Existing retrieval windows, 60,000-character manuscript window, model capacity and cumulative session budget still apply. A positive cap refuses an oversized review rather than truncating evidence or claiming it was performed. |

Explicit `outputs` cannot be combined with `--with-report`/`--no-report`. Report structure
selection never overrides measured facts or certifies scientific success. Changed
search/ingestion settings are rejected on resume when they differ from saved values;
explicit report settings can invalidate only writer/report/audit outputs, not research
evidence or measurements. Use `research-report` to add a report to an existing prefix.

`report.figures.max_figures = 0` selects automatic generation, not text-only output.
For an adaptive document it uses only supported, evidence-bound planned diagrams;
it does not invent figures to fill a target. Supplied images can coexist with these
diagrams. A positive value is an explicit overall report limit, including imported
analysis figures; excess fails rather than silently dropping user data. Disable
generation with `enabled = false` or `mode = "off"`. These native diagrams are
editable concept overviews, not arbitrary mechanism illustrations or image synthesis.

Choose `task.kind = "measurement"` only to run and analyze one supplied command
as-is, without literature discovery, candidate design, CodeTask editing, or a
baseline comparison. It requires `outputs = ["experiments"]`, an explicit
`execution.command`, and finite process limits. Use `auto`/research for an
evidence-driven candidate or a comparative experiment; a measurement is not
proof of a scientific improvement.

An evaluator can preserve raw observations and runtime details for writing without
putting all rows in stdout:

```toml
[execution.output_files]
paired_observations = "tables/observations.csv"
runtime = "runtime.json"
```

The child command writes these files below the existing `SIMPLE_AR_OUTPUT_DIR`
environment variable supplied **for that invocation**. Do not set it in `.env`.
Author programs accepting an output argument can instead use literal
`{output_dir}` in `execution.command`, e.g. `--output={output_dir}/runtime.json`;
the existing local backend binds it to this invocation, without a shell.
For example, Python code can create `Path(os.environ["SIMPLE_AR_OUTPUT_DIR"])`
and write its declared files there. Existing commands need not use this option;
no cwd scan, stdout path discovery or arbitrary external file copying occurs.
An attachment name describes producer intent, not verified contents. The report
receives bounded previews with omitted-range information; its existing read-only
review tools can request another character window of a registered attachment.
They can also locate a literal phrase within that file instead of guessing an
offset, or select JSON record-array/CSV/TSV rows by up to four exact scalar
field/value conditions. Selected rows retain counts, provenance and truncation;
the tool does not aggregate or infer missing values. A lexical match is not
semantic validation. Local producer attachments
are named evidence, not paper references with invented bibliography keys.
Missing files do not invalidate otherwise valid metrics, but cannot support a
claim. Check the saved `output_evidence` in `results.json`. Changing this contract
on resume is an execution-input revision, not a report-only refresh; completed
historical runs are not silently enriched or rerun.

To use native JSON/CSV results as measured metrics rather than only attachments:

```toml
[execution.metric_sources]
elapsed = { output = "runtime", path = ["elapsed_seconds"] }
coverage = { output = "paired_observations", column = "coverage", match = { method = "candidate" } }
```

JSON paths select explicit keys/indices; CSV/TSV conditions must select one row.
Values must be finite numbers. Missing, ambiguous or conflicting stdout/file
values fail extraction; there is no implicit aggregation or column guessing.
Source file and selected location remain in the canonical result. A process
failure or timeout cannot become a successful measurement through a result file.

`task.kind = "reproduction"` is a **prepared, fixed-protocol** path: read supplied local
papers, synthesize the source evidence, execute the declared command, analyze its
measurements, and optionally write a reproduction report. Set `research.materials_only = true`,
provide `assets.papers`, `execution.command`, and `execution.protocol` with at least
`hypothesis`, `dataset`, and `expected_outcome`. Use `baseline_policy = "skip"`
and a finite process timeout. By default this mode does not edit code; it does not
propose innovations, expand seeds, or discover a missing environment. Optional
existing-project adapter preparation requires explicit `execution.code_task_config`
alongside the formal command. The referenced `[benchmark].command` becomes the
independent `validation_command`, and `[execute].timeout_sec` its positive
`validation_timeout_sec`; `execution.command` and `execution.timeout_sec` remain the
formal measurement. Validation argv must differ from measurement argv, including
after interpreter resolution. Preparation and scoped implementation/validation
precede measurement in the isolated project. Only approved adapter/entry edits are
allowed; protected paths apply, and authorization excludes changes to the declared
scientific protocol. Validation success alone does not verify scientific equivalence.
For a new adapter, `execution.initial_files = ["adapters/export.py"]` explicitly
authorizes creation; each path must be an exact allowed, absent, unprotected Python
file. Initialization creates fail-closed placeholders only in the isolated project,
then uses the ordinary index/edit/check path. `--allow` alone never creates files.
Preparation consumes this declaration; recovery does not overwrite implemented files.
CodeTask uses prepared `current`/`external` Python. Explicit task-venv preparation through
`execution.environment` can run first in the isolated workspace and bind its interpreter
to subsequent CodeTask validation and measurement. Do not combine it with an external
interpreter, current/check preparation or another check command. Paired comparisons still use the
ordinary research path. This is not arbitrary-paper preparation, an OS sandbox or
completed real-paper acceptance. Report `template = "reproduction"` preserves
the distinction between a published result, an adapted check, and local observations.
See [the complete CPU-only case](../examples/conformal_reproduction/README.md).

Advanced experiments may either use `[[execution.pairs]]` rows with a unique integer
`seed`, `baseline_command` and `candidate_command` (literal argv arrays), or declare
one literal `execution.command` together with explicit `seed_count`/`seeds` and an
explicit `seed_flag`. The canonical entrypoint expands the latter into bounded literal
pairs; it does not parse natural-language seed requests or interpolate shell text.
Omit a seed declaration to retain one configured execution and record that default
reason. Do not combine explicit `pairs` with compact seed settings. In LLM mode,
research design may propose conditions or an argv extension only after the supplied
entrypoint has been inspected; the proposal remains inside the authorized process
boundary and explicit configuration wins. With `code_task_config`, pairs replace its
benchmark commands for the research matrix. CodeTask still owns edit scope and
implementation settings.
For a single fixed command, `seed_flag` alone records its literal integer seed
without authorizing additional executions. A seed list only in
`execution.protocol.comparison_conditions` describes the supplied evaluator's
conditions; without `execution.seed_flag`, its literal command runs once and
may handle that batch internally. Such declarations are not proof that each
seed was measured. Top-level `execution.seeds`/`seed_count` request command
expansion and still require an explicit flag.

Supported literal bindings are `--seed 0` and `--seed=0`; neither alone permits seed extensions.
To start with one paired seed but leave an evidence-driven extension possible,
declare both `seeds = [0]` and `seed_flag = "--seed"`. This runs only the initial
seed by default; another seed still requires an analysis recommendation, a
matched baseline/candidate pair, remaining process budget and an accepted decision.
Explicit seed expansion replaces an existing argument rather than appending a
duplicate. A command seed conflicting with `protocol.comparison_conditions.seed`
is rejected. Declared protocol settings flow to design, measurement and reporting;
they do not certify data contents or retroactively modify historical measurements.
Comparison identity includes the protocol revision, data references, split, metric
definitions, comparison conditions, protected assets and result schema—not the
candidate ID or hypothesis text. Complete historical contracts use the same
read-only comparison; missing protocol evidence is not filled from a newer plan.
`baseline_policy` is `run` when a comparison is required, `skip` when it is not, or
`reuse` when `baseline_ref` points to a passed, same-condition canonical result in the
current session artifact store. Reuse checks the actual command, result schema and
declared data/split/metric/condition and preparation lineage; cwd alone or the full
narrative contract is not a sufficient identity. It never imports paper numbers as
measurements. The separate CodeTask TOML keeps its own `auto`/`none` compatibility
semantics; those aliases are not research-session execution policies.
`[execution.protocol]` uses the existing research experiment contract: dataset_refs,
split_spec, metric_specs, comparison_conditions and protected_assets, optionally
contract_id/hypothesis. Protected file paths are relative to the experiment cwd,
not the TOML directory; shared data may use absolute paths. Each protected asset
needs a unique `asset_id` and a file `path`. The existing before/after check
detects changes during execution. Only explicitly listed files are checked;
this does not verify an entire dataset directory or establish scientific
validity. For example:

```toml
[[execution.protocol.protected_assets]]
asset_id = "evaluator"
path = "evaluate.py"
```

No seed interpolation or extra scheduler is introduced. Grant process budgets
for every matrix run.
Use either literal `execution.command` plus `cwd`, or `execution.code_task_config`
for one prepared code boundary; they are mutually exclusive except for the
explicit reproduction adapter contract above.
Direct command argv uses the process `PATH`. A referenced CodeTask config applies
its `current`/`external` interpreter policy to a leading bare `python`/`python3`.

The sections below cover the existing CodeTask TOML for `code-task` and
`research-session --code-task-config`. Research TOML can reference this file through
`execution.code_task_config`; it does not duplicate CodeTask's implementation settings.

CodeTask TOML preserves its legacy cwd-relative paths. Use `{config_dir}` (the
absolute directory containing that TOML) in path fields and
`[benchmark].command` when a case should be relocatable. Otherwise declare an
explicit absolute path for a machine-owned project, dataset, split, or
interpreter. Quote command arguments when a path may contain spaces.
`[environment].required_paths` lists additional files/directories that must
exist before running; it checks presence, not dataset correctness. See the
[continual-learning case](../examples/continual_learning/README.md).

The old eight-stage outer-pipeline parser and its alias mapping are retired.
Historical configuration snapshots remain readable files, not executable workflows.

## Loading Rules

- `code-task init --config PATH` loads initialization settings.
- `code-task execute --config PATH` loads execution/model/budget settings.
- `research-session --code-task-config PATH` uses the same CodeTask parser.
- Explicit CLI flags override corresponding TOML values.
- Setup and command details: [Usage](USAGE.md), [CLI Reference](CLI_REFERENCE.md).

## Research continuation and decisions

### Continue a research session after preparing execution

Run `simple-ar research-session --config research.toml --session-root runs/research-session/<session>`.
Omitted goal/outputs stay unchanged; supplied goal, outputs, local papers, or execution settings
revise the existing session. The accepted plan is rebuilt, while attempt history remains and
measurements are reused only when their command, result schema, protocol, preparation lineage,
and protected assets still match. Literature additions do not by themselves repeat valid
experiments. Changing task kind still requires a new session.

Resolve a pending decision through this same command, without an interactive stdin prompt:

```bash
simple-ar research-session --session-root runs/research-session/<session> --topic "Original research goal" \
  --decision-id ID_FROM_RICH --decision-response accept

simple-ar research-session --session-root runs/research-session/<session> --topic "Original research goal" \
  --decision-id ID_FROM_RICH --decision-response revise \
  --decision-guidance "Add facts or provide a revised direction"
```

The response may be `accept`, `reject`, or `revise`. Revising a delivery selection also
requires an explicit report setting, such as `--report-template analysis_report`. TOML may
place `decision_id`, `decision_response`, and `decision_guidance` under `[continuation]`.
The proposal and reply remain separate decision artifacts; exact replay does not repeat
completed work, and changed task/protocol inputs invalidate an old decision.

### Explicit continuation allowances

Continuation uses the existing `research-session` command. A `[continuation]` table is optional
and only applies with `--session-root`:

```toml
[continuation]
authorization_id = "review-20260924-1"
reason = "Bounded continuation after review"
additional_attempts = 2
additional_no_progress = 1
remaining = { total_tokens = 50000, llm_requests = 8 }
```

`remaining` starts a new allowance for subsequent calls on already configured resource
dimensions. It does not estimate or resolve unknown historical usage; earlier ledger entries and
attempt counters remain intact. `additional_attempts` and `additional_no_progress` increase their
persisted caps and do not reset use. CLI equivalents are `--authorization-id`,
`--authorization-reason`, repeatable `--authorize-remaining DIMENSION=AMOUNT`,
`--additional-attempts`, and `--additional-no-progress`. Use the same ID only to replay exactly
the same terms; a new allowance requires a new ID. Do not edit the ledger or manifest by hand.
Authorization alone on a completed session keeps its status, attempts and artifacts unchanged.
Resources and attempt capacity may be replenished separately; execution still checks all required
limits. Replaying identical terms does not replenish spent capacity and can finish an interrupted
resource-ledger write. For report refresh, authorize first and then run `research-report --refresh`;
report-only work does not require expanding process permissions.

Saved research/report settings remain authoritative unless explicitly revised through the
session entrypoint. Declare intended process limits when creating a session that will later run
experiments; continuation can only extend dimensions already present in that session's ledger.
Automatic repository discovery/download/setup is not implemented.

Research preparation honors CodeTask auto/copy/git_worktree and protected paths. Auto preserves
uncommitted source by falling back to copy with a recorded reason; explicit git_worktree uses
committed HEAD. Preparation records workspace/Git provenance, not an automatic commit per candidate.
Sparse/empty workspace options remain limited to standalone CodeTask.

## CodeTask Field Reference

### Code-Task Fields

| Field | Meaning |
| --- | --- |
| `[code_task].kind` | `existing_project` patches an existing source project; `greenfield` starts from an empty workspace and generates a project under `code_task/workspace/generated_project`. |
| `[code_task].code_root` | Source project path for `existing_project`; optional scaffold/source root for `greenfield`. The original project is not edited. |
| `[code_task].task_file` | Task description for standalone CodeTask; research-session prepares its implementation handoff through the canonical application. |
| `[benchmark].command` | Command executed inside `code_task/workspace` before and after edits. It should print parseable metrics such as `accuracy: 0.82`. |
| `[benchmark].primary_metric` | Main metric used for the objective verdict. Unknown metrics are still recorded, but need directions to decide improvement. |
| `[benchmark.metric_directions]` | Direction map for metrics: `higher`, `lower`, `resource`, or `ignore`. |
| `[environment].mode` | `current` uses the active SimpleAutoResearch Python; `external` uses `[environment].python` or `[environment].python_executable`. No dependencies are installed automatically. |
| `[environment].python_executable` | Required when `mode = "external"`; the selected executable resolves a leading bare `python`/`python3` in the benchmark argv. Absolute commands and non-Python commands are retained. |
| `[environment].required_paths` | Optional path list checked when this CodeTask config is loaded; useful for data files and fixed splits that the command needs. |
| `[workspace].mode` | Workspace strategy: `auto`, `copy`, `git_worktree`, `sparse_copy`, or `empty` for greenfield code-task runs. Existing projects default to `auto`, which tries git worktree first and falls back to guarded copy when needed. |
| `[workspace].reuse_source_venv` | If a source `.venv` or `venv` is detected, record and use that Python as the execution interpreter. |
| `[implementation].provider` | Code-task implementation backend. `local` keeps the in-process SimpleAutoResearch path; `fake` is deterministic for tests; `local_llm` uses the configured LLM; `codex`, `claude_code`, `opencode`, and `external_cli` use the external-agent handoff boundary when explicitly enabled. |
| `[implementation].agent_mode` | Implementation mode for the backend: `model` keeps SimpleAutoResearch as the harness, `handoff` ingests candidate files from an external agent package, and `delegated_workspace` is recognized but currently fails explicitly until snapshot/diff/rollback execution is implemented. |
| `[implementation].allow_external_agent` | Required before external CLI backends may launch. External outputs remain untrusted until SimpleAutoResearch review, validation, benchmark, or result guards accept them. |
| `[implementation].agent_model` / `.agent_binary` / `.agent_args` / `.agent_timeout_sec` | Optional external backend launch controls. Leave `agent_model` empty unless you know the external CLI/account supports the model name. |
| `[implementation].max_repair_attempts` | Upper bound for implementation-side repair attempts, including greenfield review repair before validation. |
| `[resource].max_runtime_sec` | Runtime budget surfaced to greenfield planning and external-agent handoff context. |
| `[resource].max_files` / `.max_generated_lines` | Code-task generation budgets. `[execute].max_files` and `[execute].max_generated_lines` override these during `code-task execute`; otherwise execute reads these values. |
| `[resource].max_memory_mb` / `.allow_gpu` | Memory/GPU constraints exposed to planning. They describe the allowed resource profile; dependency installation is still not automatic. |
| `[edit_scope].allowed_patterns` | Optional workspace-relative glob allowlist for files automated edits may touch. Empty means all normalized non-protected workspace paths are editable. |
| `[edit_scope].protected_patterns` | Additional workspace-relative glob patterns treated as read-only evidence. Defaults for tests, benchmarks, `.env`, secrets, and credentials are always retained. |
| `[edit_scope].mode` | Optional label stored in `manifest.json`; it does not change behavior by itself. |
| `[safety].max_file_bytes` | Max copied file size for copy/sparse modes. This avoids accidentally copying huge model/data artifacts. |
| `[safety].validation_max_file_bytes` | Max file size scanned by static validation. |

Static checks honor Python source encoding. In a Git worktree, a syntax defect
in a byte-identical file from the recorded frozen commit stays a visible warning
unless strict validation is enabled. Changed/new invalid files and unverified
baselines remain errors; this is not a test-folder exemption or runtime success.

### Execute And Budget Fields

| Field | Meaning |
| --- | --- |
| `[execute].to_step` | Last step the state-aware executor may attempt. Use `propose-edits` to stop before applying patches, or `review` to stop after post-apply structured review. |
| `[execute].use_llm` | Enables or disables LLM-backed work-plan, patch-plan, edit-proposal, and repair steps. |
| `[execute].timeout_sec` | Benchmark timeout used by executor-managed baseline and patched runs. If omitted, execute falls back to `[benchmark].timeout`, then `[resource].max_runtime_sec`. |
| `[execute].stream_benchmark_output` | Live benchmark log mode: `off`, `line`, `auto`, or `summary`. Use `auto` for tqdm-like progress output. |
| `[execute].baseline_policy` | Existing-project baseline behavior: `auto`/`run` executes the unchanged benchmark; `skip`/`none` continues without baseline comparison; `provided` records metrics from `[execute].baseline_metrics_file`. |
| `[execute].baseline_metrics_file` | JSON or metric-line file used with `baseline_policy = "provided"`. Accepted JSON shapes include `{"accuracy": 0.8}`, `{"metrics": {...}}`, and `{"metric_values": {...}}`. |
| `[execute].apply_proposed_edits` | Lets execute apply an already reviewed proposal. Keep false for review-first workflows. |
| `[execute].allow_large_edits` | Allows application of reviewed proposals that exceed the normal budget but fit the large budget. |
| `[execute].allow_planning_fallback` | Permits explicit offline plans and greenfield scaffolds after known, complete response-format failures. Scaffolds emit no invented measurements; the unimplemented runner fails until replaced by an actual experiment. It does not replace provider, budget, deadline or incomplete-stream failures with an offline success. Keep false when the task requires model-generated implementation. |
| `[execute].planning_mode` | Complete greenfield architecture work unit; default `compact`. `tool_agent` is a deprecated alias. Historical unfinished planning receives a method migration receipt; original stages and failures are retained without resetting the ledger. |
| `[execute].planning_review_rounds` | Deprecated; default `0`. Nonzero saved values emit a migration notice and grant no review calls. Remove stage selectors and choose one existing `[models.code_task].planner` connection; named profiles are not silently replaced. |
| `[execute].llm_retry_attempts` | Bounds completed-response corrections for work/patch planning and greenfield architecture/file generation, including the first attempt. Transport retry/backoff belongs only to the shared model connection's `retry_attempts` setting (legacy: `SIMPLE_AR_LLM_RETRY_ATTEMPTS`); stages do not multiply it. Unreconciled ledger operations prevent correction and fallback, even when their numeric dimensions are accounted. |
| `[execute].repair_rounds` | Non-negative bound (default `0`) for technical repair after failed validation/benchmark execution. In `research-session` with a CodeTask project, this also bounds repair/retest after a failed single-condition research revision; each retest uses the accepted protocol and process budget. It does not authorize a scientific method change, seed extension, or paired-revision repair. Repairs still require review. |
| `[execute].max_files` | Max files included in LLM context for plan/proposal/repair steps; also limits files in a new greenfield plan, including any inserted `main.py`. If a complete plan exceeds the limit, planning rejects it rather than silently discarding modules or dependencies. Saved plans are not rebuilt under a new limit. |
| `[execute].max_source_chars_per_file` | Per-file source snippet budget for LLM context. |
| `[execute].max_generated_lines` | Greenfield generation line budget. If omitted, execute falls back to `[resource].max_generated_lines`, then to a conservative default. |
| `[models.code_task].planner` | Model used for work-plan, patch-plan, and greenfield architecture planning. |
| `[models.code_task].writer` | Model used for greenfield file generation. Existing-project edit proposals use `editor`. |
| `[models.code_task].reviewer` | Model used for code-task patch review and greenfield generated-project review. |
| `[models.code_task].editor` | Model used for edit proposal generation. |
| `[models.code_task].repair` | Model used for repair proposals after failures. |
| `[budget].profile` | Active edit budget profile. `normal` permits compact changes in up to four allowed files (12,000 total edit characters); `large` permits larger reviewed changes with explicit approval. |
| `[budget].max_batches` | Maximum number of implementation batches the executor may create for one code task. |
| `[budget].cost_cap_usd` | Optional cost cap when provider usage includes cost estimates. |
| `[budget.*].max_files` | Max files a single edit proposal may touch. |
| `[budget.*].max_edits` | Max old/new replacement operations in a proposal. |
| `[budget.*].max_old_chars` / `[budget.*].max_new_chars` | Per-edit old/new text character limits. |
| `[budget.*].max_total_edit_chars` | Total character budget across all edits. |
| `[budget.*].max_proposal_chars` | Total serialized proposal budget. |

## Workspace Mode Variants

### `auto`

`auto` is the recommended existing-project default. It prefers a detached
`git_worktree` when `code_root` is inside a local Git repository with at least
one commit. If worktree preparation is not possible, it falls back to `copy`
and records the reason plus next steps in the manifest and CLI output.

```toml
[workspace]
mode = "auto"
reuse_source_venv = false
```

When `code_root` points to a subdirectory inside a larger repository,
SimpleAutoResearch creates the worktree at the repository root and uses the
matching subdirectory as the project root for indexing, editing, and running.

### `copy`

`copy` creates a guarded physical copy under `code_task/workspace`. Use it for
non-Git projects, dirty filesystem experiments that are not committed yet, or
cases where you deliberately want to include the current working tree state.

```toml
[workspace]
mode = "copy"
reuse_source_venv = false

[safety]
max_file_bytes = 2000000
```

### `git_worktree`

`git_worktree` creates a detached worktree and fails instead of falling back
when Git isolation is not possible. `code_root` may be the repository root or a
project subdirectory inside the repository. The source project must be a local
Git repository with at least one commit. A remote GitHub repository is not
required.

```toml
[workspace]
mode = "git_worktree"
reuse_source_venv = true
```

### `sparse_copy`

`sparse_copy` copies only allowlisted paths and always applies built-in
exclusions for `.git`, virtualenvs, `runs`, cache/build directories, data/model
directories, `.env`, and secret-like paths.

```toml
[workspace]
mode = "sparse_copy"
include = ["src/**", "tests/**", "configs/**", "main.py", "pyproject.toml"]
exclude = ["data/**", "models/**", "checkpoints/**"]
reuse_source_venv = false
```

Use `sparse_copy` only when you understand the project dependency graph. It can
omit files that runtime imports need.

## Standalone Code-Task Config

Use this with `simple-ar code-task init --config PATH` and later
`simple-ar code-task execute RUN_DIR --config PATH`.

```toml
[code_task]
kind = "existing_project"     # existing_project | greenfield
code_root = "path/to/project"
task_file = "tasks/improve_model.md"
output_root = "runs"
name = "my-code-task"

[benchmark]
command = "python main.py --config configs/experiment.json"
primary_metric = "accuracy"

[benchmark.metric_directions]
accuracy = "higher"
macro_f1 = "higher"
latency_ms = "resource"
loss = "lower"

[environment]
mode = "current"
# mode = "external"
# python = ".venv/Scripts/python.exe"

[workspace]
mode = "copy"
reuse_source_venv = false

[safety]
max_file_bytes = 2000000

[execute]
to_step = "run"
use_llm = true
timeout_sec = 120
repair_rounds = 1
stream_benchmark_output = "auto"
baseline_policy = "auto"
apply_proposed_edits = false
allow_large_edits = false
allow_planning_fallback = false
planning_mode = "compact"
planning_review_rounds = 0
llm_retry_attempts = 3
max_files = 8
max_source_chars_per_file = 4000
max_generated_lines = 1600

[implementation]
provider = "local"            # local | fake | local_llm | codex | claude_code | opencode | external_cli
agent_mode = "model"          # model | handoff | delegated_workspace
allow_external_agent = false
agent_model = ""              # empty means use the external CLI/account default
agent_binary = ""
agent_args = []
agent_timeout_sec = 600

[resource]
max_runtime_sec = 120
max_files = 8
max_generated_lines = 1600
max_memory_mb = 4096
allow_gpu = false

[budget]
profile = "normal"
max_batches = 3

[budget.normal]
max_files = 2
max_edits = 4
max_old_chars = 3000
max_new_chars = 4000
max_total_edit_chars = 12000
max_proposal_chars = 24000
```

For a standalone greenfield code task, omit `code_root` unless you deliberately
want to start from a scaffold/template directory. The workspace defaults to
`empty`, and `execute` generates the project under
`code_task/workspace/generated_project`:

```toml
[code_task]
kind = "greenfield"
task_file = "tasks/build_new_project.md"
name = "greenfield-project"

[benchmark]
command = "python generated_project/main.py"
primary_metric = "accuracy"

[workspace]
mode = "empty"

[execute]
to_step = "run"
max_files = 16
max_generated_lines = 4000
baseline_policy = "none"

[implementation]
provider = "local"
agent_mode = "model"
allow_external_agent = false

[resource]
max_runtime_sec = 600
max_files = 16
max_generated_lines = 4000
allow_gpu = false
```

To test a Codex/Claude/OpenCode handoff for the same greenfield task, keep the
same `[code_task]`, `[benchmark]`, and `[resource]` sections, then change only
the implementation backend:

```toml
[implementation]
provider = "codex"
agent_mode = "handoff"
allow_external_agent = true
agent_model = ""          # use the model configured by the external CLI/account
agent_timeout_sec = 1800
```

The external agent still writes untrusted candidate files first. SimpleAutoResearch
ingests them, copies them into `code_task/workspace/generated_project`, and then
runs the normal review, validation, benchmark, guard, memory, and repair path.

## CodeTask In A Research Session

Use `research-session --code-task-config` with the existing-project CodeTask TOML.
CodeTask owns editing and validation; the application owns measurement and reporting.

## Execute And Budget

`execute` is state-aware. The config below controls how far it may proceed,
which models it uses, how much context it includes, and what edit size is
allowed.

```toml
[execute]
to_step = "run"
use_llm = true
timeout_sec = 60
repair_rounds = 1
max_files = 8
max_source_chars_per_file = 4000
stream_benchmark_output = "auto"
baseline_policy = "auto"
apply_proposed_edits = false
allow_large_edits = false
allow_planning_fallback = false
planning_review_rounds = 0
llm_retry_attempts = 3

[models.code_task]
planner = "gpt-4o-mini"
writer = "gpt-4o-mini"
reviewer = "gpt-4o-mini"
editor = "gpt-4o-mini"
repair = "gpt-4o-mini"

[budget]
profile = "normal"
max_batches = 3
cost_cap_usd = 2.0

[budget.normal]
max_files = 2
max_edits = 4
max_old_chars = 3000
max_new_chars = 4000
max_total_edit_chars = 12000
max_proposal_chars = 24000
```

`stream_benchmark_output` values:

| Value | Meaning |
| --- | --- |
| `off` / `false` | Do not relay benchmark logs live. |
| `line` | Relay newline-delimited output. |
| `auto` / `true` | Handle regular line logs and carriage-return progress such as `tqdm`. |
| `summary` | Print only a tail summary after the benchmark finishes. |

`baseline_policy` values for existing-project tasks:

| Value | Meaning |
| --- | --- |
| `auto` | Default behavior. Run the unchanged benchmark when a baseline is needed for comparison. |
| `run` | Force an unchanged baseline run. |
| `skip` | Skip the unchanged baseline but still allow code understanding, review, validation, and final benchmark execution. |
| `provided` | Record user-supplied metrics from `baseline_metrics_file`; summaries mark them as provided, not reproduced. |
| `none` | Mark the task as having no meaningful baseline comparison. Useful for pure generation or acceptance-style tasks. |

## Edit Scope Behavior

`[edit_scope]` is enforced in multiple places: repo-map role tagging, context
selection, work-plan normalization, edit proposal normalization, repair
proposal normalization, and final `apply-edits` validation.

- The source project is edited only inside `code_task/workspace`.
- If `allowed_patterns` is empty, any normalized workspace-relative path may be
  edited unless protected.
- If `allowed_patterns` is set, an edit path must match at least one allowed
  pattern and must not match any protected pattern.
- Default protected patterns for tests, benchmarks, `.env`, secrets, and
  credential-like paths are always retained. User `protected_patterns` add to
  that baseline rather than replacing it.
- Work-plan target files still constrain the current batch, so a file can be
  allowed by `[edit_scope]` but rejected for being outside the active batch.
- `apply-edits` rechecks workspace-relative paths, edit scope, active batch
  target files, and exact old-text matches before writing.
