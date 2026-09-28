"""Shared pytest fixtures: isolated data roots and configured stores."""

from __future__ import annotations

import dataclasses

import pytest

from zcluster.config import Config
from zcluster.core.store import Store
from zcluster.kernel.coder import DimSpec, MortonCoder


@pytest.fixture
def base_config(tmp_path):
    return Config(
        data_root=str(tmp_path / "data"),
        chunk_size=4,
        default_interval_budget=64,
        max_interval_budget=100000,
        arrow_compression="zstd",
        code_uint64_when_fit=True,
        log_level="WARNING",
        log_file=str(tmp_path / "logs" / "test.log"),
        source_path="test-config",
    )


@pytest.fixture
def make_store(base_config):
    import contextlib

    @contextlib.contextmanager
    def _make(cfg=None):
        cfg = cfg or base_config
        s = Store(cfg)
        try:
            yield s
        finally:
            s.close()
    return _make


@pytest.fixture
def wide_config(tmp_path):
    """Total interleaved bits > 64 => fixed-binary code column path."""
    return dataclasses.replace(
        Config(
            data_root=str(tmp_path / "wide"),
            chunk_size=8, default_interval_budget=256,
            max_interval_budget=1_000_000, arrow_compression="zstd",
            code_uint64_when_fit=True, log_level="WARNING",
            log_file=str(tmp_path / "logs" / "wide.log"),
            source_path="test-wide",
        )
    )
