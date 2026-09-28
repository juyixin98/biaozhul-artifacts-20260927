"""Generate local synthetic sample archives for manual curl requests.

Run:  .venv/bin/python examples/make_samples.py
Writes into examples/samples/ using the same independent stdlib-only builder
used by the test suite. Nothing here requires network or real data.
"""
from __future__ import annotations

import gzip
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fixtures import builder as fb  # noqa: E402

OUT = Path(__file__).resolve().parent / "samples"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "good.zip").write_bytes(
        fb.zip_bytes([
            {"name": "docs/readme.txt", "data": b"hello from a safe archive\n"},
            {"name": "docs/notes.txt", "data": b"second file"},
        ])
    )
    (OUT / "good.tar").write_bytes(
        fb.tar_bytes([{"name": "dir/file.txt", "data": b"plain tar payload"}])
    )
    (OUT / "traversal.zip").write_bytes(
        fb.zip_bytes([{"name": "../escape.txt", "data": b"evil"}])
    )
    (OUT / "case_collision.zip").write_bytes(
        fb.zip_bytes([{"name": "A", "data": b"1"}, {"name": "a", "data": b"2"}])
    )
    (OUT / "symlink_escape.zip").write_bytes(
        fb.zip_bytes([
            {"name": "lnk", "link": "../../../../etc/passwd"},
            {"name": "lnk/x", "data": b"y"},
        ])
    )
    (OUT / "symlink_loop.zip").write_bytes(
        fb.zip_bytes([
            {"name": "a", "link": "b"}, {"name": "b", "link": "a"},
            {"name": "a/f", "data": b"z"},
        ])
    )
    (OUT / "hardlink.tar").write_bytes(
        fb.tar_bytes([{"name": "t", "data": b"x"}, {"name": "h", "hardlink": "t"}])
    )
    (OUT / "fifo.tar").write_bytes(fb.tar_bytes([{"name": "p", "fifo": True}]))
    (OUT / "declared_lie.zip").write_bytes(fb.zip_with_declared_size_mismatch())
    (OUT / "crc_lie.zip").write_bytes(fb.zip_with_crc_mismatch())
    payload = fb.random_bytes(4, seed=9) * 60000  # ~240 KiB, deflates ~900x
    (OUT / "bomb.zip").write_bytes(fb.zip_bytes([{"name": "z", "data": payload}]))
    (OUT / "tar_gz.tar.gz").write_bytes(
        gzip.compress(fb.tar_bytes([{"name": "a", "data": b"x"}]))
    )
    (OUT / "encrypted.zip").write_bytes(fb.zip_encryption_flag())
    print(f"wrote {len(list(OUT.iterdir()))} sample archives to {OUT}")


if __name__ == "__main__":
    main()
