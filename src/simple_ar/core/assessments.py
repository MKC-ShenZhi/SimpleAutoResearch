"""Read current artifact assessments from native receipts; no assessment state store."""
from __future__ import annotations

from typing import Any, Literal

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


class ArtifactAssessment(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: Literal['artifact_assessment.v1']
    subjects: dict[str, str] = Field(min_length=1)
    evidence_sha256: dict[str, str] = Field(default_factory=dict)
    coverage: Literal['complete', 'partial']
    verdict: Literal['passed', 'needs_revision', 'incomplete']
    findings: list[ArtifactFinding] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def consistent(self):
        for digest in [*self.subjects.values(), *self.evidence_sha256.values()]:
            if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('Assessment requires actual full-file SHA256 versions')
        if any(any(self.subjects.get(path) != digest for path,digest in row.subjects.items())
                for row in self.findings):
            raise ValueError('Finding targets must belong to inspected subjects')
        required = any(row.severity == 'required' for row in self.findings)
        if self.verdict == 'passed' and (required or self.coverage != 'complete'):
            raise ValueError('Partial coverage or required findings cannot pass')
        if required and self.verdict != 'needs_revision':
            raise ValueError('Required findings need revision')
        return self


def current_assessments(workspace: Any, observations: list[dict]) -> list[ArtifactAssessment]:
    """Caller supplies its accepted native observations, in journal order.

    Revalidate every subject and evidence version. Old receipts remain untouched;
    a stale assessment neither passes the new bytes nor becomes a current defect.
    """
    result = []
    for observed in observations:
        value = (observed.get('metadata') or {}).get('assessment')
        if value is None or not (observed.get('status') == 'ok'
                or observed.get('status') == 'error' and value.get('verdict') == 'incomplete'):
            continue
        assessment = ArtifactAssessment.model_validate(value)
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


def assessment_issues(workspace: Any, observations: list[dict], artifacts: list[str],
                      *, require_pass: list[str] = ()) -> list[str]:
    """Only a later complete inspection can supersede a current required finding."""
    delivered = {workspace.resolve(path) for path in artifacts}
    required = {workspace.resolve(path) for path in require_pass}
    passed, problems = set(), {}
    for assessment in current_assessments(workspace, observations):
        subjects = {workspace.resolve(path) for path in assessment.subjects}
        if assessment.verdict == 'incomplete':
            passed.difference_update(subjects)
        if assessment.coverage == 'complete':
            for path in subjects:
                passed.discard(path)
                problems.pop(path, None)
            if assessment.verdict == 'passed':
                passed.update(subjects)
        for finding in assessment.findings:
            if finding.severity == 'required':
                for subject in finding.subjects:
                    path = workspace.resolve(subject)
                    passed.discard(path)
                    problems.setdefault(path, []).append(finding.message)
    issues = [f'Current delivered artifact retains a required finding: {path}: {message}'
        for path in delivered & problems.keys() for message in problems[path]]
    issues.extend(f'Current artifact lacks a complete independent inspection: {path}'
        for path in required - passed)
    return issues
