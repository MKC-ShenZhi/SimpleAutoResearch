"""Impact, required work and the bounded correction lifecycle are distinct."""
import unittest

from tests.report_review_fixtures import agent_inputs, document_review_inputs, prompt_payload, ReviewPromptCapture

from simple_ar.integrations.llm import LLMError
from simple_ar.report.agent import _needs_revision, _needs_evidence_recheck, run_report_agent
from simple_ar.report.audit import build_report_audit
from simple_ar.report.editor import review_document
from simple_ar.report.schema import (
    ReportContext, ReportMemory, ReportRuntimeConfig, ReportSectionDraft,
    ReportSectionPlan, ReportSectionReview, ReviewerFinding, SourceHandle, finding_requires_resolution,
)
from simple_ar.research.contracts import DocumentRecord, TextChunk
from simple_ar.research.documents.ingest import DocumentBundle
from simple_ar.report.tool_gateway import ReportToolGateway


class ReviewActionTests(unittest.TestCase):
    def finding(self, action=None, **changes):
        values = dict(finding_id='scope', type='missing_limitation', severity='minor',
            message='The stated conclusion needs a qualification.', section_id='findings',
            suggested_action='Qualify the conclusion.', required_action=action)
        return ReviewerFinding(**{**values, **changes})

    def objects(self, **settings):
        context = ReportContext(topic='Explain supplied material', report_mode='supplied_materials')
        config = ReportRuntimeConfig(template='material_report', max_review_iterations=1, **settings)
        memory = ReportMemory(section_plan=[ReportSectionPlan(section_id='findings',
            heading='Findings', goal='Explain without extending the supplied setting')])
        return agent_inputs(context, memory, config)

    def test_explicit_action_is_independent_of_impact_and_prose_wording(self):
        for kind in ('style', 'missing_limitation', 'evidence_gap'):
            for action, required in ((None, False), ('advisory', False), ('revise', True), ('verify', True)):
                with self.subTest(kind=kind, action=action):
                    finding = self.finding(action, type=kind)
                    self.assertEqual(finding_requires_resolution(finding), required)
                    review = ReportSectionReview(section_id='findings', verdict='pass', findings=[finding])
                    self.assertEqual(_needs_revision(review), required)
        # An optional instruction cannot demote existing factual/critical guards.
        for changes in ({'severity': 'critical'}, {'type': 'citation_misuse'}, {'severity': 'major'}):
            self.assertTrue(finding_requires_resolution(self.finding('advisory', **changes)))

    def test_mixed_edit_and_verification_does_not_add_redundant_recheck(self):
        from simple_ar.report.schema import ReportToolCall
        call = ReportToolCall(tool_name='get_paper_brief', arguments={'handle': 'paper:p'})
        for findings, expected in (([self.finding('verify')], True),
            ([self.finding('verify'), self.finding('revise', finding_id='edit')], False)):
            review = ReportSectionReview(section_id='findings', verdict='warning',
                                         findings=findings, context_requests=[call])
            self.assertEqual(_needs_evidence_recheck(review), expected)

    def test_verify_only_fetches_and_rejudges_without_rewriting_supported_prose(self):
        objects = self.objects()
        objects['context'].source_handles = [SourceHandle(handle='paper:p', kind='paper', paper_id='p',
            metadata={'document_id': 'doc'})]
        objects['memory'].source_handles = objects['context'].source_handles
        objects['memory'].section_plan[0].evidence_handles = ['paper:p']
        bundle = DocumentBundle(records=[DocumentRecord('doc', 'Notes', 'local', extraction_status='parsed')],
            fulltext_manifest={}, fulltext_extraction={}, sections=[],
            chunks=[TextChunk('doc:1', 'doc', 'Observation limited to one declared setting.')])
        objects['gateway'] = ReportToolGateway(objects['context'], documents=bundle)
        labels = []
        finding = self.finding('verify')
        class Client:
            def ask_json(client, system, prompt, *, label='', **kwargs):
                labels.append(label)
                if label.endswith('-evidence'):
                    assert 'Observation limited to one declared setting.' in prompt
                    return {'verdict': 'pass'}
                if 'reviewer' in label:
                    return {'verdict': 'warning', 'findings': [finding.model_dump(mode='json')],
                        'context_requests': [{'tool_name': 'search_source_chunks',
                            'arguments': {'handle': 'paper:p', 'query': 'declared setting'}}]}
                return {'draft_markdown': 'Only the supplied setting was observed.'}
        result = run_report_agent(client=Client(), **objects)
        self.assertEqual(sum(label.endswith('-evidence') for label in labels), 1)
        self.assertFalse(any('reviser' in label for label in labels))
        self.assertEqual(len(result.tool_results), 1)
        self.assertFalse(result.memory.reviewer_findings)

    def test_historical_findings_and_explicit_actions_roundtrip_without_guessing(self):
        raw = self.finding().model_dump(mode='json')
        raw.pop('required_action')
        self.assertIsNone(ReviewerFinding.model_validate(raw).required_action)
        for action in ('advisory', 'revise', 'verify'):
            value = self.finding(action)
            self.assertEqual(ReviewerFinding.model_validate_json(value.model_dump_json()), value)

    def test_failed_lookup_is_corrected_before_spending_a_prose_revision(self):
        from simple_ar.report.schema import ReportToolCall, ReportToolResult
        from unittest.mock import patch
        for lookup_recovers in (True, False):
            with self.subTest(lookup_recovers=lookup_recovers):
                objects = self.objects()
                labels, saved = [], []
                request = ReportToolCall(tool_name='get_code_task_result', arguments={'query': 'initial'})
                finding = self.finding('revise', severity='major')
                class Client:
                    def ask_json(client, system, prompt, *, label='', **kwargs):
                        labels.append(label)
                        if label.endswith('-evidence'):
                            self.assertIn('not_found', prompt)
                            return {'verdict': 'revise_required', 'findings': [finding.model_dump(mode='json')],
                                'context_requests': [request.model_copy(update={'arguments': {'query': 'corrected'}}).model_dump(mode='json')]}
                        if 'reviewer' in label:
                            if 'round-2' in label and lookup_recovers:
                                return {'verdict': 'pass'}
                            return {'verdict': 'revise_required', 'findings': [finding.model_dump(mode='json')],
                                    'context_requests': [] if 'round-2' in label else [request.model_dump(mode='json')]}
                        if 'reviser' in label:
                            self.assertIn('recorded 0.42' if lookup_recovers else 'still missing', prompt)
                        return {'draft_markdown': 'Bounded observed result.'}
                responses = [ReportToolResult(tool_name=request.tool_name, status='not_found'),
                    ReportToolResult(tool_name=request.tool_name, status='ok' if lookup_recovers else 'not_found',
                                     content={'text': 'recorded 0.42' if lookup_recovers else 'still missing'})]
                with patch.object(objects['gateway'], 'call', side_effect=responses) as calls:
                    result = run_report_agent(client=Client(), **objects, checkpoint_sink=saved.append)
                self.assertEqual(calls.call_count, 2)
                self.assertEqual(sum('reviser' in label for label in labels), 1)
                self.assertEqual(sum(label.endswith('-evidence') for label in labels), 1)
                self.assertEqual(len(result.tool_results), 2)
                self.assertEqual(bool(result.memory.reviewer_findings), not lookup_recovers)
                self.assertTrue(any(len(row['tool_results']) == 2 for row in saved[-1]['iterations']))
                before = len(labels)
                run_report_agent(client=Client(), **objects, completed_checkpoint=saved[-1])
                self.assertEqual(len(labels), before)

    def test_minor_actions_spend_corrections_only_for_required_work(self):
        for action, kind in (('advisory', 'missing_limitation'), ('revise', 'style'), ('verify', 'missing_limitation')):
            with self.subTest(action=action):
                labels = []
                finding = self.finding(action, type=kind)
                class Client:
                    def ask_json(client, system, prompt, *, label='', **kwargs):
                        labels.append(label)
                        if 'reviewer' in label:
                            if 'round-2' in label:
                                return {'verdict': 'pass', 'findings': []}
                            return {'verdict': 'warning', 'findings': [finding.model_dump(mode='json')]}
                        if 'reviser' in label:
                            row = prompt_payload(prompt)
                            assert row['review_findings'][0]['required_action'] == action
                            return {'draft_markdown': 'Only the supplied setting was observed; wider validity is unknown.'}
                        return {'draft_markdown': 'The supplied observations are limited to this setting.'
                                if action == 'advisory' else 'The conclusion holds everywhere.'}
                result = run_report_agent(client=Client(), **self.objects())
                self.assertEqual(sum('reviser' in label for label in labels), int(action != 'advisory'))
                if action == 'advisory':
                    self.assertEqual(result.memory.reviewer_findings[0].required_action, action)
                else:
                    self.assertIn('wider validity is unknown', result.report_body)
                    self.assertFalse(result.memory.reviewer_findings)

    def test_exhausted_allowance_retains_required_work_and_audit_warning(self):
        objects = self.objects()
        objects['config'].max_review_iterations = 0
        finding = self.finding('verify')
        class Client:
            def ask_json(client, system, prompt, *, label='', **kwargs):
                if 'reviewer' in label:
                    return {'verdict': 'pass', 'findings': [finding.model_dump(mode='json')]}
                return {'draft_markdown': 'A supplied conclusion.'}
        result = run_report_agent(client=Client(), **objects)
        self.assertEqual(result.memory.reviewer_findings[0].required_action, 'verify')
        audit = build_report_audit(report=result.report_body, report_body=result.report_body,
                                  context=objects['context'], memory=result.memory)
        self.assertEqual(audit.status, 'warning')
        self.assertFalse(any(row.action == 'revise' for row in result.iterations))

    def test_action_survives_interruption_without_resetting_correction_quota(self):
        objects = self.objects()
        finding = self.finding('verify')
        labels, saved = [], []
        class Client:
            def ask_json(client, system, prompt, *, label='', **kwargs):
                labels.append(label)
                if 'reviewer' in label:
                    return {'verdict': 'warning', 'findings': [finding.model_dump(mode='json')]}
                return {'draft_markdown': 'Still needs checking.'}
        def sink(row):
            saved.append(row)
            if any(event['action'] == 'revise' for event in row['iterations']):
                raise LLMError('interrupted after candidate save')
        client = Client()
        with self.assertRaises(LLMError):
            run_report_agent(client=client, checkpoint_sink=sink, **objects)
        result = run_report_agent(client=client, completed_checkpoint=saved[-1], **objects)
        self.assertEqual(sum('reviser' in label for label in labels), 1)
        self.assertEqual(result.memory.reviewer_findings[0].required_action, 'verify')

    def test_historical_review_is_neutral_but_current_actions_are_explicit(self):
        objects = self.objects()
        objects['memory'].reviewer_findings = [self.finding('verify'), self.finding('advisory', finding_id='polish')]
        before = [row.model_dump(mode='json') for row in objects['memory'].reviewer_findings]
        capture = ReviewPromptCapture()
        inputs = document_review_inputs(objects['memory'], objects['config'],
            [ReportSectionDraft(section_id='findings', heading='Findings', draft_markdown='Text')],
            template=objects['template'])
        review_document(client=capture, **inputs,
            historical_findings=[objects['memory'].reviewer_findings[0]])
        self.assertEqual([row['finding_id'] for row in capture.views[0]['historical_findings_to_check']], ['scope'])
        for field in ('required_action', 'severity', 'suggested_action'):
            self.assertNotIn(field, capture.views[0]['historical_findings_to_check'][0])
        self.assertEqual([row.model_dump(mode='json') for row in objects['memory'].reviewer_findings], before)
        self.assertNotIn('findings', capture.views[0]['output_schema']['section_reviews'][0])
        review_document(client=capture, **inputs)
        self.assertIn('required_action', capture.views[1]['output_schema']['section_reviews'][0]['findings'][0])


if __name__ == '__main__':
    unittest.main()
