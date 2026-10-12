# Workflows And Artifacts

[中文版本](WORKFLOWS_zh.md)

This document explains what SimpleAutoResearch is doing internally: task-driven
capabilities, artifact ownership, recovery boundaries, and module boundaries. It avoids
duplicating the full artifact manual; for concrete commands and file trees, see
[Usage And Configuration](USAGE.md). For command flags, see
[CLI Reference](CLI_REFERENCE.md); for TOML fields, see
[Configuration Reference](CONFIG_REFERENCE.md).

## Task-Driven Execution And Recovery

Ordinary `start` passes goals, read-only materials and constraints to
`ToolApplication`. One action loop combines source investigation, code/execution,
analysis, writing, visual/image work and publication through `CommonToolGateway`,
reusing domain modules, `SessionController`, artifacts and the budget ledger.
Creation preferences express outcomes, not mandatory internal pipelines or access
grants. Network, vision, process proposals and resources have explicit boundaries;
process commands still need exact approval. Workspace isolation is not an OS sandbox.

Exact table, fixed code scope/validation and reproduction protocols retain the
TOML/`research-session` path through `ResearchApplication`. These serve public new
tasks as well as history. Native tasks accept feedback and recover through
`start --resume-task`; structured sessions keep their own recovery entrypoints.
Recovery does not rebuild old tasks with new defaults, accept partial responses
as delivery or replay completed side effects. The accepted-plan, segmented Writer
and capability flows below describe their structured consumers and saved tasks.
Module reuse does not establish acceptance of every combination, graphical feedback,
scientific capability or delivery reliability.

New default research plans generate a standalone summary only when requested
(or when summary is the default deliverable). With local sources and explicit
materials-only scope, a report-only survey/research task can ingest and write
directly from retained original text. Reading notes and synthesis remain
available when the accepted plan selects them; explicit summary, assessment,
design and experiment requests keep their evidence dependencies. This does not
certify source comprehension or paper quality. Recovery follows the saved plan.

When the accepted task contains neither search nor evidence reading, query
planning retains the deterministic source handoff without a second model call.
The Writer still receives originals and may read relevant passages. This does
not merge sources or invent evidence: one paper can report multiple methods,
controls and ablations, whose comparisons remain limited to their tested settings.

For material writing, outline lookups and the Writer's optional gap lookup are
separate bounded batches under the same tool allowance. An outline read does not
disable the author's later request for a missing passage. Checkpoints retain
both results; historical tasks keep their already-consumed batch rather than
replaying it. Writing, format recovery and section review preserve all supplied
read results; cost is controlled by tool allowances and the session token budget,
not a second last-six-results filter. Review evidence references use actual JSON Pointers to supplied
fields. Resolving a field establishes its location and ownership, not scientific
support; quotations and old saved references retain their validation rules.
Bounded source overviews keep the highest-ranked matching passage from each
selected section; a section opening is additional context when capacity permits,
not a replacement for a retrieved result. The views still represent partial reading.

### Changed-source review coverage

For Python-only local patches, supplementary context follows indexed imports
and direct callers, including conditional, function-local and relative imports,
rather than unrelated role-ranked data or documents. This is static association,
not dynamic loading or a complete transitive call graph. Whole-project reviews,
non-Python changes and historical indexes without import observations keep their
broader selection. Deterministic checks retain their original project scope.

Existing-project review allocates its existing cluster/file allowance to changed
files first, then selects background role clusters. The review metadata lists
`changed_files_outside_review_clusters` when the allowance cannot cover a larger
change set; selection is not proof of whole-file review or patch correctness.
Review and repair share exact current-source windows around diff/failure lines,
with range and partial-coverage labels. Generated-project review without a diff
retains separate head/tail views, not a fabricated contiguous source excerpt.
Protected source remains read-only; review does not authorize edits or replace
runtime validation. This changes input selection, not the number of model rounds.

Model concerns are advisory, including matching concerns from several groups.
Repeated wording is not independent failure evidence. Scope violations, definite
missing interfaces and recorded validation failures remain blocking and are
recorded without model calls. Batch edit limits filter editable paths while
preserving their located source windows; only missing windows use prefix reads.
Only an
already-authorized command may run; neither a warning nor a passing static check
certifies behavior. Existing failed review records are not rewritten on recovery.

### Initial source defects versus new defects

CodeTask initialization retains only initial syntax-error checksums in the existing
workspace manifest. It reuses the source index, not a second source snapshot or
all-file integrity system. Non-strict static validation records unchanged initial
syntax failures as warnings; changed/new failures and strict mode still fail.
Refreshing the source index does not replace initial evidence. Saved legacy copy
tasks without that evidence remain conservative; current source is not backfilled
as an initialization baseline. A frozen Git baseline remains usable.

This distinction does not prove that an initial defect is intentional, that a
project can run, or that a patch works. Inspect warnings and run the declared
behavior checks; static `passed` is not runtime success.

### Optional natural-language task setup

`start --chat` precedes the explicit structured configuration/session path. A small setup draft
retains original user replies, proposed settings, asset approvals and the existing
budget ledger for this conversation. Named asset previews are bounded and explicitly
mark unread/truncated content. Models select semantic settings, not execution powers.
After confirmation, the same serializer validates and saves task TOML. Setup recovery
(`--resume-setup`) reuses awaiting proposals and accounting; execution recovery remains
the existing session controller. Structured setup and descriptive execution need no API;
opting into chat uses the configured model for clarification.

For reproduction, the same adapter may read named project instructions/entry source
and explicitly supplied data-location metadata, and request bounded additional
indexed text. It proposes a source-backed command and
protocol for confirmation, not an execution result. Accepted values feed the existing
serializer/executor; directory and timeout are not model authority. Preparation notes
remain distinct from scientific sources. There is no second execution runtime or
automatic dependency installer behind this conversation.
Repeated `--data-path` inputs also enter `[assets].data` and the existing session
inventory, not document ingestion or a separate data registry. Direct measurements
can use the same named inputs without relocation. CodeTask preparation copies
declared project data into the isolated workspace and protects it from automated
patches; external inputs remain in place and completed workspaces are unchanged.

### Explicit task dependency preparation

A confirmed `[execution.environment]` venv or explicit current-environment check adds `prepare_execution` before
the declared measurement. The existing preparation attempt owns the environment,
`environment_setup.json`, process records and logs; the original process backend
and session ledger account for creation, selected requirements/explicit project package installation and
`pip check`. `execution.json` is the existing `prepared_execution.v1` artifact,
binding only the command's Python executable. There is no second environment runtime.
Failed setup never starts the scientific command. Inspect with `status RUN`; explicitly
continuing a failed session creates a fresh attempt without clearing previous usage.
Completed recovery reuses the prepared command/results. A venv is not an OS sandbox
or a portable environment image; installation does not certify scientific conditions.
This single-command path does not change CodeTask's current/external environment modes.
An optional `check_command` runs after venv setup, or alone in current mode with
no installs. It uses the same backend, budget and logs; failure or timeout stops
measurement. The formal command is not shortened, and check output stays in
preparation records rather than entering measured results. The user supplies the
check; the framework does not infer a safe smoke command from a filename.

### Describe and plot existing data (no model required)

For **analysis, figures and a written report in the same session**, add
`--with-report --model env` to the command below. Confirmed `start --chat`
can select this when you ask for a report. Analysis still runs without API
calls; the existing material writer then uses the saved numerical package and
figures, with review and audit. No online research, training or second writing
session is implied. Expert TOML uses `outputs = ["data_analysis", "report"]`
and a model connection. Resume the printed session with the same model; do not
rerun setup or create another writing task.

Newly guided data reports compose the sections together, then review the complete
body with bounded revision. Existing task configs keep their accepted scopes.

When requesting a report, add `--material README.md` for data documentation or
notes, and `--document reference.pdf` for a reference paper. They are read-only
local sources, retained through the existing document bundle and available to
writing after resume. Keep your goal in `--goal`, metadata in these files, and
the raw table in `--data-file`; documentation does not override confirmed
analysis settings or become measured evidence. Analysis-only does not consume
these additional sources. Captions describe the plotted quantities; detailed
provenance and rebuilding records stay in the linked analysis package.

```bash
uv run simple-ar start --kind data_analysis --goal "Describe my measurements" \
  --data-file ./observations.csv --value-column score --group-column method \
  --observation-unit "one run" --interaction autonomous --yes
```

Explicit columns and row semantics are required; IDs and metrics are not guessed.
Setup checks bounded input shape and selected column names before saving task files.
Unknown columns report the available names. This preview does not compute measurements
or certify numeric values; ingestion validates values and freezes the actual bytes.
`observations` (default) calculates count, mean and sample standard deviation per group/column.
For bars of existing means or other summaries use `--data-mode values`: labels must be unique,
values are not averaged again and error bars are not inferred. `--data-missing omit`
explicitly allows per-column omission with counts; otherwise missing values fail, not become zero.
Nonfinite/nonnumeric values, duplicate columns, malformed/empty input and nested JSON fail explicitly.
Reports distinguish the configured missing-value policy from observed input use.
Counts cover all selected records (complete x/y pairs for coordinates), not the
writing preview; `reject` does not mean rows were deleted.

The same session/controller freezes the UTF-8 CSV/TSV or homogeneous JSON records and settings,
then emits `analysis.json`, `analysis.md`, copied data, editable SVGs, vector PDFs and
300-dpi PNG previews. Matplotlib is installed with the package; no GUI or separate
plotting setup is needed. One figure counts once regardless of its three formats. No search, model,
training or invented experiment status is involved. A completed calculation does not verify
collection, units, independence, significance or causality. Outputs contain copied data: review
sensitivity before sharing. Physical limits default to 20 MiB input and 100 figure pages
(`--data-max-mb`, `--data-max-figures` override); excess pages fail rather than drop data.
Separate metrics have separate axes; categories are paginated without dropping data.
`--figure-width column|wide` uses generic 3.5/7-inch targets, not venue-specific dimensions.
Visual checks are initially `not_performed`, separate from numeric computation.

For numeric curves or coordinate pairs, use `--data-mode values --data-plot line|scatter
--x-column step`. Optional `--group-column` separates category series without
aggregating or pairing them. Repeat `--value-column` for separate
axes; `--x-unit` records the declared x unit. Lines sort by numeric x and require
unique x coordinates within each group; scatter retains duplicate x values.
Explicit `--data-missing omit` keeps missing coordinates in the package but does
not plot unavailable scatter positions. Lines need complete x and break at missing y.
Group legends paginate on common axes without dropping categories.
Dense scatter panels use smaller translucent markers and retain every complete
coordinate pair in supplied record order; no sampling, jitter or smoothing.
For writing, the package also carries all-complete-pair counts and per-group
axis minima, quartiles, medians and maxima. Quartiles interpolate at `(n-1)*p`;
these are marginal descriptive summaries, not joint association, significance
or native image inspection. Raw points are not replaced by the summaries.
Opt-in `--data-association pearson` adds the descriptive coefficient for complete
x/y pairs inside each group, not a difference or implicit pooled result. Constant
axes and fewer than two pairs produce an explicit undefined status. Two nonconstant
pairs give ±1; there is no significance or causal interpretation. The same copied
data, numerical attachment, writing projection and relocation checks carry it.
There is no smoothing, regression, replicate aggregation or guessed error bar.
For row-level `observations` analysis, each group/column also retains the
full nonmissing empirical five-number distribution. These summaries and counts
travel into writing and its numerical appendix; they do not turn a mean bar into
a box plot or imply paired differences. Imported legacy packages recompute them
from the copied input without changing the original package.
`--data-max-points` defaults to 10000 rows per coordinate figure and is adjustable;
overflow fails without sampling. The same copied input, resume and rebuilding apply.
See the complete [coordinate case](../examples/data-curves/README.md).

Resume with `research-session --session-root PATH` even if the original data changes/disappears;
completed input snapshots are reused. Copy the completed analysis directory, then rebuild figures
there with `python -m simple_ar.result_analysis.table analysis.json` using an installed package.
The rebuilder refuses changed computed records; it is not a full file-integrity certification.
Explicit rebuilding also refreshes generated captions/encoding in `analysis.json`
and `analysis.md`. Ordinary completed-session recovery leaves saved deliveries unchanged.
Supply the completed `analysis.json` as `--material` in a later writing task to
recheck the values and attach its editable figures. Keep its copied `input.csv`,
`input.tsv` or `input.json` beside it; do not submit a raw table as writing JSON.
Complex statistics are not inferred.

For explicitly matched baseline/candidate observations, add `--paired-baseline baseline`
and select both columns. Each row must represent a matched pair of the same quantity/unit.
The analysis adds paired differences, sample standard deviation and standard error;
`omit` uses jointly nonmissing pairs. At least two pairs are needed for uncertainty.
The extra difference figure shows ±1 SE assuming independent pairs, not a confidence
interval or significance test. Rebuilding and material writing recheck the complete
paired result against copied input. Try the [complete no-API case](../examples/data-paired/README.md).
The baseline/candidate bar panels share the declared quantity's numeric scale;
the paired-difference panel has its own labelled axis. This prevents independent
automatic scaling from disguising a difference or suggesting a larger one.

Server numerical acceptance: `python scripts/validate_table_nist.py --output-root runs/nist-NEW`.
It compares two official NIST StRD datasets with certified mean/sample-standard-deviation values
and an independent Decimal calculation. This tests arithmetic and CLI delivery, not an agent,
paper-reproduction or complete NIST benchmark score. Official data is fetched once and retained;
if server download is blocked, put the unmodified public `.dat` files in that output's `inputs/`.

### Write from existing material

```bash
uv run simple-ar start --kind writing --goal "Explain my results and limitations" \
  --material ./notes.md --interaction autonomous --yes
```

Repeat `--material` for drafts, notes or result descriptions; `--document` identifies
a separate bibliographic paper. Writing extracts and persists the supplied text, then
uses the shared Writer, Reviewer, assembly and audit capabilities. It does not search,
create innovation candidates, execute experiments or manufacture an empty synthesis.
Supplied-source survey/research tasks requesting only a report likewise use the
existing document-to-report dispatch without a model routing call. This does not
skip article planning or review, and accepted historical routes are not rewritten.
Inputs are Markdown, text, HTML, PDF, ordinary JSON or a completed `table_analysis.v1` analysis package.
Material writing reserves bounded views for explicit abstract sections and selects
other retained passages against the task with the existing lexical retrieval.
Long abstracts may span multiple windows; each window retains its exact chunk
offsets and omission status. This is not whole-document reading or semantic
verification. Unheaded parsed notes remain body text, not inferred abstracts;
missing headings do not mean the document lacks relevant results.
New guided writing tasks using built-in templates enable adaptive article planning:
the saved request and evidence determine a concise title, section responsibilities,
length allocation and placement of registered data figures. Custom templates retain
their topology, and existing TOML tasks/checkpoints are unchanged. Complete section
lengths and recorded assembly-owned data captions, tables and provenance additions
reach writing and review. They share assembly's text builder, without changing saved
drafts; incomplete old package previews remain explicitly unknown. These are planning
aids, not final export word-count certificates or paper-quality guarantees.
For observation analyses, column summaries may use different nonmissing rows,
while paired differences use complete pairs. Writing receives these definitions
alongside the saved counts; combined tables should label each sample basis or
separate the summaries. Arithmetic checks do not certify the interpretation.
Writer task input places these additions in one shared field for joint, section
and format-recovery calls; neighboring prose does not duplicate the attachment preview.
Canonical pre-render text counts also include the title, normalized headings, cited references
and experiment appendix, without duplicating the whole report in every prompt.
Section measurement detail follows the argument plan's metric references, not
the heading's wording or language. Review also receives metrics named by the
current draft; legacy tasks without an argument plan retain the compact overview.
Future renderer output and attachment rechecks still require final artifact inspection.
Draft References are cleaned before protected data/caption additions, not afterwards.
During section verification the preview replaces the adopted version with the current
candidate. Attachments belonging to a not-yet-drafted section remain visible under its
frozen heading, with pending ownership recorded and no invented future prose. Unknown
ownership makes the preview unavailable. Whole-document review uses the same canonical
total; raw section and attachment counts remain components, not competing whole totals.
Whole-document inspection can cover every supplied section. Its correction allowance
uses the frozen argument plan's section set (legacy plans retain two targets),
prioritizing required and severe findings; excess findings remain unresolved.
Joint editing can coordinate current sections literally quoted in a required editing
finding, without copying that opinion to another section. Advisory/verification-only
opinions and absent quotations do not expand targets. Saved correction requests retain
their original targets during recovery; no extra model round is granted.
Saved source briefs use bounded evidence views without
rewriting the original records or removing authorized source backtracking.
Section verification and final delivery are different gates. The candidate guard
rejects a correction that takes a complete, fitting delivery outside its frozen
length range; it does not require each local edit to fix a document that was
already outside the range. Final audit still records such an unmet requirement,
even if the model passes the edited section. Do not infer semantic certification
from `status: passed`; inspect `semantic_review_status`, unresolved findings and
the delivered prose separately. An exhausted editing allowance does not silently
increase on recovery.
Fixed and custom templates supply section requirements, not just headings.
Adaptive built-in planning receives the template's intended use and explicitly
separated writing principles: argument, method explanation, result interpretation
and figure/prose organization. These guide composition, not mandatory sections;
fallback chapter assignments are not another proposed outline. The accepted plan supplies
the writing responsibilities. Material-based writing keeps supplied material visible alongside
papers within the source budget; more references do not replace the task's own results.
For data-backed writing, pass the analysis task's printed `analysis.json` path
to `--material` (optionally alongside notes). The system checks the saved records
against the package-local copied data, freezes that package and regenerates its
native SVGs instead of trusting supplied figure paths or executing scripts.
Malformed packages, missing data, escaping paths and stale values fail before writing.
Writing imports are limited to 20 MiB per JSON/input file; standalone figure
rebuilding retains the original analysis's configured input limit.

The report contains an exact descriptive-value appendix plus a relative-path
`analyses/` directory with copied data, analysis JSON/Markdown and editable SVGs.
Copy the whole report directory to retain those links and rebuildable figures.
`report.figures.enabled = false` or `mode = "off"` omits figures from the report,
not the evidence package; an explicit total figure limit fails instead of dropping figures.
Review copied data for privacy before sharing. Arithmetic rechecking does not
verify collection, semantics, independence, significance or scientific validity;
these are not experiments independently repeated by this session.

The default is a supplied-material report (`material_report`). Add `--template experiment` for a
paper-style draft with explicit evidence gaps. It does not authorize experiments or
guarantee scientific quality. Local source metadata can be incomplete; do not rely on
generated bibliographies without checking them. Template/review files are checked at setup.
Guided material reports, analysis reports and source reviews default to joint drafting
and whole-document review. Long paper and custom templates retain section-based defaults;
explicit TOML settings and saved sessions keep their selected scopes.
The article plan can select registered analysis figures for the main text;
unselected charts, editable exports and numerical records remain in the linked
analysis package. Older plans without figure selections retain whole-package placement.
Expert custom templates must include the corresponding review criteria; use the
research TOML report settings for explicit criteria paths.

Resume the printed session with `research-session --session-root PATH --model env`.
Persisted extraction is reused, including after a writer failure; editing an original
file does not silently replace the saved evidence. This is task-scoped input reuse,
not a general cross-session memory service.

For explicit structured protocols, `start` saves task inputs and delegates to
`research-session`; this adapter is not another planner or lifecycle. Preparation alone makes no model/process calls.
Reading-to-synthesis preserves limitations, open questions, confidence and refs,
labels omitted card rows, and does not silently truncate user execution constraints.
This does not certify semantic understanding.
Report planning, writing, format correction and both review levels use the saved
problem/goal request, not the short memory objective as a substitute for requirements.
Distinct inputs are retained; exact duplicates are supplied once. Historical snapshots
without the original request use their recorded objective. Completed recovery does not
rewrite old prose. Large full-document requests can exceed the existing review window:
that is an explicit incomplete review, not permission to silently drop requirements.
For supplied-source reviews and prepared reproduction, synthesis summarizes
evidence without inventing innovation candidates. An explicitly requested
candidate assessment/design still uses research synthesis. Reading notes remain
model interpretations; report Writer, section Reviewer and document Reviewer receive
the same bounded, identified source passages and source-access status. Truncation
is explicit, and absence from an excerpt is not absence from the paper.
When all reading inputs are explicitly required, automatic reading omits redundant
coarse screening/reranking; retrieved/mixed pools and explicit model screening retain
selection. Basic PDF reading installs pypdf's official font extra for embedded CFF
font decoding, rather than custom glyph rules. This is not OCR, layout reconstruction
or a guarantee that formulas, tables and scientific objects were extracted correctly.
Targeted follow-up questions can access references and appendices within
the same saved source, while initial overview sampling prioritizes the body. Original
section labels accompany these excerpts; a reference entry is not experimental evidence.
Explicit numbered objects (figures, tables, theorems and related statements) route
to their retained definition/caption rather than a prose cross-reference. This is
navigation, not mathematical verification. Native source surveys use the existing
article planner even without a legacy survey contract; `outline_strategy = "template"`
keeps fixed topology, custom templates and frozen checkpoints are not replanned.
Delivery shares its six source windows between exact saved followup hits and
a section-aware overview of note/claim references. Up to two followup slots are
reserved; either pool can borrow unused slots. Selected late windows retain
their original offsets. A saturated followup no longer erases every earlier
method passage, but omitted windows still need a targeted read; this is not
semantic verification or a change to the saved reading note.
Initial overview and task-focused selection use retained line positions within a
document, not body-first ingestion priority. Missing or mixed source locations
preserve within-source order; equal-line ties are not guessed from chunk IDs.
Task-match neighbors stay in the same file and can still contain retention gaps.
Task-selected passages count toward document and section coverage. Remaining
windows first cover unrepresented sections, then spread within each section
instead of spacing across the whole paper. With enough slots this includes
section boundaries; limited slots still omit text and do not certify that every
definition or qualification was read. Bibliography is not reintroduced merely
because substantive passages were already selected.
Selection remains a bounded sample, not a complete reading or claim verification.
Reviewers can locate omitted passages with `search_source_chunks` over the registered
source's saved extracted text, then request neighboring chunks. This is bounded lexical
retrieval, not semantic verification or a fresh download; an unmatched query is not proof
that the original source lacks the information. A passing review that also requests
evidence is provisional: the fetched evidence is saved and the same draft is rechecked
once before acceptance. Still-pending requests become an explicit source-verification
gap, including when backtracking is disabled or exhausted. Mixed evidence/prose corrections
also save completed reads before invoking the Writer. Saved context continues into a resumed
revision and candidate review without rebilling tool use, even when no new read is requested.
Section and document reviews
use the existing tool/revision budgets; recent tool results take priority in bounded prompts,
with omissions recorded. Resume reconstructs consumed tool calls from saved results instead
of resetting the allowance. No-request reviews do not gain an extra model call.
Independent section and document inspection default to original source windows,
not prior reading-card conclusions. Source metadata, truncation and recorded
reading coverage remain visible. Cards are available on explicit request as derived
context; Writer inputs and saved records are unchanged. Omitted text is not absent
source text, and a coverage record does not certify comprehension or paper quality.
Whole-document inspection does not automatically replay earlier roles' synthesis-tool
text as primary evidence. The text remains available through an explicit request,
labelled as a recorded, unverified interpretation; matching source handles retain
their own identity. If interrupted after saving that inspection's requested context,
resume rechecks with the saved results rather than repeating the lookup. Historical
findings still require resolution; context separation does not certify model judgement.
Writer and section Reviewer share a bounded view of adopted section prose and the
frozen section responsibilities. Format recovery retains this view; resume reconstructs
it from saved sections rather than another summary store. Excerpts include head/tail
positions and omitted character counts. They help coherence, not source verification.
Responsibilities are labelled planning intent, not assertions in the current body.
Whole-document inspection omits the earlier drafting-purpose list and instead
checks the original request, current prose, factual/style criteria and evidence.
Saved plans and Writer access remain unchanged; derived length forecasts remain
visible separately from actual canonical counts.
Drafting, format recovery and candidate verification treat reviewer allegations and
proposed remedies as fallible derived context: first check the baseline and sources,
not insert an absent assertion merely to satisfy an opinion. These labels do not
certify semantic correctness, change saved plans or close historical issues.
New adaptive planning can record `document_plan.length_budget` for an
explicit whole-document word request, anchored to a literal task quotation. The
same canonical preview reserves known title/headings, data attachments and experiment
appendix before allocating approximate section-body targets. These are shares, not
minimums; the original request and current correction take precedence. Unselected
references and future visual text remain unresolved costs, not zero. The final
delivery is mechanically checked against a valid frozen length budget using its
actual report text, not the body or forecast. An out-of-range document stays
unresolved; a malformed contract or missing final text requires verification.
No contract means no inferred word gate. Whitespace-token counts do not verify all languages
or word-count conventions. Pages, characters and excluded-body scopes are not
silently converted. No extra planning call is added, and saved frozen plans remain
unchanged on recovery. A located quotation validates the anchor, not its interpretation.
Candidate verification uses the same check: if a complete, previously fitting
delivery goes out of range after a section correction, a model `pass` cannot
adopt that candidate. Correction and recovery retain the existing allowance.
Incomplete previews and already-out-of-range originals are not assigned a guessed
section-local fix; final audit still checks the actual assembled report. This
prevents that regression, not all length failures or semantic errors.
Survey planning receives the same complete original task. Its proposed 2–12
usable sections do not gain keyword-inserted coverage chapters or filler
subsections, with or without a word request. Generic titles alone are not grounds
for rejecting an outline. Broad-survey coverage guidance and source routing
remain; custom and frozen plans are unchanged. Preserving a proposal is not a
guarantee that it answers the task or has adequate scientific coverage.
The claim view follows current adopted drafts, preserving separate declarations even
when two sections reuse a claim id; rejected revisions do not replace current prose.
Revision verification receives the original findings, requested changes and a bounded
view of the previous draft, alongside source evidence. Removing unsupported or repeated
prose is allowed; preserving supported facts and qualifications matters more than preserving
word count. Checkpoints retain correction requests and editor candidates: resume reuses
them and consumed correction/source-call allowances rather than generating a fresh allowance.
Normal drafting and format recovery use the same effective correction instructions:
explicit requests plus required proposed changes. Legacy overall revision requests
remain actionable; optional or verification-only suggestions are not silently turned
into rewrites, and their original opinions remain visible. The section edit scope
names the body, display heading and supported metadata eligible for this call, and the read-only plan, data, other drafts and
assembly text. An unavailable owner correction stays unresolved, not a pretend Writer
edit. Verification observes baseline/candidate section token counts and their delta;
these derived observations can support a style check but not measured-fact counter-evidence.
Growth may be necessary for a supported correction; status alone proves nothing.
These checks are model-assisted, not certification of every important claim or paper quality.
Live single-section responses reject a different explicit section ID through the existing
one-format-correction path. Absent, null and legacy empty IDs bind to the requested section;
saved history is not migrated and no new edit target is opened.
Historical-opinion checking is not another discovery or writing role: it omits
drafting criteria/section-purpose directives, retaining the full task, current prose
and evidence. New findings or rewrite instructions from that role are rejected;
the existing one-format correction and saved unresolved opinions remain intact.
Independent inspection still receives its criteria and owns discovery.
An invalid batch stays rejected. If its complete target/identity/role envelope is
unambiguous, each finding/check is separately subjected to the same strict evidence
validation. After the existing one format correction, the saved valid subset may
continue within the original editing allowance. Invalid records and loose rewrite
instructions from a mixed invalid section never enter the Writer or close opinions.
The raw answer and incomplete-review finding remain; resuming its saved subset does
not resend a consumed correction. A usable subset is not a completed full inspection.
Current limitations and open questions follow the frozen input and adopted drafts,
not an append-only union of every revision. The existing checkpoint owner refreshes
this view before resumed prompts and immediately after adoption. Historical draft
metadata identifies superseded/pending/rejected notes to withdraw; original input
constraints and legacy notes without a traceable owner remain. Draft history stays
unchanged. These notes are not independently verified source facts.
After a built-in adaptive outline succeeds, its frozen document plan owns section
structure for both surveys and experiment reports. Default drafting order is no
longer a second plan; independent review retains factual checks but omits the
explicit `Default Structure` fallback block. Custom criteria and frozen legacy
criteria remain authoritative and are not rewritten during recovery. A project-local
`templates/report` directory still overrides installed template defaults; use a
directory without that override when checking a wheel's packaged defaults.
Before drafting, unsent corrections targeting the same section combine the independent
inspection's requirements and checked unresolved opinions. One candidate is verified against
that combined contract; saved candidates retain their original contract on recovery.
New findings about a saved candidate's target stay in the current unresolved list
and inspection history instead of overwriting the contract or silently disappearing.
This does not increase section/target allowances or close omitted historical opinions.

New required factual findings carry literal quotations from the current draft.
Definite metric mismatches also identify non-derived counter-evidence; an unavailable
observation should remain a verification/gap request rather than a fabricated mismatch.
`evidence_quotes` can select a listed field with `anchor: "executor_record:0"`
(an exact key shown beside its path in this request's `evidence_locator.pointers_by_role`,
not an index the model must count). Retained requests with the old zero-based arrays
remain readable; missing keys are never guessed or shifted.
Omitting `quote` selects the entire field without model transcription. The shared
default response example uses that anchor-only shape; direct Pointer selection and
strict literal quotations remain optional compatible paths. The controller saves
the actual Pointer, raw value and role with `mode: "field_reference"`. No short
alias is persisted or rebound on recovery. This is not a model quotation or semantic
certificate. Optional supplied Pointer/role must agree; an optional quote remains
strictly checked. A direct JSON Pointer without `quote` can select any eligible
scalar actually supplied in the current request, including fields omitted from the
bounded locator; it uses the same raw-value/ownership persistence, not another index.
If a literal quote is supplied, its role and text remain strictly checked: an invalid
quotation is never converted into a field reference. Old records keep their shape.
The reviewer cannot relabel a declared setting as an executor
observation or cite a prior opinion as source evidence. Section backtracking results
are part of the same addressable request. Legacy opinions remain readable without
invented anchors; supplied new evidence anchors are checked. These checks establish
location and ownership, not semantic support or scientific correctness. A bad location
receives bounded parent-structure feedback, not an automatically repaired reference.
Drafting and recovery also distinguish prepared conditions from execution observations.

Within a single recorded source passage (`evidence_passages`, `chunks` or
`source_front_matter` text), ASCII layout whitespace can differ, for example a PDF
line break rendered as a space. Words, numbers, punctuation, case and hyphenation
remain unchanged; separate fields/windows are never stitched. Other scalar fields,
including file paths and identities, retain literal matching. The original passage
and response are preserved. This handles presentation, not semantic verification.

Review findings can record `required_action`: `advisory` leaves an optional
suggestion, `revise` requires a bounded correction, and `verify` requires source
checking or qualification/removal of the unsupported assertion. Impact severity
is separate from this action; minor necessary work is not automatically ignored.
A verification-only request can use the existing source tools and rejudge the
same draft before rewriting. Mixed correction/verification uses the ordinary
fetch→revise→check path. No experiment is authorized by these findings, and
recovery does not refill tool or correction allowances. Unresolved required work
keeps audit at warning or worse. Old findings without this field retain their
severity/type policy; explicit advice cannot demote existing factual safeguards.
Agent-result/checkpoint `reviewer_findings` retains historical observations,
including provisional and resolved issues; current unresolved findings belong
to `memory.reviewer_findings`. Inspect the associated review/tool events before
treating a historical finding as a remaining defect.
Independent document inspection does not receive old reviewer opinions as source facts.
Historical checking describes its response shape once; exact opinion identities and
targets are listed in the check request, not repeated with full quotation schemas.
JSON transport handling preserves literal unknown string escapes without a new
provider request. Valid JSON escapes keep their existing meaning; damaged Unicode,
structure or control characters are not reconstructed. A nested object cannot stand
in for a damaged outer response. Domain identity, quotation and value checks still
apply after decoding, and decoding success is not content verification.
It does not also ask for new discovery or revision instructions. All current
sections remain visible as quotation evidence. Invalid extra checks are still rejected.
Distinct opinions sharing a model ID get request-local check handles; their original
IDs remain metadata. Closure maps to the exact requested record, not every opinion
with that original ID. Recovery reconstructs handles from the saved request order.
Only fully identical repeated records are deduplicated; changed action, severity,
evidence or identity remains separate. These controls do not certify semantic judgement.
The prior proposed remedy, severity and required action are omitted from this checker
projection; the editor retains them in unchanged original history and correction
contracts. Those priorities do not establish the allegation. Cold checking retains original
passages, bibliography and reading coverage, while earlier model reading cards are
available only on explicit request. Writer inputs and saved source/tool records stay
unchanged; a missing passage still requires backtracking or an explicit gap.
A separate check can resolve an eligible historical opinion only with a reason and
exact quotations with their current section identity. Cross-section evidence can
identify another supplied section, but the opinion remains under its original
target; legacy string quotes match only that target. Omitted or invalid checks retain
the issue; controller-owned failures remain outside model closure. Parsed answers
rejected by schema/quotation validation are saved with their error in the existing
iteration trace, not accepted as findings. A correction closes only its original
contract's findings; other issues in the same section remain active. These records
support diagnosis and recovery, not independent scientific verification.
Whole-document inspection and opinion-check source lookups share a checkpointed
read helper with rejected-answer lookups. Before execution, the existing event
stores an allocated blocked result with no source content; success replaces it.
Restoration counts either result and does not replay the read. An allocated but
unconfirmed result is not evidence and can leave verification pending. This does
not grant another provider request or guarantee complete scientific validation.
Section and document review requests include bounded `evidence_locator` navigation to
current scalar fields, grouped by record ownership. Copy an applicable full path and
quote its actual field; the locator itself is not evidence. Original passage paths
precede metadata, and the listing reports omitted paths. Unlisted visible fields remain
valid targets; omission is not proof of missing source information. Navigation copies
no source text and does not change role/quotation validation. Near the whole-document
window limit, it is reduced or omitted instead of dropping existing evidence.
An unresolved verification-only opinion can remain pending without a prose rewrite.
Its evidence requests are handled by the opinion checker; outstanding requests
prevent closure even if the model proposes closure. This does not certify that the
opinion is valid or invalid. Explicit bounded corrections still run, and a separate
correction cannot close that pending verification. Current-assertion source guards
remain unchanged; unresolved required work still affects audit.
One format correction is allowed after a parsed document-review answer fails
validation. It keeps exact opinion targets, quotation checks and the bounded evidence contract;
the rejected answer is not source evidence, and its unsupported judgement need not
be preserved. A started correction consumes this
allowance even if its result is unknown; a completed correction reuses its saved
validated response. Saved context resumes under its original inspection, opinion
check or final-review owner. Transport, decoding and budget failures do not permit
this format correction, and the existing evidence-window limit still applies.
An invalid opinion does not prevent a separately valid read request: all response targets
must be unique and allowed for this role, and only registered read-only tools are eligible.
The gateway still validates arguments, registered sources and cumulative call limits.
Rejected findings/instructions are not adopted; newly fetched material is passed to the
original single correction, not a new repair cycle. Reads are allocated before execution
and checkpointed after completion. Interrupted reads remain explicitly unconfirmed and
are not repeated on resume. Retrieval alone does not verify a scientific judgement.
Controller failures keep separate operation identities even with identical wording;
only actual success of their owning operation clears a stale service failure.
Whole-document editing can select the frozen argument plan's section set;
legacy plans retain at most two targets. Each target uses
the existing `max_review_iterations` allowance, including rejected candidates.
Saved iterations preserve consumed allowance and original findings/instructions;
resuming cannot expand the frozen legacy target contract or reset correction attempts.
Explicit document drafting instead counts joint candidate rounds under the same
  limit and preserves previously consumed legacy rounds and pending contracts.
  A joint Writer can ask the registered tools for one retained-source batch before
  composing or correcting, when the overview lacks a needed condition or passage.
  These are the same read-only tools and limits used by review, not a new reading
  pipeline. Unavailable or unmatched reads remain unknown, not proof of absence.
Known publication dates, DOI and author-list coverage notes survive document handoff
and share one metadata projection for writing, references, BibTeX and `citation_map.json`.
Missing details are displayed rather than inferred. Provider metadata is not independent
identity/version verification: same-title records and complete-looking fields can still be wrong.
The shared projection flags impossible ISO dates and explicit DOI/arXiv locator or
version conflicts without choosing a record by title or earliest year. Known DOI
wrappers are normalized, not resolved online; raw source records remain unchanged.
Audit warns for cited conflicts. Missing metadata and unused conflicting sources
do not by themselves block delivery, and consistent fields remain unverified.
Extracted text before the first recognized heading is retained as front matter;
Bounded chunk budgets prioritize body evidence and may leave front matter out of indexed chunks.
Model reading may propose missing local citation fields from an explicit bounded
front-matter view in the existing reading call; it does not replace recorded metadata.
Adaptive material-writing planning uses the same saved view in its existing
outline call, without requiring a separate reading stage. Accepted source-matched
proposals stay in the existing outline checkpoint and are projected consistently
into drafting, assembly, citation maps and BibTeX on recovery. Custom fixed outlines
and old plans without proposals do not acquire new metadata automatically.
Filename-only title placeholders are requested as missing titles. Exact short
byline subsets may be retained with incomplete coverage; erroneous full-list
transcriptions are not accepted through fuzzy matching. Publication dates in
ISO or complete English written-month forms are calendar-checked while the
recorded text remains unchanged; ambiguous numeric dates and guessed years are
not accepted. This checks the given field's format, not whether a date belongs
to publication rather than submission or to the correct version.
Local filename titles are labelled as placeholders, not established publication titles.
`get_paper_brief` returns recorded metadata alongside saved front matter, even if
the indexed chunk cap omitted that header. It does not open live file paths or
guess missing headers in old saved bundles; truncation and missing text stay explicit.
For a local source, missing title/byline/date/DOI/public URL can be filled in the
report projection only with the same source's saved front-matter locator and matching
quotation. A title can replace only the filename default. Original records and
provider/user metadata are unchanged; incomplete bylines are explicit. Citation maps,
Markdown and BibTeX share the projection. There is no network lookup, guessing from
filenames or independent identity/version validation. Unread or unavailable fields
remain unknown. Older notes keep their narrower source-matched title behavior.
Front matter is a parser label, not a guarantee that the text contains only a
byline. When no explicit abstract was identified, that opening text remains a
candidate for task-related passage selection. It is not relabelled as an abstract
or treated as exhaustive reading.
Sectioning keeps numbered publication titles inside bibliography rather than
opening new body sections. Explicit Markdown headings and appendices can reopen
the body; trailing numeric table cells are not implicit headings. A descriptive
body heading mentioning references is not itself a bibliography. These are
text-structure heuristics, not PDF layout or table reconstruction.
Reading notes receive the user task focus separately from source evidence. Default excerpts
retain an ingest-sized chunk; smaller windows mark further clipping. Note identities and
declared references are checked against their owning document. This prevents misattribution,
not semantic errors, and does not turn a bounded overview into full-text verification.
Report inputs also retain the original task and the recorded source-access mix
(parsed text versus metadata/abstract only). An explicit report citation cap
does not truncate search or reading candidates; it is checked on the final body. Unresolved major
factual reviewer findings or a violated explicit cap fail the report audit;
the failed audit remains inspectable and delivery pauses. Style warnings do
not become scientific failures, and a passing mechanical audit is not a
certificate of semantic support.

Report diagrams require labels actually present in the section: no generic
filler or inferred arrows. Paired figures keep input metric order (at most four
by default), without favoring continual-learning metrics. They share the headless
Matplotlib renderer and deliver SVG, PDF and PNG. When the accepted argument plan
assigns a recorded metric to exactly one drafted section, its plot appears there,
independent of the heading's language. Otherwise it remains in the comparison
appendix. Lines connect recorded pairs, not confidence intervals; rendering is
not scientific validation.

For structured research, the entrypoint is `research-session`. It owns attempts, artifacts,
reports and audits in one session, while `ResearchApplication` selects only the
capabilities justified by the task, supplied assets and accepted execution
constraints. There is no user-facing fixed stage sequence.

```text
task + assets + constraints
  -> short accepted plan
  -> capability execution and observed artifact
  -> application decision: next action, revision, delivery, or stop
  -> explicit recovery/reload from persisted refs without repeating valid work
```

The application owns plan acceptance, capability order, comparison decisions and
delivery choices. Each capability owns its input contract and attempt output;
`SessionController` owns attempt, budget, lineage and artifact persistence. A
resume reads those facts and reconstructs the next accepted action; it does not
replay completed side effects or let the core invent a domain-specific stage.

Public entrypoints remain intentionally small:

```text
simple-ar start                  # ordinary native goal-driven tasks
simple-ar start --resume-task PATH # native feedback and recovery
simple-ar research-session       # exact structured protocols and saved-session recovery
simple-ar research-brief         # compatibility request/result adapter
simple-ar status RUN_DIR         # read-only archive display
```

## Capability Runs

Alongside the task-driven entrypoint, `simple_ar.core` provides an opt-in boundary
for new replaceable capabilities. A capability receives declared input
references through `CapabilityContext`, writes outputs through an
attempt-scoped `ArtifactStore`, and returns a `CapabilityResult`. The
`SessionController` can persist a bounded attempt and its decision without
turning the application into an unrestricted task graph.

This boundary is compositional: it does not schedule arbitrary actions or
change the artifact paths expected by existing commands and adapters. The
offline reference package in `tests/fixtures/capability_package_minimal/` shows the
smallest supported handoff; domain-specific schemas belong to the capability,
not to the shared core.

An optional lifecycle profile narrows the capabilities that a session may
execute. The built-in scopes are `research_brief`, `survey`, `experiment`,
`paper_audit`, and `full_research`; they are allow-lists rather than automatic
workflow runners. Unknown profile names remain compatible with older callers.
For a new named-profile session without an explicit budget, the controller
allocates one attempt per named capability plus two bounded recovery attempts;
callers can override this with `BudgetState`, while loaded legacy manifests
retain their persisted budget.

For a caller-owned multi-capability handoff, the application layer should call
`SessionController.execute_attempt()` for each explicit capability in the chosen
sequence. It persists execution facts; the application decides whether to stop,
continue or complete the research. A resumed process should inspect status and
attempt lineage, and explicitly construct the next call; the core does not
silently rerun an interrupted attempt or choose a domain-specific best result.
When an interruption has been manually confirmed, the caller can use
`SessionController.recover_interrupted()` to close the stale running attempt as
an explicit failure before constructing a retry or repair attempt. The method
does not retry or overwrite an existing result envelope.
The controller rejects any new attempt while a prior attempt is still marked
`running`, so recovery cannot accidentally create a second active branch.
Core validates capability scope, budget and input artifacts before creating a
new attempt. Research sequencing belongs to the application, not a second
fixed transition recipe in the execution boundary.

Use `SessionController.attempt_output_refs()` to pass declared outputs from a
completed or failed attempt to a later capability. Attempt-local paths are
converted to session-root references without copying or merging artifacts;
the caller still decides which attempt and which output to use.
When an alternative should start from an earlier completed or failed attempt,
pass its ID as `parent_attempt_id` to `SessionController.execute_attempt()`. The
controller validates and records that parent. Without this argument, attempts continue from the
current attempt as before; the controller does not infer branches or select a
winner. Use `attempt_lineage()` to inspect the root-to-node chain for a
comparison or recovery view; it reads only persisted attempt manifests and
does not merge artifacts or schedule work.

Historical research-session readers expose the stored `next_capability` and
execution evidence. They do not recompute a next-step recommendation with a
retired policy; structured research decisions belong to ResearchApplication.

For a library caller that wants one in-memory value, use
`research.brief.build_research_brief()` is an in-memory compatibility view only.
The default registry deliberately does not expose a composite `research_brief`
capability: sessions persist `read` and `synthesize` separately. Historical
`research_brief.v1` handoffs remain readable, but they are not a second
executable lifecycle.

`simple-ar research-brief` is a compatibility request/result adapter over the
same `ResearchApplication` used by `research-session`, not another orchestrator.
It requests a literature summary and uses the canonical lifecycle and default
request/token budgets. Its research capabilities are:

```text
plan -> search -> document_ingest -> read -> synthesize
```

For an online topic, the command uses the configured built-in source providers:

```bash
uv run simple-ar research-brief --topic "reliable agents"
```

For a reproducible local run, provide one or more Markdown/text documents:

```bash
uv run simple-ar research-brief --topic "reliable agents" \
  --local-document tests/fixtures/research/reliable_agents.md \
  --output-root runs/research-brief
```

The command creates a timestamped v2 session directory. Each handoff remains in
its own dynamically named attempt. Read `session_manifest.json.state_refs` for
the actual paths; use the CLI's printed `Synthesis handoff` path for downstream
commands instead of constructing an attempt ID. The canonical outputs are `research_plan.json`,
`search_result.json`, `document_bundle.json`, `read_result.json`, and
`synthesis_result.json`; capability results and attempt manifests record their
status and lineage. The command does not silently retry or overwrite a prior
attempt. `--query`, `--provider`, `--max-results`, `--max-chunks`, and
`--idea-limit` are the deliberately small controls for this path; more complex
policies remain application-owned. The aggregate `research_brief.v1` format
is still accepted as an input for older callers.

The standalone path is explicit about model use. Without `--model` it is an
offline/deterministic composition: search, parsing, card derivation, and
structured direction extraction use the supplied inputs. With `--model NAME`,
the existing LLM client is used for research planning, bounded Read screening/
reranking and paper notes, and synthesis; the handoff records the Read
provenance plus `planner: llm` and `generation_mode: llm`. Missing credentials,
transport failures, or malformed model output fail the relevant attempt; they
do not silently become a model-generated result.

The segmented `research-experiment` creator is retired. Experiment and analysis
capabilities remain shared by the formal application, without a separate session lifecycle.

For a structured session that owns both sides of this handoff, use
`simple-ar research-session`. It reuses the same explicit
`plan -> search -> document_ingest -> read -> synthesize` prefix, records a
`research_design.v1` handoff, and continues with one explicit `ExperimentRequest`
and the existing Analysis capability. By default the command is still supplied
by the caller. Passing `--code-task-config` selects the existing project-style
Code-Task backend for that experiment attempt instead: its project, benchmark,
workspace, baseline, and execution settings remain owned by the TOML, and its
output is normalized into the same canonical result. This is a controlled
composition rather than an unrestricted research loop.

When neither an execution command nor `--code-task-config` is supplied, the same
entrypoint remains a literature-only composition. It ends at the evidence-backed
summary without creating an execution request; with a model, it can continue to
the research-only report path. This is the supported no-experiment form, not an
implicit placeholder experiment.

If that session ends with a failed experiment but retains its design and
analysis handoffs, one explicit recovery can reuse the same literature and
design without rebuilding them:

```bash
uv run simple-ar research-session-continue \
  --session-root runs/research-session/<session> \
  --cwd tests/fixtures/research \
  --primary-metric accuracy \
  --metric-direction accuracy=higher \
  --command python -c "print('accuracy: 0.90')"
```

For a canonical `session_manifest.v2`, this creates a new dynamically named
experiment attempt whose parent is the failed candidate, then reuses the
existing literature and design for deterministic analysis. The revised command
is supplied by the caller; search and design are not repeated. The original
attempt remains intact. This boundary accepts only a simple explicit technical
failure; paired, prepared-data, and CodeTask sessions keep their dedicated
bounded recovery paths. A scientific negative result is evidence and is not
silently retried. Legacy `session_manifest.v1` sessions retain the fixed
`experiment-002`/`analysis-002` compatibility behavior.

The canonical session can continue through `research-report` after `--no-report`.
It reuses persisted evidence and measurements, adding only missing report actions.
Writer execution and checkpoints belong to `report/writing.py`; assembly and audit
remain separate canonical capabilities in the same application lifecycle.

Writing and both review levels distinguish declared protocol, recorded execution,
and independent implementation checks. Where the registered document bundle is
available, reviewers can request bounded passages around a cited chunk; cached
excerpts alone are not reported as fresh source reading. Revision traces retain
candidate text and whole-document adoption decisions even when verification fails.
Inspect unresolved findings before treating a generated report as a checked paper.

With explicit `report.draft_scope = "document"`, cross-section correction uses
one saved candidate and checks the complete candidate before adopting the changed
sections together. Rejected corrections leave the original manuscript intact.
The same trace retains correction requests, completed checks and consumed rounds;
default section editing and pending legacy candidates keep their previous path.

An assembled report can be exported independently with `report-export`:
the canonical citation-key body and bibliography become an editable ACM
manuscript. Export reuses the saved text and figures; it does not repeat reading,
writing or measurements. Rendering/compilation status is separate from report
audit status. See the [command reference](CLI_REFERENCE.md#simple-ar-report-export).

The pdfLaTeX demonstration handles common scientific Greek/math Unicode glyphs
through fixed export-owned declarations, without changing the original Markdown.
This is not general multilingual font support. Unsupported characters or missing
TeX packages retain the editable project and compilation diagnostics rather than
silently changing the content or claiming PDF delivery.

Slash-separated prose receives export-owned break opportunities without changing
its font or values; math and URL targets are not rewritten. Compilation alone
does not check page layout or fonts. Incomplete bibliographic metadata can produce
nested ACM/natbib labels: the built-in compiler protects generated labels without
inventing authors/dates; manual-build instructions in the exported README explain
the corresponding limitation. Retain missing-metadata and font warnings.

Historical sessions remain inspectable, but no second Writer/report/audit executor
resumes them. `build_research_session_report_inputs()` and
`build_code_task_report_inputs()` only project existing evidence without executing
or modifying the session. The segmented `research-code-task` creator is retired;
use `research-session --code-task-config` for complete tasks.

Applications that want the built-in adapters can use
`research.register_research_capabilities(registry, names=...)`. The `names`
argument is optional for the complete adapter set or can select only the
capabilities needed by a path; registration is still explicit and does not
create a scheduler. The helper covers deterministic research planning,
Search, Document Ingest, Read, Synthesis, Research Design, Experiment, Analysis, Report,
Report Audit, and Research Brief.
`analysis` is the canonical standalone result-analysis name; `analyze` remains
available as a legacy registry/session alias when explicitly selected.

The `plan` adapter reuses the existing question, query, and source-budget
builders and writes one `research_plan.v1` handoff. It is deterministic by
default; a caller can explicitly pass `use_llm=True` and the shared client to
obtain a normalized model-assisted plan. It does not choose the next
capability. The narrow `research_design` adapter consumes a persisted synthesis,
selects an explicit idea by default, or selects among the persisted candidates
when the caller explicitly enables its shared LLM client, and writes a
`research_design.v1` handoff containing the already-derived
`ResearchExperimentContract`. Without an explicit idea id, deterministic mode
considers candidates in shared evidence/execution-readiness order, preserving
input order for ties. It checks whether the selected contract is minimally
executable, but it does not invent a command, metric value, experiment matrix,
code, or execution plan. Domain-specific code generation and execution
implementations remain caller-owned.
`research.planning.search_request_from_plan()` is the small in-memory adapter
for passing that plan to the existing `SearchRequest`; it does not invoke a
provider or add retry, deduplication, or selection policy.

The individual evidence steps are also available when a caller already owns
their inputs: register `research.evidence.reader.run_read_capability()` for a
`DocumentBundle`, or `research.synthesis.run_synthesis_capability()` for an
expanded evidence pack. They write one `read_result.json` or
`synthesis_result.json` handoff respectively and leave document fetching,
LLM policy, and transition decisions to the caller.

For a session that starts at retrieval, register
`research.sources.run_search_capability()` explicitly. It writes one
attempt-local `search_result.json` containing normalized paper rows and the
provider/query response statuses. It is a handoff, not a replacement for the
legacy Search projection or its candidate-selection policy.

When a caller wants to continue directly from evidence synthesis into the
standalone execution adapter, the bounded recipe permits
`synthesize -> experiment -> analysis`. The caller still supplies the
`ExperimentRequest`, execution backend, and next-step decision; no design or
repair policy is inferred by the core.
When the request comes from a persisted `synthesis_result.v1`,
`research.experiment_request_from_synthesis()` transfers its existing
research-level experiment contract. The caller still supplies the
`RunRequest`, result schema, and execution decision; the helper does not
approve `needs_review` or implicitly execute, retry, or choose the next stage.

When the next step owns full-text access, register
`research.documents.run_document_ingest_capability()` with a
`DocumentIngestRequest`. It writes one restorable `document_bundle.json`
containing document records, sections, chunks, and extraction status. A later
Read attempt can load that declared artifact with
`DocumentBundle.from_handoff_dict()`; ingest itself does not select papers or
call an LLM.

After a Read attempt, `ReadResult.from_handoff_dict(payload, bundle=bundle)`
restores the typed cards while requiring the original document bundle
explicitly; source chunk text is therefore not copied into the Read artifact.
`research.brief.evidence_pack_from_read()` is the small adapter for passing
those cards to Synthesis when a caller composes the two capabilities directly.
Read generation and restoration also validate each declared `evidence_refs`
against the bundle's chunk IDs. Unresolved references remain visible as
diagnostics and downgrade the result to `partial`; the check does not scan
other files or block metadata-only compatibility reads.

The execution slice follows the same rule: register
`research.experiment.run_experiment_capability()` explicitly when a session
needs to run a `RunRequest`. It exposes the existing canonical result as
`results.json` and declares the captured streams as
`execution/stdout.txt` and `execution/stderr.txt` in the same attempt; register
`research.analysis.analyze_experiment_capability()` for the separate analysis
step. Failed or timed-out execution is never converted into a successful
capability, and its diagnostic streams remain available to later capabilities.
Missing analysis evidence is reported as `partial`; only a `passed` analysis
is exposed as `completed`. Persisted `analysis_handoff.v1` data can be restored
with `AnalysisHandoff.from_handoff_dict()` without rerunning or copying the
execution artifact.
For two completed result mappings, `research.analysis.compare_experiment_results()`
provides a small status-and-metric comparison that can be passed as
`ExperimentRequest.comparisons`; unknown directions and missing evidence remain
`inconclusive`, and the caller still owns any follow-up decision.
The resulting `AnalysisResult` also exposes a conservative evidence status
(`passed`, `failed`, `blocked`, `incomplete`, or `metric_below_target`).
It requires an explicit execution handoff and never schedules a retry or
transition; persisted standalone analyses additionally write
`analysis_status.json`.
The application consumes the analysis evidence directly and owns the next action.

For an already assembled report, register
`report.audit.run_report_audit_capability()` when a session needs a standalone
audit. The caller passes explicit report artifact references and typed report
state; the adapter writes the existing `report_audit.json` shape and reports
warnings as partial rather than silently treating them as a clean pass.

When a caller owns completed section drafts, register
`report.capability.run_report_capability()` first. It assembles those drafts
with the existing report assembler, optional heading numbering, and optional
planned figure renderer, producing an attempt-local `report.md`. Generated
figures are declared as the same attempt's `figure` outputs, while the figure
manifest remains an index; a missing renderer output is reported as `partial`.
It does not write an audit or invoke the writer; pass the declared report
reference to the separate audit capability.

### 1. Research Report (Literature-First, Segmented/Advanced)

Use this when you want a literature review, survey, or DeepResearch-like report without emphasizing experiments.
For an ordinary complete research task, use `start`; this section describes a reusable
capability boundary.

Conceptual flow:

```text
plan -> search -> read -> synthesize -> report
```

In the literature-first structured path, without an execution command or CodeTask
configuration, `research-session` uses the literature-only path and does not start an experiment. Reports belong
to the same lifecycle; `--no-report` explicitly omits that delivery.

### 2. Code Task (Existing Codebase)

Use this when you already have code and want a focused modification, optimization, repair, or benchmark improvement.

Conceptual flow:

```text
init workspace -> index code -> map repo -> probe environment
-> apply baseline policy -> build context pack
-> plan patch -> approve -> propose edits -> apply edits
-> review changes -> validate -> run patched benchmark -> post-run review
-> compare results
-> analyze failure -> repair proposal
```

Key boundaries:

- Ordinary `execute` uses one patch plan. Decomposition is opt-in through
  `execute --to-step work-plan` / `--to-step batch` or an existing work plan.
  Interactive execution follows the same rule. Explicit batch workflows retain
  their scope, approval, completion and repair history.
- The source project is prepared under `code_task/workspace`; existing-project
  runs default to `auto`, which prefers a detached `git_worktree` for committed
  Git projects and falls back to a guarded `copy` with recorded next-step hints.
  For monorepos, the worktree is created at the repository root and the matching
  project subdirectory becomes the editable project root. Experimental
  `sparse_copy` copies only configured include patterns and always excludes
  data/model/cache/secret-like paths. The original code is never modified.
- Patch application is gated by an explicit human approval step.
- Edit proposals are conservative old/new replacements, not free-form rewrites.
- Controlled patch proposals and application call their implementation directly.
  The duplicate editor-adapter layer has been retired; artifact metadata retains
  `controlled_patch` for provenance. External Harness integration is later work.
- Multiple ordered edits may target one file, but every `old` block must remain
  uniquely matchable; invalid proposals stop before workspace files are written.
- `code-task execute` can run the next safe steps, but it stops at plan approval
  and proposal review unless the user explicitly continues.
- Work-plan items are meant to be executable implementation batches. The
  executor skips obvious analysis-only items when choosing the first active
  batch, so an LLM-generated "inspect the project" item does not constrain the
  edit stage by accident.
- When several reviewed work-plan items form a small serial dependency chain
  that must land together, such as feature producer, model consumer, and config
  switch, the active batch may merge them. The separate plan remains visible,
  while `batch_state.json.work_item.source_work_item_ids` and `target_files`
  show the bounded execution scope used by the edit proposal.
- A benchmark-passing repair is not automatically a task success. The
  before/after verdict comes from `code_task/run/comparison.json`; if patched
  metrics remain below baseline, the system has recovered execution but has not
  achieved an improvement objective yet.
- Baseline execution is a policy, not an unconditional cost. `auto`/`run`
  records unchanged metrics, `skip`/`none` continues without comparison, and
  `provided` stores user-supplied metrics with an explicit provenance marker.
- Current execution uses workspace isolation plus an explicit interpreter
  policy. It supports `current` and `external`; managed environment creation is
  planned later. `workspace.reuse_source_venv` can point a worktree/copy/sparse
  run at an existing source `.venv` Python without installing dependencies.

Bundled examples:

- `scripts/research_session_smoke.py`: canonical research workflow smoke;
  literature-only reports use the same application, not an old pipeline config.
- `examples/code_task_medium_review/`: standalone code-task workflow over a
  multi-module review classifier with a `main.py` entrypoint, JSON config,
  visible progress output, and a task that naturally touches feature extraction,
  model scoring, and configuration.
- `examples/code_task_digits_mlp/`: standalone coding on a lightweight NumPy
  MLP benchmark, useful for real local CPU measurements without GPU.

### 3. Research With Experiment

Use `research-session` for a research task with an explicit execution command or
`--code-task-config`. The application owns the research lifecycle; CodeTask
implements changes in the prepared workspace, while the experiment capability
owns measurements.

Conceptual flow:

```text
plan -> search -> document ingest -> read -> synthesize -> design
-> prepare/implement when requested -> experiment -> analysis -> report -> audit
```

- Research inputs and constraints are passed to CodeTask through the research
  handoff. Ordinary modification uses one approved patch plan; explicit or
  existing work plans retain their batch workflow.
- Implementation produces frozen patch, validation, review and plan evidence.
  These checks do not themselves establish a scientific improvement.
- CodeTask preparation is task-bound, not only a source copy. Goal, constraint
  or editing-contract revisions require a fresh prepared run; old plans and
  evidence remain available and the same session budget continues. Preferences
  alone do not require another prepared workspace.
  Saved task configuration keeps the declared source/execution settings;
  prepared directories remain in their own artifact and are overlaid when run.
  Revision invalidation and checkpoint consent compare the task declaration,
  not a configuration flattened into an earlier workspace.
- Experiment results and comparisons keep their execution references. A failed
  process is not a valid measurement; a valid negative result is not an
  instruction to repair forever.
- Report generation uses the recorded literature, implementation and measurement
  evidence. Literal visibility checks look for the registered metric name and
  value in the same prose line or table row. Human-readable aliases, row/column
  relationships and rounding are not inferred: an unmatched value requires
  verification, not an automatic omission claim or rewrite. The unresolved list
  remains. Framework-rendered measurement tables are checked against persisted
  results; altered baseline/candidate cells fail the audit.
  This is a consistency check, not independent validation of the measurements.
  Mechanical audit success is not proof of publication quality or semantic
  correctness (`semantic_review_status` remains `semantic_unchecked`).
- Terminal delivery shows the latest measured candidate's method-evidence status
  separately from code validation and report audit. Paired runs cross-check each
  candidate measurement against the collection's implementation reference;
  mixed or incomplete lineage is shown as unavailable. `not independently checked`
  is an evidence limit, not a failed experiment or an implicit method approval.
- Resuming report work must not rerun experiments that have completed. See the
  session commands above and `scripts/research_session_smoke.py` for the entry
  point; the offline smoke is a fixture, not real scientific validation.

## Historical Eight-Stage Artifacts

The old eight-stage executor has been removed. `run`, `resume`,
`research-code-task` and `research-experiment` reject execution and point to
`research-session`; old template flags do not select a supported workflow.

Existing stage-shaped archives are not deleted or rewritten. `status RUN_DIR`
can display their recorded manifest and pipeline state. The private
`_legacy.documents.load_search_document_bundle(search_dir)` reads an archived
Search directory without creating a runtime context. Historical artifact names
describe stored evidence, not a second executable pipeline.

## Search And LLM Boundaries

Search retrieves records and records provenance. Document ingest handles local
or permitted remote text; Read selects and analyzes evidence, Synthesis combines
it, and Design proposes an experiment. Missing full text remains an explicit
limitation, not a claim that the paper was read in full.

Paper notes use a bounded combination of section overview and lexical matches
to the task, searched across retained substantive chunks. Matching excerpts
include nearby same-source context within the existing excerpt budget. No
embedding index, extra model round or task-specific paper rule is required.
Each new note records `reading_coverage`: available chunk count, shown chunk
IDs and an explicit `semantic_verification = not_performed` marker. A match
helps locate evidence; it does not prove a claim or establish that no contrary
passage exists. Unmatched or differently worded questions still need targeted
source reading, and source acquisition/extraction failures remain limitations.

Model notes may request up to two specific followup queries. The reader searches
only that source's retained text, with one round and at most six bounded windows
per document (including neighbors). New text permits one note-revision call;
different hits share the window budget across questions, with omitted candidates
recorded rather than interpreted as absence from the source.
no request or no new window adds no call. Queries, original excerpts and unresolved
questions reach synthesis and writing; superseded interpretations remain in the
read artifact, not the current synthesis input. No match is not evidence of
absence. Unresolved requests keep the result partial, without recursive searches
or downloads. Recovery uses the existing read attempt, not a new per-paper API
checkpoint; a failed read may repeat its calls when resumed.

Bounded notes may also retain `claim_scopes`: the claimed object, property,
conditions, evidence kind and same-document passage references. Missing fields
remain unknown; unresolved or cross-source references keep reading partial.
Synthesis and report views preserve this scope. These are model interpretations,
not independently verified claims. Parsed document access and a bounded model
note are shown separately, including note coverage; neither means full-document
comprehension. Adopted-report context includes bounded, position-labelled table
rows alongside prose windows, not a second source of measured results.

LLM-generated ideas and local novelty checks are research suggestions, not proof
of originality. Offline fixture output is not model-backed scientific analysis.
See the capability entrypoints above for typed inputs and outputs.

## Artifact Ownership Summary

Experiment report assembly keeps reader prose separate from recorded evidence.
`data_tables=linked` adds a short link; `full` includes the summary tables as well.
Both modes preserve a local JSON/Markdown record package and explicit registered
source copies. Planning and preview use the same protected block as assembly.
Native whole-document inspection and historical checks receive that block too,
including their correction/context paths, rather than only counting its length.
Audit still checks required body metrics, package values/text and declared copy
presence; it does not independently verify the experiment or later copy tampering.
Reassembly and ACM export retain the original task and results; exports copy this
native package, not arbitrary local links or source directories.

Report assembly owns heading presentation and the final reference list, not the
meaning of section prose. A distinct leading subsection is retained; only an exact
duplicate of its section label is removed. Fenced-code headings and literal
`References` text are not document boundaries. Citation cleanup and numbering use
the same boundary rules. Final writing checkpoints save current unresolved
findings separately from historical review records, including when whole-document
review is off. Reassembly does not rerun reading, models or experiments and does
not independently certify the content.

- `session_manifest.json` records session and attempt state; the application
  chooses research actions, and the shared budget ledger records usage.
- Each `attempts/` directory owns its declared artifacts. References, rather
  than fixed stage numbers, connect search, reading, synthesis, implementation,
  experiments, analysis, writing and audit.
- Implementation freezes the patch and checks used for the measured revision.
  Experiment execution owns observed metrics; analysis interprets them.
- Report writing, assembly and audit retain their own attempt artifacts. A
  completed process, a completed workflow and a publication-quality paper are
  different claims.
- Historical numbered stage directories remain archives only. Rebuildable caches
  are not the authority for results and must not replace source artifacts.

## Code Task Artifact Boundaries

Standalone code tasks and research-session implementations use the same conceptual
layout. The important boundary is what each group is responsible for:

- `workspace/`: isolated editable project copy, worktree, or sparse subset.
- `meta/`: environment reports, repo maps, locate results, edit proposals,
  validation reports, applied-edit summaries, and LLM usage.
- `context_packs/`: bounded prompt context assembled from ranked editable files
  and protected read-only evidence.
- `attempts/`: durable work-plan and batch state for multi-step implementation
  and repair loops.
- `run/`: baseline/patched benchmark logs, metrics, execution reports, failure
  analysis, and before/after comparison.
- `repairs/`: bounded repair proposals grouped by repair attempt.

Tests, benchmarks, environment files, secrets, and user-configured protected paths
are indexed as read-only evidence by default and should not be edited by proposal,
repair, or apply steps. Edit-scope behavior and full artifact paths are described
in [Usage And Configuration](USAGE.md) and [Configuration Reference](CONFIG_REFERENCE.md).

## Code-Task Environment Strategy

Environment handling is intentionally separated from source-code isolation:

- Source-code isolation means user code is prepared under `code_task/workspace`
  before any patch is applied. In default `auto` mode this is usually a detached
  worktree for committed Git projects, with guarded copy fallback when Git is
  unavailable. The editable project root may be a subdirectory inside the
  worktree for monorepo-style code roots.
- Execution isolation means benchmarks run with a selected Python/runtime environment.

Today, code-task has the first kind of isolation and records environment signals
with `meta/environment_report.json`. It can select either the active
SimpleAutoResearch Python environment or a user-provided external interpreter.
It does not yet create virtual environments or install dependencies automatically.

The planned environment modes are:

- `current`: use the active SimpleAutoResearch Python environment. Supported now.
- `external`: use a user-provided Python or Conda interpreter. Supported now.
- `project-venv`: create a per-run environment inside the run directory. Planned.
- `shared-env-cache`: reuse environments keyed by dependency-file and platform hashes. Planned.
- `docker`: run in a container when stronger isolation is needed. Planned.

The default should remain conservative: dependency installation must be explicit
and reviewable, and user project packages should not be silently installed into
SimpleAutoResearch's own environment.

## Why Split Capabilities Internally

Internal capability boundaries do not mean exposing parallel product mainlines.
They keep the implementation from becoming one rigid pipeline while keeping
the formal entrypoint simple.

- If the user wants a survey, code stages should be skipped.
- If the user wants to optimize existing code, literature stages should be optional.
- `research-session` can include a prepared code experiment while keeping a
  bounded lifecycle boundary.
- Tests, recovery, developers, and future workflows can compose modules without
  making ordinary users understand the internal assembly.
- Each module can be upgraded independently, but there must not be a second
  session state, artifact contract, or report core.

This follows one practical lesson from AutoResearchClaw: complex behavior is easier to control when it is exposed as workflow modes and capabilities, not as one ever-growing sequence of flags.
