#!/usr/bin/env python3
"""End-to-end local demonstration of the segmentation backend.

Runs the real FastAPI stack in-process (via TestClient) against a throwaway
SQLite database, seeds it from data/seed_lexicon.json, and walks through
every behaviour called out in the task:

  1. ambiguous short clause -> optimum + second-best gap
  2. unknown words -> explicit fallback length/cost, no dropped characters
  3. repeated words
  4. variable-length normalization (full-width + ß -> ss, soft hyphen delete)
  5. whole-version publishing + a request pinned to the old version
  6. diagnostics (decision + redaction)

Run:  python scripts/demo.py
No network, no accounts, no real data.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.api import create_app  # noqa: E402
from app.config import Settings  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(resp) -> dict:
    body = resp.json()
    print(f"HTTP {resp.status_code}  request_id={body.get('request_id')}")
    return body


def main() -> int:
    tmp_dir = Path(tempfile.mkdtemp(prefix="segback-demo-"))
    settings = Settings(db_path=tmp_dir / "demo.db", seed_on_start=True)
    app = create_app(settings)

    with TestClient(app) as c:
        banner("health (synthetic seed lexicon auto-published as version 1)")
        print(json.dumps(c.get("/health").json(), ensure_ascii=False, indent=2))

        banner("1) ambiguous clause: 研究生命  (研究|生命 vs 研究生|命)")
        b = show(c.post("/segment", json={"text": "研究生命"},
                        headers={"X-Request-ID": "demo-ambiguous"}))
        print(" segments :", [s["raw_text"] for s in b["segments"]])
        print(" types    :", [s["type"] for s in b["segments"]])
        print(f" best={b['best_cost']:.4f} second={b['second_best_cost']:.4f} "
              f"gap={b['cost_gap']:.4f} -> {b['gap_class']}/{b['decision']}")
        print(" coverage :", b["coverage"])

        banner("2) unknown words: 星巴克咖啡  (fallback keeps every character)")
        b = show(c.post("/segment", json={"text": "星巴克咖啡"},
                        headers={"X-Request-ID": "demo-unknown"}))
        for s in b["segments"]:
            print(f"  {s['type']:7} {s['raw_text']!r:14} length={s['norm_end']-s['norm_start']} "
                  f"cost={s['cost']:.2f} raw[{s['raw_start']}:{s['raw_end']}]")
        print(" coverage :", b["coverage"])

        banner("3) repeated words: 哈哈哈哈")
        b = show(c.post("/segment", json={"text": "哈哈哈哈"}))
        print(" segments:", [s["surface"] for s in b["segments"]],
              f"gap={b['cost_gap']:.4f}")

        banner("4) variable-length normalization: ｓｔｒａßｅ + soft hyphen")
        b = show(c.post("/segment", json={"text": "ｓｔｒａßｅ"}))
        print(" normalized:", repr(b["normalized_text"]))
        print(" char_map  :", b["char_map"], "(norm 4 and 5 share raw index 4 = ß->ss)")
        print(" segments  :", [(s["surface"], s["type"]) for s in b["segments"]])
        raw = "研­究生命"
        b = show(c.post("/segment", json={"text": raw}))
        print(f" raw={raw!r} -> normalized={b['normalized_text']!r}")
        print(" deleted raw indices:", b["deleted_raw_indices"])
        print(" raw slices         :", [s["raw_text"] for s in b["segments"]])
        print(" joined raw slices  :",
              "".join(s["raw_text"] for s in b["segments"]) == raw)

        banner("5) publish a WHOLE new version; old requests stay pinned")
        v2 = show(c.post("/versions", json={
            "note": "demo whole-version replacement",
            "words": [
                {"word": "ab", "freq": 100}, {"word": "cd", "freq": 100},
                {"word": "a", "freq": 5000}, {"word": "bcd", "freq": 5000},
            ],
        }))
        print(" published version_id:", v2["version_id"], "checksum:", v2["checksum"])
        latest = c.post("/segment", json={"text": "abcd"}).json()
        pinned = c.post("/segment", json={"text": "abcd", "version_id": 1}).json()
        print(" latest (v2):", [s["surface"] for s in latest["segments"]],
              "version:", latest["version_id"])
        print(" pinned  v1 :", [s["surface"] for s in pinned["segments"]],
              "version:", pinned["version_id"],
              "(seed lexicon: 'abc' is a word, 'd' is an unknown char)")

        banner("6) failure categories")
        for payload in [
            {"text": ""},
            {"text": "x", "version_id": 999},
            {},
        ]:
            r = c.post("/segment", json=payload)
            print(f" payload={payload} -> {r.status_code} {r.json()['error']}: "
                  f"{r.json()['message']}")

        banner("7) diagnostics explain decisions and redact sensitive input")
        secret = "银行卡号6222000000000000"
        c.post("/segment", json={"text": secret}, headers={"X-Request-ID": "demo-secret"})
        rec = c.get("/diagnostics", params={"request_id": "demo-secret"}).json()["record"]
        safe = {k: rec[k] for k in ("request_id", "outcome", "reason", "version_id",
                                    "input_chars", "best_cost", "cost_gap",
                                    "gap_class", "input_preview")}
        print(json.dumps(safe, ensure_ascii=False, indent=2))
        assert secret not in json.dumps(safe), "raw sensitive text leaked into diagnostics"
        print(" -> full secret string absent from the diagnostic record: OK")

    print("\nDemo finished. Database left at:", settings.db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
