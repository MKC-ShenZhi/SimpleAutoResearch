# VLA policies and world models: a generated survey

[中文](README_zh.md) · [Showcase](../../docs/showcase/README.md)

**Read a complete framework-generated report, or rebuild its editable sources
without a model call.** The ten-page report compares what VLA policies and world
models predict, where predictions enter control, and the limits of the evidence.
It contains 16 references, two explanatory figures and two tables.

[Read the PDF](report/survey.pdf) · [Read the Markdown](report/manuscript.md) · [中文简报](report/briefing_zh.md)

![Where prediction enters the control loop](report/figures/fig1_prediction_in_loop.png)

## Rebuild the delivered report

No API key is needed. These commands rebuild existing prose, **not** repeat the
source search or generate a new survey. Copy the example to a fresh directory:

```bash
mkdir -p runs/examples
cp -R examples/robotics-survey runs/examples/robotics-survey
cd runs/examples/robotics-survey/report
```

On PowerShell, use `New-Item -ItemType Directory -Force runs/examples` and
`Copy-Item examples/robotics-survey runs/examples/robotics-survey -Recurse`, then
change to the same `report` directory. Do not reuse a destination from an older run.

Reading the PDF needs no extra software. Building it needs Pandoc, pdfLaTeX,
BibTeX and the packages used by `survey.tex`, including Latin Modern (`lmodern`).
For Ubuntu, these are provided by `pandoc`, `texlive-latex-extra`,
`texlive-fonts-recommended` and `lmodern`. Use trusted inputs: TeX is not a sandbox.

To recompile the delivered TeX with the included PNG figures:

```bash
pdflatex -no-shell-escape -halt-on-error survey.tex
bibtex survey
pdflatex -no-shell-escape -halt-on-error survey.tex
pdflatex -no-shell-escape -halt-on-error survey.tex
```

Stop if any command fails. To regenerate TeX after editing `manuscript.md`, first run:

```bash
pandoc manuscript.md -s --natbib --bibliography=references.bib -V biblio-style=plainnat -V geometry:margin=2.2cm -V fontsize=10pt -V linkcolor=blue -V urlcolor=blue -V colorlinks=true --metadata lang=en -o survey.tex
```

Then repeat the TeX commands. Optional figure regeneration uses Python's standard
library and `rsvg-convert` (Ubuntu package `librsvg2-bin`):

```bash
python3 make_figures.py
```

Linux rebuilds were verified; other platforms and exact PDF byte identity are not
certified. Generated files may differ with TeX, Pandoc, fonts and timestamps.

## Try your own investigation

Follow [Get started](../../docs/USAGE.md) for model setup and [task guides](../../docs/WORKFLOWS.md)
for source retrieval. A bounded starting request is:

```bash
uv run simple-ar start "Compare VLA policies and world models for a robotics reading group. Distinguish prediction targets, how they enter control, and real-robot versus simulation evidence. Start with a bounded technical memo, source links and a comparison table; disclose full-text gaps and do not create a cross-paper performance ranking." --model profile:daily --allow-network --max-turns 20 --max-tokens 400000
```

This starts a new paid investigation, not this report's replay or a guarantee of
ten pages. Add accessible material with `--source PATH`. Code execution, visual
inspection and PDF compilation require their own permissions and dependencies;
network permission alone does not grant them. Review the initial memo before
requesting a longer report.

## Provenance and limits

Prose, figures, bibliography and TeX are unchanged framework exports. Maintainers
selected the public files and wrote these reproduction instructions; original
build logs, workspace-specific helpers, article snapshots and task journals are
not included. `provenance.json` records the source hashes.

The final delivery used code equivalent to `d3b0721`; earlier candidates,
connection changes, operator feedback and explicit continuations are part of the
history. Humans supplied goals, approvals, environment dependencies and independent
checks, not the scientific prose or figure code. All-stage settled usage was
**164 physical requests / 6,086,900 tokens**, with unknown usage retained separately.
This is development cost, not an unattended single-run result or a monetary bill.
Final writing used official `deepseek-flash`, low thinking and streaming.

The task remains `partial`: this is an opportunistic 16-paper corpus, not a
systematic or comprehensive 2026 survey. Only two papers are from 2026; PlayWorld's
full text was acquired but only abstract-level statements were used. Some author
lists were not verified and study names are used instead. Figures are qualitative
reading aids, not new experiments or comparable performance measurements. An
independent final ten-page visual check and relocated build were completed; full
automated page-by-page vision sign-off was not.

Project licensing is undecided. Follow upstream rights and consult original papers
before relying on scientific claims; this export contains no third-party full texts.
