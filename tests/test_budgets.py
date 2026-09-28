"""Compression-bomb and budget enforcement fixtures."""

from __future__ import annotations

import io
import zipfile

from archguard.audit import AuditLogger
from archguard.budget import Budget
from archguard.config import Config
from archguard.engine import Engine
from archguard.errors import RejectionCategory
from archguard.isolation import ensure_home
from archguard.store import Store

from fixtures_archive import ZipSpec, build_zip


def make_engine(home, **limits):
    base = Config.load().to_dict()
    base["home"] = home
    base.update(limits)
    cfg = Config(**base)
    ensure_home(cfg.home)
    store = Store(cfg.home)
    audit = AuditLogger(cfg.home)
    audit.set_sink(store.record_event)
    return Engine(cfg, store, audit), store


def test_total_bytes_bomb_rejected(tmp_path):
    engine, store = make_engine(tmp_path / "h", max_total_bytes=1024)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("bomb.bin", b"\0" * (1024 * 1024))  # ~1MiB zeros, tiny on disk
    v = engine.inspect(buf.getvalue(), input_name="bomb.zip")
    assert v.status == "rejected"
    assert v.category == RejectionCategory.BUDGET_TOTAL_BYTES.value
    # Bomb rejected purely from declared metadata -> no run dir at all.
    assert list((tmp_path / "h" / "runs").iterdir()) == []
    store.close()


def test_file_count_bomb_rejected(tmp_path):
    engine, store = make_engine(tmp_path / "h", max_files=10)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for i in range(50):
            z.writestr(f"f{i:03d}.txt", b"x")
    v = engine.inspect(buf.getvalue(), input_name="many.zip")
    assert v.category == RejectionCategory.BUDGET_FILE_COUNT.value
    store.close()


def test_depth_bomb_rejected(tmp_path):
    engine, store = make_engine(tmp_path / "h", max_depth=3)
    data = build_zip([ZipSpec("a/b/c/d/e.txt", data=b"x")])
    v = engine.inspect(data, input_name="deep.zip")
    assert v.category == RejectionCategory.BUDGET_DEPTH.value
    store.close()


def test_compression_ratio_bomb_rejected(tmp_path):
    # Ratio limit 10:1; highly compressible content blows past it.  Keep the
    # absolute total under max_total_bytes by setting that high.
    engine, store = make_engine(
        tmp_path / "h", max_total_bytes=100_000_000, max_compression_ratio=10.0
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("ratio.bin", b"\0" * (2 * 1024 * 1024))
    on_disk = len(buf.getvalue())
    v = engine.inspect(buf.getvalue(), input_name="ratio.zip")
    assert v.category == RejectionCategory.BUDGET_RATIO.value, v.detail
    assert on_disk < 100_000  # genuinely tiny relative to declared size
    store.close()


def test_budget_under_limits_accepted(tmp_path):
    engine, store = make_engine(
        tmp_path / "h",
        max_total_bytes=1024,
        max_files=2,
        max_depth=2,
        max_compression_ratio=1000.0,
    )
    data = build_zip([
        ZipSpec("a.txt", data=b"aa"),
        ZipSpec("b.txt", data=b"bb"),
    ])
    v = engine.inspect(data, input_name="ok.zip")
    assert v.status == "accepted", v.detail
    store.close()
