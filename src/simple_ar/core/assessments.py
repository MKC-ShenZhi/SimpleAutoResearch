"""Read current artifact assessments from native receipts; no assessment state store."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ArtifactFinding(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    severity: Literal['required', 'advisory']
    message: str = Field(min_length=1)
    subjects: dict[str, str] = Field(min_length=1)

    @model_validator(mode='after')
    def nonblank(self):
        if not self.message.strip():
            raise ValueError('Assessment finding requires a nonblank explanation')
        return self


class TextWindowScope(BaseModel):
    """A version-bound requested text window; never a whole-file certificate."""
    model_config = ConfigDict(extra='forbid', strict=True)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    size_bytes: int = Field(ge=0)

    @model_validator(mode='after')
    def ordered(self):
        if not self.start <= self.end <= self.size_bytes:
            raise ValueError('Inspection window must lie within the bound file')
        return self


class ArtifactAssessment(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: Literal['artifact_assessment.v1', 'artifact_assessment.v2']
    subjects: dict[str, str] = Field(min_length=1)
    evidence_sha256: dict[str, str] = Field(default_factory=dict)
    coverage: Literal['complete', 'partial', 'scoped']
    verdict: Literal['passed', 'needs_revision', 'incomplete']
    findings: list[ArtifactFinding] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    scope: TextWindowScope | None = None
    reassessed_prior: list[str] | None = None
    review_id: str | None = None

    @model_validator(mode='after')
    def consistent(self):
        if self.schema_version == 'artifact_assessment.v1':
            if self.scope is not None or self.reassessed_prior is not None or self.coverage == 'scoped':
                raise ValueError('Historical assessments cannot acquire a new review scope')
        elif (len(self.subjects) != 1 or self.reassessed_prior is None or not self.review_id
              or self.coverage == 'scoped' and self.scope is None
              or self.scope is not None and self.coverage == 'complete'):
            raise ValueError('Current subject assessment has an invalid review scope')
        if self.reassessed_prior is not None and (len(set(self.reassessed_prior)) != len(self.reassessed_prior)
                or any(len(value) != 64 or any(c not in '0123456789abcdef' for c in value)
                       for value in self.reassessed_prior)):
            raise ValueError('Reassessed opinions require distinct retained finding identities')
        for digest in [*self.subjects.values(), *self.evidence_sha256.values()]:
            if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('Assessment requires actual full-file SHA256 versions')
        if any(any(self.subjects.get(path) != digest for path,digest in row.subjects.items())
                for row in self.findings):
            raise ValueError('Finding targets must belong to inspected subjects')
        required = any(row.severity == 'required' for row in self.findings)
        if self.verdict == 'passed' and (required or self.coverage not in {'complete', 'scoped'}):
            raise ValueError('Partial coverage or required findings cannot pass')
        if required and self.verdict != 'needs_revision':
            raise ValueError('Required findings need revision')
        return self


def finding_id(assessment: dict, finding: dict) -> str:
    """Identify one actual prior opinion, including its original review identity."""
    reference = assessment.get('review_id')
    if not reference:
        # Unbound legacy data is never stronger than its complete original
        # assessment. Normal recorded observations carry an owner reference.
        reference = json.dumps(assessment, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    identity = [reference, finding.get('subjects', {}), finding.get('message', '')]
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode()).hexdigest()


def current_assessments(workspace: Any, observations: list[dict]) -> list[ArtifactAssessment]:
    """Caller supplies its accepted native observations, in journal order.

    Revalidate every subject and evidence version. Old receipts remain untouched;
    a stale assessment neither passes the new bytes nor becomes a current defect.
    """
    result = []
    seen_reviews = set()
    for observed in observations:
        metadata = observed.get('metadata') or {}
        if 'assessment' in metadata and 'assessments' in metadata:
            raise ValueError('Inspection metadata cannot contain both assessment and assessments')
        modern = 'assessments' in metadata
        values = metadata['assessments'] if modern else [metadata.get('assessment')]
        if modern and not isinstance(values, list):
            raise ValueError('Subject inspection assessments must be a list')
        for index, value in enumerate(values):
            if value is None or not (observed.get('status') == 'ok'
                    or observed.get('status') == 'error' and value.get('verdict') == 'incomplete'):
                continue
            assessment = ArtifactAssessment.model_validate(value)
            if not assessment.review_id:
                binding = metadata.get('inspection_binding') or {}
                reference = binding.get('assessment_path') or observed.get('record')
                if reference:
                    # A historical receipt can contain several subject assessments.
                    assessment = assessment.model_copy(update={'review_id': str(reference) +
                        (f'#{index}' if modern else '')})
            if assessment.review_id and assessment.review_id in seen_reviews:
                continue  # Replaying the same opinion is not a new review.
            if assessment.review_id:
                seen_reviews.add(assessment.review_id)
            if modern and len(assessment.subjects) != 1:
                raise ValueError('Subject inspection requires singleton assessments')
            try:
                if workspace.artifact_hashes(assessment.subjects) != assessment.subjects:
                    continue
            except (ValueError,OSError):
                continue  # Missing old versions remain history, not current acceptance.
            try:
                evidence_current = workspace.artifact_hashes(assessment.evidence_sha256) == assessment.evidence_sha256
            except (ValueError,OSError):
                evidence_current = False
            if not evidence_current:
                # A newer review whose evidence changed cannot silently fall back
                # to an earlier, less-informed pass of the same target bytes.
                required_findings = [row for row in assessment.findings if row.severity=='required']
                assessment = assessment.model_copy(update={'coverage':'partial',
                    'verdict':'needs_revision' if required_findings else 'incomplete',
                    'findings':required_findings,
                    'limitations':[*assessment.limitations,'Inspection evidence changed; reassess outstanding concerns against current sources']})
            result.append(assessment)
    return result


def assessment_state(assessments: list[dict], *, resolve: Callable | None = None
                     ) -> tuple[set[str], dict[str, dict[str, dict]]]:
    """Reduce current opinions once for both feedback and delivery decisions."""
    def subject(path: str) -> str:
        return str(resolve(path)) if resolve is not None else path

    passed, problems = set(), {}
    for assessment in assessments:
        subjects = {subject(path) for path in assessment['subjects']}
        # Reading the whole target is not a completed review. In particular,
        # historical failed reviews may retain complete read coverage.
        passed.difference_update(subjects)
        if assessment['coverage'] == 'complete' and assessment['verdict'] != 'incomplete':
            for path in subjects:
                # A complete read does not establish that a narrower review
                # actually re-examined earlier required opinions.
                if assessment['schema_version'] == 'artifact_assessment.v1':
                    problems.pop(path, None)
                else:
                    for identity in assessment.get('reassessed_prior') or []:
                        problems.get(path, {}).pop(identity, None)
            if assessment['verdict'] == 'passed':
                passed.update(subjects)
        for finding in assessment.get('findings', []):
            if finding.get('severity') == 'required':
                identity = finding_id(assessment, finding)
                for original in finding['subjects']:
                    path = subject(original)
                    passed.discard(path)
                    problems.setdefault(path, {})[identity] = finding
    return passed, problems


def assessment_issues(workspace: Any, observations: list[dict], artifacts: list[str],
                      *, require_pass: list[str] = ()) -> list[str]:
    """Only a conclusive complete inspection can supersede a required finding."""
    delivered = {str(workspace.resolve(path)) for path in artifacts}
    required = {str(workspace.resolve(path)) for path in require_pass}
    passed, problems = assessment_state([row.model_dump(mode='json')
        for row in current_assessments(workspace, observations)], resolve=workspace.resolve)
    issues = [f'Current delivered artifact retains a required finding: {path}: {message}'
        for path in delivered & problems.keys() for message in
            (finding['message'] for finding in problems[path].values())]
    issues.extend(f'Current artifact lacks a complete independent inspection: {path}'
        for path in required - passed)
    return issues
