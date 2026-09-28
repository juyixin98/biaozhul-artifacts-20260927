#!/usr/bin/env python3
"""Local end-to-end demo -- no running server or network needed.

Builds a throwaway SQLite dictionary DB, publishes two versions, and runs
ambiguous / OOV / normalization / pinning scenarios through the real
registry + DAG segmenter.

    python scripts/demo.py
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app.diagnostics import RequestDiagnostics, new_request_id
from app.storage.registry import VersionRegistry
from app.storage.repository import DictionaryRepository

V1 = [
    {"surface": "研究", "frequency": 100},
    {"surface": "研究生", "frequency": 50},
    {"surface": "生命", "frequency": 90},
    {"surface": "学", "frequency": 30},
    {"surface": "生", "frequency": 20},
    {"surface": "命", "frequency": 15},
    {"surface": "fish", "frequency": 40},
    {"surface": "strasse", "frequency": 35},
]

V2 = [
    {"surface": "研究", "frequency": 100},
    {"surface": "研究生", "frequency": 300},   # now strongly preferred
    {"surface": "生命", "frequency": 90},
    {"surface": "学", "frequency": 30},
    {"surface": "生", "frequency": 20},
    {"surface": "命", "frequency": 15},
    {"surface": "fish", "frequency": 40},
    {"surface": "strasse", "frequency": 35},
]


def show(title: str, result) -> None:
    print(f"\n=== {title} ===")
    print(f"request_id : {result.request_id}")
    print(f"version    : {result.version}")
    print(f"normalized : {result.normalized_text!r}")
    for tok in result.tokens:
        tag = "OOV" if tok.is_unknown else "dict"
        print(f"  [{tag:>4}] {tok.display!r:>8} cost={tok.cost:8.4f} "
              f"norm=[{tok.norm_start},{tok.norm_end}) "
              f"orig=[{tok.orig_start},{tok.orig_end})")
    print(f"best path  : {list(result.best.surfaces)} cost={result.best.cost:.4f}")
    if result.runner_up:
        print(f"runner-up  : {list(result.runner_up.surfaces)} cost={result.runner_up.cost:.4f}")
        print(f"gap        : {result.gap_rounded} ({result.gap_status})")
    else:
        print(f"gap        : n/a ({result.gap_status})")
    print(f"coverage   : tiled={result.orig_covered} rebuilt={result.reconstructed} "
          f"unknown_tokens={result.unknown_tokens} removed_chars={result.removed_chars}")


def main() -> None:
    tmpdir = tempfile.mkdtemp(prefix="segdemo-")
    db_path = Path(tmpdir) / "demo.db"
    repo = DictionaryRepository(db_path)
    registry = VersionRegistry(repo, unknown_char_cost=8.0)

    v1 = registry.publish(V1, note="demo v1")
    print(f"published {v1.version} ({v1.word_count()} words)")

    diag = lambda: RequestDiagnostics(new_request_id())

    cases = ["研究生命", "南京市", "ﬁsh", "STRASSE", "a​b"]  # contains a zero-width space
    for text in cases:
        show(f"v1 segment {text!r}", registry.segment(text, diag()))

    v2 = registry.publish(V2, note="demo v2: boost 研究生")
    print(f"\npublished {v2.version} ({v2.word_count()} words)")

    show("v2 (current): '研究生命'", registry.segment("研究生命", diag()))
    show("v1 pinned after v2 publish: '研究生命'",
         registry.segment("研究生命", diag(), version_ref=v1.version))

    print("\nDiagnostics for the pinned request (why accepted):")
    d = RequestDiagnostics(new_request_id())
    registry.segment("研究生命", d, version_ref=v1.version)
    print(json.dumps(d.public_view(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
