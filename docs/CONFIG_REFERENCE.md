# Configuration Reference

[中文版本](CONFIG_REFERENCE_zh.md) · [Usage](USAGE.md) · [CLI reference](CLI_REFERENCE.md)

Use this reference to look up configuration fields and constraints. A model catalog defines connections; research TOML defines an exact task; CodeTask TOML defines an editable project. Credentials belong in the environment or private `.env`.

| Look up | Section |
| --- | --- |
| Connection selection, profile fields, transport | [Named model connections](#named-model-connections) |
| Credentials and deployment variables | [Credentials and environment](#credentials-and-environment) |
| Goal, assets, resource limits and execution | [Research TOML](#research-toml-sections-and-defaults) |
| Writing methods and report options | [Writing from supplied material](#writing-from-supplied-material) |
| Table fields and statistical transformations | [Existing data](#existing-data-descriptive-analysis) |
| Decisions and additional allowances | [Continuation](#research-continuation-and-decisions) |
| Project, interpreter, models and edit budgets | [CodeTask fields](#codetask-field-reference) |
| Python creation, native controls and trusted services | [Python API](#python-task-creation) |

## Guided setup

Ordinary `start`, `--guided` and outcome preferences save a native session, not editable research TOML. Startup arguments supply its goal, read-only materials, permissions, interpreter and allowances; resume retains saved values. Explicit structured protocols and `--chat` save research TOML and, when needed, reference CodeTask TOML. A chat setup draft is not an execution session.

Structured precedence is defaults → TOML → explicit CLI. CLI lists replace file lists. Research paths resolve from the TOML directory; argv stays literal. Protocol protected paths use execution cwd. CodeTask retains separate [loading rules](#loading-rules). Native access/resource changes use explicit continuation controls rather than ordinary overrides.

### V2.10 task declarations (development branch)

V2.10 work continues on `feat/v2.10-project-research-assistant`; verified improvements may be integrated into main before the milestone is complete. For the scope actually checked, see [development evidence and limits](EVALUATION.md#v210-development-evidence).

The development branch stores the original request separately from material roles, data meanings and outcome preferences. CLI and UI confirm the same effective declarations rather than appending generated path lists to the user's words. Preferences guide delivery, not capability access; declarations are neither verified facts nor additional permissions. Omitting `--with-report` does not prohibit a report requested in prose. Historical sessions retain their original input rules.

On the V2.10 development branch, native `[task]` TOML also accepts `extensions = ["../my-skill"]`.
These explicitly selected trusted local packages use the common tool registry and saved task;
v1 accepts instruction packages and Python tools declaring read-only/plan operations.
Trusted Python runs with the host process’s OS privileges, including direct file, process and network access; declarations and host-service restrictions are not a sandbox. Load only trusted packages.
See [local extension setup and limitations](LOCAL_EXTENSIONS.md).

For new work based on saved outputs, native `[task]` also accepts `from_session = "../saved-task"`
and `reuse = ["artifacts"]` (or named groups listed by `simple-ar results`). The session path is
relative to the configuration; group names are not paths. This reuses byte-checked materials,
not the previous permissions, budget or execution state. Use `--resume-task` for the same task.

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

Changing a saved text connection explicitly starts a new native conversation boundary, not a new task or budget. A professional focus whose actual connection changes does the same; an unchanged connection keeps its boundary. Original replies, reasoning and tool records remain available, but incompatible prior assistant messages are not replayed as the new provider's conversation. This allows supported thinking-mode transitions without inventing missing reasoning.

| Profile field | Default / allowed values |
| --- | --- |
| `api` | Required: `openai_chat`, `openai_responses`, `openai_images`, `cctq_images_async` |
| `base_url`, `model`, `api_key_env`, `capabilities` | Required: HTTP(S) URL without userinfo/query/fragment; nonempty model; credential variable name; nonempty list from `text`, `code`, `vision`, `image_generate`, `image_edit` |
| `proxy_env` | Empty; variable containing HTTP(S) proxy URL without userinfo/query/fragment; resolved URL is not persisted |
| `stream`, `http2` | `false`, `false`; streaming supported for Chat only; Responses profiles require `stream=false`, including for structured tasks |
| `request_timeout_sec` | `180`; positive seconds. For finite SDK Chat streaming, also caps the entire stream's wall-clock duration, not just inactivity; arriving chunks do not reset it. Expiry without finish/usage stops without adopting partial content or releasing unknown charges. |
| `connect_timeout_sec` | Unset keeps the historical TCP/TLS cap of 20 seconds. A finite positive value changes SDK connection waiting, bounded by a finite positive request timeout. Text SDK profiles only; not a generation deadline or a retry allowance. |
| `max_output_tokens` | Unset; positive output cap |
| `context_input_tokens` | `24000`; positive native input working-set target, not provider context-window declaration |
| `retry_attempts` | `3`; at least 1, includes first physical request |
| `connect_retry_attempts` | `1`; optional higher ceiling for verified pre-dispatch connection failures only. Each attempt consumes request budget; never expands retries for interrupted/unknown responses |
| `native_response_corrections` | Strict integer `0` or `1`, default `0`; `1` explicitly authorizes one completed, settled truncation correction in native Chat turns only. Responses and Images profiles cannot enable it; no environment variable or CLI prompt |
| `retry_base_delay_sec`, `retry_max_delay_sec` | `1`, `12`; positive seconds |
| `reasoning_effort` | Optional reasoning parameter, separate from the exact `model` ID; empty means omit it. Accepted values depend on the provider/model, not the profile alias or Codex subagent settings. Configurable values: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`. A requested effort is not proof the provider applied it. |
| `thinking_mode` | Empty, `enabled`, `disabled`, `auto`; Chat disabled thinking requires empty effort or `none` |
| `reasoning_output_tokens` | Unset; positive fallback for active effort only; explicit call/profile caps win |
| `json_response_format` | `auto`, `off`, `json_object`; off omits native JSON parameter, local validation remains |
| `chat_token_limit_param` | `auto`, `max_tokens`, `max_completion_tokens` |
| `input_price_per_million`, `output_price_per_million` | Unset; nonnegative estimates |
| `image_size` | Unset; `auto`, `256x256`, `512x512`, `1024x1024`, `1024x1536`, `1536x1024`, `1792x1024`, `1024x1792` |
| `image_quality` | Unset; `auto`, `standard`, `hd`, `low`, `medium`, `high` |

Catalog fields/types are strict. Images profiles accept only image capabilities and reject explicitly supplied text stream/token/context/reasoning/JSON/price options. Text profiles reject image size/quality and generation capabilities. Capabilities are declarations, not API probes; provider parameter support needs checking. Native decisions and `focus_work` require Chat; `compose_document` can also use Responses on the writing route. Focus switches change neither tools nor shared allowances.

New named research/native tasks and chat drafts save non-secret bindings. Model, endpoint, protocol, HTTP/2, reasoning settings and applicable code/writing routes stay pinned. Streaming, timeout, output cap, retries/delays and input working-set target may be retuned; keys may rotate behind the same variable. Transport retuning does not resume a failed call or release unknown charges; explicit recovery is still required. Vision grants save a separate binding. Unrelated image profiles do not invalidate text tasks. A native task's retained catalog location is only a lookup hint: the original connection binding still rejects receiver/protocol drift before dispatch. Missing files require an explicit or matching user catalog; missing credentials remain errors. Old tasks without hints are not silently backfilled; legacy tasks without bindings retain legacy resolution. Standalone CodeTask does not acquire retrospective pinning. Native failed-decision replacement requires the explicit CLI retry contract.

`native_response_corrections=1` requires a budget ledger and finite output cap, and applies only to `chat_turn` after a complete `finish_reason=length` response has passed usage settlement. The partial response/tools are never adopted or executed; its discarded status remains in the ledger reason. The same client/model, cap, history, tools and logical budget call ID are reused. One internal system notice states the truncation, non-execution and cap, requesting a complete operation under existing instructions; it contains no discarded text, reasoning or arguments. A second truncation, empty output, unsupported finish reason, content filter or error stops. Ordinary text, JSON and vision calls are unchanged.

This is one semantic correction, not a network retry or a promise of two physical requests: each completion uses existing transport retries, reservations, settlement and failure accounting in the same ledger. Existing budget limits and active `protect_remaining` floors still apply, with no extra allowance. If this completion introduced any still unknown/reserved attempts (including a transport retry before eventual success), correction/adoption stops; older bounded unknown entries are retained and do not themselves disable correction. Enabling can spend additional requests/tokens and still fail; no quality improvement is guaranteed. The option is pinned connection authorization, not an operational timeout field: old bindings missing it mean `0`; switching saved `0`/missing to `1` requires an explicit connection change and does not retroactively authorize historical contracts or revive failed tasks.

HTTP/2 requires the `simple-autoresearch[http2]` extra on the execution host (`uv sync --extra http2` in a checkout). Missing image support fails before reservation/submission; intact completed images can restore offline. A profile proxy affects only that connection and fails before paid calls if invalid/missing; without it standard client routing remains.

Finite-timeout SDK Chat streaming bounds submission plus consumption, then at most two seconds of cleanup. Connect waiting is at most `min(connect_timeout_sec, request_timeout_sec)` seconds; omitted connection settings retain the historical 20-second cap. Read/write/pool keep their configured timeout. SDK internal retries are disabled. Missing completion markers retain unknown usage; capped text may retry on the same API within the count, preserving failed-attempt reservations. Uncapped/visual requests and insufficient budgets stop. The optional connection setting also applies to synchronous SDK Chat/Responses; it is not supported by LiteLLM or Images profiles. From an existing asyncio loop, use `await asyncio.to_thread(client.ask, ...)`.

Reasoning labels/caps are provider-specific; reasoning may count against output and later input. A length-stopped reply with reasoning but no final answer is incomplete. Immediate overload is not cured by timeout changes. Diagnostics retain exception type, available HTTP status and allowlisted protocol codes/types; SDK Chat failures also retain the last safe TCP/TLS/HTTP event when observed, without headers or trace payloads. A pre-dispatch connection failure is distinct from an interrupted response with unknown usage; a later successful TLS probe does not certify API stability. Uncapped output can exceed the task's input-based remaining estimate, so task allowances alone are not strict provider spending caps.

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

Document routing keeps TLS verification, rejects wildcard hosts and affects linked supporting materials only, not model/search/PDF/ZIP transport. Unlisted hosts are direct; without explicit routing this downloader ignores ambient proxies. `SIMPLE_AR_OUTPUT_DIR` is provided per process, never a global setting.

Connector keys use authorization headers. On HTTP 429 a batch stops live calls to that provider, checks opted-in caches and continues other providers. Web uses Tavily basic search without upgrades, generated answers or automatic retries; missing keys fail. Undated pages do not satisfy year filters. Extraction is independent of search, requires acquisition permission and preserves original URL/text/request ID/reported usage separately. Cache reuse avoids calls; extraction failures do not fall back/upgrade. PDFs keep separate permissions. Snippets/metadata are discovery material, not verified originals.

## Research TOML: sections and defaults

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

Run `simple-ar research-session --config research.toml`. See [the survey example](../examples/survey/README.md). Unknown research sections/fields/types fail. Model-backed common creation defaults to finite task allowances of 30 requests and 600000 tokens; without a model, omitted model allowances are zero. Saved usage/limits remain on resume; legacy sessions retain their original budget contract.

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

Local PDFs parse even when remote full text is disabled. Remote PDFs need download/cache-retention permissions. Provenance records total/extracted/empty pages and truncation; extraction is not OCR or full understanding. Basic parser enforces ceilings; unstructured rejects ceilings it cannot enforce. Legacy coverage is not reconstructed.

Search plans use focused queries and preserve explicit seeds. arXiv joins plain keywords with `AND` and passes advanced expressions through. With model reading and automatic expansion, one gap-driven search/ingest/read cycle shares the original limits. Linked-page redirects use at most four GETs per acquisition with public-address checks. No interaction mode may bypass required material or permission checks for the affected operation.

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

Omit command and CodeTask for literature-only work.

For fixed measurements with `reuse`, `baseline_ref` paths are relative to the configuration file, or the current directory when no configuration is supplied. The framework checks the command, metric schema, comparison protocol and original hashes of explicitly protected files, then retains the original result as a read-only task snapshot. Only candidate conditions execute; the baseline keeps its original measurement identity and creates no new process or usage entry. Missing protected-file evidence, mismatched conditions, failed results or changed inputs block reuse. A scoped CodeTask can consume the same external baseline: verified observations and snapshot hashes bind implementation approval and reach the code module directly. This does not claim the baseline came from the candidate's preparation. Imports claiming a producing preparation or implementation still require their original lineage owner; a bare result cannot erase that lineage. Historical results and ledgers are preserved.

| `[execution.environment]` field | Default / constraint |
| --- | --- |
| `mode` | venv creates task environment; current requires explicit check and performs no install/interpreter override. Omit table for ordinary current execution |
| `requirements`, `install_project` | `[]`, `false`; requirements relative to execution cwd |
| `python_executable` | Venv base interpreter, default runtime |
| `timeout_sec`, `check_command` | 300 seconds per step; optional literal short-check argv |

Preparation/checks share process budgets/logs; failure stops measurement. Checks emit no measurement metrics. Prepared venv resolves bare Python aliases; conflicting explicit executables in source-owned measurements are rejected, never silently replaced. Installation can run build code/network; workspace is not an OS sandbox. Completed recovery does not reinstall.

Exact tasks may prepare an unchanged source, measure a baseline, implement a separate candidate and reuse that preparation during feedback. Frozen conditions record their source independently of baseline/candidate roles. Explicit `skip` requires only declared candidate runs and produces descriptive results, not a fabricated baseline comparison. External baseline reuse must preserve the lineage described above; unsupported new declarations fail before creation rather than selecting another controller.

Ordinary benchmarks may use identical validator and formal argv: validation and measurement require separate physical calls and ledger records, and validator results never count as measurements. Strict reproduction still requires a distinct validation command. Greenfield `copy`/`sparse_copy` with a real source project may include source baselines; those frozen conditions carry `source="preparation"` and `before_implementation=true`. Prepare first, complete every declared baseline, then call `implement(prepared_result)` with the actual successful baseline facts. Missing/failed baselines cannot authorize generation. Empty/source-less projects cannot invent an unchanged-source baseline; use real source or verified external measurement evidence. Every project measurement cwd must still match the original source root, and edit scope and path projection remain unchanged.

```toml
[execution.output_files]
runtime = "runtime.json"
observations = "tables/observations.csv"

[execution.metric_sources]
elapsed = { output = "runtime", path = ["elapsed_seconds"] }
coverage = { output = "observations", column = "coverage", match = { method = "candidate" } }
```

Files belong under the invocation's `SIMPLE_AR_OUTPUT_DIR`. The literal `{output_dir}` argument binds that directory without shell expansion. Register files using relative POSIX paths. There is no file-count limit; each attachment must be a UTF-8 regular file no larger than 2 MiB.

Registered attachments receive previews and read handles. The system does not scan the working directory, infer paths from stdout, or copy external files. Missing, unreadable, or oversized attachments are reported separately from metric success and cannot support claims.

`results.json.output_evidence` records provenance and truncation. Readers can request character windows, literal matches, or JSON-array, CSV, and TSV rows selected by up to four exact scalar conditions. These reads do not aggregate records.

JSON metrics select explicit keys/indices; CSV/TSV must select one row. Missing, ambiguous, nonfinite or stdout-conflicting values fail; files cannot turn failed/timed-out processes into successful measurement. Attachment-contract changes are execution revisions, not report refreshes.

Reproduction requires materials_only=true, local papers, command, finite timeout, baseline_policy="skip" and hypothesis/dataset/expected_outcome. No innovation, seed expansion or implicit environment discovery. Optional CodeTask prepares scoped adapter/entry edits: its benchmark is the independent validator, execute timeout the positive validation limit. Validator argv must differ from measurement after interpreter resolution; it must pass first. Supported flat tasks create declared initial files directly through CodeTask, not initializer placeholders; historical contracts retain their original continuation. Method/split/conditions remain protected. Explicit current checks or a task venv run before implementation, with the full preparation/validator budget checked first; checks cannot run the formal measurement. A venv requires initially current CodeTask Python and cannot combine with source-venv reuse. Feedback retains the recorded environment without reinstalling. See [the example](../examples/conformal_reproduction/README.md).

Each `[[execution.pairs]]` entry requires a unique integer `seed` and literal argv arrays named `baseline_command` and `candidate_command`:

```toml
[[execution.pairs]]
seed = 0
baseline_command = ["python", "evaluate.py", "--method", "baseline", "--seed", "0"]
candidate_command = ["python", "evaluate.py", "--method", "candidate", "--seed", "0"]
```

Alternatively, compact expansion needs `command`, unique `seeds` or positive `seed_count`, and explicit `seed_flag`. Do not combine it with `pairs`. Existing `--seed 0` / `--seed=0` arguments are replaced, not duplicated. A flag alone records one seed without authorizing extension. Protocol-only seed lists describe evaluator conditions and keep one command.

An extension needs an analysis recommendation, a matched pair, an accepted decision and remaining process capacity. Conflicting command/protocol seeds fail.

Baseline identity checks command/schema/data/split/metrics/conditions/protocol revision/preparation/protected assets, not candidate ID/hypothesis/cwd alone. Missing historical evidence is not filled from newer plans; paper numbers are not local measurements. Protected files need unique asset_id/path, relative to execution cwd or absolute; checks cover listed files only. Budget every matrix invocation. Direct argv uses process PATH; CodeTask resolves leading bare Python through its environment policy.

## Writing from supplied material

Structured writing uses kind="writing", outputs=["report"], materials/papers and a model. Ordinary `start --kind writing` saves native state. Papers are sources; drafts/notes/results are materials. Duplicate/dual-role paths fail. Text/Markdown/HTML/PDF are supported; JSON objects/arrays remain unverified text. Malformed JSON fails; declared table_analysis.* cannot degrade to ordinary text.

Completed table_analysis.v1 imports need adjacent copied data; JSON/input each ≤20 MiB. Arithmetic is rechecked and SVG/PDF/PNG regenerated. Stale/incomplete packages fail. Calculation checks do not verify collection/science; raw tables are not verified experiment packages. Extracted inputs persist for resume. Put genre/length/count scope in the goal: soft targets differ from hard ranges, with no invented tolerance or pages-to-words conversion; saved reference/title/heading count scope remains.

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

Article makes one complete-body writing request then deterministic assembly/audit. Set `reviewer="llm"`, `document_review=true`, `review_scope="document"` for independent inspection. Zero correction rounds inspect without rewriting. Positive rounds reuse the existing joint editor: retain the initial response, propose complete candidates and adopt only after independent inspection. Rejected or unavailable proposals consume rounds; saved candidates and completed inspections recover without repeating calls. Unknown or rejected responses are not blindly resent. Unfinished required corrections fail rather than publish a successful revision. These checks are not scientific certification.

Fixed structure and citation limits do not require the sectional Writer. Without automatic corrections, missing, duplicate, unplanned or reordered article-level headings and excess citations reject the response without resending it. With bounded corrections, a complete initial candidate may be retained for editing but cannot be published until the constraints and independent inspection pass. Subsections remain free. Incompatible explicit controls fail. Refresh/revision may use prior accepted prose separately from evidence; old responses/charges remain. Historical articles resume with their saved constraints, not today's defaults.

`sections` preserves the accepted plan, checkpoints and bounded corrections. With `draft_scope="auto"`, joint drafting requires a positive length target for every section and a total of at most 2,000 words; otherwise drafting is sectional. New document/auto correction tasks inspect the current manuscript rather than adding a separate check of old opinions. Replacing earlier opinions requires complete current-document coverage; original opinions remain in history. Saved inspections resume without another inspection request, while historical checkpoints keep their prior checking contract. Joint revisions are complete candidates, and rejected or pending rounds still count; adoption requires independent inspection of the complete candidate. If document inspection cannot finish, drafts are saved and the task pauses.

Sectional writing may reread registered originals within the existing tool allowance. This does not authorize new searches, arbitrary file reads or processes. Internal backtracking settings are not public research TOML fields.

Auto template uses source_review for at most one citable source, survey for multiple, material_report for writing, and experiment/reproduction/analysis_report from goal judgment. Explicit templates cannot override facts; execution passed is not scientific success. Saved resolved templates remain. Adaptive non-survey sectional planning adds one call plus at most one correction; invalid pointers fail, fallback needs explicit runtime policy. Figure ownership can be evidence-planned; structured guided writing saves adaptive, guided data/survey/reproduction may save joint drafting/review rather than base defaults.

Both data-table modes retain the whole portable package. Unique figure/link owners use their frozen section; ambiguous ownership uses descriptive-data section. Experimental packages retain experiment_evidence.json/readable companion/registered copies; audit checks projections/presence, not science or later tampering. ACM carries registered packages, not arbitrary links. Concepts are evidence-bound, never invented to fill a figure quota. Unknown bibliography stays unknown.

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

Pre-save checks shape/columns; ingest checks finite numbers and freezes bytes/settings. Missing never becomes zero; nonnumeric/nonfinite fails. Resume reuses snapshots; changed transformations need new task. Analysis-only ignores models; report additionally requires model/shared budget and may include local text/Markdown/HTML/PDF notes/references, without overriding numeric settings or authorizing search.

| Plot | Required mode / behavior |
| --- | --- |
| bar | Observations: grouped count/mean/sample SD/quartiles; values: preserve values with unique labels |
| box | Observations only; Q1–Q3, median, observed min–max; quartiles interpolate (n−1)p; no coordinate/Pearson options |
| heatmap | Values + unique row labels/common declared quantity/unit; global raw color scale across pages; no normalization/sorting/clustering; omit keeps NA cells, entirely missing matrix fails; no coordinate/Pearson/pairing/shared layout |
| line | Values + numeric x; unique x per group, sorted, complete x required; missing y breaks lines |
| scatter | Values + numeric x; duplicates retained; omit keeps missing-coordinate rows in package but not plot |

Box whiskers are neither uncertainty nor Tukey cutoffs. Coordinates are not aggregated/fitted; groups mean series, not pairs/replicates. Shared axes require declared common unit without verification/normalization; all series positions count. Legends/pages retain groups/common panel scales; heatmap limits count missing cells too. Labels/values/counts/omissions remain.

Paired baseline requires observations/bar or box, baseline+candidates selected, same-row matching/common quantity/unit. It retains candidate-minus-baseline values, complete/missing counts, mean/sample SD/SE. Omit jointly filters pairs, never subtracts separate filtered means. ≥2 independent pairs show ±1 SE, not confidence/significance/improvement. Box marginals use each column's nonmissing rows; differences use complete pairs in separate panel.

Pearson requires line/scatter x/y and computes per group/y from jointly present coordinates. Constant axes or <2 pairs undefined; two nonconstant pairs give ±1. Counts/reasons retained; no point changes, regression/significance/causality/independence claim. Small r does not exclude nonlinear association. [Curves example](../examples/data-curves/README.md).

## Research continuation and decisions

### Continue a research session after preparing execution

New common tasks continue with the printed `start --resume-task SESSION`: use feedback for text changes and [explicit supply](CLI_REFERENCE.md#native-recovery) for missing or explicitly replaced native contracts. The legacy plan/reanalysis/report-refresh rules below apply to saved ResearchApplication sessions, not to new tasks merely because they used research TOML.

For those legacy sessions, use `research-session --config research.toml --session-root SESSION`. Omitted goal/outputs stay; explicit goal/output/local-paper/execution revisions rebuild dependent plans, retaining history. Reuse measurements only with matching command/schema/protocol/preparation/protected assets. New task kind or incompatible search/ingestion needs new session; report-only changes rebuild only report, research-report adds missing report.

Continuation decision_id/decision_response/decision_guidance answer pending choices; responses accept/reject/revise, delivery revisions also need report settings. Exact replay restores interrupted saved revisions without repeating work; different terms/changed inputs invalidate old decisions. Interaction changes cannot mix with reanalysis/report-only refresh, except delivery answers may include mode/report settings. See [CLI reference](CLI_REFERENCE.md).

### Explicit continuation allowances

```toml
[continuation]
authorization_id = "continuation-1"
reason = "Finish the saved goal"
additional_attempts = 2
additional_no_progress = 1
remaining = { total_tokens = 50000, llm_requests = 8 }
```

This `[continuation]` table applies to legacy sessions with `--session-root`. New common tasks use the [native allowance controls](CLI_REFERENCE.md#native-recovery). New remaining amounts apply to subsequent calls on configured ledger dimensions; history/unknown charges persist. Attempt/no-progress additions increase caps, not reset counters. Stable ID/reason required; same ID/terms complete interrupted authorization idempotently, never replenish spent capacity. Changed terms need new ID. Do not edit ledger/manifest.

Authorization alone leaves completed sessions completed and runs nothing. Authorize then refresh report (normally writer/assembly/audit: three attempts, failed retries extra). Report-only needs no process expansion. Declare process dimensions at creation if later execution is intended.

## Loading Rules

CodeTask init --config loads initialization; execute --config loads execution/model/budget; research-session --code-task-config shares parser. Explicit CLI overrides corresponding values. CodeTask relative paths use command cwd. `{config_dir}` expands code/task/output paths, Python paths, benchmark command, baseline metrics and required paths; resource `${...}` substitutions rejected. Quote spaced command paths. Required paths check existence only.

Most CodeTask sections ignore unknown keys; workspace/ablation reject extras, retired stage selectors fail, budget accepts overrides. Misspellings may have no effect. Historical implementation.max_repair_attempts is not loaded by the current public parser; use supported execute.repair_rounds. Old outer-pipeline configs are read-only history.

## CodeTask Field Reference

### Code-Task Fields

| Field group | Default / behavior |
| --- | --- |
| code_task.kind/code_root/task_file/output_root/name | existing_project, required source/task, runs, optional suffix; greenfield task required, optional scaffold, generated workspace/generated_project |
| code_task.max_file_bytes, safety.max_file_bytes | First overrides second; default 2000000, copy/sparse; 0 disables |
| benchmark.command/timeout/primary_metric/metric_directions | Workspace command string, optional timeout/metric/directions |
| metrics.primary/primary_metric/directions/metric_directions | Compatibility aliases; benchmark primary wins; later direction maps then CLI override |
| environment.mode/python/python_executable/required_paths | current/external; external Python required, python alias wins; bare leading Python resolved, other argv retained; presence list default empty |
| workspace.mode/include/exclude/reuse_source_venv | Existing auto, greenfield empty; sparse globs; reuse false, detected venv supplies external Python |
| edit_scope.allowed_patterns/protected_patterns/mode | Allowlist / additional read-only globs / label; label grants nothing |
| safety.validation_max_file_bytes | Default 500000; execute scan overrides |
| implementation.provider/agent_mode/allow_external_agent | local; fake/local_llm/codex/claude_code/opencode/external_cli supported; model/handoff, delegated_workspace explicitly unavailable; external launches require true |
| implementation.agent_model/agent_binary/agent_args/agent_timeout_sec | Optional; timeout 600; empty model retains external account default |
| resource.max_runtime_sec/max_files/max_generated_lines/max_memory_mb/allow_gpu | Planning/handoff declarations; runtime/files/lines feed execute fallbacks, not OS enforcement/install |

External candidates pass normal review/validation/benchmark/guards. Interpreter selection installs nothing. Static checks honor Python encoding: verified byte-identical frozen Git syntax defects warn unless strict; changed/new invalid files/unverified baselines error.

### Execute And Budget Fields

| Field | Default / behavior |
| --- | --- |
| execute.to_step/use_llm | run, true; steps in [CLI](CLI_REFERENCE.md) |
| execute.model | General selector → models.code_task.default → models.default → llm.model |
| llm.enabled/model | Compatibility; execute settings win |
| models.code_task.planner/writer/reviewer/editor/repair/summarizer | Planning, greenfield files, review, proposals, repair, summaries; explicit routes or shared connection |
| execute.timeout_sec/timeout | First → alias → benchmark.timeout → resource.max_runtime_sec → 60 |
| execute.skip_validation/strict_validation/validation_max_file_bytes | false/false/500000 |
| execute.stream_benchmark_output | off/false; line, auto/true carriage returns, summary tail |
| execute.baseline_policy/baseline_metrics_file | auto; run forces, skip skips, none no comparison, provided marks supplied values not reproduced |
| execute.apply_proposed_edits/allow_large_edits/allow_planning_fallback | false; explicit application / reviewed large edits / eligible fallback |
| execute.planning_mode/planning_review_rounds | compact/0; tool_agent deprecated; retired rounds grant no calls |
| execute.llm_retry_attempts/repair_rounds | 3 completed-response attempts including first / 0 technical repair rounds; transport retries separate |
| execute.max_files/max_source_chars_per_file/max_generated_lines | 8/4000/1600; resource fallbacks for files/lines; complete oversized plans fail, saved plans not rebuilt |
| execute.ablation.review_gate | strict; runtime blocks safety/executability but permits packaging/report gaps |
| execute.budget_profile/max_batches/cost_cap_usd | Override corresponding budget fields |
| budget.profile/max_batches/cost_cap_usd | Normal edit profile; optional batch/cost caps when estimates available |
| budget.max_files/max_edits/max_old_chars/max_new_chars/max_total_edit_chars/max_proposal_chars | Numeric overrides; selected budget.normal/budget.large overrides top-level |

Normal permits up to four allowed files, 12000 total edit characters. Large needs explicit approval and absolute bounds (32000 per source block). Provided JSON metrics: flat map, metrics or metric_values wrapper. Process success differs from objective improvement.

#### Planning failures and repair

A complete malformed planning response can receive bounded correction or explicitly allowed fallback. Provider failures, exhausted budgets, deadlines, partial streams and unreconciled operations do not become offline success. Offline greenfield generation uses a four-file development scaffold, not task-specific modules guessed from keywords. It fails until implemented and emits no invented metrics; a file cap below four rejects this scaffold. Model planning chooses its structure from the task: file and line limits are ceilings, not target sizes. Saved plans retain their original files.

Technical repairs require review and remain within the accepted protocol and process limits. They do not authorize method changes, seed extensions or paired-revision repair. Reusing a repair checks bound inputs and project bytes; snapshots and rollback remain available. Interrupted candidates are not blindly replayed.

#### Historical planning

Method migration retains old stages, failures, hashes and usage. Retired stage model routes are rejected. Generated projects and imported immutable accepted plans skip replanning.

## Workspace Mode Variants

`research-session --code-task-config` retains the configured file-size cap, sparse include/exclude rules and source-venv selection. Declared project data remains copy/worktree-only; sparse mode does not bypass that protection. Source-venv reuse cannot be combined with managed task-environment preparation. After preparation, validation, measurement and reload use its selected interpreter. Candidate revisions copy the accepted source with that interpreter; baseline revisions reapply the original import policy. A size cap that would omit accepted candidate files stops preparation instead of silently discarding them.

CodeTask source inventories skip symbolic links and Windows reparse entries. Old receipts that hashed linked targets require inspection; recovery does not reread those targets to manufacture a matching source snapshot. Workspace isolation is not an OS sandbox.

### `auto`

Existing default: detached worktree where possible, guarded copy with recorded reason otherwise. Research falls back for uncommitted source. Repository-subdirectory projects use matching worktree subdirectory.

### `copy`

Copies eligible working-tree state, including uncommitted source; exclusions/size limits may omit files.

### `git_worktree`

Explicit committed HEAD, local repository with a commit; no remote required. Fails instead of falling back; does not include uncommitted source.

### `sparse_copy`

Standalone include/exclude globs plus built-in Git/venv/runs/cache/build/data/model/secret exclusions. May omit runtime dependencies. Research preparation also supports sparse_copy, but declared project data cannot bypass copy protection through sparse mode. Greenfield TOML defaults empty; copy/sparse scaffolds need code_root. CLI --workspace-mode does not accept empty.

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

## CodeTask In A Research Session

execution.code_task_config / --code-task-config references the same file. CodeTask owns editing/validation; research owns measurement/report. Reproduction adapters explicitly permit separate formal command. Numeric edit overrides reach research/recovery without bypassing protection/absolute limits.

Exact code, preparation, comparisons and report/feedback share the native task owner. A declared environment is prepared once; default process limits include setup and repairs, while explicit limits—including zero—remain authoritative. Sources, summaries and code outputs may be combined without manufacturing a scientific design. When one is required, implementation consumes the accepted original design and binds its approval to current source/evidence; acceptance never grants process permission. Continue native tasks with simple-ar start --resume-task SESSION and a Chat profile. Explicit legacy run-adoption APIs retain their original contracts; new CLI tasks never fall back to them. Worktree approval cannot yet bind checkout filters, attributes, submodules or newline transformations: explicitly choose copy or retain the expert protocol where these semantics matter.

When `report` is requested, composition consumes the accepted implementation evidence and its actual revision history. The writing owner executes the saved article/section method, adaptive planning, review and bounded revision settings; ordered required headings and citation ceilings remain mandatory. Completed delivery requires both validated code and the current canonical manuscript, including after feedback. Task `max_output_tokens` survives professional routing without changing the named connection; explicit per-call limits, including `max_section_tokens`, take precedence. Missing report revisions or unresolved required findings remain partial, not completed.

Explicit greenfield edit scopes and budgets apply to the complete generated candidate before publication, including deterministic entry files and received agent output. A rejected candidate does not replace the previous project. Native feedback edits the accepted project and retains its generation/API/artifact contract; it does not regenerate the architecture or bypass whole-project checks by changing task kind. A purely technical exact task does not invent a default score; scientific result contracts remain unchanged.

New native professional reports render declared visual plans only; a figure quota or a heading is not a request to invent a diagram. Supplied authored figures remain available. Historical assembly retains its saved rendering policy.

## Execute And Budget

Use the [field table](#execute-and-budget-fields). Connection retries, research allowances, process limits, edit sizes and correction rounds are distinct; none resets another's usage.

## Edit Scope Behavior

Only workspace files are edited. Empty allowlist allows normalized non-protected paths; otherwise allowed match and no protected match required. Built-in tests/benchmarks/.env/secrets/credentials stay protected; custom patterns add protection. Batch/work-plan targets narrow scope further. Context/planning/proposal/repair enforce scope; apply rechecks paths, unique exact old text and budgets. Empty old creates an absent allowed file, never replaces existing; creations share snapshots/rollback.

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

These controls describe the current development implementation. Saved tasks retain their recorded contracts; creation defaults do not migrate old sessions.

| API setting | Behavior |
| --- | --- |
| `delivery_files=["**/*.md", "**/*.pdf"]` | Require nonempty workspace outputs for an unqualified completed delivery. Relative patterns check presence, not quality; source PDFs do not count. Requirements are explicit, not inferred from prose; disclosed partial delivery remains possible. |
| `tool_contracts="outcomes"` (creation default) | Present complete professional operations first. Load low-level `write_file`, `edit_file` and `run_process` contracts when useful through `load_tools`; loading grants no permission. Explicit `full` exposes all allowed contracts; `on_demand` starts with registered request-visible tools and a capability directory. Saved modes remain unchanged. |
| `context_protocol="auto"` (creation default) | New operation tasks save `working.v2`: retain requests in order, recent complete turns and notes written by the current work owner through `update_work_plan.notes`. Earlier observations are supplied by explicit record references, with version and capacity status; unrelated history does not fill unused space. There is no background summary model call. Omission keeps notes across feedback; `[]` clears them. Notes remain judgments, not verified facts or permission. |
| `context_protocol="working.v1"` (compatibility) | Retains its original automatic, budgeted summary checkpoints and observation selection. Saved tasks, checkpoint failures, charges and recovery are not migrated. |
| `context_protocol="conversation.v1"` (saved/compatibility tasks) | Preserve chronological projection and original records. Older tasks are not silently upgraded. Explicit `None` retains the original legacy projection. Working-set targets remain soft; actual shared token allowance remains enforced. |
| `control_protocol="operations.v1"` (new tasks) | Ordinary `start`, structured intake and `ToolApplication.create` combine independent read/write, execution, inspection and publication operations in one action loop. New creation rejects `None` and legacy professional-controller options. Use `load` and `supply` to continue saved tasks under their original contracts. |
| `completion_policy="available_operations.v1"` (new operations tasks) | Keep operations available within actual resource and approval limits. A last affordable decision may retain useful nonterminal work. Exhaustion pauses with retained outputs; only an explicit validated delivery completes a request. Saved tasks retain their recorded closing/protected decision policies. |
| `report={"status":"canonical","manuscript":"report.md","formats":["markdown","pdf"]}` | A delivery identifies its editable manuscript and requested formats. New tasks bind `delivery_review_protocol=task.v1`: the delivery operation owns an independent review of the complete requested work and actual delivered inventory, on the same ledger. A narrow local check cannot fulfill this obligation. Requested publications must match the manuscript. |
| `limitations` / `unresolved` in delivery | Limitations describe bounded research uncertainty or unavailable measurements. Unresolved requested work or required defects make the delivery partial. Neither permits a false quality claim. |
| `summary_source={"path":"report.md","offset_bytes":0,"max_bytes":1024}` in delivery | Optional window of an actual delivered text file. After final work succeeds, its exact text replaces the free `summary`; the source must belong to the delivery and match its existing byte binding. Missing, unreadable or invalid UTF-8 windows do not fall back to free prose. Omit to retain the original free-description contract. An excerpt certifies neither scientific correctness nor representativeness. |
| `create_research_task(..., allow_vision=True)` | Strict boolean, default `False`. Requires the named vision route and connection; shares `services.budget_limits`. Neither prose nor an injected text client grants inspection. Grants no source network or image generation and changes neither saved permissions nor the return type. |
| `supply(..., replace_existing=True)` | Caller can explicitly replace `delivery_files`, `tool_contracts` or `completion_policy`. Unrelated supply preserves saved policy and ledger. |

Delivery follows the request: a Chinese Markdown explanation, code or tables can be final artifacts without a formal report claim. Request PDF/TeX only when needed. An explicit report claim still requires the current manuscript inspection; requested publication must match it. This does not certify automatic format selection or overall task success.

Submit a canonical report when it is ready for review; a separate preliminary `inspect_artifacts` call is optional. The delivery review retains original requests, feedback, manuscript, requested formats and every delivered file version. A verified complete pass can be reused while that scope and its supporting evidence remain current. Failed or incomplete work is retained; a new explicit delivery may request another review, while recovery never repeats an in-flight or failed call. Existing tasks without this saved protocol retain their original delivery checks. Read-only review cannot execute numerical recomputations and does not certify scientific correctness.

`inspect_artifacts` accepts decodable text targets; unsupported targets reject admission before a model call without creating an assessment. Use `inspect_images` for rendered images. The inspector can locate and read necessary sources inside authorized roots, sharing the original ledger. New `subjects.v3` work has no private six-round limit: it preserves each response and read, reserves final-opinion capacity and can report incomplete scope. An invalid final permits one correction. Saved protocols retain their original limits.

Raw supporting windows already read by another operation can be reused after exact receipt, current permission, file-version and byte-range validation. This includes actual reads from failed inspections, but never their rejected opinions or a prior target's pass. Reuse is optional within the common input capacity; it does not certify a claim. Editing a target invalidates its previous pass; an incomplete reassessment cannot fall back to it, and changed evidence cannot erase outstanding required findings. Unknown usage stops further requests. Image inspection separately requires authorization and a named vision route; compilation or text review does not certify visual or scientific correctness.

Budgeted image inspection requires an explicit `max_output_tokens` in the vision profile. Choose enough capacity for both reasoning and the review response; this setting is separate from the text profile, which can remain uncapped. See the [model catalog example](../examples/model-profiles/models.toml).

A frozen `report_requirement` still requires a manuscript claim, its fixed outline and citation ceiling. New tasks use independent operations rather than the legacy Writer's draft/review scheduling settings; `target_words` is a writing target, not a mechanically certified minimum.

Tool loading and resource policies do not guarantee completion, scientific quality or a particular cost. See [task budgets and evidence](EVALUATION.md#v210-development-evidence) before sizing a long report.

### Historical writing contracts, output binding and retained work

The `compose_document` options below apply to explicit compatibility integrations and saved professional tasks. New `operations.v1` tasks edit their manuscript directly, inspect the current version and export it; they do not use `report_protocol`, `professional_inputs` or `update_work_plan.report_intent`. Historical writing remains executable on its original ledger.

| Operation / field | Contract |
| --- | --- |
| Report composition and delivery | Interpret deliverables in the normal action batch, without a separate classification call. Use original materials and actual results; publish the adopted manuscript with matching assets. Converting existing prose adopts its current version without mandatory rewriting. A disclosed candidate remains partial. Frozen writing/execution requirements cannot be relaxed by a work plan. |
| `compose_document.professional_review={"max_revision_rounds":1}` | Independent inspection and at most one correction round; zero inspects without correction. New professional document work defaults to one round for either granularity; exact and historical calls retain their original defaults. Native report obligations declare their controls in `update_work_plan.report_intent`; inherited/frozen controls take precedence. |
| Feedback and figure inputs | The writing owner receives original feedback before selecting evidence and authors captions from actual materials/request; figure inputs are existing paths, not coordinator-authored authoritative captions. Implementation investigation also receives relevant original feedback. Initial/feedback writing shares ownership, evidence checks and budget. |
| Whole-article corrections | Return exact replacements bound to the manuscript version; an empty list adopts an unchanged valid candidate. Preserve original responses and earlier versions. Intact prior composition inputs can be reused for feedback. Unresolved required findings prevent accepted publication. |
| Retained author revisions | `compose_document.prior_result` can continue a complete checked but unadopted article or section revision, retaining its exact evidence and original negative inspection. Changed correction wording does not replace an explicitly selected pending revision with the older canonical draft. A local correction limit does not discard improved work or authorize automatic retries. The request owner chooses the next action; accepted publication still requires resolved findings. A proved semantic handback may resume a saved pause without a quota refill, while failed or unknown operations remain stopped. |
| Attachment inspection | New professional document work preserves complete selected files separately from model inspection. Text shares a 256,000-byte view; supported CSV/TSV/JSON tables can use SHA-bound profiles from the existing complete-table reader (20 MiB source limit, 96,000-byte combined profile view). Profiles expose counts and bounded examples, not per-value or scientific verification. Unread attachments retain explicit gaps and block accepted delivery while a draft can still be saved. Existing subject/copy limits and historical contracts remain. |
| `compose_document.adopt_draft=true` | Check and adopt `prior_draft` without a drafting request; requires article writing and independent professional review already enabled. The body has its own source receipt, not a fabricated model response. `reuse_prior_inputs` requires an intact same-task producer; external drafts do not acquire that authority. Frozen controls and exact-replacement correction still apply. |
| `run_process.inputs` / `outputs` | Optional file inputs and workspace outputs: up to 64 each, 64 MiB per binding. Approval pins inputs; successful execution records output bytes. Writing/publication rejects a declared producer with changed inputs/outputs; regenerate with a new invocation. No inferred dependencies, coverage of undeclared files, extra permissions or retroactive binding. |
| Process failure / preflight | Nonzero exits or stops return errors with exit code/logs. Invalid proposals return the professional owner's diagnosis without execution. Historical observations remain unchanged. |
| `app.run()` / `app.result_snapshot()` | Include `retained_work`: intermediate workspace files, last operation and continuation guidance. These are not accepted deliveries. Failed writing may retain a candidate; rejected inspection may retain individually validated observations in `incomplete_review.json`. Correct from original evidence as a new version; unfinished inspection stays unresolved. |

Composition checks CommonMark image links before registering the canonical result; export retains path and compilation checks. Section writing refuses a known outline that cannot afford even one request per section before paid material selection, but that lower bound does not guarantee completion. Provenance, arithmetic and prose inspection do not certify visual or scientific correctness.

### Trusted services, snapshots and legacy APIs

Services may inject a text client, independent reviewer and `SearchProviderRegistry`. Clients implement `with_budget` and `ask_json` or `chat_turn`, binding the actual ledger, session and attempt. Reload with `ToolApplication.load(path, services=services)` before further service work. Persistence retains type/model identities and provider names, not credentials or executable objects. Missing dependencies fail rather than fall back to environment clients or built-in providers. Custom implementations are caller-trusted, not sandboxed or independently identity-verified. Prefer named connections for CLI/restart portability; `model="env"` selects the configured default.

`snapshot_sources=True` retains selected regular files only, bounded individually by `max_snapshot_bytes` (default 16 MiB), with hashes binding retained bytes. It does not copy a whole directory. Legacy v1 import uses those copies, preserves parent links and originals, and records historical budget as `unknown_not_imported` rather than importing or resetting it. Continue the new task with `start --resume-task`; originals retain their own recovery contract.

New structured CLI tasks use the common creation API and reject unsupported declarations before creation. `create_session` / `ResearchApplication.create` remain explicit compatibility APIs with their original `view` / `advance` and historical-run contracts. Initialized-run adoption and producing-source baseline lineage require their original records; copying directories or bare results cannot replace them.
