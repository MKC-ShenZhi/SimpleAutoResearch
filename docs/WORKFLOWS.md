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

These are starting points, not exclusive modes. A task can investigate sources, change code,
run calculations and produce a report. Existing results can be reused without rerunning experiments.

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

### Supplied templates (V2.10 development)

Native tasks can use a trusted **Pandoc LaTeX template** instead of the built-in ACM layout:

```bash
uv run simple-ar start "Use this report and supplied trusted template. Preserve the Markdown and references, export an editable TeX project and PDF, and render every page for inspection. Explain any unchecked submission requirements." --source ./paper --allow-execution
```

Keep the report and a focused template bundle in the supplied directory, not your
credentials or unrelated files. The template needs `$body$` and the standard
`$for(header-includes)$$header-includes$$endfor$` hook in its preamble.
This is not automatic conversion of arbitrary conference TeX or official-template discovery.
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

For finite rules explicitly supplied by the user, `export_report` also accepts
`"checks": {"max_pages": 4, "forbidden_identifiers": ["Example Author", "某研究所"]}`.
Either rule can be omitted; the object must contain at least one. `max_pages`
is a positive integer counting **all** PDF pages, including references/appendices.
Identifiers are 1–32 unique, nonblank literal strings, each at most 200 Unicode
characters without control characters. Matching is case-sensitive exact substring
matching, without regex, normalization, whitespace joining or OCR. Supply the actual
identifiers to check; the assistant must not infer author identities or venue rules.

Checks require `compile_pdf=true` and existing Poppler `pdfinfo` (page count) and/or
`pdftotext -enc UTF-8` (text). Each requested observation uses the same approved
runner/process ledger: at most 30 seconds per tool, PDF at most 32 MB, page-info
output at most 64 KB and UTF-8 text at most 1 MB. No tools, fonts or packages are
installed. Page and text results are separate in `export.json` under `checks`;
accepted output is retained in `checks/pdfinfo.txt` and/or `checks/text.txt`, with
evidence hashes and the checked PDF hash. Inspect those files and process receipts.
No extractable text, replacement characters, unavailable tools, exceeded bounds,
truncated output or failed rules prevent optional final delivery while preserving
the editable project and diagnostics. No automatic edits, suppression of facts or
extra compilation attempts follow a check failure.

A literal miss only means the supplied string was absent from extracted text.
Images, PDF metadata and indirect identity are not covered; anonymity, visual
quality and official compliance remain unverified.
Successful and failed declared checks share the existing output byte binding.
Reading a saved result does not rerun checks, and changed files require a new
export identity. Omitted checks preserve historical request/manifest bindings;
old exports acquire no retroactive check evidence. The standalone `report-export`
CLI has no new flags; this optional declaration belongs to the shared tool.

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

For native tasks:

```bash
uv run simple-ar results "SESSION"
uv run simple-ar start --resume-task "SESSION" "Your follow-up request"
```

For structured `research-session` tasks, use the session path printed by that command:

```bash
uv run simple-ar research-session --session-root "SESSION" --model env
```

Keep complete delivery directories rather than copying only a PDF and losing figures, sources
or reconstruction materials. When paused or failed, inspect the reason first.
See the [CLI reference](CLI_REFERENCE.md) for approval, retry and allowance rules.
Do not delete records or repeatedly start new tasks as a substitute for recovery.
