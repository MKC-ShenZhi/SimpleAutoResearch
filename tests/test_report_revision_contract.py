"""A correction is verified against its request and keeps its consumed budget."""
import json
import unittest

from simple_ar.integrations.llm import LLMError, LLMResponseError
from simple_ar.report.agent import run_report_agent, _review_section_with_recovery, _writer_prompt, _writer_recovery_prompt
from simple_ar.report.narrative import pending_revision_review, revision_context
from simple_ar.report.schema import (
    ReportContext, ReportIterationRecord, ReportMemory, ReportRuntimeConfig,
    ReportSectionDraft, ReportSectionPlan, ReportSectionReview, ReportToolResult, ReviewerFinding,
)
from simple_ar.report.templates import load_report_template_bundle
from simple_ar.report.tool_gateway import ReportToolGateway
from tests.report_review_fixtures import draft_quotes, prompt_payload


class RevisionContractTests(unittest.TestCase):
    def test_planning_and_repair_allegations_are_not_promoted_to_source_facts(self):
        from simple_ar.report.schema import ReportDocumentPlan
        context, config, section, memory, template = self.objects()
        section.goal = "Discuss an alternative named by planning but not established by the source."
        baseline = ReportSectionDraft(section_id=section.section_id, heading=section.heading,
            draft_markdown="Only the measured observation and its qualification are established.")
        review = ReportSectionReview(section_id=section.section_id, verdict="revise_required", findings=[
            ReviewerFinding(finding_id="fallible", type="unsupported_claim", severity="major", required_action="revise",
                message="The alleged alternative appears in the prose.", suggested_action="Qualify that alternative.")])
        original = review.model_dump(mode="json")
        for planned in (False, True):
            with self.subTest(planned=planned):
                if planned:
                    memory.document_plan = ReportDocumentPlan(sections=[section])
                common = dict(context=context, memory=memory, section=section, config=config,
                    previous_draft=baseline, review=review, draft_mode="section_revision")
                normal = prompt_payload(_writer_prompt(**common, template=template, extra_context=[], source_batch_index=1,
                    source_batch_count=1, include_previous_draft=True))
                corrected = prompt_payload(_writer_recovery_prompt(**common))
                for view in (normal, corrected):
                    self.assertEqual(view["narrative_context"]["planning_status"]["scope"],
                        "organizational_intent_not_current_prose_or_scientific_support")
                    self.assertIn("not observed claims", " ".join(view["document_plan"]["interpretation_rules"]))
                    self.assertIn("Correct or omit", " ".join(view["document_plan"]["interpretation_rules"]))
                    self.assertEqual("schema_version" in view["document_plan"], planned)
                    self.assertEqual(view["review_findings_status"]["independent_verification"], "not_performed")
                    self.assertIn("do not manufacture", " ".join(view["narrative_context"]["writing_rules"]))
                    self.assertEqual(view["review_findings"], original["findings"])
                self.assertEqual(normal["review_findings_status"], corrected["review_findings_status"])
                from simple_ar.report.agent import _reviewer_context
                checked = _reviewer_context(context=context, memory=memory, section=section, template=template,
                    config=config, draft=baseline, previous_draft=baseline, revision_review=review)
                self.assertEqual(checked["revision_context"]["review_opinions_status"],
                    normal["review_findings_status"])
                self.assertEqual(checked["revision_context"]["target_findings"], original["findings"])
                self.assertEqual(normal["document_plan"], corrected["document_plan"])
                self.assertEqual(normal["document_plan"]["interpretation_rules"],
                                 checked["document_plan"]["interpretation_rules"])
                self.assertNotIn("sections", checked["document_plan"])
                self.assertNotIn(section.goal, json.dumps(checked))
        self.assertEqual(review.model_dump(mode="json"), original)
        self.assertEqual(baseline.draft_markdown, "Only the measured observation and its qualification are established.")

    def test_single_section_responses_cannot_relocate_explicit_target(self):
        from simple_ar.report.agent import _normalize_draft_response, _normalize_review_response
        context, config, section, memory, template = self.objects()
        bad_draft = {"section_id": "other", "draft_markdown": "A real-looking but misdirected body."}
        before = dict(bad_draft)
        with self.assertRaisesRegex(LLMResponseError, "not requested"):
            _normalize_draft_response(bad_draft, section)
        self.assertEqual(bad_draft, before)
        for response in ({"section_id": "other", "verdict": "pass"}, {"verdict": "pass", "findings": [
                {"section_id": "other", "message": "A misplaced opinion."}]}):
            with self.assertRaisesRegex(LLMResponseError, "not requested"):
                _normalize_review_response(response, section)
        self.assertEqual(_normalize_draft_response({"draft_markdown": "Observed only.", "heading": "Display heading"}, section)["section_id"], section.section_id)
        self.assertEqual(_normalize_review_response({"findings": [{"message": "Legacy observation."}]}, section)["findings"][0]["section_id"], section.section_id)
        self.assertEqual(_normalize_draft_response({"heading": "Display heading"}, section)["heading"], "Display heading")
        for identity in (None, ""):
            response = {"section_id": identity, "findings": [{"section_id": identity, "message": "Legacy observation."}]}
            normalized = _normalize_review_response(response, section)
            self.assertEqual(normalized["section_id"], section.section_id)
            self.assertEqual(normalized["findings"][0]["section_id"], section.section_id)
            self.assertIs(response["section_id"], identity)
            self.assertEqual(_normalize_draft_response({"section_id": identity}, section)["section_id"], section.section_id)
        for identity in (False, 0, " "):
            with self.assertRaisesRegex(LLMResponseError, "not requested"):
                _normalize_draft_response({"section_id": identity}, section)

    def test_wrong_section_review_uses_existing_one_format_correction(self):
        context, config, section, memory, template = self.objects()
        draft = ReportSectionDraft(section_id=section.section_id, heading=section.heading, draft_markdown="Observed only.")
        calls = []
        class Client:
            def ask_json(self, system, prompt, **kwargs):
                calls.append(prompt_payload(prompt))
                return {"section_id": "other" if len(calls) == 1 else section.section_id, "verdict": "pass"}
        reviewed = _review_section_with_recovery(client=Client(), context=context, memory=memory, section=section,
            template=template, config=config, draft=draft, label="target-review")
        self.assertEqual(len(calls), 2)
        self.assertEqual(reviewed.section_id, section.section_id)
        self.assertEqual(calls[0]["narrative_context"], calls[1]["narrative_context"])

    def test_normal_and_format_recovery_share_required_actions_not_opinion_messages(self):
        context, config, section, memory, template = self.objects()
        original = ReportSectionDraft(section_id=section.section_id, heading=section.heading, draft_markdown="Observed limitation only.")
        review = ReportSectionReview(section_id=section.section_id, verdict="revise_required",
            revision_instructions=["Retain attribution."], findings=[
                ReviewerFinding(finding_id="required", type="style", severity="minor", required_action="revise",
                    message="Repeated material", suggested_action="Remove repeated prose while retaining the qualification."),
                ReviewerFinding(finding_id="advice", type="style", severity="major", required_action="advisory",
                    message="Optional style", suggested_action="Add an optional heading."),
                ReviewerFinding(finding_id="verify", type="evidence_gap", severity="major", required_action="verify",
                    message="An unverified allegation", suggested_action="Replace the observed condition with this alleged one."),
                ReviewerFinding(finding_id="no_action", type="style", severity="major", required_action="revise",
                    message="This opinion is not itself a rewrite instruction.")])
        before = review.model_dump(mode="json")
        common = dict(context=context, memory=memory, section=section, config=config,
            previous_draft=original, review=review, draft_mode="section_revision")
        ordinary = prompt_payload(_writer_prompt(**common, template=template, extra_context=[], source_batch_index=1,
            source_batch_count=1, include_previous_draft=True))
        recovery = prompt_payload(_writer_recovery_prompt(**common))
        expected = ["Retain attribution.", "Remove repeated prose while retaining the qualification."]
        self.assertEqual(ordinary["review_instructions"], expected)
        self.assertEqual(recovery["review_instructions"], expected)
        self.assertEqual(ordinary["narrative_context"]["edit_scope"], recovery["narrative_context"]["edit_scope"])
        self.assertEqual(ordinary["narrative_context"]["edit_scope"]["eligible_section_ids"], [section.section_id])
        self.assertEqual([row["finding_id"] for row in ordinary["review_findings"]], ["required", "advice", "verify", "no_action"])
        self.assertEqual(review.model_dump(mode="json"), before)

    def test_revision_observation_counts_actual_candidate_without_forcing_shrinkage(self):
        from simple_ar.report.agent import _reviewer_context
        context, config, section, memory, template = self.objects()
        baseline = ReportSectionDraft(section_id=section.section_id, heading=section.heading, draft_markdown="One observed value.")
        candidate = baseline.model_copy(update={"draft_markdown": "One observed value with necessary qualification."})
        view = _reviewer_context(context=context, memory=memory, section=section, template=template, config=config,
            draft=candidate, revision_review=self.review(), previous_draft=baseline)
        contract = view["revision_context"]
        self.assertEqual(contract["length_observation"]["baseline_section_tokens"], 3)
        self.assertEqual(contract["length_observation"]["candidate_section_tokens"], 6)
        self.assertEqual(contract["length_observation"]["candidate_minus_baseline_tokens"], 3)
        self.assertIn("Remove the significance claim", " ".join(contract["effective_instructions"]))
        self.assertEqual(contract["revision_instructions"], self.review().revision_instructions)
        unknown = revision_context(self.review(), None, candidate=candidate)["length_observation"]
        self.assertIsNone(unknown["baseline_section_tokens"])
        self.assertIsNone(unknown["candidate_minus_baseline_tokens"])
        other = baseline.model_copy(update={"section_id": "different_target"})
        self.assertIsNone(revision_context(self.review(), other, candidate=candidate)["length_observation"]["candidate_minus_baseline_tokens"])

    def test_all_findings_retained_but_only_two_highest_priority_targets_revised(self):
        context, config, section, memory, template = self.objects(document_review=True)
        memory.section_plan.extend([ReportSectionPlan(section_id=sid, heading=sid, goal="Facts")
                                    for sid in ("body", "third")])
        labels = []
        class Client:
            def ask_json(client, system, prompt, *, label='', **kwargs):
                labels.append(label)
                if label == 'report-document-reviewer':
                    return {'section_reviews': [{'section_id': sid, 'verdict': 'revise_required',
                        'findings': [{'finding_id': sid + '-defect', 'type': 'unsupported_claim',
                                      'severity': level, 'required_action': 'revise',
                                      'message': 'A concrete qualification is missing.',
                                      'draft_quotes': draft_quotes(prompt, sid)}]}
                        for sid, level in [('conclusion', 'minor'), ('body', 'major'), ('third', 'critical')]]}
                if label == 'report-document-verifier':
                    return {'section_reviews': []}
                if 'reviewer' in label or 'verifier' in label:
                    return {'verdict': 'pass'}
                sid = next(sid for sid in ('conclusion', 'body', 'third') if label.endswith(sid))
                return {'section_id': sid, 'draft_markdown': 'Qualified evidence.'}
        result = run_report_agent(client=Client(), context=context, memory=memory, config=config,
                                  template=template, gateway=ReportToolGateway(context))
        self.assertEqual([row.section_id for row in result.iterations if row.action == 'document_revise'],
                         ['third', 'body'])
        self.assertNotIn('report-document-reviser-conclusion', labels)
        self.assertTrue(any(row.finding_id == 'conclusion-defect' for row in result.memory.reviewer_findings))
        self.assertTrue(any(row.type == 'document_revision_unresolved' for row in result.memory.reviewer_findings))
        self.assertFalse(any(row.type == 'document_review_unavailable' for row in result.memory.reviewer_findings))

    def objects(self, **settings):
        context = ReportContext(topic='Supplied observations', report_mode='supplied_materials')
        config = ReportRuntimeConfig(template='analysis_report', max_review_iterations=1, **settings)
        section = ReportSectionPlan(section_id='conclusion', heading='Conclusion', goal='Summarize without duplicated details')
        memory = ReportMemory(section_plan=[section])
        template = load_report_template_bundle(report_mode=context.report_mode, config=config)
        return context, config, section, memory, template

    def review(self, prompt=None):
        return ReportSectionReview(section_id='conclusion', verdict='revise_required', findings=[
            ReviewerFinding(finding_id='f1', type='unsupported_claim', severity='major',
                            message='The draft turns descriptive observations into a significance claim.',
                            suggested_action='Remove the significance claim; retain the evidence limit.',
                            draft_quotes=draft_quotes(prompt, 'conclusion') if prompt else [])],
            revision_instructions=['Remove the repeated table.', 'Keep the source attribution and qualification.'])

    def test_ordinary_and_recovery_writer_can_remove_wrong_or_repeated_content(self):
        context, config, section, memory, template = self.objects()
        original = ReportSectionDraft(section_id=section.section_id, heading=section.heading, draft_markdown='Repeated text. ' * 200)
        common = dict(context=context, memory=memory, section=section, config=config,
                      previous_draft=original, review=self.review(), draft_mode='section_revision')
        prompts = [_writer_prompt(**common, template=template, extra_context=[], source_batch_index=1,
                                  source_batch_count=1, include_previous_draft=True), _writer_recovery_prompt(**common)]
        for prompt in prompts:
            row = prompt_payload(prompt)
            self.assertNotIn('at least about', row['revision_preservation_requirement'])
            self.assertIn('Remove unsupported or duplicated', row['revision_preservation_requirement'])
            self.assertEqual(row['review_findings'][0]['finding_id'], 'f1')
            self.assertIn('Keep the source attribution and qualification.', row['review_instructions'])

    def test_verifier_and_format_retry_receive_exact_request_and_bounded_baseline(self):
        context, config, section, memory, template = self.objects()
        original = ReportSectionDraft(section_id=section.section_id, heading=section.heading,
                                      draft_markdown='Start ' + '中' * 8000 + ' End')
        revised = original.model_copy(update={'draft_markdown': 'Observations only; significance is unknown.'})
        seen = []
        class Client:
            def ask_json(self, system, prompt, **kwargs):
                seen.append(prompt_payload(prompt))
                if len(seen) == 1:
                    raise LLMResponseError('bad outer object')
                return {'section_id': 'conclusion', 'verdict': 'pass'}
        _review_section_with_recovery(client=Client(), context=context, template=template, memory=memory,
            section=section, draft=revised, config=config, label='verify', revision_review=self.review(), previous_draft=original)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0]['revision_context'], seen[1]['revision_context'])
        contract = seen[0]['revision_context']
        self.assertEqual(contract['revision_instructions'], self.review().revision_instructions)
        self.assertEqual(contract['target_findings'][0]['finding_id'], 'f1')
        self.assertGreater(contract['original_section']['prose_characters_omitted'], 0)
        self.assertEqual(contract['original_section']['position_unit'], 'unicode_characters')

    def test_interrupted_candidate_restores_request_and_does_not_reset_revision_limit(self):
        context, config, section, memory, template = self.objects()
        checkpoints, labels = [], []
        class Client:
            resumed = False
            def ask_json(client, system, prompt, *, label='', **kwargs):
                labels.append(label)
                row = prompt_payload(prompt)
                if 'reviewer' in label:
                    if client.resumed:
                        self.assertEqual(row['revision_context']['revision_instructions'], self.review().revision_instructions)
                        self.assertIn('Old significance claim.', row['revision_context']['original_section']['prose_windows'][0]['text'])
                        # Even if still bad, do not grant another revision on recovery.
                    return self.review(prompt).model_dump(mode='json')
                return {'section_id': 'conclusion', 'heading': 'Conclusion',
                        'draft_markdown': 'Candidate still unqualified.' if 'reviser' in label else 'Old significance claim.'}
        client = Client()
        def checkpoint(row):
            checkpoints.append(row)
            if any(event['action'] == 'revise' for event in row['iterations']):
                raise LLMError('Interrupted after persisting candidate')
        kwargs = dict(client=client, context=context, memory=memory, config=config,
                      template=template, gateway=ReportToolGateway(context))
        with self.assertRaises(LLMError):
            run_report_agent(**kwargs, checkpoint_sink=checkpoint)
        client.resumed = True
        result = run_report_agent(**kwargs, completed_checkpoint=checkpoints[-1])
        self.assertEqual(sum('reviser' in label for label in labels), 1)
        self.assertEqual(sum(row.action == 'revise' for row in result.iterations), 1)
        self.assertTrue(any(row.finding_id == 'f1' for row in result.memory.reviewer_findings))

    def test_historical_events_without_new_field_do_not_invent_instructions(self):
        draft = ReportSectionDraft(section_id='conclusion', heading='Conclusion', draft_markdown='Candidate')
        events = [ReportIterationRecord(iteration=1, section_id='conclusion', action='review',
                                        status='revise_required', findings=self.review().findings),
                  ReportIterationRecord(iteration=2, section_id='conclusion', action='revise', status='revised', draft=draft)]
        review, baseline = pending_revision_review(events, 'conclusion')
        self.assertEqual(review.revision_instructions, [])
        self.assertIsNone(baseline)
        self.assertEqual(revision_context(review, baseline)['original_section'], {})

    def test_interrupted_editor_reuses_candidate_and_original_request(self):
        context, config, section, memory, template = self.objects(document_review=True)
        memory.section_plan.append(ReportSectionPlan(section_id='body', heading='Observations', goal='Facts'))
        checkpoints, labels = [], []
        class Client:
            resumed = False
            def ask_json(client, system, prompt, *, label='', **kwargs):
                labels.append(label)
                if label == 'report-document-reviewer':
                    review = self.review(prompt)
                    if client.resumed:
                        review.revision_instructions = ['A different new suggestion; do not overwrite saved request.']
                    return {'section_reviews': [review.model_dump(mode='json')]}
                if label == 'report-document-verifier':
                    return {'section_reviews': []}
                if label == 'report-document-verifier-conclusion':
                    self.assertEqual(prompt_payload(prompt)['revision_context']['revision_instructions'], self.review().revision_instructions)
                    return {'section_id': 'conclusion', 'verdict': 'pass'}
                if 'reviewer' in label:
                    return {'verdict': 'pass'}
                sid = 'body' if 'body' in label else 'conclusion'
                return {'section_id': sid, 'heading': sid, 'draft_markdown':
                        'Saved corrected candidate.' if 'reviser' in label else 'Original evidence.'}
        client = Client()
        def checkpoint(row):
            checkpoints.append(row)
            if any(event['action'] == 'document_revise' for event in row['iterations']):
                raise RuntimeError('Interrupted immediately after candidate save')
        kwargs = dict(client=client, context=context, template=template, memory=memory, config=config,
                      gateway=ReportToolGateway(context))
        with self.assertRaises(RuntimeError):
            run_report_agent(**kwargs, checkpoint_sink=checkpoint)
        client.resumed = True
        result = run_report_agent(**kwargs, completed_checkpoint=checkpoints[-1])
        self.assertEqual(labels.count('report-document-reviser-conclusion'), 1)
        self.assertEqual(sum(row.action == 'document_revise' for row in result.iterations), 1)
        self.assertIn('Saved corrected candidate.', result.report_body)
        self.assertTrue(next(row for row in result.iterations if row.action == 'document_revise').adopted)

    def test_document_verifier_checks_original_finding_and_can_keep_original(self):
        context, config, section, memory, template = self.objects(document_review=True)
        memory.section_plan.append(ReportSectionPlan(section_id='body', heading='Observations', goal='State facts'))
        targets = []
        class Client:
            def ask_json(client, system, prompt, *, label='', **kwargs):
                if label == 'report-document-reviewer':
                    return {'section_reviews': [self.review(prompt).model_dump(mode='json')]}
                if label == 'report-document-verifier-conclusion':
                    row = prompt_payload(prompt)
                    targets.append(row['revision_context'])
                    self.assertEqual(row['revision_context']['target_findings'][0]['finding_id'], 'f1')
                    return self.review(prompt).model_dump(mode='json')
                if 'reviewer' in label:
                    return {'verdict': 'pass'}
                sid = 'body' if 'body' in label else 'conclusion'
                return {'section_id': sid, 'heading': sid, 'draft_markdown':
                        'Still invalid replacement.' if 'reviser' in label else 'Original evidence-bounded section.'}
        result = run_report_agent(client=Client(), context=context, template=template, memory=memory,
                                  config=config, gateway=ReportToolGateway(context))
        self.assertEqual(len(targets), 1)
        self.assertNotIn('Still invalid replacement.', result.report_body)
        candidate = next(row for row in result.iterations if row.action == 'document_revise')
        self.assertFalse(candidate.adopted)
        self.assertIn('Still invalid replacement.', candidate.draft.draft_markdown)

    def test_saved_document_source_context_is_reused_without_double_consumption(self):
        context, config, section, memory, template = self.objects(document_review=True)
        memory.section_plan.append(ReportSectionPlan(section_id='body', heading='Observations', goal='Facts'))
        checkpoints, labels = [], []
        class Client:
            resumed = False
            def ask_json(client, system, prompt, *, label='', **kwargs):
                labels.append(label)
                if label == 'report-document-reviewer':
                    return {'section_reviews': [self.review(prompt).model_dump(mode='json')]}
                if label == 'report-document-verifier':
                    return {'section_reviews': []}
                if label == 'report-document-verifier-conclusion':
                    if client.resumed:
                        self.assertIn('Saved original qualification', prompt)
                        return {'verdict': 'pass'}
                    return {'verdict': 'pass', 'context_requests': [
                        {'tool_name': 'get_synthesis_brief', 'arguments': {}}]}
                if 'reviewer' in label:
                    return {'verdict': 'pass'}
                sid = 'body' if 'body' in label else 'conclusion'
                return {'section_id': sid, 'draft_markdown': 'Bounded descriptive account.'}
        client = Client()
        gateway = ReportToolGateway(context)
        gateway.call = lambda request: ReportToolResult(tool_name=request.tool_name,
            content={'text': 'Saved original qualification'})
        def checkpoint(row):
            checkpoints.append(row)
            if any(event['action'] == 'document_context' for event in row['iterations']):
                raise RuntimeError('Interrupted after saving verification evidence')
        kwargs = dict(client=client, context=context, template=template, memory=memory, config=config)
        with self.assertRaises(RuntimeError):
            run_report_agent(**kwargs, gateway=gateway, checkpoint_sink=checkpoint)
        client.resumed = True
        resumed_gateway = ReportToolGateway(context)
        def unexpected_lookup(request):
            self.fail('The saved evidence must not be fetched again')
        resumed_gateway.call = unexpected_lookup
        result = run_report_agent(**kwargs, gateway=resumed_gateway, completed_checkpoint=checkpoints[-1])
        self.assertEqual(labels.count('report-document-reviser-conclusion'), 1)
        self.assertEqual(len(result.tool_results), 1)
        self.assertEqual(resumed_gateway.call_counts['get_synthesis_brief'], 1)
        self.assertTrue(next(row for row in result.iterations if row.action == 'document_revise').adopted)

    def test_exhausted_editor_allowance_is_not_reset_by_resume(self):
        context, config, section, memory, template = self.objects(document_review=True)
        memory.section_plan.append(ReportSectionPlan(section_id='body', heading='Observations', goal='Facts'))
        checkpoints, labels = [], []
        class Client:
            resumed = False
            def ask_json(client, system, prompt, *, label='', **kwargs):
                labels.append(label)
                if label == 'report-document-reviewer':
                    return {'section_reviews': [self.review(prompt).model_dump(mode='json')]}
                if label == 'report-document-verifier-conclusion':
                    return self.review(prompt).model_dump(mode='json')
                if 'reviewer' in label:
                    return {'verdict': 'pass'}
                if client.resumed and 'reviser' in label:
                    self.fail('A previously rejected editor candidate must not earn another correction')
                sid = 'body' if 'body' in label else 'conclusion'
                return {'section_id': sid, 'draft_markdown': 'Original bounded account.'}
        client = Client()
        def checkpoint(row):
            checkpoints.append(row)
            if row['document_review_done']:
                # Emulate interruption at the immediately preceding checkpoint
                # boundary, after the candidate verification was recorded.
                row['document_review_done'] = False
                raise RuntimeError('Interrupted before final editor checkpoint')
        kwargs = dict(client=client, context=context, template=template, memory=memory,
                      config=config, gateway=ReportToolGateway(context))
        with self.assertRaises(RuntimeError):
            run_report_agent(**kwargs, checkpoint_sink=checkpoint)
        client.resumed = True
        result = run_report_agent(**kwargs, completed_checkpoint=checkpoints[-1])
        self.assertEqual(labels.count('report-document-reviser-conclusion'), 1)
        self.assertEqual(sum(row.action == 'document_revise' for row in result.iterations), 1)
        self.assertTrue(any(row.type == 'document_revision_unresolved' for row in result.memory.reviewer_findings))

    def test_editor_continues_rejected_candidate_with_original_and_new_defects(self):
        for interrupt_after in (None, 'document_verify', 'second_candidate'):
            with self.subTest(interrupt_after=interrupt_after):
                context, config, section, memory, template = self.objects(document_review=True)
                config.max_review_iterations = 2
                memory.section_plan.append(ReportSectionPlan(section_id='body', heading='Observations', goal='Facts'))
                checkpoints, labels, contracts = [], [], []
                class Client:
                    revisions = 0
                    def ask_json(client, system, prompt, *, label='', **kwargs):
                        labels.append(label)
                        if label == 'report-document-reviewer':
                            return {'section_reviews': [self.review(prompt).model_dump(mode='json')]}
                        if label == 'report-document-verifier':
                            return {'section_reviews': []}
                        if label == 'report-document-verifier-conclusion':
                            contract = prompt_payload(prompt)['revision_context']
                            self.assertTrue(any(row['finding_id'] == 'f1' for row in contract['target_findings']))
                            if client.revisions == 1:
                                return {'section_id': 'conclusion', 'verdict': 'revise_required',
                                        'findings': [{'finding_id': 'citation-1', 'type': 'citation_misuse',
                                                      'severity': 'minor', 'message': 'Necessary citation lost.',
                                                      'draft_quotes': draft_quotes(prompt, 'conclusion')}],
                                        'revision_instructions': ['Restore the nearby citation.']}
                            self.assertIn('Restore the nearby citation.', contract['revision_instructions'])
                            return {'verdict': 'pass'}
                        if 'reviewer' in label:
                            return {'verdict': 'pass'}
                        sid = 'body' if 'body' in label else 'conclusion'
                        if 'document-reviser' in label:
                            client.revisions += 1
                            row = prompt_payload(prompt)
                            contracts.append(row)
                            if client.revisions == 2:
                                self.assertIn('Restore the nearby citation.', row['review_instructions'])
                                self.assertTrue(any(f['finding_id'] == 'f1' for f in row['review_findings']))
                                self.assertTrue(any(f['finding_id'] == 'citation-1' for f in row['review_findings']))
                                self.assertIn('Descriptive candidate missing citation.', prompt)
                            text = ('Descriptive candidate missing citation.' if client.revisions == 1
                                    else 'Descriptive candidate with source attribution.')
                        else:
                            text = 'Original account.'
                        return {'section_id': sid, 'draft_markdown': text}
                client = Client()
                def checkpoint(row):
                    checkpoints.append(row)
                    candidates = [event for event in row['iterations'] if event['action'] == 'document_revise']
                    stop = ((interrupt_after == 'document_verify' and any(
                        event['action'] == 'document_verify' for event in row['iterations']))
                        or (interrupt_after == 'second_candidate' and len(candidates) == 2))
                    if stop:
                        raise RuntimeError('Interrupted at persisted editor boundary')
                kwargs = dict(client=client, context=context, template=template, memory=memory,
                              config=config, gateway=ReportToolGateway(context))
                if interrupt_after:
                    with self.assertRaises(RuntimeError):
                        run_report_agent(**kwargs, checkpoint_sink=checkpoint)
                    result = run_report_agent(**kwargs, completed_checkpoint=checkpoints[-1])
                else:
                    result = run_report_agent(**kwargs, checkpoint_sink=checkpoint)
                self.assertEqual(client.revisions, 2)
                candidates = [event for event in result.iterations if event.action == 'document_revise']
                self.assertEqual([event.adopted for event in candidates], [False, True])
                self.assertIn('Descriptive candidate with source attribution.', result.report_body)
                self.assertNotIn('Descriptive candidate missing citation.', result.report_body)
                self.assertFalse(any(finding.finding_id in {'f1', 'citation-1'}
                                     for finding in result.memory.reviewer_findings))

    def test_editor_two_failed_candidates_exhaust_allowance_even_after_recovery(self):
        context, config, section, memory, template = self.objects(document_review=True)
        config.max_review_iterations = 2
        memory.section_plan.append(ReportSectionPlan(section_id='body', heading='Observations', goal='Facts'))
        checkpoints, labels = [], []
        class Client:
            def ask_json(client, system, prompt, *, label='', **kwargs):
                labels.append(label)
                if label == 'report-document-reviewer':
                    return {'section_reviews': [self.review(prompt).model_dump(mode='json')]}
                if label == 'report-document-verifier-conclusion':
                    return self.review(prompt).model_dump(mode='json')
                if 'reviewer' in label:
                    return {'verdict': 'pass'}
                return {'section_id': 'body' if 'body' in label else 'conclusion',
                        'draft_markdown': 'Rejected candidate.' if 'document-reviser' in label else 'Original account.'}
        kwargs = dict(client=Client(), context=context, template=template, memory=memory,
                      config=config, gateway=ReportToolGateway(context))
        def checkpoint(row):
            checkpoints.append(row)
            if row['document_review_done']:
                row['document_review_done'] = False
                raise RuntimeError('Interrupted before final completion save')
        with self.assertRaises(RuntimeError):
            run_report_agent(**kwargs, checkpoint_sink=checkpoint)
        result = run_report_agent(**kwargs, completed_checkpoint=checkpoints[-1])
        self.assertEqual(labels.count('report-document-reviser-conclusion'), 2)
        self.assertNotIn('Rejected candidate.', result.report_body)
        self.assertEqual(sum(event.action == 'document_revise' for event in result.iterations), 2)
        self.assertTrue(any(f.type == 'document_revision_unresolved' for f in result.memory.reviewer_findings))

    def test_editor_recovery_cannot_open_a_third_target_section(self):
        context, config, section, memory, template = self.objects(document_review=True)
        config.max_review_iterations = 2
        memory.section_plan.extend([ReportSectionPlan(section_id=sid, heading=sid, goal='Facts')
                                    for sid in ('body', 'third')])
        labels, checkpoints = [], []
        class Client:
            resumed = False
            def ask_json(client, system, prompt, *, label='', **kwargs):
                labels.append(label)
                if label == 'report-document-reviewer':
                    ids = ['third'] if client.resumed else ['conclusion', 'body']
                    return {'section_reviews': [{'section_id': sid, 'verdict': 'revise_required',
                            'findings': [{'finding_id': sid + '-attribution', 'type': 'citation_misuse',
                                          'severity': 'major', 'message': 'Missing source attribution.',
                                          'draft_quotes': draft_quotes(prompt, sid)}],
                            'revision_instructions': ['Clarify source attribution.']} for sid in ids]}
                if label == 'report-document-verifier':
                    return {'section_reviews': []}
                if 'reviewer' in label or 'verifier' in label:
                    return {'verdict': 'pass'}
                if client.resumed and 'reviser' in label:
                    self.fail('Recovery cannot select a third editor target')
                sid = next(sid for sid in ('conclusion', 'body', 'third') if sid in label)
                return {'section_id': sid, 'draft_markdown': 'Supported section.'}
        client = Client()
        def checkpoint(row):
            checkpoints.append(row)
            if row['document_review_done']:
                row['document_review_done'] = False
                raise RuntimeError('Interrupted before completion save')
        kwargs = dict(client=client, context=context, template=template, memory=memory,
                      config=config, gateway=ReportToolGateway(context))
        with self.assertRaises(RuntimeError):
            run_report_agent(**kwargs, checkpoint_sink=checkpoint)
        client.resumed = True
        result = run_report_agent(**kwargs, completed_checkpoint=checkpoints[-1])
        self.assertEqual(sum(event.action == 'document_revise' for event in result.iterations), 2)
        self.assertTrue(any(f.type == 'document_revision_unresolved' and f.section_id == 'third'
                            for f in result.memory.reviewer_findings))


if __name__ == '__main__':
    unittest.main()
