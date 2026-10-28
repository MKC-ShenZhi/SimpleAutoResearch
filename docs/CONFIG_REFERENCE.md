# Configuration Reference

[中文版本](CONFIG_REFERENCE_zh.md) · [Usage](USAGE.md) · [CLI reference](CLI_REFERENCE.md)

| Configuration | What it controls |
| --- | --- |
| Model catalog (`models.toml`) | Named connections, routes and transport; secrets stay in the environment or a private `.env`. |
| Native task TOML (`start --task-config`) | Inputs and preferences for a native task, including [trusted local packages](#trusted-local-packages). |
| Research TOML (`research-session --config`) | A structured goal, sources, execution, writing and analysis. |
| CodeTask TOML | The editable project, workspace, interpreter, edit scope and execution budget. |

### Quick example

Save this as `task.toml`, put `notes.md` alongside it, then run
`simple-ar start --task-config task.toml`:

```toml
[task]
request = "Compare the supplied evidence and explain its limitations."
model = "env"
source = ["notes.md"]
```

The source is mounted read-only and its path is relative to the TOML file.
Configure a model first; see [named connections](#named-model-connections).
For an explicit structured protocol, see the [research TOML example](#research-toml-sections-and-defaults).

| Look up | Section |
| --- | --- |
| Model profiles, selection and routing | [Named model connections](#named-model-connections) |
| Secrets and deployment settings | [Credentials and environment](#credentials-and-environment) |
| Task, research and execution fields | [Research TOML](#research-toml-sections-and-defaults) |
| Report and data settings | [Writing](#writing-from-supplied-material) · [Existing data](#existing-data-descriptive-analysis) |
| Resume and additional allowances | [Continuation](#research-continuation-and-decisions) |
| Project fields, workspace and edit scope | [CodeTask](#codetask-field-reference) · [Edit scope](#edit-scope-behavior) |
| Extension descriptors and Python integrations | [Trusted local packages](#trusted-local-packages) · [Python API](#python-task-creation) |

## Guided setup

Ordinary `start` and `--guided` save a native session. Structured protocols and `--chat` save research TOML and may reference CodeTask TOML; a chat setup draft is not an execution session. Resume keeps saved inputs and allowances. Outcome preferences guide delivery without granting access or forbidding a report requested in the goal.

Both native and structured TOML use defaults → file → explicit CLI precedence; CLI lists replace file lists. A native task file contains one `[task]` table. Research paths resolve from the TOML directory, command argv stays literal, and protected paths resolve from execution cwd. [CodeTask loading](#loading-rules) has separate path rules.

### Native task TOML fields

| `[task]` field | Type / default / purpose |
| --- | --- |
| `request` | String goal and deliverables; required unless supplied on the command line. |
| `model` | String selector; default `env`. |
| `source` | List of read-only file or directory paths, relative to this TOML. |
| `kind` | Optional outcome preference: `survey`, `bug_fix`, `reproduction`, `writing`, `data_analysis`, `figure` or `code`. |
| `project`, `document`, `material` | Project path, original-source paths, and notes/draft/result paths; `document` and `material` are lists. |
| `data_file`, `data_path` | Data file path and additional input-path list. |
| `output_root`, `project_python` | Output root (default `runs/assistant`) and optional Python entry path. |
| `allow_execution`, `allow_network`, `allow_vision` | Booleans, default `false`; process proposals, public-source acquisition and image inspection require their respective permissions. Each process command also needs approval. |
| `max_turns`, `max_tokens` | Integer model allowances, default `30` requests and `600000` tokens. |
| `max_processes`, `max_process_seconds` | Cumulative process invocation and wall-time caps; default to `max_turns × 10` and `max_turns × 300`. Zero disables the respective capacity. |
| `max_images` | Integer paid image generation/edit submissions, default `0`; separate from vision inspection. |
| `from_session`, `reuse` | Saved-task path and list of named output groups for new work; see below. |
| `extensions` | List of explicitly selected paths; see [trusted local packages](#trusted-local-packages). |

Relative paths in these fields use the task file's directory. Explicit CLI values override the file. `simple-ar start --write-task-config task.toml` exports editable settings without creating a task.

<a id="v210-task-declarations-development-branch"></a>

To create new work from saved outputs, native `[task]` accepts `from_session = "../saved-task"` and `reuse = ["artifacts"]`, or groups listed by `simple-ar results`. The path is relative to the task configuration. Reused materials are byte-checked; permissions, budget and execution state are not inherited. Use `--resume-task` to continue the same task.

## Named model connections

Load one catalog from an explicit catalog path where offered, `SIMPLE_AR_MODELS_CONFIG`, or `~/.config/simple-ar/models.toml`. New native tasks also retain the selected catalog's absolute location for each authorized consumer. On continuation, explicit caller/environment selection wins, then an existing retained location, then the user catalog. Catalogs are not merged or discovered by scanning project directories. `models --config PATH` inspects that file without activating it for later commands. `start`, `models` and the client load current-directory `.env` without replacing existing environment variables.

```toml
version = 1

[profiles.daily]
api = "openai_chat"
base_url = "https://provider.example/v1"
model = "provider-model-name"
api_key_env = "RESEARCH_API_KEY"
capabilities = ["text", "code"]
max_output_tokens = 8000

[routes]
default = "daily"
text = "daily"
code = "daily"
writing = "daily"
```

Replace endpoint/model with supported values and set the referenced credential privately. See [the catalog example](../examples/model-profiles/models.toml). `simple-ar models` checks settings and credential presence without API calls. Interactive `start` offers text profiles; an explicit model is retained. Structured `--prepare-only` can save TOML before credentials exist; native preparation preflights credentials without making calls.

For the official DeepSeek API, [this catalog](../examples/model-profiles/deepseek.toml) uses Flash for text/code and image inspection, with separate effort and output bounds. The provider documents [high effort for agent work](https://api-docs.deepseek.com/guides/thinking_mode/) and [Flash image input](https://api-docs.deepseek.com/guides/vision/); these are API capabilities, not a quality guarantee. Select the catalog before creating a task. Restore a historical task's saved connection when resuming it; changing a default does not authorize switching that task.

| Selector / consumer | Resolution |
| --- | --- |
| `env` | Catalog `text`, then `default`; without a catalog, legacy environment connection |
| `profile:NAME` | Complete named connection |
| `route:NAME` | Purpose route, then capability-compatible `default` |
| Nested CodeTask | `code`, then `default`, unless explicitly selected |
| Native writing | Explicit `writing` route; otherwise selected task connection, including explicit profile |
| Vision / image | Compatible configured `vision` / `image` connection; no text-client substitution |

`[model].name` accepts selectors too. With a catalog, a raw model ID is accepted only when it exactly matches the model of the connection resolved for that purpose; it does not search profiles by model ID or switch connections. Use `profile:NAME` to select another connection. Missing names, credentials or capabilities fail without provider fallback. Profile names must be nonempty and contain no `:`. Routes: `default`, `text`, `code`, `writing`, `vision`, `image`.

| Profile field | Default / allowed values |
| --- | --- |
| `api` | Required: `openai_chat`, `openai_responses`, `openai_images`, `cctq_images_async` |
| `base_url`, `model`, `api_key_env`, `capabilities` | Required: HTTP(S) URL without userinfo/query/fragment; nonempty model; credential variable name; nonempty list from `text`, `code`, `vision`, `image_generate`, `image_edit` |
| `proxy_env` | Empty; variable containing HTTP(S) proxy URL without userinfo/query/fragment; resolved URL is not persisted |
| `stream`, `http2` | `false`, `false`; streaming supported for Chat only; Responses profiles require `stream=false`, including for structured tasks |
| `request_timeout_sec` | `180`; positive seconds. For finite SDK Chat streams, also bounds the whole stream. |
| `connect_timeout_sec` | Unset: SDK TCP/TLS cap of 20 seconds. Otherwise finite positive seconds, bounded by a finite request timeout; text SDK only. |
| `max_output_tokens` | Unset; positive output cap |
| `context_input_tokens` | `24000`; positive native input working-set target, not provider context-window declaration |
| `retry_attempts` | `3`; at least 1, includes first physical request |
| `connect_retry_attempts` | `1`; optional higher attempt ceiling only for verified pre-dispatch connection failures. Each attempt uses request budget. |
| `native_response_corrections` | Strict integer `0` (default) or `1`; authorizes one settled truncation correction for native Chat turns only. |
| `retry_base_delay_sec`, `retry_max_delay_sec` | `1`, `12`; positive seconds |
| `reasoning_effort` | Empty (omit), `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`; support depends on provider and model. |
| `thinking_mode` | Empty, `enabled`, `disabled`, `auto`; Chat disabled thinking requires empty effort or `none` |
| `reasoning_output_tokens` | Unset; positive fallback for active effort only; explicit call/profile caps win |
| `json_response_format` | `auto` (default), `off`, `json_object`; off omits native JSON parameter, local validation remains |
| `chat_token_limit_param` | `auto` (default), `max_tokens`, `max_completion_tokens` |
| `input_price_per_million`, `output_price_per_million` | Unset; nonnegative estimates |
| `image_size` | Unset; `auto`, `256x256`, `512x512`, `1024x1024`, `1024x1536`, `1536x1024`, `1792x1024`, `1024x1792` |
| `image_quality` | Unset; `auto`, `standard`, `hd`, `low`, `medium`, `high` |

### Profile constraints

- Profile fields and types are strict. Images profiles accept image capabilities and reject text-generation options; text profiles reject image size, quality and generation capabilities. Capabilities are declarations, not provider probes. Native decisions and `focus_work` require Chat; `compose_document` can use Responses on the writing route. Budgeted vision inspection requires an explicit vision-profile `max_output_tokens`.
- HTTP/2 needs `simple-autoresearch[http2]` on the execution host. A configured proxy applies only to that profile. SDK Chat streaming includes submission and consumption in its finite request timeout. From an existing asyncio loop, use `await asyncio.to_thread(client.ask, ...)`.
- New named research/native tasks and chat drafts pin model, endpoint, protocol, HTTP/2, reasoning, correction authorization and applicable routes. Stream, timeouts, output cap, retries and input target may be adjusted; keys can rotate under the same variable. An explicit native text-connection change starts a new conversation boundary without resetting the task or budget. Saved catalog locations are lookup hints; older tasks retain their contracts, and standalone CodeTask does not gain retrospective pinning.
- `native_response_corrections=1` requires a ledger and finite output cap. Only a completed, settled `finish_reason=length` Chat turn can be corrected once; its partial text and tools are not adopted. The correction shares the original budget and stops if a new attempt has unknown or reserved usage. Enabling it on a saved connection requires an explicit connection change.
- A timeout or interrupted response can leave usage unknown; changing transport settings does not release that reservation or resume a failed call. Output caps and price estimates do not guarantee a provider spending limit.

<a id="global-env"></a>

## Credentials and environment

The model TOML can be shared globally. The `.env` file is read from the command's
current working directory, not automatically from the global model configuration directory.

Keep credentials and deployment settings here or in the environment; task paths, data and interpreters belong in task inputs/TOML. Catalog profiles override legacy model/URL/key settings.

| Variable | Purpose / legacy default |
| --- | --- |
| `SIMPLE_AR_MODELS_CONFIG` | Catalog path; absolute path recommended for shared setup |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `SIMPLE_AR_MODEL` | Legacy connection; model `gpt-4o-mini` |
| `SIMPLE_AR_LLM_TIMEOUT_SEC`, `SIMPLE_AR_MAX_OUTPUT_TOKENS` | `180` seconds; output cap unset |
| `SIMPLE_AR_LLM_RETRY_ATTEMPTS`, `SIMPLE_AR_LLM_RETRY_BASE_DELAY_SEC`, `SIMPLE_AR_LLM_RETRY_MAX_DELAY_SEC` | `3` attempts; `1`/`12`-second delays |
| `SIMPLE_AR_LLM_BACKEND`, `SIMPLE_AR_LLM_API` | Legacy transport/API; API defaults to `responses`. Native decisions require explicit `chat`; these variables do not override catalog profiles |
| `SIMPLE_AR_JSON_RESPONSE_FORMAT`, `SIMPLE_AR_CHAT_TOKEN_LIMIT_PARAM` | Legacy JSON/token parameter policy |
| `SIMPLE_AR_LLM_REASONING_EFFORT`, `SIMPLE_AR_LLM_THINKING`, `SIMPLE_AR_LLM_REASONING_OUTPUT_TOKENS`, `SIMPLE_AR_LLM_STREAM` | Legacy reasoning/stream controls |
| `SIMPLE_AR_INPUT_PRICE_PER_1M`, `SIMPLE_AR_OUTPUT_PRICE_PER_1M` | Optional cost estimates |
| `SIMPLE_AR_LLM_MAX_WORKERS` | Shared model worker bound |
| `OPENALEX_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY` | Optional independent connector keys; anonymous access keeps quotas |
| `TAVILY_API_KEY` | Required for optional `web` basic search / `tavily_basic` extraction |
| `SIMPLE_AR_DOCUMENT_ROUTE_HOSTS`, `SIMPLE_AR_DOCUMENT_PROXY_ENV`, `SIMPLE_AR_DOCUMENT_CA_BUNDLE` | Linked-document exact comma-separated hosts, proxy-variable name, optional trusted CA file |
| `SIMPLE_AR_RESEARCH_INDEX_ROOT` | Historical index location for explicit cleanup; default `.simple_ar_cache/research_index`. Not a source-query backend. |

Linked-document routing uses exact hosts and TLS verification; it does not change model, search, PDF or ZIP transport. Without explicit routing the downloader ignores ambient proxies. `SIMPLE_AR_OUTPUT_DIR` is supplied per process, not configured globally.

The `web` provider uses Tavily basic search; extraction is separately authorized and cached. Remote PDFs have separate permissions. Snippets and metadata are discovery material, not verified originals.

## Research TOML: sections and defaults

For a structured survey, save `research.toml` alongside `paper.md` and run
`simple-ar research-session --config research.toml`:

```toml
[task]
goal = "Compare supplied evidence and limitations"
kind = "survey"
outputs = ["report"]

[model]
name = "env"

[research]
materials_only = true

[assets]
papers = ["paper.md"]
```

See the [survey example](../examples/survey/README.md). Unknown research sections, fields and types fail validation. Model-backed common creation defaults to 30 requests and 600000 tokens; without a model, omitted model allowances are zero. Resume retains saved usage and limits.

### Task, model and budget

| Field | Default / constraint |
| --- | --- |
| `task.goal` | Required for new session |
| `task.kind` | `auto`; `survey`, `bug_fix`, `measurement`, `reproduction`, `writing`, `data_analysis` |
| `task.outputs` | List from `summary`, `report`, `experiments`, `bug_fix`, `code_task`, `data_analysis`; kind constraints below |
| `task.output_root`, `task.selected_idea_id` | `runs/research-session`; optional existing grounded candidate ID |
| `model.name` | File default `env`; empty string: deterministic |
| `model.feasibility_review_model` | Optional same-provider feasibility reviewer; shared budget, saved choice cannot change |
| `model.max_output_tokens` | Optional positive output cap |
| `budget.total_tokens`, `budget.llm_requests` | Model-backed common creation: positive integer caps, default 600000 tokens / 30 requests |
| `budget.process_invocations`, `budget.process_wall_seconds` | Nonnegative; task-derived defaults; declare finite intended execution limits |

The following kind restrictions apply to structured `research-session`, not to the tool combinations available to ordinary `start` tasks.

| Kind | Constraint |
| --- | --- |
| `survey` | Literature only; no execution except explicit code-delivery compatibility contract |
| `bug_fix` | Existing-project CodeTask reference; `bug_fix` output, optional report; no baseline |
| `measurement` | Declared command(s); `experiments`, optional summary/report (writing requires a model); no CodeTask; sources/design and verified external baselines may accompany the same task |
| `reproduction` | Prepared fixed protocol below; experiments, optional report |
| `writing` | Local materials/papers; report only; no execution |
| `data_analysis` | Supplied table; data_analysis, optional report; no research/CodeTask/process |
| `auto` | Declared contracts and the common action loop; capabilities are selected as needed, not a second fixed workflow; installation requires its own authorization |

Explicit outputs cannot combine with `--with-report`/`--no-report`. Feasibility review is model judgment, not executable proof.

### Research inputs and behavior

| Field | Default / constraint |
| --- | --- |
| `research.providers`, `research.queries` | Optional lists. Built-in provider names: `arxiv`, `openalex`, `semantic_scholar`, `local_files`, `web`; `web` needs a Tavily key. Explicit queries remain search seeds |
| `research.max_results`, `research.max_chunks`, `research.idea_limit` | Positive; CLI `10`, `300`, `3` |
| `research.max_pdf_pages` | Unset: all local pages; positive ceiling records truncation; frozen extraction changes need new session |
| `research.read_max_shortlist` | Optional; default all up to 24. Explicit papers retained within limit; excess fails |
| `research.cache_dir` | Optional shared full-text cache; not persisted as a safe resume change |
| `research.use_fulltext`, `research.allow_pdf_download`, `research.keep_raw_pdf`, `research.materials_only` | `false`. Materials-only disables search, not model reading; writing always local |
| `research.max_fulltext_documents`, `research.max_pdf_mb` | Optional positive remote caps; guided full-text search: 6 documents, 20 MiB each |
| `research.web_extract_backend` | `direct` or `tavily_basic` |
| `research.max_iterations` | Default `1`; bounds additional source collections in new common tasks. Legacy scientific follow-ups retain their saved contract |
| `research.interaction` | CLI default `checkpoints`; also `assisted`/`autonomous`. New tasks use protocol/design confirmation; legacy stage/delivery policies remain with their saved owner. See [interaction policies](CLI_REFERENCE.md#interaction-policies-and-pending-decisions) |
| `assets.papers`, `assets.materials`, `assets.data` | Bibliographic sources; writing drafts/notes/results/packages; execution inputs. Project data copied into isolated preparation, external data stays; no argv substitution or split verification |

Local PDFs can be parsed without remote full-text access; remote PDFs require download and retention permissions. Page limits record truncation, and unsupported parsers reject limits they cannot enforce. Search preserves explicit query seeds; arXiv joins plain keywords with `AND`. Automatic expansion with model reading can add one search/ingest/read cycle within existing limits.

### Execution and report

Fields below are under `[execution]`.

| Field | Constraint |
| --- | --- |
| `command`, `cwd`, `timeout_sec` | Literal argv, existing cwd, optional positive timeout; research-relative cwd uses TOML directory; CLI cwd defaults `.` |
| `code_task_config` | Referenced CodeTask TOML; normally exclusive with command except reproduction adapters |
| `primary_metric`, `metrics`, `metric_directions` | Optional schema; direction list e.g. `["accuracy=higher"]`; higher/lower/resource/ignore |
| `output_files`, `metric_sources` | Registered attachments / explicit finite numeric selectors |
| `pairs`, `seeds`, `seed_flag`, `seed_count` | Explicit paired argv or compact expansion; do not mix |
| `baseline` | Optional table declaring the control `command` and overrides; otherwise uses the main command |
| `baseline_policy`, `baseline_ref` | run/skip/reuse; fixed measurements can reference passed same-condition canonical result files, with a path list in condition order for paired runs |
| `protocol` | contract_id/hypothesis, dataset_refs, split_spec, metric_specs, comparison_conditions, protected_assets; reproduction also dataset/expected_outcome |
| `initial_files` | With explicit CodeTask preparation: exact allowed, absent, unprotected Python paths, not limited to reproduction. New tasks pass these as required creations to CodeTask, not initializer stubs. Saved legacy contracts retain their original semantics. |

Omit command and CodeTask for literature-only work. For `baseline_policy="reuse"`, `baseline_ref` is relative to the configuration (or cwd without one). The prior result must pass and match command, schema, protocol and protected-file hashes; it becomes a read-only snapshot and only candidate conditions run. A CodeTask can use the same verified external baseline without claiming it came from its own preparation.

| `[execution.environment]` field | Default / constraint |
| --- | --- |
| `mode` | venv creates task environment; current requires explicit check and performs no install/interpreter override. Omit table for ordinary current execution |
| `requirements`, `install_project` | `[]`, `false`; requirements relative to execution cwd |
| `python_executable` | Venv base interpreter, default runtime |
| `timeout_sec`, `check_command` | 300 seconds per step; optional literal short-check argv |

Preparation and checks share process budgets; failure stops measurement. A task venv resolves bare Python aliases, while conflicting explicit executables are rejected. Installation can run build code or use the network; a workspace is not an OS sandbox.

When source baselines are declared, complete them before implementation; an empty project cannot invent one. `skip` runs only declared candidates. Ordinary validation and measurement may use the same argv but are separate calls; reproduction requires distinct validation argv.

```toml
[execution.output_files]
runtime = "runtime.json"
observations = "tables/observations.csv"

[execution.metric_sources]
elapsed = { output = "runtime", path = ["elapsed_seconds"] }
coverage = { output = "observations", column = "coverage", match = { method = "candidate" } }
```

Attachments must be UTF-8 regular files of at most 2 MiB under `SIMPLE_AR_OUTPUT_DIR`, registered with relative POSIX paths. A literal `{output_dir}` argument binds that directory. Registered files have previews and read handles; missing or oversized files cannot support claims. `results.json.output_evidence` records provenance and truncation. Readers can select character windows, literal matches or JSON/CSV/TSV rows with up to four exact scalar conditions.

Metric selectors must identify a finite value: explicit JSON keys/indices or one CSV/TSV row. Missing, ambiguous or stdout-conflicting values fail; a file cannot make a failed process successful.

#### Fixed-protocol reproduction

Set `materials_only=true`, local papers, command, finite timeout, `baseline_policy="skip"`, hypothesis, dataset and expected outcome. An optional CodeTask prepares scoped adapter/entry edits; its benchmark must validate successfully before measurement, with different argv after interpreter resolution. Initial files must be explicitly allowed and absent. No method, split or seed expansion is implied; environment checks precede implementation. A task venv requires initially current CodeTask Python and cannot combine with source-venv reuse. See the [reproduction example](../examples/conformal_reproduction/README.md).

#### Pairs and seeds

Each `[[execution.pairs]]` entry requires a unique integer `seed` and literal argv arrays named `baseline_command` and `candidate_command`:

```toml
[[execution.pairs]]
seed = 0
baseline_command = ["python", "evaluate.py", "--method", "baseline", "--seed", "0"]
candidate_command = ["python", "evaluate.py", "--method", "candidate", "--seed", "0"]
```

Alternatively use `command`, unique `seeds` or a positive `seed_count`, and an explicit `seed_flag`; do not mix with `pairs`. Existing seed flags are replaced. A flag alone records one seed; protocol-only seeds describe conditions without expanding commands. Additional pairs require an analysis recommendation, an accepted decision and process capacity.

Baseline reuse checks command, data, split, metrics, conditions, preparation and declared protected assets. Each protected file needs a unique `asset_id` and path, relative to execution cwd or absolute. Budget every invocation; direct argv uses process PATH and CodeTask resolves bare Python according to its environment policy.

## Writing from supplied material

Structured writing uses `kind="writing"`, `outputs=["report"]`, local materials or papers, and a model. Ordinary `start --kind writing` creates a native task. Put sources in `assets.papers` and drafts, notes or results in `assets.materials`; duplicate or dual-role paths fail. Supported inputs include text, Markdown, HTML and PDF. Ordinary JSON is unverified material; malformed JSON and invalid `table_analysis.*` packages fail.

For `table_analysis.v1`, keep the adjacent data copy; JSON and input are each limited to 20 MiB. Calculations are checked and figures regenerated. State genre, length and count scope in the goal; distinguish a target from a hard range.

| `[report]` field | Default / behavior |
| --- | --- |
| `writing_method` | New article; independent document inspection and bounded whole-article corrections use its owner; explicit sectional/adaptive controls use sections; saved methods do not silently switch |
| `template` | auto; source_review/survey/material_report/experiment/reproduction/analysis_report or Markdown template |
| `reviewer`, `max_review_iterations` | Article defaults to no review; explicit llm + document review/scope inspects once at 0 rounds, or proposes at most N complete candidates at N positive rounds; each candidate is independently checked before adoption |
| `document_review`, `review_scope` | Sections: false, section; document scope requires document review |
| `draft_scope` | section; document/auto require document review/scope and full source strategy |
| `outline_strategy` | auto uses a suggested outline; template fixes article-level headings/order; explicit custom templates keep their planned topology; adaptive requires sections |
| `data_tables` | linked; full also includes analysis tables / experiment summary tables |
| `max_section_tokens` | 0/unset no per-call cap; positive bounds section, joint response or article according to method |
| `max_cited_sources` | Unset/0 no cap; positive limits distinct body-cited sources without shrinking the evidence pool; checked in article drafting and final assembly, including captions; excess is not repaired by deleting citations or redrafting |
| `max_document_review_prompt_chars` | 0 no extra complete-request cap; positive refuses oversized review rather than truncating; existing 60,000-character manuscript/retrieval windows remain |
| `figures` | report.figures.enabled=false or mode="off" disables generation; max_figures=0 automatic, positive overall cap includes imports and fails on excess |

### Writing constraints

- `article` drafts the complete body in one request. For independent inspection, set `reviewer="llm"`, `document_review=true` and `review_scope="document"`; zero correction rounds inspect without rewriting. Required headings and citation limits are checked before adoption.
- `sections` keeps its plan and checkpoints. With `draft_scope="auto"`, joint drafting requires a positive length target for every section and a total of at most 2,000 words; otherwise drafting is sectional. Sectional rereads use existing access and tool allowances.
- Auto templates select `source_review`, `survey`, `material_report`, `experiment`, `reproduction` or `analysis_report` according to sources and task. Guided settings may explicitly differ from base defaults. Both table modes retain the portable evidence package; figures must be grounded in supplied evidence.
- Saved writing methods and constraints continue on resume. Inspection and arithmetic checks do not certify scientific correctness.

## Existing data: descriptive analysis

```toml
[task]
goal = "Describe supplied measurements"
kind = "data_analysis"
outputs = ["data_analysis"]

[analysis]
file = "observations.csv"
value_columns = ["score"]
observation_unit = "one run"
group_column = "method"
value_unit = "score points"
```

| `[analysis]` field | Default / constraint |
| --- | --- |
| `file`, `value_columns`, `observation_unit` | Required UTF-8 CSV/TSV/JSON-record path, nonempty numeric-column list, nonempty row meaning |
| `group_column`, `value_unit`, `x_column`, `x_unit`, `attribution`, `paired_baseline` | Empty; unknown units if omitted; attribution is declaration, not acquisition/bibliographic verification |
| `mode`, `plot`, `missing`, `width` | observations, bar, reject, wide; missing omit / width column supported |
| `association`, `series_layout` | none, separate; pearson/shared for coordinates |
| `max_mb`, `max_points`, `max_figures` | Positive adjustable 20 MiB, 10000 positions/cells, 100 SVG pages; overflow fails without sampling |

Ingest checks finite numbers and saves the input bytes and settings. Missing values never become zero; changed transformations require a new task. Analysis-only does not use a model. A report requires a model and shared budget; additional local notes do not authorize search.

| Plot | Required mode / behavior |
| --- | --- |
| bar | Observations: grouped count/mean/sample SD/quartiles; values: preserve values with unique labels |
| box | Observations only; Q1–Q3, median, observed min–max; quartiles interpolate (n−1)p; no coordinate/Pearson options |
| heatmap | Values + unique row labels/common declared quantity/unit; global raw color scale across pages; no normalization/sorting/clustering; omit keeps NA cells, entirely missing matrix fails; no coordinate/Pearson/pairing/shared layout |
| line | Values + numeric x; unique x per group, sorted, complete x required; missing y breaks lines |
| scatter | Values + numeric x; duplicates retained; omit keeps missing-coordinate rows in package but not plot |

Box whiskers show observed min–max, not uncertainty or Tukey cutoffs. Coordinates are not fitted or aggregated. Shared axes require a declared common unit; limits count all series and missing heatmap cells.

`paired_baseline` requires observations/bar or box, baseline and candidates, and same-row matching with a common quantity and unit. It computes candidate minus baseline on complete pairs, retaining counts, mean, sample SD and SE. With at least two independent pairs, ±1 SE is not a confidence or significance claim. Box marginals use each column's nonmissing rows; paired differences use complete pairs.

`pearson` requires line/scatter coordinates and uses jointly present x/y pairs per group and column. Constant axes or fewer than two pairs are undefined; two nonconstant pairs give ±1. No regression, causality or independence is inferred. See the [curves example](../examples/data-curves/README.md).

## Research continuation and decisions

### Continue a research session after preparing execution

For new common tasks, use `start --resume-task SESSION`; use feedback for text changes and [explicit supply](CLI_REFERENCE.md#native-recovery) for missing or replaced contracts. Saved ResearchApplication sessions use `research-session --config research.toml --session-root SESSION`.

For those saved sessions, omitted goals and outputs remain; explicit goal, paper or execution revisions rebuild dependent plans. Measurement reuse requires matching command, schema, protocol, preparation and protected assets. Incompatible task kind or ingestion changes need a new session; report-only changes do not rerun measurement. `research-report` can add a missing report.

Use `continuation.decision_id`, `decision_response` (`accept`, `reject`, `revise`) and `decision_guidance` for pending choices. Delivery revisions also require report settings. Changed terms or inputs invalidate the prior decision; interaction changes cannot be combined with reanalysis or report-only refresh, except when answering a delivery decision.

### Explicit continuation allowances

```toml
[continuation]
authorization_id = "continuation-1"
reason = "Finish the saved goal"
additional_attempts = 2
additional_no_progress = 1
remaining = { total_tokens = 50000, llm_requests = 8 }
```

This table applies to saved sessions using `--session-root`; new common tasks use [native allowance controls](CLI_REFERENCE.md#native-recovery). `remaining` sets future capacity; attempt and no-progress additions increase caps without resetting usage. A stable ID and reason are required: repeating the same ID and terms completes an interrupted authorization without replenishing spent capacity; changed terms require a new ID. Authorization alone does not restart a completed session or refresh its report. Declare process dimensions at creation if later execution is intended.

## Standalone Code-Task Config

```toml
[code_task]
kind = "existing_project"
code_root = "{config_dir}/project"
task_file = "{config_dir}/task.md"

[benchmark]
command = "python main.py"
primary_metric = "accuracy"

[benchmark.metric_directions]
accuracy = "higher"

[workspace]
mode = "copy"

[edit_scope]
allowed_patterns = ["src/**", "main.py"]

[execute]
to_step = "propose-edits"
```

Use `code-task init --config PATH`, then `code-task execute RUN_DIR --config PATH`.

For greenfield tasks, set `kind="greenfield"`, omit `code_root` unless supplying a scaffold, and declare the actual generated-project benchmark.

For external handoff, choose a supported provider and set `agent_mode="handoff"` and `allow_external_agent=true`. Candidate files still pass normal review, validation and benchmark checks.

## Loading Rules

CodeTask init --config loads initialization; execute --config loads execution/model/budget; research-session --code-task-config shares parser. Explicit CLI overrides corresponding values. CodeTask relative paths use command cwd. `{config_dir}` expands code/task/output paths, Python paths, benchmark command, baseline metrics and required paths; resource `${...}` substitutions rejected. Quote spaced command paths. Required paths check existence only.

Most CodeTask sections ignore unknown keys; workspace/ablation reject extras, retired stage selectors fail, budget accepts overrides. Misspellings may have no effect. Historical implementation.max_repair_attempts is not loaded by the current public parser; use supported execute.repair_rounds. Old outer-pipeline configs are read-only history.

## CodeTask Field Reference

### Code-Task Fields

| Section | Fields | Default / behavior |
| --- | --- | --- |
| `[code_task]` | `kind`, `code_root`, `task_file`, `output_root`, `name` | `existing_project` requires source and task; output root defaults to `runs`. `greenfield` requires a task and can use a scaffold; output is `workspace/generated_project`. |
| `[code_task]`, `[safety]` | `max_file_bytes` | Copy/sparse file limit defaults to 2000000 bytes; `code_task` wins, `0` disables it. |
| `[benchmark]` | `command`, `timeout`, `primary_metric`, `metric_directions` | Workspace command string and optional timeout, metric and direction map. |
| `[metrics]` | `primary`, `primary_metric`, `directions`, `metric_directions` | Compatibility aliases; benchmark primary wins, later direction maps and explicit CLI values override earlier ones. |
| `[environment]` | `mode`, `python`, `python_executable`, `required_paths` | `current` or `external`; external requires Python, with `python` taking precedence. Only a leading bare Python is resolved; required paths default to an empty list and check existence only. |
| `[workspace]` | `mode`, `include`, `exclude`, `reuse_source_venv` | Existing projects default to `auto`, greenfield to `empty`; sparse globs are optional and source-venv reuse defaults to `false`. |
| `[edit_scope]` | `allowed_patterns`, `protected_patterns`, `mode` | Allowlist, additional protected globs and a label; the label grants no permission. |
| `[safety]` | `validation_max_file_bytes` | Static scan limit defaults to 500000 bytes; execute can override it. |
| `[implementation]` | `provider`, `agent_mode`, `allow_external_agent` | Provider defaults to `local`; `fake`, `local_llm`, `codex`, `claude_code`, `opencode`, `external_cli` are supported. `model` and `handoff` modes work; `delegated_workspace` is unavailable. External launches require `true`. |
| `[implementation]` | `agent_model`, `agent_binary`, `agent_args`, `agent_timeout_sec` | Optional model, program and arguments; timeout defaults to 600 seconds. Empty model uses the external account default. |
| `[resource]` | `max_runtime_sec`, `max_files`, `max_generated_lines`, `max_memory_mb`, `allow_gpu` | Planning/handoff declarations; runtime, files and lines can supply execute fallbacks. They do not enforce OS limits or install dependencies. |

External candidates pass normal review, validation and benchmark checks. Interpreter selection installs nothing. Static checks allow verified, byte-identical frozen-source syntax defects as warnings unless strict; new, changed or unverified defects fail.

<a id="execute-and-budget"></a>

### Execute And Budget Fields

| Section | Fields | Default / behavior |
| --- | --- | --- |
| `[execute]` | `to_step`, `use_llm` | `run`, `true`; available steps are in the [CLI reference](CLI_REFERENCE.md). |
| `[execute]` | `model` | General selector, then `models.code_task.default`, `models.default`, `llm.model`. |
| `[llm]` | `enabled`, `model` | Compatibility defaults; explicit execute settings win. |
| `[models.code_task]` | `planner`, `writer`, `reviewer`, `editor`, `repair`, `summarizer` | Per-role selectors; otherwise use the general connection. |
| `[execute]` | `timeout_sec`, `timeout` | First `timeout_sec`, then `timeout`, `benchmark.timeout`, `resource.max_runtime_sec`, finally 60 seconds. |
| `[execute]` | `skip_validation`, `strict_validation`, `validation_max_file_bytes` | Defaults `false`, `false`, 500000 bytes. |
| `[execute]` | `stream_benchmark_output` | Default `off`/`false`; `line` forwards lines, `auto`/`true` also handles carriage returns, and `summary` shows a tail. |
| `[execute]` | `baseline_policy`, `baseline_metrics_file` | Default `auto`; `run`, `skip`, `none` and `provided` choose baseline treatment. Provided metrics are not remeasured. |
| `[execute]` | `apply_proposed_edits`, `allow_large_edits`, `allow_planning_fallback` | Default `false`; explicitly permit application, larger edits or eligible fallback. |
| `[execute]` | `planning_mode`, `planning_review_rounds` | `compact`, `0`; `tool_agent` is deprecated and retired review rounds grant no calls. |
| `[execute]` | `llm_retry_attempts`, `repair_rounds` | `3` completed-response attempts including the first; `0` technical repair rounds. Transport retries are separate. |
| `[execute]` | `max_files`, `max_source_chars_per_file`, `max_generated_lines` | `8`, `4000`, `1600`; resource settings can supply file/line fallbacks. Oversized plans fail. |
| `[execute.ablation]` | `review_gate` | `strict`; `runtime` blocks safety and executability issues while permitting packaging/report gaps. |
| `[execute]` | `budget_profile`, `max_batches`, `cost_cap_usd` | Override the corresponding budget settings. |
| `[budget]` | `profile`, `max_batches`, `cost_cap_usd` | `normal` profile; optional batch and estimated-cost caps. |
| `[budget]`, `[budget.normal]`, `[budget.large]` | `max_files`, `max_edits`, `max_old_chars`, `max_new_chars`, `max_total_edit_chars`, `max_proposal_chars` | Numeric edit and proposal limits; the selected profile table overrides top-level values. |

Connection retries, research allowances, process limits, edit sizes and correction rounds are separate. Normal permits up to four allowed files, 12000 total edit characters. Large needs explicit approval and absolute bounds (32000 per source block). Provided JSON metrics: flat map, metrics or metric_values wrapper. Process success differs from objective improvement.

#### Planning failures and repair

Complete malformed planning responses can receive bounded correction or explicitly allowed fallback; provider failures and incomplete responses cannot. Offline greenfield fallback is a four-file development scaffold, not a completed implementation. Technical repair requires review and stays within accepted protocol, scope and process limits.

#### Historical planning

Saved accepted plans retain their files and usage. Retired stage routes are rejected; generated projects and imported accepted plans skip replanning.

## Workspace Mode Variants

Research preparation retains the configured copy limits, sparse rules and source-venv policy. Source-venv reuse cannot be combined with a managed task environment. Inventories skip symbolic links and Windows reparse entries; workspace isolation is not an OS sandbox.

### `auto`

Existing default: detached worktree where possible, guarded copy with recorded reason otherwise. Research falls back for uncommitted source. Repository-subdirectory projects use matching worktree subdirectory.

### `copy`

Copies eligible working-tree state, including uncommitted source; exclusions/size limits may omit files.

### `git_worktree`

Explicit committed HEAD, local repository with a commit; no remote required. Fails instead of falling back; does not include uncommitted source.

### `sparse_copy`

Standalone include/exclude globs plus built-in Git/venv/runs/cache/build/data/model/secret exclusions. May omit runtime dependencies. Research preparation also supports sparse_copy, but declared project data cannot bypass copy protection through sparse mode. Greenfield TOML defaults empty; copy/sparse scaffolds need code_root. CLI --workspace-mode does not accept empty.

## Edit Scope Behavior

Only workspace files are edited. Empty allowlist allows normalized non-protected paths; otherwise allowed match and no protected match required. Built-in tests/benchmarks/.env/secrets/credentials stay protected; custom patterns add protection. Batch/work-plan targets narrow scope further. Context/planning/proposal/repair enforce scope; apply rechecks paths, unique exact old text and budgets. Empty old creates an absent allowed file, never replaces existing; creations share snapshots/rollback.

## CodeTask In A Research Session

Set `execution.code_task_config` or `--code-task-config` to the CodeTask file. CodeTask owns editing and validation; research owns measurement and reporting. Reproduction adapters may also declare a separate measurement command. Explicit edit limits still apply during research and recovery.

Preparation, implementation and measurement share the task's permissions and process limits. A requested report must use the accepted implementation evidence and current manuscript. Greenfield scope and budgets apply to the whole candidate. For checkout filters, attributes, submodules or newline transformations, select `copy` or retain the expert worktree protocol.

## Trusted local packages

Select only packages you trust. V1 supports instruction packages and Python tools
that declare `read_only` or `plan`; Python runs with the host process's OS
privileges. There is no OS sandbox.

```toml
[task]
extensions = ["../my-skill"]
```

Use this in native task TOML with `simple-ar start --task-config path/to/task.toml`.
Each entry is a package directory containing `extension.toml` or a descriptor file,
resolved relative to the task configuration. Selection is explicit: no automatic
discovery, download or installation. Instructions-only and tools-only packages work.

| Author declaration | Meaning |
| --- | --- |
| `[extension]` `id`, `version`, `description`, `scope` | Required nonempty metadata; ID is a unique namespace matching `[a-z][a-z0-9_]{0,23}`. |
| `instructions`, `resources` | Optional package-relative instruction file and list of companion files; files must stay inside the package. |
| `[[tools]]` `name`, `description` | Required tool name and description; name follows the same ID pattern, public name is `id__name`, and `instructions` is reserved. |
| `implementation`, `function` | Explicit standalone `.py` file and Python function identifier; package-relative imports are unsupported. |
| `permission_level` | `read_only` or `plan`; authors must keep tools deterministic and side-effect-free. |
| `input_schema` | Required object schema, validated as JSON Schema Draft 2020-12. Local `$defs`/`$ref` work; remote and `file:` reference retrieval is disabled. |
| `completion` | Optional `{ kind = "delivery", field = "handoff", closing = true }`; see below. |

A tool implements `function(arguments, services) -> dict` and returns a JSON object.
`services["call_tool"](name, arguments)` supplies policy-checked host read/plan
operations on authorized mounts. It grants no arbitrary write, process, secret or
network service. Direct Python can still read/write files, launch processes or
access the network with host privileges. Instruction bodies enter context only when
`id__instructions` is called; loading a tool schema does not load the body.

For optional `completion`, `kind` defaults to `delivery` and `field` defaults to
empty. A nonempty field must be in the input schema and contain the delivery object
(`summary`, optional `artifacts` and `unresolved`); return it under the same key.
With an empty field, the entire arguments and result are the object. `kind="question"`
uses the whole `{question = "..."}` object. `closing=true` allows an already-visible
tool in the last decision only when it needs no further model work; it grants no
permission or budget.

Resume and execution require the original descriptor, code, instructions and declared
resources at their saved paths and hashes. Missing or changed files block use; restore
the original package. Undeclared dependencies are not covered by identity checks;
module globals do not persist across resume.

See the example [descriptor](../examples/local_extension/extension.toml),
[instructions](../examples/local_extension/SKILL.md) and
[Python tool](../examples/local_extension/notes.py); loader details are in
[`extensions.py`](../src/simple_ar/tools/extensions.py).

## Python task creation

Use `create_research_task` for structured research, writing, tables and declared execution. It shares CLI input validation and returns a `ToolApplication`; creating it makes no model or process call. Use `ToolApplication.create` for direct native integrations.

### Create, run and reload

```python
from pathlib import Path
from simple_ar.app.research_intake import create_research_task
from simple_ar.app.tool_application import ToolApplication
from simple_ar.research.workflow_contracts import ResearchBrief

app = create_research_task(
    ResearchBrief(request_text="Explain the evidence and its limits.",
        requested_outputs=("summary",),
        asset_requests=({"locator": "notes.md", "role": "material"},)),
    root=Path("runs/evidence-note"), model="route:default",
)
app.run(progress=print)
restored = ToolApplication.load(Path("runs/evidence-note"))
```

Paths resolve from `ResearchApplicationServices.input_base_dir`, not the output task directory. Services supply research settings and finite `budget_limits`; explicit zero allowances stay zero. Omit `model` for caller-directed actions and later explicit supply. A requested report is a delivery obligation, not permission to call a model. Source aliases, environments and verified external baselines use the same input compilation as CLI creation.

```python
from simple_ar.app.research_application import ResearchApplicationServices

services = ResearchApplicationServices(input_base_dir=Path.cwd(), config={
    "execution": {"command": ["python", "experiment.py"], "cwd": ".", "timeout_sec": 120,
        "result_schema": {"primary_metric": "score", "required_metrics": ["score"]}}},
    budget_limits={"process_invocations": 1, "process_wall_seconds": 120})
app = create_research_task(ResearchBrief(request_text="Measure the supplied program.",
    requested_outputs=("experiments",)), root=Path("runs/measurement"), services=services)
```

Put declared commands/conditions in `services.config["execution"]`. Optional `code_task` retains CodeTask defaults, edit boundaries and independent validation. Execution requires a separately approved proposal; `execution_confirmation=True` additionally requires acceptance of the frozen protocol. Omitted/empty CodeTask `allowed_patterns` retains the default protected source scope; use explicit globs to narrow it.

When research is required, omitted comparison controls leave design open rather than imply `baseline_policy="skip"`. An accepted design binds unchanged-source baselines and implemented candidates separately; it cannot widen scope, replace the environment or invent a baseline for an empty project. Without a model, an experiment task can retain materials and pause for [explicit supply](CLI_REFERENCE.md#native-recovery), with or without a declared command. Collection alone is not completion. First model binding requires positive new request/token allowances on the original ledger.

### Native controls and compatibility

Saved tasks retain their recorded controls. These settings apply to Python integrations:

| API setting | Behavior |
| --- | --- |
| `delivery_files=["**/*.md", "**/*.pdf"]` | Explicit relative patterns require nonempty workspace outputs for complete delivery; presence is not quality. |
| `tool_contracts="on_demand"` | Creation default. Common tools are available first; optional contracts use `load_tools`. `full` exposes all allowed contracts. Loading grants no permission. |
| `context_protocol="auto"` | Creation default; new operation tasks use `working.v4` within the shared input budget. `update_work_plan.notes` is retained if omitted and cleared by `[]`. |
| `context_protocol="working.v1"`, `"conversation.v1"`, `None` | Compatibility choices retain their saved selection and recovery rules. |
| `control_protocol="operations.v1"` | New tasks combine permitted operations in one loop; use `load` and `supply` for saved tasks. |
| `completion_policy="available_operations.v1"` | Operations remain available within permissions and resource limits; exhaustion preserves work and pauses. |
| `report={"status":"canonical","manuscript":"report.md","formats":["markdown","pdf"]}` | Declares the editable manuscript and output formats; `delivery_review_protocol=task.v1` independently reviews the current work and inventory. Publications must match the manuscript. |
| Delivery `limitations`, `unresolved` | Record uncertainty and unfinished required work respectively; unresolved work makes delivery partial. |
| Delivery `summary_source={"path":"report.md","offset_bytes":0,"max_bytes":1024}` | Uses a byte-bound UTF-8 window of a delivered file as the summary; invalid or changed sources fail. |
| `create_research_task(..., allow_vision=True)` | Default `False`; a named vision route and shared `services.budget_limits` are required. It does not grant image generation or source network access. |
| `supply(..., replace_existing=True)` | Explicitly replaces `delivery_files`, `tool_contracts` or `completion_policy`; unrelated supply keeps saved policy and ledger. |

`inspect_artifacts` accepts decodable text; use `inspect_images` for images. A changed target requires a new inspection. Authorized local figures may be referenced across directories; publication binds their bytes, and changed figures require new approval. A frozen `report_requirement` retains its manuscript, outline and citation constraints; `target_words` is a target.

### Historical writing contracts, output binding and retained work

These options apply to explicit compatibility integrations and saved professional tasks. New `operations.v1` tasks edit, inspect and export directly without `report_protocol`, `professional_inputs` or `update_work_plan.report_intent`.

| Operation / field | Behavior |
| --- | --- |
| `compose_document.professional_review={"max_revision_rounds":1}` | Independent inspection and at most one correction; zero inspects without correction. Frozen controls take precedence. |
| `compose_document.prior_result` | Continue a checked but unadopted revision with its evidence and inspection; unresolved required findings prevent accepted publication. |
| `compose_document.adopt_draft=true` | Check and adopt `prior_draft` without a drafting call; requires article writing and independent professional review. `reuse_prior_inputs` requires an intact same-task producer. |
| `run_process.inputs`, `run_process.outputs` | Optional bindings, up to 64 each and 64 MiB per binding. Approval binds inputs; successful execution records outputs. Changed bindings require a new invocation. |
| `app.run()`, `app.result_snapshot()` | Expose `retained_work`, including intermediate files and continuation guidance; retained work is not an accepted delivery. |

Saved writing contracts keep their review, evidence and budget rules. Existing figures, feedback and attachments remain evidence inputs; inspection does not certify scientific correctness.

### Trusted services, snapshots and legacy APIs

Services may inject a text client, independent reviewer and `SearchProviderRegistry`. Clients implement `with_budget` and `ask_json` or `chat_turn`; reload with `ToolApplication.load(path, services=services)`. Persistence saves identities, not credentials or executable objects. Missing services fail rather than falling back. Custom implementations are caller-trusted and not sandboxed.

`snapshot_sources=True` saves selected regular files with hashes, each bounded by `max_snapshot_bytes` (default 16 MiB). Legacy v1 import retains parent links and records historical budget as `unknown_not_imported`. `create_session` and `ResearchApplication.create` remain compatibility APIs with their saved `view` / `advance` contracts.
