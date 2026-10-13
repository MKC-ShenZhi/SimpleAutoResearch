# SimpleAutoResearch

**A task-focused research assistant: investigate, edit, analyse, and write with inspectable results.**

[中文](README_zh.md) · [Quick start](#quick-start) · [Examples](#choose-a-case) · [Documentation](#documentation) · [Changelog](CHANGELOG.md)

Describe your goal and provide the material you already have. The native `start`
entry investigates it and chooses tools in one saved task. SimpleAutoResearch
delivers files you can inspect, edit, rebuild, and continue using.

Scientific modules can be used individually or combined as needed: **existing data →
analysis and plots → report → optional ACM export**. Code work includes creating
projects, analysis scripts and editable figures as well as repairing a repository.
Writing from existing results need not repeat an experiment.

> **Source-visible preview.** The native tool loop and explicit structured workflows are available; complete cross-task acceptance, general autonomous reproduction and integrated external coding Agents are not claimed. Inspect sources, measurements and audits before relying on a result.
>
> Licensing is not yet selected. Public repository access is not an open-source license.

## What can you do with it?

| Task | Bring | Get |
| --- | --- | --- |
| Research a direction | A question, scope and optional papers | Source-backed comparisons, reading notes and a report |
| Create or modify code | A goal, optional repository and validation requirements | Generated source or an edited workspace, change records and actual validation status |
| Analyse and plot data | A table, selected columns and their meanings | Descriptive statistics, paired comparisons and editable SVG/PDF/PNG figures |
| Write from material | Papers, notes, a draft or saved analysis | An editable report or paper-style draft, references and reused figures |
| Reproduce a specified conclusion | A paper, prepared project/data and agreed protocol | Recorded measurements and an account of the reproduction scope |
| Export a saved report | Report artifacts and export tools | An editable ACM LaTeX project, with optional PDF compilation |

The explicit table-analysis shortcut can run without a model API. Native tasks,
research, generated code and writing require one; measurement requires the target
scientific environment. `start` accepts goals or optional outcome preferences;
`report-export` uses an existing structured report.
See [current boundaries](#current-boundaries) for the supported scope.

### Why use an assistant around these tasks?

For a table-analysis task that also needs a written report, use
`simple-ar start --kind data_analysis --with-report` (or request it in
`start --chat`). With explicit table column selectors, analysis, reusable figures
and reviewed writing share the same structured session. That analysis shortcut
alone remains zero-API; native investigation and writing require a model.

- **Less material handoff.** Saved analysis packages and report artifacts can
  feed the next task instead of becoming disconnected chat answers.
- **Inspect actual work.** Plans, source excerpts, patches, measurements,
  usage and rejected revisions remain available as files.
- **Continue saved work.** Compatible completed results and supported writing
  checkpoints can be reused; continuation retains accounting.
- **Keep control.** Choose checkpoints or autonomous actions within the accepted
  scope. Missing critical inputs and permissions still require participation.

These are intended workflow benefits, not evidence that the assistant outperforms
a general coding Agent. Cross-task quality and first-time user experience remain
under validation.

## Quick start

Requirements: **Python 3.12+**, Git and [uv](https://docs.astral.sh/uv/). Native source search also needs ripgrep (`rg`) on the execution host.
Follow the [complete ordinary workflow](docs/USAGE.md#a-copyable-ordinary-user-workflow) for installation,
named TOML/secret references, materials, approval, inspection, feedback and recovery;
it includes a minimal catalog and Bash/PowerShell configuration.

### 1. Install

The current preview is on `feat/v2.9-task-driven-research`, not the default branch:

```bash
git clone --branch feat/v2.9-task-driven-research https://github.com/Wchanging/SimpleAutoResearch.git
cd SimpleAutoResearch
uv sync
```

### 2. Try a task without an API

The included demonstration data can be plotted without credentials, a GPU or
a training environment:

```bash
uv run simple-ar research-session --config examples/data-curves/research.toml
```

Inspect the printed analysis package and SVG paths under `runs/data-curves/`.
This case plots supplied demonstration coordinates; it is not an experiment
or benchmark result. See the [case guide](examples/data-curves/README.md).

### 3. Configure a model for research, code or writing

For a simpler setup across tasks and projects, configure a shared [named model
catalog](docs/CONFIG_REFERENCE.md#named-model-connections). Copy [the catalog
example](examples/model-profiles/models.toml) to `~/.config/simple-ar/models.toml`,
or a private location such as `.local/models.toml` and set `SIMPLE_AR_MODELS_CONFIG`
to its absolute path. Replace the example's endpoints and model IDs with your
provider's settings. TOML owns connection settings and routes; each `api_key_env`
names a credential variable whose value stays in your environment or private `.env`.
The example uses `CCTQ_API_KEY`; choose the variable names appropriate to your provider.

Configure `default` once for ordinary tasks and `code` for code-capable work;
add `writing`, `vision` or `image` routes when needed. Without an explicit `writing`
route, native writing keeps the task connection. `start` displays text connections
and lets terminal users choose; `--model profile:NAME` selects one explicitly.
`simple-ar models` checks local settings and credential presence without a request;
declared capabilities do not verify provider support. Native decisions require
Chat mode; structured text workflows can also use Responses.

The environment-only connection remains compatible when no catalog is configured.
Copy `.env.example` to `.env` **only if you do not already have one**:
`cp .env.example .env` on a shell, or `Copy-Item .env.example .env` on PowerShell.
Edit it with your provider's actual values:

```dotenv
OPENAI_API_KEY=your_api_key
OPENAI_BASE_URL=https://your-provider.example/v1
SIMPLE_AR_MODEL=your_model_id
SIMPLE_AR_LLM_API=chat
SIMPLE_AR_LLM_STREAM=false
```

Use an API format supported by your provider; `responses` is also supported.
Streaming is optional; enable it only when your provider handles it reliably.
Keep secrets out of Git. These global model/transport settings apply to the legacy
connection; named profiles use their own settings. Project paths, interpreters,
data and task limits belong in task configuration.
[Advanced settings](docs/CONFIG_REFERENCE.md) are optional.

### 4. Start your own task

```bash
uv run simple-ar start
```

Describe your goal at the prompt, or pass it directly. The experimental native
loop investigates materials and chooses actions without requiring a function selection:

```bash
uv run simple-ar start "Explain these results" --source results/ --model profile:NAME
```

Repeat `--source PATH` for read-only materials. Public retrieval requires
`--allow-network`; process proposals require `--allow-execution` and separate
command approval. `--prepare-only` saves the task without model calls or execution.
Native preparation still preflights the model connection and credentials; saving TOML before setting a key is structured setup behavior.
Inspect the proposed source, command and side effects. Confirm each command in an interactive terminal,
or copy the current `Approval token` after a pause. Replace `SESSION` below with the full printed `Task session` path:

```bash
uv run simple-ar start --resume-task "SESSION" --approve-execution "TOKEN"
uv run simple-ar status "SESSION"
uv run simple-ar results "SESSION"
uv run simple-ar start --resume-task "SESSION" "Keep the initial version, add evidence limits, and deliver a separate feedback version"
uv run simple-ar results "SESSION"
uv run simple-ar start --resume-task "SESSION"
```

`TOKEN` is the exact token for the current proposal; `--yes` does not approve commands.
Inspect actual artifacts before feedback, which retains the budget and may need more approvals.
The last command resumes a saved boundary; completed tasks without pending actions return saved terminal state.
After interruption, confirm the previous process has stopped; do not blindly replay unknown side effects.
See the CLI reference for explicit continuation of failed model decisions.

Complete cross-task acceptance is still pending. `start --guided` offers outcome
preferences; ordinary `--kind` choices use the same native loop. Explicit controls
such as `--template`, `--sources`, `--allow`/`--validate` or a reproduction protocol
retain structured compatibility. For `--kind data_analysis`, explicit
`--value-column`, `--x-column` or `--paired-baseline` without `--scripted` selects
the no-API table shortcut. Structured
protocols cannot mix native access/budget options; see the [exact routing rules](docs/CLI_REFERENCE.md#goal-driven-code-analysis-and-figures).
For code execution, `--project-python PATH` can select an existing project interpreter;
omit it to use the current environment. It does not create environments or install dependencies.
For model-assisted clarification of a structured task, opt in with:

```bash
uv run simple-ar start --chat
```

Describe your goal and answer the material questions. The model proposes a function
and semantic settings; you confirm them before the structured task configuration
is saved. Execution choices remain explicit. For code repair, chat can propose a test command using
inspected project instructions; you confirm it and the editable scope before
ordinary execution. An explicit `--validate` command is not replaced. Replies and
named asset previews go to your configured model, so setup itself incurs API usage. `--resume-setup PATH` resumes
the saved conversation and its accounting; it is not execution recovery or a general
coding chat. Use structured setup with `--prepare-only` when you want no API calls.

Or try the supplied model-backed survey:

```bash
uv run simple-ar research-session --config examples/survey/research.toml
```

This example surveys small-memory continual learning using available abstracts
and metadata. It **does not download full text or train models**. Change
`[task].goal` for another question; use the guide/configuration reference when
you need supplied papers or best-effort full-text retrieval.

> **Cost:** model calls are billable. Native tasks default to 30 requests and
> 600,000 shared tokens, retained on resume; structured API caps are optional.
> Uncapped model output can exceed a token estimate. Process time, resources and research-round
> limits are separate. There is no fixed completion-time promise.

## Choose a case

One directory contains one case's inputs, configuration and instructions;
generated work goes to `runs/`. Run the checked-in config directly—do not copy
templates into `runs/` first.

| Case | Purpose | Preparation |
| --- | --- | --- |
| [Data curves](examples/data-curves/README.md) | Supplied line/scatter coordinates | No API or training; included demonstration data |
| [Descriptive analysis](examples/data-analysis/README.md) | Tables, descriptive bars and rebuilding | No API; explicit data/row semantics |
| [Paired observations](examples/data-paired/README.md) | Matched differences, omissions and standard-error plots | No API; artificial demonstration, not a benchmark |
| [Literature survey](examples/survey/README.md) | Research report without training | Model API and internet |
| [Conformal reproduction](examples/conformal_reproduction/README.md) | Bounded, adapted numerical reproduction | Follow the case environment/protocol; not whole-paper or benchmark acceptance |
| [ICML 2025 RCP subset](examples/rcp_reproduction/README.md) | Fixed author-code-assisted real-data comparison | Prepare pinned code, data, paper and scientific Python; two methods, ten paired seeds. Not whole-paper reproduction; writing quality needs separate review. |
| [Multi-file code review](examples/code_task_medium_review/README.md) | Scoped editing and validation | Model API for generated edits; Python standard-library project |
| [Digits MLP](examples/code_task_digits_mlp/README.md) | Small model-code task | NumPy/scikit-learn; `uv sync --extra examples`, retain the extra in `uv run` |
| [Continual learning](examples/continual_learning/README.md) | Experimental research improvement | Prepared Mammoth/CIFAR-100 project, fixed split, environment and GPU budget |

The two code examples use standalone `code-task`, not a full research loop.
[TabM](examples/tabm_research/README.md) is a prepared-server diagnostic, not a
fresh-checkout quick start. See the [example index](examples/README.md) for scope.

## What you receive

| Delivery | Files to inspect | Reuse |
| --- | --- | --- |
| Data analysis | `analysis.json`, `analysis.md`, copied input, editable SVGs, vector PDFs and PNG previews | Rebuild the package or use its JSON as writing material |
| Report | `report.md`, `report_body.md`, `references.bib` and the separately printed audit | Review claims alongside sources; export the report attempt directory |
| ACM export | `main.tex`, `body.tex`, bibliography, figures and `export.json` | Edit/move the project; inspect `build.log` when compilation is requested |
| Code/measurement | Edited workspace, actual validation/run logs and saved results | Inspect the actual change and evaluation conditions, not only the final prose |

The terminal prints exact session/attempt paths. The table describes structured
deliveries; native tasks save a workspace and delivery manifest, and native writing
can produce an editable `manuscript.md` for publication. Keep the whole analysis
directory so its copied data remains available after moving it.

For example, connect an analysis package to writing without repeating its task:

```bash
uv run simple-ar start --kind writing --goal "Explain these results and their limitations" --material "PATH_TO_ANALYSIS/analysis.json"
```

Replace `PATH_TO_ANALYSIS` with the printed analysis directory; model access is
needed for writing. This ordinary writing preference uses the native loop; ask it
for publication of its manuscript when needed. For an existing structured report
attempt (for example, from the survey config), export separately:

```bash
uv run simple-ar report-export --report-dir "PATH_TO_REPORT_ATTEMPT" --output runs/acm-draft
```

Replace the report path; choose an unused output directory. Pandoc is required;
SVG conversion requires `rsvg-convert`. Add `--compile` only when pdfLaTeX,
BibTeX and acmart are installed. An uncompiled source project is not a verified
PDF or a conference submission. See the [export guide](docs/CLI_REFERENCE.md#simple-ar-report-export).

## Bring your own material

Describe the goal, intended delivery and available inputs:

- **Survey:** question, scope, source papers and whether online search is allowed.
- **Code:** creation or modification goal, optional repository and validation requirements; exact edit scope and validator belong to the structured protocol.
- **Data:** table, selected fields, row/value meanings and units.
- **Writing:** source material, report or paper-style draft, and optional reference papers.
- **Reproduction:** specified paper conclusion, prepared execution conditions and fixed evaluation.

Native tasks save confirmed inputs, permissions and limits in their session;
structured setup saves editable TOML. Project paths, data, interpreters and task
limits belong there. See [Usage](docs/USAGE.md) for worked
examples and [Configuration](docs/CONFIG_REFERENCE.md) for expert fields.

## Inspect and resume

The terminal prints the session directory, deliverables and current action.
Read actual measurements and source evidence alongside the report.

For a native task, use its saved path and optionally append feedback:

```bash
uv run simple-ar start --resume-task "SESSION_PATH"
```

For an unchanged structured model-backed session, retain the historical command:

```bash
uv run simple-ar research-session --session-root "SESSION_PATH" --model env
```

Do not create another task to resume saved work. For the model-free data example,
omit `--model env`. A new run without `--session-root` creates a new session.
Continuation does not reset an exhausted budget or imply background execution
after disconnect. Confirm an interrupted worker has stopped before recovery;
see the [CLI reference](docs/CLI_REFERENCE.md).

New structured CLI sessions default to `checkpoints`; use `--interaction assisted`,
`checkpoints` or `autonomous` to choose participation. Examples can specify a
different policy. No mode supplies missing facts, assets or permissions.

## How it fits together

```text
Goal + material + constraints
             ↓
      Select necessary work ←──────────┐
             ↓                         │
 Research / Code / Data / Run / Write   │
             ↓                         │
 Save evidence and artifacts ─── Reassess
                                       ↓
                              Deliver or request input
```

One shared session owns attempts, accounting, artifacts and recovery.
Domain modules own their evidence and execution. Technical repair, scientific
revision and prose revision are different actions—not one generic retry.

## Current boundaries

- **Reading and writing:** models can misread material or leave unresolved claims and incomplete references. Audits and compilation do not certify scientific or publication quality.
- **Data and figures:** deterministic summaries and bar/box/line/scatter/matrix-heatmap plots use declared semantics. Other methods and editable method figures can use generated code with approved execution; raster generation/editing uses a named image route and allowance. Availability does not certify statistics, visual quality or scientific correctness.
- **Execution:** workspaces and bounded processes are not an OS sandbox. Prepare the target environment and protect sensitive inputs.
- **Scope:** general autonomous provisioning, integrated external Agents and formal PaperBench/ScienceAgentBench results are not claimed.

See [workflows](docs/WORKFLOWS.md) for evidence and recovery boundaries.

## Documentation

| Guide | Purpose |
| --- | --- |
| [Usage](docs/USAGE.md) | Start a goal, select optional preferences, inspect outputs and troubleshoot |
| [Configuration](docs/CONFIG_REFERENCE.md) | Named connections, compatible environment settings and task inputs |
| [CLI reference](docs/CLI_REFERENCE.md) | Commands, continuation and explicit revisions |
| [Workflows and artifacts](docs/WORKFLOWS.md) | Evidence, execution and saved-result boundaries |
| [Development](docs/DEVELOPMENT.md) | Module ownership and engineering contracts |
| [Changelog](CHANGELOG.md) | Implemented changes and migration notes |

## Contributing and acknowledgements

Issues and reproducible cases are welcome. Discuss code contributions before
opening a PR while licensing is undecided. Include commands, relevant
configuration and diagnostics; **remove credentials and private data**.

Workflow and presentation inspiration includes
[AutoResearchClaw](https://github.com/aiming-lab/AutoResearchClaw),
[OpenResearch](https://github.com/alphaXiv/OpenResearch) and
[ARIS](https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep).
This project keeps its own compact, file-based runtime and scoped native workflows.
Reports, examples and usability improvements matter alongside code.
