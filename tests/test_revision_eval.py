"""Revision evaluations: four-combination attribution, minimal sufficient
change sets, endpoint re-judgement, data revisions, staleness and dossiers.

The module-scoped ``eval_world`` fixture builds the whole fictional story once
(base cohort, wk11/wk15 uploads, v1.1 native-tree and v1.2 flat revisions,
two evaluations); tests then assert on the shared results.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import CutoffBatch, NormalizedRecord, SourceRecord, Subject
from app.services.conditions import (
    FALSE,
    TRUE,
    UNKNOWN,
    change_variables,
    compile_flat_rules,
    criteria_tree,
    diff_trees,
    eval_tree,
    mix_rules,
    semantic_key,
    skeleton_key,
    validate_tree,
    ConditionTreeError,
)
from app.services.screening import evaluate_subject, load_subject_data
from scripts import fixtures, scenario
from sqlalchemy import select


# ---------------------------------------------------------------------------
# shared world
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def eval_world():
    scenario.reset_schema()
    with TestClient(app) as client:
        scenario.seed_centers(client)
        proto = scenario.create_protocol(client)
        up1 = scenario.upload_batch1(client)
        cutoff1 = scenario.make_cutoff(client, "cutoff-2024-04-01", list(up1.values()))
        run1 = scenario.start_run(client, proto["revision_id"], cutoff1["id"], "full")
        assert run1.status_code in (200, 201), run1.text
        scenario.decide_all_identity(client)
        run1b = scenario.start_run(client, proto["revision_id"], cutoff1["id"], "full").json()
        snap1 = client.post(
            f"/screening-runs/{run1b['id']}/snapshots", headers=scenario.auth("analysis")
        ).json()
        client.post(f"/snapshots/{snap1['id']}/lock", headers=scenario.auth("analysis"))
        client.post(f"/snapshots/{snap1['id']}/follow-up-plans", headers=scenario.auth("analysis"))
        analysis1 = client.post(
            f"/snapshots/{snap1['id']}/analysis-runs",
            json={"kind": "efficacy"},
            headers=scenario.auth("analysis"),
        ).json()
        analysis1s = client.post(
            f"/snapshots/{snap1['id']}/analysis-runs",
            json={"kind": "safety"},
            headers=scenario.auth("analysis"),
        ).json()
        report1 = client.post(
            "/reports", json={"analysis_run_id": analysis1["id"]}, headers=scenario.auth("analysis")
        ).json()

        # wk11 subjects + the batch-2 amendments; mapping remediation and a new
        # identity adjudication happen after the old cutoff is frozen.
        up1b = scenario.upload_batch1b(client)
        up2 = scenario.upload_batch2(client)
        cutoff3 = scenario.make_cutoff(
            client,
            "cutoff-2024-04-08",
            sorted(list(up1.values()) + list(up1b.values()) + list(up2.values())),
        )
        mapping = client.post(
            "/mappings",
            json={
                "center_code": "SOUTH",
                "kind": "medication",
                "source_code": "M-99",
                "study_code": "METFORMIN",
            },
            headers=scenario.auth("reviewer"),
        ).json()
        client.post(
            f"/mappings/{mapping['id']}/reapply?dry_run=false", headers=scenario.auth("reviewer")
        )
        scenario.decide_all_identity(client)
        run3 = scenario.start_run(client, proto["revision_id"], cutoff3["id"], "full").json()
        snap3 = client.post(
            f"/screening-runs/{run3['id']}/snapshots", headers=scenario.auth("analysis")
        ).json()
        client.post(f"/snapshots/{snap3['id']}/lock", headers=scenario.auth("analysis"))
        analysis3 = client.post(
            f"/snapshots/{snap3['id']}/analysis-runs",
            json={"kind": "efficacy"},
            headers=scenario.auth("analysis"),
        ).json()

        # wk15 (N08 creatinine, HBA1C backfill, S01 correction, new E06) + cutoff4.
        up3 = scenario.upload_batch3(client)
        cutoff4 = scenario.make_cutoff(
            client,
            "cutoff-2024-04-22",
            sorted(
                list(up1.values()) + list(up1b.values()) + list(up2.values()) + list(up3.values())
            ),
        )
        rev11 = scenario.create_revision(client, proto["protocol_id"], "v1.1", fixtures.PROTOCOL_RULES_V1_1)
        rev12 = scenario.create_revision(client, proto["protocol_id"], "v1.2", fixtures.PROTOCOL_RULES_V1_2)

        ev1 = scenario.run_evaluation(
            client, proto["revision_id"], rev11["id"], cutoff3["id"], cutoff4["id"]
        )
        assert ev1.status_code == 201, ev1.text
        ev2 = scenario.run_evaluation(
            client, proto["revision_id"], rev12["id"], cutoff4["id"], cutoff4["id"]
        )
        assert ev2.status_code == 201, ev2.text

        world = {
            "client": client,
            "protocol": proto,
            "rev11": rev11,
            "rev12": rev12,
            "cutoffs": {"c1": cutoff1, "c3": cutoff3, "c4": cutoff4},
            "uploads": {"b1": up1, "b1b": up1b, "b2": up2, "b3": up3},
            "snap1": snap1,
            "snap3": snap3,
            "analysis1": analysis1,
            "analysis1s": analysis1s,
            "analysis3": analysis3,
            "report1": report1,
            "ev1": ev1.json(),
            "ev2": ev2.json(),
        }
        # patient_ref -> study_id lookup for readable assertions
        with SessionLocal() as db:
            rows = db.execute(
                select(SourceRecord.payload, NormalizedRecord.subject_id)
                .join(NormalizedRecord, NormalizedRecord.source_record_id == SourceRecord.id)
                .where(SourceRecord.record_type == "case")
            ).all()
            subjects = {s.id: s.study_id for s in db.scalars(select(Subject)).all()}
            ref_map = {}
            for payload, sid in rows:
                ref = (payload or {}).get("patient_ref")
                if ref and sid:
                    ref_map[ref] = subjects[sid]
            world["ref_map"] = ref_map
        world["cases1"] = scenario.evaluation_cases(client, world["ev1"]["id"])
        world["cases2"] = scenario.evaluation_cases(client, world["ev2"]["id"])
        yield world


def case_of(world, cases_key, ref):
    return world[cases_key][world["ref_map"][ref]]


# ---------------------------------------------------------------------------
# condition-tree unit tests (pure, no database)
# ---------------------------------------------------------------------------
def _stub_data(records):
    """Minimal SubjectData stand-in: list of (source, normalized) tuples."""

    class _Data:
        def __init__(self, recs):
            self.records = recs

        def by_kind(self, kind):
            return [(s, n) for s, n in self.records if n.kind == kind]

    import types

    out = []
    for i, (kind, normalized, converted, unit) in enumerate(records, start=1):
        src = types.SimpleNamespace(id=i)
        norm = types.SimpleNamespace(
            kind=kind, normalized=normalized, converted_value=converted, study_unit=unit
        )
        out.append((src, norm))
    return _Data(out)


def test_three_state_and_or_not():
    from datetime import date

    idx = date(2024, 1, 1)
    no_records = _stub_data([])
    # unknown AND false -> false dominates; unknown alone -> unknown
    tree = {
        "all_of": [
            {"id": "a", "leaf": "age_range", "min": 18},  # unknown: no birth date
            {"id": "d", "leaf": "diagnosis", "codes": ["T2DM"]},  # unknown: no dx rows
        ]
    }
    assert eval_tree(tree, no_records, idx)["verdict"] == UNKNOWN

    data = _stub_data(
        [
            ("case", {"birth_date": "1950-01-01"}, None, None),
            ("diagnosis", {"code": "T2DM", "diagnosis_date": "2023-01-01"}, None, None),
        ]
    )
    # age false (74 > 70) dominates the unknown-free tree -> false
    tree2 = {
        "all_of": [
            {"id": "a", "leaf": "age_range", "max": 70},
            {"id": "d", "leaf": "diagnosis", "codes": ["T2DM"]},
        ]
    }
    assert eval_tree(tree2, data, idx)["verdict"] == FALSE
    # OR: one true is enough
    tree3 = {
        "any_of": [
            {"id": "a", "leaf": "age_range", "max": 70},
            {"id": "d", "leaf": "diagnosis", "codes": ["T2DM"]},
        ]
    }
    assert eval_tree(tree3, data, idx)["verdict"] == TRUE
    # NOT flips only definite values; unknown stays unknown
    assert eval_tree({"not": {"leaf": "diagnosis_present", "codes": ["ADV"]}}, data, idx)[
        "verdict"
    ] == TRUE
    assert eval_tree({"not": {"leaf": "age_range", "min": 18}}, no_records, idx)[
        "verdict"
    ] == UNKNOWN


def test_semantic_key_ignores_display_names():
    a = {"id": "x", "leaf": "lab_range", "label": "肌酐", "code": "CREATININE", "max": 2.0}
    b = {"id": "x", "leaf": "lab_range", "label": "完全不同的显示名", "code": "CREATININE", "max": 2.0}
    assert semantic_key(a) == semantic_key(b)
    c = {"id": "x", "leaf": "lab_range", "label": "肌酐", "code": "CREATININE", "max": 2.2}
    assert semantic_key(a) != semantic_key(c)


def test_diff_label_only_change_is_not_a_member_change():
    old = fixtures.PROTOCOL_RULES
    renamed = {
        **old,
        "inclusion": {**old["inclusion"], "lab": {**old["inclusion"]["lab"], "max": 2.0}},
    }
    # compile both and rename one label in the new tree
    new_tree = compile_flat_rules(renamed)
    for child in new_tree["all_of"]:
        if child.get("id") == "inclusion.lab.CREATININE":
            child["label"] = "血清肌酐（改名）"
    diff = diff_trees(criteria_tree(old), new_tree)
    assert diff["changed_count"] == 0
    label_only = [c for c in diff["conditions"] if c.get("label_only")]
    assert label_only, "expected the renamed condition to be flagged label_only"
    assert all(c["change"] == "unchanged" for c in diff["conditions"])


def test_skeleton_change_forces_atomic_criteria_block():
    diff = diff_trees(
        criteria_tree(fixtures.PROTOCOL_RULES), criteria_tree(fixtures.PROTOCOL_RULES_V1_1)
    )
    assert diff["structure_changed"] is True
    assert diff["mixable"] is False
    variables = change_variables(fixtures.PROTOCOL_RULES, fixtures.PROTOCOL_RULES_V1_1, diff)
    kinds = {v["id"] for v in variables}
    # one atomic criteria block + the index-date revision; never per-leaf mixes
    assert "criteria_block" in kinds
    assert "index_date" in kinds
    assert not any(k.startswith("cond:") for k in kinds)
    # leaf-level alignment is still reported for readability
    by_id = {c["condition_id"]: c for c in diff["conditions"]}
    assert by_id["inclusion.lab.CREATININE"]["change"] == "changed"
    assert by_id["inclusion.lab.HBA1C"]["change"] == "added"
    assert by_id["inclusion.diagnosis"]["change"] == "unchanged"


def test_same_skeleton_allows_per_condition_mixing():
    diff = diff_trees(
        criteria_tree(fixtures.PROTOCOL_RULES), criteria_tree(fixtures.PROTOCOL_RULES_V1_2)
    )
    assert diff["mixable"] is True
    variables = change_variables(fixtures.PROTOCOL_RULES, fixtures.PROTOCOL_RULES_V1_2, diff)
    assert {v["id"] for v in variables} == {"cond:inclusion.age", "cond:inclusion.lab.CREATININE"}
    # mixing applies exactly one condition
    mixed = mix_rules(fixtures.PROTOCOL_RULES, fixtures.PROTOCOL_RULES_V1_2, ["cond:inclusion.age"])
    tree = criteria_tree(mixed)
    nodes = {c["condition_id"]: c for c in diff_trees(tree, tree)["conditions"]}
    assert nodes  # sanity
    age = [c for c in tree["all_of"] if c.get("id") == "inclusion.age"][0]
    lab = [c for c in tree["all_of"] if c.get("id") == "inclusion.lab.CREATININE"][0]
    assert age["max"] == 70  # from v1.2
    assert lab["max"] == 2.0  # still v1.0


def test_skeleton_key_ignores_parameters_and_labels():
    t1 = compile_flat_rules(fixtures.PROTOCOL_RULES)
    t2 = compile_flat_rules(fixtures.PROTOCOL_RULES_V1_2)
    assert skeleton_key(t1) == skeleton_key(t2)


def test_invalid_tree_rejected():
    with pytest.raises(ConditionTreeError):
        validate_tree({"all_of": [{"leaf": "lab_range", "code": "X"}]})  # no id
    with pytest.raises(ConditionTreeError):
        validate_tree({"all_of": [{"id": "a", "leaf": "nonsense"}]})


# ---------------------------------------------------------------------------
# four-combination evaluation
# ---------------------------------------------------------------------------
def test_four_combinations_actually_ran(eval_world):
    combos = eval_world["ev1"]["combos"]
    # old rules x old data: the published baseline cohort
    assert combos["old_old"]["included"] == 14
    # new rules x old data: HBA1C is required but absent everywhere in the old
    # batch, so nobody is included and most cases stay pending — the old batch
    # is never topped up with new-batch data to complete the grid.
    assert combos["new_old"]["included"] == 0
    assert combos["new_old"]["insufficient_data"] > 0
    # joint: the amended world
    assert combos["new_new"]["included"] == 16
    summary = eval_world["ev1"]["summary"]
    assert summary["membership"]["count_old"] == 14
    assert summary["membership"]["count_new"] == 16
    assert summary["categories"]["joint"] == 2
    assert summary["categories"]["data_only"] == 2
    assert summary["categories"]["protocol_only"] == 4
    assert summary["categories"]["masked"] > 0


def test_joint_threshold_and_late_upload_together(eval_world):
    """P-N08: creatinine 2.1 mg/dL arrives only in the new batch and needs the
    loosened v1.1 threshold — neither change alone admits him."""
    n08 = case_of(eval_world, "cases1", "P-N08")
    assert n08["outcomes"] == {
        "old_old": "insufficient_data",
        "new_old": "insufficient_data",
        "old_new": "excluded_ineligible",  # 2.1 > 2.0 under the old threshold
        "new_new": "included",
    }
    assert n08["category"] == "joint"
    msc_sets = {frozenset(s) for s in n08["msc"]["sets"]}
    # the two changes must hold together; they are not two independent reasons
    assert msc_sets == {frozenset({"criteria_block", "data"})}
    assert n08["msc"]["status"] == "complete"


def test_loosen_and_tighten_cancel_out(eval_world):
    """P-N10: the loosened creatinine threshold would admit him (false→true),
    the tightened age ceiling keeps him out (true→false). Outcome unchanged,
    interaction recorded, and no single reason is claimed to be the only one."""
    n10 = case_of(eval_world, "cases1", "P-N10")
    assert n10["outcomes"]["old_old"] == "excluded_ineligible"
    assert n10["outcomes"]["new_new"] == "excluded_ineligible"
    assert n10["reason_changed"] is True
    assert n10["condition_interaction"] is True
    # several sufficient explanations coexist; the engine lists them all
    sets = {frozenset(s) for s in n10["msc"]["sets"]}
    assert frozenset({"criteria_block"}) in sets
    assert frozenset({"index_date"}) in sets
    assert frozenset({"data"}) in sets
    # verdicts show the two conditions moving in opposite directions
    client = eval_world["client"]
    det = client.get(
        f"/revision-evaluations/{eval_world['ev1']['id']}/cases/{n10['study_id']}",
        headers=scenario.auth("analysis"),
    ).json()
    age = det["condition_verdicts"]["inclusion.age"]["verdicts"]
    creat = det["condition_verdicts"]["inclusion.lab.CREATININE"]["verdicts"]
    assert age["old_old"] == "true" and age["new_new"] == "false"
    assert creat["old_old"] == "false" and creat["new_new"] == "true"


def test_count_preserving_member_replacement(eval_world):
    """v1.0 -> v1.2 on one fixed cutoff: the total stays 16, but P-N09 leaves
    (age 74 > 70) and P-S06 enters (creatinine ≈2.035 ≤ 2.05)."""
    ev2 = eval_world["ev2"]
    mem = ev2["summary"]["membership"]
    assert mem["count_old"] == mem["count_new"] == 16
    assert mem["net_change"] == 0
    assert mem["entered"] == [eval_world["ref_map"]["P-S06"]]
    assert mem["left"] == [eval_world["ref_map"]["P-N09"]]
    # per-condition minimal sufficient sets (same skeleton: mixable)
    n09 = case_of(eval_world, "cases2", "P-N09")
    s06 = case_of(eval_world, "cases2", "P-S06")
    assert n09["msc"]["sets"] == [["cond:inclusion.age"]]
    assert s06["msc"]["sets"] == [["cond:inclusion.lab.CREATININE"]]
    # N08's 2.1 mg/dL stays out at the 2.05 ceiling — no third member moves
    n08 = case_of(eval_world, "cases2", "P-N08")
    assert n08["outcomes"]["new_new"] == "excluded_ineligible"


def test_index_date_revision_rejudges_endpoint(eval_world):
    """P-E05: moving the index date from the first visit (2024-03-15) to the
    first diagnosis (2024-02-20) shifts age (64→63), the lab window and the
    follow-up anchor; the 2024-04-24 follow-up moves from +40d (outside the
    60–200d endpoint window) to +64d (inside)."""
    e05 = case_of(eval_world, "cases1", "P-E05")
    assert e05["index_dates"]["old_old"] == "2024-03-15"
    assert e05["index_dates"]["new_new"] == "2024-02-20"
    client = eval_world["client"]
    det = client.get(
        f"/revision-evaluations/{eval_world['ev1']['id']}/cases/{e05['study_id']}",
        headers=scenario.auth("analysis"),
    ).json()
    ep = det["endpoint_status"]["primary_efficacy"]["status"]
    assert ep["old_old"] == "not_eligible"
    assert ep["new_new"] == "eligible"
    attr = det["endpoint_attribution"]["primary_efficacy"]
    assert attr["rule_side_effect"] is True
    assert attr["index_date_shift"] == {"old": "2024-03-15", "new": "2024-02-20"}
    # the endpoint verdict is traceable to the follow-up source row
    ev = det["endpoint_status"]["primary_efficacy"]["evidence"]["new_new"]
    assert ev and ev[0]["record_type"] == "followup" and ev[0]["source_record_id"]
    # the evaluation reports shifted follow-up anchors without touching tasks
    impact = client.get(
        f"/revision-evaluations/{eval_world['ev1']['id']}/impact",
        headers=scenario.auth("analysis"),
    ).json()["impact"]
    assert eval_world["ref_map"]["P-E05"] in impact["index_date_shifted"]
    assert impact["follow_up_tasks_to_review"]["count"] > 0


def test_missing_condition_stays_pending_without_borrowing(eval_world):
    """P-S07: v1.1's new HBA1C condition has no data anywhere for him. Under
    the new protocol he stays pending (insufficient), never auto-excluded —
    and the old-data combination does not borrow the new batch's HBA1C rows."""
    s07 = case_of(eval_world, "cases1", "P-S07")
    assert s07["outcomes"]["new_old"] == "insufficient_data"
    assert s07["outcomes"]["new_new"] == "insufficient_data"
    assert s07["outcomes"]["old_old"] == "included"
    assert s07["category"] == "protocol_only"
    # everyone else's old-data verdict for HBA1C is unknown even though the
    # new batch has values: combinations never share data
    n01 = case_of(eval_world, "cases1", "P-N01")
    client = eval_world["client"]
    det = client.get(
        f"/revision-evaluations/{eval_world['ev1']['id']}/cases/{n01['study_id']}",
        headers=scenario.auth("analysis"),
    ).json()
    hba1c = det["condition_verdicts"]["inclusion.lab.HBA1C"]["verdicts"]
    assert hba1c["new_old"] == "unknown"  # old batch: no HBA1C rows
    assert hba1c["new_new"] == "true"  # new batch: backfilled
    assert hba1c["old_old"] is None  # condition did not exist in v1.0


def test_data_only_changes_are_not_protocol_effects(eval_world):
    """P-S01's creatinine correction (200→170 µmol/L) and the brand-new P-E06
    are pure data effects; the mapping remediation and identity adjudication
    are listed as their own revisions."""
    s01 = case_of(eval_world, "cases1", "P-S01")
    assert s01["category"] == "data_only"
    assert s01["outcomes"]["old_new"] == "included"
    assert "correction" in s01["data_tags"]
    e06 = case_of(eval_world, "cases1", "P-E06")
    assert e06["category"] == "data_only"
    assert e06["outcomes"]["old_old"] is None

    client = eval_world["client"]
    dr = client.get(
        f"/revision-evaluations/{eval_world['ev1']['id']}", headers=scenario.auth("analysis")
    ).json()["data_revisions"]
    assert any(m["source_code"] == "M-99" for m in dr["mappings"])
    assert dr["identity"], "the S08/S08B adjudication must be listed"
    assert any(u["batch_code"] == "south-2024-wk15" for u in dr["uploads"])
    # S02's inclusion is a mapping-revision effect, tagged as such
    s02 = case_of(eval_world, "cases1", "P-S02")
    assert "remapped" in s02["data_tags"]
    assert s02["category"] == "masked"  # not a protocol-only/protocol effect


def test_traceability_to_source_rows(eval_world):
    client = eval_world["client"]
    n08 = case_of(eval_world, "cases1", "P-N08")
    det = client.get(
        f"/revision-evaluations/{eval_world['ev1']['id']}/cases/{n08['study_id']}",
        headers=scenario.auth("analysis"),
    ).json()
    # the joint outcome is traceable to the late creatinine source row
    evidence = det["evidence"]["new_new"]
    lab_evidence = [e for e in evidence if e["record_type"] == "lab"]
    assert lab_evidence and all(e["source_record_id"] for e in lab_evidence)
    # per-condition verdicts exist for every aligned condition
    assert "inclusion.lab.CREATININE" in det["condition_verdicts"]
    assert det["condition_verdicts"]["inclusion.lab.CREATININE"]["verdicts"]["new_new"] == "true"


def test_impact_catalog_and_unaffected_analyses(eval_world):
    client = eval_world["client"]
    impact = client.get(
        f"/revision-evaluations/{eval_world['ev1']['id']}/impact",
        headers=scenario.auth("analysis"),
    ).json()["impact"]
    by_id = {a["analysis_run_id"]: a for a in impact["analyses"]}
    # the snapshot-1 analyses (efficacy + safety) are untouched: not batch-marked
    assert by_id[eval_world["analysis1"]["id"]]["disposition"] == "reusable"
    assert by_id[eval_world["analysis1s"]["id"]]["disposition"] == "reusable"
    # the snapshot-3 efficacy analysis loses members / has an endpoint change
    a3 = by_id[eval_world["analysis3"]["id"]]
    assert a3["disposition"] == "needs_reconfirmation"
    changed_refs = {m["study_id"] for m in a3["changed_members"]}
    assert eval_world["ref_map"]["P-N09"] in changed_refs
    assert eval_world["ref_map"]["P-E05"] in changed_refs  # endpoint re-judged
    # reports are referenced, never rewritten
    assert by_id[eval_world["analysis1"]["id"]]["reports"][0]["content_hash"]


def test_evaluation_does_not_rewrite_reports(eval_world):
    client = eval_world["client"]
    report = client.get(
        f"/reports/{eval_world['report1']['id']}", headers=scenario.auth("analysis")
    ).json()
    assert report["content_hash"] == eval_world["report1"]["content_hash"]
    assert report["frozen"] is True


def test_roles(eval_world):
    client = eval_world["client"]
    body = {
        "old_revision_id": eval_world["protocol"]["revision_id"],
        "new_revision_id": eval_world["rev11"]["id"],
        "old_cutoff_id": eval_world["cutoffs"]["c3"]["id"],
        "new_cutoff_id": eval_world["cutoffs"]["c4"]["id"],
    }
    # centre uploaders cannot launch evaluations
    r = client.post("/revision-evaluations", json=body, headers=scenario.auth("north"))
    assert r.status_code == 403
    # unauthenticated calls are refused
    assert client.post("/revision-evaluations", json=body).status_code == 401
    # rule reviewer may evaluate, but only the analysis lead files a dossier
    r = client.post("/revision-evaluations", json=body, headers=scenario.auth("reviewer"))
    assert r.status_code == 201
    ev_id = r.json()["id"]
    r = client.post(
        f"/revision-evaluations/{ev_id}/migration-dossiers",
        json={},
        headers=scenario.auth("reviewer"),
    )
    assert r.status_code == 403


def test_stale_evaluation_must_be_reevaluated(eval_world):
    """A centre upload landing after the evaluation voids its diff list: the
    dossier is refused, a fresh evaluation on a new cutoff catches the late
    HBA1C (P-E04 flips), and only then is the dossier written."""
    client = eval_world["client"]
    up4 = scenario.upload_batch4(client)
    r = client.post(
        f"/revision-evaluations/{eval_world['ev1']['id']}/migration-dossiers",
        json={"note": "premature"},
        headers=scenario.auth("analysis"),
    )
    assert r.status_code == 409
    assert "重新评估" in r.json()["detail"]["message"]
    assert any("east-2024-wk16" in reason for reason in r.json()["detail"]["reasons"])
    # the evaluation is now marked stale and stays readable
    ev = client.get(
        f"/revision-evaluations/{eval_world['ev1']['id']}", headers=scenario.auth("analysis")
    ).json()
    assert ev["status"] == "stale"
    # a dossier on a stale evaluation keeps being refused
    r = client.post(
        f"/revision-evaluations/{eval_world['ev1']['id']}/migration-dossiers",
        json={},
        headers=scenario.auth("analysis"),
    )
    assert r.status_code == 409

    c = eval_world["cutoffs"]
    up = eval_world["uploads"]
    cutoff5 = scenario.make_cutoff(
        client,
        "cutoff-2024-04-29",
        sorted(
            list(up["b1"].values())
            + list(up["b1b"].values())
            + list(up["b2"].values())
            + list(up["b3"].values())
            + list(up4.values())
        ),
    )
    ev3 = scenario.run_evaluation(
        client,
        eval_world["protocol"]["revision_id"],
        eval_world["rev11"]["id"],
        c["c3"]["id"],
        cutoff5["id"],
    )
    assert ev3.status_code == 201, ev3.text
    ev3 = ev3.json()
    cases3 = scenario.evaluation_cases(client, ev3["id"])
    e04 = cases3[eval_world["ref_map"]["P-E04"]]
    assert e04["outcomes"]["new_new"] == "included"
    assert e04["category"] == "joint"
    assert {frozenset(s) for s in e04["msc"]["sets"]} == {frozenset({"criteria_block", "data"})}

    r = client.post(
        f"/revision-evaluations/{ev3['id']}/migration-dossiers",
        json={"note": "v1.1 迁移"},
        headers=scenario.auth("analysis"),
    )
    assert r.status_code == 201, r.text
    dossier = r.json()
    assert dossier["input_snapshot"]["result"] == "fresh"
    dispositions = {a["analysis_run_id"]: a["disposition"] for a in dossier["analyses"]}
    assert dispositions[eval_world["analysis1"]["id"]] == "reusable"
    assert dispositions[eval_world["analysis3"]["id"]] == "needs_reconfirmation"
    # acknowledging records review; it neither approves nor rewrites anything
    ack = client.post(
        f"/migration-dossiers/{dossier['id']}/acknowledge", headers=scenario.auth("analysis")
    )
    assert ack.status_code == 200
    assert ack.json()["status"] == "acknowledged"
    report = client.get(
        f"/reports/{eval_world['report1']['id']}", headers=scenario.auth("analysis")
    ).json()
    assert report["content_hash"] == eval_world["report1"]["content_hash"]


def test_budget_exhaustion_keeps_partial_explanation(eval_world):
    """With a tiny budget the joint case cannot be fully enumerated: the
    proven sets are kept, the remainder is explicitly left unexplained, and
    nothing is advertised as the only cause."""
    client = eval_world["client"]
    ev = scenario.run_evaluation(
        client,
        eval_world["protocol"]["revision_id"],
        eval_world["rev11"]["id"],
        eval_world["cutoffs"]["c3"]["id"],
        eval_world["cutoffs"]["c4"]["id"],
        budget_per_case=1,
    )
    assert ev.status_code == 201, ev.text
    cases = scenario.evaluation_cases(client, ev.json()["id"])
    n08 = cases[eval_world["ref_map"]["P-N08"]]
    assert n08["msc"]["status"] == "partial"
    assert n08["msc"]["evaluations_used"] <= 1
    assert "预算耗尽" in n08["msc"]["note"]
    assert ev.json()["msc_stats"]["partial"] >= 1


def test_compiled_tree_matches_flat_evaluator(eval_world):
    """For flat rules the compiled-tree path must agree with the official
    evaluator subject-by-subject (same outcomes, same reason codes)."""
    with SessionLocal() as db:
        batch = db.get(CutoffBatch, eval_world["cutoffs"]["c4"]["id"])
        upload_ids = sorted(int(u) for u in batch.upload_ids)
        subjects = db.scalars(select(Subject).where(Subject.merged_into_subject_id.is_(None))).all()
        mixed = mix_rules(fixtures.PROTOCOL_RULES, fixtures.PROTOCOL_RULES, [])
        assert "criteria" in mixed and "inclusion" not in mixed
        checked = 0
        for subject in subjects:
            data = load_subject_data(db, subject.id, upload_ids)
            if not data.records:
                continue
            official = evaluate_subject(db, fixtures.PROTOCOL_RULES, data, False, 0)
            via_tree = evaluate_subject(db, mixed, data, False, 0)
            assert via_tree["outcome"] == official["outcome"], subject.study_id
            assert via_tree["reason_codes"] == official["reason_codes"], subject.study_id
            checked += 1
        assert checked >= 20


def test_revision_compare_endpoint_and_validation(eval_world):
    client = eval_world["client"]
    r = client.post(
        "/protocols/revisions/compare",
        json={
            "old_revision_id": eval_world["protocol"]["revision_id"],
            "new_revision_id": eval_world["rev11"]["id"],
        },
        headers=scenario.auth("reviewer"),
    )
    assert r.status_code == 200
    diff = r.json()["condition_diff"]
    assert diff["structure_changed"] is True
    by_id = {c["condition_id"]: c for c in diff["conditions"]}
    assert by_id["inclusion.lab.HBA1C"]["change"] == "added"
    assert by_id["inclusion.diagnosis"]["label_only"] is True
    # an invalid native tree cannot be stored
    bad = client.post(
        f"/protocols/{eval_world['protocol']['protocol_id']}/revisions",
        json={"version": "v9.9", "rules": {"criteria": {"all_of": [{"leaf": "age_range"}]}}},
        headers=scenario.auth("reviewer"),
    )
    assert bad.status_code == 422
