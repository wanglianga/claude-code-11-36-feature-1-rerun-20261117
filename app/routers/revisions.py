"""Revision evaluations and migration dossiers.

A revision evaluation pins (old revision, new revision, old cutoff, new
cutoff), runs the four combinations and attributes every subject's change to
the protocol, the data, or their joint effect. A migration dossier freezes
the resulting impact catalog for an approved amendment — after re-checking
that the evaluation inputs have not moved on.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (
    Center,
    CutoffBatch,
    EvaluationCase,
    MigrationDossier,
    ProtocolRevision,
    RevisionEvaluation,
    Subject,
    User,
)
from ..schemas import DossierIn, RevisionCompareIn, RevisionEvaluationIn
from ..security import audit, require_roles
from ..services.conditions import change_variables, criteria_tree, diff_trees
from ..services.evaluate import (
    CATEGORY_LABELS,
    COMBO_LABELS,
    EvaluationError,
    EvaluationStale,
    create_dossier,
    run_evaluation,
    staleness_reasons,
)

router = APIRouter(tags=["revision-evaluation"])


def _evaluation_summary(db: Session, ev: RevisionEvaluation) -> dict:
    old_rev = db.get(ProtocolRevision, ev.old_revision_id)
    new_rev = db.get(ProtocolRevision, ev.new_revision_id)
    old_cutoff = db.get(CutoffBatch, ev.old_cutoff_id)
    new_cutoff = db.get(CutoffBatch, ev.new_cutoff_id)
    return {
        "id": ev.id,
        "status": ev.status,
        "stale_reason": ev.stale_reason,
        "protocol_id": ev.protocol_id,
        "old_revision": {"id": old_rev.id, "version": old_rev.version, "status": old_rev.status},
        "new_revision": {"id": new_rev.id, "version": new_rev.version, "status": new_rev.status},
        "old_cutoff": {"id": old_cutoff.id, "label": old_cutoff.label},
        "new_cutoff": {"id": new_cutoff.id, "label": new_cutoff.label},
        "input_fingerprint": ev.input_fingerprint,
        "budget_per_case": ev.budget_per_case,
        "combos": ev.combos,
        "combo_labels": COMBO_LABELS,
        "summary": ev.summary,
        "msc_stats": ev.msc_stats,
        "created_by": ev.created_by,
        "created_at": ev.created_at.isoformat(),
        "completed_at": ev.completed_at.isoformat() if ev.completed_at else None,
    }


@router.post("/revision-evaluations", status_code=201)
def create_evaluation(
    body: RevisionEvaluationIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("analysis_lead", "rule_reviewer")),
):
    old_rev = db.get(ProtocolRevision, body.old_revision_id)
    new_rev = db.get(ProtocolRevision, body.new_revision_id)
    if not old_rev or not new_rev:
        raise HTTPException(404, "protocol revision not found")
    if old_rev.protocol_id != new_rev.protocol_id:
        raise HTTPException(422, "the two revisions must belong to the same protocol")
    if not db.get(CutoffBatch, body.old_cutoff_id) or not db.get(CutoffBatch, body.new_cutoff_id):
        raise HTTPException(404, "cutoff batch not found")

    evaluation = RevisionEvaluation(
        protocol_id=old_rev.protocol_id,
        old_revision_id=old_rev.id,
        new_revision_id=new_rev.id,
        old_cutoff_id=body.old_cutoff_id,
        new_cutoff_id=body.new_cutoff_id,
        budget_per_case=body.budget_per_case,
        created_by=user.username,
    )
    db.add(evaluation)
    db.flush()
    try:
        run_evaluation(db, evaluation)
    except EvaluationError as exc:
        raise HTTPException(422, str(exc))
    audit(
        db,
        user.username,
        "revision_evaluation.run",
        "revision_evaluation",
        evaluation.id,
        {
            "old_revision": old_rev.version,
            "new_revision": new_rev.version,
            "old_cutoff": body.old_cutoff_id,
            "new_cutoff": body.new_cutoff_id,
            "categories": (evaluation.summary or {}).get("categories"),
        },
    )
    db.commit()
    return _evaluation_summary(db, evaluation)


@router.get("/revision-evaluations")
def list_evaluations(db: Session = Depends(get_db), user: User = Depends(require_roles())):
    rows = db.scalars(select(RevisionEvaluation).order_by(RevisionEvaluation.id)).all()
    return [_evaluation_summary(db, ev) for ev in rows]


@router.get("/revision-evaluations/{evaluation_id}")
def get_evaluation(
    evaluation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    ev = db.get(RevisionEvaluation, evaluation_id)
    if not ev:
        raise HTTPException(404, "evaluation not found")
    freshness = staleness_reasons(db, ev) if ev.status == "completed" else []
    return {
        **_evaluation_summary(db, ev),
        "condition_diff": ev.condition_diff,
        "change_variables": ev.change_variables,
        "data_revisions": ev.data_revisions,
        "category_labels": CATEGORY_LABELS,
        "freshness": {"stale": bool(freshness), "reasons": freshness},
    }


@router.get("/revision-evaluations/{evaluation_id}/conditions")
def evaluation_conditions(
    evaluation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    ev = db.get(RevisionEvaluation, evaluation_id)
    if not ev:
        raise HTTPException(404, "evaluation not found")
    return {
        "evaluation_id": ev.id,
        "condition_diff": ev.condition_diff,
        "change_variables": ev.change_variables,
        "note": "按语义节点比较：仅改显示名称的条件标记 label_only，不触发成员变化；"
        "结构不同的条件树不按片段拼接，整体作为一个原子变更",
    }


def _case_row(db: Session, case: EvaluationCase) -> dict:
    subject = db.get(Subject, case.subject_id)
    center = db.get(Center, case.center_id)
    return {
        "study_id": subject.study_id if subject else None,
        "subject_id": case.subject_id,
        "center_code": center.code if center else None,
        "outcomes": case.outcomes,
        "index_dates": case.index_dates,
        "category": case.category,
        "category_label": CATEGORY_LABELS.get(case.category),
        "reason_changed": case.reason_changed,
        "condition_interaction": case.condition_interaction,
        "data_tags": case.data_tags,
        "msc": case.msc,
    }


@router.get("/revision-evaluations/{evaluation_id}/cases")
def evaluation_cases(
    evaluation_id: int,
    category: str | None = None,
    only_changed: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    ev = db.get(RevisionEvaluation, evaluation_id)
    if not ev:
        raise HTTPException(404, "evaluation not found")
    stmt = (
        select(EvaluationCase, Subject, Center)
        .join(Subject, Subject.id == EvaluationCase.subject_id)
        .join(Center, Center.id == EvaluationCase.center_id)
        .where(EvaluationCase.evaluation_id == evaluation_id)
        .order_by(Center.code, Subject.study_id)
    )
    rows = db.execute(stmt).all()
    out = []
    for case, subject, center in rows:
        if category and case.category != category:
            continue
        changed = (
            case.category != "unchanged"
            or case.reason_changed
            or case.condition_interaction
            or any(
                row["status"].get("old_old") != row["status"].get("new_new")
                for row in (case.endpoint_status or {}).values()
            )
        )
        if only_changed and not changed:
            continue
        out.append(
            {
                "study_id": subject.study_id,
                "subject_id": subject.id,
                "center_code": center.code,
                "outcomes": case.outcomes,
                "index_dates": case.index_dates,
                "category": case.category,
                "category_label": CATEGORY_LABELS.get(case.category),
                "reason_changed": case.reason_changed,
                "condition_interaction": case.condition_interaction,
                "data_tags": case.data_tags,
                "msc": case.msc,
            }
        )
    return out


@router.get("/revision-evaluations/{evaluation_id}/cases/{study_id}")
def evaluation_case_detail(
    evaluation_id: int,
    study_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    ev = db.get(RevisionEvaluation, evaluation_id)
    if not ev:
        raise HTTPException(404, "evaluation not found")
    subject = db.scalar(select(Subject).where(Subject.study_id == study_id))
    if not subject:
        raise HTTPException(404, "subject not found")
    case = db.scalar(
        select(EvaluationCase).where(
            EvaluationCase.evaluation_id == evaluation_id,
            EvaluationCase.subject_id == subject.id,
        )
    )
    if not case:
        raise HTTPException(404, "subject not in this evaluation's scope")
    center = db.get(Center, case.center_id)
    return {
        "study_id": subject.study_id,
        "subject_id": subject.id,
        "center_code": center.code if center else None,
        "outcomes": case.outcomes,
        "combo_labels": COMBO_LABELS,
        "index_dates": case.index_dates,
        "reason_codes": case.reason_codes,
        "evidence": case.evidence,
        "category": case.category,
        "category_label": CATEGORY_LABELS.get(case.category),
        "reason_changed": case.reason_changed,
        "condition_interaction": case.condition_interaction,
        "condition_verdicts": case.condition_verdicts,
        "endpoint_status": case.endpoint_status,
        "endpoint_attribution": case.endpoint_attribution,
        "data_tags": case.data_tags,
        "msc": case.msc,
    }


@router.get("/revision-evaluations/{evaluation_id}/impact")
def evaluation_impact(
    evaluation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    ev = db.get(RevisionEvaluation, evaluation_id)
    if not ev:
        raise HTTPException(404, "evaluation not found")
    return {
        "evaluation_id": ev.id,
        "impact": ev.impact,
        "data_revisions": ev.data_revisions,
    }


@router.post("/revision-evaluations/{evaluation_id}/migration-dossiers", status_code=201)
def make_dossier(
    evaluation_id: int,
    body: DossierIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("analysis_lead")),
):
    ev = db.get(RevisionEvaluation, evaluation_id)
    if not ev:
        raise HTTPException(404, "evaluation not found")
    try:
        dossier = create_dossier(db, ev, user.username, body.note)
    except EvaluationStale as exc:
        audit(
            db,
            user.username,
            "migration_dossier.stale",
            "revision_evaluation",
            evaluation_id,
            {"reasons": exc.reasons},
        )
        db.commit()
        raise HTTPException(
            409,
            {
                "message": "评估输入已失效，必须重新评估，不能沿用旧差异清单",
                "reasons": exc.reasons,
            },
        )
    except EvaluationError as exc:
        raise HTTPException(409, str(exc))
    audit(
        db,
        user.username,
        "migration_dossier.create",
        "migration_dossier",
        dossier.id,
        {"evaluation_id": evaluation_id, "analyses": len(dossier.analyses)},
    )
    db.commit()
    return _dossier_view(db, dossier)


def _dossier_view(db: Session, dossier: MigrationDossier) -> dict:
    old_rev = db.get(ProtocolRevision, dossier.old_revision_id)
    new_rev = db.get(ProtocolRevision, dossier.new_revision_id)
    return {
        "id": dossier.id,
        "evaluation_id": dossier.evaluation_id,
        "status": dossier.status,
        "protocol_id": dossier.protocol_id,
        "old_revision": {"id": old_rev.id, "version": old_rev.version},
        "new_revision": {"id": new_rev.id, "version": new_rev.version},
        "input_snapshot": dossier.input_snapshot,
        "analyses": dossier.analyses,
        "case_changes": dossier.case_changes,
        "note": dossier.note,
        "created_by": dossier.created_by,
        "created_at": dossier.created_at.isoformat(),
        "governance": "案卷只记录影响与处置建议；不重写旧报告，不重新批准方案修订",
    }


@router.get("/migration-dossiers")
def list_dossiers(db: Session = Depends(get_db), user: User = Depends(require_roles())):
    rows = db.scalars(select(MigrationDossier).order_by(MigrationDossier.id)).all()
    return [_dossier_view(db, d) for d in rows]


@router.get("/migration-dossiers/{dossier_id}")
def get_dossier(
    dossier_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles()),
):
    dossier = db.get(MigrationDossier, dossier_id)
    if not dossier:
        raise HTTPException(404, "dossier not found")
    return _dossier_view(db, dossier)


@router.post("/migration-dossiers/{dossier_id}/acknowledge")
def acknowledge_dossier(
    dossier_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("analysis_lead")),
):
    """Record that the research lead reviewed the dossier. This never
    re-approves the protocol and never edits a report."""
    dossier = db.get(MigrationDossier, dossier_id)
    if not dossier:
        raise HTTPException(404, "dossier not found")
    dossier.status = "acknowledged"
    audit(db, user.username, "migration_dossier.acknowledge", "migration_dossier", dossier.id)
    db.commit()
    return _dossier_view(db, dossier)


@router.post("/protocols/revisions/compare")
def compare_revisions(
    body: RevisionCompareIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("rule_reviewer", "analysis_lead")),
):
    """Semantic condition diff between two revisions.

    Nodes are compared by stable condition id and semantic content, so two
    people editing the same tree concurrently get a node-level alignment —
    never a splice of two individually legal fragments into a third meaning.
    """
    old_rev = db.get(ProtocolRevision, body.old_revision_id)
    new_rev = db.get(ProtocolRevision, body.new_revision_id)
    if not old_rev or not new_rev:
        raise HTTPException(404, "protocol revision not found")
    old_rules = old_rev.rules or {}
    new_rules = new_rev.rules or {}
    diff = diff_trees(criteria_tree(old_rules), criteria_tree(new_rules))
    variables = change_variables(old_rules, new_rules, diff)
    return {
        "old_revision": {"id": old_rev.id, "version": old_rev.version},
        "new_revision": {"id": new_rev.id, "version": new_rev.version},
        "condition_diff": diff,
        "change_variables": variables,
        "note": "仅显示名称变化（label_only）不计入语义差异；结构变化时条件块整体作为一个原子变更",
    }
