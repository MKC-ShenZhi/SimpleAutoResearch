# Small-memory continual-learning survey

Produce a literature survey of small-memory continual learning for image
classification, comparing methods, evidence and open questions. The entire user
request and limits are in `research.toml`; no project, dataset or GPU is needed.

Complete [model setup](../../docs/USAGE.md), then run from the repository root:

```bash
uv run --no-sync simple-ar research-session --config examples/survey/research.toml
```

This makes real model and literature-provider requests. API request/token totals
are uncapped; process execution is disabled. The default reads available abstracts
and metadata, not downloaded full texts. It is a basic workflow case, not a
full-text review or publication-quality guarantee.

Outputs are created under `runs/survey/<session>/`, including the report and audit.
Keep the printed session path to resume with the same config plus `--session-root`.
Provider failure may pause the run; do not delete previous evidence to retry.
