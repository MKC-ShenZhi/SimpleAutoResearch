# Delivered work

[中文](README_zh.md) · [Project home](../../README.md)

These examples show artifacts produced through SimpleAutoResearch's native task and feedback path during development acceptance. They are not benchmarks or promises of unattended completion. Humans supplied goals, approvals and independent checks; framework fixes and additional authorized budgets were needed. No manually authored RSI report is presented here as framework output.

## Bike Sharing: from observations to a revised report

**The task.** Analyse the public [UCI Bike Sharing dataset](https://doi.org/10.24432/C5W894), explain patterns and limitations, and deliver rerunnable analysis, a figure and a report. The input included daily and hourly files plus their description; the analysis used 731 daily observations from 2011–2012, without pooling daily and hourly rows.

**The feedback.** Break the year-to-year comparison down by working-day status and distinguish within-group changes from changes in the mix of days. Preserve the original accepted tables.

![Revised six-panel analysis](assets/bike-sharing-revised.png)

**The delivery.** The framework generated analysis code, numerical tables, the revised figure and manuscript, and an ACM export. The public selection contains the figure and [four-page PDF](assets/bike-sharing-report.pdf); the original acceptance package also retained scripts, data references and rebuild materials.

[![First page of the delivered report](assets/bike-sharing-report-page-1.png)](assets/bike-sharing-report.pdf)

Independent checking recomputed 732 table cells with no differences and checked relocation/rebuilding. The saved content review records no manual rewriting of the canonical manuscript. These checks support this artifact, not all possible analyses.

**What remains imperfect.** This is descriptive observational analysis, not evidence of causality or independent repeated experiments. Dense figure labels, awkward code and table wrapping, and a sparse last page remain visible. Compilation is not a scientific quality score.

**Cost and version context.** The saved run records 63 physical model requests, 2,027,970 settled tokens and 40 process invocations including compilation. Five unknown-use reservations totaling 127,644 tokens were retained separately. The original 40-request/1.6M-token allowance was insufficient; four additional bounded authorizations were recorded. Publication continued through r1–r3 development candidates, including a caption-ownership fix. This was not a single frozen-version, single-budget success.

## Study figure: feedback without repeating the experiment

**The task.** Turn already recorded air-quality calibration results into an editable explanatory figure. Then revise it for a single-column, grayscale presentation, separating fitting, prediction and evaluation.

| Initial figure | Feedback version |
| --- | --- |
| ![Initial study figure](assets/study-figure-before.png) | ![Revised study figure](assets/study-figure-after.png) |

The first figure had layout collisions and its visual check encountered a service failure; it was not a passing final artifact. The feedback version changed the layout and hierarchy while using existing recorded measurements. It did not retrain a model, run a new experiment or establish external-site validity.

The original task delivered editable drawing source and exports. Independent review recorded two relocated drawing rebuilds with byte-identical rasters and 132 original files unchanged. This public selection publishes the before/after PNGs, not the complete execution archive.

The run records 34 model requests, 864,326 settled tokens and four processes; five unknown-use reservations remain in its ledger. Goals, execution permissions and review were human-supplied.

## Provenance and reuse

The published files are byte-for-byte copies of saved acceptance artifacts, not redesigned marketing mockups. [Artifact checksums and source identities](artifacts.json) distinguish the two tasks and versions. Source identities are repository-relative development records; those ignored run archives are not included in a checkout.

The Bike Sharing data is attributed to UCI and Fanaee-T & Gama, *Event labeling combining ensemble detectors and background knowledge*, [DOI](https://doi.org/10.1007/s13748-013-0040-3). Consult upstream data terms before reuse. These files demonstrate outputs; they are not a downloadable reproduction bundle or evidence that V2.9 is fully finished.

To try your own task, use [Get started](../USAGE.md) and [Task guides](../WORKFLOWS.md). Actual model cost, iteration count and output quality depend on the task and services.
