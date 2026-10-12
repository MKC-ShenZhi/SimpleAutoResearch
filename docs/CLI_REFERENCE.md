# CLI Reference

[中文版本](CLI_REFERENCE_zh.md)

Use this page to look up command syntax and effects. For a first task, follow [Usage](USAGE.md); for TOML fields, use [Configuration](CONFIG_REFERENCE.md). `simple-ar --help` and each command's `--help` show the installed version's options.

## Guided entry

`simple-ar models [--config PATH] [--profile NAME --purpose text|code|writing|vision|image]`
inspects named connections and credential presence locally, without API calls or showing keys.
For shared setup, put the catalog at `~/.config/simple-ar/models.toml` or select
its absolute path with `SIMPLE_AR_MODELS_CONFIG`. TOML holds routes and connection
settings; `api_key_env` references credentials held in the environment/private `.env`.
`start` displays text profiles and offers a terminal selection. `--model env`
uses text/default when a catalog exists, otherwise the legacy environment connection;
`--model profile:NAME` selects explicitly. Code work uses the code route; native
writing without a writing route keeps the task connection. Native decisions require
Chat. See [model configuration](CONFIG_REFERENCE.md#named-model-connections) for activation and routing.

```bash
uv run simple-ar start
uv run simple-ar start --guided
uv run simple-ar start --chat
uv run simple-ar start --kind writing --material notes.md --model env
```

Bare `start` prompts for a natural-language goal and uses the native loop.
Ordinary `--kind` choices and `--guided` select outcome preferences for that same
native owner: survey, writing, reproduction, repair, data, figure and code.
The adapter asks for a goal and common read-only material/project/data paths;
optional public-source/process-proposal access needs explicit confirmation.
It reuses the named-model selector and task summary. The kind grants no access.
Explicit structured protocols retain `research-session` compatibility; their
controls cannot be mixed with native access/budget options.

### Native task entry (experimental)

```bash
simple-ar start "Inspect these results and explain the comparison" --source results/ --model profile:NAME --yes
simple-ar start --resume-task SESSION "Shorten the explanation without rerunning"
simple-ar start --resume-task SESSION --retry-model
simple-ar start --resume-task SESSION "Use these additional notes" --source notes/ --allow-network --yes
simple-ar start --resume-task SESSION --authorize-remaining llm_requests=10 --authorize-remaining total_tokens=100000 --authorization-id continuation-1 --authorization-reason "Finish the saved goal" --yes
```

A positional request, `--goal` without structured options, or bare `start` uses the
native tool loop, not the structured task pipeline.
Repeat `--source PATH` to expose read-only files/directories as `source0`, `source1`, etc.;
new files are written in the session's `workspace/`. New mounts must be regular files
or directories, not symlinks or private/runtime paths such as `.aws` or `.env`.
The same private-name policy applies inside mounted roots regardless of letter
case, including listing, search and inspection before source reads.
Creation and added-access requests use the same checks before changing the task;
an invalid addition leaves the existing mounts, permissions and ledger unchanged.
`--prepare-only` saves a task without
model calls. `--max-turns` (30) and `--max-tokens` (600000) bound the whole saved task,
including feedback; resume does not reset them. This entry currently supports local
text investigation, source search/acquisition, code creation, result explanation and
publication export. Image tools require explicit named routes and allowances.
Complete research acceptance has not passed; tool availability is not task success.
Token accounting reserves the estimated input before dispatch. Without an output
cap, the final response can exceed the remaining estimate; actual usage is still
recorded and further requests stop. A task token allowance is not a strict
provider-side spending cap when output is uncapped.
On continuation, `--source`, `--allow-network`, `--allow-execution` and `--allow-vision`
can explicitly add access after confirmation (`--yes` for noninteractive use).
Existing mounts, model binding, interpreter, completed work and budgets remain unchanged;
process commands still require separate approval. Started or unknown operations must
be reconciled first. On resume, `--prepare-only` displays the proposal without applying
it; do not combine an access grant with `--approve-execution`.
After a terminated, failed native model decision without an accepted reply,
`--retry-model` explicitly permits a new decision attempt using the saved finite
output cap and remaining allowance. Prior failures and bounded unknown charges
remain recorded and reserved; neither tools nor the whole task are replayed.
Cached replies follow ordinary recovery without a new request. Running requests,
unconfirmed reservations, unbounded unknown charges and pending tools are refused.
Use this option separately from feedback, access/resource grants and command
approval. Ordinary resume does not silently replace a failed model request;
`--prepare-only` still makes no change or call.
To replace an unreliable connection for that closed failed decision, add
`--model profile:NAME` or `--model route:NAME` to `--retry-model`. The confirmation
shows the selected text, code and writing connections. Their resolved bindings and
the transition are saved together with the new decision identity; vision retains
its original binding. The replacement must use Chat and the same finite output cap.
History that lacks required reasoning fields is not reconstructed: choose a
compatible non-thinking profile instead. Cached replies cannot change connections,
and an explicit different `--model` without `--retry-model` is rejected. Omitting
`--model` retains the saved selector, not the current environment default.
To explicitly continue a resource-limited saved goal, use the same repeated
`--authorize-remaining DIMENSION=AMOUNT` format as `research-session`, with
`--authorization-id` and `--authorization-reason`. These options require
`--resume-task`; an ID or reason without allowances is rejected. The confirmation
shows recorded cumulative usage and the proposed remaining allowances: historical
usage is preserved, and the new remaining amount is not a total historical limit.
`--yes` accepts this summary; `--prepare-only` only previews it and never authorizes
or runs the task. Reusing an authorization ID does not replenish spent
capacity. Ordinary `--max-tokens`/`--max-turns` do not add resources on resume.
Model bindings and permissions remain unchanged; commands still need exact approval.
Confirm resource continuation separately from `--approve-execution`, and reconcile
started or unknown operations first.
File tools accept short authorized mount locators, such as `source0/README.md` and
`workspace/project`, as well as observed absolute paths within those roots.
Observations provide filesystem addresses for generated code and argv; mount names
are not actual directories, and embedded program strings are never rewritten.
Experimental `focus_work` selects the TOML `code` or optional `writing` route for a
concrete work goal, or returns to the original task connection with `task`. It changes
the next model decision only, not tools, authorization, approvals or shared allowances.
Request a switch alone; resume derives its focus from saved results without creating
a subtask or resetting the ledger. Successful switching is not task verification.
Both professional focuses can investigate evidence, edit, use authorized tools and
deliver in the same action loop. Writing without an explicit route retains the
original task client. Native focus requires Chat mode and rejects unsupported
protocols; `compose_document` separately retains its existing Responses support.
Absolute paths do not authorize extra roots or source modification.
The latest actual `run_process` result and latest nonzero result remain available when older turns are
indexed. A complete, path-bound read-back is reused rather than duplicated.
This retains that execution's exit code and streams, not proof about the current
file version or an unresolved-failure declaration; a cancelled or blocked proposal does not replace the result.
Historical indexes may use lossless columns/rows instead of repeated object keys.
Original receipts remain readable; final context metadata is included in input estimates.
Source lookup and composition check resolved ownership for both path forms: generated
workspace documents and attempt history cannot be used as original-source records.
Composition may fill missing bibliographic fields from retained front matter in the
same writing response. Exact source quotations are checked; this is not independent
publication-identity verification, and supplied metadata is not replaced.
Each model turn receives remaining shared-ledger capacity, unknown-usage dimensions
and an approximate input-cost estimate. These inform scope decisions, not guarantee
completion or increase the saved limits.
`prepare_implementation` may take `goal` and `project_path`
without a file list: the code owner inspects the bounded project, selects related
existing/new files in one model request and passes hash-bound originals to the
existing generation request. Selected existing files that need no change can
return empty replacements and retain their original bytes; selection does not
require manufacturing edits. Explicit `files` remain available for new or already
investigated code. Selection and generation share the task ledger; selection is
retained with coverage and inputs, not a second plan or session. A complete
response-contract rejection can receive one correction within the remaining
allowance; both proposals are retained. Transport failures, unknown use and
Python syntax failures are not replayed by this owner. Interrupted submissions
are not blindly replayed. Generation does not transfer, execute, verify or deliver
the candidate.

`inspect_project` reuses the read-only project preparation inspector for an
authorized directory and optional explicit `data_paths`. It returns declared
dependencies, entry/test/document candidates, coverage and a readable full
attempt record. Private paths are excluded before indexing. This derived
observation is not original-paper evidence, execution authorization or proof
that the task interpreter is ready; no project code is imported or run.
`copy_project` prepares an editable project from an authorized source directory
using the existing CodeTask copy owner through the same workspace gateway.
Set `source` to the directory and `path` to a nonexistent subdirectory of the
writable workspace; absolute authorized paths and legacy mount aliases are accepted.
For example, tool arguments `{"source":"source0/project","path":"workspace/project"}`
copy ordinary source, tests, configuration, documentation and binary resources
without a shell, network access or project execution. The source stays unchanged.
Cache/build directories, symlinks and native private/runtime paths are skipped,
including `.git`, `.aws`, `.ssh`, `.codex`, `.agents`, virtual environments and
all `.env*` names (also `.env.example`). Oversized individual files are skipped.
The `copy` report records counts, bytes and a bounded list of skips with reasons;
inspect it before deciding whether the editable subset is sufficient.
`max_files` defaults to 1000 (1–4096), `max_total_bytes` to 134217728
(128 MiB; 1–536870912), and `max_file_bytes` to 2000000 (1–67108864).
Invalid paths, an empty eligible source, an existing destination or exceeded
file-count/total-byte limits reject the operation without publishing a target.
Successful data includes `absolute_path`, `source`, `copy` and `files` with
project-relative paths and SHA256 digests. Before publishing the candidate,
`attempts/IDENTITY/project-copy.json` records the request and output hashes.
Recovery requires the same request and matching current target hashes; a repeated
ID returns the saved result rather than copying again. A mismatch blocks recovery.
This is an editable subset, not a complete Git repository copy. It does not
guarantee no omissions, installed dependencies, a ready environment or verified
execution. Execution continues to require the normal authorization and approval
under the existing SessionController owner.
`write_file` accepts an optional explicit `delivery` containing `summary`,
`artifacts` and `unresolved`. It must include the file being written; all other
artifacts must already exist in the writable workspace. Invalid paths are rejected
before writing. A successful final write uses the existing delivery/terminal-state
owner; ordinary writes and standalone `deliver_result` retain their behavior.
Artifacts may also name workspace project directories. The existing inventory
expands each directory to an explicit snapshot of at most 200 accessible files,
excluding private paths, virtual environments, Python bytecode caches and symlinks.
Empty or oversized selections are rejected, not silently truncated. This records
files, not an archive or proof that the project is portable or correct.
New native tasks save a non-secret connection binding. Changing the named model,
endpoint or protocol rejects further calls and execution; restore the original
catalog, or start a new task. Credential rotation and operational timeout/output
limits use the existing compatibility rules. Older native tasks without this
snapshot retain legacy resolution; their historical connection cannot be reconstructed.
Composition and code preparation resolve their clients through that task owner;
they do not independently choose another default connection. Newly authorized
vision also saves its own non-secret binding, excluding the unused code route.
Changing its model/profile/endpoint rejects image inspection before sending;
credentials may rotate and operational timeout/output settings may change.
Older already-authorized vision tasks without a binding retain legacy resolution,
not a reconstructed historical identity. Image generation keeps its versioned
submission/recovery owner and is separate from this vision binding.
The soft input-history retention target uses the current connection's
`context_input_tokens` (default 24000 estimated tokens), including tool schemas,
and finite capacity subtracts the current connection's effective output reservation
from the remaining token allowance, without dividing it by the request ceiling.
The current complete operation remains intact even if it exceeds the target.
Earlier complete assistant/tool blocks are retained contiguously while shared capacity
permits, preserving decisions and their matching results. `retained_turn_start` identifies
that suffix; omitted turns may contribute version-checked text windows and a recent
observation view limited to 2000 estimated tokens, without duplicating retained blocks.
`history_archive` identifies omitted accepted-turn ranges and historical failure counts,
not missing evidence or resolved failures. The read-only `read_history` tool pages those
indexes without a model call or another memory store. Continue byte windows with the
returned `end_turn` pinned and `next_offset_bytes`; attempt numbers are not turn indexes.
Original records and inline cancellation results remain accessible. The latest execution
and latest nonzero execution retain their original diagnosis when not already visible.
Historical identities are not current file contents or verified conclusions. Older model
deliberation remains in raw records. This is not an output cap or exact tokenizer guarantee and does not bypass
budget preflight; a protected context may still exceed the target or remaining capacity.
If the indivisible current turn alone exceeds this soft target, optional earlier
blocks, indexes and text windows are omitted; the complete operation is never split.
`--project-python PATH` selects an existing interpreter for native `python`/`python3` process
calls and preserves a virtualenv entry path. It neither installs dependencies nor
creates an environment. The saved interpreter is reused on task continuation;
resume cannot silently replace it. Its executable directory leads the filtered
process `PATH`, so child commands can use the same task environment. Python scripts
should use `sys.executable` for portable child calls; no alias shims or environments
are created. Command approval binds the resolved argv and filtered environment;
changes require approval again. Existing execution receipts recover without rerunning.

`list_directory` can discover filenames recursively with `file_glob`, returning
mounted paths and pagination without file contents. Traversal stops after 4096
entries and reports `inventory_truncated`; a truncated inventory is not proof
that a file is absent. For retained document records, omit the query in
`find_source_passages` to inspect identity and coverage first, then query text
for actual evidence rather than reading the whole serialized bundle.

The experimental `compose_document` tool drafts an article from selected retained
DocumentBundle chunk IDs, or creates a feedback version from an existing Markdown
draft. It uses the explicit writing route when configured, otherwise the task's
selected text connection, with the shared model budget, and saves
a new `manuscript.md` with publication citation keys and references, and preserves
the old draft. Inspect, edit and export that same body; the assembler's display
snapshot remains a tool record, not another editable manuscript.
New whole-article work units explicitly bind their assembly form and retain authored
heading hierarchy. Historical work units keep their original assembly form during
resume; changing a saved assembly is not a way to relayout a completed export.
New compositions make one whole-article writing request from selected original
passages, material windows, known bibliography, figures and an optional prior
draft, without the legacy section-planning/review lifecycle. The original input
and response are retained before validation. The shared LLM client owns bounded
transport attempts and accounting; this work unit adds no format correction or
separate retry loop. Directory selection remains a separate
shared-budget request when needed. Structured writing commands are unchanged.
An omitted material line limit keeps byte-window semantics. Recovery reads the
originally bound byte range and verifies its contents, rather than reinterpreting
historical serialized defaults under the current reader.
It does not search, run experiments or independently review its output; the calling task must inspect
the article and verify consequential claims. A complete, input-bound article
response can resume assembly and missing publication files without another model
request. Historical Writer results retain their original checkpoint validation.
Partial, unknown or failed writing results are not resubmitted; changed
partial files are preserved and rejected. Reuse checks the saved manuscript, references
and figure bytes. Edited or missing outputs (or older records without byte bindings)
require inspection or a new feedback version, not automatic regeneration.
Ordinary-entry composition acceptance
has not passed merely because this tool is available.
Retained original connector identities are reused for citations without a second
intake role assignment. Unknown publication fields stay unknown; prior drafts
and derived analysis remain different from original references.

For local material directories, `material_roots` delegates file selection to the
writing owner instead of requiring coordinator-selected `materials` windows.
Selection uses the same bounded, private-path-filtered inventory as code preparation;
it consumes the shared model allowance and binds the visible file versions.
The result reports selected files, unread coverage and omitted-file counts.
Roots may include retained `document_bundle.v1` originals, such as the task's
`evidence` directory. Selection preserves their identities and chooses focused
source questions for the existing passage retriever and Writer; generated
workspace files and attempt history cannot become original-source evidence.
Source declarations and previews are not verified claims. The operation does
not search or acquire new sources.
`material_roots` may accompany explicit `sources`, figures and a prior draft;
only explicit `materials` windows and `material_roots` are mutually exclusive. Explicit
locators use authorized absolute paths or mount aliases such as `workspace/…`;
they are checked before paid selection. Saved mixed-selection requests retain
their bound recovery semantics, not permission to change the selection on resume.
Selection is not exhaustive reading, statistical validation or visual inspection;
changed inputs and interrupted selection are not blindly replayed.

New native `edit_file` declarations use a `replacements` list for exact,
non-overlapping changes against the same original. Historical `old_text`/`new_text`
single-change inputs remain accepted with the same validation and write boundaries.
Every span must match uniquely before anything is written. The current digest,
backup and atomic-write path are shared with `write_file`; stale, ambiguous or
overlapping changes and edits to read-only sources are rejected.
Version conflicts return the authorized target's current digest and the supplied
expectation without writing or returning its body. This is a snapshot, not an
automatic retry: an explicit matching expectation is still required, and later
changes reject it. Read-only or private targets do not expose a conflict digest.

`read_file` accepts either a byte offset or a one-based `start_line` from search
results. Line windows retain the digest and byte continuation, cap the returned
bytes/lines, and support files up to 256000 bytes; larger inputs use byte windows.
An explicit `max_lines` also limits a byte-origin window, including a read from
offset zero. Omitting it on a byte read retains the byte-only behavior. Both
limits apply when supplied; `max_bytes` can end within a line. Continue at the
returned `next_offset_bytes`, rather than estimating offsets from decoded text.

For larger data or image files, `read_file` with `full_digest=true` streams the
digest of an authorized regular file up to 64 MiB while returning only the
requested text window. The default digest policy is unchanged. `copy_file`
copies text or binary bytes up to the same bound using that source digest;
replacing an existing destination also requires its observed digest. It shares
the version checks, backup and atomic commit with text writes, without decoding
the source or exposing its contents to the model. Directory copying retains its
separate size limits and omission report.

Native `search_files` requires `rg` (ripgrep) on the execution host. It searches
literal text in authorized files and reports the scanned file page, continuation
and truncation limits; it is not an exhaustive repository index. Read-only search
still consumes a shared process receipt and allowance. New tasks allow up to ten
process invocations per model-turn allowance, with the total process wall-clock
budget unchanged. Recovery retains the saved limits, including older tasks.

`--allow-network` authorizes public source search and original-text acquisition,
independently of process execution. Existing arXiv, OpenAlex, Semantic Scholar and
optional Tavily providers are reused; the Tavily provider requires its own credential.
Acquired originals/search records live in read-only `evidence/`, not writable output.
Question-based passage lookup is local and preserves source offsets and truncation.
It also accepts retained document records in supplied read-only mounts, so existing
originals can be reused without network access; generated workspace records are rejected.
Each acquisition currently bounds one document, three fetch attempts, 8 MiB and
60 PDF pages; parser coverage/failures remain recorded. Search excerpts are not full
text, and retrieval is not verification. The shared `source_operations` allowance
counts provider/adapter operations, not HTTP requests or vendor credits.

`--allow-vision` separately authorizes `inspect_images`: mounted PNG/JPEG/WebP
images are sent to the TOML `vision` route, using the same task request/token ledger.
A missing vision route fails preflight, without substituting the text model.
Feedback and original image copies are saved per inspection; a saved response is
reused, while an interrupted request without a response is not blindly resent.
Rejected response content is reported separately from connection failure and
retained locally; it is not accepted as findings or automatically resent.
Visual feedback is not proof of numerical or scientific correctness. Source search
authorization does not grant vision access, and vision access does not grant search.

`--max-images N` authorizes up to N image-generation/edit submissions through the
TOML `image` route; default zero keeps these tools disabled. `create_image` saves a
new version and never replaces its input. `resume_image` accepts that exact version:
saved completions are reused, known async tasks are queried/downloaded, and unknown
dispatches are not resubmitted. All versions share the task's persistent image ledger;
resume does not reset the allowance. Endpoint/proxy/credential checks occur before
task creation. These raster illustrations are not measurements or editable vectors.

`export_report` converts workspace Markdown and its local assets into an editable
ACM project and optionally a PDF, reusing the existing exporter and shared process
receipts. It requires execution permission and explicit approval; compilation does
not establish article quality or compliance with a requested page count. Completed
exports include the complete project, figures and rebuild instructions. New exports
bind output file digests before reuse; changed or missing files cannot inherit an
old compilation result. Historical input-only receipts remain readable as history,
not present-byte certification or automatic delivery. Interrupted exports without
a successful output binding are not blindly repeated. Earlier versions remain.

`--allow-execution` permits process proposals, not automatic execution. Each command
needs terminal approval or the printed `--approve-execution TOKEN` on resume; `--yes`
only confirms task creation. A workspace is **not an OS sandbox**: timeout and log
bounds do not enforce CPU, memory, filesystem or network isolation. Resume retains
the saved model connection, sources, permissions, receipts and budget; it cannot be
combined with structured setup/reuse switches. A delivery manifest verifies file
existence, not scientific correctness. Unknown interrupted operations are not replayed.
Feedback cancels unstarted pending proposals; operations already attempted must first
be reconciled. `simple-ar results SESSION` lists the current delivered files and
limitations, with the native resume command. During a paused revision an older
delivery is retained on disk but is not presented as the revised task's completion.

For multi-step work the assistant can use `update_work_plan` to record a concise,
changeable approach alongside actual actions. The latest plan for the current user
request remains visible after history compaction and resume, without a separate
planning model or scheduler. New feedback requires a current plan rather than
treating an old plan as accepted. Step statuses and cited records are the assistant's
claims: they do not grant execution approval, verify results or complete delivery.
Simple tasks need not create a plan.

Online surveys can request new sources after reading exposes a specific gap.
With model reading and automatic expansion enabled, one follow-up search/ingest/read
cycle reuses the original limits and saved evidence. `--fulltext` still controls
remote full-text permission; finding an abstract does not establish full-text support.
`--web-extract-backend direct|tavily_basic` selects original HTML/text acquisition
for `start` and `research-session`; it does not enable acquisition by itself or
change PDF permissions. The optional Tavily backend requires `TAVILY_API_KEY`.
Reading may request a data or project URL quoted in the source rather than another
paper query. Linked-page redirects share the remaining fetch allowance, up to four
GETs per acquisition, with public-address checks at each hop. Acquired documentation
is not proof of runnable code, successful data download or scientific correctness.

| Options | Applies to | Purpose |
| --- | --- | --- |
| `--guided`, `--kind`, `--goal`, `--output-root` | New tasks | Optional outcome preference, goal and output location; strict controls select compatibility |
| `--model` | Model-backed tasks | Named connection or compatible environment route |
| `--interaction` | Structured tasks | Participation policy; explicitly supplying it selects compatibility |
| `--prepare-only`, `--yes` | Native / structured setup | Save without execution/API, or accept the displayed summary; no extra execution authority |
| `--chat`, `--resume-setup PATH` | Model-assisted setup | Clarify a task or resume its saved setup draft |
| `--sources materials\|search`, `--document PATH`, `--fulltext`, `--max-cited-sources N` | Survey / code repair | Optional source access, repeatable documents, retrieval and citation limits |
| `--material PATH`, `--template` | Writing | Repeatable notes/results and requested report format |
| `--project`, `--validate`, `--allow PATTERN`, `--project-python PATH` | Code repair / opt-in reproduction adapter | Project, independent validator, repeatable edit scope and existing interpreter |
| `--data-path PATH` | Code/reproduction | Repeatable input file/directory, not the table-analysis source |
| `--hypothesis`, `--dataset`, `--expected-outcome`, `--metric` | Reproduction | Accepted conclusion, conditions and emitted metrics |
| `--cwd`, `--timeout-sec`, `--output-files JSON` | Reproduction | Execution location, timeout and registered producer files |
| `--metric-sources JSON` | Reproduction | Explicit JSON path or CSV/TSV unique-row metric selection from registered files; no aggregation |
| `--environment current\|venv`, `--requirements PATH`, `--install-project` | Reproduction | Explicit dependency preparation |
| `--check-argv JSON` | Reproduction | Explicit short check before measurement; no inferred command, and no check metrics in measured results |
| `--command ARGV...` | Reproduction | Actual command; place it last |
| `--with-report` | Data analysis / code repair | Also write from saved analysis or code-validation results; requires a model |

Author output arguments can use literal `{output_dir}`, bound to the current
invocation without shell expansion. See [file metric selectors](CONFIG_REFERENCE.md).

Preference kinds: `survey`, `bug_fix`, `reproduction`, `writing`, `data_analysis`, `figure`, `code`. Technical `--chat` retains supported structured protocols; figure/scripted proposals cannot recreate a retired scaffold and are refused. Structured preparation makes no model call; chat setup itself uses the model and needs a terminal. Do not combine `--chat` with `--prepare-only`.

Code-repair chat can inspect project text and propose a validation argv with quoted
source basis. Confirmation serializes it into the existing CodeTask benchmark
command; explicit `--validate` is retained. Setup does not run commands or install
dependencies; explicit structured repair still confirms edit scope and execution.

Code repair can use `--document PATH` for supporting documentation or
`--sources search` for explicitly authorized discovery; `--fulltext` requires
online source access. Add `--with-report` to explain the actual patch and validation
results without treating code checks as scientific measurements. These options do
not widen `--allow` or change the independent validator.

Code repair's interpreter defaults to current. `--project-python` preserves an existing virtualenv entry path; it does not create or install an environment. Reproduction also accepts it for the formal command and short check; with explicitly chosen `--environment venv`, it selects the base interpreter for the new task environment. Chat can adopt an existing interpreter path quoted from your message after confirmation; it never selects one from source instructions.

Explicit reproduction protocols keep the fixed-command path. For an existing Python
project already validated in a reproduction session, use `start --kind reproduction
--from-session SESSION --reuse code_project --document PAPER --goal "Check readiness and repeat"`.
This prepares an isolated task with the saved single-command protocol, output selectors,
edit scope, checker and interpreter. It does not recreate existing adapters or reinstall
dependencies. Confirm the displayed execution before running. Paired/extended protocols
remain on the original session's resume path; execution overrides require a separate task.

For an existing Python
project, explicitly combine `--project`, repeatable `--allow PATTERN` and
`--validate "…"` with an inspected project check to authorize a scoped result adapter or
entry glue. The validator must differ from the formal measurement `--command`
(placed last); validator outputs are not scientific measurements. CodeTask edits
only the allowed isolated worktree/copy, with tests, secret files and declared
project data protected; validation must pass before the formal command runs there.
`--cwd`, if supplied, must match `--project`. Use current Python or an existing
`--project-python` interpreter, or explicitly choose `--environment venv` with approved
requirements/project installation before adaptation in the same isolated workspace.
Both validation and measurement then use that task interpreter, not the base interpreter.
Do not add `--check-argv` beside the independent
CodeTask validator. Installation failure stops subsequent work. It does not authorize changes
to methods, splits or evaluation conditions, and workspace isolation is not an OS
sandbox. This opt-in capability is not autonomous arbitrary-paper preparation or
evidence that real-paper reproduction acceptance has passed.

Reproduction chat may propose exact new Python adapter paths and an independent
validation argv after inspecting project source and command instructions. The
displayed scope is adopted only on confirmation; explicit `--allow` / `--validate`
remain authoritative. This proposal does not execute or implement the adapter.
On authorized preparation, confirmed new paths are initialized only in the isolated
workspace before indexing. Keep task output outside the author project; ordinary
`--allow` paths without a creation declaration must already exist.

Chat setup can propose public project/data/material downloads from inspected paper or project
material, with separate confirmation for each asset. The proposal selects a viewed
source path; code locates its literal URL rather than requiring a copied quotation.
PDF parsing preserves HTTP hyperlink targets. Declared lockfiles can be read on
request through bounded source windows, but are not sent by default; static
dependency evidence does not establish a runnable environment. If project Python
requirements exclude the framework interpreter, supply a compatible interpreter
with `--project-python` or confirm a named Python asset; a textual assumption does
not change the execution environment. Projects accept a direct ZIP
or a GitHub repository's default-branch snapshot, not a fixed commit; data stays a
single downloaded file without automatic unpacking. Limits are 3 assets, 20 MiB
per download, and 80 MiB / 5000 entries per project ZIP by default.
Set `--asset-max-mb N` for a different project/data download capacity before chat
setup; ZIP expansion is limited to `4*N` MiB. Confirmation displays these limits,
and the saved receipt preserves them on recovery. ZIP format is checked from
the archive itself, not inferred from the URL suffix. This does not authorize a
download or installation by itself; supporting documents retain their own limits.
Only public HTTPS URLs are accepted. Public identity/version/download query
parameters are retained; credential-bearing or signed links, URL user information
and fragments are rejected. Downloads do not authorize execution or
dependency installation; this is not a security sandbox or complete SSRF protection.
Supporting material can be an official instruction page or paper; it uses the existing
document preview and question reads, with one public GET, no redirects and separate
PDF permission. It counts against both asset and document limits, preserves its
source URL and download ledger, and does not provide authority to execute commands.
The acquisition path is implemented; end-to-end real-world reproduction acceptance
has not yet passed.

For a supplied paper link, use `start --kind reproduction --chat --document
https://… --goal "…"`. Confirm acquisition before the model receives an attributed
excerpt; PDF downloading/raw retention requires separate permission or explicit
`--fulltext`. The preview is limited to 8000 characters / three PDF pages, not
full-paper understanding. When a question needs later methods or instructions,
chat can request a read-only lookup in an already supplied document or material.
For a paper identity or an unavailable source, chat can propose public-source searches
instead of requiring you to find every URL. Confirm the displayed queries and providers:
Tavily plus OpenAlex when `TAVILY_API_KEY` is set, otherwise arXiv plus OpenAlex.
Setup permits two discovery rounds of at most two queries each, with five results
per query/provider. Metadata and provider failures are saved; metadata is not an
acquired paper or verified author identity. Recovery reuses saved responses, and
an interrupted request is not automatically resent. Downloads and execution remain
separate confirmations.
Use `--material` for ordinary JSON/BibTeX notes and recorded results, not
`--document`; dialogue previews retain that material role and do not verify its claims.
Reproduction preparation can retain these notes in its task assets and use them
to propose continuation work. This does not promote an old observation into a new
measurement; numerical reuse must be checked against its actual raw data and conditions.
Expanded local extraction requires confirmation (up to 40 PDF pages / 20 MiB);
only bounded query excerpts reach the model. Saved extraction bundles and
acquisition receipts are reused on setup recovery, without parsing or fetching
again. On recovery, you may explicitly skip a failed paper acquisition and
continue with other supplied material; its receipt and unresolved consumption
remain saved, and the failed resource does not become evidence.
Ordinary `--prepare-only` does not fetch links. Repository and
execution proposals still need inspected source text and separate confirmation.
New result adapters may use source-defined invocation contracts without a prose
README; this is preparation basis, not proof of successful execution.
When choices remain, answer them or type `accept-proposal` to adopt the displayed
settings and assumptions explicitly. Unanswered factual questions stay unknown;
execution still requires its own confirmation. An exhausted saved setup can
review an already valid proposal without another model call or budget reset.
For reproduction setup, `--fulltext` permits acquisition; the generated task
consumes the acquired/local files rather than starting another retrieval phase.

### Existing data (no API)

Use `start --kind data_analysis --data-file PATH --value-column NAME` for the deterministic shortcut; add `--with-report --model env` for writing. Without selected columns or other protocol controls, the data preference uses native investigation. The table options are shared with `research-session`:

| Option group | Purpose |
| --- | --- |
| `--data-file` | Input table |
| `--group-column`, `--value-column` | Row grouping and repeatable values |
| `--observation-unit`, `--value-unit`, `--data-attribution` | Declared semantics |
| `--data-mode observations\|values` | Aggregate observations (default) or preserve supplied values |
| `--data-plot bar\|box\|heatmap\|line\|scatter` | Figure type; default `bar` |
| `--data-missing reject\|omit` | Reject missing values (default) or explicitly omit them |
| `--paired-baseline COLUMN` | Same-row paired differences against the named baseline column |
| `--x-column`, `--x-unit`, `--data-association none\|pearson` | Numeric coordinates and optional descriptive correlation; default `none` |
| `--series-layout separate\|shared`, `--figure-width column\|wide` | Coordinate axes default `separate`; figure width defaults `wide` |
| `--data-max-mb`, `--data-max-points`, `--data-max-figures` | Input/figure bounds; defaults `20` MiB, `10000` points and `100` figures |

For an accompanying report, `start` accepts `--material` and `--document`;
`research-session` uses `--material` and `--local-document`. These provide context,
not a different numeric transformation. Analysis-only ignores model settings.
See [data fields](CONFIG_REFERENCE.md#existing-data-descriptive-analysis) for compatible option combinations.

### Goal-driven code, analysis and figures

```bash
simple-ar start --kind data_analysis --scripted --data-file measurements.csv --goal "Compare cost and quality in a three-panel figure" --allow-execution
simple-ar start --kind figure --document method.md --goal "Draw observations → comparison → evidence" --allow-execution
simple-ar start --kind code --material requirements.md --goal "Build a reusable CSV validator" --allow-execution
```

These new tasks use the existing native action loop, configuration, ledger and
recovery. They create no repair scaffold, synthetic evaluator or `bug_fix` task.
The goal is retained verbatim before appended delivery requirements and declarations.
The creation choice is a starting preference, not a tool or plot-type restriction.
Figure requests ask for code/layout source, PNG, editable vector SVG and explanation;
scripted analysis requests runnable source, computed results and explanation.
Actual outputs and verification remain task work, not an entry-point certification.

Local `--document`, `--material`, `--project`, `--data-path`, `--data-file` and `--source`
inputs become read-only mounts, with their declared roles preserved. Data-file
inputs retain the regular-file/no-symlink/private-file protection and 20 MiB limit;
use `--source` for larger bounded investigation. No input snapshot or placeholder
dataset is manufactured. New source and outputs belong to the task workspace.
Supplied evaluation files remain read-only; this is not an OS sandbox.
Public document URLs in this adapter are rejected: express them in the goal and
grant `--allow-network` separately when acquisition is intended.

Explicit columns, units, attribution, observation units, pairing, missing-value
policy and presentation choices are preserved as user declarations in the goal;
unprovided preset defaults do not restrict native analysis. These declarations do
not establish statistical assumptions or independently verify results.
`--with-report` requests a report from actual materials/results.
`--model`, `--project-python`, `--output-root` and native resource/access flags
go directly to the same owner; interpreter selection does not install dependencies.
No creation choice grants process/network permission. Use `--allow-execution`
for command proposals; each command still needs individual approval.
`--yes` accepts the summary only. `--prepare-only` saves a native session without
model/process calls. Continue using `start --resume-task SESSION`.

Explicit controls outside the native/shared/material/declaration destinations
select structured compatibility: source/retrieval policy, report templates/limits,
edit scope/validator, reproduction command/metrics/environment, physical table
limits and interaction settings. Even explicitly supplying a parser default
selects that contract; no strict field is silently translated into a preference.
`--chat`/`--resume-setup` keep technical setup and its confirmed historical settings;
`--from-session`/`--reuse` keep bounded registered scope and validators. The
recognized preference set is `survey`, `bug_fix`, `reproduction`, `writing`,
`data_analysis`, `figure`, `code`; `--scripted` requires `--kind data_analysis`.
Creation preferences cannot be applied to `--resume-task`.
For `data_analysis`, explicit `--value-column`, `--x-column` or `--paired-baseline` without `--scripted` selects the
no-model TableSpec shortcut; a data path alone is an ordinary native preference.
Figure/scripted requests with incompatible strict controls are rejected before
task creation. A project alone is a read-only native mount; `--allow`/`--validate`
retain exact structured editing and execution semantics. Strict protocols cannot
mix native access/budget controls. Direct `research-session` and `code-task init/execute`
remain explicit structured interfaces; their saved sessions continue under their
original execution owner. This migration does not equate their planning, baseline,
review or measurement settings to a natural-language native task.

## Reuse deliveries

Native creations expose delivered files via `results SESSION`; feedback uses
`start --resume-task SESSION`. Historical registered packages and projects retain
the following compatibility routes.

Structured compatibility code tasks default to one technical repair round. When process limits are
omitted, `research-session` reserves one validation plus the configured repair/retest
rounds; explicit limits, including zero, remain authoritative. A repair allowance
does not authorize installation, scientific protocol changes or unlimited retries.

`simple-ar start --from-session SESSION --reuse code_project --goal "Change the layout"`
continues a validated code/plot project in a new isolated task, retaining its
registered edit scope, protected files, validator and patch policy. The original
delivery remains unchanged. This selector cannot be mixed with writing material
selectors or new project/scope/validator overrides.

```bash
simple-ar results SESSION
simple-ar start --from-session SESSION --reuse data_analysis --goal "Explain the cost/performance trade-off"
simple-ar start --from-session SESSION --reuse report --goal "Shorten the discussion without changing measured results"
```

Select current registered `data_analysis`, `report`, `summary` or `code_analysis` results. Reuse starts
a new writing task and leaves the original session unchanged. Reports include their
registered source text bundle, saved experiment evidence and recorded citation identities
where present; retained passages do not trigger another download, and metadata does not
stand in for primary source text or re-verification. The reused body is retained separately
as an earlier draft for editing, not a source-evidence choice or new measured result.
Retained papers keep their connector identities and recorded citation aliases;
document/chunk storage IDs do not create new references. Linked documentation
retains its material role and available text rather than a duplicate paper handle.
Analysis packages retain their existing source-data recheck. This is distinct
from resuming an interrupted execution.

To change the argument or chapter structure, use `--reuse report` with that goal
to start a new adaptive writing task. It reuses the registered evidence without
rewriting the original task's accepted outline. Normal revision keeps that outline.

Historical validated script-analysis deliveries selected with `--reuse code_analysis` carry
their source, input data, recorded results and PNG/vector exports into writing.
Generated projects use `analysis.py` as the entry point; helper modules and editable
layout sources may live in `src/` and are included in the portable package.
Explicit compatible CodeTask projects retain the large edit profile's file and total-size limits,
with its per-source-block limit set to the existing 32,000-character absolute ceiling.
Advanced numeric overrides in the CodeTask `[budget]` table also reach research
execution and recovery; they do not bypass the absolute limits or protected paths.
The report can select figures for its body while retaining the complete linked package.
Object-shaped `outputs/results.json` may include `figures` records with a package-relative
`path` and an authored `caption`. Registration preserves these captions for writing and
export; older path-only records retain the filename fallback. Captions are not verification.
When source identities are supplied, `results.json` may retain a `citation_map` in the
existing `citation_map.v1` format. Writing reuses these recorded references without
inventing missing authors, dates or identifiers; metadata is not primary-source evidence.
Copying script outputs is not an independent numerical recomputation. Older sessions
without the registered package retain their text/JSON-only reuse behavior.

## Concept images

Research follow-up with new searches:
`simple-ar start --kind survey --from-session SESSION --reuse report --goal "Your new question"`.
This creates a new task with the report's source bundle rather than its manuscript,
excluding embedded prior drafts and derived analysis attachments. Repeated sources
are merged without combining passages from different text versions. It
defaults to online search. Reused sources still need relevance and fact checks.
Both preparation and execution accept saved source bundles. Supplying local
sources does not disable an explicitly requested online search. For legacy
sessions, declared report-material ownership excludes untyped local drafts;
filenames alone do not establish a source's role.

```bash
simple-ar image --config models.toml --prompt "A clean method overview" --output figures/v1
simple-ar image --config models.toml --input figures/v1/image.png --prompt "Keep the layout; use green" --output figures/v2
```

Uses the named `image` route, or `--model profile:NAME`, with `image_generate` and,
for edits, `image_edit` capabilities. The original stays untouched. Each version
has its own request record and one-request ledger; repeating the identical completed
request restores files without another API call. Failed/interrupted requests are
not automatically resent. The generic `openai_images` API requires provider base64
output and rejects URL-only output. With explicit `cctq_images_async`, recovery of a
saved task ID polls the same task and authenticates its configured same-origin
`files/0` download, without another POST. A saved completed task downloads directly;
timeout and HTTP protocol settings may change for recovery without changing or
resubmitting the generation. A request without a saved ID is not
automatically resubmitted. The provider must implement this asynchronous contract;
the adapter does not automatically switch APIs or models. These are raster concepts, not scientific
measurements or vector layers.

### Inspect a rendered figure

```bash
simple-ar image-review --config models.toml --input figures/v1/image.png \
  --goal "Check readable labels, arrow routing and unwanted text" --output figures/review-v1
```

Requires a configured `vision` route; `--model profile:NAME` selects another vision
connection. Repeat `--input` for up to four images. The command preserves source
copies, the model response, `review.json` and `feedback.md`. Reopening identical
completed inputs makes no API call; a saved raw response can be normalized offline.
Changed inputs need a new output version. Interrupted requests without a saved
response are not blindly resent. Apply feedback using the original code project or
a new `image --input` edit; this command does not edit or certify the figure.

## Project preparation

```bash
uv run simple-ar project-info --project ./project --data-path ./data \
  --output runs/project-preparation-NEW
```

Writes `preparation.json` and `preparation.md` containing indexed entry candidates, dependency declarations, excerpts and named data locations. It does not execute, install or download project code/data. Static dependency observations refer to the inspecting interpreter, not a solved target environment.

Use an unused output directory. Relative data paths resolve from the project root. Notes may be reused as writing material or reproduction setup input.

## Command Overview

| Command | Purpose |
| --- | --- |
| `simple-ar start` | Natural-language goal or ordinary outcome preference enters the native tool loop; explicit structured protocols retain their configuration and session owner. |
| `simple-ar research-session` | Canonical entry for a bounded task-driven session; the accepted plan selects applicable research and execution steps. |
| `simple-ar research-session-continue` | Retry one failed canonical explicit experiment. |
| `simple-ar research-session-migrate` | Create a canonical successor from a read-only `session_manifest.v1`. |
| `simple-ar research-report` | Generate and audit a report from a completed research session. |
| `simple-ar report-export` | Convert assembled report artifacts into a new editable ACM manuscript; optional local TeX compilation. |
| `simple-ar research-brief` | Segmented/development interface for an evidence-backed research brief. |
| `simple-ar status` | Print status for a research run or code-task run. |
| `simple-ar tools ...` | Export tool schemas, call run-local tools, or serve read-only tools over MCP stdio. |
| `simple-ar inspect` | Build a local artifact index for a run. |
| `simple-ar search-artifacts` | Search indexed run artifacts. |
| `simple-ar clean` | Preview and remove rebuildable run caches. |
| `simple-ar code-task ...` | Work with an existing codebase or a greenfield code task in an isolated editable workspace. |

## Research Commands

### Report controls

The two report entrypoints use the same settings, with different option prefixes:

| `research-session` | `research-report` | Purpose |
| --- | --- | --- |
| `--report-document-review` / `--no-report-document-review` | `--document-review` / `--no-document-review` | Enable or disable whole-document review |
| `--report-review-scope section\|document` | `--review-scope section\|document` | Review sections individually or after drafting the body |
| `--report-draft-scope section\|document\|auto` | `--draft-scope section\|document\|auto` | Draft separately, jointly, or jointly only for fully sized plans up to 2,000 words |
| `--report-outline-strategy auto\|template\|adaptive` | `--outline-strategy auto\|template\|adaptive` | Select template-based or evidence-led planning |
| `--report-data-tables linked\|full` | `--data-tables linked\|full` | Link data records or also include summary tables |
| `--max-document-review-prompt-chars N` | `--max-document-review-prompt-chars N` | Optional complete review-request character ceiling |

With `auto` and figures enabled, reports containing registered supplied figures
use evidence-led planning to select inline figures and their section owners.
Text-only reports retain their direct template path; `template` remains an explicit override.

Omitted options retain configuration or saved settings. Base defaults are section
drafting/review, document review off, `auto` planning, `linked` data tables and no
additional review-request ceiling (`0`); guided setup may save different choices.
Joint drafting requires document review, document review scope and the full source
strategy. See [configuration reference](CONFIG_REFERENCE.md) for these dependencies
and persisted correction limits.

### Prepared reproduction

Prepared paper-conclusion checks use `research-session --config examples/conformal_reproduction/research.toml`
after the preparation in that case's README. `--task-kind reproduction` requires local source
materials and an explicit fixed protocol; it is not an autonomous environment/code discovery tool.

### `simple-ar report-export`

```bash
uv run simple-ar report-export --report-dir PATH_TO_REPORT_ATTEMPT --output runs/acm-draft --compile
```

The input directory must contain `report_body.md` with its source citation keys,
and `references.bib` when cited. Pandoc converts this existing text; no model or
experiment is rerun. Local figures inside the report directory are copied;
SVG figures used in TeX require `rsvg-convert`. Remote image URLs are not fetched.
Registered vector exports are retained even when the body uses a PNG preview.
`--title` supplies a publication-facing title. Existing output directories are
retained; choose a new one for each export.

`source.md` and TeX use the exported figure paths, so the directory can be moved.
Editable SVG sources are kept alongside converted figures. Links to external
local evidence become labels, with their original targets recorded in `export.json`;
datasets and source artifacts are not automatically copied or redistributed.
Literal fenced code travels in `code/block-*.txt`; keep this directory with the
editable project. TeX uses `fvextra` for code lines and `seqsplit` for long inline
identifiers, without changing the canonical Markdown or executing code content.
`etoolbox` protects these commands in titles/captions; PDF metadata keeps literal text.

The output is a single-column `acmart` manuscript demonstration with editable
`main.tex`, `body.tex`, bibliography, figures, and `export.json`. This does not
select a conference's submission options or certify the report's claims.
When the report has a figure manifest, its recorded captions are bound to the
exported figures; only an exact adjacent duplicate caption paragraph is removed.
Without a manifest, the supplied image captions are left unchanged.
Generated figures prefer nearby placement (`htbp`) while allowing normal LaTeX
floating; placement is not forced and compilation alone is not visual acceptance.
Pandoc is required. `--compile` additionally needs `pdflatex`, `bibtex`, and
the acmart TeX packages, `fvextra`, `seqsplit`, `etoolbox`, and scalable fallback fonts (full `cm-super` on Debian/Ubuntu,
not just its 10pt-only minimal subset); it records `build.log` and returns a nonzero status
when compilation is unavailable or fails, keeping the generated source.
Compilation currently uses pdfLaTeX; non-Latin scripts may require adapting
the editable project to another engine and fonts.


### `simple-ar research-brief` (segmented/development interface)

**Purpose**: build a small evidence-backed brief from a topic or local
Markdown/text documents.

**Usage**:

```bash
uv run simple-ar research-brief \
  --topic "reliable agents" \
  --local-document tests/fixtures/research/reliable_agents.md \
  --output-root runs/research-brief
```

The command creates a timestamped session with separate plan, search, document
ingest, read, and synthesize attempts. It does not silently retry or overwrite an attempt;
`--query`, `--provider`, `--max-results`, `--max-chunks`, and `--idea-limit` are
the deliberately small controls for this path. Pass the same optional
`--cache-dir` to later sessions to reuse downloaded full-text files; when it is
omitted, the cache stays inside the current session.

The capability is deterministic when `--model` is omitted. To use the shared
LLM transport for question/query planning and evidence synthesis, opt in
explicitly:

```bash
uv run simple-ar research-brief \
  --topic "reliable agents" \
  --local-document tests/fixtures/research/reliable_agents.md \
  --model "$SIMPLE_AR_MODEL"
```

The run prints and persists the selected mode. LLM mode requires the normal
`.env` provider settings; a missing key or failed model response is reported
as a failed attempt rather than replaced by deterministic prose.

### Retired `simple-ar research-experiment`

The separate design→experiment→analysis creator is retired. Use `research-session`
for research tasks. The old `--synthesis-file` argument is not silently translated.
Domain capabilities remain composable at library level; historical artifacts remain readable.

### `simple-ar research-session` (canonical task-driven entry)

Invalid LLM task plans get at most one correction attempt, never a silent fixed-plan fallback.
Returned proposals and validation errors are saved in `attempts/plan-*/task_plan_proposals.json`;
if no valid plan is produced, the session pauses without executing downstream actions.

Use `simple-ar research-session --config examples/survey/research.toml`
for a file-based start. The [survey example](../examples/survey/README.md) is a complete
literature-only file; the [configuration reference](CONFIG_REFERENCE.md) documents the
shared parser fields and defaults. For task shapes with different boundaries, use the
independent files in [Usage And Configuration](USAGE.md#choose-a-task-and-its-smallest-input).
Explicit CLI options override the file. `--outputs` selects `summary`, `report`,
and/or `experiments`; `--total-tokens`, `--llm-requests`, `--max-output-tokens`,
`--process-invocations`, and `--process-wall-seconds` expose the corresponding limits.
For an already prepared command that only needs one measurement and analysis,
select `--task-kind measurement --outputs experiments`; this path does not search
papers or design a new research candidate. The ordinary `auto` path still does.
Add `--session-root PATH` to continue the saved accepted plan. Omitted goal/outputs remain
unchanged; a supplied new goal, output set, local document, or execution configuration is an
explicit revision. The application keeps attempt history and reuses only measurements whose
command, result schema, protocol, preparation lineage, and protected assets still match; other
dependent steps are replanned. Changing task kind still requires a new session.

If a worker was killed before persisting its result, stop that worker and resume
with `--session-root PATH --recover-interrupted --model env`. This closes the
orphaned attempt in the same budget and retries its step, retaining earlier
evidence and valid report checkpoints. Apply input/configuration changes in a
separate resume. Ordinary API failures with a persisted result use normal resume.
An unavailable whole-document check pauses writing while retaining its drafts and
revision candidate. Resolve capacity or transport before resuming; unchanged writing
content reuses that work, but changed source/measurement inputs invalidate it.
For a model-backed session, pass `--model env` (using the configured
`SIMPLE_AR_MODEL`) or `--model NAME` on every resume. An omitted model fails
before state changes instead of silently replacing model work with deterministic work.
On resume, saved report settings remain in force unless a report option is
explicitly supplied; on a session that already requests a report, an explicit
report change rebuilds only report outputs. Use `research-report` to add the
report deliverable to a prefix that did not request one.
Changed search/ingestion settings that cannot be revised against the saved
session are rejected before execution; start a new session to apply them.
`[research].max_iterations` controls bounded follow-up rounds; `0` stops after
the first analysis.

#### Interaction policies and pending decisions

New CLI sessions default to `checkpoints`. Select `assisted`, `checkpoints`, or
`autonomous` with `--interaction`; TOML uses `[research] interaction = "checkpoints"`.
Sessions without a saved interaction field keep their legacy behavior. No mode supplies
missing facts, assets, or permissions; those remain hard blockers and cannot be accepted
without providing the required input.

Apply a mode change separately from `--reanalyze` or a report-only refresh; these
combinations are rejected rather than silently ignoring the mode. A delivery
decision reply can include both its report settings and a mode change.

- `assisted` confirms the first execution protocol and key delivery choices, and asks
  about research choices such as a supplement or candidate revision.
- `checkpoints` confirms the first protocol, material research-direction changes, and
  automatic delivery tradeoffs; bounded same-protocol supplements remain authorized.
- `autonomous` chooses among valid options within the saved round limit, but pauses
  when evidence or prerequisites do not support a safe next action.

Rich shows the decision id, question, rationale, options, and continuation commands.
Reply through the same `research-session` entry:

```bash
simple-ar research-session --session-root runs/research-session/<session> --topic "Original research goal" \
  --decision-id ID_FROM_RICH --decision-response accept

simple-ar research-session --session-root runs/research-session/<session> --topic "Original research goal" \
  --decision-id ID_FROM_RICH --decision-response revise \
  --decision-guidance "Add facts or give the revised research direction"
```

`--decision-response` accepts `accept`, `reject`, or `revise`. Revising a delivery
choice also requires a report setting, such as `--report-template analysis_report`.
TOML can provide `decision_id`, `decision_response`, and `decision_guidance` under
`[continuation]`. The proposal and its answer are separate research-decision artifacts;
the answer records its brief, execution, or report revision. If a reply is saved before those
inputs and the process stops, replaying the same decision restores the saved revision; different
revision terms are rejected. An exact replay after persistence does not repeat completed work,
and changed inputs make an old decision stale.
Rich is read-only and never waits for terminal stdin.

Continuation allowances use the same command and may be supplied as CLI options or a
`[continuation]` TOML table. `--authorize-remaining DIMENSION=AMOUNT` (repeatable) defines a
new remaining allowance for calls after this authorization; it does not estimate or resolve
unknown historical usage. `--additional-attempts` and `--additional-no-progress` raise the
persisted caps without resetting prior counters. Every allowance requires a stable
`--authorization-id` and `--authorization-reason`; replaying the same id and terms is
idempotent, while changed terms require a new id. Never edit the ledger or manifest directly.

On a completed session, authorization without input revisions keeps the session completed
and executes no research or report actions. Authorize capacity first, then run
`research-report --refresh` or repeat the original report-configuration command.
A full report refresh normally needs three attempts (writer, assembly, audit); failed retries
cost additional attempts. Report-only recovery does not require increasing process budgets.

```bash
simple-ar research-session --config research.toml --session-root runs/research-session/<session> \
  --local-document new-paper.md \
  --authorization-id review-20260924-1 --authorization-reason "Bounded continuation after review" \
  --additional-attempts 2 --additional-no-progress 1 \
  --authorize-remaining total_tokens=50000 --authorize-remaining llm_requests=8
```

At a paused or completed analysis checkpoint, use `--reanalyze` separately from input
revisions or continuation authorizations to reconsider
existing measurements and resume research decisions without requesting a report.
Historical artifacts and consumed budgets remain intact; an accepted follow-up
may run new experiments within the existing budget. Pending research follow-ups
must be continued before requesting reanalysis.

The completion table resolves the latest implementation, candidate measurement,
and analysis from the accepted plan. Earlier revision refs remain separately
listed so the candidate lineage is still navigable.

**Purpose**: run one bounded session whose accepted plan selects the steps
applicable to the task and supplied assets. With an execution command or
`--code-task-config`, the plan may include preparation, implementation,
measurement, and analysis. Without either, the session remains literature-only
and never creates an execution request or process.

When a model is available, the same application continues through `report` and
`report_audit`; `--no-report` is for debugging or prefix-only inspection. A
later `research-report` call can add that deliverable to a completed canonical
prefix without rerunning its research evidence or experiment. Use
`research-session-continue` only for an explicit recovery/revision decision.

**Usage** (the command must be the final option):

```bash
uv run simple-ar research-session \
  --topic "reliable agents" \
  --local-document tests/fixtures/research/reliable_agents.md \
  --cwd tests/fixtures/research \
  --primary-metric accuracy \
  --metric-direction accuracy=higher \
  --command python -c "print('accuracy: 0.75')"
```

To use the existing Code-Task implementation backend in the same session,
omit `--command` and provide a Code-Task TOML plus a model:

```bash
uv run simple-ar research-session \
  --topic "reliable agents" \
  --local-document tests/fixtures/research/reliable_agents.md \
  --code-task-config examples/code_task_medium_review/configs/code_task.toml \
  --model "$SIMPLE_AR_MODEL" \
  --output-root runs/research-session
```

The TOML remains the source of Code-Task project, benchmark, workspace,
baseline, and execution settings. The generated code-task artifacts stay under
the preparation/implementation attempts of the session and are normalized
into the same canonical result consumed by Analysis; no second code generator
is introduced.

The canonical application reuses the established Code-Task implementation and
validation capabilities inside its isolated preparation workspace. If the
proposal requires the `large` budget, `[execute].allow_large_edits = true` must
be set explicitly in the Code-Task TOML after reviewing the proposal;
otherwise the session preserves its artifacts and stops at the approval
boundary.

The optional `--cache-dir` is forwarded to document ingest. Valid cached
full-text files are reused on later sessions; the default remains session-local
for backward compatibility.

It preserves the same attempt-local artifacts as the individual entries and
adds no implicit retry or repair policy. Use `research-brief` for literature-only preparation; experiments stay in the
canonical research session.
For a session created with `--no-report`, use the narrow
`simple-ar research-report` command later to request the report. It reopens the
canonical application only for the missing report actions and reuses the
persisted synthesis, measurements, and analysis. For a single explicit
invocation, add `--model NAME --with-report` before the final `--command`;
`--report-reviewer`, `--max-review-iterations`, and `--max-section-tokens` are
stored in the same application configuration. The last option accepts `0` to
leave the per-section provider output cap unset.
Writer execution and checkpoints belong to `report/writing.py` in the canonical
application. Historical report-input projections are read-only; the former
standalone report-session execution APIs have been retired.

Omitting `--model` keeps planning, reading, synthesis, design selection, and
analysis deterministic. When `--model NAME` is supplied, the same shared
client is used for planning, bounded reading/screening and paper notes,
synthesis, selection among grounded research ideas, and result analysis;
provider failures remain visible and are not silently converted into offline
output.

To inspect a persisted capability session without rerunning it, use the existing
status command:

```bash
uv run simple-ar status runs/research-session/<session>
```

When the directory contains the canonical `session_manifest.json` (schema
`session_manifest.v2`), status prints the application state, current attempt,
bounded budget, attempt counts, and last decision. A legacy
`session_manifest.v1` remains read-only compatibility input. Existing pipeline
and Code-Task directories keep their original status behavior.

When an open session has no active attempt, status may also print
`Handoff: ready_for_report` or `Continuation: explicit ...`. These are persisted
next-step hints only; they do not mean a background process is running, and the
caller must invoke the next operation explicitly.

### `simple-ar research-session-continue`

**Purpose**: run one caller-supplied recovery experiment in an existing
session. For a canonical `session_manifest.v2`, it reuses literature, design,
and the failed candidate attempt, then runs deterministic analysis again. The
canonical boundary is deliberately limited to a simple explicit experiment;
paired runs, prepared data, and CodeTask failures use their own bounded paths.
Legacy `session_manifest.v1` execution is retired. Migration imports evidence,
not an executable continuation of the old stage plan.

**Usage** (the command must be the final option):

```bash
uv run simple-ar research-session-continue \
  --session-root runs/research-session/<session> \
  --cwd tests/fixtures/research \
  --primary-metric accuracy \
  --metric-direction accuracy=higher \
  --command python -c "print('accuracy: 0.90')"
```

For a canonical session, the corrected command must replace a technical
`failed`/`timed_out` candidate; a scientific negative result is retained as
evidence and is not silently retried. The new attempt records the previous
experiment as its parent, preserves the original attempt directory, and does
not rerun search or design. A report-requesting session returns with the next
report action; otherwise the repaired result is analyzed and the session can
complete. Historical sessions are not modified by this command.

### `simple-ar research-session-migrate`

**Purpose**: create a new canonical session from an old `session_manifest.v1`
without rewriting the old directory. The importer does not guess historical
budget remaining or translate unknown artifacts into current state. Copy only
small, explicitly named state artifacts when their files are available.

**Usage**:

```bash
uv run simple-ar research-session-migrate \
  --source-root runs/legacy/<session> \
  --destination-root runs/research-session/<successor> \
  --artifact search \
  --requested-output report
```

The command prints the new session path, `parent_session`, migration artifact,
imported/skipped counts, and `Historical budget: unknown_not_imported`. The
destination must be empty and outside the legacy session root.

### `simple-ar research-report`

**Purpose**: continue an existing `research-session` whose analysis handoff
is ready for reporting. The command uses the existing report Writer/Reviewer,
assembler, and audit implementations; it does not rerun search or the
experiment.

**Usage**:

```bash
uv run simple-ar research-report \
  --session-root runs/research-session/<session> \
  --model "$SIMPLE_AR_MODEL"
```

Use `--max-section-tokens N` to set an explicit Writer/Reviewer output cap
per section. `0` omits that per-call cap, which is the default for report
runtime configuration; the session budget still applies.

The report and audit are appended as new attempts under the same session. A
second invocation with an existing canonical report is idempotent: it reads
the completed state and does not rerun the Writer. Explicit `--refresh` reuses
the retained analysis and creates new report/audit attempts while retaining old
reports; it does not rerun measurements or analysis. Model-backed
sessions use the model to interpret hypotheses; numeric and execution-status
checks remain deterministic. Refresh uses the remaining session budget.
Omitted template/reviewer/limit flags keep saved settings. Explicit options
update report configuration and rebuild only report outputs. If the persisted
attempt/no-progress cap is exhausted, use `research-session` with an explicit
continuation authorization; report generation has no cap bypass.
Use `--reviewer disabled`
only when an explicit writer-only comparison is wanted; the final audit still
runs. Legacy sessions remain readable but cannot be resumed by a second report
executor. An invalid canonical session fails explicitly instead of falling back
to another workflow.

### Retired segmented CodeTask command

`research-code-task` and its separate session executor have been retired.
Use `research-session --code-task-config` for research tasks or standalone
`code-task` for coding tasks. Historical segmented results remain readable.


### `simple-ar status`

**Purpose**: print compact status for either a research run or a code-task run.

**Usage**:

```bash
uv run simple-ar status runs/<run-id>
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Research or code-task run directory. |

**Outputs**:

- prints stage status for pipeline runs
- prints environment, plan, patch, validation, benchmark, comparison, and repair state for code-task runs

**Notes**:

This command is read-only.

## Tools And MCP

### `simple-ar tools schema`

**Purpose**: export registered real tool schemas in MCP or OpenAI function-tool
format.

**Usage**:

```bash
uv run simple-ar tools schema --format mcp
uv run simple-ar tools schema --format openai --output tool_schema.json
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `--format` | enum | `mcp` or `openai`. Default: `mcp`. |
| `--output PATH` | path | Optional file output. Prints to stdout when omitted. |

### `simple-ar tools call`

**Purpose**: call one run-local read-only tool and write a compact trace.

**Usage**:

```bash
uv run simple-ar tools call runs/<run-id> list_experiment_artifacts
uv run simple-ar tools call runs/<run-id> search_generated_code --args-json '{"query":"run_experiment","max_matches":10}'
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Existing run directory. |
| `TOOL_NAME` | string | Registered tool name. |
| `--args-json JSON` | object | Tool arguments as a JSON object. |
| `--args-file PATH` | path | Read tool arguments from a JSON file. Useful on shells where inline JSON quoting is awkward. |
| `--debug-payloads` | flag | Keep larger trace payloads. Default traces are compact. |

**Outputs**:

- tool result JSON on stdout;
- `RUN_DIR/tools/tool_trace.jsonl`.

### `simple-ar tools serve-mcp`

**Purpose**: expose run-local read-only tools over MCP stdio.

**Usage**:

```bash
uv run simple-ar tools serve-mcp runs/<run-id>
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Existing run directory whose artifacts tools may inspect. |
| `--debug-payloads` | flag | Keep larger trace payloads. |

**Notes**:

- current server methods: `initialize`, `ping`, `tools/list`, `tools/call`;
- only real registered read-only experiment tools are exposed by default;
- no write, shell, network, or dependency-install tool is enabled by this command.

## Artifact Tools

### `simple-ar inspect`

**Purpose**: index and summarize local run artifacts.

**Usage**:

```bash
uv run simple-ar inspect runs/<run-id>
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Run directory to inspect. |

**Outputs**:

- `artifact_index.json`

**Notes**:

Inspection records metadata and bounded previews, not full-text reading. It does
not follow file symlinks or open special files such as named pipes. It does
not hash every file: `sha256` is null unless a Python caller explicitly uses
`build_artifact_index(..., hash_files=True)`. Experiment asset fingerprints are unchanged.

### `simple-ar search-artifacts`

**Purpose**: search indexed run artifacts with lexical retrieval.

**Usage**:

```bash
uv run simple-ar search-artifacts runs/<run-id> "accuracy" --top-k 5
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Run directory. |
| `QUERY` | string | Search query. |
| `--top-k N` | int | Number of results. Default: `8`. |
| `--include-operational` | flag | Also search manifests, runner metadata, and other operational files. |

**Outputs**:

- prints matching artifact chunks and their source paths
- writes `artifact_index.json`, `artifact_chunks.jsonl`, and `artifact_search_results.json`

**Notes**:

Search builds its own current index; running `inspect` first is unnecessary.
Files larger than 8 MiB are listed as `file_too_large` in `skipped_files` and in
CLI output, not searched as if a prefix were the complete source. Binary files
are not chunked. This artifact-search bound does not change table-analysis or
paper-ingest limits. Narrow/export a large log when text retrieval is needed.

### `simple-ar clean`

**Purpose**: review and delete rebuildable caches for one run while preserving
audit artifacts.

**Usage**:

```bash
uv run simple-ar clean runs/<run-id>
uv run simple-ar clean runs/<run-id> --yes
uv run simple-ar clean runs/<run-id> --all-caches
uv run simple-ar clean --shared-index
uv run simple-ar clean --shared-cache
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Run directory to clean. Optional when `--shared-index` or `--shared-cache` is used. |
| `--yes` | flag | Delete the displayed targets without the interactive `yes` prompt. |
| `--all-caches` | flag | Delete every known rebuildable cache/index/context artifact for this run after a stronger warning. |
| `--shared-index` | flag | Strong cleanup: clear the shared research index store across runs/tests. |
| `--shared-cache` | flag | Strongest shared cleanup: clear shared research indexes, literature provider cache, and external-agent handoff archives. |
| `--index-root PATH` | path | Shared index root for `--shared-index`/`--shared-cache`; defaults to `SIMPLE_AR_RESEARCH_INDEX_ROOT` or `.simple_ar_cache/research_index`. |
| `--literature-cache-root PATH` | path | Literature provider cache root for `--shared-cache`; defaults to `.simple_ar_cache/literature`. |
| `--allow-external-index-root` | flag | Allow shared cleanup to touch a path outside the current workspace. |

**Outputs**:

- prints a Rich tree preview before deletion
- canonical sessions (`session_manifest.json`, schema `session_manifest.v2`): deletes recognized provider-query JSON metadata in `cache/literature/` and `artifact_search_results.json`; full text is retained because saved reading can reference it
- historical layouts: deletes known caches such as `02-search/documents/fulltext_cache/` and `02-search/documents/extracted_text/`
- removes this run's rows from the shared SQLite research index when `index_meta.json` points to a workspace-local shared store
- with `--all-caches`, canonical sessions also delete artifact indexes/chunks; historical layouts additionally include their known research indexes, code-task repo maps, locate outputs and context packs

**Notes**:

`clean` keeps reports, manifests, papers, parser audit files such as
`fulltext_extraction.json`, read-stage Paper Briefs, synthesis briefs, retained
debug coverage reports when present, and portable `research_index/chunks.jsonl`.
It does not delete the run directory itself. Canonical `attempts`, `outputs`,
`documents`, downloaded full text and unrecognized cache files remain intact;
custom external cache paths are not followed.
Provider metadata above the 1 MiB inspection bound is retained and listed as
uninspected, not deleted based only on its filename. In historical layouts,
`--all-caches` removes `research_index/chunks.jsonl` as rebuildable index data,
but still keeps final reports, metadata, manifests and benchmark outputs.

`--shared-index` is stronger than `--all-caches`: it clears the shared
SQLite/LanceDB accelerator store across runs, so future runs must rebuild index
state. It keeps run-local audit artifacts because it does not touch run
directories.

`--shared-cache` is stronger again: it clears the shared research index,
`.simple_ar_cache/literature`, and `.simple_ar_cache/agent_handoff_archives`,
so future runs may need to re-query literature providers, rebuild local indexes,
and will no longer have prior external-agent handoff transcripts.

## Code Task Commands

Code-task commands prepare an isolated workspace under
`runs/<run-id>/code_task/workspace`. Existing-project runs default to `auto`,
which prefers a detached git worktree and falls back to a guarded copy when Git
cannot be used safely. Explicit `git_worktree` fails with an actionable
checklist instead of silently falling back. Greenfield runs start from an empty
workspace and generate the project there. The original project is never edited.

For normal use, start with the high-level orchestration commands. The low-level
primitive commands are mainly for debugging, learning, or fine-grained human
intervention.

### High-Level Orchestration

For existing projects, ordinary `execute` uses one patch plan. Work-plan/batch
creation is opt-in through `--to-step work-plan` / `--to-step batch` or an
existing work plan, including interactive mode. These are not mandatory stages
for every modification.

#### `simple-ar code-task init`

**Purpose**: create a code-task run, prepare the editable workspace, and build
the first code index.

**Usage**:

```bash
uv run simple-ar code-task init --config examples/code_task_medium_review/configs/code_task.toml
uv run simple-ar code-task init --code-root path/to/project --task-file task.md --benchmark-command "python main.py"
uv run simple-ar code-task init --kind greenfield --task-file task.md --benchmark-command "python generated_project/main.py"
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `--config PATH` | path | TOML config for init settings. CLI flags override config values. |
| `--kind MODE` | enum | `existing_project` for patching an existing codebase, or `greenfield` for from-scratch generation. |
| `--code-root DIR` | path | Source project. Required for `existing_project`; optional scaffold/source root for `greenfield`. |
| `--task-file PATH` | path | Markdown/text task description. Required unless set in config. |
| `--output-root DIR` | path | Directory where the code-task run is created. |
| `--name TEXT` | string | Run name suffix. |
| `--benchmark-command TEXT` | string | Command run inside the workspace before and after edits. |
| `--primary-metric NAME` | string | Primary metric for before/after verdicts. |
| `--metric-direction NAME=DIRECTION` | repeatable | Metric direction: `higher`, `lower`, `resource`, or `ignore`. |
| `--env-mode MODE` | enum | `current` or `external`. |
| `--python PATH` | path | Python executable for `--env-mode external`. |
| `--workspace-mode MODE` | enum | `auto`, `copy`, `git_worktree`, `sparse_copy`, or `empty`. `greenfield` defaults to `empty`; existing projects default to `auto`. |
| `--workspace-include GLOB` | repeatable | Include pattern for `sparse_copy`. |
| `--workspace-exclude GLOB` | repeatable | Additional exclude pattern for `sparse_copy`. |
| `--workspace-reuse-source-venv` | flag | Reuse a detected source `.venv` Python as external execution policy. |
| `--max-file-bytes N` | int | Maximum copied file size in copy/sparse modes. Use `0` to disable. |

**Outputs**:

- `code_task/manifest.json`
- `code_task/task.md`
- `code_task/workspace/`
- `code_task/meta/codebase_index.json`
- `code_task/meta/repo_map.json`
- `code_task/meta/repo_map_summary.md`

**Notes**:

For reusable settings, prefer TOML. See
[Configuration Reference](CONFIG_REFERENCE.md#standalone-code-task-config).

#### `simple-ar code-task execute`

**Purpose**: advance a code-task run to the next safe stop based on current
artifacts.

**Usage**:

```bash
uv run simple-ar code-task execute runs/<run-id> --config examples/code_task_medium_review/configs/code_task.toml
uv run simple-ar code-task execute runs/<run-id> --to-step propose-edits
uv run simple-ar code-task execute runs/<run-id> --apply-proposed-edits --timeout 60
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--config PATH` | path | Optional TOML for model routing, budget, and runtime settings. |
| `--to-step STEP` | enum | Stop no later than `probe`, `baseline`, `work-plan`, `batch`, `plan`, `propose-edits`, `apply-edits`, `review`, `validate`, `run`, `analyze-failure`, or `repair`. |
| `--dry-run` | flag | Print the next action without writing artifacts. |
| `--model NAME` | string | Model override for LLM-backed steps. |
| `--no-llm` | flag | Use deterministic fallbacks where possible. |
| `--timeout N` | int | Benchmark timeout. |
| `--baseline-policy MODE` | enum | Existing-project baseline handling: `auto`, `run`, `skip`, `provided`, or `none`. Use `skip`/`none` for expensive baselines, or `provided` with a metrics file. |
| `--baseline-metrics-file PATH` | path | JSON or metric-line file used when `--baseline-policy provided`. |
| `--planning-mode MODE` | enum | Greenfield planning: `tool_agent` uses staged planning and bounded review; `compact` uses a single architecture call before retries. |
| `--yes` | flag | Auto-approve inline review gates in normal execute mode; with `--interactive`, auto-continue primitive prompts. Use only after you are comfortable approving the reviewed plan/proposal. |
| `--interactive` | flag | Debug mode: confirm each primitive step instead of running continuously to the next review gate. |
| `--no-review-inline` | flag | Disable inline review prompts and stop at review gates instead. |
| `--skip-validation` | flag | Run benchmark even when static validation has not passed. |
| `--strict-validation` | flag | Treat higher-risk validation warnings as errors. |
| `--validation-max-file-bytes N` | int | Max file size scanned by static validation. |
| `--apply-proposed-edits` | flag | Apply reviewed `proposed_edits.json` after plan approval. |
| `--allow-large-edits` | flag | Allow a reviewed proposal that exceeds the normal edit budget. |
| `--allow-planning-fallback` | flag | Allow explicit offline planning or fallback after known, complete response-format failures; not after provider, budget, deadline or partial-stream failures. |
| `--llm-retry-attempts N` | int | Bound completed-response corrections for work/patch planning and greenfield generation, including the first attempt. Transport retries belong to the shared model connection; unreconciled operations prevent correction and fallback. |
| `--repair-rounds N` | int | Number of bounded repair proposals after failure. |
| `--max-files N` | int | Context file budget for LLM steps. |
| `--max-source-chars-per-file N` | int | Per-file source context budget. |
| `--env-mode MODE` | enum | `current` or `external`. |
| `--python PATH` | path | Python executable for external env mode. |

**Outputs**:

- `code_task/work_plan.md` and `work_plan.json`
- `code_task/attempts/attempt-*/batches/batch-*/batch_state.json`
- `code_task/patch_plan.md`
- `code_task/meta/proposed_edits.json`
- `code_task/meta/applied_edits.json`
- `code_task/meta/review_report.json` and `review_report_post_run.json`
- `code_task/meta/validation_report.json`
- `code_task/meta/resource_probe.json` and `resource_decision.json` after `probe`
- `code_task/memory/task_memory.md`, `compressed_memory.md`, and `review_findings.jsonl`
- `code_task/run/baseline/`, `code_task/run/patched/`, `code_task/run/comparison.json`
- `code_task/summary.md`

**Notes**:

`execute` preserves review gates without forcing a separate command for every
gate. In an interactive terminal, it renders a yellow Rich review panel for
`patch_plan.md`, `proposed_edits.json`, or large-edit approval and asks whether
to continue. In non-interactive shells it stops cleanly at the gate unless
`--yes` is supplied. Completed steps are shown as skipped on resume. Use
`--interactive` only when debugging primitive steps; combine it with `--yes` to
auto-continue those primitive prompts. Use `--yes` in normal execute mode only
when automated approval is intentional, and `--no-review-inline` when you want
the older stop-and-rerun behavior. Full workflow walkthroughs live in
[Usage And Configuration](USAGE.md#recommended-path-toml--execute).

When LLM work planning or patch planning returns malformed JSON, execute stops
with `llm_planning_failed` and does not write an offline fallback plan. Rerun
the same `execute` command to retry the LLM step. Use `--no-llm` for a fully
deterministic plan, or `--allow-planning-fallback` only when a fallback plan is
acceptable for the current task.

After edits are applied, `execute` also runs a structured reviewer step before
static validation, then repeats a post-run review when patched metrics are
available. Blocking reviewer findings are written to `code_task/memory/` so the
next repair attempt can reason from the latest failure evidence.

When `--config PATH` is provided, `execute` also reads standalone code-task
generation settings from `[implementation]` and `[resource]`. This is how
greenfield tasks select the local backend or an explicit Codex/Claude/OpenCode
handoff without adding provider-specific CLI flags.

For greenfield tasks, `execute` also writes
`code_task/meta/dependency_advice.json` and `.md` before implementation
planning. The JSON contains a full scan of installed Python distributions; the
terminal output and model context use the compact task-relevant subset. This is
advice-only and never installs packages automatically.

Greenfield planning defaults to `tool_agent`, which writes intermediate
requirements, architecture, interface, file-plan, and planning-review artifacts
under `code_task/meta/planning/`. Use `--planning-mode compact` for a lower-call
planning path; subsequent file generation, review, and execution are shared.

Generated-project review checks structure and scaffold risks; it does not
certify task acceptance. Natural-language command names, output paths and
library preferences are assessed contextually, not by a fixed phrase-matching
gate. Model findings remain advisory; actual validation and benchmark evidence
are separate. When a review contract changes, resume preserves the previous
report under `code_task/meta/review_history/` before refreshing the active report.

Bounded greenfield review/runtime repair selects related files from the actual
diagnostics and prepares one isolated candidate through the shared code owner.
Application retains API/static guards and snapshot rollback; repaired files
resync `code_task/meta/code_artifacts.json` before review/validation continues.
Completed repair reuse checks inputs and project bytes. Legacy unbound results
remain history; explicit new rounds preserve that history and may continue work.
Interrupted candidates are not blindly replayed. Blocking findings still stop
execution with files, diagnostics and snapshots preserved.

#### `simple-ar code-task decide-plan`

**Purpose**: record a human decision for the current patch plan.

**Usage**:

```bash
uv run simple-ar code-task decide-plan runs/<run-id> --decision approve
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--decision VALUE` | enum | `approve`, `reject`, or `revise`. Required. |
| `--note TEXT` | string | Optional review note. |
| `--reviewer TEXT` | string | Reviewer label. Default: `user`. |

**Outputs**:

- updates plan decision state in `manifest.json`

**Notes**:

Use `revise` or `reject` when the patch plan should not proceed to edit
proposal generation.

### Low-Level Primitives

The following commands are usually called by `execute`. Use them when you want
manual control or need to debug a specific stage.

#### `simple-ar code-task map`

**Purpose**: rebuild repo-map artifacts from the editable workspace.

**Usage**:

```bash
uv run simple-ar code-task map runs/<run-id> --show-summary
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--no-refresh-index` | flag | Reuse the existing `codebase_index.json`. |
| `--show-summary` | flag | Print `repo_map_summary.md`. |

**Outputs**:

- `code_task/meta/codebase_index.json`
- `code_task/meta/repo_map.json`
- `code_task/meta/repo_map_summary.md`

**Notes**:

This command is deterministic and does not call the LLM.

#### `simple-ar code-task locate`

**Purpose**: rank likely editable files and read-only evidence from the repo map.

**Usage**:

```bash
uv run simple-ar code-task locate runs/<run-id> --query "improve classifier"
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--query TEXT` | string | Locate query. Defaults to `code_task/task.md`. |
| `--top-k N` | int | Candidate count per group. Default: `8`. |
| `--refresh-map` | flag | Rebuild index and repo map before ranking. |
| `--no-read-only` | flag | Omit protected read-only evidence files. |
| `--show-summary` | flag | Print `locate_results.md`. |

**Outputs**:

- `code_task/meta/locate_results.json`
- `code_task/meta/locate_results.md`

**Notes**:

Protected evidence such as tests and benchmarks can inform planning but is not
editable by default.

#### `simple-ar code-task context`

**Purpose**: build a bounded prompt-ready context pack.

**Usage**:

```bash
uv run simple-ar code-task context runs/<run-id> --max-files 8 --max-total-chars 20000
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--query TEXT` | string | Locate query. Defaults to `task.md`. |
| `--top-k N` | int | Locate candidate budget. |
| `--max-files N` | int | Maximum snippets included. |
| `--max-source-chars-per-file N` | int | Per-file snippet budget. |
| `--max-total-chars N` | int | Total snippet budget. |
| `--refresh-map` | flag | Rebuild map before packing context. |
| `--show-prompt` | flag | Print `prompt_context.md`. |

**Outputs**:

- `code_task/context_packs/context-NNN/context_pack.json`
- `code_task/context_packs/context-NNN/prompt_context.md`
- `code_task/context_packs/context-NNN/selected_snippets.jsonl`

**Notes**:

This command does not modify files.

#### `simple-ar code-task probe`

**Purpose**: inspect runtime and project environment signals.

**Usage**:

```bash
uv run simple-ar code-task probe runs/<run-id>
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--env-mode MODE` | enum | `current` or `external`. |
| `--python PATH` | path | Python executable for external env mode. |

**Outputs**:

- `code_task/meta/environment_report.json`
- `code_task/meta/resource_probe.json`
- `code_task/meta/resource_decision.json`

**Notes**:

`probe` does not install dependencies or run project benchmark code.

#### `simple-ar code-task baseline`

**Purpose**: run the recorded benchmark before applying edits.

**Usage**:

```bash
uv run simple-ar code-task baseline runs/<run-id> --timeout 60
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--command TEXT` | string | Override the benchmark command for this run. |
| `--timeout N` | int | Benchmark timeout. |
| `--skip-validation` | flag | Run even when static validation has not passed. |
| `--env-mode MODE` | enum | `current` or `external`. |
| `--python PATH` | path | Python executable for external env mode. |

**Outputs**:

- `code_task/run/baseline/execution_report.json`
- `code_task/run/baseline/stdout.txt`
- `code_task/run/baseline/stderr.txt`
- `code_task/run/baseline/metrics.json`

**Notes**:

The benchmark command runs inside `code_task/workspace`.

#### `simple-ar code-task work-plan`

**Purpose**: generate a batch-oriented implementation work plan.

**Usage**:

```bash
uv run simple-ar code-task work-plan runs/<run-id>
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--model NAME` | string | Model override. |
| `--no-llm` | flag | Use fallback planning. |
| `--force` | flag | Regenerate existing work-plan artifacts. |
| `--allow-planning-fallback` | flag | Allow fallback after known, complete work-plan response-format failures, not transport failures. |
| `--llm-retry-attempts N` | int | Completed-response work-plan correction bound, including the first attempt; no additional transport retry layer. |
| `--max-files N` | int | Planning context file budget. |
| `--max-source-chars-per-file N` | int | Per-file source context budget. |

**Outputs**:

- `code_task/work_plan.json`
- `code_task/work_plan.md`

**Notes**:

Work-plan target files become part of the later edit-scope check.

#### `simple-ar code-task batch`

**Purpose**: create an attempt/batch state directory for one work-plan item.

**Usage**:

```bash
uv run simple-ar code-task batch runs/<run-id> --work-item W1
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--work-item ID` | string | Work-plan item id, such as `W1`. Required. |
| `--attempt-id ID` | string | Optional attempt id, such as `attempt-001`. |
| `--force` | flag | Create a new batch even when one already exists. |

**Outputs**:

- `code_task/attempts/attempt-NNN/attempt_state.json`
- `code_task/attempts/attempt-NNN/batches/batch-NNN/batch_state.json`

**Notes**:

`batch` does not call the LLM or edit files.

#### `simple-ar code-task plan`

**Purpose**: generate a human-reviewable patch plan for the active batch.

**Usage**:

```bash
uv run simple-ar code-task plan runs/<run-id>
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--model NAME` | string | Model override. |
| `--no-llm` | flag | Use fallback plan. |
| `--force` | flag | Regenerate existing plan. |
| `--allow-planning-fallback` | flag | Allow fallback after known, complete patch-plan response-format failures, not transport failures. |
| `--llm-retry-attempts N` | int | Completed-response patch-plan correction bound, including the first attempt; no additional transport retry layer. |
| `--max-files N` | int | Context file budget. |
| `--max-source-chars-per-file N` | int | Per-file source context budget. |

**Outputs**:

- `code_task/patch_plan.md`

**Notes**:

Run `decide-plan` before generating edit proposals.

#### `simple-ar code-task propose-edits`

**Purpose**: ask the model to produce controlled old/new text edits.

**Usage**:

```bash
uv run simple-ar code-task propose-edits runs/<run-id>
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--model NAME` | string | Model override. |
| `--no-llm` | flag | Write deterministic empty proposal. |
| `--force` | flag | Regenerate existing proposal. |
| `--max-files N` | int | Editable context file budget. |
| `--max-source-chars-per-file N` | int | Per-file source context budget. |
| `--allow-large-edits` | flag | Accept a large but bounded proposal after review. |

**Outputs**:

- `code_task/meta/proposed_edits.json`
- `code_task/meta/proposal_warnings.json`
- per-batch proposal artifacts when an active batch exists

**Notes**:

The proposal format is structured JSON, not a unified diff.

#### `simple-ar code-task apply-edits`

**Purpose**: safely apply controlled old/new text edits inside the workspace.

**Usage**:

```bash
uv run simple-ar code-task apply-edits runs/<run-id>
uv run simple-ar code-task apply-edits runs/<run-id> --edits-file runs/<run-id>/code_task/repairs/repair-001/proposed_edits.json
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--edits-file PATH` | path | Specific proposal file to apply. |
| `--allow-unapproved-plan` | flag | Bypass plan approval for local tests/demos. |
| `--allow-large-edits` | flag | Apply a reviewed proposal that requires large-edit approval. |

**Outputs**:

- modifies files under `code_task/workspace`
- `code_task/meta/applied_edits.json`
- `code_task/patch.diff`

**Notes**:

Path, edit-scope, old-text, and large-edit checks run before file writes.
An empty `old` creates an absent file; it never replaces an existing file.
Model proposals may create files within an explicit edit-scope allowlist, or an
explicit batch target. Protected paths remain read-only. Creation uses the same
edit budgets and snapshots as replacements, including removal during rollback.

#### `simple-ar code-task validate`

**Purpose**: run lightweight static validation over the workspace.

**Usage**:

```bash
uv run simple-ar code-task validate runs/<run-id> --strict
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--strict` | flag | Treat higher-risk warnings as errors. |
| `--max-file-bytes N` | int | Maximum file size scanned. |

**Outputs**:

- `code_task/meta/validation_report.json`

**Notes**:

Validation is static and intentionally conservative.

#### `simple-ar code-task run`

**Purpose**: run the benchmark after edits.

**Usage**:

```bash
uv run simple-ar code-task run runs/<run-id> --timeout 60
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--command TEXT` | string | Override recorded benchmark command. |
| `--timeout N` | int | Benchmark timeout. |
| `--skip-validation` | flag | Run even when static validation has not passed. |
| `--env-mode MODE` | enum | `current` or `external`. |
| `--python PATH` | path | Python executable for external env mode. |

**Outputs**:

- `code_task/run/patched/execution_report.json`
- `code_task/run/patched/stdout.txt`
- `code_task/run/patched/stderr.txt`
- `code_task/run/patched/metrics.json`
- `code_task/run/comparison.json` when baseline metrics also exist

**Notes**:

Metric comparison distinguishes "benchmark passed" from "objective improved."

#### `simple-ar code-task analyze-failure`

**Purpose**: summarize the latest failed validation or benchmark result.

**Usage**:

```bash
uv run simple-ar code-task analyze-failure runs/<run-id>
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |

**Outputs**:

- `code_task/run/patched/failure_analysis.md` or `code_task/meta/failure_analysis.md`

**Notes**:

This command is deterministic and does not modify source files.

#### `simple-ar code-task repair`

**Purpose**: propose bounded repair edits from the latest failure context.

**Usage**:

```bash
uv run simple-ar code-task repair runs/<run-id>
```

**Options**:

| Option | Type | Description |
| --- | --- | --- |
| `RUN_DIR` | path | Code-task run directory. |
| `--model NAME` | string | Model override. |
| `--no-llm` | flag | Write deterministic empty repair proposal. |
| `--max-files N` | int | Repair context file budget. |
| `--max-source-chars-per-file N` | int | Per-file source context budget. |

**Outputs**:

- `code_task/repairs/repair-NNN/proposed_edits.json`
- updates `code_task/summary.md`

**Notes**:

Repair proposals are not applied automatically. Review and apply them with
`apply-edits --edits-file ...`.
