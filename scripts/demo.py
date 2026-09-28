#!/usr/bin/env python3
"""Local demo: run the fixture set through the HTTP API in-process.

Exercises every repair outcome (SOLVED / UNSOLVABLE / ALREADY_VALID) against
the fixtures and exits non-zero if any outcome deviates from the hand-checked
expectation.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.api.routes import create_app  # noqa: E402
from app.config import Settings  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

CASES = [
    ("chained_overlap.srt", "srt", {}, "SOLVED"),
    ("same_start.vtt", "vtt", {}, "SOLVED"),
    ("multibyte.srt", "srt", {}, "SOLVED"),
    ("unrepairable.srt", "srt", {"media_duration_ms": 3000}, "UNSOLVABLE"),
    ("clean.vtt", "vtt", {}, "ALREADY_VALID"),
]


def main():
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Settings(db_path=str(Path(tmp) / "demo.db")))
        client = TestClient(app)
        meta = client.get("/v1/meta").json()
        print(f"subtitle-validator {meta['app_version']}  "
              f"python {meta['python']}  numpy {meta['numpy']}  "
              f"fastapi {meta['fastapi']}")
        failures = 0
        for name, fmt, extra, expected in CASES:
            content = (FIXTURES / name).read_text(encoding="utf-8")
            resp = client.post("/v1/validations",
                               json={"format": fmt, "content": content, **extra})
            if resp.status_code != 201:
                print(f"[FAIL] {name}: HTTP {resp.status_code} {resp.text}")
                failures += 1
                continue
            body = resp.json()
            repair = body["repair"]
            print(f"\n== {name} (job {body['job_id'][:8]}, "
                  f"sha256 {body['input_sha256'][:12]}) ==")
            print(f"  cues={body['cue_count']}")
            for d in body["diagnostics"]:
                print(f"  [{d['severity']}] {d['code']} cues={d['cue_indices']}: "
                      f"{d['message']}")
            print(f"  repair: {repair['status']} "
                  f"minimal_change_ms={repair['minimal_change_ms']}")
            if repair["proposal"]:
                for p in repair["proposal"]:
                    o, n = p["original"], p["proposed"]
                    mark = "*" if p["change_ms"] else " "
                    print(f"   {mark} cue {p['index']}: "
                          f"[{o['start_ms']},{o['end_ms']}] -> "
                          f"[{n['start_ms']},{n['end_ms']}]  "
                          f"({'; '.join(p['reasons'])})")
            if repair["status"] != expected:
                print(f"  [FAIL] expected repair status {expected}")
                failures += 1
        print(f"\n{'DEMO OK' if failures == 0 else f'{failures} FAILURES'}")
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
