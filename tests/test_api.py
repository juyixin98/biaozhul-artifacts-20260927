"""HTTP service tests: status mapping, run ids, and end-to-end update flow."""

import pytest

# Ensure src is on the path (conftest handles it, but TestClient needs import).
from fastapi.testclient import TestClient

from lc.app import AppState, create_app
from lc.clock import FixedClock
from lc.config import KernelConfig
from lc.types import Checkpoint


@pytest.fixture
def client(tmp_path, golden, monkeypatch):
    state = AppState(db_path=str(tmp_path / "api.db"))
    # freeze clock after all fixture timestamps (boundary tests move it)
    state.kernel.clock = FixedClock(
        golden["constants"]["T0"] + 400 * golden["constants"]["SLOT_MS"]
    )
    app = create_app(state)
    with TestClient(app) as c:
        c.lc_state = state
        yield c


def _install(client, golden):
    r = client.post("/checkpoint", json=golden["checkpoint"])
    assert r.status_code == 201, r.text
    return r.json()["result"]


def test_health_and_empty_head(client):
    assert client.get("/health").json()["status"] == "ok"
    head = client.get("/head").json()
    assert head["initialized"] is False


def test_checkpoint_then_update_ok(client, golden):
    res = _install(client, golden)
    assert res["accepted"] is True
    chain = next(v for v in golden["vectors"] if v["kind"] == "chain")
    item = chain["items"][0]
    r = client.post(
        "/headers/update",
        json={
            "header": item["header"],
            "certificate": item["certificate"],
            "next_committee": None,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()["result"]
    assert body["accepted"] is True
    assert body["round"] == 101


def test_underweight_is_422_computation(client, golden):
    _install(client, golden)
    vec = next(
        v for v in golden["vectors"] if v["id"] == "weight_below_threshold"
    )
    r = client.post(
        "/headers/update",
        json={"header": vec["header"], "certificate": vec["certificate"]},
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INSUFFICIENT_WEIGHT"
    assert err["category"] == "computation_failure"
    assert r.json().get("run_id")


def test_untrusted_branch_is_409_state_conflict(client, golden):
    _install(client, golden)
    vec = next(
        v for v in golden["vectors"] if v["id"] == "untrusted_branch_unknown_parent"
    )
    r = client.post(
        "/headers/update",
        json={"header": vec["header"], "certificate": vec["certificate"]},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "UNTRUSTED_BRANCH"


def test_malformed_input_is_400(client, golden):
    _install(client, golden)
    r = client.post(
        "/headers/update",
        json={"header": {"round": -1}, "certificate": {}},
    )
    assert r.status_code == 400  # schema validation -> input_error
    assert r.json()["error"]["category"] == "input_error"
    # structurally valid JSON but bad hex -> 400 from the parser
    vec = next(v for v in golden["vectors"] if v["id"] == "stale_round")
    bad_header = dict(vec["header"])
    bad_header["body_root"] = "0xzz"
    r2 = client.post(
        "/headers/update",
        json={"header": bad_header, "certificate": vec["certificate"]},
    )
    assert r2.status_code == 400
    assert r2.json()["error"]["category"] == "input_error"


def test_trust_expired_is_410(client, golden):
    _install(client, golden)
    period = golden["constants"]["trust_period_ms"]
    client.lc_state.kernel.clock.set(golden["constants"]["T0"] + period + 1)
    vec = next(v for v in golden["vectors"] if v["id"] == "stale_round")
    r = client.post(
        "/headers/update",
        json={"header": vec["header"], "certificate": vec["certificate"]},
    )
    assert r.status_code == 410
    body = r.json()
    assert body["error"]["code"] == "TRUST_EXPIRED"
    assert body["error"]["details"]["needs_new_checkpoint"] is True


def test_second_checkpoint_conflict_409(client, golden):
    _install(client, golden)
    r = client.post("/checkpoint", json=golden["checkpoint"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CHECKPOINT_CONFLICT"


def test_header_lookup_404_and_hit(client, golden):
    _install(client, golden)
    r = client.get("/headers/0x" + "ab" * 32)
    assert r.status_code == 404
    root = golden["checkpoint"]["root"]
    r2 = client.get(f"/headers/{root}")
    assert r2.status_code == 200
    assert r2.json()["header"]["round"] == 100


def test_replay_endpoint_atomic(client, golden):
    _install(client, golden)
    chain = next(v for v in golden["vectors"] if v["kind"] == "chain")
    items = [
        {"header": it["header"], "certificate": it["certificate"]}
        for it in chain["items"]
    ]
    r = client.post("/replay", json={"items": items})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["tip_after"]["tip_round"] == 103
