"""Revision evaluations: four-combination differential attribution.

One evaluation pins down four inputs — old protocol revision, new protocol
revision, old data cutoff, new data cutoff — and actually runs the four
combinations:

* ``old_old``  baseline: old rules on old data
* ``new_old``  protocol-only: new rules on old data
* ``old_new``  data-only: old rules on new data
* ``new_new``  joint: new rules on new data

Every subject gets a per-combination outcome, index date, reason codes,
evidence and per-condition three-state verdicts. Cases whose final state
changed (or whose conditions interact) are explained with **minimal
sufficient change sets**: the smallest combinations of changes (condition
edits, index-date revision, data cutoff) that already reproduce the final
outcome. The search is budget-limited; when the budget runs out the case
keeps the combinations proven so far and is marked partial — a reason found
first is never advertised as the only cause.

Identity adjudications and code-mapping corrections that happened in the same
window are listed as their own data revisions, never mixed into the protocol
effect. The evaluation is read-only for the rest of the system: it creates no
snapshots, rewrites no reports and approves no revisions.
"""
from __future__ import annotations

import hashlib
import itertools
import json
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    AnalysisRun,
    Center,
    CodeMapping,
    CohortSnapshot,
    CutoffBatch,
    EvaluationCase,
    FollowUpPlan,
    FollowUpTask,
    IdentityCandidate,
    MigrationDossier,
    NormalizedRecord,
    ProtocolRevision,
    Report,
    RevisionEvaluation,
    SnapshotCase,
    SourceRecord,
    Subject,
    Upload,
)
from .conditions import change_variables, criteria_tree, diff_trees, mix_rules
from .screening import (
    INCLUDED,
    evaluate_subject,
    load_center_subjects,
    load_subject_data,
    pending_identity_for,
    rules_hash,
)

COMBOS = ("old_old", "new_old", "old_new", "new_new")
COMBO_LABELS = {
    "old_old": "旧方案 × 旧资料（基线）",
    "new_old": "新方案 × 旧资料（仅方案变化）",
    "old_new": "旧方案 × 新资料（仅资料变化）",
    "new_new": "新方案 × 新资料（联合）",
}

CATEGORY_LABELS = {
    "unchanged": "无变化",
    "protocol_only": "仅方案影响",
    "data_only": "仅资料影响",
    "joint": "联合影响（需方案与资料共同成立）",
    "masked": "最终状态不变，但中间组合存在差异（抵消/遮蔽）",
}

OUTCOME_ORDER = [
    "included",
    "insufficient_data",
    "pending_adjudication",
    "excluded_ineligible",
    "consent_withdrawn",
    "not_in_scope",
]


class EvaluationError(RuntimeError):
    pass


class EvaluationStale(EvaluationError):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------
def run_evaluation(db: Session, evaluation: RevisionEvaluation) -> RevisionEvaluation:
    old_rev = db.get(ProtocolRevision, evaluation.old_revision_id)
    new_rev = db.get(ProtocolRevision, evaluation.new_revision_id)
    old_cutoff = db.get(CutoffBatch, evaluation.old_cutoff_id)
    new_cutoff = db.get(CutoffBatch, evaluation.new_cutoff_id)
    if not all([old_rev, new_rev, old_cutoff, new_cutoff]):
        raise EvaluationError("evaluation inputs are incomplete")
    old_rules = old_rev.rules or {}
    new_rules = new_rev.rules or {}

    diff = diff_trees(criteria_tree(old_rules), criteria_tree(new_rules))
    diff["rules_hash_old"] = rules_hash(old_rules)
    diff["rules_hash_new"] = rules_hash(new_rules)
    variables = change_variables(old_rules, new_rules, diff)
    old_uploads = sorted(int(u) for u in (old_cutoff.upload_ids or []))
    new_uploads = sorted(int(u) for u in (new_cutoff.upload_ids or []))
    if old_uploads != new_uploads:
        variables.append({"id": "data", "kind": "data", "label": "资料截止批次"})

    evaluation.condition_diff = diff
    evaluation.change_variables = variables
    evaluation.input_fingerprint = _input_fingerprint(old_rules, new_rules, old_cutoff, new_cutoff)

    centers = db.scalars(select(Center).where(Center.active.is_(True)).order_by(Center.id)).all()
    scope: dict[int, dict] = {}
    for center in centers:
        for tag, upload_ids in (("old", old_uploads), ("new", new_uploads)):
            if not upload_ids:
                continue
            for subject in load_center_subjects(db, center.id, upload_ids):
                entry = scope.setdefault(
                    subject.id,
                    {"old": False, "new": False, "subject": subject, "center_id": center.id},
                )
                entry[tag] = True

    data_revisions, subject_tags = _data_revisions(db, old_cutoff, new_cutoff)
    study_of = {sid: entry["subject"].study_id for sid, entry in scope.items()}

    msc_stats = {"complete": 0, "partial": 0, "not_needed": 0, "not_applicable": 0, "evaluations_used": 0}
    cases: list[EvaluationCase] = []
    for sid in sorted(scope):
        entry = scope[sid]
        subject = entry["subject"]
        identity_pending = pending_identity_for(db, subject)
        data_cache: dict[str, object] = {}
        combo_results: dict[str, dict | None] = {}
        for combo in COMBOS:
            rules = old_rules if combo.startswith("old") else new_rules
            tag = "old" if combo.endswith("old") else "new"
            if not entry[tag]:
                combo_results[combo] = None
                continue
            if tag not in data_cache:
                data_cache[tag] = load_subject_data(
                    db, sid, old_uploads if tag == "old" else new_uploads
                )
            combo_results[combo] = evaluate_subject(
                db,
                rules,
                data_cache[tag],
                identity_pending,
                old_rev.id if combo.startswith("old") else new_rev.id,
                want_trace=True,
            )
        case = _build_case(
            db,
            evaluation,
            subject,
            entry,
            combo_results,
            diff,
            variables,
            old_rules,
            new_rules,
            data_cache,
            identity_pending,
            subject_tags.get(sid, []),
            msc_stats,
        )
        db.add(case)
        cases.append(case)

    evaluation.combos = _combo_counts(cases)
    evaluation.summary = _summary(cases, study_of)
    evaluation.data_revisions = data_revisions
    evaluation.impact = _impact(db, evaluation, cases, old_rev, study_of)
    evaluation.msc_stats = msc_stats
    evaluation.status = "completed"
    evaluation.completed_at = datetime.now(timezone.utc)
    db.commit()
    return evaluation


def _input_fingerprint(old_rules, new_rules, old_cutoff, new_cutoff) -> str:
    material = {
        "old_rules": rules_hash(old_rules),
        "new_rules": rules_hash(new_rules),
        "old_uploads": sorted(int(u) for u in (old_cutoff.upload_ids or [])),
        "new_uploads": sorted(int(u) for u in (new_cutoff.upload_ids or [])),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------
# per-case comparison
# ---------------------------------------------------------------------------
def _build_case(
    db: Session,
    evaluation: RevisionEvaluation,
    subject: Subject,
    entry: dict,
    combo_results: dict,
    diff: dict,
    variables: list[dict],
    old_rules: dict,
    new_rules: dict,
    data_cache: dict,
    identity_pending: bool,
    tags: list[str],
    msc_stats: dict,
) -> EvaluationCase:
    outcomes = {c: (r["outcome"] if r else None) for c, r in combo_results.items()}
    index_dates = {
        c: (r["index_date"].isoformat() if r and r["index_date"] else None)
        for c, r in combo_results.items()
    }
    reasons = {c: (list(r["reason_codes"]) if r else None) for c, r in combo_results.items()}
    evidence = {c: (list(r["evidence"]) if r else None) for c, r in combo_results.items()}

    a, b, c_, d = (outcomes[k] for k in COMBOS)
    category = _categorize(a, b, c_, d)
    # The explanation changed when the *decisive* reason (the one that settled
    # the outcome) differs between baseline and final, e.g. an exclusion that
    # used to be a lab violation is now an age violation.
    reason_changed = _decisive(reasons["old_old"], a) != _decisive(reasons["new_new"], d)

    # Per-condition verdict alignment across the four combinations.
    verdict_rows: dict[str, dict] = {}
    flips = 0
    for cond in diff.get("conditions", []):
        cid = cond["condition_id"]
        row = {
            "change": cond["change"],
            "label_only": bool(cond.get("label_only")),
            "label_old": cond.get("label_old"),
            "label_new": cond.get("label_new"),
            "verdicts": {},
        }
        for combo in COMBOS:
            res = combo_results[combo]
            verdicts = (res.get("condition_verdicts") or {}) if res else {}
            row["verdicts"][combo] = verdicts.get(cid)
        v_old, v_new = row["verdicts"]["old_old"], row["verdicts"]["new_new"]
        # Only parameter-changed conditions count toward an interaction, and
        # only when both sides were actually evaluated (a subject outside the
        # old scope has no baseline verdict to flip against). An added/removed
        # condition always "flips" against its own absence and would make
        # every case look interactive.
        if cond["change"] == "changed" and v_old is not None and v_new is not None and v_old != v_new:
            flips += 1
        verdict_rows[cid] = row
    interaction = flips >= 2

    # Endpoint eligibility per combination.
    endpoints: dict[str, dict] = {}
    for combo in COMBOS:
        res = combo_results[combo]
        for key, value in ((res or {}).get("endpoint_status") or {}).items():
            row = endpoints.setdefault(key, {"status": {}, "evidence": {}})
            row["status"][combo] = value["status"]
            if combo in ("old_old", "new_new"):
                row["evidence"][combo] = value.get("evidence") or []
    endpoint_attribution = {}
    if a == INCLUDED and d == INCLUDED:
        endpoint_attribution = _endpoint_attribution(endpoints, index_dates)

    # Minimal sufficient change sets: computed only for cases with a real
    # state change (protocol_only/data_only/joint) or a condition interaction
    # — never to "explain" an outcome that did not change.
    needs_msc = category in ("protocol_only", "data_only", "joint") or interaction
    msc = {"status": "not_needed", "sets": [], "evaluations_used": 0, "budget": evaluation.budget_per_case}
    if needs_msc and d is not None:
        old_uploads_data = data_cache.get("old")
        new_uploads_data = data_cache.get("new")

        def evaluate_mixed(var_ids: list[str]) -> str:
            rule_vars = [v for v in var_ids if v != "data"]
            rules = mix_rules(old_rules, new_rules, rule_vars) if rule_vars else old_rules
            data = new_uploads_data if "data" in var_ids else old_uploads_data
            if data is None:
                return "not_in_scope"
            # protocol_revision_id is unused by the evaluator; 0 is a placeholder.
            return evaluate_subject(db, rules, data, identity_pending, 0)["outcome"]

        var_ids = [v["id"] for v in variables]
        sets, used, status = _msc_search(evaluate_mixed, var_ids, d, evaluation.budget_per_case)
        msc = {
            "status": status,
            "sets": [sorted(s) for s in sets],
            "evaluations_used": used,
            "budget": evaluation.budget_per_case,
            "target_outcome": d,
        }
        if status == "partial":
            msc["note"] = (
                "预算耗尽：以下仅为已证明的充分组合，可能还有其他解释；"
                "未解释范围需人工排查，不能把已找到的组合当作唯一原因"
            )
        msc_stats["evaluations_used"] += used
        msc_stats[status] = msc_stats.get(status, 0) + 1
    elif needs_msc:
        msc = {"status": "not_applicable", "sets": [], "evaluations_used": 0,
               "note": "病例不在新资料范围内，无法构造混合评估"}
        msc_stats["not_applicable"] += 1
    else:
        msc_stats["not_needed"] += 1

    return EvaluationCase(
        evaluation_id=evaluation.id,
        subject_id=subject.id,
        center_id=entry["center_id"],
        outcomes=outcomes,
        index_dates=index_dates,
        reason_codes=reasons,
        evidence=evidence,
        category=category,
        reason_changed=reason_changed,
        condition_interaction=interaction,
        condition_verdicts=verdict_rows,
        endpoint_status=endpoints,
        endpoint_attribution=endpoint_attribution,
        data_tags=sorted(tags),
        msc=msc,
    )


def _decisive(reasons: list[str] | None, outcome: str | None) -> str | None:
    """The reason code that settled the outcome (the last one recorded).

    ``included`` has no decisive reason: every condition passed. Comparing
    decisive reasons instead of full reason lists keeps cosmetic differences
    (a shifted age value, an extra satisfied condition) out of the signal.
    """
    if reasons is None or outcome in (None, "included"):
        return None
    return reasons[-1] if reasons else None


def _categorize(a, b, c, d) -> str:
    """Classify the four-combination outcome pattern.

    ``a`` old_old, ``b`` new_old, ``c`` old_new, ``d`` new_new.
    """
    if a == b == c == d:
        return "unchanged"
    if a == c and b == d:
        return "protocol_only"
    if a == b and c == d:
        return "data_only"
    if a == d:
        # Final state unchanged, but at least one intermediate combination
        # differs: a loosening and a tightening cancelled out, or a joint
        # effect is masked. Kept visible instead of collapsing to "unchanged".
        return "masked"
    return "joint"


def _msc_search(evaluate_fn, var_ids: list[str], target: str, budget: int):
    """Enumerate change subsets by increasing size; a subset is sufficient
    when it reproduces the final outcome, and minimal when no proper subset
    does. Budget exhaustion keeps the proven sets and reports partial."""
    found: list[frozenset] = []
    used = 0
    for size in range(1, len(var_ids) + 1):
        for subset in itertools.combinations(var_ids, size):
            current = frozenset(subset)
            if any(f <= current for f in found):
                continue  # a proper subset already suffices: not minimal
            if used >= budget:
                return found, used, "partial"
            if evaluate_fn(sorted(current)) == target:
                found.append(current)
            used += 1
    return found, used, "complete"


def _endpoint_attribution(endpoints: dict, index_dates: dict) -> dict:
    """Attribute endpoint flips using the four-combination table itself.

    Holding the data fixed (new cutoff), old rules vs new rules isolates the
    protocol-side effect; holding the rules fixed isolates the data-side
    effect. Endpoint windows are anchored to the index date, so an index-date
    revision re-judges eligibility along that dependency — the shift is
    reported explicitly.
    """
    out: dict[str, dict] = {}
    for key, row in endpoints.items():
        status = row.get("status", {})
        a, b = status.get("old_old"), status.get("new_old")
        c, d = status.get("old_new"), status.get("new_new")
        if a == d:
            continue

        def applicable(s):
            # not_applicable means the subject is outside the cohort there —
            # that is the membership change speaking, not the endpoint.
            return s in ("eligible", "not_eligible", "unknown")

        rule_side = (applicable(b) and applicable(a) and b != a) or (
            applicable(d) and applicable(c) and d != c
        )
        data_side = (applicable(c) and applicable(a) and c != a) or (
            applicable(d) and applicable(b) and d != b
        )
        idx_old, idx_new = index_dates.get("old_old"), index_dates.get("new_new")
        note_parts = []
        if rule_side:
            note_parts.append("方案侧变化改变终点资格")
        if data_side:
            note_parts.append("资料侧变化改变终点资格")
        if rule_side and idx_old != idx_new:
            note_parts.append(
                f"终点窗口锚定索引日期，索引日期 {idx_old} → {idx_new} 使随访重新落入/落出窗口"
            )
        out[key] = {
            "old": a,
            "new": d,
            "combos": status,
            "rule_side_effect": rule_side,
            "data_side_effect": data_side,
            "index_date_shift": {"old": idx_old, "new": idx_new} if idx_old != idx_new else None,
            "note": "；".join(note_parts) or "终点资格沿依赖重新判定",
        }
    return out


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------
def _combo_counts(cases: list[EvaluationCase]) -> dict:
    counts = {combo: {k: 0 for k in OUTCOME_ORDER} for combo in COMBOS}
    for case in cases:
        for combo in COMBOS:
            outcome = case.outcomes.get(combo) or "not_in_scope"
            counts[combo][outcome] = counts[combo].get(outcome, 0) + 1
    return counts


def _summary(cases: list[EvaluationCase], study_of: dict[int, str]) -> dict:
    categories: dict[str, int] = {}
    for case in cases:
        categories[case.category] = categories.get(case.category, 0) + 1
    old_included = sorted(c.subject_id for c in cases if c.outcomes.get("old_old") == INCLUDED)
    new_included = sorted(c.subject_id for c in cases if c.outcomes.get("new_new") == INCLUDED)
    entered = sorted(set(new_included) - set(old_included))
    left = sorted(set(old_included) - set(new_included))
    names = lambda sids: sorted(study_of.get(s, str(s)) for s in sids)  # noqa: E731
    return {
        "subjects_total": len(cases),
        "categories": categories,
        "membership": {
            "count_old": len(old_included),
            "count_new": len(new_included),
            "entered": names(entered),
            "left": names(left),
            "net_change": len(new_included) - len(old_included),
        },
        "reason_changed": names(c.subject_id for c in cases if c.reason_changed),
        "condition_interactions": names(c.subject_id for c in cases if c.condition_interaction),
        "index_date_shifted": names(
            c.subject_id
            for c in cases
            if c.index_dates.get("old_old") != c.index_dates.get("new_new")
        ),
    }


def _impact(db: Session, evaluation: RevisionEvaluation, cases: list[EvaluationCase], old_rev, study_of: dict[int, str]) -> dict:
    by_sid = {c.subject_id: c for c in cases}
    new_included = {c.subject_id for c in cases if c.outcomes.get("new_new") == INCLUDED}
    name = lambda sid: study_of.get(sid, str(sid))  # noqa: E731

    analyses = []
    rows = db.execute(
        select(AnalysisRun, CohortSnapshot)
        .join(CohortSnapshot, CohortSnapshot.id == AnalysisRun.snapshot_id)
        .where(CohortSnapshot.protocol_revision_id == old_rev.id)
        .order_by(AnalysisRun.id)
    ).all()
    for analysis, snapshot in rows:
        members = db.scalars(
            select(SnapshotCase).where(SnapshotCase.snapshot_id == snapshot.id)
        ).all()
        changed_members = []
        for member in members:
            case = by_sid.get(member.subject_id)
            if case is None:
                continue
            outcome_changed = case.outcomes.get("old_old") != case.outcomes.get("new_new")
            endpoint_changed = _endpoint_changed(case)
            if outcome_changed or endpoint_changed:
                changed_members.append(
                    {
                        "study_id": name(member.subject_id),
                        "subject_id": member.subject_id,
                        "outcome_old": case.outcomes.get("old_old"),
                        "outcome_new": case.outcomes.get("new_new"),
                        "endpoint_changed": endpoint_changed,
                    }
                )
        growth = sorted(name(s) for s in (new_included - {m.subject_id for m in members}))
        reports = db.scalars(
            select(Report).where(Report.analysis_run_id == analysis.id).order_by(Report.id)
        ).all()
        analyses.append(
            {
                "analysis_run_id": analysis.id,
                "kind": analysis.kind,
                "snapshot_id": snapshot.id,
                "snapshot_status": snapshot.status,
                "disposition": "needs_reconfirmation" if changed_members else "reusable",
                "changed_members": changed_members,
                "new_members_elsewhere": growth,
                "reports": [
                    {
                        "report_id": r.id,
                        "report_version": r.report_version,
                        "content_hash": r.content_hash,
                        "frozen": r.frozen,
                    }
                    for r in reports
                ],
                "note": (
                    "队列成员或终点资格有变化，需研究者重新确认"
                    if changed_members
                    else "成员资格与终点判定均未变，可继续引用旧队列"
                ),
            }
        )

    shifted = {
        c.subject_id
        for c in cases
        if c.index_dates.get("old_old") != c.index_dates.get("new_new")
    }
    follow_up_count = 0
    if shifted:
        follow_up_count = int(
            db.scalar(
                select(func.count(FollowUpTask.id))
                .join(FollowUpPlan, FollowUpPlan.id == FollowUpTask.plan_id)
                .join(CohortSnapshot, CohortSnapshot.id == FollowUpPlan.snapshot_id)
                .where(
                    FollowUpTask.subject_id.in_(shifted),
                    FollowUpTask.status == "scheduled",
                    CohortSnapshot.protocol_revision_id == old_rev.id,
                )
            )
            or 0
        )

    endpoint_changes = []
    for case in cases:
        if not _endpoint_changed(case):
            continue
        for key, row in (case.endpoint_status or {}).items():
            old, new = row["status"].get("old_old"), row["status"].get("new_new")
            if old != new:
                endpoint_changes.append(
                    {
                        "study_id": name(case.subject_id),
                        "subject_id": case.subject_id,
                        "endpoint": key,
                        "old": old,
                        "new": new,
                    }
                )

    return {
        "analyses": analyses,
        "endpoints": {"changed": endpoint_changes, "changed_count": len(endpoint_changes)},
        "index_date_shifted": sorted(name(s) for s in shifted),
        "follow_up_tasks_to_review": {
            "count": follow_up_count,
            "note": "索引日期平移影响随访排期锚点；本功能不改动任何随访任务",
        },
        "scope_note": "本目录仅提供研究数据口径与影响解释，不改变受试者治疗或随访安排",
    }


def _endpoint_changed(case: EvaluationCase) -> bool:
    """Endpoint eligibility moved while the subject stays in the cohort.

    A subject who leaves the cohort gets ``not_applicable`` endpoints — that
    is their membership change speaking, not an endpoint re-judgement.
    """
    if case.outcomes.get("old_old") != INCLUDED or case.outcomes.get("new_new") != INCLUDED:
        return False
    for row in (case.endpoint_status or {}).values():
        if row.get("status", {}).get("old_old") != row.get("status", {}).get("new_new"):
            return True
    return False


# ---------------------------------------------------------------------------
# data revisions (listed separately, never attributed to the protocol)
# ---------------------------------------------------------------------------
def _data_revisions(db: Session, old_cutoff: CutoffBatch, new_cutoff: CutoffBatch):
    old_ids = set(int(u) for u in (old_cutoff.upload_ids or []))
    new_only = [int(u) for u in (new_cutoff.upload_ids or []) if u not in old_ids]
    subject_tags: dict[int, set[str]] = {}

    def tag(sid, label):
        if sid:
            subject_tags.setdefault(int(sid), set()).add(label)

    uploads = []
    if new_only:
        for upload in db.scalars(select(Upload).where(Upload.id.in_(new_only)).order_by(Upload.id)).all():
            center = db.get(Center, upload.center_id)
            rows = db.execute(
                select(NormalizedRecord.subject_id)
                .join(SourceRecord, SourceRecord.id == NormalizedRecord.source_record_id)
                .where(
                    SourceRecord.upload_id == upload.id,
                    NormalizedRecord.subject_id.is_not(None),
                )
            ).all()
            sids = {r[0] for r in rows if r[0]}
            corrected = set(
                r[0]
                for r in db.execute(
                    select(NormalizedRecord.subject_id)
                    .join(SourceRecord, SourceRecord.id == NormalizedRecord.source_record_id)
                    .where(
                        SourceRecord.upload_id == upload.id,
                        SourceRecord.revision > 1,
                        NormalizedRecord.subject_id.is_not(None),
                    )
                ).all()
                if r[0]
            )
            for sid in sids:
                tag(sid, "new_rows")
            for sid in corrected:
                tag(sid, "correction")
            uploads.append(
                {
                    "upload_id": upload.id,
                    "center_code": center.code if center else None,
                    "batch_code": upload.batch_code,
                    "subjects": sorted(sids),
                    "correction_subjects": sorted(corrected),
                }
            )

    mappings = []
    for mapping in db.scalars(
        select(CodeMapping).where(CodeMapping.created_at > old_cutoff.created_at).order_by(CodeMapping.id)
    ).all():
        center = db.get(Center, mapping.center_id)
        rows = db.execute(
            select(NormalizedRecord.subject_id)
            .join(SourceRecord, SourceRecord.id == NormalizedRecord.source_record_id)
            .where(
                NormalizedRecord.center_id == mapping.center_id,
                NormalizedRecord.kind == mapping.kind,
                NormalizedRecord.source_code == mapping.source_code,
                NormalizedRecord.subject_id.is_not(None),
            )
        ).all()
        sids = {r[0] for r in rows if r[0]}
        for sid in sids:
            tag(sid, "remapped")
        mappings.append(
            {
                "mapping_id": mapping.id,
                "center_code": center.code if center else None,
                "kind": mapping.kind,
                "source_code": mapping.source_code,
                "study_code": mapping.study_code,
                "created_at": mapping.created_at.isoformat(),
                "affected_subjects": sorted(sids),
            }
        )

    identity = []
    candidates = db.scalars(
        select(IdentityCandidate).where(
            (IdentityCandidate.created_at > old_cutoff.created_at)
            | (IdentityCandidate.decided_at > old_cutoff.created_at)
        )
    ).all()
    for cand in candidates:
        for sid in (cand.subject_a_id, cand.subject_b_id):
            tag(sid, "identity")
        identity.append(
            {
                "candidate_id": cand.id,
                "subjects": sorted([cand.subject_a_id, cand.subject_b_id]),
                "status": cand.status,
                "decided_at": cand.decided_at.isoformat() if cand.decided_at else None,
            }
        )

    # Present subjects by study_id in the report; keep internal tags by id.
    all_sids: set[int] = set()
    for upload in uploads:
        all_sids.update(upload["subjects"])
        all_sids.update(upload["correction_subjects"])
    for mapping in mappings:
        all_sids.update(mapping["affected_subjects"])
    for cand in identity:
        all_sids.update(cand["subjects"])
    study_of = {
        s.id: s.study_id
        for s in db.scalars(select(Subject).where(Subject.id.in_(all_sids or {0}))).all()
    }

    def names(sids):
        return sorted(study_of.get(s, str(s)) for s in sids)

    for upload in uploads:
        upload["subjects"] = names(upload["subjects"])
        upload["correction_subjects"] = names(upload["correction_subjects"])
    for mapping in mappings:
        mapping["affected_subjects"] = names(mapping["affected_subjects"])
    for cand in identity:
        cand["subjects"] = names(cand["subjects"])

    revisions = {
        "uploads": uploads,
        "mappings": mappings,
        "identity": identity,
        "note": "补传、编码映射与身份裁决单列；其影响不计入方案修订影响",
    }
    return revisions, subject_tags


# ---------------------------------------------------------------------------
# staleness and migration dossiers
# ---------------------------------------------------------------------------
def staleness_reasons(db: Session, evaluation: RevisionEvaluation) -> list[str]:
    """Inputs moved on since the evaluation ran? Then its diff list is void."""
    reasons: list[str] = []
    since = evaluation.completed_at or evaluation.created_at
    old_cutoff = db.get(CutoffBatch, evaluation.old_cutoff_id)
    new_cutoff = db.get(CutoffBatch, evaluation.new_cutoff_id)
    covered = set(int(u) for u in (new_cutoff.upload_ids or []))
    all_ids = covered | set(int(u) for u in (old_cutoff.upload_ids or []))
    center_ids = {
        u.center_id for u in db.scalars(select(Upload).where(Upload.id.in_(all_ids))).all()
    } if all_ids else set()

    if center_ids:
        late_uploads = db.scalars(
            select(Upload).where(
                Upload.center_id.in_(center_ids),
                Upload.status == "completed",
                Upload.received_at > since,
            )
        ).all()
        for upload in late_uploads:
            if upload.id not in covered:
                center = db.get(Center, upload.center_id)
                reasons.append(
                    f"中心 {center.code if center else upload.center_id} 在评估完成后又有补传 "
                    f"{upload.batch_code}（未包含在评估的新截止批次中）"
                )
        late_mappings = db.scalars(
            select(CodeMapping).where(
                CodeMapping.center_id.in_(center_ids),
                CodeMapping.created_at > since,
            )
        ).all()
        for mapping in late_mappings:
            reasons.append(f"评估完成后新增编码映射 {mapping.kind}:{mapping.source_code}")
        late_identity = db.scalars(
            select(IdentityCandidate).where(
                (IdentityCandidate.created_at > since) | (IdentityCandidate.decided_at > since)
            )
        ).all()
        if late_identity:
            reasons.append(f"评估完成后有 {len(late_identity)} 条身份候选发生变化")

    diff = evaluation.condition_diff or {}
    for rev_id, key, label in (
        (evaluation.old_revision_id, "rules_hash_old", "旧方案"),
        (evaluation.new_revision_id, "rules_hash_new", "新方案"),
    ):
        rev = db.get(ProtocolRevision, rev_id)
        if rev and diff.get(key) and rules_hash(rev.rules or {}) != diff[key]:
            reasons.append(f"{label}修订内容在评估后发生变化")
    return reasons


def create_dossier(
    db: Session, evaluation: RevisionEvaluation, actor: str, note: str | None = None
) -> MigrationDossier:
    """Freeze a migration dossier from a completed, still-fresh evaluation.

    The dossier records which analyses may keep referencing the old cohort and
    which need researcher re-confirmation. It never rewrites reports and never
    re-approves the protocol revision.
    """
    if evaluation.status == "stale":
        raise EvaluationStale([evaluation.stale_reason or "evaluation is stale"])
    if evaluation.status != "completed":
        raise EvaluationError(f"evaluation is {evaluation.status}; nothing to migrate from")
    new_rev = db.get(ProtocolRevision, evaluation.new_revision_id)
    if new_rev is None or new_rev.status != "approved":
        raise EvaluationError("migration dossiers require an approved new protocol revision")

    reasons = staleness_reasons(db, evaluation)
    if reasons:
        evaluation.status = "stale"
        evaluation.stale_reason = "; ".join(reasons)
        db.commit()
        raise EvaluationStale(reasons)

    dossier = MigrationDossier(
        evaluation_id=evaluation.id,
        protocol_id=evaluation.protocol_id,
        old_revision_id=evaluation.old_revision_id,
        new_revision_id=evaluation.new_revision_id,
        input_snapshot={
            "old_revision_id": evaluation.old_revision_id,
            "new_revision_id": evaluation.new_revision_id,
            "old_cutoff_id": evaluation.old_cutoff_id,
            "new_cutoff_id": evaluation.new_cutoff_id,
            "input_fingerprint": evaluation.input_fingerprint,
            "staleness_checked_at": datetime.now(timezone.utc).isoformat(),
            "result": "fresh",
        },
        analyses=evaluation.impact.get("analyses", []),
        case_changes={
            "membership": evaluation.summary.get("membership"),
            "categories": evaluation.summary.get("categories"),
            "endpoints": evaluation.impact.get("endpoints"),
            "index_date_shifted": evaluation.impact.get("index_date_shifted"),
            "follow_up_tasks_to_review": evaluation.impact.get("follow_up_tasks_to_review"),
            "msc_stats": evaluation.msc_stats,
        },
        note=note,
        created_by=actor,
    )
    db.add(dossier)
    db.commit()
    return dossier
