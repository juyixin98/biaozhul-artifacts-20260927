"""Synthetic sensitive-data fixtures.

Everything here is fabricated locally: no real cards, no real resident IDs.
Values are generated with valid check digits so the evidence validators
accept them; a separate set of *invalid* values drives the uncertainty path.
"""
from __future__ import annotations

from app.rules import cn_id_ok, luhn_ok

SYNTH_EMAIL = "alice.synth@example.test"
SYNTH_MOBILE = "13812345678"
SYNTH_API_TOKEN = "sk_test_SYNTH1234567890abcdef"
SYNTH_SECRET_HEX = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
# 32 hex chars: must not look like a 13-19 digit run (it is hex), fine.


def make_luhn(prefix: str, total_len: int) -> str:
    """Append digits (and a check digit) to build a Luhn-valid synthetic PAN."""
    body = prefix
    while len(body) < total_len - 1:
        body += "0"
    digits = [int(c) for c in body]

    def checksum(ds: list[int]) -> int:
        total = 0
        for i, d in enumerate(reversed(ds)):
            if i % 2 == 0:  # would be doubled with check digit appended
                d *= 2
                if d > 9:
                    d -= 9
            total += d
        return (10 - total % 10) % 10

    number = body + str(checksum(digits))
    assert luhn_ok(number)
    return number


def make_cn_id(prefix17: str) -> str:
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    check_map = "10X98765432"
    assert len(prefix17) == 17 and prefix17.isdigit()
    total = sum(int(prefix17[i]) * weights[i] for i in range(17))
    number = prefix17 + check_map[total % 11]
    assert cn_id_ok(number)
    return number


# UnionPay-shaped synthetic card (62 prefix, 16 digits).
SYNTH_BANK_CARD = make_luhn("621226000000", 16)
# Visa-shaped synthetic card.
SYNTH_BANK_CARD_VISA = make_luhn("411111111111", 16)
# Synthetic resident ID with a valid check digit.
SYNTH_CN_ID = make_cn_id("11010119900307888")

# Same digit shape but invalid check digits -> uncertainty candidates.
BAD_BANK_CARD = SYNTH_BANK_CARD[:-1] + (
    "0" if SYNTH_BANK_CARD[-1] != "0" else "1"
)
assert not luhn_ok(BAD_BANK_CARD)
BAD_CN_ID = SYNTH_CN_ID[:-1] + ("0" if SYNTH_CN_ID[-1] != "0" else "1")
BAD_CN_ID = BAD_CN_ID[:17] + (
    "0" if BAD_CN_ID[17] not in ("0",) else "1"
)
# Ensure invalid without assumptions about the check char.
if cn_id_ok(BAD_CN_ID):
    BAD_CN_ID = BAD_CN_ID[:17] + ("X" if BAD_CN_ID[17] != "X" else "0")
assert not cn_id_ok(BAD_CN_ID)

# Truncated token prefix: long enough to be suspicious, short of the rule.
TRUNCATED_TOKEN = "sk_test_AB12CD34"  # 8 body chars after second underscore
