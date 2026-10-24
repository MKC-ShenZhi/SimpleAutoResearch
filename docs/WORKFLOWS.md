# Task guides

[中文](WORKFLOWS_zh.md) · [Get started](USAGE.md) · [CLI reference](CLI_REFERENCE.md)

Start with the work you want to accomplish: what to provide, how to run it, and what to inspect.
Complete [model setup](USAGE.md) first. Replace `SESSION` and example paths with locations on the execution host.

## Choose a starting point

| Your goal | Guide |
| --- | --- |
| Investigate a research direction | [Direction research](#direction-research) |
| Fix a project or create code from requirements | [Code work](#code-work) |
| Analyze experiments and produce a main figure | [Analysis and data figures](#analysis-and-data-figures) |
| Draw a method diagram or concept illustration | [Method and concept figures](#method-and-concept-figures) |
| Write from existing materials | [Writing and publication](#writing-and-publication) |
| Check a result from a paper | [Finite reproduction](#finite-reproduction) |

These recipes compose capabilities, rather than select fixed execution flows.
A task can investigate sources, change code, run calculations and produce a report.
Existing results can be reused without rerunning experiments. Grant only the access
needed by the recipe; commands still need approval and model/API calls may incur charges.

The main-branch [installation](USAGE.md#installation) and public examples remain valid. Sections marked V2.10 describe ongoing work on `feat/v2.10-project-research-assistant`; checked improvements can be integrated into main in stages while V2.10 development continues. Development checks and public cases retain separate evidence; see [tested bounds and limits](EVALUATION.md#v210-development-evidence). Review [effective task settings](USAGE.md#configure-once-and-continue-from-the-result) before starting. Preferences guide deliverables, while permissions and budget settings govern access and resources.

## Direction research

**Provide:** the topic, purpose and constraints that affect the recommendation, such as dates,
hardware or methods of interest. Add specific papers or notes with `--source`, if available.

```bash
uv run simple-ar start "Investigate recent low-resource tabular learning methods for one 24GB GPU. Compare public data, code, evaluation conditions and costs. Recommend directions with sources and unresolved questions." --allow-network
```

**Inspect:** sources, comparisons, the report and unresolved questions. Distinguish source facts
from inference. Cross-paper numbers require comparable conditions; an abstract is not full-text evidence.

**Continue:**

```bash
uv run simple-ar start --resume-task "SESSION" "Investigate the two most promising routes further. Compare data licenses and training requirements, then update the report."
```

Network permission covers public source search and retrieval, not installation or command execution.
See [configuration](CONFIG_REFERENCE.md) for search providers and credentials.
Use the [structured survey example](../examples/survey/README.md) when queries, source limits
and report settings need to be fixed in advance.

## Code work

**Existing project:** provide its path, the failure, expected behavior and any known verification
procedure. You do not need to identify every file to change.

```bash
uv run simple-ar start "This project crashes on input with missing values. Locate the cause, fix the relevant code, verify the behavior and explain the changes." --source ./my-project --allow-execution
```

**New code:** describe inputs, outputs and constraints. Include execution permission when you want
the proposed program to be tested.

```bash
uv run simple-ar start "Create a Python CLI that summarizes selected metrics from CSV files in a directory. Handle missing columns and empty files. Deliver source, examples and runnable verification." --allow-execution
```

`--source` mounts originals read-only. Inspect the delivered working copy or code package;
do not assume your original project was modified.
Proposed commands still require individual approval. Review their code and side effects before execution.

On the V2.10 development branch, first-time work on an ordinary or copied project,
and feedback on a registered working copy, preserve unchanged non-runtime members.
The bounded edit set is separate from project retention: currently up to 12
edited sources, with complete copying bounded to 4,096 members, 64 MiB per file
and 128 MiB total. Retained binary/large dependencies keep their bytes without
being sent as editable text or certified as inspected. This does not discover
missing dependencies or copy an entire task history. Oversized projects are refused
before generation. Prepared projects use the same owner-bound member inventory at
approval and execution; they do not need to enumerate every member as a separate
public process input. Broader project-continuity acceptance remains incomplete.

**Inspect:** source or patches, actual verification logs, outcomes and unresolved issues.
Model review and static checks do not replace behavioral verification.
Continue the same session with a failing input or a changed requirement.

### Fixed edit scope and verification

Keep a scoped repair in the ordinary task session when the project, editable paths and
independent validator are already known:

```bash
uv run simple-ar start --kind bug_fix --goal "Fix the missing-value crash" --project ./my-project --allow "src/**" --validate "python tests/check.py"
```

Approve the displayed work and source identity before execution. This contract keeps code
changes and validation under CodeTask; generic workspace writes/processes cannot bypass it.
Feedback can continue a verified candidate as a new version, with the same scope, validator
and remaining budget. Originals and prior candidates stay unchanged. Disclose incomplete
work rather than delivering an unrelated file as a completed repair.

For explicit baseline, environment or staged approval controls, use standalone CodeTask:

```bash
uv run simple-ar code-task init --config examples/code_task_medium_review/configs/code_task.toml
uv run simple-ar code-task execute "RUN" --config examples/code_task_medium_review/configs/code_task.toml
```

Replace `RUN` with the directory printed by `init`, and follow plan/proposal approval prompts.
See the [multi-file example](../examples/code_task_medium_review/README.md) and
[configuration reference](CONFIG_REFERENCE.md) for workspace, environment, baseline and execution settings.

## Analysis and data figures

**Provide:** CSV/TSV/JSON data, a data dictionary and the meaning of each row.
For stability comparisons, explain repeated runs, pairing and cost units.
Ask for clarification where these cannot be inferred reliably.

```bash
uv run simple-ar start "Analyze these runs for performance stability and time cost. Inspect fields and missing values first, then produce a multi-panel main figure and results explanation. Deliver rerunnable source." --source ./results --allow-execution
```

The directory can include a data dictionary or README.
With a configured vision route, `--allow-vision` permits inspection of rendered images.
Otherwise, open the figures yourself to check labels and layout.

**Inspect:** methods, included/excluded rows, numerical tables, figures and reconstruction materials.
Successful execution does not validate statistical assumptions.
Check that full populations and subsets, or paired and independent samples, have not been confused.

**Give feedback:**

```bash
uv run simple-ar start --resume-task "SESSION" "Keep the calculations. Make the main figure double-column width, share the legend and distinguish series in grayscale."
```

### No-API shortcut for known columns

For deterministic descriptive statistics or supplied coordinates, run a prepared TOML:

```bash
uv run simple-ar research-session --config examples/data-curves/research.toml
```

See [coordinate figures](../examples/data-curves/README.md) and
[paired observations](../examples/data-paired/README.md).
These contain demonstration data, not scientific experiments.
The [configuration reference](CONFIG_REFERENCE.md) describes columns, missing values and plot options.

Keep `analysis.json`, its copied data and figures together. After relocating the whole analysis
directory, rebuild in an environment with this package installed:

```bash
uv run python -m simple_ar.result_analysis.table /path/to/analysis.json
```

For custom code analysis, rerun its delivered scripts instead of using this analysis-package command.

## Method and concept figures

Describe components, relationships and the point the diagram should communicate.
A draft helps establish notation and terminology:

```bash
uv run simple-ar start "Draw a method overview from this draft. Distinguish training from inference. Deliver editable layout source, SVG and PNG." --source ./draft.md --allow-execution
```

**Inspect:** arrows, grouping and labels against the actual method; check exported images for
overlap, cropping and legibility. Request changes in the same session.

Teasers and concept illustrations use an Images connection. After configuring the image route:

```bash
uv run simple-ar image --prompt "Create a concept illustration for a research report showing retrieval, experiments and feedback. Do not invent performance numbers." --output runs/concept-v1
uv run simple-ar image --prompt "Keep the main composition, simplify the background and leave room for a title." --input runs/concept-v1/IMAGE_FILE --output runs/concept-v2
```

Replace `IMAGE_FILE` with the actual image returned by the first command.
Generation and editing both call the image API; the original remains in its old directory.
A bitmap is not an editable vector diagram. Verify generated labels, structure and scientific details.

To permit image generation within a native task, explicitly set `--max-images N`.
See the [CLI reference](CLI_REFERENCE.md) for image connections, visual review and options.

## Writing and publication

**Provide:** papers, notes, data descriptions or completed results.
Specify the audience, genre, language and approximate length.
If you only want to organize existing results, say not to rerun experiments.

```bash
uv run simple-ar start "Write a technical report from these materials. Organize the question, evidence, comparisons and limitations. Preserve citations and existing figures; do not rerun experiments. Deliver editable text." --source ./materials
```

**Inspect:** prose, references, figures and unresolved items.
Check authors, dates, numbers, conditions and citation attribution.
Do not present experiments absent from the materials as completed work.

Distinguish local editing from restructuring:

```bash
uv run simple-ar start --resume-task "SESSION" "Keep the first draft. Reorganize it into problem, route comparisons, evidence limitations and recommendations without changing the results."
```

### Continue a retained draft (V2.10 development)

If a complete article, section draft or unadopted article revision remains after failed independent inspection, ask to inspect and finish that candidate. The writing owner checks original author responses and unchanged evidence before reuse; it can inspect the retained revision without reverting to the initial draft. Older drafts lacking the necessary production records do not qualify. Failed inspection remains a failure until a new inspection accepts the candidate; retained files alone are not completed delivery.

### Export ACM LaTeX and PDF

The exporter accepts an assembled report directory containing `report_body.md` and
`references.bib`; it is not a converter for arbitrary Markdown.

```bash
uv run simple-ar report-export --report-dir /path/to/assembled-report --output runs/paper-export --compile
```

Pandoc is required. Compilation also needs `pdflatex`, BibTeX and `acmart`;
SVG figures may need `rsvg-convert`. Omit `--compile` to produce the LaTeX project only.
Use a new output directory. Keep the whole project, figures and logs, and inspect every PDF page.
Compilation does not certify conference compliance.
See the [CLI reference](CLI_REFERENCE.md) for files and dependencies.

### Supplied templates

On the V2.10 development branch, native tasks can use a trusted **Pandoc LaTeX template** instead of the built-in ACM layout:

```bash
uv run simple-ar start "Use this report and supplied trusted template. Preserve the Markdown and references, export an editable TeX project and PDF, and render every page for inspection. Explain any unchecked submission requirements." --source ./paper --allow-execution
```

Keep the report and a focused template bundle in the supplied directory, not your
credentials or unrelated files. The template needs `$body$` and the standard
`$for(header-includes)$$header-includes$$endfor$` hook in its preamble.
The renderer consumes a prepared template. The inspected conversions used a supplied template and an operator-provided recipe, with zero model/API calls; both five-page outputs were readable with minor layout limits. This is not evidence of autonomous template discovery. For a new task requiring template preparation, provide an explicit
official entry point, year and submission stage. The task may use research and code capabilities to retrieve rules and adapt an example into a Pandoc wrapper, subject to network and execution permissions. Treat this as a request requiring verification, not an accepted end-to-end workflow:

```bash
uv run simple-ar start "Use https://iclr.cc/Conferences/2025/CallForPapers and its links to prepare the supplied manuscript in the 2025 initial anonymous submission layout. Preserve the prose, references, figures, tables and numbers. Retain template/rule sources and adaptation notes; deliver editable sources, PDF and every page preview. Disclose unmet rules without changing scientific content to force compliance. Do not rerun experiments or install an environment." --source ./paper --allow-network --allow-execution
```

The assistant should verify the year and stage and retain the rules and template as
task materials. Template adaptation handles variables, packages and layout; users
need not write a wrapper, select internal tools or edit task JSON. Files already in
an authorized directory can be copied into the working manuscript directory using
their observed full hashes. Use an existing Pandoc template directly; native conference
TeX needs adaptation. If retrieval fails, retain the failure and request the template
and rules rather than guessing another year or silently falling back to ACM.
Research, code and file tools provide this composition; the renderer itself does
not discover or download templates. Autonomous completion remains unverified.
Authored sections, including Abstract or Summary, stay in the supplied template's body;
their headings do not implicitly populate its `$abstract$` metadata slot.

The shared `export_report` tool accepts an optional `template` object with `path`
(relative to its Markdown), `assets` (explicit flat companion files), `engine`
(`pdflatex` or installed `xelatex`) and optional `top_level_division`
(`section`, `chapter`, `part`). Ask the assistant to inspect the layout and select
the appropriate hierarchy; no class-name rules or scientific restructuring are inferred.
At most 32 companions/16 MB are supported; no partials, scripts or custom build commands.
The standalone `report-export` command keeps its ACM default.

Template bytes, figures and output versions share the existing approval and recovery
contract. Only use trusted TeX: no shell escape is enabled, but this is not a sandbox.
Inspect warnings and every page. Fonts, explicit column widths, page limits and
anonymity still require checking; compilation alone does not establish compliance.

You can also ask the assistant to check a page limit or specific identifying
strings you supply. The shared `export_report` tool accepts, for example,
`"checks": {"max_pages": 4, "forbidden_identifiers": ["Example Author"]}`;
either rule may be omitted. These checks require PDF compilation and installed
Poppler `pdfinfo` and/or `pdftotext`, and use the same command approval and process
budget. The standalone `report-export` CLI does not expose these options.

Page limits count all pages, including references and appendices. String checks
match case-sensitive literal substrings in extracted text; they do not cover
images, metadata or indirect identification, and do not certify anonymity.
If an official limit applies only to the main body, do not use that number as an
all-PDF `max_pages` limit. Body boundaries, minimum length, stage options and other
requirements need separate checks against the rules and actual PDF. `checks` only
executes the finite checks specified; it does not certify all official requirements.
Inspect `export.json` and the retained `checks/pdfinfo.txt` or `checks/text.txt`.
Failed or unavailable checks block optional final delivery but retain the project
and diagnostics. They do not automatically revise the manuscript. Reading a saved
result does not rerun checks; changed files need a new export.

The development exporter budgets figure and caption height together using the actual template layout. Captions that cannot fit, and unsupported composite/nested images, fail explicitly with sources retained. It preserves scientific text, numbering, links and citations; inspect compact figures and table wrapping yourself.

Model-based visual inspection additionally needs a configured vision connection
and explicit `--allow-vision`; rendering alone does not grant it.

## Finite reproduction

**Provide:** the paper, target conclusion, machine resources, available implementation and data.
Ask for an investigation and feasible scope before approving preparation and execution.

```bash
uv run simple-ar start "Check one main conclusion from this paper. Inspect the supplied implementation, data and dependencies. Propose a CPU-sized scope and short run, then execute with approval and explain differences." --source ./paper.pdf --source ./implementation --allow-network --allow-execution
```

Missing implementations, data permissions or resources may require user preparation.
Automatic reproduction of arbitrary papers is not guaranteed.

**Inspect:** original conditions, actual protocol, environment and data descriptions, short-run
and measurement logs, and differences. Label limited scopes or synthetic adaptations explicitly;
do not present them as full reproduction.

For a prepared protocol, start with the [low-cost conclusion check](../examples/conformal_reproduction/README.md).
See [configuration](CONFIG_REFERENCE.md) for structured environment settings.
Failed preparation is not a measurement. Virtual environments isolate dependencies, not hostile code.

## View results and continue

Use the continuation command printed for the task. New native and common structured tasks continue with:

```bash
uv run simple-ar results "SESSION"
uv run simple-ar start --resume-task "SESSION" "Your follow-up request"
```

Saved legacy ResearchApplication sessions retain their original command:

```bash
uv run simple-ar research-session --session-root "SESSION" --model env
```

### Reuse materials in a new task (V2.10 development)

To give saved outputs a new goal and budget, add `from_session = "../saved-task"` and `reuse = ["artifacts"]` to the existing native `[task]` TOML table. You can also select named groups listed by `simple-ar results`; group names are not filesystem paths. The session path is relative to the configuration. Reuse checks material bytes and does not inherit permissions, budget or execution state. To continue the original task, use `--resume-task` instead.

### Add a trusted local extension (V2.10 development)

Add `extensions = ["../my-skill"]` to the same native `[task]` table and start with `--task-config PATH`. Selected packages share the task's registry, history and recovery. V1 accepts instructions and Python tools declaring read-only/plan operations; missing or changed packages block continuation. Python runs with the host process's OS privileges, including direct file, process and network access. The declarations and restricted host services are not a sandbox. Use only trusted packages; see [setup and identity rules](LOCAL_EXTENSIONS.md) (English).

Keep complete delivery directories rather than copying only a PDF and losing figures, sources
or reconstruction materials. When paused or failed, inspect the reason first.
See the [CLI reference](CLI_REFERENCE.md) for approval, retry and allowance rules.
Do not delete records or repeatedly start new tasks as a substitute for recovery.
