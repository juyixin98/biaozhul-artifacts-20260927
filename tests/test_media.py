"""Text/binary classification and printable-run extraction tests."""

import pytest

from secretscan import media


def test_utf8_text_is_text():
    kind = media.classify("héllo wörld\nline2\n".encode("utf-8"))
    assert kind.kind == "text"
    assert kind.reason == "utf8-decodable"


def test_nul_byte_means_binary():
    kind = media.classify(b"abc\x00def")
    assert kind.kind == "binary"
    assert kind.reason == "nul-byte-present"


def test_invalid_utf8_is_binary_not_text():
    # latin-1 bytes that are not valid UTF-8: classified binary, reason
    # explicitly states why.
    kind = media.classify(b"caf\xe9 without nuls")
    assert kind.kind == "binary"
    assert kind.reason == "not-valid-utf8"


def test_empty_file_is_text():
    assert media.classify(b"").kind == "text"


def test_printable_runs_extract_embedded_ascii_from_binary():
    data = b"\x00\xff\x01TOKEN=abcdefghij\x00\x00klmn\x00endvalue1234"
    runs = media.printable_runs(data, min_run=8)
    joined = b"|".join(r for r, _ in runs)
    assert b"abcdefghij" in joined
    # Offsets are reported and non-decreasing.
    offsets = [off for _, off in runs]
    assert offsets == sorted(offsets)
    assert all(data[off:off + len(r)] == r for r, off in runs)


def test_printable_runs_respect_minimum_length():
    data = b"short12 longenoughvalue"
    runs = media.printable_runs(data, min_run=12)
    assert all(len(r) >= 12 for r, _ in runs)
    assert b"longenoughvalue" in b"".join(r for r, _ in runs)


def test_printable_runs_reject_bad_min_run():
    with pytest.raises(ValueError):
        media.printable_runs(b"abc", min_run=0)
