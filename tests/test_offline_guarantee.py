"""Offline guarantee: scanning never opens a network socket.

The scanner is required to be fully offline (no credential validation, no
telemetry). We enforce that at test time by replacing ``socket.socket`` with
a sentinel that raises on construction; a complete scan must still succeed.
"""

from __future__ import annotations

import socket

from secretscan.fingerprint import CandidateFingerprinter
from secretscan.rules import load_rules
from secretscan.scanner import Scanner


class NetworkAttempted(RuntimeError):
    pass


def _deny(*args, **kwargs):
    raise NetworkAttempted("secret scanning must not open sockets")


def test_scan_completes_with_all_sockets_blocked(snapshot_copy, fixed_keys, monkeypatch):
    monkeypatch.setattr(socket, "socket", _deny)
    monkeypatch.setattr(socket, "create_connection", _deny)
    # ssl wraps sockets; with socket construction denied it can't connect,
    # but block the high-level API too for explicitness.
    import ssl

    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", _deny, raising=False)

    master, salt = fixed_keys
    scanner = Scanner(load_rules("config/rules.yaml"), CandidateFingerprinter(master, salt))
    result = scanner.scan(
        str(snapshot_copy),
        scan_id="scan_offline",
        project_id="proj_offline",
        request_id="req_offline",
    ).to_dict()
    assert result["summary"]["candidates_total"] == 5
