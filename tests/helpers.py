"""Helpers shared by tests (client-side base64, scan driving, paging)."""
from __future__ import annotations

import base64
from typing import Iterable, List, Optional, Sequence, Tuple


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def create_version(client, patterns: Sequence[bytes], *,
                   encoding: str = "binary",
                   case_mode: str = "sensitive",
                   name: str = "") -> str:
    resp = client.post("/versions", json={
        "name": name,
        "encoding": encoding,
        "case_mode": case_mode,
        "patterns": [b64(p) for p in patterns],
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["version_id"]


def open_scan(client, version_id: str) -> str:
    resp = client.post("/scans", json={"version_id": version_id})
    assert resp.status_code == 201, resp.text
    return resp.json()["scan_id"]


def feed(client, scan_id: str, chunk: bytes,
         version_id: Optional[str] = None, expected: int = 200):
    payload = {"chunk": b64(chunk)}
    if version_id is not None:
        payload["version_id"] = version_id
    resp = client.post(f"/scans/{scan_id}/chunks", json=payload)
    assert resp.status_code == expected, resp.text
    return resp.json()


def page_all(client, scan_id: str, limit: int = 4) -> List[dict]:
    """Drain every hit page following next_cursor; asserts resumability."""
    out: List[dict] = []
    cursor = None
    pages = 0
    while True:
        q = f"?limit={limit}"
        if cursor:
            q += f"&cursor={cursor}"
        resp = client.get(f"/scans/{scan_id}/hits{q}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        out.extend(body["hits"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
        if pages > 10000:
            raise AssertionError("pagination did not terminate")
    return out


def hit_tuples(hits: Iterable[dict]) -> List[Tuple[int, int, int]]:
    return sorted((h["start"], h["end"], h["pattern_id"]) for h in hits)
