"""Assembly owns section presentation, not subsection content or code syntax."""
import unittest
import tempfile
from pathlib import Path

from simple_ar.report.assembler import assemble_report_sections, apply_section_numbering
from simple_ar.report.schema import ReportSectionDraft
from simple_ar.report.citations import strip_references_section
from simple_ar.report.capability import ReportAssemblyRequest, assemble_report_document
from dataclasses import replace
from simple_ar.report.ports import DeterministicFigureRenderer
from simple_ar.report.schema import ReportRuntimeConfig, ReportDocumentPlan


class AssemblyBoundaryTests(unittest.TestCase):
    def test_template_is_not_figure_intent_and_saved_config_keeps_original_contract(self):
        request = ReportAssemblyRequest(title='Report', template_name='survey_long',
            sections=(ReportSectionDraft(section_id='taxonomy', heading='Taxonomy',
                draft_markdown='- Method family A\n- Method family B\n- Method family C'),))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fresh = assemble_report_document(request, report_dir=root / 'fresh')
            self.assertEqual(fresh.figures, ())
            self.assertFalse((root / 'fresh' / 'figures').exists())
            # Serialized config is a historical contract, including default
            # auto mode; fields explicitly saved must not become fresh defaults.
            saved = ReportRuntimeConfig().model_dump(mode='json')
            legacy = assemble_report_document(replace(request, config=saved), report_dir=root / 'saved')
            self.assertEqual(len(legacy.figures), 1)
            explicit = assemble_report_document(request, report_dir=root / 'port',
                figure_renderer=DeterministicFigureRenderer())
            self.assertEqual(explicit.report_body_markdown, legacy.report_body_markdown)
            whole = replace(request, config=saved, assembly_mode='whole_article', sections=(
                request.sections[0].model_copy(update={'draft_markdown':
                    '# Report\n\n## Taxonomy\n\n' + request.sections[0].draft_markdown}),))
            assembled = assemble_report_document(whole, report_dir=root / 'article')
            self.assertEqual(assembled.figures, ())
            self.assertNotIn('![', assembled.report_body_markdown)
            empty_plan = assemble_report_document(replace(request, config=saved,
                document_plan=ReportDocumentPlan()), report_dir=root / 'empty-plan')
            self.assertEqual(empty_plan.figures, ())

    def assemble(self, body, heading='Results'):
        return assemble_report_sections(title='Report', sections=[
            ReportSectionDraft(section_id='results', heading=heading, draft_markdown=body)])

    def test_distinct_leading_subsection_keeps_its_label(self):
        for prefix in ('#', '##', '###'):
            with self.subTest(prefix=prefix):
                self.assertIn('### Calibration boundary\n\nA bounded observation.',
                    self.assemble(f'{prefix} Calibration boundary\n\nA bounded observation.'))

    def test_only_duplicate_section_heading_is_removed(self):
        for label in ('Results', 'results', 'Results ##'):
            with self.subTest(label=label):
                report = self.assemble(f'## {label}\n\n### Calibration\n\nA finding.')
                self.assertEqual(report.count('## Results\n'), 1)
                self.assertEqual([line for line in report.splitlines() if line.startswith('#')],
                                 ['# Report', '## Results', '### Calibration'])
                self.assertIn('### Calibration\n\nA finding.', report)

    def test_code_fences_keep_heading_like_content_and_reference_markers(self):
        for opening, closing in (('```python', '```'), ('~~~~', '~~~~'),
                                  ('````text', '````'), ('  ```python', '  ```')):
            with self.subTest(opening=opening):
                code = f'{opening}\n# literal heading\n## References\nprint("keep")\n{closing}'
                report = self.assemble(code + '\n\nConcluding prose.')
                self.assertIn(code, report)
                self.assertIn('Concluding prose.', report)

    def test_literal_reference_text_is_not_a_section_boundary(self):
        body = 'The literal ## References is an example, not a heading.\n\nMore evidence.'
        self.assertIn(body, self.assemble(body))

    def test_real_reference_section_is_left_to_the_citation_renderer(self):
        report = self.assemble('A finding.\n\n## References\n\nInvented reference.')
        self.assertIn('A finding.', report)
        self.assertNotIn('Invented reference', report)

    def test_non_heading_hash_and_indented_code_are_not_deleted(self):
        for body in ('#not-a-heading\n\nAn observation.', '    ## literal code\n    value = 2'):
            with self.subTest(body=body):
                self.assertIn(body, self.assemble(body))

    def test_different_numbered_label_is_not_assumed_to_be_a_duplicate(self):
        for label in ('2025 Results', 'A Results', '1. Results'):
            with self.subTest(label=label):
                self.assertIn(f'### {label}\n\nAn observation.',
                              self.assemble(f'## {label}\n\nAn observation.'))

    def test_numbering_does_not_change_fenced_examples(self):
        for opening, closing in (('```', '```'), ('~~~~python', '~~~~'), ('````', '````')):
            with self.subTest(opening=opening):
                code = f'{opening}\n## Literal heading\n{closing}'
                report = apply_section_numbering(f'# Report\n\n{code}\n\n## Results\nText.', mode='academic')
                self.assertIn(code, report)
                self.assertIn('## 1 Results', report)

    def test_shorter_fence_does_not_end_a_longer_fenced_block(self):
        code = '````text\n```\n## Literal heading\n````'
        self.assertIn(code, self.assemble(code))
        self.assertIn(code, apply_section_numbering('# Report\n\n' + code, mode='academic'))

    def test_fenced_trailing_whitespace_is_preserved(self):
        code = '```text\nvalue with two spaces  \n```'
        self.assertIn(code, self.assemble(code))
        self.assertIn(code, apply_section_numbering('# Report\n\n' + code, mode='academic'))

    def test_citation_cleanup_shares_code_and_literal_reference_boundaries(self):
        body = '```text\n## References\nkeep this\n```\n\nReferences\nAn ordinary paragraph.'
        self.assertIn(body, strip_references_section(body))
        self.assertNotIn('Invented source', strip_references_section(body + '\n\n## References\nInvented source'))

    def test_complete_assembly_does_not_reintroduce_code_truncation(self):
        code = '```python\n## References\nprint("kept")\n```'
        body = '### Calibration boundary\n\n' + code + '\n\nA supplied observation [@p].'
        with tempfile.TemporaryDirectory() as tmp:
            result = assemble_report_document(ReportAssemblyRequest(title='Report',
                sections=(ReportSectionDraft(section_id='results', heading='Results', draft_markdown=body),),
                config={'figures': {'enabled': False}},
                papers=({'id': 'p', 'title': 'Recorded source', 'authors': [], 'url': ''},)),
                report_dir=Path(tmp))
        self.assertIn('### Calibration boundary', result.report_body_markdown)
        self.assertIn(code, result.report_body_markdown)
        self.assertIn('A supplied observation [@p].', result.report_body_markdown)
        self.assertFalse(result.removed_citations)


if __name__ == '__main__':
    unittest.main()
