"""Tamper-evident audit: hash chain, HMAC manifest, SQLite correlation."""

from __future__ import annotations

import json
from pathlib import Path

from archguard.audit import (
    canonical_json,
    hmac_sign,
    hmac_verify,
    verify_chain,
    verify_manifest_file,
)

from fixtures_archive import ZipSpec, build_zip


def test_hash_chain_links_records(svc):
    data = build_zip([ZipSpec("a.txt", data=b"x")])
    v = svc["engine"].inspect(data, input_name="ok.zip")
    result = verify_chain(svc["audit"].log_path)
    assert result["ok"] is True
    assert result["records"] >= 6  # start, scan, budget, plan, extract, verify...

    # Records are correlated to the run id and ordered.
    events = svc["store"].get_events(v.run_id)
    run_ids = {e["run_id"] for e in events}
    assert run_ids == {v.run_id}
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)


def test_hash_chain_detects_tampering(svc):
    data = build_zip([ZipSpec("a.txt", data=b"x")])
    svc["engine"].inspect(data, input_name="ok.zip")
    log = svc["audit"].log_path
    lines = log.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[2])
    record["detail"]["tampered"] = True
    lines[2] = json.dumps(record, sort_keys=True)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = verify_chain(log)
    assert result["ok"] is False
    assert result["break_line"] == 3


def test_manifest_signature_validates(svc):
    data = build_zip([ZipSpec("a.txt", data=b"x")])
    v = svc["engine"].inspect(data, input_name="ok.zip")
    manifest_path = Path(v.manifest_path)
    result = verify_manifest_file(manifest_path, svc["audit"].key)
    assert result["ok"] is True
    assert result["run_id"] == v.run_id
    assert result["status"] == "accepted"


def test_manifest_tampering_invalidates_signature(svc):
    data = build_zip([ZipSpec("a.txt", data=b"x")])
    v = svc["engine"].inspect(data, input_name="ok.zip")
    path = Path(v.manifest_path)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["manifest"]["files"][0]["sha256"] = "deadbeef"
    path.write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")
    result = verify_manifest_file(path, svc["audit"].key)
    assert result["ok"] is False


def test_hmac_roundtrip():
    key = b"0" * 32
    msg = canonical_json({"a": 1, "b": [2, 3]})
    sig = hmac_sign(key, msg)
    assert hmac_verify(key, msg, sig)
    assert not hmac_verify(key, msg + b" ", sig)
    assert not hmac_verify(key, msg, "not-hex")


def test_rejected_run_also_has_signed_manifest(svc):
    bad = build_zip([ZipSpec("../escape", data=b"x")])
    v = svc["engine"].inspect(bad, input_name="bad.zip")
    assert v.status == "rejected"
    result = verify_manifest_file(Path(v.manifest_path), svc["audit"].key)
    assert result["ok"] is True
    assert result["status"] == "rejected"
    envelope = json.loads(Path(v.manifest_path).read_text())
    assert envelope["manifest"]["failure"]["category"] == "PATH_TRAVERSAL"


def test_store_records_final_status(svc):
    good = build_zip([ZipSpec("a.txt", data=b"x")])
    vg = svc["engine"].inspect(good, input_name="ok.zip")
    bad = build_zip([ZipSpec("../b", data=b"x")])
    vb = svc["engine"].inspect(bad, input_name="bad.zip")

    assert svc["store"].get_run(vg.run_id)["status"] == "accepted"
    assert svc["store"].get_run(vb.run_id)["status"] == "rejected"
    stats = svc["store"].stats()
    assert stats["accepted"] == 1
    assert stats["rejected"] == 1
    assert stats["events"] >= 2
