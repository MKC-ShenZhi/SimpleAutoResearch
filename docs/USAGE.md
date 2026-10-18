# Get started

[中文版本](USAGE_zh.md)

Follow this tutorial to install SimpleAutoResearch, configure a model, investigate
local materials, inspect the delivery, and continue the same task.
For task recipes, see [Task guides](WORKFLOWS.md);
for options and fields, use the [CLI](CLI_REFERENCE.md) and
[configuration](CONFIG_REFERENCE.md) references.

## Your first task

The example uses the checked-in data-curve case as accessible material.
It asks for an explanation first; process execution is an optional next step.
The native `start` entry is experimental. Inspect the actual output and its
evidence limits before relying on it.

### Installation

You need Python 3.12+, Git, uv, and ripgrep (`rg`) on the execution host.
Use the main branch. From a terminal:

```bash
git clone --branch main https://github.com/Wchanging/SimpleAutoResearch.git
cd SimpleAutoResearch
uv sync
uv run simple-ar start --help
rg --version
```

Run the remaining examples from this repository directory.
On Windows, commands use the same `uv run simple-ar` entry.

### Configure a model connection

Save the following as `~/.config/simple-ar/models.toml`, or set an alternative location below.
Replace the endpoint and model ID with values supported by your provider.
Native tasks require a Chat connection; the `code` route supports later code work.

```toml
version = 1

[profiles.daily]
api = "openai_chat"
base_url = "https://your-provider.example/v1"
model = "your_model_id"
api_key_env = "RESEARCH_API_KEY"
capabilities = ["text", "code"]
stream = true

[routes]
default = "daily"
code = "daily"
```

This example uses Chat streaming. Check that your provider supports the selected
model and capabilities; output limits and timeouts are configurable in the catalog.

Create or edit a private `.env` in the command's current directory, preserving
existing entries. Add the actual referenced credential:

```dotenv
RESEARCH_API_KEY=your_actual_secret
```

Here `~` means your user directory, including on Windows; the default path is
`.config/simple-ar/models.toml` beneath it. For a different location, set this
variable in the terminal or in `.env`:

```bash
export SIMPLE_AR_MODELS_CONFIG=/absolute/private/path/models.toml
```

```powershell
$env:SIMPLE_AR_MODELS_CONFIG = 'C:/private/models.toml'
```

Credentials come from the environment or the current directory's `.env`;
existing environment variables take precedence. Keep both files private and
out of source mounts. Ensure they remain discoverable when changing terminals
or directories. You do not need duplicate global model/endpoint settings.

Check the selected connections locally:

```bash
uv run simple-ar models --profile daily --purpose text
uv run simple-ar models --profile daily --purpose code
```

`credential=set` means a credential was found, not that the remote API works.
Optional writing, vision and image routes, transport settings, and the legacy
environment-only fallback are in
[named connections](CONFIG_REFERENCE.md#named-model-connections).
`--model env` uses the catalog's text/default route when a catalog exists.
Without a catalog, native tasks require `SIMPLE_AR_LLM_API=chat` in the
environment or current-directory `.env`; the legacy default is `responses`.
Responses streaming is unavailable, including for structured tasks.

### Supply accessible materials

This repository includes a README, TOML and CSV for a demonstration plot:

```bash
uv run simple-ar start "Explain the supplied curve data and configuration in a short Markdown note. Distinguish demonstration values from measured experiments; list evidence limits and retain the initial version." --source examples/data-curves --model profile:daily --yes
```

For your own task, replace `examples/data-curves` with a real file or directory
on the execution host. Repeat `--source PATH` for additional materials.
Mounted originals are read-only; your goal and selected material content are
sent to the model. Do not mount credentials, private configuration or unrelated data.

### Start with a task

`--yes` accepts the displayed task summary. The example grants neither public
source retrieval nor process proposals. Add access only when the task needs it:

| Need | Native option | Boundary |
| --- | --- | --- |
| Public search and source acquisition | `--allow-network` | Does not authorize installation or commands |
| Propose a calculation or other process | `--allow-execution` | Each command still requires approval |
| Inspect rendered images | `--allow-vision` | Sends images to the configured vision route |
| Generate/edit raster concepts | `--max-images N` | Requires an image route and retains the allowance |

Access and budgets are retained across feedback and resume.
`--max-turns` and `--max-tokens` bound model work; available request slots do
not guarantee that the next request fits the token balance.
See [native entry](CLI_REFERENCE.md#native-task-entry-experimental) for limits.

If a process is needed, continue with the printed `Task session` path:

```bash
uv run simple-ar start --resume-task "SESSION" "Calculate the requested quantities; propose the command and retain the explanation." --allow-execution --yes
```

Inspect the proposed command, source, paths and side effects before approving
it in the terminal. Without interactive approval, or after declining, the task
pauses and prints an `Approval token`. After review, use that exact current token:

```bash
uv run simple-ar start --resume-task "SESSION" --approve-execution "TOKEN"
```

`SESSION` and `TOKEN` are placeholders. Each new command may need new approval.
Workspace isolation is not an operating-system sandbox.

### Inspect results

```bash
uv run simple-ar status "SESSION"
uv run simple-ar results "SESSION"
```

Open the actual paths printed by `results`. Check the prose, source references,
calculated results, validation status and unresolved work relevant to your goal.
A saved file or delivery record is not a quality certificate.
During a pause, `results` lists no current reusable delivery for a native task;
older files may remain on disk from before your feedback.

For deeper artifact inspection:

```bash
uv run simple-ar inspect "SESSION"
uv run simple-ar search-artifacts "SESSION" "evidence"
```

### Give feedback and resume

Record the initial artifact paths, then request a separate feedback version:

```bash
uv run simple-ar start --resume-task "SESSION" "Keep the initial version. Add a section explaining missing-value handling and the limits of the evidence; deliver a separate feedback version."
uv run simple-ar results "SESSION"
```

Feedback can call the model or propose commands under the same budget,
materials, connection and approval rules. To add evidence in the same task,
include `--source PATH` with your feedback.

To continue a saved boundary without new feedback:

```bash
uv run simple-ar start --resume-task "SESSION"
```

A completed task with no pending action returns its saved terminal state.
After interruption, confirm that the previous worker has stopped before
continuing; in-flight operations and unknown side effects must not be blindly replayed.
For an eligible terminated model failure, use `--resume-task SESSION --retry-model`
separately from feedback or new authorization. It retains failed charges and
uses remaining budget; see the [recovery conditions](CLI_REFERENCE.md#native-task-entry-experimental).
See the CLI reference for additional allowances and recovery options.

## Configure once and continue from the result

Export an annotated native task configuration, review it, then start. Exporting needs no model credentials and creates no task:

```bash
uv run simple-ar start "Inspect these results, compare stability and cost, and make a main figure and explanation as needed" --source ./my-results --model profile:daily --max-processes 16 --max-process-seconds 600 --write-task-config task.toml
uv run simple-ar start --task-config task.toml
```

Relative material paths use the configuration's directory. Files must be readable on the execution machine.
Explicit CLI values override the corresponding file settings. Review the summary and enter `y` to start.
`max_processes` and `max_process_seconds` enter the actual ledger as cumulative invocation and wall-time caps, including search backends, rather than per-command timeouts.
Zero invocations disables processes; proposing commands still requires individual approval.
Natural-language resource suggestions do not automatically become hard limits: check the summary and explicit settings.

Find and continue existing tasks without locating internal JSON:

```bash
uv run simple-ar start --tasks-in runs/assistant
uv run simple-ar start --tasks-in runs/assistant --select-task
uv run simple-ar start --tasks-in runs/assistant --resume-task EXACT_DIRECTORY_NAME "Keep the measurements, revise the main figure and explanation"
```

Discovery reads the supplied directory and direct children only, at most 256 entries, without another task index.
Interactive selection requires a terminal; scripts use an exact listed directory name.
Recorded status does not certify quality; `partial` retains unresolved work.
Feedback continues the same task and ledger; starting from a configuration again creates a new task.
Creation flags cannot reset saved process limits on resume; additional resources require explicit continuation authorization.

When no operation remains pending, explicitly record another configured text connection, then submit feedback separately:

```bash
uv run simple-ar start --resume-task SESSION --switch-model profile:another --switch-id connection-1 --switch-reason "Use another configured service"
uv run simple-ar start --resume-task SESSION "Continue revising the saved report"
```

Review the displayed task/code/writing destinations and models before confirming. Recording a switch makes no model request, replays no tool, and retains delivery and accounting.
Each switch ID has one immutable intent; replaying an older ID does not revert a later change.
Reconcile pending, in-flight or unconsumed responses first. A terminated failed model decision still uses `--retry-model --model profile:another`; switching cannot bypass uncertain operations.

## Choose a task and its smallest input

Use the [six workflow recipes](WORKFLOWS.md) for research, code, data, writing,
reproduction and export. `start --guided` offers outcome preferences for native work.

For fixed table columns, validation commands or reproduction protocols, use
structured configuration. Its recovery commands differ from ordinary tasks;
see the [CLI reference](CLI_REFERENCE.md).

For a first calculation without API credentials:

```bash
uv run simple-ar research-session --config examples/data-curves/research.toml
```

This plots supplied demonstration coordinates, not an experiment.
Native `--prepare-only` still checks the connection and credentials but makes
no model/process call; structured preparation can save TOML without credentials.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Missing profile/key or wrong endpoint | Run `models`; check the catalog path, `api_key_env`, current directory and environment precedence |
| Provider timeout or interrupted stream | Inspect saved usage and completion diagnostics; named profiles use their own timeout/stream/retry settings, not legacy `SIMPLE_AR_LLM_*` settings |
| Network works on your laptop but not the host | Configure a reachable proxy on the execution host; its `127.0.0.1` is local to that host |
| Task paused at a command | Review the current proposal and approve its exact token |
| Resume refuses an in-flight/unknown operation | Confirm the worker stopped and follow the entry-specific recovery rules |
| Data plot lacks Chinese/non-Latin glyphs | Install a suitable font and configure Matplotlib's `font.family`; inspect PNG/PDF labels |
| `uv run` reports cache permissions | Fix local cache permissions or use `.\.venv\Scripts\simple-ar.exe` on Windows |

A transport timeout is not a whole-task deadline; retries may take longer.
Keep session and usage records when diagnosing failure.
CodeTask plan/proposal gates and bounded repairs are covered in
[the code recipe](WORKFLOWS.md#code-work).
