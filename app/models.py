"""ORM models for the clinical cohort recomputation service.

Design notes
------------
* Source rows are append-only. An amended hospital row produces a new
  ``source_records`` row with ``revision + 1``; the superseded row is kept but
  marked inactive. Nothing is destroyed, so every statistic can be traced back.
* ``normalized_records`` keeps the original text/unit next to the standardized
  value/unit, plus the conversion rule that was applied.
* Screening decisions are versioned by run. A cohort snapshot is immutable once
  locked and is the only artefact analysis tasks may read.
* ``subject_dependencies`` is the dependency index used by incremental runs.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    role: Mapped[str] = mapped_column(String(40))  # admin|center_uploader|rule_reviewer|analysis_lead|privacy_admin
    center_id: Mapped[int | None] = mapped_column(ForeignKey("centers.id"), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Center(Base):
    __tablename__ = "centers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    # Date parsing formats and any center-level normalization config.
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    mapping_version: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CodeMapping(Base):
    """Hospital-local code -> study code. Versioned; never rewritten."""

    __tablename__ = "code_mappings"
    __table_args__ = (UniqueConstraint("center_id", "kind", "source_code", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    center_id: Mapped[int] = mapped_column(ForeignKey("centers.id"))
    kind: Mapped[str] = mapped_column(String(40))  # sex|diagnosis|medication|lab|consent|response
    source_code: Mapped[str] = mapped_column(String(120))
    study_code: Mapped[str] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Protocol(Base):
    __tablename__ = "protocols"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProtocolRevision(Base):
    __tablename__ = "protocol_revisions"
    __table_args__ = (UniqueConstraint("protocol_id", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    protocol_id: Mapped[int] = mapped_column(ForeignKey("protocols.id"))
    version: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|approved|retired
    rules: Mapped[dict] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(80))
    approved_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Upload(Base):
    __tablename__ = "uploads"
    __table_args__ = (UniqueConstraint("center_id", "batch_code"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    center_id: Mapped[int] = mapped_column(ForeignKey("centers.id"))
    batch_code: Mapped[str] = mapped_column(String(120))
    file_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    checksum: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="received")  # received|partial|completed|failed
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    processed_rows: Mapped[int] = mapped_column(Integer, default=0)
    last_row_no: Mapped[int] = mapped_column(Integer, default=0)
    interrupted: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(80))


class CutoffBatch(Base):
    __tablename__ = "cutoff_batches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    label: Mapped[str] = mapped_column(String(160), unique=True)
    upload_ids: Mapped[list] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceRecord(Base):
    """One hospital source row. Old revisions stay for audit."""

    __tablename__ = "source_records"
    __table_args__ = (
        UniqueConstraint("center_id", "source_uid", "revision"),
        Index("ix_source_upload_active", "upload_id", "active"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    upload_id: Mapped[int] = mapped_column(ForeignKey("uploads.id"))
    center_id: Mapped[int] = mapped_column(ForeignKey("centers.id"))
    source_uid: Mapped[str] = mapped_column(String(160))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    record_type: Mapped[str] = mapped_column(String(30))  # case|diagnosis|lab|medication|followup|consent
    source_row_no: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("source_records.id"), nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Subject(Base):
    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    study_id: Mapped[str] = mapped_column(String(40), unique=True)
    center_id: Mapped[int] = mapped_column(ForeignKey("centers.id"))
    # Salted hash of the hospital-local patient reference. The clear reference
    # stays inside source_records and is only exposed to privacy admins.
    patient_key_hash: Mapped[str] = mapped_column(String(64))
    family_initial: Mapped[str | None] = mapped_column(String(4), nullable=True)
    given_initial: Mapped[str | None] = mapped_column(String(4), nullable=True)
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    merged_into_subject_id: Mapped[int | None] = mapped_column(ForeignKey("subjects.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("center_id", "patient_key_hash"),)


class IdentityCandidate(Base):
    __tablename__ = "identity_candidates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subject_a_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))
    subject_b_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))
    evidence: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|confirmed|rejected
    decided_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NormalizedRecord(Base):
    __tablename__ = "normalized_records"
    __table_args__ = (UniqueConstraint("source_record_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_records.id"))
    subject_id: Mapped[int | None] = mapped_column(ForeignKey("subjects.id"), nullable=True)
    center_id: Mapped[int] = mapped_column(ForeignKey("centers.id"))
    kind: Mapped[str] = mapped_column(String(30))
    source_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    study_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(20))  # ok|pending|failed
    issues: Mapped[list] = mapped_column(JSONB, default=list)
    original_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    study_unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    value_num: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    converted_value: Mapped[float | None] = mapped_column(Numeric(20, 6), nullable=True)
    conversion: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    normalized: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SubjectDependency(Base):
    __tablename__ = "subject_dependencies"
    __table_args__ = (UniqueConstraint("subject_id", "source_record_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_records.id"))


class ScreeningRun(Base):
    __tablename__ = "screening_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    protocol_revision_id: Mapped[int] = mapped_column(ForeignKey("protocol_revisions.id"))
    cutoff_batch_id: Mapped[int] = mapped_column(ForeignKey("cutoff_batches.id"))
    strategy: Mapped[str] = mapped_column(String(20))  # full|incremental
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|partial|completed|failed
    base_run_id: Mapped[int | None] = mapped_column(ForeignKey("screening_runs.id"), nullable=True)
    trigger_type: Mapped[str | None] = mapped_column(String(30), nullable=True)  # upload|mapping|manual
    trigger_ref: Mapped[str | None] = mapped_column(String(160), nullable=True)
    verification_of_run_id: Mapped[int | None] = mapped_column(ForeignKey("screening_runs.id"), nullable=True)
    equivalence_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    affected_subjects: Mapped[list] = mapped_column(JSONB, default=list)
    reused_count: Mapped[int] = mapped_column(Integer, default=0)
    recomputed_count: Mapped[int] = mapped_column(Integer, default=0)
    executed_by: Mapped[str] = mapped_column(String(80))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScreeningRunCenter(Base):
    """Per-center checkpoint so an interrupted run resumes, not restarts."""

    __tablename__ = "screening_run_centers"
    __table_args__ = (UniqueConstraint("run_id", "center_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("screening_runs.id"))
    center_id: Mapped[int] = mapped_column(ForeignKey("centers.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|running|completed
    processed_subjects: Mapped[int] = mapped_column(Integer, default=0)
    total_subjects: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScreeningDecision(Base):
    __tablename__ = "screening_decisions"
    __table_args__ = (UniqueConstraint("run_id", "subject_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("screening_runs.id"))
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))
    center_id: Mapped[int] = mapped_column(ForeignKey("centers.id"))
    outcome: Mapped[str] = mapped_column(String(30))  # included|excluded_missing|excluded_ineligible|consent_withdrawn|pending_adjudication
    reason_codes: Mapped[list] = mapped_column(JSONB, default=list)
    evidence: Mapped[list] = mapped_column(JSONB, default=list)
    index_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    visits_merged: Mapped[int] = mapped_column(Integer, default=0)
    safety_retain: Mapped[bool] = mapped_column(Boolean, default=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    reused: Mapped[bool] = mapped_column(Boolean, default=False)
    reused_from_run_id: Mapped[int | None] = mapped_column(ForeignKey("screening_runs.id"), nullable=True)


class Reconciliation(Base):
    __tablename__ = "reconciliations"
    __table_args__ = (UniqueConstraint("run_id", "center_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("screening_runs.id"))
    center_id: Mapped[int] = mapped_column(ForeignKey("centers.id"))
    raw_rows: Mapped[int] = mapped_column(Integer, default=0)
    normalized_ok: Mapped[int] = mapped_column(Integer, default=0)
    pending: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    subjects_total: Mapped[int] = mapped_column(Integer, default=0)
    included: Mapped[int] = mapped_column(Integer, default=0)
    missing_data: Mapped[int] = mapped_column(Integer, default=0)
    identity_review: Mapped[int] = mapped_column(Integer, default=0)
    excluded_ineligible: Mapped[int] = mapped_column(Integer, default=0)
    consent_withdrawn: Mapped[int] = mapped_column(Integer, default=0)
    pending_subjects: Mapped[int] = mapped_column(Integer, default=0)
    merged_visits: Mapped[int] = mapped_column(Integer, default=0)
    balanced: Mapped[bool] = mapped_column(Boolean, default=False)
    checks: Mapped[dict] = mapped_column(JSONB, default=dict)


class CohortSnapshot(Base):
    __tablename__ = "cohort_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("screening_runs.id"))
    protocol_revision_id: Mapped[int] = mapped_column(ForeignKey("protocol_revisions.id"))
    cutoff_batch_id: Mapped[int] = mapped_column(ForeignKey("cutoff_batches.id"))
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|locked|superseded
    supersedes_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("cohort_snapshots.id"), nullable=True)
    case_count: Mapped[int] = mapped_column(Integer, default=0)
    safety_count: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str] = mapped_column(String(64), default="")
    created_by: Mapped[str] = mapped_column(String(80))
    locked_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SnapshotCase(Base):
    __tablename__ = "snapshot_cases"
    __table_args__ = (UniqueConstraint("snapshot_id", "subject_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("cohort_snapshots.id"))
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))
    center_id: Mapped[int] = mapped_column(ForeignKey("centers.id"))
    outcome: Mapped[str] = mapped_column(String(30))
    reason_codes: Mapped[list] = mapped_column(JSONB, default=list)
    evidence: Mapped[list] = mapped_column(JSONB, default=list)
    index_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    safety_retain: Mapped[bool] = mapped_column(Boolean, default=False)
    # Treatment arm captured at snapshot time, so a later amendment cannot
    # retroactively change a frozen analysis.
    treatment_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("screening_decisions.id"))


class FollowUpPlan(Base):
    __tablename__ = "follow_up_plans"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("cohort_snapshots.id"))
    protocol_revision_id: Mapped[int] = mapped_column(ForeignKey("protocol_revisions.id"))
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FollowUpTask(Base):
    __tablename__ = "follow_up_tasks"
    __table_args__ = (UniqueConstraint("plan_id", "subject_id", "visit_day"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("follow_up_plans.id"))
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))
    center_id: Mapped[int] = mapped_column(ForeignKey("centers.id"))
    protocol_revision_id: Mapped[int] = mapped_column(ForeignKey("protocol_revisions.id"))
    visit_day: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(40))
    scheduled_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="scheduled")  # scheduled|completed|cancelled_consent_withdrawn
    blocked_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("cohort_snapshots.id"))
    protocol_revision_id: Mapped[int] = mapped_column(ForeignKey("protocol_revisions.id"))
    kind: Mapped[str] = mapped_column(String(30))  # efficacy|safety
    status: Mapped[str] = mapped_column(String(20), default="completed")
    results: Mapped[dict] = mapped_column(JSONB, default=dict)
    executed_by: Mapped[str] = mapped_column(String(80))
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    analysis_run_id: Mapped[int] = mapped_column(ForeignKey("analysis_runs.id"))
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("cohort_snapshots.id"))
    protocol_revision_id: Mapped[int] = mapped_column(ForeignKey("protocol_revisions.id"))
    report_version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    supersedes_report_id: Mapped[int | None] = mapped_column(ForeignKey("reports.id"), nullable=True)
    frozen: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(80))
    entity: Mapped[str] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RevisionEvaluation(Base):
    """One differential attribution run: two protocol revisions x two data
    cutoffs, evaluated as the four combinations.

    The evaluation is an assessment artefact: it never creates snapshots,
    never rewrites reports and never approves a revision. Its results go stale
    when the underlying data or code mappings move on.
    """

    __tablename__ = "revision_evaluations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    protocol_id: Mapped[int] = mapped_column(ForeignKey("protocols.id"))
    old_revision_id: Mapped[int] = mapped_column(ForeignKey("protocol_revisions.id"))
    new_revision_id: Mapped[int] = mapped_column(ForeignKey("protocol_revisions.id"))
    old_cutoff_id: Mapped[int] = mapped_column(ForeignKey("cutoff_batches.id"))
    new_cutoff_id: Mapped[int] = mapped_column(ForeignKey("cutoff_batches.id"))
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|completed|failed|stale
    budget_per_case: Mapped[int] = mapped_column(Integer, default=32)
    input_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    condition_diff: Mapped[dict] = mapped_column(JSONB, default=dict)
    change_variables: Mapped[list] = mapped_column(JSONB, default=list)
    combos: Mapped[dict] = mapped_column(JSONB, default=dict)
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    data_revisions: Mapped[dict] = mapped_column(JSONB, default=dict)
    impact: Mapped[dict] = mapped_column(JSONB, default=dict)
    msc_stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvaluationCase(Base):
    """Per-subject four-combination comparison inside one evaluation."""

    __tablename__ = "evaluation_cases"
    __table_args__ = (UniqueConstraint("evaluation_id", "subject_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("revision_evaluations.id"))
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id"))
    center_id: Mapped[int] = mapped_column(ForeignKey("centers.id"))
    # outcome / index_date / reasons / evidence per combination key
    # (old_old, new_old, old_new, new_new); null outcome = not in that scope.
    outcomes: Mapped[dict] = mapped_column(JSONB, default=dict)
    index_dates: Mapped[dict] = mapped_column(JSONB, default=dict)
    reason_codes: Mapped[dict] = mapped_column(JSONB, default=dict)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    category: Mapped[str] = mapped_column(String(30))  # unchanged|protocol_only|data_only|joint|masked
    reason_changed: Mapped[bool] = mapped_column(Boolean, default=False)
    condition_interaction: Mapped[bool] = mapped_column(Boolean, default=False)
    condition_verdicts: Mapped[dict] = mapped_column(JSONB, default=dict)
    endpoint_status: Mapped[dict] = mapped_column(JSONB, default=dict)
    endpoint_attribution: Mapped[dict] = mapped_column(JSONB, default=dict)
    data_tags: Mapped[list] = mapped_column(JSONB, default=list)
    msc: Mapped[dict] = mapped_column(JSONB, default=dict)


class MigrationDossier(Base):
    """Migration dossier: which published analyses may keep referencing the
    old cohort and which a researcher must re-confirm. Read-only with respect
    to reports, snapshots and protocol revisions."""

    __tablename__ = "migration_dossiers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("revision_evaluations.id"))
    protocol_id: Mapped[int] = mapped_column(ForeignKey("protocols.id"))
    old_revision_id: Mapped[int] = mapped_column(ForeignKey("protocol_revisions.id"))
    new_revision_id: Mapped[int] = mapped_column(ForeignKey("protocol_revisions.id"))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|acknowledged
    input_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    analyses: Mapped[list] = mapped_column(JSONB, default=list)
    case_changes: Mapped[dict] = mapped_column(JSONB, default=dict)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
