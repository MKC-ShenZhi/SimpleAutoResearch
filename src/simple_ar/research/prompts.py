from __future__ import annotations

from simple_ar.research.contracts import CLAIM_SCOPE_RULES
from simple_ar.research.evidence.bibliography import CITATION_FIELD_VALUE_RULE

PLAN_SYSTEM = (
    "You help scope small, reproducible research projects. "
    "Keep the plan concrete, modest, and testable."
)

READ_SYSTEM = (
    "You are a careful source reviewer. Structure retrieved document "
    "metadata and extracted snippets into bounded notes, separating facts, "
    "uncertainty, and interpretation without inventing details. "
    "For a missing user requirement, new_source_queries can request an exact "
    "HTTP(S) link quoted in the supplied source, including data or project documentation. "
    "Never invent a link. For other papers, use short scholarly keyword queries "
    "with the essential domain/task and one missing concept. Separate different "
    "routes; do not concatenate years, alternatives, every constraint or a "
    "natural-language instruction. Supporting constraints are checked in the "
    "retrieved evidence, not all required in one query."
)

SYNTHESIZE_SYSTEM = (
    "You synthesize shortlisted reading artifacts into research themes, gaps, "
    "and modest testable hypotheses grounded in cited paper/card evidence."
)

RESEARCH_DESIGN_SYSTEM = (
    "You select one evidence-grounded research direction and propose a small, "
    "structured execution protocol for a bounded experiment. The supplied project "
    "entry facts and execution boundary are authoritative. You may choose seeds, "
    "comparison need, metrics, stopping criteria, and literal argv parameters only "
    "within inspected entrypoints; you never grant yourself permissions, change the "
    "working directory, install software, access the network, or invent results."
)

RESEARCH_PLANNER_SYSTEM = (
    "You are a careful research retrieval planner. Break a topic into scoped "
    "research questions, expand search queries with useful terminology, and "
    "state out-of-scope boundaries. Do not claim that papers exist. Only plan "
    "what evidence should be searched for."
)


def plan_user_prompt(topic: str) -> str:
    """Build the planning prompt for a user-provided research topic.

    Args:
        topic: Research topic entered on the command line.

    Returns:
        Prompt requesting goal and problem Markdown as JSON fields.
    """
    return (
        "Given this research topic, write JSON with two string fields: "
        "`goal_markdown` and `problem_markdown`.\n\n"
        f"Topic:\n{topic}"
    )


def research_planner_user_prompt(
    *,
    topic: str,
    problem_markdown: str,
    seed_queries_json: str,
    required_facets_json: str,
    max_queries: int,
    max_rounds: int,
    mode: str,
) -> str:
    """Build the prompt for LLM-backed research-question and query planning.

    Args:
        topic: User-provided research topic.
        problem_markdown: Problem artifact produced by the plan stage.
        seed_queries_json: JSON list of configured seed queries.
        required_facets_json: JSON list of facets requested by config.
        max_queries: Maximum number of executable queries to return.
        max_rounds: Planned retrieval-round budget.
        mode: Research mode, such as ``lite``, ``standard``, or ``strong``.

    Returns:
        Prompt requesting bounded JSON fields for retrieval planning.
    """
    return (
        "Create a retrieval plan for this research topic. Return JSON with "
        "these fields:\n"
        "- `questions`: list of objects with `question`, `facet`, `rationale`, "
        "`required`, `negative_scope`, and `success_criteria`.\n"
        "- `query_specs`: ordered objects capped by `max_queries`. Each object "
        "must include `facet`, `title_keywords`, `abstract_keywords`, and "
        "`rationale`.\n"
        "- `queries`: optional fallback list of short paper-search keyword "
        "queries derived from `query_specs`.\n"
        "- `required_facets`: evidence facets to cover.\n"
        "- `negative_terms`: terms or scopes that should be avoided.\n"
        "- `temporal_scope`: optional object with inclusive integer `start_year`, "
        "`end_year`, and `basis_quote` copied verbatim from Topic, containing both "
        "year endpoints. Return null/omit when Topic does not explicitly bound "
        "publication years. Never derive it from current date, paper metadata, "
        "or the generated Problem artifact; do not invent a recency window. "
        "This bounds core survey evidence, not separately identified foundational context.\n"
        "- `rationale`: one short explanation of the plan.\n\n"
        "Guidelines:\n"
        "- Include at least one overview question, then method/benchmark/"
        "dataset/code/limitation questions when relevant.\n"
        "- Expand beyond the user's exact keywords with synonyms and likely "
        "paper terminology, but keep queries specific.\n"
        "- Think like scholarly search over paper title and abstract fields. "
        "`title_keywords` should identify the essential domain/task in 2-5 "
        "high-signal terms or short phrases that should co-occur. Keep each "
        "query focused; do not require all constraints or route synonyms in "
        "one query. `abstract_keywords` should be 3-8 supporting terms likely to "
        "appear in abstracts.\n"
        "- Query strings must be compact paper-search keyword queries suitable "
        "for OpenAlex/Semantic Scholar/arXiv/BM25, not browser questions. "
        "Choose terms appropriate to the actual research topic.\n"
        "- Avoid procedural words that are unlikely to be paper metadata terms "
        "unless paired with the domain, such as standalone `planner`, "
        "`implementer`, `handoff`, or `reviewer`.\n"
        "- Generate multiple focused title/abstract keyword combinations "
        "instead of one long descriptive query.\n"
        "- Do not invent paper titles, citations, datasets, or repositories.\n"
        "- Prefer queries that can help later screening and coverage checks.\n"
        "- Keep every string concise.\n\n"
        f"Topic:\n{topic}\n\n"
        f"Problem artifact:\n{problem_markdown}\n\n"
        f"Seed queries JSON:\n{seed_queries_json}\n\n"
        f"Required facets JSON:\n{required_facets_json}\n\n"
        f"Research mode: {mode}\n"
        f"max_queries: {max_queries}\n"
        f"max_rounds: {max_rounds}\n"
    )


def paper_note_user_prompt(paper_json: str, evidence_snippets: str = "", *, topic: str = "", problem_markdown: str = "", revision_context_json: str = "", front_matter_json: str = "") -> str:
    """Build the reading prompt for a single paper metadata record.

    Args:
        paper_json: JSON text containing one paper metadata object.
        evidence_snippets: Optional source-labelled retrieval snippets from the
            current run.
        topic: User's task focus, not evidence of a paper's claims.
        problem_markdown: User's requested scope and constraints, kept separate
            from retrieved source text.

    Returns:
        Prompt requesting one structured paper note.
    """
    evidence_block = _evidence_block(evidence_snippets)
    task_context = (
        "Task context (user request, not source evidence):\n"
        f"Topic: {topic}\n{problem_markdown}\n\n"
        if topic.strip() or problem_markdown.strip() else ""
    )
    return (
        task_context + "Given one paper metadata record as JSON, write one synthesis-ready "
        "Paper Brief as a JSON object. Use only the supplied metadata and "
        "source snippets. If evidence is thin, write `unknown` or an empty "
        "list instead of inventing details.\n\n"
        "Required fields:\n"
        "- `paper_id`: stable id from the input.\n"
        "- `title`: paper title. If title_role is filename_placeholder_not_publication_title, obtain the actual title from the supplied front matter and include its exact quote in bibliographic_fields; a filename is not known publication metadata. Preserve already supplied publication titles.\n"
        "- `bibliographic_fields`: optional list (at most five) of missing local citation fields visible in the supplied front_matter, each with `field` (title, authors, published, doi or url), `value` (string, or list of author names), exact `section_id` and verbatim `quote`. "
        + CITATION_FIELD_VALUE_RULE + " Never infer publication year from the filename, DOI digits, cited references or PDF creation time. For authors, set `complete` true only when the entire byline is visible and copied; otherwise false. Prefer a short exact byline subset over an unreliable full-list transcription. Do not use a cited work's metadata or invent a URL from an identifier. Return [] when unavailable or already supplied.\n"
        "- `evidence_role`: one of overview, method, benchmark, dataset, code, "
        "limitation, comparison, or other.\n"
        "- `one_sentence_summary`: concise factual summary.\n"
        "- `problem`: problem or research setting studied by the paper.\n"
        "- `method`: method, system, or approach summary.\n"
        "- `datasets`: list of dataset or benchmark names, if visible.\n"
        "- `metrics`: list of metrics or evaluation criteria, if visible.\n"
        "- `key_claims`: list of conservative claims explicitly supported by "
        "the input.\n"
        "- `claim_scopes`: at most eight objects for important task-relevant claims, each with "
        "`claim`, `object` (what is predicted, measured or constructed), `property` (the specific guarantee "
        "or observation), `conditions` (list), `evidence_kind` (theoretical, empirical, conjecture, proposal, "
        "or unknown), and `evidence_refs` (exact supplied chunk ids). Missing details remain unknown; "
        "do not turn a declaration into verified support.\n"
        "- `limitations`: list of limitations, risks, or missing evidence.\n"
        "- `relation_to_topic`: why this paper matters for the current topic.\n"
        "- `synthesis_hint`: one short sentence saying how synthesize should "
        "use this paper.\n"
        "- `possible_experiment_hooks`: list of small experiment/code-task "
        "ideas suggested by the evidence.\n"
        "- `open_questions`: list of questions that remain unresolved.\n"
        "- `evidence_refs`: list of paper ids, snippet labels, or empty list.\n"
        "- `confidence`: low, medium, or high.\n\n"
        "- `followup_queries`: up to two specific source phrases (each at most 500 characters) to locate evidence needed for an important task-relevant claim, apparent omission or contradiction; otherwise []. These search saved text only, not the web.\n\n"
        "Rules:\n"
        "- Do not produce long prose. This is a machine-readable brief for "
        "later synthesis and experiment design.\n"
        "- Do not claim novelty or performance unless it is explicit in the "
        "input.\n"
        "- Prefer useful uncertainty over confident hallucination.\n\n"
        + "\n".join(CLAIM_SCOPE_RULES) + "\n"
        + "- The displayed snippets are a window, not the entire saved source. Before asserting that an important result, condition or comparison is absent, request a specific followup query if more saved text may resolve it. Do not request generic background unrelated to the task.\n"
        "- In revision, use the original snippets and actual lookup passages to correct the previous note. Preserve supported facts and references; revise mistaken missing-evidence claims. A lexical match or no match does not establish semantic support or absence from the original. Unresolved questions remain explicit; there is no further automatic round.\n\n"
        "- Compare repeated statements of important results or assumptions. If values or claims differ, first check whether their conditions differ. Preserve an unresolved source disagreement with both locators in limitations/open_questions; do not silently choose one value or average them. Correcting a mistaken model note is different from resolving disagreement within the source.\n\n"
        f"Paper JSON:\n{paper_json}"
        f"{evidence_block}"
        + (f"\n\nSaved front_matter for citation extraction (not another source):\n{front_matter_json}" if front_matter_json else "")
        + (f"\n\nPrior note and bounded source lookup (model interpretation and original passages remain distinct):\n{revision_context_json}" if revision_context_json else "")
    )


def read_coarse_screening_user_prompt(
    *,
    topic: str,
    problem_markdown: str,
    papers_json: str,
    research_plan_json: str,
    min_shortlist: int = 0,
) -> str:
    """Build the prompt for abstract-level read-stage coarse screening.

    The coarse pass is designed for larger retrieval sets. It only sees compact
    metadata and abstracts, so it should decide whether a paper deserves deeper
    reading without trying to synthesize the field.
    """
    return (
        "Coarsely screen this small batch of retrieved source metadata for the "
        "current research problem. Return JSON with `decisions`, a "
        "list of objects. Each object must contain `paper_id`, `decision` "
        "(`keep` or `drop`), `coarse_relevance_score` (0-5), `reason`, "
        "`likely_facet`, and `confidence`.\n\n"
        "Also return `question_assessments`: one object per actual required research "
        "question_id in Research Plan, with `question_id`, `status` (direct_candidate, "
        "context_only, missing), `paper_ids` (only candidates in this batch), `reason`, "
        "and `new_source_queries` (at most two strings, each <=500 characters). "
        "Assess the question's specific object, property and conditions, not a generic "
        "facet label. A benchmark for point prediction may be useful context without "
        "answering an interval-coverage method question. direct_candidate means only "
        "metadata suggests pertinent evidence, never an answered question. For missing "
        "or context-only core evidence propose focused external queries for the needed "
        "source type, respecting any stated temporal scope, not phrases to search this source. All-drop batches "
        "must still assess question gaps. Do not assert field-wide absence.\n\n"
        "Rules:\n"
        "- Use only title, abstract, source metadata, and the research plan. Do "
        "not infer details that are not present.\n"
        "- This is a fast abstract-level pass. Do not write long summaries.\n"
        "- Candidates may be papers, datasets, repositories or documentation. Match "
        "the source type to the requested evidence; a paper mentioning code is not "
        "a substitute for implementation documentation, and metadata is not verification.\n"
        "- Keep papers that could help answer a research question, explain a "
        "method family, provide benchmark/dataset/code signals, or expose a "
        "limitation relevant to a later experiment.\n"
        "- Drop papers that are clearly outside the topic, purely adjacent, or "
        "lack useful evidence for the configured research questions.\n"
        "- If a paper is thin but plausibly relevant, keep it with lower "
        "confidence instead of pretending certainty.\n"
        "- If `min_shortlist` is greater than zero, this run values broader "
        "coverage for later synthesis. Avoid dropping plausible overview, "
        "method, benchmark, dataset, or limitation papers too early.\n"
        "- `likely_facet` should be compact, such as overview, method, "
        "benchmark, dataset, code, limitation, or other.\n"
        "- Keep reasons concise and auditable.\n\n"
        f"Topic:\n{topic}\n\n"
        f"Problem artifact:\n{problem_markdown}\n\n"
        f"Research Plan JSON:\n{research_plan_json}\n\n"
        f"Paper Batch JSON:\n{papers_json}\n\n"
        f"min_shortlist: {min_shortlist}\n"
    )


def read_rerank_user_prompt(
    *,
    topic: str,
    problem_markdown: str,
    papers_json: str,
    research_plan_json: str,
    coarse_decisions_json: str,
    max_shortlist: int,
    min_shortlist: int = 0,
    question_assessments_json: str = "[]",
) -> str:
    """Build the prompt for read-stage reranking of coarsely kept papers."""
    return (
        "Select from the supplied source candidates for structured reading. Return JSON "
        "with one field `ranked_papers`, a list of objects. Each object must "
        "contain `paper_id`, `decision` (`keep` or `drop`), "
        "`reading_priority` (1 is highest priority), `relevance_score` (0-5), "
        "`quality_score` (0-5), `evidence_role`, `reason`, "
        "`synthesis_hint`, and `confidence`.\n\n"
        "Also return `question_assessments` using actual required research question_ids, "
        "with status direct_candidate/context_only/missing, paper_ids, reason, and "
        "new_source_queries (at most two strings, <=500 characters each). Compare the "
        "whole candidate assessment supplied below with each question's object, property "
        "and conditions. Context for a benchmark or foundational method is not direct "
        "evidence for a different question. Prioritize direct question evidence before "
        "context, without filling a quota with irrelevant papers. Only cite kept paper_ids "
        "as prospective candidates; metadata never proves a question answered. For "
        "unresolved questions propose external queries for the needed source type, not local text lookups.\n\n"
        "Rules:\n"
        "- Prefer a diverse shortlist that covers the research questions rather "
        "than many near-duplicate papers.\n"
        "- Match evidence needs to source types: implementation and access details "
        "need maintainer or dataset documentation, not only academic discussions. "
        "Do not prefer a publication over a relevant primary document just because "
        "it is a paper. Preserve actual software, access and resource constraints; "
        "a mentioned tool is not automatically an eligible implementation route.\n"
        "- `evidence_role` should describe how the paper helps later synthesis: "
        "overview, method, benchmark, dataset, code, limitation, comparison, or "
        "other.\n"
        "- `synthesis_hint` should be one compact sentence explaining what the "
        "synthesize stage should look for in this paper.\n"
        "- Do not invent methods, datasets, metrics, repositories, or results.\n"
        "- Keep at most `max_shortlist` papers unless the input contains fewer "
        "papers.\n"
        "- If `min_shortlist` is greater than zero and enough useful papers are "
        "available, keep at least that many diverse papers. Drop below it only "
        "when the candidates are off-topic or near duplicates, and explain why.\n"
        "- This is prioritization for reading and synthesis, not final novelty "
        "judgment.\n\n"
        f"Topic:\n{topic}\n\n"
        f"Problem artifact:\n{problem_markdown}\n\n"
        f"Research Plan JSON:\n{research_plan_json}\n\n"
        f"Coarse Decisions JSON:\n{coarse_decisions_json}\n\n"
        f"Whole-candidate question observations (batch-local, not verified answers):\n{question_assessments_json}\n\n"
        f"Kept Papers JSON:\n{papers_json}\n\n"
        f"max_shortlist: {max_shortlist}\n"
        f"min_shortlist: {min_shortlist}\n"
    )


def read_screening_user_prompt(
    *,
    topic: str,
    problem_markdown: str,
    papers_json: str,
    research_plan_json: str,
    max_shortlist: int,
    min_shortlist: int = 0,
) -> str:
    """Build the prompt for read-stage paper screening and prioritization."""
    return (
        "Review the retrieved paper metadata for the current research problem. "
        "Return JSON with one field `decisions`, a list of objects. Each object "
        "must contain `paper_id`, `decision` (`keep` or `drop`), "
        "`reading_priority` (1 is highest priority), `relevance_score` (0-5), "
        "`quality_score` (0-5), `reason`, and `confidence`.\n\n"
        "Rules:\n"
        "- Keep only papers that are useful for answering the research questions "
        "or designing a bounded follow-up experiment.\n"
        "- Prefer papers with methods, benchmarks, datasets, limitations, or code "
        "signals relevant to the topic.\n"
        "- Do not invent facts that are not in metadata or the research plan.\n"
        "- If metadata is thin but potentially relevant, keep it with lower "
        "confidence rather than pretending certainty.\n"
        "- Keep at most `max_shortlist` papers unless all retrieved papers are "
        "clearly necessary.\n"
        "- If `min_shortlist` is greater than zero and the batch contains enough "
        "potentially useful papers, avoid over-pruning. Keep borderline but "
        "relevant papers with lower confidence so the reranker can decide.\n"
        "- This is read-stage screening, not novelty review.\n\n"
        f"Topic:\n{topic}\n\n"
        f"Problem artifact:\n{problem_markdown}\n\n"
        f"Research Plan JSON:\n{research_plan_json}\n\n"
        f"Retrieved Papers JSON:\n{papers_json}\n\n"
        f"max_shortlist: {max_shortlist}\n"
        f"min_shortlist: {min_shortlist}\n"
    )


def synthesize_user_prompt(
    notes_markdown: str,
    paper_notes_json: str,
    evidence_snippets: str = "",
    structured_context_json: str = "",
    source_count: int | None = None,
) -> str:
    """Build the synthesis prompt from free-form and structured notes.

    Args:
        notes_markdown: Human-readable notes produced by the read stage.
        paper_notes_json: Structured notes serialized as JSON text.
        evidence_snippets: Optional source-labelled retrieval snippets from the
            current run.
        structured_context_json: Optional compact JSON assembled from Paper
            Briefs, the synthesis brief, retrieval coverage, and bounded idea
            hints.

    Returns:
        Prompt requesting synthesis and hypothesis Markdown as JSON fields.
    """
    evidence_block = _evidence_block(evidence_snippets)
    structured_block = (
        "\n\nStructured Read/Synthesis Context JSON:\n"
        f"{structured_context_json}"
        if structured_context_json.strip()
        else ""
    )
    source_scope_guidance = (
        "- With at most one source, summarize its actual claims and available observations; "
        "do not invent cross-paper consensus, disagreement, or method families.\n"
        if source_count is not None and source_count <= 1 else
        "- Group multiple papers into 2-4 evidence-supported themes or approach patterns.\n"
        "- Separate observed agreement, disagreement, and missing evidence.\n"
    )
    return (
        "Given literature notes, write JSON with the required string fields "
        "`synthesis_markdown` and `hypothesis_markdown`. You may also return "
        "an `idea_candidates` list containing at most the requested number of "
        "bounded, evidence-grounded experiment ideas. The hypothesis must be "
        "small enough for a local experiment or code-task follow-up. Prefer "
        "structured Paper Briefs, the synthesis brief, and source-labelled "
        "snippets when they are provided, and do not make claims that cannot "
        "be traced to notes, briefs, or snippets.\n\n"
        "Synthesis requirements:\n"
        + source_scope_guidance
        + "\n".join(CLAIM_SCOPE_RULES) + "\n"
        + "- Identify concrete gaps that could become bounded experiments.\n"
        "- Propose the next experiment, not the whole eventual sequence. For an iterative task, "
        "distinguish the supplied original baseline, the first proposed candidate, and later alternatives "
        "that must be chosen after measurement. Do not list an uncreated future candidate as an existing "
        "baseline or require its results before the first experiment.\n"
        "- The hypothesis should name a measurable change, likely metric, and "
        "failure condition when supported by the context.\n"
        "- Use stable paper ids as provenance anchors where possible. If you use\n"
        "  a chunk/card id, copy the complete identifier exactly from the supplied\n"
        "  JSON; do not shorten provider or document prefixes.\n\n"
        "- Treat `allowed_motivation_refs` in the Structured Read/Synthesis\n"
        "  Context JSON as a closed allowlist. Every `motivation_refs` entry\n"
        "  must equal one of those strings exactly. Never cite a paper or chunk\n"
        "  from memory, from the broader search pool, or from an omitted source.\n\n"
        "- If a Prepared Experiment Boundary is supplied, it is authoritative for\n"
        "  the executable idea: keep its project, dataset, benchmark, metrics,\n"
        "  and runtime constraints. Use literature only to motivate a compatible\n"
        "  change; do not substitute a dataset or task from a retrieved paper.\n"
        "  If the literature conflicts with that boundary, choose a compatible\n"
        "  direction and state the limitation instead of silently changing it.\n\n"
        "If `idea_candidates` is present, each item must contain non-empty "
        "`idea_id`, `title`, `hypothesis`, `proposed_change`, and "
        "`motivation_refs`, plus optional `expected_outcome`, "
        "`required_baselines`, `required_datasets`, `metrics`, `feasibility`, "
        "and `risks`. Use JSON lists for list fields; a single string is accepted "
        "for optional list fields for compatibility. Every motivation ref must be copied exactly from an "
        "evidence/card/chunk identifier in the supplied context. Do not invent "
        "paper ids, claims, datasets, metrics, commands, or results.\n\n"
        + (f"Notes Markdown:\n{notes_markdown}\n\n" if notes_markdown.strip() else "")
        + f"Structured Notes JSON:\n{paper_notes_json}"
        f"{structured_block}"
        f"{evidence_block}"
    )


def research_design_user_prompt(
    *,
    research_context: str,
    ideas_json: str,
    novelty_checks_json: str,
    contract_json: str,
    execution_context: str = "",
    execution_boundary_json: str = "{}",
    entry_facts_json: str = "{}",
    requested_idea_id: str = "",
) -> str:
    """Build the bounded model prompt for selecting an idea and protocol."""

    boundary = (
        "\n\nPrepared Experiment Boundary (hard):\n"
        + execution_context[:8000]
        + "\n"
        if execution_context.strip()
        else ""
    )
    requested_clause = (
        f"- An explicit idea id was supplied; return `{requested_idea_id}` exactly.\n"
        if requested_idea_id.strip()
        else ""
    )
    return (
        "Select exactly one candidate research idea for the next bounded experiment "
        "and return a JSON object with `selected_idea_id`, `rationale`, and an "
        "`execution_protocol` object.\n\n"
        "Rules:\n"
        "- `selected_idea_id` must be copied exactly from the candidate list.\n"
        + requested_clause
        + "- Prefer a measurable, feasible direction with clear evidence references "
        "and lower unresolved risk.\n"
        "- The rationale must be concise and refer only to supplied candidates, "
        "novelty checks, and the existing contract.\n"
        "- Do not create a new idea or change the hypothesis or proposed change.\n"
        "- `execution_protocol` may contain only these fields: `command`, "
        "`baseline_command`, `pairs`, `seeds`, `seed_count`, `seed_flag`, "
        "`baseline_policy`, `result_schema`, `comparison_required`, "
        "`decision_reason`, and `stopping_criteria`.\n"
        "- Omit unused optional fields entirely (no null or empty placeholders). "
        "Use either explicit `pairs` OR compact `seeds`/`seed_count` with "
        "`seed_flag`, never both. For one fixed benchmark invocation, omit all "
        "pair and seed fields. Do not invent a seed flag unless the inspected "
        "entrypoint supports it.\n"
        "- Field types: command/baseline_command are non-empty arrays of strings; "
        "baseline_policy is one of run, skip, reuse; comparison_required is a "
        "JSON boolean; decision_reason is a string; stopping_criteria is an "
        "array of non-empty strings (NOT an object). result_schema is an object. "
        "seeds is an array of distinct integers; seed_count is a positive integer "
        "(choose seeds or seed_count); seed_flag is a non-empty string. Each pairs "
        "item has exactly seed (integer), baseline_command and candidate_command "
        "(argv arrays).\n"
        '- Minimal execution_protocol example for a single comparison using the '
        'configured evaluator: {"baseline_policy":"run","comparison_required":true,'
        '"decision_reason":"Compare the candidate with the original under the same evaluation",'
        '"stopping_criteria":["Stop after the bounded comparison"]}. '
        "Do not expand this object with unused fields.\n"
        "- Commands must be literal argv lists derived from the inspected authorized "
        "entrypoint. Do not return shell text, cwd, timeout, budget, installers, "
        "network actions, or arbitrary file paths.\n"
        "- Do not return `input_refs`: the application binds provenance from the "
        "actual inspected attempt inputs.\n"
        "- Explicit execution settings win over this proposal. Never invent a metric "
        "or result; use the configured result schema when present.\n\n"
        "- Decide comparison_required and baseline_policy from the actual task: "
        "measuring an existing method or repeating seeds does not itself require "
        "a separate control run. A baseline mentioned in source literature is not "
        "an instruction to execute it. For an improvement comparison, explain the "
        "required control and choose run or reuse based on supplied evidence; "
        "otherwise choose skip. Do not claim a measured improvement without "
        "comparable control evidence.\n"
        "- When a Prepared Experiment Boundary is supplied, preserve its project, "
        "dataset, benchmark, and runtime constraints; select only a compatible "
        "candidate.\n\n"
        f"Research context:\n{research_context}\n\n"
        f"Candidate ideas JSON:\n{ideas_json}\n\n"
        f"Novelty checks JSON:\n{novelty_checks_json}\n\n"
        f"Existing experiment contract JSON:\n{contract_json}\n"
        f"Execution boundary JSON:\n{execution_boundary_json}\n\n"
        f"Inspected project entry facts JSON:\n{entry_facts_json}\n"
        f"{boundary}"
    )


def _evidence_block(evidence_snippets: str) -> str:
    """Format optional retrieval evidence for prompt inclusion."""
    stripped = evidence_snippets.strip()
    if not stripped:
        return ""
    return (
        "\n\nRetrieved Evidence Snippets:\n"
        "Each snippet is labelled as `[evidence_id | path:line_start-line_end | query=...]`.\n"
        f"{stripped}"
    )


def _research_evidence_block(summary: str) -> str:
    """Format compact research evidence cards for report drafting."""
    stripped = summary.strip()
    if not stripped:
        return ""
    return (
        "\n\nResearch Evidence Summary:\n"
        "This summary is generated from structured search-stage evidence artifacts.\n"
        f"{stripped}"
    )
