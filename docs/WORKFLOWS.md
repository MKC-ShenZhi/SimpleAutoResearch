# Task Recipes

Five copy-and-run recipes covering the most common research workflows. Every recipe uses the same `simple-ar start` entry point with explicit permission flags; if you prefer a GUI, toggle the same options inside the [Desktop Workbench](USAGE.md#desktop-research-workbench).

[中文](WORKFLOWS_zh.md) · [Get started](USAGE.md) · [CLI reference](CLI_REFERENCE.md) · [Showcase](showcase/README.md)

---

## 1. Open-Topic Literature Survey (Network Search + BibTeX + PDF)

```bash
uv run simple-ar start \
  "Conduct a technical survey comparing test-time compute scaling and process reward models: retrieve 8-12 verifiable papers into references.bib, write a structured survey with comparison tables, and export a compiled PDF." \
  --model profile:daily --allow-network --allow-execution --yes
```

- To synthesize local PDFs offline, omit `--allow-network` and mount `--source ./papers`.

---

## 2. Scientific Code Experiments, Ablations & Figures

```bash
uv run simple-ar start \
  "Reproduce the baseline in the supplied project, run at least 3 controlled ablation/improvement experiments, generate high-resolution comparison figures (PNG), and write a report with quantitative tables and limitations." \
  --source ./my_project --model profile:daily --allow-execution --yes
```

- On Linux machines with `bubblewrap` (`bwrap`), add `--sandbox-execution` to run ordinary commands inside an isolated workspace sandbox.

---

## 3. Analysis & Publication Figures from Saved Results (No Refitting)

```bash
uv run simple-ar start \
  "Analyse the saved evaluation tables, generate two high-resolution comparison figures (PNG) and a concise report; do not refit models, and deliver rerunnable plotting scripts." \
  --source ./saved_results --model profile:daily --allow-execution --yes
```

---

## 4. English Conference Paper Export (e.g., NeurIPS / EMNLP Template)

Place the official `.sty` / `.cls` file in a folder (e.g. `./neurips_template`) and mount it read-only via `--source`:

```bash
uv run simple-ar start \
  "Conduct a controlled study on this dataset, generate publication figures, write an English paper with verifiable BibTeX citations, and compile a PDF using the supplied neurips_2025.sty template." \
  --source ./neurips_template --source ./data \
  --model profile:daily --allow-network --allow-execution --yes
```

---

## 5. Feedback Iteration, Cross-Project Reuse & Continuation

```bash
# Inspect delivered artifacts, ledger accounting, and unresolved items
uv run simple-ar results "SESSION_PATH"

# Give targeted feedback on the same session (preserves v1, skips unrelated reruns)
uv run simple-ar start --resume-task "SESSION_PATH" \
  "Keep fig_v1.png intact, create a paired side-by-side fig_v2.png with exact value labels, and update the report."

# Reuse delivered code and SKILL.md methodology on a brand-new dataset
uv run simple-ar start --from-session "SESSION_PATH" --reuse artifacts \
  --source ./new_dataset --model profile:daily --allow-execution \
  "Transfer the prior methodology and package to this new dataset and compare whether the findings hold."

# Resume after a budget pause or transient provider disconnect
uv run simple-ar start --resume-task "SESSION_PATH" \
  --authorize-remaining llm_requests=20 --authorize-remaining total_tokens=1000000 \
  --authorization-id continue-1 --authorization-reason "Finish final review and delivery" --yes
uv run simple-ar start --resume-task "SESSION_PATH" --retry-model --yes
```
