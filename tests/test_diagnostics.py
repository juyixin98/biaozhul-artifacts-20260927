"""Diagnostics redaction tests: secrets never printed; ids/state retained."""
from __future__ import annotations

import json
import logging

import pytest

from reorgindex.diag.masking import mask_address, mask_hash, redact
from reorgindex.diag.logger import Diagnostics, new_request_id
from reorgindex.storage.store import IndexStore

pytestmark = pytest.mark.kernel


def test_request_id_shape():
    rid = new_request_id()
    assert rid.startswith("req-") and len(rid) == len("req-") + 16


def test_mask_hash_keeps_prefix_only():
    h = "ab" * 32
    masked = mask_hash(h)
    assert masked.startswith("ab" * 6) and masked.endswith("…")
    assert "ab" * 32 not in masked


def test_mask_address():
    addr = "rx1" + "c" * 40
    masked = mask_address(addr)
    assert masked.startswith("rx1ccccc") and masked.endswith("cccc")
    assert addr not in masked


def test_redact_strips_signer_secrets_but_keeps_state():
    record = {
        "request_id": "req-abc",
        "block_hash": "ab" * 32,
        "signature": "deadbeef" * 16,
        "pubkey": "01" * 32,
        "sender": "rx1" + "a" * 40,
        "height": 3,
        "weight": 20,
        "nested": {"pow_signature": "ff" * 32, "ok": 1},
    }
    safe = redact(record)
    text = json.dumps(safe)
    assert "deadbeef" not in text
    assert "ff" * 32 not in text
    assert '"height": 3' in text and '"weight": 20' in text
    assert safe["request_id"] == "req-abc"
    assert safe["nested"]["ok"] == 1


def test_diagnostics_persist_and_log_redacted(tmp_path, caplog):
    store = IndexStore(tmp_path / "diag.db")
    diag = Diagnostics(store, log_level="INFO")
    rid = "req-diag-1"
    with caplog.at_level(logging.INFO, logger="reorgindex.diag"):
        diag.record(
            request_id=rid,
            outcome="REJECTED",
            reason="REORG_FINALIZED",
            block_hash="cd" * 32,
            height=6,
            parent="ef" * 32,
            weight=36,
            detail="would detach a final block",
        )
    rows = store.diagnostics_for_request(rid)
    assert len(rows) == 1
    assert rows[0]["active_height"] is None or isinstance(rows[0]["active_height"], int)
    # stdout log line must not contain the full 64-char hash.
    log_text = " ".join(r.getMessage() for r in caplog.records)
    assert "cd" * 32 not in log_text
    assert "REORG_FINALIZED" in log_text
    store.close()
