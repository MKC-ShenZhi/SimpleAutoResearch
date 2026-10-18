# Examples

Each directory is one complete case with its own inputs, configuration, entry
command and expected outputs. Run commands from the repository root.

| Case | Goal | Entry and preparation |
| --- | --- | --- |
| [Saved-result figure feedback](figure-feedback/README.md) | Replay an actual delivered comparison figure; optional new model-assisted request | `uv run python examples/figure-feedback/build_figure_v2.py --output runs/examples/figure-feedback/replay`; no API or training for replay; fresh output directory required |
| [Survey](survey/README.md) | Literature survey, no training | `research-session --config examples/survey/research.toml`; configure model access |
| [Data analysis](data-analysis/README.md) | Descriptive statistics and editable plots of existing data | `research-session --config examples/data-analysis/research.toml`; no model or GPU; Matplotlib is installed with the package |
| [Data curves](data-curves/README.md) | Supplied numeric coordinates, explicit gaps, editable line/scatter figures | `research-session --config examples/data-curves/research.toml`; no model or GPU; no separate plotting setup |
| [Paired data](data-paired/README.md) | Matched-row differences, joint missing counts and standard-error plots on artificial data | `research-session --config examples/data-paired/research.toml`; no model or GPU; bundled plotting library; demonstration, not scientific evidence |
| [Conformal reproduction](conformal_reproduction/README.md) | Low-cost, fixed-protocol synthetic check of a published conclusion | Download the supplied paper, then run the checked-in research config; standard-library simulation, no GPU |
| [ICML 2025 RCP subset](rcp_reproduction/README.md) | Author-code-assisted House comparison, two fixed methods and ten paired seeds | Prepare the pinned author source, paper, data and scientific Python first; actual CPU measurement case, not whole-paper or benchmark reproduction. Review the report separately. |
| [Continual learning](continual_learning/README.md) | Mammoth/CIFAR-100 research improvement | Prepare the case-declared project, data and split under `runs/assets`, then run the checked-in research config |
| [Digits MLP](code_task_digits_mlp/README.md) | Small CPU model-code improvement | Included project/task/config; requires NumPy and scikit-learn |
| [Medium review](code_task_medium_review/README.md) | Small multi-file code improvement | Included project/task/config; Python standard library |

The coding cases use standalone `code-task`; they are not full autonomous
research or paper-quality acceptance. Research cases use `research-session`.
A runnable case is not a claim of scientific or writing-quality acceptance.

[TabM research](tabm_research/README.md) is kept separately as an experimental
prepared-server diagnostic. Its external paper, code, data and Python environment
are not included in a fresh checkout; it is not a quick-start case.

## Files and outputs

- `examples/<case>/`: reusable case inputs only, never generated runs or credentials.
- Model catalog TOML: named connections and routes; see [model setup](../docs/USAGE.md).
- `.env`: ignored credentials and, optionally, the catalog path. Task assets and
  interpreters belong in the task TOML; `{config_dir}` keeps case paths portable.
- `runs/`: generated state, workspaces, measurements and reports.
- `tests/fixtures/`: internal regression material, not additional user examples.
- `scripts/research_session_smoke.py`: offline developer wiring check, not an acceptance case.

Keep credentials out of committed templates. Do not start GPU examples just to
validate configuration. Removed historical cases
remain recoverable from Git; do not copy dated outputs back into examples.
