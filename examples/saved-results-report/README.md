# From saved results to a figure and report

[中文](README_zh.md) · [Showcase](../../docs/showcase/README.md)

**Try the delivered analysis without an API key or model training.** This example
contains a framework-generated six-page report, a five-panel figure, four tables,
the plotting source and five frozen result files.

[Read the PDF](report/report.pdf) · [Read the Markdown](report/manuscript.md) · [View the figure](report/figures/monthly_update_panel.png)

## Rebuild the analysis

From the repository root, install the locked dependencies and copy the example
to a new directory. Work on the copy: the original generated script writes beside
itself.

```bash
uv sync --locked
```

Linux/macOS:

```bash
mkdir -p runs/examples
cp -R examples/saved-results-report runs/examples/saved-results-report
uv run python runs/examples/saved-results-report/report/make_figure_tables_v2.py
```

PowerShell:

```powershell
New-Item -ItemType Directory -Force runs/examples | Out-Null
Copy-Item examples/saved-results-report runs/examples/saved-results-report -Recurse
uv run python runs/examples/saved-results-report/report/make_figure_tables_v2.py
```

Use a fresh destination each time. Open `report/figures/monthly_update_panel.png`
under the copied directory; the script also writes four CSV tables and
`report/derived_metrics.json`. It exits nonzero if its consistency checks fail.
It re-aggregates saved results; it does **not** repeat the original experiment.
Linux/Python 3.12 replay is checked; other platforms and fonts are not certified.

### Optional: rebuild the PDF

The delivered PDF opens without TeX. To recompile the editable export, install
pdfLaTeX and its required packages (see `report/latex/main.tex`), then run:

```bash
cd runs/examples/saved-results-report/report/latex
pdflatex -no-shell-escape -halt-on-error main.tex
pdflatex -no-shell-escape -halt-on-error main.tex
```

This builds `main.pdf` from the delivered TeX. It does not regenerate prose from
Markdown or automatically embed changes to the plotting script. For a new report
or coordinated figure/text revision, start a model-assisted task instead.

## Ask the assistant to do new work

After [configuring a model](../../docs/USAGE.md), run from the repository root:

```bash
uv run simple-ar start "Inspect these saved calibration results. Compare model form, update policy, coverage and recorded computation cost. Produce a multi-panel figure, editable source and a short Markdown report. Do not refit models; distinguish measured timings from annualised scenarios and do not invent the application domain." --source examples/saved-results-report/report/inputs --model profile:daily --allow-execution --max-turns 20 --max-tokens 400000
```

This is a **new paid task**, not a replay of the original conversation or a
guarantee of the same report. Review proposed commands before approving them.
Keep the printed task path for [feedback and recovery](../../docs/USAGE.md).
The budget is a ceiling, not a cost estimate or a promise that the task will finish.

## Provenance and limits

The scientific prose, figure source, numbers and TeX are unchanged exports of a
native framework task; maintainers selected the public files and wrote this guide.
Final delivery used code equivalent to `d3b0721`; the whole development history
spans earlier candidates and multiple model connections. Humans supplied goals,
defect feedback, approvals, environment setup and independent checks, not the
scientific text or plotting implementation.

The complete development history recorded **180 settled physical requests and
5,871,880 settled tokens**, plus retained unknown usage. This is not a single-run
cost or a monetary bill. Final writing used official `deepseek-flash` with low
thinking and streaming. Offline replay makes no model requests.

Independent verification checked 72 numerical conditions and relocated rebuilds;
the final six pages were inspected separately. The task remains `partial`: no
uncertainty intervals, only one test season, machine-specific timings, minor TeX
overflow warnings and no successful fresh paid visual review of the final figure.
The inputs do not establish a physical application domain or target units.

`provenance.json` binds the selected files to the original delivery. Original
failures and journals are retained privately; they are not redistributed here.
Historical environment paths in saved metadata are not runtime dependencies.
No raw third-party articles, credentials or task journals are included. Project
licensing remains undecided; this export does not grant additional upstream rights.
