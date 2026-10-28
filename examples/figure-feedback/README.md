# Saved-result figure feedback

[中文](README_zh.md) · [Showcase](../../docs/showcase/README.md)

This is a small **replayable export from a real framework acceptance task**, not a new benchmark or training example. The renderer, five saved-result files and caption are unchanged copies of the delivered output. No model key, raw observation download, training or network access is needed for replay.

## Replay the delivered figure

From the repository root after `uv sync --locked`:

```bash
uv run python examples/figure-feedback/build_figure_v2.py --output runs/examples/figure-feedback/replay
```

Use a fresh output directory each time: the original renderer intentionally refuses a nonempty directory. Outputs are `main.svg`, `main.png`, `preview.png`, `panel_D_review.png` and `audit.json`. Matplotlib is a core dependency. The accepted Linux/Python 3.12 execution uses one thread and up to 120 CPU seconds/2 GiB address space where supported; it is not an OS sandbox or a certified Windows/macOS replay.

![Delivered figure](../../docs/showcase/assets/landscape-feedback-after.png)

The renderer checks the input hashes and 354 saved-value/layout conditions. Independent acceptance separately matched all eight displayed values to the saved CSV and verified byte-identical main/preview PNGs after relocation in the same environment. Different Matplotlib/font versions need not produce identical pixels.

## Try a new model-assisted task

Configure text and vision profiles first; see [setup](../../docs/USAGE.md). This starts new paid work with its own budget, not a replay of the original model conversation:

```bash
uv run simple-ar start "Use these saved results and the initial figure to make an editable grayscale landscape figure. Make Frozen versus Monthly errors visually comparable for both model families and both metrics, preserve exact values and units, label separate scales clearly, retain fitting/prediction/evaluation roles, and deliver source, SVG, PNG, caption and replay instructions. Do not refit models or acquire data." --source examples/figure-feedback/data --source docs/showcase/assets/landscape-feedback-before.png --model profile:daily --allow-execution --allow-vision
```

Approve only the proposed commands you have reviewed. A text profile alone does not enable vision. Results, cost and iteration count may differ. Use [task feedback](../../docs/USAGE.md#give-feedback-and-resume) to continue your own task.

## Provenance and limitations

The accepted revision used framework commit `f45b596`, CCTQ/`gpt-6.1-sol`, 11 physical requests and 211,774 recorded tokens, one rendering process (2.56 s), with no new unknown-use reservation or allowance extension. Humans supplied the feedback, approved execution and independently inspected results; they did not edit the delivered source or prose. An earlier failed feedback task remains failed with its own costs. This separate bounded task reused only the exported initial figure/materials, not its ledger.

The upstream observations are [UCI Air Quality, Saverio Vito](https://doi.org/10.24432/C59K5F); these are saved derived calibration results, not a fresh independent validation. See upstream usage terms and cite the dataset. The example includes historical environment paths in saved metadata; these are provenance, not dependencies or credentials.

No refitting, scientific revalidation, external-site test or physical print proof is claimed. Linear/quadratic axes have different explicit scales. The unchanged caption says “Left to right” although panels A–C are stacked on the left; treat this as a recorded wording limitation. The native task's qualified `partial` status is retained. Framework licensing is not yet selected; publication of this example does not grant blanket rights to third-party materials.
