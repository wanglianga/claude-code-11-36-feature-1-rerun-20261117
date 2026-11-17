"""Shared Pydantic schemas and small helpers for the API layer."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CenterIn(BaseModel):
    code: str = Field(min_length=2, max_length=40)
    name: str
    config: dict[str, Any] = Field(default_factory=dict)


class MappingIn(BaseModel):
    center_code: str
    kind: str
    source_code: str
    study_code: str
    note: str | None = None


class ProtocolIn(BaseModel):
    name: str
    description: str | None = None


class RevisionIn(BaseModel):
    version: str
    rules: dict[str, Any]


class UploadIn(BaseModel):
    center_code: str
    batch_code: str
    file_name: str | None = None
    records: list[dict[str, Any]]


class CutoffIn(BaseModel):
    label: str
    upload_ids: list[int]


class RunIn(BaseModel):
    protocol_revision_id: int
    cutoff_batch_id: int
    strategy: Literal["full", "incremental"] = "full"
    base_run_id: int | None = None
    trigger_type: Literal["upload", "mapping", "manual"] | None = None
    trigger_ref: str | None = None
    # Fault-injection hooks used by the tests and the demo to prove that an
    # interrupted run resumes from its per-centre checkpoints instead of
    # restarting. Refused unless COHORT_ALLOW_FAULT_INJECTION is enabled.
    max_centers: int | None = None
    crash_after_centers: int | None = None


class IdentityDecisionIn(BaseModel):
    status: Literal["confirmed", "rejected"]
    note: str | None = None


class AnalysisIn(BaseModel):
    kind: Literal["efficacy", "safety"] = "efficacy"


class ReportIn(BaseModel):
    analysis_run_id: int
    note: str | None = None


class ConsentWithdrawalIn(BaseModel):
    allow_retention: bool = False
    withdrawal_date: str | None = None


class RevisionEvaluationIn(BaseModel):
    old_revision_id: int
    new_revision_id: int
    old_cutoff_id: int
    new_cutoff_id: int
    # Budget of mixed evaluations per case when searching minimal sufficient
    # change sets. Exhaustion keeps proven sets and marks the case partial.
    budget_per_case: int = Field(default=32, ge=1, le=256)


class RevisionCompareIn(BaseModel):
    old_revision_id: int
    new_revision_id: int


class DossierIn(BaseModel):
    note: str | None = None
