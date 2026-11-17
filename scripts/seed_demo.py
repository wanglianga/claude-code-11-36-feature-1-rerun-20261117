"""End-to-end demo against a running API or in-process.

    python -m scripts.seed_demo            # in-process, resets the demo DB
    python -m scripts.seed_demo --base-url http://127.0.0.1:8000   # live server

The script tells the whole story required by the business case:

  1. freezes a first cutoff and runs a full screening;
  2. prints the per-centre reconciliation the analysis lead must sign off;
  3. locks a snapshot and publishes report v1;
  4. a late centre upload arrives, creating a second cutoff;
  5. an incremental recomputation and an independent full recomputation agree
     subject-by-subject, and report v1 is untouched;
  6. report v2 points at the new snapshot.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

TOKENS = {
    "server": "analysis-token",
    "reviewer": "reviewer-token",
    "privacy": "privacy-token",
    "north": "center-north-token",
    "south": "center-south-token",
    "east": "center-east-token",
}


class Api:
    def __init__(self, base_url: str | None):
        if base_url:
            import httpx

            self.client = httpx.Client(base_url=base_url, timeout=60)
            self.close = self.client.close
        else:
            from fastapi.testclient import TestClient

            from app.main import app

            self.client = TestClient(app)
            self.client.__enter__()
            self.close = self.client.__exit__

    def as_(self, who: str) -> dict:
        return {"Authorization": f"Bearer {TOKENS[who]}"}

    def get(self, path: str, who: str = "server", **kw):
        r = self.client.get(path, headers=self.as_(who), **kw)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, who: str = "server", **kw):
        r = self.client.post(path, headers=self.as_(who), **kw)
        r.raise_for_status()
        return r.json()


SAFE_RESET_DATABASES = {"cohorthub", "cohorthub_test", "cohorthub_demo"}


def reset_local_database() -> None:
    """Rebuild the demo schema.

    Guarded so a mis-pointed COHORT_DATABASE_URL cannot wipe an unrelated
    database: the target must be one of the known demo databases, or the caller
    must explicitly opt in with COHORT_ALLOW_RESET=1.
    """
    from sqlalchemy import text

    from app.db import Base, SessionLocal, engine
    from app.security import seed_users

    name = engine.url.database
    if name not in SAFE_RESET_DATABASES and os.environ.get("COHORT_ALLOW_RESET") != "1":
        raise SystemExit(
            f"refusing to drop schema of database {name!r}. "
            "Use one of %s, or set COHORT_ALLOW_RESET=1."
            % sorted(SAFE_RESET_DATABASES)
        )

    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_users(db)


def seed(api: Api) -> dict:
    from scripts import fixtures

    existing = api.get("/centers")
    if existing:
        raise SystemExit(
            "the target database is not empty (%d centres). Reset it first, or point "
            "COHORT_DATABASE_URL at a fresh demo database." % len(existing)
        )
    centers = {}
    for code, spec in fixtures.CENTER_SPECS.items():
        centers[code] = api.post(
            "/centers",
            "reviewer",
            json={"code": code, "name": spec["name"], "config": spec["config"]},
        )["id"]
    # Relink centre uploaders to their centre.
    from app.db import SessionLocal
    from app.security import seed_users

    with SessionLocal() as db:
        seed_users(db)

    for code in fixtures.CENTER_SPECS:
        for mapping in fixtures.mappings_for(code):
            api.post("/mappings", "reviewer", json=mapping)

    proto = api.post(
        "/protocols",
        "reviewer",
        json={"name": "DKD-MET cohort (fictional)", "description": "Demo protocol"},
    )
    rev = api.post(
        f"/protocols/{proto['id']}/revisions",
        "reviewer",
        json={"version": "v1.0", "rules": fixtures.PROTOCOL_RULES},
    )
    api.post(f"/protocols/revisions/{rev['id']}/approve", "reviewer")

    uploads = {}
    for upload in fixtures.BATCH1_UPLOADS:
        who = {"NORTH": "north", "SOUTH": "south", "EAST": "east"}[upload["center_code"]]
        uploads[upload["batch_code"]] = api.post("/uploads", who, json=upload)["id"]
    return {
        "centers": centers,
        "protocol_id": proto["id"],
        "revision_id": rev["id"],
        "uploads": uploads,
    }


def _print_evaluation(api: "Api", ev: dict) -> None:
    """Print the four-combination table and the key attributed cases."""
    combos = ev["combos"]
    labels = ev["combo_labels"]
    print("\n四种组合的病例结局分布（实际运行，不借用其他组合的资料）")
    print(f"{'组合':28}{'纳入':>5}{'缺资料':>7}{'待判':>6}{'不符合':>7}{'撤回':>6}{'不在范围':>8}")
    for key in ("old_old", "new_old", "old_new", "new_new"):
        c = combos[key]
        print(
            f"{labels[key]:28}{c['included']:>5}{c['insufficient_data']:>7}"
            f"{c['pending_adjudication']:>6}{c['excluded_ineligible']:>7}"
            f"{c['consent_withdrawn']:>6}{c['not_in_scope']:>8}"
        )
    s = ev["summary"]
    mem = s["membership"]
    print(
        f"\n纳入人数 {mem['count_old']} → {mem['count_new']}（净 {mem['net_change']:+d}）；"
        f"进入 {len(mem['entered'])}，离开 {len(mem['left'])}"
    )
    print("归因分类：" + "，".join(f"{k}={v}" for k, v in sorted(s["categories"].items())))
    print(f"最小充分组合搜索：{ev['msc_stats']} ")

    cases = api.get(f"/revision-evaluations/{ev['id']}/cases")
    interesting = [c for c in cases if c["category"] != "unchanged" or c["reason_changed"]]
    print(f"\n状态变化或理由变化的病例（{len(interesting)} 例，按中心）：")
    for c in interesting:
        o = c["outcomes"]
        print(
            f"  {c['study_id']}（{c['center_code']:5}）{c['category_label']}\n"
            f"      旧旧={o['old_old']} 新旧={o['new_old']} 旧新={o['old_new']} 新新={o['new_new']}"
            + (f"\n      最小充分组合={c['msc']['sets']}（{c['msc']['status']}）" if c["msc"].get("sets") else "")
            + ("\n      条件交互：多条条件同向/反向翻转" if c["condition_interaction"] else "")
        )
    detail = api.get(f"/revision-evaluations/{ev['id']}")
    dr = detail["data_revisions"]
    print(
        f"\n数据修订单列（不计入方案影响）：补传 {len(dr['uploads'])} 批、"
        f"映射 {len(dr['mappings'])} 条、身份裁决 {len(dr['identity'])} 条"
    )
    ep = detail.get("impact") or api.get(f"/revision-evaluations/{ev['id']}/impact")["impact"]
    if ep["endpoints"]["changed"]:
        for ch in ep["endpoints"]["changed"]:
            print(f"  终点变化：{ch['study_id']} {ch['endpoint']}：{ch['old']} → {ch['new']}")


def _print_reconciliation(run_detail: dict) -> None:
    print("\n每中心对账（分析负责人签核前必须全部 balanced=true）")
    header = (
        f"{'中心':6}{'原始行':>7}{'标准化':>7}{'待判行':>7}{'失败行':>7}"
        f"{'病例':>6}{'纳入':>6}{'缺资料':>7}{'待判':>6}{'不符合':>7}{'撤回':>6}{'守恒':>6}"
    )
    print(header)
    for c in run_detail["reconciliation"]:
        print(
            f"{c['center_code']:6}{c['raw_rows']:>8}{c['normalized_ok']:>9}{c['pending']:>9}"
            f"{c['failed']:>8}{c['subjects_total']:>7}{c['included']:>7}"
            f"{c['missing_data']:>8}{c['pending_subjects']:>7}"
            f"{c['excluded_ineligible']:>9}{c['consent_withdrawn']:>7}"
            f"{str(c['balanced']):>7}"
        )
    t = run_detail["counts"]
    print(
        f"{'合计':6}{t['raw_rows']:>8}{t['normalized_ok']:>9}{t['pending']:>9}"
        f"{t['failed']:>8}{t['subjects_total']:>7}{t['included']:>7}"
        f"{t['missing_data']:>8}{t['pending_subjects']:>7}"
        f"{t['excluded_ineligible']:>9}{t['consent_withdrawn']:>7}"
        f"{str(t['balanced']):>7}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=None, help="use a running server instead of in-process")
    args = parser.parse_args(argv)

    api = Api(args.base_url)
    try:
        if not args.base_url:
            reset_local_database()
        parts = seed(api)
        rev = parts["revision_id"]
        uploads = parts["uploads"]

        print("== 1. 冻结截止批次并执行全量筛选 ==")
        cutoff1 = api.post(
            "/cutoffs",
            "server",
            json={"label": "cutoff-2024-04-01", "upload_ids": sorted(uploads.values())},
        )
        run1 = api.post(
            "/screening-runs",
            "server",
            json={
                "protocol_revision_id": rev,
                "cutoff_batch_id": cutoff1["id"],
                "strategy": "full",
            },
        )
        detail1 = api.get(f"/screening-runs/{run1['id']}")
        _print_reconciliation(detail1)

        print("\n== 2. 隐私管理员裁决身份候选（不自动合并） ==")
        cands = api.get("/identity-candidates", "privacy")
        for cand in cands:
            api.post(
                f"/identity-candidates/{cand['id']}/decide",
                "privacy",
                json={"status": "rejected", "note": "demo: distinct per source records"},
            )
        print(f"已裁决 {len(cands)} 条候选，全部由隐私管理员人工确认")

        run1b = api.post(
            "/screening-runs",
            "server",
            json={
                "protocol_revision_id": rev,
                "cutoff_batch_id": cutoff1["id"],
                "strategy": "full",
            },
        )
        print("\n== 3. 锁定快照并发布报告 v1 ==")
        snap1 = api.post(f"/screening-runs/{run1b['id']}/snapshots", "server")
        api.post(f"/snapshots/{snap1['id']}/lock", "server")
        plan1 = api.post(f"/snapshots/{snap1['id']}/follow-up-plans", "server")
        analysis1 = api.post(
            f"/snapshots/{snap1['id']}/analysis-runs", "server", json={"kind": "efficacy"}
        )
        # A safety analysis on the same snapshot: its retained-withdrawal
        # population is untouched by the later amendment, which is what keeps
        # it "reusable" in the migration dossier.
        analysis1s = api.post(
            f"/snapshots/{snap1['id']}/analysis-runs", "server", json={"kind": "safety"}
        )
        report1 = api.post("/reports", "server", json={"analysis_run_id": analysis1["id"]})
        print(
            f"快照 #{snap1['id']} 病例 {snap1['case_count']}，安全分析 {snap1['safety_count']}；"
            f"随访计划 #{plan1['id']}；疗效分析 #{analysis1['id']}、安全分析 #{analysis1s['id']}；"
            f"报告 v{report1['report_version']}，校验和 {report1['content_hash'][:12]}…"
        )

        print("\n== 4. 迟到补录（中心补传 + 结果更正） ==")
        from scripts import fixtures

        late = {}
        for upload in fixtures.BATCH2_UPLOADS:
            who = {"NORTH": "north", "SOUTH": "south", "EAST": "east"}[upload["center_code"]]
            late[upload["batch_code"]] = api.post("/uploads", who, json=upload)["id"]
        cutoff2 = api.post(
            "/cutoffs",
            "server",
            json={
                "label": "cutoff-2024-04-15",
                "upload_ids": sorted(list(uploads.values()) + list(late.values())),
            },
        )

        print("\n== 5. 增量重算 vs 全量重算，同一方案修订与截止点 ==")
        inc = api.post(
            "/screening-runs",
            "server",
            json={
                "protocol_revision_id": rev,
                "cutoff_batch_id": cutoff2["id"],
                "strategy": "incremental",
                "base_run_id": run1b["id"],
            },
        )
        verify = api.post(f"/screening-runs/{inc['id']}/verify-equivalence", "reviewer")
        print(
            f"增量：复用 {inc['reused_count']} 例，重算 {inc['recomputed_count']} 例，"
            f"受影响 {inc['affected_subject_count']} 例"
        )
        print(
            f"与全量重算比较 {verify['subjects_compared']} 例："
            f"{'完全一致' if verify['equal'] else '存在差异'}，"
            f"Mismatch {verify['mismatch_count']}"
        )

        print("\n== 6. 新快照取代旧快照，旧报告不变 ==")
        snap2 = api.post(f"/screening-runs/{inc['id']}/snapshots", "server")
        api.post(f"/snapshots/{snap2['id']}/lock", "server")
        analysis2 = api.post(
            f"/snapshots/{snap2['id']}/analysis-runs", "server", json={"kind": "efficacy"}
        )
        report2 = api.post("/reports", "server", json={"analysis_run_id": analysis2["id"]})
        old = api.get(f"/reports/{report1['id']}")
        print(
            f"新快照 #{snap2['id']}（取代 #{snap2['supersedes_snapshot_id']}）病例 {snap2['case_count']}；"
            f"报告 v{report2['report_version']} 引用方案修订 {report2['content']['protocol']['version']}"
        )
        print(
            f"报告 v1 仍指向快照 #{old['content']['snapshot']['id']}，"
            f"内容校验和未变：{old['content_hash'] == report1['content_hash']}"
        )

        print("\n== 7. 方案修订的差异归因与分析迁移评估 ==")
        from scripts import fixtures as fx

        print("\n-- 7.1 中心补传（wk11 新病例）→ 新截止批次；映射补建与身份裁决单列 --")
        up1b = {}
        for upload in fx.BATCH1B_UPLOADS:
            who = {"NORTH": "north", "SOUTH": "south", "EAST": "east"}[upload["center_code"]]
            up1b[upload["batch_code"]] = api.post("/uploads", who, json=upload)["id"]
        cutoff3 = api.post(
            "/cutoffs",
            "server",
            json={
                "label": "cutoff-2024-04-08",
                "upload_ids": sorted(
                    list(uploads.values()) + list(late.values()) + list(up1b.values())
                ),
            },
        )
        # 映射补建（SOUTH M-99）与新身份候选裁决发生在旧截止冻结之后，评估会把
        # 它们列为独立的数据修订，而不是方案影响。
        m = api.post(
            "/mappings",
            "reviewer",
            json={
                "center_code": "SOUTH",
                "kind": "medication",
                "source_code": "M-99",
                "study_code": "METFORMIN",
            },
        )
        reapply = api.post(f"/mappings/{m['id']}/reapply?dry_run=false", "reviewer")
        cands = api.get("/identity-candidates", "privacy")
        for cand in cands:
            api.post(
                f"/identity-candidates/{cand['id']}/decide",
                "privacy",
                json={"status": "rejected", "note": "demo: distinct patients"},
            )
        print(
            f"映射补建 M-99→METFORMIN：修复 {reapply['fixed_rows']} 行；"
            f"新身份候选 {len(cands)} 条已由隐私管理员裁决"
        )
        run3 = api.post(
            "/screening-runs",
            "server",
            json={
                "protocol_revision_id": rev,
                "cutoff_batch_id": cutoff3["id"],
                "strategy": "full",
            },
        )
        snap3 = api.post(f"/screening-runs/{run3['id']}/snapshots", "server")
        api.post(f"/snapshots/{snap3['id']}/lock", "server")
        analysis3 = api.post(
            f"/snapshots/{snap3['id']}/analysis-runs", "server", json={"kind": "efficacy"}
        )
        print(f"快照 #{snap3['id']}（cutoff-2024-04-08）病例 {snap3['case_count']}，已锁定并做疗效分析 #{analysis3['id']}")

        print("\n-- 7.2 wk15 补传（N08 肌酐 + HBA1C 回补 + S01 更正 + 新病例 E06）→ 新截止批次 --")
        up3 = {}
        for upload in fx.BATCH3_UPLOADS:
            who = {"NORTH": "north", "SOUTH": "south", "EAST": "east"}[upload["center_code"]]
            up3[upload["batch_code"]] = api.post("/uploads", who, json=upload)["id"]
        cutoff4 = api.post(
            "/cutoffs",
            "server",
            json={
                "label": "cutoff-2024-04-22",
                "upload_ids": sorted(
                    list(uploads.values())
                    + list(late.values())
                    + list(up1b.values())
                    + list(up3.values())
                ),
            },
        )
        rev11 = api.post(
            f"/protocols/{parts['protocol_id']}/revisions",
            "reviewer",
            json={"version": "v1.1", "rules": fx.PROTOCOL_RULES_V1_1},
        )
        api.post(f"/protocols/revisions/{rev11['id']}/approve", "reviewer")
        rev12 = api.post(
            f"/protocols/{parts['protocol_id']}/revisions",
            "reviewer",
            json={"version": "v1.2", "rules": fx.PROTOCOL_RULES_V1_2},
        )
        api.post(f"/protocols/revisions/{rev12['id']}/approve", "reviewer")
        print(
            "v1.1（原生条件树：肌酐放宽 2.0→2.2、年龄收紧 75→70、索引日期改为首次诊断、"
            "新增 HBA1C 必需条件、部分条件仅改名）与 v1.2（扁平参数微调）已批准"
        )

        print("\n-- 7.3 评估一：v1.0→v1.1 × cutoff-04-08→cutoff-04-22，四种组合实际运行 --")
        ev1 = api.post(
            "/revision-evaluations",
            "server",
            json={
                "old_revision_id": rev,
                "new_revision_id": rev11["id"],
                "old_cutoff_id": cutoff3["id"],
                "new_cutoff_id": cutoff4["id"],
            },
        )
        _print_evaluation(api, ev1)

        print("\n-- 7.4 评估二：v1.0→v1.2 × 同一截止批次（人数不变的成员替换） --")
        ev2 = api.post(
            "/revision-evaluations",
            "server",
            json={
                "old_revision_id": rev,
                "new_revision_id": rev12["id"],
                "old_cutoff_id": cutoff4["id"],
                "new_cutoff_id": cutoff4["id"],
            },
        )
        mem2 = ev2["summary"]["membership"]
        print(
            f"纳入人数 {mem2['count_old']} → {mem2['count_new']}（净变化 {mem2['net_change']}），"
            f"但成员被替换：进入 {mem2['entered']}，离开 {mem2['left']}"
        )
        ev2_cases = api.get(f"/revision-evaluations/{ev2['id']}/cases")
        for c in ev2_cases:
            if c["category"] != "unchanged" or c["reason_changed"]:
                print(
                    f"  {c['study_id']}（{c['center_code']}）{c['category_label']}："
                    f"{c['outcomes']['old_old']} → {c['outcomes']['new_new']}，"
                    f"最小充分组合 {c['msc'].get('sets')}"
                )

        print("\n-- 7.5 评估后中心再次补传 → 迁移案卷拒绝沿用旧差异清单，必须重评 --")
        up4 = {}
        for upload in fx.BATCH4_UPLOADS:
            who = {"NORTH": "north", "SOUTH": "south", "EAST": "east"}[upload["center_code"]]
            up4[upload["batch_code"]] = api.post("/uploads", who, json=upload)["id"]
        stale = api.client.post(
            f"/revision-evaluations/{ev1['id']}/migration-dossiers",
            headers=api.as_("server"),
            json={"note": "v1.1 迁移"},
        )
        print(f"基于评估一提交案卷：HTTP {stale.status_code}（评估已失效）")
        if stale.status_code == 409:
            for reason in stale.json()["detail"]["reasons"]:
                print(f"  失效原因：{reason}")
        cutoff5 = api.post(
            "/cutoffs",
            "server",
            json={
                "label": "cutoff-2024-04-29",
                "upload_ids": sorted(
                    list(uploads.values())
                    + list(late.values())
                    + list(up1b.values())
                    + list(up3.values())
                    + list(up4.values())
                ),
            },
        )
        ev3 = api.post(
            "/revision-evaluations",
            "server",
            json={
                "old_revision_id": rev,
                "new_revision_id": rev11["id"],
                "old_cutoff_id": cutoff3["id"],
                "new_cutoff_id": cutoff5["id"],
            },
        )
        ev3_cases = api.get(f"/revision-evaluations/{ev3['id']}/cases")
        e04 = [
            c
            for c in ev3_cases
            if c["center_code"] == "EAST" and c["category"] == "joint"
        ]
        for c in e04:
            print(
                f"重评捕获新补传影响：{c['study_id']}（EAST）"
                f"{c['outcomes']['old_old']} → {c['outcomes']['new_new']}，"
                f"最小充分组合 {c['msc'].get('sets')}"
            )
        dossier = api.post(
            f"/revision-evaluations/{ev3['id']}/migration-dossiers",
            "server",
            json={"note": "v1.1 迁移案卷（基于重评）"},
        )
        print(f"基于重评（评估 #{ev3['id']}）的迁移案卷 #{dossier['id']} 已生成")

        print("\n-- 7.6 影响目录：哪些分析可继续引用旧队列，哪些需研究者重新确认 --")
        for a in dossier["analyses"]:
            print(
                f"  分析 #{a['analysis_run_id']}（{a['kind']}，快照 #{a['snapshot_id']}）："
                f"{'可继续引用旧队列' if a['disposition'] == 'reusable' else '需研究者重新确认'}"
                f"（{a['note']}）"
            )
        api.post(f"/migration-dossiers/{dossier['id']}/acknowledge", "server")
        old1 = api.get(f"/reports/{report1['id']}")
        old2 = api.get(f"/reports/{report2['id']}")
        print(
            f"旧报告内容校验和未变：v1={old1['content_hash'] == report1['content_hash']}，"
            f"v2={old2['content_hash'] == report2['content_hash']}；案卷不重写报告、不重新批准方案"
        )

        summary = {
            "rev": rev,
            "cutoff1": cutoff1["id"],
            "run1": run1b["id"],
            "snapshot1": snap1["id"],
            "report1": report1["id"],
            "cutoff2": cutoff2["id"],
            "run_incremental": inc["id"],
            "equivalence_equal": verify["equal"],
            "snapshot2": snap2["id"],
            "report2": report2["id"],
            "cutoff3": cutoff3["id"],
            "snapshot3": snap3["id"],
            "revision_v1_1": rev11["id"],
            "revision_v1_2": rev12["id"],
            "evaluation_v10_v11": ev1["id"],
            "evaluation_v10_v12": ev2["id"],
            "evaluation_rerun": ev3["id"],
            "dossier": dossier["id"],
        }
        print("\n== 结果摘要 ==")
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    finally:
        api.close()


if __name__ == "__main__":
    sys.exit(main())
