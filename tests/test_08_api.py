"""HTTP API behaviour with an in-process ASGI client."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ffg_slash.api import router
from ffg_slash.logging_setup import configure_logging
from ffg_slash.registry import ValidatorRegistry
from ffg_slash.storage import Storage

from .conftest import CHAIN_ID, GENESIS_ROOT, corrupt_signature, make_vote


def _client(tmp_path, keys):
    logger, run_id, _ = configure_logging(tmp_path / "logs")
    storage = Storage(tmp_path / "api.sqlite3")
    storage.init_meta(CHAIN_ID, GENESIS_ROOT)
    registry = ValidatorRegistry(CHAIN_ID)
    registry.add_epoch(2, {keys["alpha"][1]: 1, keys["bravo"][1]: 1,
                           keys["charlie"][1]: 1, keys["delta"][1]: 1})
    from ffg_slash.detector import SlashingService
    service = SlashingService(CHAIN_ID, GENESIS_ROOT, registry, storage,
                              run_id, logger)
    app = FastAPI()
    app.state.service = service
    app.include_router(router)
    return TestClient(app), service


def test_post_vote_lifecycle_and_status_codes(tmp_path, keys):
    client, svc = _client(tmp_path, keys)
    seed, pub = keys["alpha"]

    assert client.get("/health").json()["chain_id"] == CHAIN_ID

    good = make_vote(seed, pub, source_epoch=1, target_epoch=2,
                     target_root=b"\xAA" * 32)
    r1 = client.post("/votes", json=good.to_envelope())
    assert r1.status_code == 201
    assert r1.json()["status"] == "accepted"

    r_dup = client.post("/votes", json=good.to_envelope())
    assert r_dup.status_code == 200
    assert r_dup.json()["status"] == "duplicate"

    conflict = make_vote(seed, pub, source_epoch=1, target_epoch=2,
                         target_root=b"\xBB" * 32)
    r_conf = client.post("/votes", json=conflict.to_envelope())
    assert r_conf.status_code == 201
    assert len(r_conf.json()["evidences"]) == 1

    forged = client.post("/votes", json=corrupt_signature(
        make_vote(seed, pub, source_epoch=1, target_epoch=2,
                  target_root=b"\xCC" * 32)).to_envelope())
    assert forged.status_code == 400
    assert forged.json()["reject_reason"] == "invalid_signature"

    r_malformed = client.post("/votes", content=b"{not json",
                              headers={"content-type": "application/json"})
    assert r_malformed.status_code == 422


def test_evidence_and_stats_endpoints(tmp_path, keys):
    client, svc = _client(tmp_path, keys)
    seed, pub = keys["alpha"]
    client.post("/votes", json=make_vote(
        seed, pub, source_epoch=1, target_epoch=2,
        target_root=b"\x01" * 32).to_envelope())
    client.post("/votes", json=make_vote(
        seed, pub, source_epoch=1, target_epoch=2,
        target_root=b"\x02" * 32).to_envelope())

    listing = client.get("/evidences").json()["evidences"]
    assert len(listing) == 1
    eid = listing[0]["evidence_id"]
    detail = client.get(f"/evidences/{eid}")
    assert detail.status_code == 200
    assert detail.json()["evidence_id"] == eid
    assert client.get("/evidences/nope").status_code == 404

    stats = client.get("/stats").json()
    assert stats["invalid_signatures"] == 0
    assert stats["real_conflicts_total"] == 1
    assert "finality" in stats
