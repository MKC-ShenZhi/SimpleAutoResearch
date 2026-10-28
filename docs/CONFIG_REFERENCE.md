# Configuration Reference

[中文版本](CONFIG_REFERENCE_zh.md) · [Usage](USAGE.md) · [CLI reference](CLI_REFERENCE.md)

<a id="files-precedence-and-effective-timing"></a>

## Configuration file navigation

Choose the file you want to edit. Sections below follow the same order as this table. Most users only need credentials and a model connection; use task TOML when you want to set materials, permissions or budgets before starting. Advanced files apply only to their respective entry points.

| File / input | Purpose and location | When edits apply |
| --- | --- | --- |
| [`.env` / environment](#environment-file) | API keys, model catalog location and deployment settings; `.env` lives in the command's current directory. | Next command or client read; existing environment variables take precedence. |
| [`models.toml`](#model-file) | Reusable model connections and routes; stored in the user configuration directory or an explicitly selected location. | New connection selection; switching a saved task requires an explicit action. |
| [`task.toml`](#task-file) | Goal, materials, permissions and resource limits for ordinary `start` tasks. | New task creation; does not modify a running task. |
| [`research.toml` (advanced)](#research-file) | Structured inputs for `research-session --config`, including search, execution, analysis and writing. | At creation; saved tasks follow continuation rules. |
| [`code-task.toml` (advanced)](#code-file) | Project, workspace, edit scope and execution settings for standalone code tasks; may also be referenced by research settings. | Workspace initialization or the next execution, depending on the field. |
| [`extension.toml` (advanced)](#extension-file) | Local extension package manifest; used only when explicitly loading an extension. | When selected; saved tasks retain version constraints. |
| [Saved task directory](#saved-task) | System-managed progress, artifacts, usage and recovery records. | **Not editable configuration**; use continuation commands. |

`task.toml`, `research.toml` and `code-task.toml` are example filenames; choose your own names. They belong to different entry points, so do not merge their tables into one universal task file. State language, length and deliverables in the task goal.

<a id="environment-file"></a>

<a id="credentials-and-environment"></a>

## `.env`: credentials and environment

<a id="global-env"></a>

Keep secrets in environment variables or a private `.env`, never shareable TOML. `start`, `models` and the model client read `.env` from the command’s current directory; existing environment variables take precedence. Storing the model catalog elsewhere does not relocate `.env`. Each setting applies when its consumer next reads the environment; none resets saved usage. Catalog profiles supply their own model/transport settings rather than inheriting legacy overrides.

| Field | Meaning | Type and default | Change effect |
| --- | --- | --- | --- |
| `SIMPLE_AR_MODELS_CONFIG` | Shared model catalog | Path string; user catalog if absent | Next catalog load |
| Variable named by profile `api_key_env` | Connection credential | Required nonempty secret string | Next client; rotate value under same name |
| `SIMPLE_AR_LLM_MAX_WORKERS` | Model concurrency ceiling | Positive integer; requested concurrency if absent | Next parallel operation |
| `OPENALEX_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY` | Optional connector keys | Strings; anonymous access if absent, with its quotas | Next connector load |
| `TAVILY_API_KEY` | `web` search / `tavily_basic` extraction key | Required string for these services | Next connector load |
| `SIMPLE_AR_DOCUMENT_ROUTE_HOSTS` | Exact linked-document routing hosts | Comma-separated string; unset | Next document download |
| `SIMPLE_AR_DOCUMENT_PROXY_ENV`, `SIMPLE_AR_DOCUMENT_CA_BUNDLE` | Proxy variable name / trusted CA path | Optional strings | Next routed download |
| `SIMPLE_AR_RESEARCH_INDEX_ROOT` | Historical index cleanup location | Path `.simple_ar_cache/research_index` | Explicit cleanup; not search backend selection |

Linked-document routing uses exact hosts and TLS verification; it does not configure model/search/PDF/ZIP transport. Without explicit routing that downloader ignores ambient proxies. `SIMPLE_AR_OUTPUT_DIR` is supplied per process by the application, not a global setting to edit.

### Environment-only compatibility connection

Skip this section when using `models.toml`: these variables configure the legacy connection without a catalog.
Prefer named connections for new setup, so model and timeout settings have one place to maintain.

| Field | Meaning | Type and default | Change effect |
| --- | --- | --- | --- |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `SIMPLE_AR_MODEL` | Legacy connection without a catalog | Strings; key required, URL unset, model `gpt-4o-mini` | Next legacy client |
| `SIMPLE_AR_LLM_API`, `SIMPLE_AR_LLM_BACKEND` | Legacy API/transport | Strings `responses`, `openai`; API also `chat`/`auto`, backend also `litellm` | Next legacy client; native decisions need Chat |
| `SIMPLE_AR_LLM_TIMEOUT_SEC`, `SIMPLE_AR_MAX_OUTPUT_TOKENS` | Legacy request timeout/output cap | Positive number `180` seconds; optional positive integer cap | Next legacy client |
| `SIMPLE_AR_LLM_RETRY_ATTEMPTS` | Legacy total physical attempts | Integer ≥1; `3` | Next legacy client |
| `SIMPLE_AR_LLM_RETRY_BASE_DELAY_SEC`, `SIMPLE_AR_LLM_RETRY_MAX_DELAY_SEC` | Legacy retry delays | Positive numbers `1`, `12` seconds | Next legacy client |
| `SIMPLE_AR_JSON_RESPONSE_FORMAT`, `SIMPLE_AR_CHAT_TOKEN_LIMIT_PARAM` | Legacy JSON/token-parameter policy | Strings `auto`, `auto`; profile choices below | Next legacy client |
| `SIMPLE_AR_LLM_REASONING_EFFORT`, `SIMPLE_AR_LLM_THINKING` | Legacy reasoning controls | Strings, empty; profile choices below | Next legacy client |
| `SIMPLE_AR_LLM_REASONING_OUTPUT_TOKENS`, `SIMPLE_AR_LLM_STREAM` | Reasoning cap/streaming | Optional positive integer; boolean text false | Next legacy client |
| `SIMPLE_AR_INPUT_PRICE_PER_1M`, `SIMPLE_AR_OUTPUT_PRICE_PER_1M` | Legacy cost estimates | Optional numbers | Next legacy client; not spending guarantees |

<a id="model-file"></a>

<a id="named-model-connections"></a>

## `models.toml`: connections and routes

The lookup order is an explicit path where supported → `SIMPLE_AR_MODELS_CONFIG` → `~/.config/simple-ar/models.toml`. Only one catalog is loaded; project catalogs are not merged or discovered automatically. Continuation may use a saved absolute location before the user default. Prefer an absolute path for shared configuration.

Save this in the selected `models.toml`, replace the endpoint/model, and set `RESEARCH_API_KEY` privately:

```toml
version = 1

[profiles.daily]
api = "openai_chat"
base_url = "https://provider.example/v1"
model = "provider-model-name"
api_key_env = "RESEARCH_API_KEY"
capabilities = ["text", "code"]

[routes]
default = "daily"
text = "daily"
code = "daily"
```

`simple-ar models` checks settings and credential presence without API calls. `models --config PATH` inspects that file without selecting it for subsequent commands. See [catalog examples](../examples/model-profiles/models.toml).

| Field | Meaning | Type and default | Change effect |
| --- | --- | --- | --- |
| `version` | Catalog format | Integer `1` | Next load |
| `profiles.NAME` | Named connection | Table; at least one; nonempty names without `:` | New selection; saved identity checked |
| `routes.default`, `text`, `code`, `writing`, `vision`, `image` | Purpose → profile, under `[routes]` | Optional strings referencing existing profiles | New route selection; saved bindings retained |

Fields below belong to `[profiles.NAME]`. **Operational** settings can be retuned when reloading a saved connection. **Identity** changes require new work or an explicit supported connection change; they cannot silently mutate an existing task.

| Field | Meaning | Type and default | Change effect |
| --- | --- | --- | --- |
| `api` | Protocol | Required `openai_chat`, `openai_responses`, `openai_images`, `cctq_images_async` | Identity |
| `base_url` | Endpoint, no userinfo/query/fragment | Required HTTP(S) string | Identity |
| `model` | Provider model ID | Required nonempty string | Identity |
| `api_key_env` | Credential variable name, not key value | Required environment-variable name | Identity; secret value can rotate |
| `capabilities` | Declared uses | Required nonempty list: `text`, `code`, `vision`, `image_generate`, `image_edit` | Identity; not a provider probe |
| `proxy_env` | Variable containing HTTP(S) proxy URL without userinfo/query/fragment | String `""` | Identity; value resolved privately |
| `http2` | HTTP/2 | Boolean false; requires installed http2 extra | Identity |
| `stream` | Chat streaming | Boolean false; Responses requires false | Operational |
| `request_timeout_sec` | Request timeout including finite SDK Chat stream | Positive number `180` seconds | Operational |
| `connect_timeout_sec` | SDK text TCP/TLS timeout | Optional finite positive number; default connection cap 20 seconds, bounded by request timeout | Operational |
| `max_output_tokens` | Output cap; provider limits still apply when omitted | Optional positive integer | Operational; explicit call cap can override |
| `context_input_tokens` | Native input working-set target | Positive integer `24000` | Operational; not provider context size |
| `retry_attempts`, `connect_retry_attempts` | Total attempts / pre-dispatch connection ceiling | Integers ≥1; `3`, `1` | Operational; attempts share budget |
| `retry_base_delay_sec`, `retry_max_delay_sec` | Retry delays | Positive numbers `1`, `12` seconds | Operational |
| `native_response_corrections` | One settled native Chat truncation correction | Strict integer `0`/`1`, default `0` | Identity; finite output cap/shared budget required |
| `reasoning_effort` | Provider effort | String empty, `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`; empty default | Identity |
| `thinking_mode` | Provider thinking | String empty, `enabled`, `disabled`, `auto`; empty default | Identity |
| `reasoning_output_tokens` | Active-reasoning fallback output cap | Optional positive integer | Identity; explicit caps win |
| `json_response_format` | JSON parameter policy | `auto`/`off`/`json_object`; auto default | Identity; off retains local validation |
| `chat_token_limit_param` | Chat token-limit parameter | `auto`/`max_tokens`/`max_completion_tokens`; auto default | Identity |
| `input_price_per_million`, `output_price_per_million` | Cost estimates | Optional nonnegative numbers | Identity; not provider spending limits |
| `image_size` | Image dimensions | Optional `auto`, `256x256`, `512x512`, `1024x1024`, `1024x1536`, `1536x1024`, `1792x1024`, `1024x1792` | Identity; image APIs only |
| `image_quality` | Image quality | Optional `auto`, `standard`, `hd`, `low`, `medium`, `high` | Identity; image APIs only |

Fields/types are strict. Image profiles accept image capabilities and reject text-generation settings; text profiles reject image-generation capabilities and size/quality. Chat disabled thinking requires empty effort or `none`. Provider support must match declared settings.

| Selector / consumer | Resolution |
| --- | --- |
| `env` | Catalog text route → default; legacy environment if no catalog |
| `profile:NAME` | Complete named connection |
| `route:NAME` | Named route → compatible default |
| Raw model ID with catalog | Must match the model already selected for the purpose; no profile search |
| Nested CodeTask, no explicit selector | Code → default |
| Native writing | Explicit writing route, otherwise task connection |
| Vision / image | Compatible configured route; no text-client substitution |

Missing names/credentials/capabilities fail without provider fallback. Native decisions and focused work require Chat; document composition can use Responses on writing. Vision inspection requires an explicit profile output cap. Saved connection identities/routes remain binding; operational tuning and key rotation do not release unknown usage reservations. Standalone CodeTask does not retrospectively pin old connections.

<a id="task-file"></a>

<a id="editable-native-task-inputs"></a>

## `task.toml`: ordinary task settings

<a id="guided-setup"></a>
<a id="native-task-toml-fields"></a>
<a id="v210-task-declarations-development-branch"></a>

Priority is defaults → TOML → explicit CLI arguments; CLI lists replace file lists. Paths in this file are relative to its directory; CLI paths are relative to the command’s current directory.

`simple-ar start --write-task-config task.toml` exports commented inputs without creating a task. Review, then run `simple-ar start --task-config task.toml`. **Using `--task-config` creates a new task; editing this file does not update a task already running.** Use `--resume-task` to continue existing work; see the [CLI reference](CLI_REFERENCE.md) for allowance changes.

```toml
[task]
request = "Compare the supplied evidence and explain its limitations."
source = ["notes.md"]
model = "env"
max_tokens = 1000000 # Example: give this new task a one-million-token allowance
```

Place `notes.md` beside the file. All fields below belong to `[task]`; paths use that file's directory. “New task” means next creation from this input, never retroactive application.

| Field | Meaning | Type and default | Change effect |
| --- | --- | --- | --- |
| `request` | Goal, language, length and deliverables | String; required unless CLI supplies goal | New task |
| `model` | Connection selector | String `env` | New task |
| `source` | Read-only file/directory mounts | Path string list `[]` | New task |
| `kind` | Outcome preference | Optional `survey`, `bug_fix`, `reproduction`, `writing`, `data_analysis`, `figure`, `code` | New task; not fixed pipeline |
| `project` | Existing read-only project | Optional path string | New task |
| `document` | Original sources | Path string list `[]` | New task |
| `material` | Notes/drafts/result packages | Path string list `[]` | New task; not verified originals |
| `data_file`, `data_path` | Main table / additional read-only data | Optional path string / path string list `[]` | New task; no inferred columns |
| `output_root` | Output parent | Path string `runs/assistant` | New task location |
| `project_python` | Project Python entry, preserving venv entry path | Optional path string | New task environment |
| `allow_execution` | Permit process proposals | Boolean false | New task; each command still needs approval |
| `allow_network` | Public-source acquisition | Boolean false | New task; not installation authorization |
| `allow_vision` | Send mounted images to vision route | Boolean false | New task; separate from generation |
| `max_turns`, `max_tokens` | Shared model request/token allowances | Integers `30`, `600000` | New ledger limits |
| `max_processes` | Cumulative process invocations, including backend processes | Nonnegative integer; max_turns × 10 | New ledger limit; zero disables |
| `max_process_seconds` | Cumulative process wall time | Nonnegative finite number; max_turns × 300 seconds | New ledger limit; not command timeout |
| `max_images` | Paid image generation/edit submissions | Integer `0` | New ledger limit |
| `from_session` | Saved task supplying delivered materials | Optional path string | New work, not resume |
| `reuse` | Registered result groups, e.g. artifacts | Distinct nonempty string list `[]`; pair with from_session | Byte-checked reuse, no inherited permissions/usage |
| `extensions` | Explicit trusted local packages | Path string list `[]` | New selection; [details](#trusted-local-packages) |

Unknown fields/types fail. The file must contain exactly one `[task]` table, at most 64 KiB. It has no arbitrary keys for model-selected search methods or task strategy.

<a id="research-file"></a>

<a id="research-toml-sections-and-defaults"></a>

## `research.toml`: structured research settings (advanced)

Priority is defaults → TOML → explicit CLI arguments; CLI lists replace file lists. File paths are relative to the TOML directory. Command arguments remain literal; resource paths do not provide general environment-variable expansion.

Sections: [task and materials](#task-materials-and-resources), [search](#search-and-reading), [execution](#execution-and-report), [analysis](#existing-data-descriptive-analysis), [writing](#writing-from-supplied-material), and [historical continuation](#research-continuation-and-decisions).

Structured settings use `simple-ar research-session --config research.toml`; these are not extra tables accepted by native `--task-config`.

```toml
[task]
goal = "Compare the supplied paper's evidence and limitations."
kind = "survey"
outputs = ["report"]

[model]
name = "env"

[research]
materials_only = true

[assets]
papers = ["paper.md"]
```

This assumes a configured model and adjacent `paper.md`. Unknown sections/fields/types fail. Tables below describe creation inputs; use [continuation](#research-continuation-and-decisions) for saved work.

### Task, materials and resources

| Field | Meaning | Type and default | Change effect |
| --- | --- | --- | --- |
| `task.goal` | Goal/deliverables | Required string | New task |
| `task.kind` | Structured contract | `auto`; also survey, bug_fix, measurement, reproduction, writing, data_analysis | New task |
| `task.outputs` | Requested outputs | Optional list: summary, report, experiments, bug_fix, code_task, data_analysis | New task; do not combine with CLI report toggles |
| `task.output_root` | Output parent | Path string `runs/research-session` | New location |
| `task.selected_idea_id` | Existing grounded candidate ID | Optional string | New selection |
| `model.name` | Connection selector | String `env` for TOML; empty for deterministic work | New task |
| `model.feasibility_review_model` | Same-provider feasibility reviewer | Optional string | New task; saved choice retained |
| `model.max_output_tokens` | Explicit model output cap | Optional positive integer | Calls using this input |
| `budget.total_tokens`, `budget.llm_requests` | Shared model allowances | Positive integers; model-backed common creation 600000 / 30; no-model omissions become zero | New ledger |
| `budget.process_invocations`, `budget.process_wall_seconds` | Cumulative execution limits | Nonnegative integers; task-derived if absent | New ledger; declare finite intended capacity |
| `assets.papers` | Local original sources | Path string list `[]` | New ingestion |
| `assets.materials` | Notes/drafts/results | Path string list `[]` | New ingestion; not original sources |
| `assets.data` | Experimental data inputs | Path string list `[]` | New task; table analysis uses analysis.file |

`survey` is literature work; `writing` requires local inputs and report-only output. `data_analysis` allows an optional report without search, CodeTask or execution. `measurement` requires declared commands and experiments (optional summary/report), without CodeTask. `bug_fix` requires existing-project CodeTask and bug_fix output (optional report). Explicit code-delivery contracts can use CodeTask without experiments. These structured restrictions are separate from native outcome preferences.

### Search and reading

<a id="research-inputs-and-behavior"></a>

Fields belong to `[research]`. Search snippets/metadata are discovery material, not verified originals.

| Field | Meaning | Type and default | Change effect |
| --- | --- | --- | --- |
| `providers` | Search providers | Optional list; common search defaults openalex, semantic_scholar, arxiv; also local_files, web | New search |
| `queries` | Explicit search seeds | String list `[]` | New search |
| `max_results`, `max_chunks`, `idea_limit` | Retrieval/ingestion/candidate ceilings | Positive integers 10 / 300 / 3 | New source work |
| `max_pdf_pages` | Local extraction page ceiling | Optional positive integer; all pages if absent | New extraction; frozen ingestion needs new task |
| `read_max_shortlist` | Reading shortlist ceiling | Optional positive integer; all up to 24 | New reading; excess explicit sources fail |
| `cache_dir` | Shared full-text cache | Optional path string | New cache choice; not safe resume override |
| `use_fulltext`, `allow_pdf_download`, `keep_raw_pdf` | Full-text use / remote PDF permission / raw retention | Booleans false | New source work |
| `materials_only` | Disable online search | Boolean false; writing local regardless | New task; model reading still possible |
| `max_fulltext_documents`, `max_pdf_mb` | Remote count / per-PDF MiB ceiling | Optional positive integers; no explicit caps if absent; guided full-text setup 6 / 20 | New acquisition |
| `web_extract_backend` | Extraction service | direct / tavily_basic; direct default | New extraction; Tavily key required |
| `max_iterations` | Additional source-collection bound | Nonnegative integer 1 | New task; historical policy retained |
| `interaction` | Confirmation policy | checkpoints / assisted / autonomous; checkpoints default | New task; saved changes use continuation |

### Execution and report

Fields belong to `[execution]`. Literature-only work needs no command/CodeTask. Commands are literal argv, without shell interpretation. Permission, command approval and capacity remain separate requirements.

| Field | Meaning | Type and default | Change effect |
| --- | --- | --- | --- |
| `command` | Measurement command | Optional nonempty string list | New declaration |
| `cwd` | Existing execution directory | Path string; CLI default `.`; file value relative to TOML | New declaration; argv stays literal |
| `timeout_sec` | Per-command timeout | Optional positive integer seconds | New declaration |
| `code_task_config` | CodeTask preparation config | Optional TOML path | New preparation; usually exclusive with command except adapters |
| `primary_metric`, `metrics` | Primary/required metrics | Optional string / string list `[]` | New schema |
| `metric_directions` | Metric direction | String list, e.g. `["accuracy=higher"]`; higher/lower/resource/ignore | New schema |
| `baseline_policy` | Control treatment | run / skip / reuse; inferred run for pairs/explicit baseline, otherwise skip | New contract |
| `baseline_ref` | Passed same-condition result file(s) | Optional path string or ordered list, relative to input base | New verified reuse |
| `baseline` | Control command/overrides | Optional table; main command used if control command absent | New comparison |
| `pairs` | Explicit baseline/candidate conditions | Table array; unique integer seed, argv baseline_command and candidate_command | New comparison; exclusive with single command |
| `seeds`, `seed_count`, `seed_flag` | Compact command expansion | Optional distinct integer list or positive count, with explicit string flag | New conditions; do not mix with pairs |
| `protocol` | Scientific conditions | Optional table: contract_id, hypothesis, dataset_refs, split_spec, metric_specs, comparison_conditions, protected_assets; reproduction also dataset, expected_outcome | New protocol |
| `initial_files` | Required absent Python files with explicit CodeTask | Nonempty exact path list | New scoped creation; not stubs |
| `output_files` | Output name → relative file | String map `{}` | New registration |
| `metric_sources` | Metric → JSON/CSV selector | Table `{}` | New metric extraction |
| `environment` | Preparation/check settings | Optional table; omit for ordinary current execution | New preparation |

Protected assets need unique asset_id and file path, relative to execution cwd or absolute. Reuse checks command, data, split, schema, conditions, preparation and protected bytes. Seed metadata alone does not infer commands or authorize extra runs.

Fixed-protocol reproduction requires kind reproduction, experiments (optional report), local papers, materials_only=true, one command, finite timeout, baseline_policy=skip, and protocol hypothesis/dataset/expected_outcome. Optional CodeTask adapters require an independently passing benchmark with different resolved argv from measurement. Initial files must be absent and explicitly allowed. See [example](../examples/conformal_reproduction/README.md).

Fields under `[execution.environment]`:

| Field | Meaning | Type and default | Change effect |
| --- | --- | --- | --- |
| `mode` | Task venv or current-environment check | Required venv / current; current requires check_command | New preparation |
| `requirements` | Project-relative requirement files | String list `[]`; must stay in execution project | New venv installation |
| `install_project` | Install declared project package | Boolean false; venv only | New installation with authorization |
| `python_executable` | Venv base interpreter | String; runtime interpreter default; forbidden for current | New venv |
| `timeout_sec` | Per-step timeout | Positive integer 300 seconds | New preparation |
| `check_command` | Short independent check | Optional argv list; required for current | New check, distinct from measurement |

Preparation shares process capacity. Venv measurement requires leading bare Python; explicit executables are not silently replaced. CodeTask adapters with managed installation need current CodeTask Python, no source-venv reuse, and their own validator instead of check_command.

For a program writing runtime.json under `SIMPLE_AR_OUTPUT_DIR`:

```toml
[execution.output_files]
runtime = "runtime.json"

[execution.metric_sources]
elapsed = { output = "runtime", path = ["elapsed_seconds"] }
```

JSON selectors use explicit key/index paths; CSV/TSV uses output, column and optional match (up to four exact scalar conditions). Selectors must identify one finite value. Registered UTF-8 regular files have relative POSIX paths under the process output directory and a 2 MiB limit each. Literal `{output_dir}` argv binds that directory. File metrics cannot rescue a failed process or conflict with stdout metrics.


<a id="existing-data-descriptive-analysis"></a>

### Existing data: descriptive analysis

Use `[analysis]`, kind data_analysis, and outputs data_analysis (optionally report).

```toml
[task]
goal = "Describe supplied measurements."
kind = "data_analysis"
outputs = ["data_analysis"]

[analysis]
file = "observations.csv"
value_columns = ["score"]
observation_unit = "one run"
```

Analysis-only uses no model; a report needs model resources. Ingestion freezes bytes/settings; editing the original table does not replace saved analysis.

| Field | Meaning | Type and default | Change effect |
| --- | --- | --- | --- |
| `file` | UTF-8 CSV/TSV/JSON records | Required path string | New ingestion |
| `value_columns` | Distinct numeric columns | Required nonempty string list | New analysis |
| `observation_unit` | Meaning of one row | Required nonempty string | New analysis |
| `group_column`, `value_unit` | Group labels/common value unit | Strings empty | New analysis; missing unit stays unknown |
| `mode` | Aggregate observations / preserve values | observations / values; observations default | New analysis |
| `missing` | Missing handling | reject / omit; reject default | New analysis; never zero-fill |
| `plot` | Plot family | bar / box / heatmap / line / scatter; bar default | New analysis |
| `width` | Output width | wide / column; wide default | New figures |
| `max_mb`, `max_figures`, `max_points` | Input MiB / SVG pages / points or cells | Positive integers 20 / 100 / 10000 | New ingestion/rendering; excess fails, no sampling |
| `x_column`, `x_unit` | Numeric coordinate/unit | Strings empty | New line/scatter |
| `series_layout` | Coordinate axes | separate / shared; separate default | New line/scatter figures |
| `paired_baseline` | Same-row baseline value column | String empty | New observations/bar or box differences |
| `association` | Descriptive association | none / pearson; none default | New line/scatter analysis |
| `attribution` | User source declaration | String empty | New analysis; not bibliographic verification |

Choose a plot that matches the data:

| Plot | Data requirements and display |
| --- | --- |
| Bar | `observations` summarizes count, mean, sample SD and quartiles; `values` preserves supplied values and requires unique labels. |
| Box | Requires `observations`; shows Q1–Q3, median and observed min–max. |
| Heatmap | Requires `values`, unique row labels and a common quantity/unit; no automatic normalization or clustering. |
| Line | Requires `values` and numeric x, unique within each group; sorts by x and breaks at missing y. |
| Scatter | Requires `values` and numeric x; retains duplicate points. As with line plots, shared axes require a common unit. |

Paired differences use complete same-row pairs, candidate minus baseline; marginal boxes may use more rows. Standard error is not a confidence/significance claim. Pearson uses jointly present x/y; constant axes or fewer than two pairs are undefined. See [curve examples](../examples/data-curves/README.md).

<a id="writing-from-supplied-material"></a>

### Writing from supplied material

<a id="writing-constraints"></a>

Structured writing uses kind writing, report-only output, a model and local assets.papers/materials. Paths cannot occupy both roles. Text, Markdown, HTML and PDF are supported; writing materials also accept JSON and validated analysis packages. Keep table_analysis.v1 beside its copied data; both have 20 MiB limits and are rechecked on import. Ordinary JSON is unverified material. State genre/length/intended figures in the goal.

Fields belong to research `[report]`, not native `[task]`. New work normally chooses article; explicit section/adaptive controls can select sections. Explicit incompatible article settings fail. Guided setup can supply explicit values.

| Field | Meaning | Type and default | Change effect |
| --- | --- | --- | --- |
| `writing_method` | Whole article / sections | Optional article / sections; normally article unless controls require sections | New writing; saved method retained |
| `template` | Built-in/custom Markdown outline | String auto; source_review, survey, material_report, experiment, reproduction, analysis_report | New plan |
| `reviewer` | Independent review | llm / disabled; unrequested article review disabled; sections research CLI llm | New writing/review |
| `max_review_iterations` | Correction rounds | Nonnegative integer; unrequested article review 0; sections research CLI 1 | New review; zero can inspect without correction |
| `document_review` | Whole-document inspection | Boolean false | New review |
| `review_scope` | Review granularity | section / document; section default; document needs document_review | New review |
| `draft_scope` | Draft granularity | section / document / auto; section default, article correction may select document | New writing; document/auto needs document review/scope |
| `outline_strategy` | Outline selection | auto / template / adaptive; auto default; adaptive needs sections | New plan |
| `data_tables` | Link evidence / include body tables too | linked / full; linked default | New assembly |
| `max_section_tokens` | Section/joint/article output cap | Nonnegative integer 0 = no extra cap | New model calls |
| `max_cited_sources` | Distinct body-cited source ceiling | Nonnegative integer 0 = no cap | New drafting/assembly; evidence pool retained |
| `max_document_review_prompt_chars` | Complete review-request ceiling | Nonnegative integer 0 = no extra ceiling | New review; excess refused, not truncated |
| `figures` | Deterministic figure settings | Table below | New assembly |

For article inspection explicitly set reviewer=llm, document_review=true, review_scope=document. Zero rounds inspects once; positive rounds bound complete corrections. Section draft_scope=auto drafts jointly only with positive length targets for every section totaling at most 2,000 words; otherwise sectional.

Fields under `[report.figures]`:

| Field | Meaning | Type and default | Change effect |
| --- | --- | --- | --- |
| `enabled` | Supported deterministic figures | Boolean true | New assembly |
| `mode` | Automatic / opt out | auto / off; auto default | New assembly |
| `max_figures` | Overall cap including imports | Nonnegative integer 0 = automatic; positive cap fails on excess | New assembly |
| `format` | Figure format | String svg only | New assembly |

These do not authorize paid image generation; native generation uses image route/max_images. Writing review and arithmetic checks do not certify scientific correctness.

<a id="research-continuation-and-decisions"></a>

### Research continuation and decisions

<a id="continue-a-research-session-after-preparing-execution"></a>
<a id="explicit-continuation-allowances"></a>

New common tasks use `simple-ar start --resume-task SESSION`. Resources use `--authorize-remaining` plus authorization ID/reason; missing/replaced inputs use `--supply-task`; named text-connection changes use `--switch-model` plus switch ID/reason. See [CLI reference](CLI_REFERENCE.md). Editing creation TOML/defaults/saved JSON is not a substitute.

Saved ResearchApplication sessions use `research-session --config PATH --session-root SESSION`. For that compatibility entry point, `[continuation]` accepts:

| Field | Meaning | Type and default | Change effect |
| --- | --- | --- | --- |
| `authorization_id`, `reason` | Stable authorization ID/explanation | Strings required for allowance changes | Explicit continuation |
| `additional_attempts`, `additional_no_progress` | Add attempt/no-progress capacity | Optional integers | Continuation; usage retained |
| `remaining` | Resource name → future capacity | Nonempty numeric map, e.g. `{ total_tokens = 50000, llm_requests = 8 }` | Continuation; no historical reset |
| `decision_id` | Exact pending decision | Optional string | Explicit response |
| `decision_response` | Action | accept / reject / revise | Explicit response |
| `decision_guidance` | Revision facts/direction | Optional string | Explicit response; not process approval |

Repeat identical authorization ID/terms to finish interrupted authorization without replenishing spent capacity; changed terms need a new ID. Increasing budget never erases costs or unknown reservations, nor alone restarts completed work. Saved-session goal/source/execution changes may invalidate dependent plans; incompatible kind/frozen ingestion needs a new task. Report-only changes do not rerun measurement.

<a id="code-file"></a>

<a id="standalone-code-task-config"></a>

## `code-task.toml`: standalone code tasks (advanced)

```toml
[code_task]
kind = "existing_project"
code_root = "{config_dir}/project"
task_file = "{config_dir}/task.md"

[workspace]
mode = "copy"

[edit_scope]
allowed_patterns = ["src/**"]

[execute]
to_step = "probe"
use_llm = false
```

With adjacent project directory/task.md, run `simple-ar code-task init --config PATH`, then `simple-ar code-task execute RUN_DIR --config PATH`. Add a real benchmark.command before measurement. Greenfield uses kind=greenfield and omits code_root unless supplying a scaffold.


<a id="loading-rules"></a>

### Loading rules

Init reads project/workspace inputs; execute reads execution/model/budget. Research references the same parser via execution.code_task_config. Ordinary relative paths use **command cwd**, not TOML directory. `{config_dir}` expands code_root/task_file/output_root, Python paths, benchmark.command, execute.baseline_metrics_file and environment.required_paths. Quote command paths containing spaces. `${...}` resource expressions are rejected.

Most sections ignore unknown keys; workspace/execute.ablation reject extras. Misspellings can have no effect. implementation.max_repair_attempts is not loaded; use execute.repair_rounds. Editing initialization inputs does not rebuild an existing workspace; new limits do not regenerate an accepted plan.

<a id="codetask-field-reference"></a>

### CodeTask field reference

“Init” means a new standalone workspace or research preparation. “Execute” means next supported execute; embedded research retains accepted settings and uses task continuation.

| Field | Meaning | Type and default | Change effect |
| --- | --- | --- | --- |
| `code_task.kind` | Project kind | existing_project / greenfield; existing_project default | Init |
| `code_task.code_root` | Source/scaffold | Path string; required for existing | Init |
| `code_task.task_file` | Task description | Path string; required standalone | Init |
| `code_task.output_root`, `code_task.name` | Run parent/label | Path runs; optional string | Init |
| `code_task.max_file_bytes`, `safety.max_file_bytes` | Copy file ceiling; code_task wins | Integer 2000000; 0 disables | Init |
| `benchmark.command`, `benchmark.timeout` | Workspace command / timeout | Optional string / integer seconds | Init / execute timeout fallback |
| `benchmark.primary_metric`, `benchmark.metric_directions` | Objective/directions | Optional string / string map | Init |
| `metrics.primary`, `primary_metric`, `directions`, `metric_directions` | Aliases under metrics | Optional strings/maps | Init; benchmark primary wins; later maps/CLI override |
| `environment.mode` | Interpreter policy | current / external; current default | Init / explicit execute selection |
| `environment.python`, `python_executable` | External interpreter under environment; python wins | Optional path; external requires it | Init / explicit execute selection |
| `environment.required_paths` | Existence checks only | Path list `[]` | Each load |
| `workspace.mode` | Workspace construction | Existing auto, greenfield empty; modes below | Init |
| `workspace.include`, `exclude` | Globs under workspace | Optional string lists | Init |
| `workspace.reuse_source_venv` | Use detected source venv | Boolean false | Init; incompatible with managed task venv |
| `edit_scope.allowed_patterns` | Allowed paths | List `[]`; standalone empty allows otherwise unprotected paths | Init |
| `edit_scope.protected_patterns` | Extra read-only paths | List `[]`; mandatory protections retained | Init |
| `edit_scope.mode` | Scope label | Optional string | Init; grants no access |
| `safety.validation_max_file_bytes` | Static scan ceiling | Positive integer 500000 bytes | Execute fallback |
| `resource.max_runtime_sec`, `max_files`, `max_generated_lines` | Resource declarations/fallbacks under resource | Optional positive integers | Planning / execute |
| `resource.max_memory_mb`, `allow_gpu` | Planning/handoff declarations under resource | Optional integer / boolean | Planning; no OS enforcement |
| `implementation.provider` | Backend | local; fake, local_llm, codex, claude_code, opencode, external_cli | Execute |
| `implementation.agent_mode` | Model/external handoff | Optional model / handoff; delegated_workspace unavailable | Execute |
| `implementation.allow_external_agent` | External launch permission | Boolean false | Execute |
| `implementation.agent_model`, `agent_binary`, `agent_args` | External model/program/argv under implementation | Optional strings / list; empty model uses external account default | Execute |
| `implementation.agent_timeout_sec` | External timeout | Positive integer 600 seconds | Execute |

<a id="execute-and-budget-fields"></a>

### Execute and budget fields

<a id="execute-and-budget"></a>

CodeTask budget is separate from research budget. Explicit execute CLI values win. Interpreter selection installs nothing; only a leading bare Python alias is resolved, preserving other argv.

| Field | Meaning | Type and default | Change effect |
| --- | --- | --- | --- |
| `execute.to_step` | Stop point | String run; supported steps in CLI reference | Next execute |
| `execute.use_llm`, `llm.enabled` | Model use; execute wins | Boolean true | Next execute |
| `execute.model` | General selector | Optional string; models.code_task.default → models.default → llm.model fallback | Next execute |
| `models.code_task.default`, `models.default`, `llm.model` | General fallback selectors | Optional strings | Next execute |
| `models.code_task.planner`, `writer`, `reviewer`, `editor`, `repair`, `summarizer` | Role selectors under models.code_task | Optional strings; general fallback | Next execute |
| `execute.timeout_sec`, `execute.timeout` | Per-command timeout | Positive integer; timeout_sec → timeout → benchmark.timeout → resource.max_runtime_sec → 60 seconds | Next execute |
| `execute.skip_validation`, `strict_validation` | Validation controls under execute | Booleans false | Next execute |
| `execute.validation_max_file_bytes` | Static ceiling | Positive integer; safety fallback then 500000 | Next execute |
| `execute.stream_benchmark_output` | Terminal output mode | String/bool off/false; line, auto/true, summary | Next execute |
| `execute.baseline_policy` | Baseline treatment | auto; also run, skip, none, provided | Next execute; provided not remeasured |
| `execute.baseline_metrics_file` | Supplied numeric JSON | Optional path; flat map or metrics/metric_values wrapper | Next execute |
| `execute.apply_proposed_edits`, `allow_large_edits`, `allow_planning_fallback` | Opt-ins under execute | Booleans false | Next execute; normal gates remain |
| `execute.planning_mode`, `planning_review_rounds` | Planning/compatibility controls under execute | compact (tool_agent deprecated alias); integer 0 | New planning; old review setting grants no calls |
| `execute.llm_retry_attempts`, `repair_rounds` | Response corrections / technical repairs under execute | Positive integer 3 / nonnegative integer 0 | Next execute; separate transport retries |
| `execute.max_files` | Planning file ceiling | Positive integer; resource.max_files → budget.max_files → 8 | New planning |
| `execute.max_source_chars_per_file` | Source context ceiling | Positive integer 4000 | Next planning |
| `execute.max_generated_lines` | Generated line ceiling | Positive integer; resource fallback then 1600 | New planning |
| `execute.ablation.review_gate` | Review blocking level | strict / runtime; strict default | Next execute; runtime still blocks safety/executability defects |
| `execute.budget_profile`, `budget.profile` | Proposal profile; execute wins | normal / large / absolute; normal default | Next proposal; larger profiles need approval |
| `execute.max_batches`, `budget.max_batches` | Batch ceiling; execute wins | Optional positive integer | Next execute |
| `execute.cost_cap_usd`, `budget.cost_cap_usd` | Estimated-cost ceiling; execute wins | Optional nonnegative number | Next execute; needs prices |
| `budget.max_files`, `max_edits`, `max_old_chars`, `max_new_chars`, `max_total_edit_chars`, `max_proposal_chars` | Proposal limits under budget or budget.PROFILE | Positive integers; defaults below | Next proposal; profile table overrides top-level |

| Profile | Files | Edits | Old block chars | New block chars | Total edit chars | Proposal chars |
| --- | --- | --- | --- | --- | --- | --- |
| normal | 4 | 4 | 3000 | 4000 | 12000 | 24000 |
| large | 4 | 8 | 12000 | 16000 | 48000 | 96000 |
| absolute | 8 | 16 | 24000 | 32000 | 120000 | 240000 |

Model tokens, processes, edit sizes, correction rounds and cost estimates are separate limits. Repairs/external candidates still require scope, review and benchmark validation.

<a id="workspace-mode-variants"></a>

### Workspace mode variants

| Mode | Initialization effect |
| --- | --- |
| auto | Existing default; detached Git worktree when feasible, otherwise protected copy; research copies dirty sources. |
| copy | Eligible current working files, including uncommitted changes, subject to exclusions/size limits. |
| git_worktree | Committed local HEAD; requires a repository with a commit; explicit failure does not fall back. |
| sparse_copy | Include/exclude selection plus mandatory exclusions; may omit dependencies. |
| empty | Greenfield default; copy/sparse scaffold requires code_root. Workspace CLI does not accept empty. |

<a id="edit-scope-behavior"></a>

### Edit scope behavior

Edits occur inside the CodeTask workspace. Nonempty allowlists narrow paths; protection wins. Built-in test/benchmark/secret/credential protections remain. Research preparation retains default source scope when no explicit allowlist is given; declare globs to narrow it. Workspace isolation is not an OS sandbox.

<a id="codetask-in-a-research-session"></a>

### CodeTask in a research session

Reference execution.code_task_config for scoped preparation, editing and validation under research permissions/shared process capacity. Measurement is separately declared where required. Accepted scope/environment/budgets persist; editing referenced TOML does not silently replace them.

<a id="extension-file"></a>

<a id="trusted-local-packages"></a>

## `extension.toml`: local extensions (advanced)

Only explicitly selected packages load; no automatic discovery/download/installation. Native task extensions paths name package directories containing extension.toml or descriptor files, relative to task TOML.

```toml
[task]
request = "Summarize the supplied note."
source = ["notes.md"]
extensions = ["../my-skill"]
```

Fields below belong to the package's extension.toml. Packages may provide instructions, tools or both. Changes require new selection; continuation requires original paths/hashes of descriptor, code, instructions and declared resources.

| Field | Meaning | Type and default | Change effect |
| --- | --- | --- | --- |
| `extension.id`, `version`, `description`, `scope` | Metadata under extension | Required nonempty strings; ID `[a-z][a-z0-9_]{0,23}` | New selection |
| `extension.instructions`, `resources` | Package-local files under extension | Optional path / path list `[]` | New selection |
| `tools` | Tool declarations | Optional table array | New selection |
| `tools.name`, `description` | Tool metadata | Required strings; name uses ID syntax, instructions reserved | New selection |
| `tools.implementation`, `function` | Standalone .py and function identifier | Required tool strings | New selection |
| `tools.permission_level` | Declared operation class | read_only / plan | New selection; author ensures no side effects |
| `tools.input_schema` | Object argument schema | Required Draft 2020-12 table; local refs only | New selection |
| `tools.completion` | Optional delivery/question completion | Table; kind delivery, field empty, closing false | New selection |

Public names are `id__name`; instructions load through `id__instructions`. Optional completion uses `{ kind = "delivery", field = "handoff", closing = false }`: the named schema field contains `summary` and optional `artifacts`/`unresolved`. With an empty field the whole object is the delivery; `kind="question"` uses `{question = "..."}` instead. Completion declarations grant no extra permission or budget.

Extension Python runs with host OS privileges despite read_only/plan declarations. Host services check authorized mounted operations but cannot sandbox Python's own file/process/network access. Undeclared dependencies are outside identity checks. See complete [descriptor](../examples/local_extension/extension.toml) and [instructions](../examples/local_extension/SKILL.md).

<a id="saved-task"></a>

## Saved task directory: viewing and changes

The application maintains task state, usage ledgers and recovery records. Editing the original creation TOML does not update these records. Do not edit internal JSON to change a task.

- Continue or provide feedback with `simple-ar start --resume-task SESSION`.
- View current deliveries with `simple-ar results SESSION`, or progress and usage with `simple-ar status SESSION`.
- Change remaining resources, supply missing settings or switch connections through the [CLI continuation options](CLI_REFERENCE.md#simple-ar-start). These actions retain prior usage and failure records.
