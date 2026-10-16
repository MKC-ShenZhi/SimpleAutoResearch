# Prepared, low-cost paper-conclusion check

This example checks the known-weight split conformal construction from
[Tibshirani et al. (2019)](https://arxiv.org/abs/1904.06019) under a **declared
synthetic adaptation**. It does not reproduce the published airfoil experiment,
all paper results, or a PaperBench task. There is no GPU training, code generation
or new candidate method. `run.py` uses only Python's standard library.

From the repository root, explicitly download the supplied primary source:

```bash
mkdir -p runs/conformal-reproduction/inputs
curl --fail --location https://arxiv.org/pdf/1904.06019 \
  --output runs/conformal-reproduction/inputs/paper.pdf
uv run simple-ar research-session --config examples/conformal_reproduction/research.toml
```

Complete [model setup](../../docs/USAGE.md) and install dependencies with `uv sync`.
An unavailable or failed PDF parser is a source-access limitation, not proof of
a successful review. The command's environment
must have `python` on PATH. Advanced users can specify a different argv in TOML.

The ordinary and weighted constructions share every calibration/test draw.
Each repetition draws a new calibration set and test point. The weighted
quantile includes the test covariate's weight as mass at infinity. The fixed
predictor removes model fitting from this isolated construction check.
The additive Gaussian noise has standard deviation 0.1 (variance 0.01),
matching Python's `random.gauss` convention; it is not a variance of 0.1.

Runtime artifacts go under `runs/conformal-reproduction`, not this example.
The application saves command output, metrics, analysis, report and audit in a
new session. The numerical JSON also records the protocol and Monte Carlo error.
Inspect it independently; `completed` means artifacts delivered, not scientific
success. The fixed numerical run can be checked without API calls:

```bash
python examples/conformal_reproduction/run.py --seed 7 --repetitions 3000 \
  --output runs/conformal-reproduction/observations/standalone.json
```

To continue an interrupted session, use the printed session-root, not a new
start command. Existing research-session resume/recovery rules apply. Report
refresh and ACM export reuse its evidence without rerunning the simulation.
