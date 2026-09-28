"""Security kernel tests: masking, fingerprints, redaction, Secret wrapper."""

import logging

from conftest import (DEV_PEPPER, DEV_PEPPER_ID, EXPECTED_MASKS, GHP_TOKEN,
                      GENERIC_TOKEN, expected_fingerprint)
from secretscan.security import (Fingerprinter, RedactingFilter, Secret,
                                 file_sha256, mask_value,
                                 redacted_preview)


def test_mask_matches_independent_rule():
    assert mask_value(GHP_TOKEN) == EXPECTED_MASKS["ghp"]
    assert mask_value(GENERIC_TOKEN) == EXPECTED_MASKS["generic"]


def test_mask_keeps_only_ends_and_is_reversible_only_by_guess():
    masked = mask_value(GHP_TOKEN)
    assert masked.startswith("ghp_")
    assert masked.endswith("EIka")
    assert "*" in masked
    # The middle of the value is not present.
    assert GHP_TOKEN[8:-4] not in masked


def test_short_values_are_fully_masked():
    assert mask_value("hunter2") == "*******"
    assert mask_value("12345678901") == "*" * 11  # <=12 boundary
    assert mask_value("123456789012") == "*" * 12
    # 13 chars: ends start being kept.
    thirteen = "1234567890123"
    masked13 = mask_value(thirteen)
    assert masked13 != "*" * 13
    assert masked13.startswith("1") and masked13.endswith("3")


def test_pem_mask_preserves_structure_but_hides_body():
    pem = ("-----BEGIN RSA PRIVATE KEY-----\n"
           "MIIEpAIBAAKCAQEAsecretbodyvaluewhichisquitelongindeed\n"
           "-----END RSA PRIVATE KEY-----")
    masked = mask_value(pem)
    assert "BEGIN RSA PRIVATE KEY" in masked
    assert "END RSA PRIVATE KEY" in masked
    assert "secretbodyvalue" not in masked


def test_secret_wrapper_never_renders_raw_value():
    secret = Secret(GHP_TOKEN)
    assert str(secret) == EXPECTED_MASKS["ghp"]
    assert repr(secret) == f"Secret({EXPECTED_MASKS['ghp']!r})"
    assert f"{secret}" == EXPECTED_MASKS["ghp"]
    # Raw value only via explicit expose().
    assert secret.expose() == GHP_TOKEN


def test_fingerprint_matches_independent_stdlib_hmac():
    fp = Fingerprinter(DEV_PEPPER)
    assert fp.pepper_id == DEV_PEPPER_ID
    assert fp.fingerprint(GHP_TOKEN) == expected_fingerprint(GHP_TOKEN)
    assert fp.fingerprint(GENERIC_TOKEN) == expected_fingerprint(GENERIC_TOKEN)


def test_fingerprint_is_content_bound_one_char_change_breaks_it():
    fp = Fingerprinter(DEV_PEPPER)
    changed = GHP_TOKEN[:-1] + ("b" if GHP_TOKEN[-1] != "b" else "c")
    assert fp.fingerprint(changed) != fp.fingerprint(GHP_TOKEN)
    assert not fp.matches(changed, fp.fingerprint(GHP_TOKEN))
    assert fp.matches(GHP_TOKEN, fp.fingerprint(GHP_TOKEN))


def test_fingerprint_depends_on_pepper():
    fp1 = Fingerprinter(DEV_PEPPER)
    fp2 = Fingerprinter("a-different-pepper-value")
    assert fp1.fingerprint(GHP_TOKEN) != fp2.fingerprint(GHP_TOKEN)


def test_file_sha256_is_stable_and_distinguishes_content():
    assert file_sha256(b"abc") == file_sha256(b"abc")
    assert file_sha256(b"abc") != file_sha256(b"abd")


def test_redacted_preview_hides_every_span_byte():
    text = "prefix TOKEN=ghp_1eAoPJ4BzuZNn3XmX7lgARsGjSQZTBCSEIka suffix"
    start = text.index("ghp_")
    end = start + 40
    preview = redacted_preview(text, start, end)
    assert "ghp_1eAo" not in preview
    assert preview.count("*") >= 10
    # Context outside the span survives.
    assert "TOKEN=" in preview or "suffix" in preview


def test_redacting_filter_scrubs_registered_values():
    flt = RedactingFilter()
    flt.register([GHP_TOKEN])
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1,
        "found value %s", (GHP_TOKEN,), None)
    assert flt.filter(record)
    assert GHP_TOKEN not in record.getMessage()
    assert EXPECTED_MASKS["ghp"] in record.getMessage()


def test_redacting_filter_leaves_unrelated_messages_alone():
    flt = RedactingFilter()
    flt.register([GHP_TOKEN])
    record = logging.LogRecord(
        "x", logging.INFO, __file__, 1, "ordinary message", (), None)
    flt.filter(record)
    assert record.getMessage() == "ordinary message"
    flt.clear()
    record2 = logging.LogRecord(
        "x", logging.INFO, __file__, 1, GHP_TOKEN, (), None)
    flt.filter(record2)
    # After clear, it no longer scrubs (registration is per-scan).
    assert record2.getMessage() == GHP_TOKEN
