from __future__ import annotations

import unittest

from simple_ar.research.prompts import (
    paper_note_user_prompt,
    read_coarse_screening_user_prompt,
    read_rerank_user_prompt,
    research_planner_user_prompt,
    synthesize_user_prompt,
)


class PromptTests(unittest.TestCase):
    def test_read_and_synthesis_prompts_accept_source_labelled_evidence(self) -> None:
        evidence = "[ev-1 | 02-search/papers.jsonl:1-1 | query=metadata]\nKnown paper row"

        read_prompt = paper_note_user_prompt('{"id": "paper-1"}', evidence_snippets=evidence)
        synth_prompt = synthesize_user_prompt(
            "# Notes\nKnown evidence.",
            '[{"paper_id": "paper-1"}]',
            evidence_snippets=evidence,
        )

        self.assertIn("Retrieved Evidence Snippets", read_prompt)
        self.assertIn("02-search/papers.jsonl:1-1", read_prompt)
        self.assertIn("Retrieved Evidence Snippets", synth_prompt)
        self.assertIn("trace", synth_prompt)

    def test_synthesis_does_not_request_cross_paper_conclusions_from_one_source(self) -> None:
        single = synthesize_user_prompt("one note", "{}", source_count=1)
        multiple = synthesize_user_prompt("several notes", "{}", source_count=3)
        self.assertIn("do not invent cross-paper consensus", single)
        self.assertNotIn("Group multiple papers", single)
        self.assertIn("Group multiple papers", multiple)

    def test_research_planner_prompt_requests_short_source_queries(self) -> None:
        prompt = research_planner_user_prompt(
            topic="multi-agent coding",
            problem_markdown="# Problem\nStudy coding agents.",
            seed_queries_json='["multi-agent coding agents"]',
            required_facets_json='["method", "benchmark"]',
            max_queries=6,
            max_rounds=2,
            mode="standard",
        )

        self.assertIn("title_keywords", prompt)
        self.assertIn("abstract_keywords", prompt)
        self.assertIn("arXiv", prompt)
        self.assertIn("paper title and abstract fields", prompt)
        self.assertIn("not browser questions", prompt)

    def test_two_step_read_prompts_bound_coarse_and_rerank_tasks(self) -> None:
        coarse = read_coarse_screening_user_prompt(
            topic="coding agents",
            problem_markdown="# Problem\nStudy evaluation.",
            papers_json='[{"paper_id": "paper-1", "abstract": "coding benchmark"}]',
            research_plan_json='{"query_plan": {"queries": ["coding agents"]}}',
        )
        rerank = read_rerank_user_prompt(
            topic="coding agents",
            problem_markdown="# Problem\nStudy evaluation.",
            papers_json='[{"paper_id": "paper-1", "abstract": "coding benchmark"}]',
            research_plan_json='{"query_plan": {"queries": ["coding agents"]}}',
            coarse_decisions_json='[{"paper_id": "paper-1", "decision": "keep"}]',
            max_shortlist=5,
        )

        self.assertIn("abstract-level pass", coarse)
        self.assertIn("coarse_relevance_score", coarse)
        self.assertIn("likely_facet", coarse)
        self.assertIn("evidence_role", rerank)
        self.assertIn("synthesis_hint", rerank)
        self.assertIn("max_shortlist", rerank)


if __name__ == "__main__":
    unittest.main()
