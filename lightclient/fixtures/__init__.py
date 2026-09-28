"""Synthetic local fixtures (test-only, but deliberately NOT test code).

This module is an *independent reference builder* for the simplified
protocol: it constructs keys, committees, headers, threshold certificates
and signed checkpoints using only ``codec`` + ``crypto`` + ``types``. It
never imports the kernel or store, so the expected answers in tests come
from an independent producer, not from the code under test.

All key material is deterministic (derived from a label), so golden
fixtures are reproducible byte-for-byte across machines.
"""

from .builder import (
    build_certificate,
    build_checkpoint_envelope,
    build_committee,
    build_header,
    build_key,
    build_legitimate_chain,
    build_replay_file,
    load_replay_file,
    member_seed,
    write_golden_fixture,
)

__all__ = [
    "build_certificate",
    "build_checkpoint_envelope",
    "build_committee",
    "build_header",
    "build_key",
    "build_legitimate_chain",
    "build_replay_file",
    "load_replay_file",
    "member_seed",
    "write_golden_fixture",
]
