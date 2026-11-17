"""Reusable API-level scenario helpers for the demo script and the tests.

Everything goes through the HTTP layer so tests exercise the same paths a
reviewer would.
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import Base, SessionLocal, engine
from app.security import seed_users

from . import fixtures

TOKENS = {
    "admin": "admin-token",
    "reviewer": "reviewer-token",
    "analysis": "analysis-token",
    "privacy": "privacy-token",
    "north": "center-north-token",
    "south": "center-south-token",
    "east": "center-east-token",
}
UPLOADER = {"NORTH": "north", "SOUTH": "south", "EAST": "east"}


def auth(who: str) -> dict:
    return {"Authorization": f"Bearer {TOKENS[who]}"}


def reset_schema() -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_users(db)


def seed_centers(client: TestClient) -> dict[str, int]:
    ids = {}
    for code, spec in fixtures.CENTER_SPECS.items():
        r = client.post(
            "/centers",
            json={"code": code, "name": spec["name"], "config": spec["config"]},
            headers=auth("reviewer"),
        )
        r.raise_for_status()
        ids[code] = r.json()["id"]
    # Link centre uploaders now that centres exist.
    with SessionLocal() as db:
        seed_users(db)
    for code in fixtures.CENTER_SPECS:
        for mapping in fixtures.mappings_for(code):
            r = client.post("/mappings", json=mapping, headers=auth("reviewer"))
            r.raise_for_status()
    return ids


def create_protocol(client: TestClient, rules=None) -> dict:
    proto = client.post(
        "/protocols",
        json={"name": "DKD-MET cohort (fictional)", "description": "Sample protocol"},
        headers=auth("reviewer"),
    ).json()
    rev = client.post(
        f"/protocols/{proto['id']}/revisions",
        json={"version": "v1.0", "rules": rules or fixtures.PROTOCOL_RULES},
        headers=auth("reviewer"),
    )
    rev.raise_for_status()
    rev = rev.json()
    client.post(
        f"/protocols/revisions/{rev['id']}/approve", headers=auth("reviewer")
    ).raise_for_status()
    return {"protocol_id": proto["id"], "revision_id": rev["id"]}


def post_upload(client: TestClient, upload: dict) -> dict:
    r = client.post(
        "/uploads", json=upload, headers=auth(UPLOADER[upload["center_code"]])
    )
    r.raise_for_status()
    return r.json()


def upload_batch1(client: TestClient) -> dict[str, int]:
    return {
        u["batch_code"]: post_upload(client, u)["id"] for u in fixtures.BATCH1_UPLOADS
    }


def upload_batch2(client: TestClient) -> dict[str, int]:
    return {
        u["batch_code"]: post_upload(client, u)["id"] for u in fixtures.BATCH2_UPLOADS
    }


def upload_batch1b(client: TestClient) -> dict[str, int]:
    return {
        u["batch_code"]: post_upload(client, u)["id"] for u in fixtures.BATCH1B_UPLOADS
    }


def upload_batch3(client: TestClient) -> dict[str, int]:
    return {
        u["batch_code"]: post_upload(client, u)["id"] for u in fixtures.BATCH3_UPLOADS
    }


def upload_batch4(client: TestClient) -> dict[str, int]:
    return {
        u["batch_code"]: post_upload(client, u)["id"] for u in fixtures.BATCH4_UPLOADS
    }


def create_revision(client: TestClient, protocol_id: int, version: str, rules: dict) -> dict:
    rev = client.post(
        f"/protocols/{protocol_id}/revisions",
        json={"version": version, "rules": rules},
        headers=auth("reviewer"),
    )
    rev.raise_for_status()
    rev = rev.json()
    client.post(
        f"/protocols/revisions/{rev['id']}/approve", headers=auth("reviewer")
    ).raise_for_status()
    return rev


def run_evaluation(
    client: TestClient,
    old_revision_id: int,
    new_revision_id: int,
    old_cutoff_id: int,
    new_cutoff_id: int,
    budget_per_case: int = 32,
    who: str = "analysis",
):
    return client.post(
        "/revision-evaluations",
        json={
            "old_revision_id": old_revision_id,
            "new_revision_id": new_revision_id,
            "old_cutoff_id": old_cutoff_id,
            "new_cutoff_id": new_cutoff_id,
            "budget_per_case": budget_per_case,
        },
        headers=auth(who),
    )


def evaluation_cases(client: TestClient, evaluation_id: int) -> dict[str, dict]:
    r = client.get(f"/revision-evaluations/{evaluation_id}/cases", headers=auth("analysis"))
    r.raise_for_status()
    return {c["study_id"]: c for c in r.json()}


def decide_all_identity(client: TestClient, status: str = "rejected") -> int:
    cands = client.get("/identity-candidates", headers=auth("privacy")).json()
    for cand in cands:
        client.post(
            f"/identity-candidates/{cand['id']}/decide",
            json={"status": status},
            headers=auth("privacy"),
        ).raise_for_status()
    return len(cands)


def make_cutoff(client: TestClient, label: str, upload_ids: list[int]) -> dict:
    r = client.post(
        "/cutoffs",
        json={"label": label, "upload_ids": sorted(upload_ids)},
        headers=auth("analysis"),
    )
    r.raise_for_status()
    return r.json()


def start_run(
    client: TestClient,
    revision_id: int,
    cutoff_id: int,
    strategy: str = "full",
    base_run_id: int | None = None,
    crash_after_centers: int | None = None,
) -> dict:
    body = {
        "protocol_revision_id": revision_id,
        "cutoff_batch_id": cutoff_id,
        "strategy": strategy,
        "base_run_id": base_run_id,
        "crash_after_centers": crash_after_centers,
    }
    return client.post("/screening-runs", json=body, headers=auth("analysis"))


def decisions_by_study(client: TestClient, run_id: int) -> dict[str, dict]:
    r = client.get(f"/screening-runs/{run_id}/decisions", headers=auth("analysis"))
    r.raise_for_status()
    return {d["study_id"]: d for d in r.json()}


def outcomes(client: TestClient, run_id: int) -> dict[str, str]:
    return {sid: d["outcome"] for sid, d in decisions_by_study(client, run_id).items()}
