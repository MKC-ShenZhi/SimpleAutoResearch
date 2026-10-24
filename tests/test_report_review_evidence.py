"""Review anchors locate supplied evidence; they do not certify its meaning."""
import json
import unittest

from tests.report_review_fixtures import document_review_inputs, prompt_payload

from simple_ar.integrations.llm import LLMError, LLMResponseError
from simple_ar.report.editor import review_document
from simple_ar.report.agent import _section_response
from simple_ar.report.review_evidence import validate_evidence_quotes, review_evidence_locator, _resolve_pointer
from simple_ar.report.schema import (
    ReportContext, ReportDraftQuote, ReportEvidenceQuote, ReportFindingCheck, ReportMemory, ReportRuntimeConfig,
    ReportSectionDraft, ReportSectionPlan, ReportSectionReview, ReportToolResult, ReviewerFinding, SourceHandle,
)
from simple_ar.report.templates import load_report_template_bundle


class ReviewEvidenceTests(unittest.TestCase):
    @staticmethod
    def profile_artifact():
        from simple_ar.report.review_evidence import SCOPED_DELIVERY_INSPECTION
        return {'path': 'source/data.csv', 'sha256': 'a' * 64, 'producer': {'owner': 'original-run'},
            'inspection_contract': SCOPED_DELIVERY_INSPECTION, 'coverage': 'table_profile',
            'profile': {'source_sha256': 'a' * 64, 'row_count': 2, 'columns': ['value'],
                'column_summary': [{'column': 'value', 'column_name_truncated': False,
                    'missing': 1, 'distinct_nonmissing': 1}],
                'example_rows': [{'value': '1'}, {'value': ''}], 'columns_omitted': 0,
                'column_names_truncated': False, 'cell_values_truncated': False,
                'status': 'counts cover the complete bounded table; meanings and units are not verified'}}

    def test_scoped_profile_review_preserves_responsibility_gaps_and_existing_defects(self):
        from simple_ar.report.review_evidence import (
            DELIVERY_REVIEW_CONTRACT, SCOPED_DELIVERY_INSPECTION, delivery_review_result,
            delivery_subject_view, attachment_review_blocks_editing,
        )
        row = self.profile_artifact()
        snapshot = {'contract': DELIVERY_REVIEW_CONTRACT, 'artifacts': [row],
            'text_admission': {'contract': SCOPED_DELIVERY_INSPECTION}}
        result = delivery_review_result(snapshot, [])
        self.assertEqual(result['status'], 'reviewed')
        self.assertEqual(result['uninspected'], [])
        self.assertFalse(attachment_review_blocks_editing(result))
        self.assertIn('not all table values', result['scope'])
        self.assertTrue(result['scope_limits'])
        profiled, = result['profiled_artifacts']
        self.assertEqual((profiled['path'], profiled['sha256']), (row['path'], row['sha256']))
        self.assertEqual(profiled['reader_coverage']['row_count'], 2)
        self.assertIn('pairing', profiled['unverified'])
        self.assertIn('profile_scope', result['artifacts'][0])
        self.assertNotIn('profile', result['artifacts'][0])
        self.assertEqual(delivery_subject_view(row),
            {key: row[key] for key in ('path', 'sha256', 'producer')})
        gap = {'path': 'source/binary', 'sha256': 'b' * 64, 'coverage': 'unsupported',
            'inspection_contract': SCOPED_DELIVERY_INSPECTION}
        incomplete = delivery_review_result({**snapshot, 'artifacts': [row, gap]}, [])
        self.assertEqual(incomplete['status'], 'incomplete')
        self.assertEqual([item['path'] for item in incomplete['uninspected']], [gap['path']])
        self.assertTrue(attachment_review_blocks_editing(incomplete))
        # An unchanged version does not lose its original full-text defect when
        # the current presentation switches to a bounded profile.
        previous = {'type': 'delivery_artifact_defect',
            'finding': {'delivery_targets': ['/delivery_artifacts/0/text'], 'message': 'Original defect'},
            'targets': [delivery_subject_view(row)]}
        retained = delivery_review_result({**snapshot, 'prior_defects': [previous]}, [])
        self.assertEqual(retained['defects'], [previous])
        self.assertEqual(retained['status'], 'needs_revision')
        self.assertTrue(attachment_review_blocks_editing(retained))
        self.assertTrue(attachment_review_blocks_editing({**retained, 'status': 'reviewed'}))

    def test_historical_status_only_admission_is_not_tightened_by_scoped_review(self):
        from simple_ar.report.review_evidence import (
            SCOPED_DELIVERY_INSPECTION, attachment_review_blocks_editing,
        )
        historical = {'defects': [{'finding': 'retained'}], 'uninspected': [{'path': 'old'}]}
        self.assertFalse(attachment_review_blocks_editing(historical))
        self.assertFalse(attachment_review_blocks_editing({**historical, 'status': 'reviewed'}))
        self.assertTrue(attachment_review_blocks_editing({**historical, 'status': 'incomplete'}))
        self.assertFalse(attachment_review_blocks_editing(
            {**historical, 'inspection_contract': 'professional_unit_text.v2'}))
        self.assertTrue(attachment_review_blocks_editing(
            {**historical, 'inspection_contract': SCOPED_DELIVERY_INSPECTION}))
        self.assertTrue(attachment_review_blocks_editing({**historical, 'status': 'reviewed',
            'artifacts': [{'inspection_contract': SCOPED_DELIVERY_INSPECTION}]}))

    def test_profile_targets_and_scalar_counterevidence_use_real_view_and_roles(self):
        from simple_ar.report.review_evidence import (
            DELIVERY_REVIEW_CONTRACT, SHARED_PRODUCER_LOCATOR_CONTRACT,
            COMPARISON_VERIFICATION_CONTRACT, project_delivery_producers, restore_delivery_producers,
            resolve_review_evidence, validate_finding_anchors, delivery_finding_targets,
        )
        row = self.profile_artifact()
        view = {'delivery_review_contract': DELIVERY_REVIEW_CONTRACT,
            'evidence_navigation_contract': SHARED_PRODUCER_LOCATOR_CONTRACT,
            'comparison_verification_contract': COMPARISON_VERIFICATION_CONTRACT,
            **project_delivery_producers([row]), 'metric_sources': {'expected_missing': 0}}
        raw = {'finding_id': 'missing-values', 'type': 'metric_mismatch', 'section_id': 'document',
            'severity': 'major', 'message': 'Missingness differs from original registered result.',
            'required_action': 'revise', 'delivery_targets': ['/delivery_artifacts/0/profile'],
            'evidence_quotes': [{'pointer': '/delivery_artifacts/0/profile/column_summary/0/missing'},
                                {'pointer': '/metric_sources/expected_missing'}]}
        resolved = resolve_review_evidence({'findings': [raw]}, view)['findings'][0]
        finding = ReviewerFinding.model_validate(resolved)
        validate_finding_anchors(ReportSectionReview(section_id='document', findings=[finding]), {}, view)
        self.assertEqual(finding.type, 'metric_mismatch')
        targets = delivery_finding_targets(finding, restore_delivery_producers(view))
        self.assertEqual(targets, [row])
        self.assertNotIn('text', targets[0])
        for suffix, role in (('row_count', 'registered_result'),
                ('example_rows/0/value', 'registered_result'),
                ('column_summary/0/distinct_nonmissing', 'registered_result'),
                ('source_sha256', 'derived_context'), ('status', 'derived_context'),
                ('columns_omitted', 'derived_context'), ('cell_values_truncated', 'derived_context')):
            selected = resolve_review_evidence({'findings': [{'evidence_quotes': [
                {'pointer': '/delivery_artifacts/0/profile/' + suffix}]}]}, view)['findings'][0]
            quote = ReportEvidenceQuote.model_validate(selected['evidence_quotes'][0])
            self.assertEqual(quote.role, role)
            validate_evidence_quotes([quote], view)
        own_only = {**raw, 'evidence_quotes': raw['evidence_quotes'][:1]}
        pending = resolve_review_evidence({'findings': [own_only]}, view)['findings'][0]
        self.assertEqual((pending['type'], pending['required_action']), ('evidence_gap', 'verify'))
        prose = {**own_only, 'delivery_targets': [],
            'draft_quotes': [{'section_id': 'document', 'quote': 'No missing values.'}]}
        prose_finding = ReviewerFinding.model_validate(
            resolve_review_evidence({'findings': [prose]}, view)['findings'][0])
        self.assertEqual(prose_finding.type, 'metric_mismatch')
        validate_finding_anchors(ReportSectionReview(section_id='document', findings=[prose_finding]),
            {'document': 'No missing values.'}, view)
        with self.assertRaises(LLMResponseError):
            resolve_review_evidence({'findings': [{'evidence_quotes': [{
                'pointer': '/delivery_artifacts/0/profile/row_count', 'role': 'executor_record'}]}]}, view)
        # The scalar quote API never admits the complete profile as raw text.
        with self.assertRaises(LLMResponseError):
            resolve_review_evidence({'findings': [{'evidence_quotes': [
                {'pointer': '/delivery_artifacts/0/profile'}]}]}, view)
        for pointer in ('/delivery_artifacts/0/profile/row_count', '/delivery_artifacts/0/profile/0',
                '/delivery_artifacts/00/profile', '/delivery_artifacts/1/profile',
                '/delivery_artifacts/0/text', '/delivery_artifacts/0/producer'):
            with self.subTest(pointer=pointer), self.assertRaises(LLMResponseError):
                delivery_finding_targets(finding.model_copy(update={'delivery_targets': [pointer]}), [row])

        _, memory, config, draft = self.objects()
        captured = []
        class Client:
            def ask_json(self, system, prompt, **ignored):
                captured.append(json.loads(prompt))
                return {'section_reviews': []}
        review_document(client=Client(), **document_review_inputs(memory, config, [draft]),
            delivery_review={'contract': DELIVERY_REVIEW_CONTRACT, 'artifacts': [row]},
            evidence_navigation_contract=SHARED_PRODUCER_LOCATOR_CONTRACT)
        supplied = captured[-1]
        self.assertEqual(restore_delivery_producers(supplied), [row])
        self.assertIn('/profile', supplied['output_schema']['section_reviews'][0]['findings'][0]['delivery_targets'][0])
        self.assertIn('not its full text', ' '.join(supplied['focus']))
        self.assertIn('/delivery_artifacts/0/profile', ' '.join(supplied['focus']))
        self.assertNotIn('only /delivery_artifacts/<index>/text', ' '.join(supplied['focus']))

    def test_bad_or_legacy_profiles_do_not_gain_inspection_authority(self):
        from copy import deepcopy
        from simple_ar.report.review_evidence import (
            DELIVERY_REVIEW_CONTRACT, SCOPED_DELIVERY_INSPECTION, delivery_target_pointer,
            delivery_finding_targets, delivery_review_result, resolve_review_evidence,
        )
        row = self.profile_artifact()
        finding = ReviewerFinding(finding_id='profile', type='evidence_gap', severity='major',
            message='Inspect source.', required_action='verify',
            delivery_targets=['/delivery_artifacts/0/profile'])
        invalid = []
        for contract in (None, 'professional_unit_text.v1', 'professional_unit_text.v2'):
            invalid.append({**row, 'inspection_contract': contract})
        for changes in ({'source_sha256': 'b' * 64}, {'row_count': True}, {'column_summary': []},
                {'column_summary': [{'column': 'value', 'column_name_truncated': False,
                    'missing': 3, 'distinct_nonmissing': 1}]},
                {'example_rows': [{'value': {'forged': 1}}, {'value': ''}]},
                {'scope': 'all scientific claims verified'}):
            invalid.append({**row, 'profile': {**deepcopy(row['profile']), **changes}})
        invalid.append({**row, 'profile': {'source_sha256': row['sha256'], 'row_count': 2}})
        for bad in invalid:
            with self.subTest(bad=bad):
                self.assertIsNone(delivery_target_pointer(bad, 0))
                with self.assertRaises(LLMResponseError):
                    delivery_finding_targets(finding, [bad])
                result = delivery_review_result({'contract': DELIVERY_REVIEW_CONTRACT,
                    'artifacts': [bad], 'text_admission': {'contract': SCOPED_DELIVERY_INSPECTION}}, [])
                self.assertEqual(result['status'], 'incomplete')
                self.assertEqual(result['profiled_artifacts'], [])
                self.assertEqual(len(result['uninspected']), 1)
                view = {'delivery_review_contract': DELIVERY_REVIEW_CONTRACT, 'delivery_artifacts': [bad]}
                quote = resolve_review_evidence({'findings': [{'evidence_quotes': [
                    {'pointer': '/delivery_artifacts/0/profile/row_count'}]}]}, view)['findings'][0]
                self.assertEqual(quote['evidence_quotes'][0]['role'], 'derived_context')
        legacy = {**row, 'inspection_contract': 'professional_unit_text.v2',
            'coverage': 'complete', 'text': 'value\n1\n'}
        self.assertEqual(delivery_target_pointer(legacy, 0), '/delivery_artifacts/0/text')
        legacy_result = delivery_review_result({'contract': DELIVERY_REVIEW_CONTRACT,
            'artifacts': [legacy]}, [])
        self.assertEqual(legacy_result['status'], 'reviewed')
        self.assertNotIn('profiled_artifacts', legacy_result)

    def test_shared_delivery_producers_roundtrip_preserves_full_records_and_source_identity(self):
        from copy import deepcopy
        from simple_ar.report.review_evidence import (
            SHARED_PRODUCER_LOCATOR_CONTRACT, project_delivery_producers, restore_delivery_producers,
        )
        producer = {'owner': 'run/one', 'nested': {'~/': ['完整原记录', None, False, 0]},
            'execution': {'stdout': 'original scientific text\n' * 500}, 'unknown_field': {'keep': True}}
        artifacts = [
            {'path': 'source/a.csv', 'sha256': 'same', 'text': 'α,1\n', 'producer': producer,
             'coverage': 'complete', 'consumers': [{'path': 'consumer/a'}], 'extra': [1, 2],
             'copy_contract': 'delivery_copies.v1', 'copies': [{'path': 'copy/a.csv', 'sha256': 'same'}]},
            {'path': 'source/b.csv', 'sha256': 'same', 'text': 'α,1\n', 'producer': deepcopy(producer),
             'coverage': 'complete'},
            {'path': 'source/c.csv', 'sha256': 'same', 'text': 'α,1\n',
             'producer': {**producer, 'owner': 'run/two'}, 'coverage': 'complete'},
            *[{'path': f'false/{i}', 'producer': value} for i, value in enumerate((None, {}, '', False, 0, True, 1))],
            {'path': 'no-producer', 'text': 'still supplied'},
        ]
        original = deepcopy(artifacts)
        projection = project_delivery_producers(artifacts)
        view = {'evidence_navigation_contract': SHARED_PRODUCER_LOCATOR_CONTRACT, **projection}
        # Roundtrip crosses the actual JSON boundary, not Python aliases.
        self.assertEqual(restore_delivery_producers(json.loads(json.dumps(view))), original)
        self.assertEqual(artifacts, original)
        self.assertEqual(len(projection['delivery_artifacts']), len(artifacts))
        self.assertEqual(len(projection['delivery_producers']), 9)
        self.assertEqual(projection['delivery_artifacts'][0]['producer'],
            projection['delivery_artifacts'][1]['producer'])
        self.assertNotEqual(projection['delivery_artifacts'][1]['producer'],
            projection['delivery_artifacts'][2]['producer'])
        for actual, frozen in zip(projection['delivery_artifacts'], original):
            self.assertEqual({k: v for k, v in actual.items() if k != 'producer'},
                {k: v for k, v in frozen.items() if k != 'producer'})
        self.assertEqual(projection['delivery_producers'][0], producer)
        self.assertLess(len(json.dumps(projection)), len(json.dumps(artifacts)))
        projection['delivery_producers'][0]['nested']['~/'].append('view-only mutation')
        self.assertEqual(artifacts, original)
        for reference in ({'$ref': '/delivery_producers/999'}, {'$ref': '/delivery_producers/00'},
                {'$ref': '/delivery_producers/-1'}, {'$ref': '/delivery_producers/0', 'extra': True},
                producer):
            bad = {**view, 'delivery_artifacts': [{'producer': reference}]}
            with self.assertRaises(LLMResponseError):
                restore_delivery_producers(bad)

    def test_delivery_producer_default_wire_bytes_remain_unchanged(self):
        from simple_ar.report.review_evidence import project_delivery_producers
        artifacts = [{'path': 'a', 'producer': {'owner': '原始'}},
            {'path': 'b', 'producer': {'owner': '原始'}}, {'path': 'c', 'producer': None},
            {'path': 'd'}]
        expected = ('{"delivery_artifacts": [{"path": "a", "producer": {"$ref": "/delivery_producers/0"}}, '
            '{"path": "b", "producer": {"$ref": "/delivery_producers/0"}}, '
            '{"path": "c", "producer": {"$ref": "/delivery_producers/1"}}, {"path": "d"}], '
            '"delivery_producers": [{"owner": "原始"}, null]}').encode('utf-8')
        for options in ({}, {'producer_table_pointer': '/delivery_producers'}):
            self.assertEqual(json.dumps(project_delivery_producers(artifacts, **options),
                ensure_ascii=False).encode('utf-8'), expected)

    def test_document_feedback_roundtrip_keeps_all_fields_and_reduces_repeated_producers(self):
        from copy import deepcopy
        from simple_ar.report.review_evidence import project_document_feedback
        producer = {'owner': 'run/one', 'execution': {'stdout': '完整原始记录\n' * 500},
            'unknown': {'~/': [None, False, 0, '', {'keep': True}]}}
        artifacts = [{**self.profile_artifact(), 'path': f'source/{i}.csv', 'producer': deepcopy(producer),
            'consumers': [{'path': 'report/body', 'unknown': ['preserve']}],
            'visual_pending': True, 'visual_reviewed': False, 'unknown': {'keep': i}}
            for i in range(25)]
        # Equal content/SHA does not erase distinct paths or producer origins.
        artifacts[1]['producer']['owner'] = 'run/two'
        artifacts.extend({'path': f'false/{i}', 'producer': value}
            for i, value in enumerate((None, {}, '', False, 0, True, 1)))
        artifacts.append({'path': 'without-producer', 'unknown': ['keep']})
        diagnosis = {'owner': 'writing/original', 'receipt_sha256': {'request.json': 'b' * 64},
            'artifact_sha256': {'draft.md': 'c' * 64}, 'unknown': {'unverified': True},
            'review': {'review_status': 'reviewed', 'findings': [],
                'section_reviews': [{'section_id': 'document', 'findings': []}],
                'delivery_review': {'status': 'reviewed', 'artifacts': artifacts, 'defects': [],
                    'uninspected': [], 'unknown': {'keep': [1, 2]}, 'visual_pending': ['source/0.csv']}}}
        original = deepcopy(diagnosis)
        before_bytes = json.dumps(diagnosis, ensure_ascii=False).encode('utf-8')
        projected = project_document_feedback(diagnosis)
        wire = json.loads(json.dumps({'source_feedback': projected}, ensure_ascii=False))
        feedback = wire['source_feedback']
        rows = feedback['review']['delivery_review']['artifacts']
        self.assertEqual(len(rows), len(artifacts))
        self.assertEqual(len(feedback['delivery_producers']), 9)
        self.assertEqual(rows[0]['producer'], rows[2]['producer'])
        self.assertNotEqual(rows[0]['producer'], rows[1]['producer'])
        # Expand only the projected producer slots, using the actual outer JSON.
        restored = deepcopy(feedback)
        for row, actual in zip(restored['review']['delivery_review']['artifacts'], artifacts):
            self.assertEqual({k: v for k, v in row.items() if k != 'producer'},
                {k: v for k, v in actual.items() if k != 'producer'})
            if 'producer' in row:
                self.assertEqual(set(row['producer']), {'$ref'})
                value = wire
                for part in row['producer']['$ref'].split('/')[1:]:
                    value = value[int(part)] if isinstance(value, list) else value[part]
                row['producer'] = deepcopy(value)
        restored.pop('delivery_producers')
        self.assertEqual(restored, original)
        after_bytes = json.dumps({'source_feedback': projected}, ensure_ascii=False).encode('utf-8')
        self.assertLess(len(after_bytes), len(before_bytes) // 4)
        self.assertEqual(json.dumps(diagnosis, ensure_ascii=False).encode('utf-8'), before_bytes)
        projected['delivery_producers'][0]['unknown']['~/'].append('view only')
        projected['review']['delivery_review']['artifacts'][0]['consumers'][0]['unknown'].append('view only')
        projected['review']['section_reviews'][0]['findings'].append({'message': 'view only'})
        projected['unknown']['unverified'] = False
        self.assertEqual(diagnosis, original)

    def test_document_feedback_preserves_absent_inspection_and_rejects_table_collision(self):
        from copy import deepcopy
        from simple_ar.report.review_evidence import project_document_feedback
        for diagnosis in ({'unknown': []}, {'review': None}, {'review': {}},
                {'review': {'delivery_review': None}},
                {'review': {'delivery_review': {'status': 'unknown', 'visual_pending': True}}}):
            original = deepcopy(diagnosis)
            view = project_document_feedback(diagnosis)
            self.assertEqual(view, original)
            view['view_only'] = True
            self.assertEqual(diagnosis, original)
        diagnosis = {'delivery_producers': ['original unknown field'],
            'review': {'delivery_review': {'artifacts': []}}}
        original = deepcopy(diagnosis)
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            project_document_feedback(diagnosis)
        self.assertEqual(diagnosis, original)

    def test_document_feedback_rejects_malformed_structure_without_losing_original_fields(self):
        from copy import deepcopy
        from simple_ar.report.review_evidence import project_document_feedback
        for diagnosis in (None, [], {'review': []}, {'review': {'delivery_review': []}},
                *[{'review': {'delivery_review': {'artifacts': value, 'unknown': 'retain'}}}
                    for value in (None, {}, 'not rows', [None], ['not an object'])]):
            original = deepcopy(diagnosis)
            with self.subTest(diagnosis=diagnosis), self.assertRaises(ValueError):
                project_document_feedback(diagnosis)
            self.assertEqual(diagnosis, original)

    def test_shared_delivery_view_locator_and_strict_quotes_use_physical_derived_paths(self):
        from simple_ar.report.review_evidence import (
            DELIVERY_REVIEW_CONTRACT, SHARED_PRODUCER_LOCATOR_CONTRACT, LOCATOR_REFERENCE_CONTRACT,
            COMPARISON_VERIFICATION_CONTRACT, restore_delivery_producers, resolve_review_evidence,
            validate_finding_anchors,
        )
        _, memory, config, draft = self.objects()
        producer = {'owner': 'actual-run', 'execution': {'~/': 'Original full record.'}}
        artifacts = [{'path': f'input/{i}', 'sha256': 'same', 'coverage': 'complete',
            'text': 'Complete scientific body.\n' * 200, 'producer': value}
            for i, value in enumerate((producer, producer, {}, None))]
        captured = []
        class Client:
            def ask_json(self, system, prompt, **ignored):
                captured.append(json.loads(prompt))
                return {'section_reviews': []}
        common = dict(client=Client(), **document_review_inputs(memory, config, [draft],
            source_evidence=[{'handle': 'paper:original', 'metadata': {
                'evidence_passages': [{'text': 'Full original passage.\n' * 100}]}}],
            metric_summary={'count': 4}, execution_evidence={'execution_records': [{'cwd': '/actual'}]}),
            delivery_review={'contract': DELIVERY_REVIEW_CONTRACT, 'artifacts': artifacts})
        for contract in (None, LOCATOR_REFERENCE_CONTRACT, SHARED_PRODUCER_LOCATOR_CONTRACT):
            review_document(**common, evidence_navigation_contract=contract,
                comparison_verification_contract=COMPARISON_VERIFICATION_CONTRACT)
            view = captured[-1]
            self.assertEqual(restore_delivery_producers(view), artifacts)
            if contract != SHARED_PRODUCER_LOCATOR_CONTRACT:
                self.assertEqual(view['delivery_artifacts'], artifacts)
                self.assertNotIn('delivery_producers', view)
        view = captured[-1]
        for key in ('sections', 'source_evidence', 'metric_sources', 'execution_evidence',
                'verified_execution_results', 'delivery_text_observation', 'output_schema'):
            self.assertEqual(view[key], captured[1][key])
        self.assertEqual(view['source_evidence'][0]['metadata']['evidence_passages'][0]['text'],
            'Full original passage.\n' * 100)
        self.assertEqual(len(view['delivery_producers']), 3)
        self.assertEqual(view['sections'][0]['markdown'], draft.draft_markdown)
        self.assertIn('anchor', view['output_schema']['section_reviews'][0]['findings'][0]['evidence_quotes'][0])
        locator = review_evidence_locator(view, max_chars=20000, max_entries=500)
        self.assertEqual(locator['coverage'], 'complete')
        view['evidence_locator'] = locator
        for role, group in locator['pointers_by_role'].items():
            for anchor, pointer in group.items():
                raw = {'findings': [{'evidence_quotes': [{'anchor': anchor}]}]}
                resolved = resolve_review_evidence(raw, view)['findings'][0]['evidence_quotes'][0]
                quote = ReportEvidenceQuote.model_validate(resolved)
                self.assertEqual(quote.pointer, pointer)
                self.assertEqual(quote.role, role)
                validate_evidence_quotes([quote], view)
                if pointer.startswith('/delivery_producers/') or pointer.endswith('/producer/$ref'):
                    self.assertEqual(role, 'derived_context')
        groups = locator['pointers_by_role']
        self.assertIn('/delivery_artifacts/0/text', groups['producer_output'].values())
        for i in (2, 3):
            self.assertIn(f'/delivery_artifacts/{i}/text', groups['recorded_material'].values())
        pointer = '/delivery_producers/0/execution/~0~1'
        self.assertIn(pointer, groups['derived_context'].values())
        finding = {'finding_id': 'metadata-only', 'section_id': 'setup', 'type': 'metric_mismatch',
            'severity': 'major', 'message': 'An allegation.', 'required_action': 'revise',
            'draft_quotes': [{'section_id': 'setup', 'paragraph': 'p1'}],
            'evidence_quotes': [{'pointer': pointer}, {'pointer': '/delivery_artifacts/0/producer/$ref'}]}
        resolved = resolve_review_evidence({'findings': [finding]}, view)['findings'][0]
        self.assertEqual((resolved['type'], resolved['required_action']), ('evidence_gap', 'verify'))
        for target in ('/delivery_producers/0/owner', '/delivery_artifacts/0/producer/$ref',
                '/delivery_artifacts/00/text', '/delivery_artifacts/99/text'):
            review = ReportSectionReview(section_id='setup', findings=[ReviewerFinding.model_validate(
                {**resolved, 'delivery_targets': [target]})])
            with self.assertRaises(LLMResponseError):
                validate_finding_anchors(review, {'setup': draft.draft_markdown}, view)
        for bad in ({'anchor': 'derived_context:99999'},
                {'pointer': '/delivery_artifacts/0/producer/execution/~0~1'},
                {'pointer': '/delivery_producers/00/owner'},
                {'pointer': '/delivery_producers/0/absent'},
                {'pointer': pointer, 'quote': 'Fabricated record.', 'role': 'derived_context'},
                {'pointer': pointer, 'quote': 'Original full record.', 'role': 'executor_record'}):
            with self.subTest(reference=bad), self.assertRaises(LLMResponseError):
                resolve_review_evidence({'findings': [{**finding, 'evidence_quotes': [bad]}]}, view)

    def test_unproven_comparison_becomes_pending_verification_never_a_pass_or_repaired_quote(self):
        from copy import deepcopy
        from simple_ar.report.review_evidence import (
            COMPARISON_VERIFICATION_CONTRACT, DELIVERY_REVIEW_CONTRACT,
            resolve_review_evidence, validate_finding_anchors, delivery_review_result,
        )
        from simple_ar.report.schema import finding_requires_resolution
        artifact = {'path': 'outputs/counts.json', 'sha256': 'a' * 64,
            'producer': 'actual-run', 'coverage': 'complete', 'text': '{"count":4,"sum":8}'}
        view = {'delivery_review_contract': DELIVERY_REVIEW_CONTRACT,
            'delivery_artifacts': [artifact], 'metric_sources': {'count': 4}}
        finding = {'finding_id': 'alleged-count', 'type': 'metric_mismatch',
            'severity': 'major', 'section_id': 'document', 'message': 'Output counts may disagree.',
            'required_action': 'revise', 'delivery_targets': ['/delivery_artifacts/0/text'],
            'evidence_quotes': [{'pointer': '/delivery_artifacts/0/text'}]}
        raw = {'findings': [finding]}
        original = deepcopy(raw)
        legacy = ReviewerFinding.model_validate(resolve_review_evidence(raw, view)['findings'][0])
        with self.assertRaisesRegex(LLMResponseError, 'counter-evidence'):
            validate_finding_anchors(ReportSectionReview(section_id='document', findings=[legacy]), {}, view)
        view['comparison_verification_contract'] = COMPARISON_VERIFICATION_CONTRACT
        qualified = ReviewerFinding.model_validate(resolve_review_evidence(raw, view)['findings'][0])
        self.assertEqual((qualified.type, qualified.required_action), ('evidence_gap', 'verify'))
        self.assertIn('Unverified metric comparison', qualified.message)
        self.assertTrue(finding_requires_resolution(qualified))
        review = ReportSectionReview(section_id='document', verdict='revise_required', findings=[qualified])
        validate_finding_anchors(review, {}, view)
        delivery = delivery_review_result({'contract': DELIVERY_REVIEW_CONTRACT, 'artifacts': [artifact]}, [review])
        self.assertEqual(delivery['status'], 'needs_revision')
        self.assertEqual(len(delivery['defects']), 1)
        self.assertEqual(raw, original)
        supported = deepcopy(raw)
        supported['findings'][0]['evidence_quotes'].append({'pointer': '/metric_sources/count'})
        self.assertEqual(resolve_review_evidence(supported, view)['findings'][0]['type'], 'metric_mismatch')
        for reference in ({'pointer': '/delivery_artifacts/4/text'},
                {'pointer': '/delivery_artifacts/0/text', 'quote': 'Invented source text', 'role': 'producer_output'}):
            invalid = deepcopy(raw)
            invalid['findings'][0]['evidence_quotes'] = [reference]
            with self.assertRaises(LLMResponseError):
                resolve_review_evidence(invalid, view)

    def test_default_document_capacity_keeps_large_evidence_and_client_budget_ownership(self):
        context, memory, config, draft = self.objects()
        captured = []
        class Client:
            def ask_json(self, system, prompt, **ignored):
                captured.append(json.loads(prompt))
                return {"section_reviews": []}
        evidence = {"rows": ["complete recorded evidence " * 5000]}
        review_document(client=Client(), **document_review_inputs(memory, config, [draft], metric_summary=evidence))
        self.assertGreater(len(json.dumps(captured[0])), 90_000)
        self.assertEqual(captured[0]["metric_sources"], evidence)
        self.assertEqual(captured[0]["sections"][0]["markdown"], draft.draft_markdown)
        class BudgetClient:
            def ask_json(self, *args, **ignored):
                raise LLMError("existing client/session capacity exhausted")
        with self.assertRaisesRegex(LLMError, "capacity exhausted"):
            review_document(client=BudgetClient(), **document_review_inputs(memory, config, [draft], metric_summary=evidence))

    def test_source_passage_layout_whitespace_is_not_a_changed_scientific_quote(self):
        view = {"source_evidence": [{"metadata": {"evidence_passages": [
            {"text": "Observed mean\nerror = 0.653;\tcontrol = 0.020."}]}}],
            "supplementary_evidence": [{"tool_name": "search_source_chunks", "content": {
                "chunks": [{"text": "Observed mean\r\nerror = 0.653;  control = 0.020."}]}}]}
        before = json.dumps(view)
        for pointer in ("/source_evidence/0/metadata/evidence_passages/0/text",
                        "/supplementary_evidence/0/content/chunks/0/text"):
            quote = ReportEvidenceQuote(pointer=pointer,
                quote="Observed mean error = 0.653; control = 0.020.", role="recorded_material")
            validate_evidence_quotes([quote], view)
            self.assertEqual(quote.quote, "Observed mean error = 0.653; control = 0.020.")
            with self.assertRaisesRegex(LLMResponseError, "recorded_material"):
                validate_evidence_quotes([quote.model_copy(update={"role": "executor_record"})], view)
        self.assertEqual(json.dumps(view), before)

    def test_source_layout_matching_rejects_non_whitespace_changes_and_window_stitching(self):
        view = {"source_evidence": [{"metadata": {"evidence_passages": [
            {"text": "Observed mean\nerror = 0.653; control = 0.020. ﬁtted model, high-\nconfidence."},
            {"text": "A different retained window."}]}}]}
        pointer = "/source_evidence/0/metadata/evidence_passages/0/text"
        for absent in ("observed mean error", "error = 0.654", "error = -0.653", "error = 0. 653",
                "error = 0.653, control", "fitted model", "high-confidence",
                "control = 0.020. A different retained window.", "error = 0.653; omitted control = 0.020."):
            with self.subTest(quote=absent), self.assertRaisesRegex(LLMResponseError, "absent or changed"):
                validate_evidence_quotes([ReportEvidenceQuote(pointer=pointer, quote=absent,
                    role="recorded_material")], view)

    def test_layout_normalization_never_changes_identity_paths_or_scalar_values(self):
        view = {"source_evidence": [{"metadata": {"title": "Two  distinct records"}}],
            "execution_evidence": {"execution_records": [{"cwd": "/a  b", "duration_sec": 3.5}]},
            "metric_sources": {"accuracy": 0.5},
            "supplementary_evidence": [{"tool_name": "get_synthesis_brief", "content": {
                "chunks": [{"text": "Earlier\ninterpretation"}]}}]}
        for pointer, quote, role in (("/source_evidence/0/metadata/title", "Two distinct records", "recorded_material"),
                ("/execution_evidence/execution_records/0/cwd", "/a b", "executor_record"),
                ("/execution_evidence/execution_records/0/duration_sec", "3 .5", "executor_record"),
                ("/metric_sources/accuracy", "0.50", "registered_result"),
                ("/supplementary_evidence/0/content/chunks/0/text", "Earlier interpretation", "derived_context")):
            with self.subTest(pointer=pointer), self.assertRaisesRegex(LLMResponseError, "absent or changed"):
                validate_evidence_quotes([ReportEvidenceQuote(pointer=pointer, quote=quote, role=role)], view)

    def test_locator_uses_current_nested_paths_and_roles_without_copying_or_mutating_evidence(self):
        view = {"objective": "Do not independently verify by declaration.",
            "source_evidence": [{"metadata": {"title": "Title", "evidence_passages": [
                {"text": "Original quantitative statement.", "chunk_id": "chunk-3"}]}}],
            "execution_evidence": {"declared_execution_context": {"cwd": "requested"},
                "execution_records": [{"cwd": "observed", "exit_code": 0}],
                "output_evidence": [{"preview": "Producer observation."}]},
            "metric_sources": {"a/b~c": 3}, "response_format_correction": {
                "rejected_response": {"quote": "Never index a rejected allegation."}},
            "revision_context": {"length_observation": {"candidate_tokens": 17},
                "previous_draft": {"draft_markdown": "An earlier draft is not evidence."}},
            "sections": [{"markdown": "Not source evidence."}]}
        before = json.dumps(view, ensure_ascii=False)
        locator = review_evidence_locator(view)
        self.assertEqual(json.dumps(view, ensure_ascii=False), before)
        groups = locator["pointers_by_role"]
        self.assertEqual(groups["recorded_material"]["recorded_material:0"], "/source_evidence/0/metadata/evidence_passages/0/text")
        self.assertIn("/execution_evidence/execution_records/0/cwd", groups["executor_record"].values())
        self.assertIn("/execution_evidence/declared_execution_context/cwd", groups["declaration"].values())
        self.assertIn("/metric_sources/a~1b~0c", groups["registered_result"].values())
        self.assertIn("/revision_context/length_observation/candidate_tokens", groups["derived_context"].values())
        self.assertEqual(locator["omitted_scalar_count"], 0)
        self.assertEqual(locator["coverage"], "complete")
        for role, pointers in groups.items():
            for pointer in pointers.values():
                _, value = _resolve_pointer(view, pointer)
                quote = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                validate_evidence_quotes([ReportEvidenceQuote(pointer=pointer, quote=quote, role=role)], view)
                if role in {"executor_record", "declaration", "producer_output"}:
                    with self.assertRaisesRegex(LLMResponseError, "role is"):
                        validate_evidence_quotes([ReportEvidenceQuote(
                            pointer=pointer, quote=quote, role="recorded_material")], view)
        encoded = json.dumps(locator)
        for absent in ("Original quantitative statement.", "observed", "Never index a rejected allegation.", "markdown", "previous_draft"):
            self.assertNotIn(absent, encoded)
        with self.assertRaisesRegex(LLMResponseError, "absent"):
            validate_evidence_quotes([ReportEvidenceQuote(pointer="/source_evidence/0/evidence_passages/0/text",
                quote="Original", role="recorded_material")], view)

    def test_locator_is_bounded_fair_and_does_not_make_omitted_fields_unavailable(self):
        view = {"source_evidence": [{"metadata": {"evidence_passages": [
            {"text": f"Retained passage {i}"} for i in range(100)]}}],
            "execution_evidence": {"execution_records": [{"cwd": "observed"}]}, "objective": "goal"}
        locator = review_evidence_locator(view, max_entries=3)
        self.assertEqual(locator["listed_scalar_count"], 3)
        self.assertEqual(locator["omitted_scalar_count"], 99)
        self.assertEqual(set(locator["pointers_by_role"]), {"recorded_material", "executor_record", "declaration"})
        small = review_evidence_locator(view, max_chars=500)
        self.assertLessEqual(len(json.dumps(small, ensure_ascii=False, separators=(",", ":"))), 500)
        self.assertEqual(small["coverage"], "partial")
        self.assertEqual(small["eligible_scalar_count"], 102)
        self.assertEqual(review_evidence_locator(view, max_entries=0)["listed_scalar_count"], 0)
        self.assertEqual(review_evidence_locator({})["coverage"], "complete")
        with self.assertRaisesRegex(ValueError, "coverage"):
            review_evidence_locator(view, max_chars=1)
        validate_evidence_quotes([ReportEvidenceQuote(
            pointer="/source_evidence/0/metadata/evidence_passages/99/text", quote="Retained passage 99",
            role="recorded_material")], view)

    def test_locator_cannot_cite_itself_and_source_quotations_still_require_correct_ownership(self):
        view = {"source_evidence": [{"summary": "A prior interpretation.", "metadata": {
            "evidence_passages": [{"text": "Original result."}]}}]}
        locator = review_evidence_locator(view)
        view["evidence_locator"] = locator
        self.assertEqual(review_evidence_locator(view), locator)
        self.assertIn("/source_evidence/0/summary", locator["pointers_by_role"]["derived_context"].values())
        with self.assertRaisesRegex(LLMResponseError, "supplied evidence"):
            validate_evidence_quotes([ReportEvidenceQuote(
                pointer="/evidence_locator/pointers_by_role/recorded_material/recorded_material:0",
                quote="/source_evidence/0/metadata/evidence_passages/0/text", role="recorded_material")], view)
        with self.assertRaisesRegex(LLMResponseError, "derived_context"):
            validate_evidence_quotes([ReportEvidenceQuote(pointer="/source_evidence/0/summary",
                quote="A prior interpretation.", role="recorded_material")], view)

    def test_document_and_section_reviews_share_actual_locator_with_strict_validation(self):
        from simple_ar.report.agent import _reviewer_context
        context, memory, config, draft = self.objects()
        passage = {"handle": "paper:p", "kind": "paper", "metadata": {
            "reading_artifact": "reading/p.json", "reading_notes": {"method": "Prior interpretation."},
            "bibliography": {"title": "Recorded title"}, "evidence_passages_truncated": True,
            "evidence_passages": [{"text": "Actual retained result."}]}}
        original = json.dumps(passage)
        from simple_ar.report.schema import SourceHandle
        memory.source_handles = [SourceHandle.model_validate(passage)]
        captured = []
        class Client:
            def ask_json(self, system, prompt, **ignored):
                captured.append(json.loads(prompt))
                return {"section_reviews": []}
        template = load_report_template_bundle(report_mode="experiment", config=config)
        review_document(client=Client(), template=template, memory=memory, sections=[draft], config=config,
            execution_summary={}, metric_summary={}, source_evidence=[passage])
        document = captured[0]
        section = _reviewer_context(context=context, memory=memory, template=template,
            section=memory.section_plan[0], draft=draft, config=config)
        for view, root in ((document, "source_evidence"), (section, "allowed_sources")):
            metadata = view[root][0]["metadata"]
            self.assertNotIn("reading_notes", metadata)
            self.assertEqual(len(metadata["evidence_passages"]), len(passage["metadata"]["evidence_passages"]))
            for actual, original_passage in zip(metadata["evidence_passages"], passage["metadata"]["evidence_passages"]):
                self.assertEqual({key: actual[key] for key in original_passage}, original_passage)
            self.assertEqual(metadata["bibliography"]["title"], "Recorded title")
            self.assertTrue(metadata["evidence_passages_truncated"])
            self.assertTrue(metadata["derived_reading_notes_on_request"]["omitted_from_this_view"])
            pointer = f"/{root}/0/metadata/evidence_passages/0/text"
            self.assertIn(pointer, view["evidence_locator"]["pointers_by_role"]["recorded_material"].values())
            validate_evidence_quotes([ReportEvidenceQuote(pointer=pointer, quote="Actual retained result.",
                role="recorded_material")], view)
        self.assertEqual(document["source_evidence"][0]["metadata"]["bibliography"], passage["metadata"]["bibliography"])
        self.assertEqual(json.dumps(passage), original)
        self.assertEqual(memory.source_handles[0].metadata, passage["metadata"])
        from simple_ar.report.review_evidence import LOCATOR_REFERENCE_CONTRACT, resolve_review_evidence
        review_document(client=Client(), template=template, memory=memory, sections=[draft], config=config,
            execution_summary={}, metric_summary={}, source_evidence=[passage],
            evidence_navigation_contract=LOCATOR_REFERENCE_CONTRACT)
        current = captured[-1]
        self.assertNotIn('evidence_navigation_contract', document)
        self.assertEqual(current['evidence_navigation_contract'], LOCATOR_REFERENCE_CONTRACT)
        self.assertIn('anchor', current['output_schema']['section_reviews'][0]['findings'][0]['evidence_quotes'][0])
        anchor = next(iter(current['evidence_locator']['pointers_by_role']['recorded_material']))
        original_reply = {'findings': [{'evidence_quotes': [{'anchor': anchor}]}]}
        normalized = resolve_review_evidence(original_reply, current)
        quote = ReportEvidenceQuote.model_validate(normalized['findings'][0]['evidence_quotes'][0])
        self.assertEqual(quote.pointer, current['evidence_locator']['pointers_by_role']['recorded_material'][anchor])
        self.assertEqual(quote.quote, 'Actual retained result.')
        self.assertEqual(quote.role, 'recorded_material')
        validate_evidence_quotes([quote], current)
        self.assertEqual(original_reply, {'findings': [{'evidence_quotes': [{'anchor': anchor}]}]})
        for bad in ({'anchor': 'recorded_material:999'}, {'anchor': anchor, 'pointer': '/missing'},
                    {'pointer': '/metric_sources/wrong~escape'}):
            with self.assertRaises(LLMResponseError):
                resolve_review_evidence({'findings': [{'evidence_quotes': [bad]}]}, current)
        with self.assertRaisesRegex(LLMResponseError, 'navigation contract'):
            resolve_review_evidence(original_reply, {**current, 'evidence_navigation_contract': 'unknown'})

    def test_locator_does_not_cut_evidence_or_reject_an_existing_near_limit_request(self):
        context, memory, config, draft = self.objects()
        captured = []
        class Client:
            def ask_json(self, system, prompt, **ignored):
                captured.append((prompt, json.loads(prompt)))
                return {"section_reviews": []}
        kwargs = dict(client=Client(), **document_review_inputs(memory, config, [draft]))
        review_document(**kwargs)
        original = dict(captured[-1][1])
        original.pop("evidence_locator")
        base_size = len(json.dumps(original, ensure_ascii=False, separators=(",", ":")))
        for extra in (0, 150, 350):
            with self.subTest(extra=extra):
                config.max_document_review_prompt_chars = base_size + extra
                review_document(**kwargs)
                prompt, view = captured[-1]
                self.assertLessEqual(len(prompt), base_size + extra)
                locator = view.pop("evidence_locator", None)
                self.assertEqual(view, original)
                if extra == 0:
                    self.assertIsNone(locator)
                elif extra == 150:
                    self.assertEqual(locator["coverage"], "not_listed")

    def test_document_review_accepts_current_heading_quotes_not_frozen_or_joined_fields(self):
        context, memory, config, draft = self.objects()
        draft.heading = "Current Misspelled Heading"
        quote = {"section_id": "setup", "quote": draft.heading}
        class Client:
            def ask_json(self, *args, **kwargs):
                return {"section_reviews": [{"section_id": "setup", "verdict": "warning", "findings": [{
                    "finding_id": "heading", "type": "style", "severity": "minor", "required_action": "revise",
                    "message": "Fix the current heading.", "draft_quotes": [quote]}]}]}
        common = dict(client=Client(), **document_review_inputs(memory, config, [draft]))
        self.assertEqual(review_document(**common)[0].findings[0].draft_quotes[0].quote, draft.heading)
        for absent in ("Setup", "Current Misspelled", draft.heading + "\n" + draft.draft_markdown, "A superseded heading"):
            quote["quote"] = absent
            with self.assertRaisesRegex(LLMResponseError, "absent"):
                review_document(**common)

    def test_historical_heading_check_uses_current_heading_and_keeps_body_compatible(self):
        context, memory, config, draft = self.objects()
        draft.heading = "Corrected Current Heading"
        old = ReviewerFinding(finding_id="heading", section_id="setup", type="style", severity="minor",
            message="An old heading opinion.")
        quoted = [draft.heading]
        class Client:
            def ask_json(self, *args, **kwargs):
                return {"section_reviews": [{"section_id": "setup", "verdict": "pass", "finding_checks": [{
                    "finding_id": old.finding_id, "status": "resolved", "explanation": "The current title is corrected.",
                    "draft_quotes": quoted}]}]}
        common = dict(client=Client(), **document_review_inputs(memory, config, [draft], historical_findings=[old]))
        self.assertEqual(review_document(**common)[0].finding_checks[0].draft_quotes, quoted)
        quoted[0] = draft.draft_markdown
        self.assertEqual(len(review_document(**common)), 1)
        quoted[0] = "Setup"
        with self.assertRaisesRegex(LLMResponseError, "absent"):
            review_document(**common)

    def test_section_review_quotes_candidate_heading_not_plan_or_previous_heading(self):
        context, memory, config, draft = self.objects()
        draft.heading = "Current Candidate Title"
        quote = {"section_id": "setup", "quote": draft.heading}
        class Client:
            def ask_json(self, *args, **kwargs):
                return {"section_id": "setup", "verdict": "warning", "findings": [{"finding_id": "heading",
                    "type": "style", "severity": "minor", "message": "A heading check.", "draft_quotes": [quote]}]}
        common = dict(client=Client(), context=context, memory=memory, config=config,
            template=load_report_template_bundle(report_mode="experiment", config=config),
            section=memory.section_plan[0], draft=draft, label="candidate-heading", extra_context=[])
        self.assertEqual(len(_section_response(**common).findings), 1)
        quote["quote"] = memory.section_plan[0].heading
        with self.assertRaisesRegex(LLMError, "absent"):
            _section_response(**common)

    def test_neighbor_quote_sources_are_separate_visible_windows_and_candidate_wins(self):
        from simple_ar.report.review_evidence import review_draft_quote_sources, draft_quote_present
        view = {"document_plan": {"sections": [{"section_id": "neighbor", "heading": "Frozen old heading"}]},
            "narrative_context": {"adopted_sections": [{"section_id": "neighbor", "heading": "Visible heading",
                "prose_windows": [{"text": "Visible head."}, {"text": "Visible tail."}],
                "table_excerpt": {"windows": [{"text": "| observed | 3 |"}]}},
                {"section_id": "candidate", "heading": "Superseded same-ID heading", "prose_windows": [{"text": "Old body"}]}]},
            "draft": {"section_id": "candidate", "heading": "Candidate heading", "draft_markdown": "Candidate body"}}
        sources = review_draft_quote_sources(view)
        for quote in ("Visible heading", "Visible head.", "Visible tail.", "| observed | 3 |"):
            self.assertTrue(draft_quote_present(sources, "neighbor", quote))
        for quote in ("Frozen old heading", "Visible headin", "Hidden middle.", "Visible head.Visible tail.", "Visible heading\nVisible head."):
            self.assertFalse(draft_quote_present(sources, "neighbor", quote))
        self.assertTrue(draft_quote_present(sources, "candidate", "Candidate heading"))
        self.assertFalse(draft_quote_present(sources, "candidate", "Superseded same-ID heading"))
        self.assertFalse(draft_quote_present(sources, "missing", "Candidate body"))

    def test_delivery_counts_and_assembly_prose_have_derived_not_scientific_ownership(self):
        view = {"length_observation": {"known_delivery_markdown_tokens": 536},
            "narrative_context": {"length_observation": {"adopted_token_count": 423}},
            "assembly_owned_content": [{"markdown": "Rechecked supplied rows; validity not verified."}],
            "presentation_contract": "prepared_presentation.v1",
            "prepared_publication": {"contract": "prepared_publication.v1", "view": {
                "logical_document": {"text": "Rendered caption, not independent measurements."}}}}
        for pointer, quote in (("/length_observation/known_delivery_markdown_tokens", "536"),
                ("/narrative_context/length_observation/adopted_token_count", "423"),
                ("/assembly_owned_content/0/markdown", "validity not verified"),
                ("/prepared_publication/view/logical_document/text", "Rendered caption")):
            reference = ReportEvidenceQuote(pointer=pointer, quote=quote, role="derived_context")
            validate_evidence_quotes([reference], view)
            with self.assertRaisesRegex(LLMResponseError, "derived_context"):
                validate_evidence_quotes([reference.model_copy(update={"role": "registered_result"})], view)

    def test_derived_delivery_count_supports_style_not_definite_metric_counter_evidence(self):
        from simple_ar.report.review_evidence import validate_finding_anchors
        for view, pointer in (({"length_observation": {"known_delivery_markdown_tokens": 536}},
                "/length_observation/known_delivery_markdown_tokens"),
                ({"presentation_contract": "prepared_presentation.v1", "prepared_publication": {
                    "contract": "prepared_publication.v1", "view": {"logical_document": {"text": "536"}}}},
                 "/prepared_publication/view/logical_document/text")):
            finding = ReviewerFinding(finding_id="length", section_id="conclusion", type="style", severity="major",
                required_action="revise", message="Presentation needs correction.",
                evidence_quotes=[ReportEvidenceQuote(pointer=pointer, quote="536", role="derived_context")])
            review = ReportSectionReview(section_id="conclusion", verdict="revise_required", findings=[finding])
            validate_finding_anchors(review, {"conclusion": "Bounded observations."}, view)
            finding.type = "metric_mismatch"
            finding.draft_quotes = [ReportDraftQuote(section_id="conclusion", quote="Bounded observations.")]
            with self.assertRaisesRegex(LLMResponseError, "non-derived"):
                validate_finding_anchors(review, {"conclusion": "Bounded observations."}, view)

    def test_revision_counts_and_edit_scope_are_derived_not_metric_counter_evidence(self):
        view = {"revision_context": {"length_observation": {"candidate_minus_baseline_tokens": 3}},
            "edit_scope": {"writer_call_unit": "One selected section only"}}
        references = [ReportEvidenceQuote(pointer="/revision_context/length_observation/candidate_minus_baseline_tokens",
            quote="3", role="derived_context"), ReportEvidenceQuote(pointer="/edit_scope/writer_call_unit",
            quote="One selected section", role="derived_context")]
        validate_evidence_quotes(references, view)
        with self.assertRaisesRegex(LLMResponseError, "derived_context"):
            validate_evidence_quotes([references[0].model_copy(update={"role": "registered_result"})], view)
        from simple_ar.report.review_evidence import validate_finding_anchors
        finding = ReviewerFinding(finding_id="length", section_id="setup", type="style", severity="major",
            required_action="revise", message="Length grew during requested trimming.", evidence_quotes=references)
        review = ReportSectionReview(section_id="setup", verdict="revise_required", findings=[finding])
        validate_finding_anchors(review, {"setup": "Observed only."}, view)
        finding.type = "metric_mismatch"
        finding.draft_quotes = [ReportDraftQuote(section_id="setup", quote="Observed only.")]
        with self.assertRaisesRegex(LLMResponseError, "non-derived"):
            validate_finding_anchors(review, {"setup": "Observed only."}, view)

    def objects(self):
        context = ReportContext(topic="Conditions and observations", report_mode="experiment",
            execution_context="Requested device CPU.", results={"execution_record": {
                "invocation_id": "run-1", "cwd": "/actual-copy", "duration_sec": 12.5,
                "observation_status": "executor_recorded"}})
        memory = ReportMemory(section_plan=[ReportSectionPlan(section_id="setup", heading="Setup", goal="Conditions")])
        config = ReportRuntimeConfig(max_review_iterations=1)
        draft = ReportSectionDraft(section_id="setup", heading="Setup", draft_markdown="The executor recorded /actual-copy.")
        return context, memory, config, draft

    def view(self):
        return {"execution_evidence": {"declared_execution_context": {"text": "Requested /original-copy."},
            "execution_records": [{"cwd": "/actual-copy", "duration_sec": 12.5}],
            "output_evidence": [{"preview": {"text": "Producer elapsed 12.4."}}]},
            "source_evidence": [{"metadata": {"evidence_passages": [{"text": "Distances beyond 22 are a terminal bin."}],
                "reading_notes": {"method_summary": "A recorded interpretation."}}}],
            "metric_sources": {"rows": [["coverage", 0.91]]},
            "extra_tool_context": [{"tool_name": "get_synthesis_brief", "content": {"text": "Unverified interpretation."}}]}

    def test_bad_pointer_changed_quote_container_and_partial_numeric_value_are_rejected(self):
        view = self.view()
        for pointer, quote in (
            ("/execution_evidence/execution_records/9/cwd", "/actual-copy"),
            ("/execution_evidence/execution_records/0/cwd", "/original-copy"),
            ("/execution_evidence/execution_records/0", "cwd"),
            ("/execution_evidence/execution_records/0/duration_sec", "12"),
            ("/execution_evidence/execution_records/-1/cwd", "/actual-copy"),
            ("file:///somewhere", "/actual-copy"),
            ("/sections/0/markdown", "a prior opinion"),
            ("/execution_evidence/bad~2escape", "value"),
        ):
            with self.assertRaises(LLMResponseError):
                validate_evidence_quotes([ReportEvidenceQuote(pointer=pointer, quote=quote,
                    role="executor_record")], view)
        validate_evidence_quotes([ReportEvidenceQuote(pointer="/execution_evidence/execution_records/0/duration_sec",
            quote="12.5", role="executor_record")], view)

    def test_invalid_location_explains_available_structure_without_guessing_a_reference(self):
        view = self.view()
        reference = ReportEvidenceQuote(pointer="/execution_records/0/cwd", quote="/actual-copy", role="executor_record")
        with self.assertRaisesRegex(LLMResponseError, 'Available keys at /:.*execution_evidence'):
            validate_evidence_quotes([reference], view)
        self.assertEqual(reference.pointer, "/execution_records/0/cwd")
        reference.pointer = "/execution_evidence/execution_records/8/cwd"
        with self.assertRaisesRegex(LLMResponseError, 'has 1 elements'):
            validate_evidence_quotes([reference], view)
        reference.pointer = '/execution_evidence/execution_records/0'
        with self.assertRaisesRegex(LLMResponseError, r'scalar field.*Available keys at /execution_evidence/execution_records/0:.*cwd'):
            validate_evidence_quotes([reference], view)
        self.assertEqual(reference.pointer, '/execution_evidence/execution_records/0')
        reference.pointer = '/execution_evidence/execution_records'
        with self.assertRaisesRegex(LLMResponseError, r'scalar field.*has 1 elements'):
            validate_evidence_quotes([reference], view)

    def test_new_factual_findings_require_current_draft_and_located_metric_counter_evidence(self):
        context, memory, config, draft = self.objects()
        finding = {"finding_id": "mismatch", "type": "metric_mismatch", "severity": "major",
            "section_id": "setup", "message": "A specific recorded mismatch."}
        class Client:
            def ask_json(self, *args, **kwargs):
                return {"section_reviews": [{"section_id": "setup", "verdict": "revise_required", "findings": [finding]}]}
        common = dict(client=Client(), **document_review_inputs(memory, config, [draft]))
        with self.assertRaisesRegex(LLMResponseError, "current-draft quotation"):
            review_document(**common)
        finding["draft_quotes"] = [{"section_id": "setup", "quote": draft.draft_markdown}]
        with self.assertRaisesRegex(LLMResponseError, "counter-evidence"):
            review_document(**common)
        finding["evidence_quotes"] = [{"pointer": "/metric_sources/duration", "quote": "12.5", "role": "registered_result"}]
        common["metric_summary"] = {"duration": 12.5}
        self.assertEqual(len(review_document(**common)[0].findings), 1)
        finding["draft_quotes"][0]["quote"] = "A superseded draft."
        with self.assertRaisesRegex(LLMResponseError, "absent"):
            review_document(**common)

    def test_historical_check_validates_source_location_and_cannot_relabel_declared_as_observed(self):
        context, memory, config, draft = self.objects()
        old = ReviewerFinding(finding_id="old-cwd", section_id="setup", type="evidence_gap", severity="major",
            message="An earlier claim about the execution directory.")
        reference = {"pointer": "/execution_evidence/declared_execution_context/text",
            "quote": "Requested device CPU.", "role": "executor_record"}
        class Client:
            def ask_json(self, *args, **kwargs):
                return {"section_reviews": [{"section_id": "setup", "verdict": "pass", "finding_checks": [{
                    "finding_id": old.finding_id, "status": "not_applicable", "explanation": "The literal role differs.",
                    "draft_quotes": [draft.draft_markdown], "evidence_quotes": [reference]}]}]}
        from simple_ar.report.execution_evidence import report_execution_evidence
        common = dict(client=Client(), **document_review_inputs(memory, config, [draft],
            execution_evidence=report_execution_evidence(context), historical_findings=[old]))
        with self.assertRaisesRegex(LLMResponseError, "declaration, not executor_record"):
            review_document(**common)
        reference.update(pointer="/execution_evidence/execution_records/0/cwd", quote="/actual-copy")
        check = review_document(**common)[0].finding_checks[0]
        self.assertEqual(check.evidence_quotes[0].role, "executor_record")

    def test_section_evidence_recheck_uses_one_addressable_payload_and_actual_tools(self):
        context, memory, config, draft = self.objects()
        tools = [ReportToolResult(tool_name="get_neighbor_chunks", content={"chunks": [{"text": "Current original passage."}]})]
        seen = []
        class Client:
            def ask_json(self, system, prompt, **kwargs):
                view, end = json.JSONDecoder().raw_decode(prompt[prompt.index('{'):])
                self_view = prompt[prompt.index('{'):]
                assert not self_view[end:].strip(), "No second JSON evidence payload"
                seen.append(view)
                return {"section_id": "setup", "verdict": "revise_required", "findings": [{
                    "finding_id": "gap", "type": "evidence_gap", "severity": "major",
                    "message": "A bounded source question remains.", "draft_quotes": [{"section_id": "setup", "quote": draft.draft_markdown}],
                    "evidence_quotes": [{"pointer": "/extra_tool_context/0/content/chunks/0/text",
                        "quote": "Current original passage.", "role": "recorded_material"}]}]}
        review = _section_response(client=Client(), context=context, memory=memory, config=config,
            template=load_report_template_bundle(report_mode="experiment", config=config),
            section=memory.section_plan[0], draft=draft, label="section-recheck", extra_context=tools)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["extra_tool_context"][0]["content"], tools[0].content)
        self.assertEqual(review.findings[0].evidence_quotes[0].role, "recorded_material")

    def test_old_brief_cards_stay_out_of_cold_review_but_explicit_requests_can_read_them(self):
        context, memory, config, draft = self.objects()
        source = {"handle": "paper:methods", "kind": "paper", "metadata": {
            "reading_notes": {"method": "An earlier interpretation."},
            "evidence_passages": [{"text": "A literal original method passage."}]}}
        brief = ReportToolResult(tool_name="get_paper_brief", content={"handles": [source]})
        before = brief.model_dump(mode="json")
        captured = []
        class Client:
            def ask_json(self, system, prompt, **ignored):
                captured.append(json.loads(prompt))
                return {"section_reviews": []}
        for requested in (None, [brief]):
            review_document(client=Client(), **document_review_inputs(memory, config, [draft],
                supplementary_evidence=[brief], requested_context=requested))
        cold = captured[0]["supplementary_evidence"][0]["content"]["handles"][0]["metadata"]
        requested = captured[1]["supplementary_evidence"][0]["content"]["handles"][0]["metadata"]
        self.assertNotIn("reading_notes", cold)
        self.assertIn("reading_notes", requested)
        self.assertIn("An earlier interpretation.", json.dumps(requested))
        self.assertEqual(brief.model_dump(mode="json"), before)

    def test_section_review_and_format_recovery_keep_primary_evidence_not_prior_cards(self):
        import copy
        from simple_ar.report.narrative import _prompt_handle_view
        context, memory, config, draft = self.objects()
        source = SourceHandle(handle="paper:methods", kind="paper", metadata={
            "reading_notes": {"method": "An earlier interpretation.", "reading_coverage": {
                "available_chunks": 20, "shown_chunk_ids": ["method-1"], "semantic_verification": "not_performed"}},
            "reading_artifact": "reading/methods.json", "reading_notes_truncated": True,
            "evidence_passages": [{"chunk_id": "method-1", "text": "The final bin also includes longer distances."}],
            "evidence_passages_truncated": True, "extraction_status": "parsed"})
        memory.source_handles = [source]
        memory.section_plan[0].evidence_handles = [source.handle]
        before = copy.deepcopy(memory.model_dump(mode="json"))
        seen = []
        class Client:
            def ask_json(self, system, prompt, **ignored):
                view = prompt_payload(prompt)
                seen.append(view)
                if len(seen) == 1:
                    raise LLMResponseError("Malformed outer response")
                return {"section_id": draft.section_id, "verdict": "pass"}
        _section_response(client=Client(), context=context,
            template=load_report_template_bundle(report_mode=context.report_mode, config=config),
            memory=memory, section=memory.section_plan[0], draft=draft, label="review-primary", config=config)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0]["allowed_sources"], seen[1]["allowed_sources"])
        metadata = seen[0]["allowed_sources"][0]["metadata"]
        self.assertNotIn("reading_notes", metadata)
        self.assertEqual(metadata["evidence_passages"], _prompt_handle_view(source)["metadata"]["evidence_passages"])
        self.assertTrue(metadata["evidence_passages_truncated"])
        marker = metadata["derived_reading_notes_on_request"]
        self.assertEqual(marker["reading_coverage"], source.metadata["reading_notes"]["reading_coverage"])
        self.assertTrue(marker["omitted_from_this_view"])
        self.assertEqual(marker["independent_verification"], "not_performed")
        self.assertEqual(memory.model_dump(mode="json"), before)

    def test_writer_keeps_cards_but_reviewer_must_explicitly_request_them(self):
        from simple_ar.report.agent import _reviewer_context, _writer_recovery_prompt
        context, memory, config, draft = self.objects()
        source = SourceHandle(handle="paper:legacy", kind="paper", metadata={
            "reading_notes": {"method": "Earlier, unverified interpretation."},
            "evidence_passages": [{"text": "Original qualifier contradicts that interpretation."}]})
        memory.source_handles = [source]
        memory.section_plan[0].evidence_handles = [source.handle]
        section = memory.section_plan[0]
        template = load_report_template_bundle(report_mode=context.report_mode, config=config)
        writer = json.loads(_writer_recovery_prompt(context=context, memory=memory, section=section,
            config=config, extra_context=[], previous_draft=None, review=None, draft_mode="section").split("\n\n", 1)[1])
        reviewer = _reviewer_context(context=context, memory=memory, section=section,
            draft=draft, template=template, config=config)
        self.assertIn("reading_notes", writer["source_handles"][0]["metadata"])
        self.assertNotIn("reading_notes", reviewer["allowed_sources"][0]["metadata"])
        brief = ReportToolResult(tool_name="get_paper_brief", content={"handles": [source.model_dump(mode="json")]})
        explicit = _reviewer_context(context=context, memory=memory, section=section,
            draft=draft, template=template, config=config, extra_context=[brief])
        pointer = "/extra_tool_context/0/content/handles/0/metadata/reading_notes/method"
        validate_evidence_quotes([ReportEvidenceQuote(pointer=pointer,
            quote=source.metadata["reading_notes"]["method"], role="derived_context")], explicit)
        with self.assertRaisesRegex(LLMResponseError, "derived_context"):
            validate_evidence_quotes([ReportEvidenceQuote(pointer=pointer,
                quote=source.metadata["reading_notes"]["method"], role="recorded_material")], explicit)
        self.assertEqual(brief.content["handles"][0], source.model_dump(mode="json"))

    def test_primary_projection_preserves_legacy_records_and_does_not_rebind_old_note_pointers(self):
        import copy
        from simple_ar.report.narrative import review_source_evidence
        source = {"handle": "paper:legacy", "metadata": {"reading_notes": {
            "method": "Old reading opinion.", "reading_coverage": {"available_chunks": 9}},
            "evidence_passages": [{"text": "Original condition."}], "custom_metadata": {"owner": "user"}}}
        before = copy.deepcopy(source)
        view = {"allowed_sources": review_source_evidence([source])}
        self.assertEqual(review_source_evidence(view["allowed_sources"]), view["allowed_sources"])
        self.assertEqual(view["allowed_sources"][0]["metadata"]["custom_metadata"], {"owner": "user"})
        original = ReportEvidenceQuote(pointer="/allowed_sources/0/metadata/evidence_passages/0/text",
            quote="Original condition.", role="recorded_material")
        validate_evidence_quotes([original], view)
        with self.assertRaisesRegex(LLMResponseError, "absent"):
            validate_evidence_quotes([ReportEvidenceQuote(pointer="/allowed_sources/0/metadata/reading_notes/method",
                quote="Old reading opinion.", role="derived_context")], view)
        for root in ("allowed_sources", "source_evidence"):
            current = {root: view["allowed_sources"]}
            pointer = f"/{root}/0/metadata/derived_reading_notes_on_request/reading_coverage/available_chunks"
            validate_evidence_quotes([ReportEvidenceQuote(pointer=pointer, quote="9", role="derived_context")], current)
            with self.assertRaisesRegex(LLMResponseError, "derived_context"):
                validate_evidence_quotes([ReportEvidenceQuote(pointer=pointer, quote="9", role="recorded_material")], current)
        self.assertEqual(source, before)
        atypical = {"metadata": {"reading_notes": "Unstructured legacy input", "evidence_passages": []}}
        self.assertEqual(review_source_evidence([atypical]), [atypical])

    def test_legacy_empty_fields_do_not_change_saved_shapes(self):
        finding = ReviewerFinding(finding_id="old", type="style", message="Recorded old issue.")
        check = ReportFindingCheck(finding_id="old", status="unresolved", explanation="Unknown.")
        self.assertNotIn("draft_quotes", finding.model_dump(mode="json"))
        self.assertNotIn("evidence_quotes", finding.model_dump(mode="json"))
        self.assertNotIn("evidence_quotes", check.model_dump(mode="json"))


if __name__ == "__main__":
    unittest.main()
