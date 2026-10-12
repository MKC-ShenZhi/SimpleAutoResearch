# Usage And Configuration

[中文版本](USAGE_zh.md)

This document explains how to install, configure, and run SimpleAutoResearch.
It is the practical user guide; workflow concepts and artifact details live in
[Workflows And Artifacts](WORKFLOWS.md), command details live in
[CLI Reference](CLI_REFERENCE.md), and TOML fields live in
[Configuration Reference](CONFIG_REFERENCE.md).

## Start with a task

`uv run simple-ar start` asks for a natural-language goal and enters the experimental
native tool loop. Supply read-only materials with repeatable `--source PATH`; public
retrieval needs `--allow-network`, and command proposals need `--allow-execution`
plus separate approval. See [native entry](CLI_REFERENCE.md#native-task-entry-experimental)
for retained budgets and continuation. Complete cross-task acceptance is still pending.

The closing result panel shows the saved workspace, pending action and ledger
balance. A retained delivery record may precede the current feedback; saved files,
terminal status and available request slots do not prove acceptance or that the
next model request fits the remaining token allowance.

New native tasks expose the full authorized tool contracts. The model can combine
workspace, sources, code, analysis, writing and publication capabilities in the same
action loop. Historical on-demand tasks retain their saved contract selection on
continuation. Tool availability does not grant execution or network access or
establish that a task has succeeded.

Add material to the same task with `simple-ar start --resume-task SESSION "Use these notes"
--source notes/ --yes`. Access flags can also be explicitly added on continuation;
this preserves completed results and the original budget, rather than starting over.
If a native model decision has terminated with no accepted reply, explicit
`--resume-task SESSION --retry-model` can use its remaining budget without
repeating completed tools. It retains failures and bounded unknown reservations;
it is not a remedy for in-flight operations, unbounded charges or exhausted budgets.
See the CLI reference for the recovery conditions and separate authorization steps.

For a supplied Python project, the native task can inspect instructions, dependency
declarations and entry candidates through the existing preparation module. Its
overview links to a retained full observation; it does not import the project,
install dependencies or execute tests. Dependency metadata belongs to the
inspecting interpreter, not necessarily the separately selected task Python.
The final `write_file` operation can carry an explicit `delivery` with its summary,
artifact paths and unresolved work. The task validates those paths and saves the
same delivery manifest after a successful write, without a separate model turn.
Writing a file alone does not complete the task or certify its contents.

For a new implementation, the native task can prepare related source files and
checks together through the existing generator, using the catalog's `code` route
and the same task budget. Original materials remain unchanged; the candidate,
source bindings and unresolved gaps are saved. Execution still needs approval,
and syntax validation alone does not establish correct behavior or delivery.
A goal alone is sufficient: the code owner can choose new file paths without a
supplied repository, coordinator-selected list or mandatory entry filename.
An existing project adds its bounded inventory and source context to that same
selection path; explicit files and source windows remain supported.
For an existing-file change, a planned output may specify `source_path` and its
observed `source_sha256`. The professional call uses the complete bounded original
to propose exact replacements, preserving unrelated text in a separate candidate.
New files still use full content; oversized sources or combined context are rejected
explicitly rather than rewriting a source from truncated input.

Observed text files can be copied into the workspace by source digest. This
includes generated candidate files, avoiding manual content retransmission when
assembling or revising a runnable project. Replacing an existing destination
still requires its current digest and keeps the ordinary backup; original mounted
sources remain read-only. Copying does not authorize execution or verify a repair.

Native tasks can inspect bounded CSV/TSV or record-array JSON tables and use the
existing deterministic analysis capability for supported summaries and explicit
paired comparisons. The package includes copied input, figures and a rebuildable
result. Row meaning, units and pairing still need evidence or clarification;
observed counts do not establish independent samples or statistical significance.
Unsupported transformations and methods use the existing code/execution path,
with its separate authorization. Writing should consume calculated results, not
replace them with mental arithmetic.

Native composition can use selected windows from supplied text, tables or saved
results together with retained literature passages. Existing figures are copied
beside the editable `manuscript.md` with relative paths; feedback creates a new
version and preserves its inputs. This does not calculate statistics or verify
figures: request the necessary calculation and checks before relying on conclusions.
The manuscript remains the input to the existing publication tool.
Figures may supply an explicit caption together with their path. That caption
belongs to the image, while surrounding prose discusses its meaning; publication
owns numbering. An exact adjacent repetition is removed during export, not
arbitrary similar discussion. Legacy figure paths remain valid.
Keep it and its figure assets in a focused authoring directory: native publication
pins the authorized local image inventory, bounded to 4,096 directory entries and
256 MB of images, excluding derived publications. Changing a local figure requires
new approval even when the text is unchanged. Private assets are not copied;
compilation failure retains the source, failed version and diagnostics.

For a PDF that needs visual inspection, the native publication tool accepts
`render_preview: true` with compilation. It derives TeX/PDF from the same Markdown
and renders every page in the same approved operation, returning image locators
for `inspect_images`. This bounded preview supports up to 12 pages at 120 DPI,
scaled to at most 1,600 pixels per dimension; larger documents are reported rather
than silently truncated. Rendering is not a visual review. Requested incomplete
previews prevent delivery, while the compiled PDF and diagnostics remain available.

### Outcome preferences and structured compatibility

`uv run simple-ar start --guided` offers outcome preferences. Ordinary survey,
writing, reproduction, repair, data, figure and code preferences use the same
native owner as a plain request. Supply a goal and common read-only material,
project or data paths; no validator, edit scope or full measurement protocol is
required. Optional public-source/process-proposal access is explicitly confirmed,
and each process command still needs approval. `--yes` accepts only the displayed
summary; `--prepare-only` makes no model/process call.
See [preference inputs](CLI_REFERENCE.md#goal-driven-code-analysis-and-figures).
Explicit controls select compatibility, rather than the preference name:

| Function | Bring | Execution boundary |
| --- | --- | --- |
| `survey` | Explicit `--sources`/retrieval controls; optional papers | Retains the declared source policy and report protocol; model needed |
| `bug_fix` | Project, allowed paths, validation command | Bounded edits and actual validation; prepared dependencies |
| `reproduction` | Paper, existing project/data, fixed protocol/command | Specified conclusion; current environment or explicitly selected task venv, no automatic data retrieval |
| `writing` | Explicit `--template` or report controls; notes/draft or analysis package | Retains the requested structured writing protocol |
| `data_analysis` | Explicit value/x/baseline column selectors without `--scripted`, or physical table controls | Deterministic table shortcut; descriptive statistics and declared plots, no API unless `--with-report` requests writing |

`--chat`/`--resume-setup` retain technical structured setup, and
`--from-session`/`--reuse` retain registered result/scope reuse. Even an explicitly
supplied default such as `--interaction checkpoints` selects compatibility;
strict controls cannot mix native access/budget flags. `figure` and `--scripted`
analysis reject incompatible structured controls before task creation.
`--prepare-only` saves task configuration without execution or API calls.
For code repair in an already prepared project environment, add
`--project-python /path/to/project-venv/bin/python` (Windows:
`C:\path\project-venv\Scripts\python.exe`). This saves the existing CodeTask
external-interpreter setting; no manual TOML edit, environment creation or
installation is needed. Omission keeps the current environment. Interpreter selection
also applies to native execution; it does not change the assistant's own Python requirement.

For model-assisted technical protocol setup, use `start --chat`: it asks about
meaningful choices, then proposes settings for the structured configuration path. This
opt-in setup uses your configured model, sends replies and supplied asset previews,
and requires confirmation. `--resume-setup PATH` resumes its draft without resetting
setup usage. It is not a general coding Agent; prepared reproduction/code execution
still requires explicit commands and scope. It cannot be combined with `--prepare-only`.
Unresolved optional settings stay out of the draft configuration until clarified;
the complete confirmed configuration is still validated before task execution.
If correction still fails, clarify the task or enter `stop` to save and exit.
Paired analysis needs baseline and candidate numeric columns in the same row;
long-format method rows must be matched and reshaped beforehand.
For a first no-API run, use
`uv run simple-ar research-session --config examples/data-curves/research.toml`.
Saved analysis packages can then become writing material. `report-export` exports
a saved structured report attempt; native manuscripts use publication in their
task. See [CLI](CLI_REFERENCE.md).

Data column selection in a terminal shows column names and sample values. For
explicitly matched runs, try [paired observations](../examples/data-paired/README.md),
which computes differences and their standard error without model calls.
Before preparing a reproduction command, `project-info --project PATH --output
runs/preparation-NEW` reads project instructions, dependency declarations and entry
candidates into reusable notes. It does not install, download or run the project.
Static `[project.scripts]` declarations also prioritize indexed root/src module
sources within the existing entry-reading allowance, even without a main guard.
Unresolved or ambiguous source locations stay visible; an entry declaration is
not proof that its executable is installed or its callable works.
Static pyproject/requirements declarations are checked against this Python's distribution metadata,
not imported; a matching version is not a successful import or reproduction run.
Requirements installer directives, included files and partial lines remain unresolved.
For source-assisted preparation, add `--chat --kind reproduction --project PATH
--document PAPER --goal "..."`. The model can follow indexed source reads and propose
argv/metrics/protocol; inspect and confirm these before the separate execution step.
This does not locate an arbitrary implementation. A single Python command may
explicitly select `--environment venv` and project-relative `--requirements PATH`
before `--command`; execution creates the task venv, installs selected files and
runs `pip check`. Add `--install-project` to also install the project package in the
same resolver call (default off, not editable; build code/network/source metadata writes
are possible). Default remains current environment. Setup only saves the choice;
see [scope, budgets and recovery](CONFIG_REFERENCE.md#guided-setup).

Structured tasks use their saved task TOML; native tasks retain their confirmed
configuration in the session. Named connections/routes live in `models.toml`,
with credentials resolved from the environment; legacy environment-only connections
remain a fallback. Ordinary use need not set every
advanced option below. See [guided setup](CONFIG_REFERENCE.md#guided-setup).

## Requirements

- Python 3.12 or newer.
- `uv` for dependency management.
- An OpenAI-compatible API key if you want LLM-backed planning, notes, synthesis, report writing, or code edits.

## Installation

Data figures use Matplotlib's font configuration. For Chinese or other non-Latin
labels, install a font covering those characters and configure `font.family` in
your `matplotlibrc` (for example, `DejaVu Sans, Noto Sans CJK SC` when both are
installed). The preview does not download fonts automatically. Missing-glyph
warnings mean PNG/PDF labels need checking; successful analysis is not a visual
quality check. See [Matplotlib's font fallback guide](https://matplotlib.org/stable/users/explain/text/fonts.html).

For graph-like method diagrams, optionally install [Graphviz](https://graphviz.org/download/)
on the execution host and make `dot` available on `PATH` (Debian/Ubuntu:
`sudo apt install graphviz`). Generated code can use this mature layout renderer;
the task should check its actual availability through an authorized command.
It is not required for data plots; setup does not install it automatically, and
rendered labels and feedback arrows still need visual inspection.

Clone the current preview branch (also shown in the [README](../README.md)):

```bash
git clone --branch feat/v2.9-task-driven-research https://github.com/Wchanging/SimpleAutoResearch.git
cd SimpleAutoResearch
```

Install dependencies:

```bash
uv sync
```

Check the CLI:

```bash
uv run simple-ar --help
```

## Environment Configuration

Use a shared named catalog for ordinary multi-task setup. Copy
[the example](../examples/model-profiles/models.toml) to
`~/.config/simple-ar/models.toml`, or a private path and set
`SIMPLE_AR_MODELS_CONFIG` to its absolute path. Set your provider's actual URL,
model and API mode in each profile; `api_key_env` references a credential variable
whose value stays in the environment or private `.env`. The example references
`CCTQ_API_KEY`; a profile can name your provider's corresponding variable instead.
Project paths, data, interpreters and task limits remain task inputs.

Configure `default` for ordinary text tasks and `code` for code work. Optional
`text`, `writing`, `vision` and `image` routes share this catalog across tasks;
native writing without its own route retains the task connection. `start` displays
text connections and offers a terminal selection; `--model profile:NAME` selects
explicitly. `--model env` means the catalog's text/default route when configured,
and the legacy environment connection otherwise. It does not bypass a catalog.
Run `simple-ar models` to inspect local configuration without API calls; it checks
credential presence and validates catalog declarations, not provider connectivity.
Native decisions require Chat; structured text workflows can also use Responses.
See [named connections](CONFIG_REFERENCE.md#named-model-connections).

For the compatible environment-only route when no catalog is configured:
Create a local `.env` file only if one does not already exist. Do not overwrite your current connection settings:

```bash
cp .env.example .env
```

On PowerShell:

```powershell
Copy-Item .env.example .env
```

Legacy model connection example; replace values with your provider's settings:

```bash
OPENAI_API_KEY=your_api_key
OPENAI_BASE_URL=https://your-provider.example/v1
SIMPLE_AR_MODEL=your_model_id
SIMPLE_AR_LLM_API=chat
SIMPLE_AR_LLM_STREAM=true
```

Transport and recovery notes (optional expert reference; `SIMPLE_AR_LLM_*`
connection settings below apply to the legacy route, not named profiles):

- `OPENAI_API_KEY` is required for the legacy LLM connection; named profiles use their `api_key_env` variable.
- `OPENAI_BASE_URL` can point to OpenAI or a third-party OpenAI-compatible `/v1` endpoint.
- Without a catalog, `SIMPLE_AR_MODEL` supplies the model name for `research-session --model env`;
  omitting `--model` when starting a session explicitly selects deterministic mode.
- `SIMPLE_AR_LLM_BACKEND` controls the transport implementation. The default
  `openai` uses the OpenAI Python SDK directly; `litellm` keeps the older
  LiteLLM compatibility layer, available with `pip install 'simple-autoresearch[litellm]'`.
  The default SDK transport does not require that extra.
- `SIMPLE_AR_LLM_API` controls the request shape. `responses` sends Responses
  API-style `instructions` plus `input` and retries transient failures on that
  API only; `chat` sends Chat Completions-style `messages` directly. The
  explicit `auto` mode tries Responses and then Chat after bounded retries for
  compatibility with gateways that expose only one surface.
- `SIMPLE_AR_LLM_STREAM=true` uses streamed Chat Completions when
  `SIMPLE_AR_LLM_API=chat`. The client assembles chunks before parsing and
  records provider usage when the gateway supplies it; Responses calls remain
  non-streamed. A finished choice plus final usage completes collection without
  waiting for connection closure. Interruptions report received character counts
  and terminal/usage state, not response text. This can avoid long non-stream connection stalls on compatible
  gateways, but it does not remove provider or client timeouts.
  Streaming requests ask for uncompressed transport. If generation explicitly finishes but the
  accounting tail fails, completed text is retained with estimated usage; an incomplete stream
  without a finish marker remains an error. Cancellation records unknown usage and closes the
  SDK connection. Retries display their attempt number and delay.
- `SIMPLE_AR_CHAT_TOKEN_LIMIT_PARAM` optionally selects `max_tokens` or
  `max_completion_tokens` for Chat Completions; `auto` selects from the model
  name when possible.
- `SIMPLE_AR_LLM_REASONING_EFFORT` is an optional documented model capability
  such as `low` or `high`, forwarded only to Chat Completions through the
  provider extension field. `SIMPLE_AR_LLM_REASONING_OUTPUT_TOKENS` supplies a
  fallback cap only when neither the caller nor client settings provide one;
  it never overrides an explicit per-call limit.
- `SIMPLE_AR_LLM_THINKING` optionally sends `thinking.type=enabled` or
  `thinking.type=disabled` through Chat Completions `extra_body`. It is unset
  by default. Use it only with a provider/model that documents the switch;
  forced-thinking models cannot be disabled. Do not combine `disabled` with
  `SIMPLE_AR_LLM_REASONING_EFFORT`.
- `SIMPLE_AR_LLM_TIMEOUT_SEC` defaults to 180 seconds per provider attempt.
  This is passed to the transport client; it is not a guaranteed end-to-end
  wall-clock deadline for a stream that continues receiving events. Retries can
  also make a logical call take longer. Stream interruptions retain uncertain
  usage, not a confirmed zero charge.
  Set a larger positive value for a slow provider, or explicitly set
  `0`/`off`/`none` to disable the client-side timeout.
- `SIMPLE_AR_MAX_OUTPUT_TOKENS` is optional. Leave it empty, or set it to
  `0`/`off`/`none`, to omit provider output-limit parameters; set a positive
  value only when you deliberately want to bound response size.
- `SIMPLE_AR_LLM_RETRY_ATTEMPTS` and the retry delay settings control bounded
  exponential backoff for transient provider errors such as connection resets,
  rate limits, timeouts, 5xx responses, and gateway errors such as Cloudflare
  524 origin timeouts.
  SDK Chat streams distinguish connection/pool acquisition failures before
  dispatch from read/write failures or interruptions after possible dispatch.
  Proven pre-dispatch failures consume one request attempt and zero tokens;
  an uncertain uncapped stream stops without automatic replay or API fallback.
  Capped text retains its conservative reservation before an eligible retry.
  A higher retry count does not override this distinction or resolve provider overload.
- `SIMPLE_AR_LLM_MAX_WORKERS` is an optional ceiling applied to each model-call
  batch in the process. Leave it unset for per-stage defaults; set it to `1`
  for a rate-limited provider without changing the research-session protocol.
- Online research calls stop after the configured bounded provider retries and
  preserve the failed attempt. Research TOML has no `[llm].allow_fallback`
  switch; set `[model].name = ""` for deterministic offline processing, and do
  not label its output as model-generated analysis. `--no-llm` is a CodeTask
  primitive option, not a research-session flag.
- To resume unchanged saved inputs after a configuration file has been edited,
  use `simple-ar research-session --session-root PATH --model env` (or
  `--model MODEL`) without
  `--config` or `--topic`. The saved goal, evidence, execution, and budget are
  restored; explicit changes remain subject to the normal resume checks.
  A model-backed session cannot resume without `--model`: the CLI fails before
  changing saved state, rather than silently switching to deterministic work.
- If the prior process was interrupted while an attempt was running and no
  capability result was saved, first confirm that worker has stopped. Then use
  `simple-ar research-session --session-root PATH --recover-interrupted --model MODEL`.
  This records one failed attempt against the existing budget and continues
  the saved plan; it does not erase completed evidence. Do not use this flag
  while another worker may still be active or combine it with input revisions.
- `SIMPLE_AR_JSON_RESPONSE_FORMAT` controls provider-native JSON mode for
  structured calls. The default `auto` tries `response_format={"type":"json_object"}`
  and falls back only when the provider explicitly rejects that format; ordinary text
  calls are unchanged. `off` retains prompt-only parsing for providers that require it;
  `json_object` always sends the format. Existing `.env` files with `off` keep that choice.
- Price fields are optional and only affect cost estimates in usage summaries.

Provider-side generation success does not prove that the client received the complete response.
For interrupted streams, inspect `content_chars`, `finish_reason`, and `usage_received`, then check
the execution host's network route instead of relying on more retries or disabling timeouts.
If a proxy is needed, configure a reachable `HTTPS_PROXY` (and `HTTP_PROXY` / `NO_PROXY` as needed)
in the running process. A personal computer's `127.0.0.1` address is not that proxy on a remote server.
Temporary SSH forwarding stops working when the tunnel closes; it is not an independent server deployment.
Preserve the session and usage records, and confirm the old process has stopped before resuming.

Named connections use the profile's `stream`, `request_timeout_sec`, and `retry_attempts`, not
legacy `SIMPLE_AR_LLM_*` connection settings. Inspect selection with `simple-ar models` first.
Streaming can still wait a long time for the first response headers. Small prompts do not guarantee
low latency: use the real profile timeout (180 seconds by default) for connectivity checks rather
than silently shortening it. This is a transport timeout, not an overall wall-clock deadline;
unlimited waiting or retries are not a remedy.

Diagnose host/connection selection, authentication/model listing, request/response headers, first
SSE event, completion/usage, parsing, and saved artifacts/accounting separately. A successful model
list does not validate generation. Finish with uninstrumented native calls, longer input/output and
resume checks; distinguish diagnostic-wrapper failures from product failures.

## Choose a task and its smallest input

For ordinary work, `start` accepts a goal and materials and chooses necessary
tools. The examples below use the explicit structured interface:
`research-session` accepts a goal, available materials, and explicit execution
conditions. The accepted plan selects the applicable capabilities; users do not
need to write an internal stage list. Use these supported shapes:

| Need | Minimum configuration and command | What it does not assume |
| --- | --- | --- |
| Online survey | `[task] outputs=["report"]`, `[research] providers=[...]`; `research-session --config survey.toml` | No experiment process or code project |
| Analyse supplied papers/notes | Add `[research] materials_only=true` and `[assets].papers=[...]` | No web search or fabricated search result |
| Write from notes/results | `[task] kind="writing"`, `outputs=["report"]`, `[assets].materials=[...]`, `[model] name="env"` | No new synthesis, search or experiment; analysis JSON must be a completed supported package |
| Describe/plot supplied data | `start --kind data_analysis --data-file PATH --value-column NAME` or the [data curves case](../examples/data-curves/README.md) | Deterministic shortcut: no model calls, arbitrary inference, smoothing or scientific validation |
| Repair an existing project | `[task] kind="bug_fix"` plus `[execution] code_task_config="code_task.toml"`; run the research config | No literature or baseline unless the task explicitly supplies it |
| Reproduce or measure an existing script | `[execution] command=["python","measure.py"], cwd="/abs/project", primary_metric="accuracy"` and a finite process budget | No automatic repository discovery or seed expansion |
| Improve an existing research project | Use `[task] kind="auto"` with `execution.code_task_config`, protected assets, benchmark and protocol; the [continual-learning case](../examples/continual_learning/README.md) is the prepared example | No dependency install, dataset download, or scientific success claim |
| Independent code work | `simple-ar code-task init --config code_task.toml` then `simple-ar code-task execute ...` | No research report lifecycle |

The following are four advanced, independent complete research TOML examples. Save each as a
file before running it; do not concatenate the sections. Relative paths resolve
from that file's directory.

### 1. Supplied-material analysis (no search)

```toml
[task]
goal = "Compare the claims and limitations in the supplied papers."
outputs = ["report"]

[model]
name = "env"

[research]
materials_only = true
max_chunks = 32

[assets]
papers = ["notes/method-a.md", "notes/method-b.md"]

[report]
template = "survey"
```

Run from the directory containing `research.toml` with
`simple-ar research-session --config research.toml`. Both read-only files must
exist; no search or experiment process is created.

### 2. Online literature survey

```toml
[task]
goal = "Survey small-memory continual learning for image classification."
outputs = ["report"]

[model]
name = "env"

[research]
providers = ["openalex", "semantic_scholar", "arxiv"]
max_results = 8
max_chunks = 32
interaction = "checkpoints"

[budget]
process_invocations = 0
process_wall_seconds = 0

[report]
template = "survey"
```

Run it from the repository root or another directory containing the file. This
uses configured providers and the current environment's LLM settings; it does
not discover a code project or start an experiment.

### 3. Existing-project bug repair

```toml
[task]
goal = "Fix the phrase-aware classification bug described by the task file."
kind = "bug_fix"
outputs = ["bug_fix"]

[model]
name = "env"

[budget]
process_invocations = 1
process_wall_seconds = 120

[execution]
code_task_config = "examples/code_task_medium_review/configs/code_task.toml"
```

Save this file at the repository root and run
`simple-ar research-session --config bugfix.toml`. The referenced CodeTask file,
its `task.md`, project files, benchmark and declared edit scope must exist. The
implementation runs in an isolated workspace and the bug-fix path does not run
a baseline or write the original fixture.

### 4. Direct CPU measurement

```toml
[task]
goal = "Measure the checked-in digits benchmark without changing its source."
kind = "measurement"
outputs = ["experiments"]

[model]
name = ""

[execution]
command = ["python", "benchmark.py"]
cwd = "examples/code_task_digits_mlp/project"
timeout_sec = 60
primary_metric = "accuracy"

[budget]
process_invocations = 1
process_wall_seconds = 60
```

Save at the repository root and run
`simple-ar research-session --config measurement.toml`. The explicit `outputs`
already excludes a report; do not combine it with `--no-report`. Direct argv
uses the process `PATH` and has no CodeTask interpreter policy; use an absolute
interpreter in `command` if `python` is not the intended environment. The
benchmark is a small CPU fixture, not evidence of a scientific improvement.
The `measurement` task kind executes the supplied command and analyzes its
result without literature search, idea generation, or research design. Use the
ordinary research task kind for a new candidate or an evidence-driven comparison.

For all four files, inspect the printed session path with
`simple-ar status RUN_DIR`. Resume or inspect a paused decision using
`simple-ar research-session --session-root RUN_DIR` and the documented decision
or continuation options. A direct `execution.command` and
`execution.code_task_config` are mutually exclusive; do not combine them.

## Research session: task to evidence

The formal user entrypoint is `research-session`. It accepts the task and assets,
creates a short accepted plan, and invokes only the capabilities justified by
that plan. Literature ingestion, CodeTask preparation/implementation, experiment
measurement, analysis, report writing and audit are separate owned boundaries;
the application reconnects them through declared artifact references. The
execution command, baseline policy, dataset, code scope and resource limits
remain explicit user/configuration inputs. This is bounded work, not an
unlimited autonomous loop.

For a literature-only session, omit both the execution command and
`--code-task-config`. Without a model, the session completes at the evidence-backed summary; with
a model, it can continue through the research-only report path. No experiment process or execution
budget is created in this form.

For a laptop-safe offline complete smoke:

```bash
uv run python scripts/research_session_smoke.py
```

See `examples/README.md` for the bounded network, LLM, and prepared-project path. On failure,
preserve the session directory and inspect the attempt; do not replace a real failure with fixture
output.


## Retrieval And Artifact Tools

Use these when you want to inspect or search files produced by a run:

```bash
uv run simple-ar inspect runs/<run-id>
uv run simple-ar search-artifacts runs/<run-id> "accuracy"
```

See [CLI Reference](CLI_REFERENCE.md#artifact-tools) for option details.

## Tool And External Agent Handoff Preview

An internal common tool and agent-handoff layer is available as a controlled
extension point for future Codex, Claude Code, OpenCode, OpenAI tool-calling, or
MCP adapters; it is not a new default runtime path.

Current behavior:

- registered tools are real local report/experiment tools, not placeholder MCP
  stubs;
- schema export is available for OpenAI-style and MCP-style tool definitions;
- default permission policy is read-only/plan-only;
- external-agent handoff packages are written under
  `runs/<run-id>/agent_handoff/<name>/`;
- backend outputs, when collected, go to `runs/<run-id>/agent_outputs/<name>/`
  and still require SimpleAutoResearch validation before they can affect a
  patch, result, or report.

The handoff package is deliberately explicit:

```text
runs/<run-id>/agent_handoff/<name>/
  instructions.md           # task + backend profile + permission summary
  tool_schema.json          # exported real tool schemas
  permission_policy.json    # write/shell/network/secret policy
  artifact_handles.json     # reviewable run artifacts exposed to the backend
  expected_outputs.json     # canonical files the backend may produce
  workspace_manifest.json   # compact run/workspace view
  context/
```

External tools remain optional strong-path adapters. Local research, report,
greenfield experiment, and code-task workflows continue to work without Codex,
Claude Code, OpenCode, or an MCP server.

Runnable backend wrappers are available behind this boundary:

- `fake`: deterministic dry-run backend used for integration tests;
- `local_llm`: uses the configured LLM to produce bounded review artifacts;
- `codex`, `claude_code`, `opencode`, `external_cli`: optional CLI backends.

Set `[implementation].provider` to one of those names when experimenting with
agent-backed greenfield generation or repair. External CLI providers also
require `[implementation].allow_external_agent = true`. Use
`[implementation].agent_binary`, `.agent_args`, and `.agent_timeout_sec` when
the executable is not on `PATH` or needs provider-specific flags. Even then, the backend
may only write candidate files inside the handoff directory; SimpleAutoResearch
copies them into the run workspace, then runs the normal code review, result
guard, benchmark, or code-task validation gates.

`[implementation].agent_mode` is the only mode switch for this layer:

- `model`: keep SimpleAutoResearch as the harness and use a local/model backend
  only for bounded generation.
- `handoff`: write an auditable handoff package for Codex, Claude Code,
  OpenCode, or another external CLI, then ingest candidate files back through
  SimpleAutoResearch gates.
- `delegated_workspace`: reserved for a future strong path where an external
  harness owns the workspace loop. The value is recognized today, but execution
  fails explicitly instead of silently falling back.

Run-local read-only tools can also be exposed over MCP stdio:

```bash
uv run simple-ar tools schema --format mcp
uv run simple-ar tools call runs/<run-id> list_experiment_artifacts
uv run simple-ar tools serve-mcp runs/<run-id>
```

For a Codex/MCP integration, configure `[implementation].provider = "codex"`
and leave `[implementation].agent_model = ""` by default so Codex CLI can use
the model configured for your account. Set `agent_model` only when you have
confirmed the model name is supported by that CLI/account.

## Code Task Workflow

The code-task workflow prepares an isolated editable workspace and never mutates
the original source project. Existing-project tasks default to `auto`, which
prefers `git_worktree` for committed Git projects and falls back to `copy` with
a recorded reason and next-step hints. It also supports explicit `copy`,
explicit `git_worktree`, and experimental `sparse_copy` for small allowlisted
subsets. Greenfield tasks use
`kind = "greenfield"` and default to an `empty` workspace where the generated
project is written. The workflow is intentionally step-by-step so each stage can
be reviewed.

Initialize from a TOML config so project paths, benchmark metrics, workspace
mode, model routing, and edit budgets stay in one reviewable file. The bundled
standalone example uses the medium review pipeline:

```bash
uv run simple-ar code-task init --config examples/code_task_medium_review/configs/code_task.toml
```

That example runs `python main.py --config configs/experiment.json
--show-progress`, prints newline progress bars during baseline/patched runs,
and uses `[execute].stream_benchmark_output = "auto"` so `code-task execute`
relays benchmark progress while still saving stdout/stderr artifacts. The
`auto` mode handles both normal `print` logs and carriage-return progress
output such as `tqdm`.

`init` creates one run directory with this core layout:

```text
runs/<run-id>/
  manifest.json                 # benchmark, workspace, environment, safety policy
  code_task/
    task.md                     # task prompt
    workspace/                  # isolated copy/worktree root
      ...                       # project root may be a subdirectory in monorepos
    meta/
      codebase_index.json       # file-level code index
      repo_map.json             # layered symbol/repo map
      repo_map_summary.md       # human-readable repo-map summary
```

It does not run code, call the LLM, or modify the original source project.

For a standalone from-scratch project, use the same code-task command with
`kind = "greenfield"` and no `code_root`:

```bash
uv run simple-ar code-task init --kind greenfield --task-file task.md --benchmark-command "python generated_project/main.py"
uv run simple-ar code-task execute runs/<run-id> --to-step run
```

In this mode, `execute` uses the shared code-task memory, reviewer,
validation, runner, and repair artifacts, but the implementation step generates
`code_task/workspace/generated_project/` instead of applying a patch to copied
source files.

Generation builds a separate candidate before replacing this directory. Previous
projects remain beside it as `.generated_project.previous-*`; failed candidates
remain as `.generated_project.candidate-*` for diagnosis. A generation failure
does not erase the delivered project. These retained directories are not active
outputs and are not deleted automatically. This protects project files, not an
atomic transaction covering all planning metadata or a second session runtime.

Before planning the greenfield implementation, execute writes
`code_task/meta/dependency_advice.json` and `.md`. It scans the active Python
environment, records a full installed-package snapshot in JSON, and prints the
task-relevant subset in the terminal. The built-in dependency catalog is used as
semantic hints, not as a whitelist, so packages installed for your server task
can still be surfaced to the planner. This is advice-only: the command may show
an optional `uv add ...` suggestion for a stronger implementation path, but it
will not install dependencies or mutate the environment for you.

When `workspace.mode = "auto"` is omitted or selected explicitly, existing
projects first try a detached git worktree. If Git cannot be used safely, the
run falls back to a guarded copy and records `requested_mode`, `selected_mode`,
`fallback_reason`, and `user_next_steps` under `manifest.json.workspace`.
The explicit `start --kind bug_fix --allow PATTERN --validate COMMAND` protocol uses this same strategy: a clean Git
project retains committed files regardless of the copy-size limit, while an
uncommitted or non-Git project uses a guarded copy of its current state. Copy
omissions are listed in the manifest; the copy limit is not a source-reading
window and the resulting subset must not be mistaken for a complete repository.

When `workspace.mode = "git_worktree"` or `--workspace-mode git_worktree` is
used explicitly, `init` creates a detached git worktree at
`code_task/workspace/` and stops with an actionable checklist if Git isolation
is not possible. `code_root` may be either the repository root or a project
subdirectory inside a larger repository. In the subdirectory case,
SimpleAutoResearch creates the worktree at the repository root and uses the
matching subdirectory as the editable project root for indexing, editing, and
benchmark execution. It records git provenance under `manifest.json.workspace`
and keeps `.git`/`.env` metadata out of the codebase index and model context.
It still does not install dependencies.

If `git_worktree` init fails, the CLI prints a checklist instead of a Python
traceback. The usual fixes are: pass a path inside the intended baseline Git
repository as `--code-root`, create an initial local commit with `git init`,
`git add .`, and `git commit -m "initial baseline"`, or switch to
`--workspace-mode copy` when the current filesystem state should be included
without committing first.

When `workspace.mode = "sparse_copy"` or `--workspace-mode sparse_copy` is
used, init copies only selected files. Configure patterns with
`[workspace].include` / `[workspace].exclude` or repeated
`--workspace-include` / `--workspace-exclude`. Built-in exclusions still block
`.git`, virtualenvs, `runs`, cache/build directories, `data`, `models`, `.env`,
and secret-like paths. This mode is useful for small known subsets, but it can
omit runtime dependencies; prefer `auto` or explicit `git_worktree`/`copy` for
general projects.

Use `[edit_scope]` when the workspace contains files that may be read but must
not be changed by the model. `[workspace]` controls what is copied or mounted;
`[edit_scope]` controls what later work-plan, proposal, repair, and apply gates
may modify.

```toml
[edit_scope]
# Empty allowed_patterns means every non-protected workspace path may be edited.
allowed_patterns = ["review_pipeline/**", "main.py"]

# These are added to the built-in protected tests/benchmarks/.env/secrets list.
protected_patterns = ["configs/locked/**"]
```

Benchmarks should print stable numeric metric lines. Supported formats are
`name: value` and `METRIC name=value`; the prefixed form is useful for generated
or external-agent projects because it is visibly machine-readable. Custom
metric names work when you declare their direction in TOML. Explicit CLI flags
are still supported for experiments and quick tests, but the TOML path is the
recommended public workflow. See
[CLI Reference](CLI_REFERENCE.md#simple-ar-code-task-init) for the full option
table and [Configuration Reference](CONFIG_REFERENCE.md#standalone-code-task-config)
for the config schema.

For existing-project tasks, `[execute].baseline_policy` controls whether the
unchanged benchmark is run before editing. The default `auto` behavior runs it
when comparison evidence is useful. Use `skip` or `none` when the baseline is
too expensive or the task is acceptance-style, and use `provided` with
`baseline_metrics_file` when you already have trusted baseline metrics. Provided
baselines are recorded as user-supplied evidence in the run summary; they are
not presented as reproduced results.

### Recommended Path: TOML + Execute

Ordinary existing-project execution uses one patch plan, then approval, edits
and validation. It does not automatically generate a work plan or batch state.
For a decomposed task, explicitly use `execute --to-step work-plan` or
`--to-step batch`; an existing work plan also retains the batch workflow.
Interactive execution follows the same rule.

For normal use, prefer a TOML config plus the state-aware executor. This keeps
commands short while preserving review gates for the patch plan and edit
proposal. The examples below use the medium review pipeline config because it
exercises multi-file edits, project config changes, metrics, and visible
benchmark progress.

1. Initialize a run:

```bash
uv run simple-ar code-task init --config examples/code_task_medium_review/configs/code_task.toml
```

The command prints a run directory such as
`runs/20260523-xxxx-medium-review-pipeline`. Replace `runs/<run-id>` in the following
commands with that printed path.

`init` writes the isolated workspace and static project map:

```text
runs/<run-id>/
  manifest.json
  code_task/
    task.md
    workspace/
    meta/
      codebase_index.json
      repo_map.json
      repo_map_summary.md
```

The workspace is the only editable copy/worktree. `task.md` is the task prompt,
the `meta/` files are the initial code map, and `manifest.json` records
benchmark, workspace, environment, and safety policy.

> Tip: The medium review pipeline runs `python main.py --config
> configs/experiment.json --show-progress` and can relay progress lines such as
> `benchmark stdout: round 1/4 ...` while still saving the full log under
> `code_task/run/<label>/stdout.txt`.

> Note: The medium task often touches feature extraction, model scoring, and
> config together. Its sample edit scope allows `configs/experiment.json`
> because a newly implemented feature family must be enabled before the
> benchmark can measure it. It may create a reviewed `large` batch; add
> `--allow-large-edits` to the final apply command only after inspecting
> `code_task/meta/proposed_edits.json`.

2. Run the state-aware executor:

```bash
uv run simple-ar code-task execute runs/<run-id> --config examples/code_task_medium_review/configs/code_task.toml
```

On an interactive terminal, this one command can walk through the plan review,
proposal review, apply, structured review, validation, and patched benchmark gates. On a
non-interactive shell, or when you answer `no`, it stops at the current review
gate and can be rerun after review. The first review gate usually writes:

```text
code_task/
  work_plan.md
  patch_plan.md
  meta/
    environment_report.json
    review_report.json
  attempts/
    attempt-001/
      batches/
        batch-001/
          batch_state.json
  run/
    baseline/
      metrics.json
```

At this point the original project is still untouched and the workspace has not
received model edits.

`execute` renders the step state with Rich and runs continuously until a real
review gate is reached. In an interactive terminal, those gates are handled
inline: a yellow review panel points to `patch_plan.md`, `proposed_edits.json`,
or large-edit approval and asks whether to continue. In non-interactive shells,
it stops cleanly at the gate unless `--yes` is supplied. If a run is
interrupted, rerun the same `code-task execute` command: completed steps are
detected and shown as skipped before the workflow advances. Use `--interactive`
only for debug mode when you want to confirm each primitive step; `--yes`
auto-continues those primitive prompts, and in normal execute mode also
auto-approves inline review gates. Use it only when automated approval is
intentional. Use `--no-review-inline` if you prefer the older stop-and-rerun
flow.

If LLM work planning or patch planning returns malformed JSON, `execute` stops
with `llm_planning_failed` and leaves the fallback artifacts unwritten. Rerun
the same command to retry the LLM step. Use `--no-llm` for a deterministic
offline plan, or `--allow-planning-fallback` only when that weaker fallback is
acceptable for the task.

After edits are applied, `execute` writes `code_task/meta/review_report.json`
before static validation. After the patched benchmark runs, it writes
`code_task/meta/review_report_post_run.json`. Blocking findings are also
recorded under `code_task/memory/` so repair prompts can reuse the latest
review evidence.
Structured review also writes `code_task/meta/review_index*.json` and
`code_task/meta/review_clusters*.json`: the former is the full project index,
and the latter records the semantic file clusters actually shown to reviewers.
Greenfield planning additionally writes
`code_task/meta/planning/agent_steps.jsonl` to make requirements,
architecture, interfaces, file-plan, and planning-review failures easier to
trace.

For greenfield runs, the same review gate checks generated files before
validation. Bounded review and runtime repair rounds use the actual diagnostics
to select related files and generate a separate, hash-bound candidate through
the shared code capability. Applying it retains the existing API/static guards
and snapshots; rejection restores the associated changes rather than leaving
a partial repair. The executor resyncs `code_task/meta/code_artifacts.json` and
reruns review/validation. Saved unbound legacy results remain historical, not
current repair certificates; an explicitly started new round preserves them
before performing new work. Interrupted generation/application is retained for
inspection, not blindly replayed. Blocking findings still stop execution.

3. At the patch-plan review panel, read `code_task/work_plan.md` and
`code_task/patch_plan.md`. If the plan is reasonable, answer `yes` to continue.
If you are running non-interactively, answered `no`, or used
`--no-review-inline`, approve it explicitly:

```bash
uv run simple-ar code-task decide-plan runs/<run-id> --decision approve --note "reviewed"
```

4. If the first executor command did not already continue, generate an edit
proposal next. Do not apply it until the inline proposal review panel appears:

```bash
uv run simple-ar code-task execute runs/<run-id> --config examples/code_task_medium_review/configs/code_task.toml --to-step propose-edits
```

Review:

- `code_task/meta/proposed_edits.json`: controlled old/new replacements.
- `code_task/meta/llm_usage_summary.json`: LLM token usage summary.
- latest `code_task/attempts/.../proposal_warnings.json`, when present.

The controlled editor records `controlled_patch` provenance in
`proposed_edits.json`, the active batch state, `applied_edits.json`, and
`manifest.json.patch`. Proposal/application functions do not run benchmarks, approve plans, or
write reports; those gates remain owned by the code-task workflow.

5. At the proposal review panel, inspect the generated edits. Answer `yes` to
apply and evaluate the patched workspace. If you are running non-interactively,
answered `no`, or used `--no-review-inline`, apply explicitly:

```bash
uv run simple-ar code-task execute runs/<run-id> --config examples/code_task_medium_review/configs/code_task.toml --apply-proposed-edits --timeout 60
```

6. Inspect the result:

```bash
uv run simple-ar status runs/<run-id>
```

Key output files:

```text
code_task/
  summary.md
  patch.diff
  meta/
    applied_edits.json
    validation_report.json
  run/
    patched/
      metrics.json
    comparison.json
```

`patch.diff` and `applied_edits.json` show what changed, `validation_report.json`
shows static checks, `metrics.json` records the patched run, and
`comparison.json` is the before/after objective verdict.

Treat `objective_improved` or `objective.status = "improved"` as the normal
success signal. A patched benchmark can pass while `objective.status` is
`regressed` or `mixed`; in that case, the code ran but the measured task goal
was not really met.

7. If the proposal needs repair, ask for one bounded repair proposal:

```bash
uv run simple-ar code-task execute runs/<run-id> --config examples/code_task_medium_review/configs/code_task.toml --to-step repair --repair-rounds 1 --timeout 60
```

Review the newest `code_task/repairs/repair-NNN/proposed_edits.json`, then
apply it explicitly:

```bash
uv run simple-ar code-task apply-edits runs/<run-id> --edits-file runs/<run-id>/code_task/repairs/repair-NNN/proposed_edits.json
uv run simple-ar code-task validate runs/<run-id>
uv run simple-ar code-task run runs/<run-id> --timeout 60
uv run simple-ar status runs/<run-id>
```

Preview the next executor action without writing artifacts:

```bash
uv run simple-ar code-task execute runs/<run-id> --config examples/code_task_medium_review/configs/code_task.toml --dry-run
```

### Optional Mapping And Context Tools

Refresh the code map at any time:

```bash
uv run simple-ar code-task map runs/<run-id>
```

`map` scans the current workspace and refreshes the static code-map artifacts:

```text
code_task/
  workspace/                  # scanned source tree
  meta/
    codebase_index.json       # file-level code index
    repo_map.json             # layered repo/symbol map
    repo_map_summary.md       # human-readable summary
manifest.json                 # updated map/workspace metadata
```

It does not call the LLM, install dependencies, run benchmark code, or modify
the original source project.

Locate likely files before planning or editing:

```bash
uv run simple-ar code-task locate runs/<run-id> --query "improve spam keyword prediction"
```

`locate` writes `code_task/meta/locate_results.json` and
`code_task/meta/locate_results.md`. It ranks editable targets separately from
read-only evidence such as tests and benchmarks, using the repo map rather than
loading the whole project into a prompt. It does not call the LLM or read files
outside the prepared workspace.

Build a bounded prompt context pack:

```bash
uv run simple-ar code-task context runs/<run-id> --max-files 8 --max-total-chars 20000
```

`context` creates `code_task/context_packs/context-NNN/` containing
`context_pack.json`, `prompt_context.md`, and `selected_snippets.jsonl`. The
pack records token-like character budgets, selected editable files,
read-only evidence, truncated snippets, and omitted files. It is a reviewable
intermediate artifact for LLM planning/editing. When a latest context pack
exists, `plan` uses it for planning context. `propose-edits` starts from its
selected snippets and keeps tests/benchmarks as read-only evidence. Bounded
follow-up reads refresh the current source window rather than treating its
character limit as a lifetime reading quota. Initial selection does not grant
or restrict authorization: newly observed files still follow the configured
edit scope and, when present, the explicit batch targets. Original observations
and the spans retained in each prompt are saved in `edit_context_followup.json`.

### Manual Primitive Path

The executor path above calls these primitive commands for you. Use this manual
path when you are learning the internals, debugging one step, or intentionally
building a custom workflow.

Probe the environment and run the unchanged baseline before asking for edits:

```bash
uv run simple-ar code-task map runs/<run-id>
uv run simple-ar code-task locate runs/<run-id>
uv run simple-ar code-task context runs/<run-id>
uv run simple-ar code-task probe runs/<run-id>
uv run simple-ar code-task baseline runs/<run-id> --timeout 60
uv run simple-ar code-task work-plan runs/<run-id>
uv run simple-ar code-task batch runs/<run-id> --work-item W1
```

`probe` writes `code_task/meta/environment_report.json` with OS, Python, tool, GPU, dependency-file, and test-directory signals. It also writes `resource_probe.json` and `resource_decision.json`, which give greenfield and external-agent paths a compact hardware profile. It does not install dependencies or run project code.

`baseline` runs the recorded benchmark command inside `code_task/workspace/`
before any patch is applied. It stores `execution_report.json`, `stdout.txt`,
`stderr.txt`, and parsed `metrics.json` under `code_task/run/baseline/`, and
updates `code_task/summary.md`.

Generate a higher-level work plan when the task is broad or may need multiple
edit batches:

```bash
uv run simple-ar code-task work-plan runs/<run-id>
uv run simple-ar code-task batch runs/<run-id> --work-item W1
```

`work-plan` writes `code_task/work_plan.json` and `code_task/work_plan.md`.
It records work items, target files, read-only evidence, validation hints,
context requests, and budget profiles. It does not generate code or edit
files. `batch` creates durable attempt state under
`code_task/attempts/attempt-NNN/batches/batch-NNN/`, which is the foundation
for later multi-round, per-batch editing and recovery. When a batch
is active, edit proposals are constrained to that batch's target files and
write extra batch-local review artifacts.

Work-plan items are intended to be executable implementation batches, not
standalone analysis notes. The LLM prompt asks the planner to put inspection
needs in `context_request`. If a model still returns an analysis-only first
item, `code-task execute` prefers the first later item that looks like a real
code change, so a broad "inspect the project" step does not accidentally become
the active edit batch.

If a model splits one tightly coupled implementation into a serial chain, for
example feature extraction -> scorer wiring -> config enablement, the batch
creator can merge that small dependent chain into one execution batch. The
reviewed `work_plan.md` still shows the separate items, but
`batch_state.json.work_item.source_work_item_ids` records the merged item ids
and `target_files` becomes the union of the coupled files. Because these merged
batches may touch more than two files, they usually use the `large` budget
profile and require explicit review before `--allow-large-edits` is used.

Generate a patch plan (LLM optional; offline mode writes a conservative plan):

```bash
uv run simple-ar code-task plan runs/<run-id>
```

If `probe`, `validate`, or `baseline` artifacts already exist, the generated
plan includes that run context so the model and reviewer can reason from
recorded environment and benchmark evidence instead of starting cold.

`plan` writes `code_task/patch_plan.md`, updates `manifest.json`, and records
selected context files. It does not change source files. In LLM mode it records
token usage under `code_task/meta/llm_usage.jsonl`; with `--no-llm` it writes a
conservative offline plan.

Review the plan, then approve it:

```bash
uv run simple-ar code-task decide-plan runs/<run-id> \
  --decision approve \
  --note "small scoped edit"
```

`decide-plan` appends a human decision to
`code_task/meta/hitl_decisions.jsonl` and updates the plan status in
`manifest.json`. Approval is the normal gate before model-generated edits can
be applied.

Ask the model for controlled edit proposals (offline mode writes an empty proposal):

```bash
uv run simple-ar code-task propose-edits runs/<run-id>
```

`propose-edits` writes `code_task/meta/proposed_edits.json`. The proposal uses
controlled old/new text replacements and is meant for review. It does not edit
the workspace by itself. A proposal may include multiple ordered edits for the
same file; each `old` block must still match uniquely when applied in sequence.
The proposal also records `editor.backend = "controlled_patch"` so future
backends can be audited through the same artifact shape.
The reserved `external_agent` backend is intentionally non-executable in this
version. It can build a reviewable invocation plan for future
Codex/Claude/OpenCode adapters, including provider, command preview, blocked
read patterns, timeout, network/shell permissions, log path, and diff path. Any
future external-agent result must still become a captured diff/proposal before
SimpleAutoResearch applies validation, benchmark execution, and summary logic.
By default, tests and benchmark files are treated as read-only evidence:
`propose-edits` omits them from editable snippets, and any model edit targeting
paths such as `tests/**`, `test_*.py`, `benchmark.py`, or `*benchmark*.py` is
dropped from the proposal. The executor also applies an edit budget after the
model returns JSON. Oversized proposals are written with warnings and rejected
edits instead of being applied; if the proposal fits the larger review budget, rerun
with `--allow-large-edits` only after reading the JSON.

Apply proposed edits inside the editable workspace:

```bash
uv run simple-ar code-task apply-edits runs/<run-id>
```

`apply-edits` applies the reviewed proposal only inside
`code_task/workspace/`, writes a human-readable `code_task/patch.diff`, writes
`code_task/meta/applied_edits.json` with changed files and hashes, and updates
the codebase index. The next `execute` step runs the structured reviewer and
writes `code_task/meta/review_report.json` before validation. It still never
mutates the original `--code-root`. If an edit cannot be matched safely,
`execute` stops with `patch_apply_failed` before workspace files are changed.
`applied_edits.json` records the proposal path and editor backend used for the
application, including manually supplied or repair proposal files.
`apply-edits` also re-checks the edit scope, so manually supplied JSON cannot
modify protected tests or benchmark files even if it bypassed the LLM proposal
step.

Validate and run the patched benchmark:

```bash
uv run simple-ar code-task validate runs/<run-id>
uv run simple-ar code-task run runs/<run-id> --timeout 60
```

`validate` writes `code_task/meta/validation_report.json` with syntax errors,
risky imports/calls, missing import warnings, and file-size warnings. It is a
static check; it does not run the benchmark.

`run` stores the patched benchmark under `code_task/run/patched/`.
When both baseline and patched artifacts exist, SimpleAutoResearch also writes
`code_task/run/comparison.json` and includes outcome, next-step guidance, and
metric deltas in `code_task/summary.md`.

Patched benchmark success is separated from task-objective success. A run may
pass the benchmark floor but still regress against baseline metrics. In that
case `manifest.json` records `objective.status = "regressed"`, `simple-ar
status` prints the objective verdict, and `summary.md` points you back to
`code_task/run/comparison.json` instead of treating the task as complete.

Analyze failures and request a bounded repair proposal:

```bash
uv run simple-ar code-task analyze-failure runs/<run-id>
uv run simple-ar code-task repair runs/<run-id>
```

`analyze-failure` reads the latest failed validation/benchmark evidence and
writes a compact diagnosis, usually under `code_task/run/patched/` or the
current run label. If the benchmark was blocked before launch by static
validation, it writes `code_task/meta/failure_analysis.md` instead. It is
deterministic and does not call the LLM.

`repair` uses the failure analysis, latest patch, task, and selected source
context to write a bounded repair proposal under
`code_task/repairs/repair-001/proposed_edits.json`. The proposal records the
source analysis path, selected context files, and repair constraints. It does
not apply the repair automatically. Repair proposal context follows the same
edit-scope rule: tests and benchmark files may inform diagnosis, but they are
not supplied as editable snippets by default. `code_task/summary.md` is
refreshed with a Repair section.

Apply a reviewed repair proposal explicitly:

```bash
uv run simple-ar code-task apply-edits runs/<run-id> \
  --edits-file runs/<run-id>/code_task/repairs/repair-001/proposed_edits.json
```

When a repair proposal is applied, `manifest.json.patch.latest_applied_proposal`
and `code_task/meta/applied_edits.json` record the repair proposal path. After a
later patched benchmark passes, stale failure-analysis and repair sections are
marked resolved so `status` and `summary.md` reflect the current state rather
than an older failed attempt.

### Troubleshooting Code Task Runs

`proposed_edits.json` was not created after `execute`:

- This is normal after the first executor call. A fresh run stops at
  `approval_required` after writing `code_task/patch_plan.md`.
- Review `code_task/patch_plan.md`, then run:

```bash
uv run simple-ar code-task decide-plan runs/<run-id> --decision approve --note "reviewed"
uv run simple-ar code-task execute runs/<run-id> --config examples/code_task_medium_review/configs/code_task.toml --to-step propose-edits
```

- Check `manifest.json`: `plan.status` should be `approved`. The decision log
  is `code_task/meta/hitl_decisions.jsonl`.

`execute` stopped with `llm_planning_failed`:

- This means the model call returned invalid/missing structured JSON for the
  work plan or patch plan. No deterministic fallback plan is written by default.
- Rerun the same `code-task execute ... --config ...` command to retry from the
  same point; completed earlier steps will be detected as skipped.
- If you want to proceed without LLM planning, rerun with `--no-llm`. If you
  still want the LLM first but accept deterministic fallback after retries, use
  `--allow-planning-fallback` or set `[execute].allow_planning_fallback = true`.

Validation passed but patched benchmark failed:

- This means the patch was syntactically acceptable but behavior or metrics got
  worse. Inspect:

```bash
code_task/run/patched/execution_report.json
code_task/run/patched/stdout.txt
code_task/run/patched/stderr.txt
code_task/run/comparison.json
code_task/summary.md
```

- Ask for a bounded repair proposal:

```bash
uv run simple-ar code-task execute runs/<run-id> --config examples/code_task_medium_review/configs/code_task.toml --to-step repair --repair-rounds 1 --timeout 60
```

- Review the newest `code_task/repairs/repair-NNN/proposed_edits.json`, then
  apply it explicitly:

```bash
uv run simple-ar code-task apply-edits runs/<run-id> --edits-file runs/<run-id>/code_task/repairs/repair-NNN/proposed_edits.json
uv run simple-ar code-task validate runs/<run-id>
uv run simple-ar code-task run runs/<run-id> --timeout 60
```

- A repair can make the benchmark pass without truly improving over baseline.
  Use `code_task/run/comparison.json`, `manifest.json.objective.status`, and
  `simple-ar status` to decide whether the task goal was met.

Patched benchmark passed but the objective is `regressed` or `mixed`:

- This is not a runtime failure; it means the patch did not satisfy the metric
  goal compared with the recorded baseline.
- Inspect `code_task/run/comparison.json` first. It lists metric deltas,
  directions, the primary metric when configured, and the conservative verdict.
- Treat this like a quality failure: revise the task/plan, regenerate a tighter
  proposal, or request a repair only if the comparison gives enough evidence for
  a bounded follow-up patch.

`apply-edits` reports `old text was not found`:

- No workspace files are changed when this happens. It means a proposal's
  `old` text does not exactly match the current workspace, or the model put
  unified-diff markers inside the structured JSON.
- Regenerate the proposal or edit the JSON manually. Each edit must use exact
  current file text in `old` and replacement file text in `new`; do not include
  `+`, `-`, `@@`, `---`, or `+++` diff markers inside either field.
- If several edits target the same nearby block, combine them into one larger
  exact old/new replacement so later edits do not invalidate earlier ones.

Large-edit approval is required:

- Read `code_task/meta/proposed_edits.json` and any
  `proposal_warnings.json` under `code_task/meta/` or the latest
  `code_task/attempts/.../batch-NNN/` directory.
- If the larger change is intentional, rerun the apply/executor command with
  `--allow-large-edits`. Do not use this flag just to silence an unclear model
  proposal.

Proposal covers only the first part of a coupled plan:

- Check `code_task/work_plan.md` and the latest
  `code_task/attempts/.../batch_state.json`. If a plan has serial items such as
  feature -> model -> config, the active batch should list all coupled ids in
  `work_item.source_work_item_ids` and all editable files in `work_item.target_files`.
- For older runs created before this behavior, create a fresh batch with
  `uv run simple-ar code-task batch runs/<run-id> --work-item W1 --force`, then
  regenerate the proposal with
  `uv run simple-ar code-task propose-edits runs/<run-id> --force`.
- If the merged batch is marked `large`, review the full proposal before using
  `--allow-large-edits`.

`uv run` fails with a local cache permission error:

- This is an environment issue outside the run artifacts. Fix the uv cache
  permissions or run the project virtualenv entrypoint directly, for example
  `.\.venv\Scripts\simple-ar.exe ...` on PowerShell.


## Command Design

The CLI keeps primitive commands because this project is still a learning
implementation. Each step is inspectable, testable, and reviewable. Config files
are used to shorten setup-heavy commands, not to hide approval gates, artifact
paths, validation results, baseline runs, or benchmark evidence.
