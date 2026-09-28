"""Rule and evidence parsing.

A :class:`Rule` is either:

* ``pattern`` — a regex over the text, gated by an *evidence validator*
  (e.g. Luhn checksum for bank cards, checksum for Chinese resident ID).
  A candidate that matches the regex but fails the validator is evidence of
  an *uncertain* fragment rather than a definite secret.
* ``field`` — a structured ``key=value`` / ``"key":"value"`` match that also
  recognizes backslash-escaped JSON (``\\"email\\":\\"a@b.c\\"`` as produced
  when a JSON document is embedded inside another JSON log line).

Rules are ordered by explicit ``priority`` (higher wins); ties are broken
deterministically so results never depend on dict iteration order.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Evidence validators
# ---------------------------------------------------------------------------


def luhn_ok(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if not digits:
        return False
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


# Weighted mod-11 checksum used by the (synthetic, 18-digit) Chinese resident
# identity number. We only validate the checksum; area/birthday digits are
# treated as opaque because every value here is fabricated.
_CN_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_CN_ID_CHECK = "10X98765432"


def cn_id_ok(value: str) -> bool:
    v = value.strip().upper()
    if not re.fullmatch(r"\d{17}[\dX]", v):
        return False
    total = sum(int(v[i]) * _CN_ID_WEIGHTS[i] for i in range(17))
    return _CN_ID_CHECK[total % 11] == v[17]


def always_ok(_: str) -> bool:
    return True


Validator = Callable[[str], bool]


@dataclass(frozen=True)
class RawMatch:
    """A raw regex candidate.

    ``start``/``end`` bound the text to replace. For field rules that is the
    value span only (key and surrounding quotes are preserved); ``whole``
    bounds the whole match and is used for evidence extraction.
    """

    start: int
    end: int
    whole_start: int
    whole_end: int
    text: str


class RuleKind(str, Enum):
    PATTERN = "pattern"
    FIELD = "field"


@dataclass(frozen=True)
class Rule:
    rule_id: str
    kind: RuleKind
    label: str
    pattern: str
    priority: int
    max_len: int
    validator: Validator = always_ok
    validator_name: str = "none"
    # When the regex matches but the validator rejects, report the candidate
    # as an uncertain fragment instead of silently dropping it.
    uncertainty_on_fail: bool = False
    _compiled: re.Pattern[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            compiled = re.compile(self.pattern)
        except re.error as exc:
            raise RuleCompileError(
                f"invalid regex for rule {self.rule_id!r}: {exc}"
            ) from exc
        object.__setattr__(self, "_compiled", compiled)

    def finditer(self, text: str) -> list[RawMatch]:
        """Return raw regex candidates (value spans for field rules)."""
        out: list[RawMatch] = []
        for m in self._compiled.finditer(text):
            if self.kind is RuleKind.FIELD:
                try:
                    vs, ve = m.start("value"), m.end("value")
                except IndexError:
                    vs, ve = m.start(), m.end()
                value = text[vs:ve]
                out.append(RawMatch(vs, ve, m.start(), m.end(), value))
            else:
                out.append(
                    RawMatch(m.start(), m.end(), m.start(), m.end(), m.group(0))
                )
        return out

    def redaction_token(self) -> str:
        return f"[REDACTED:{self.label}]"


class RuleCompileError(ValueError):
    """Category ``rule_compile_error`` — a rule definition is invalid."""


# ---------------------------------------------------------------------------
# Built-in rule definitions (synthetic secret formats only)
# ---------------------------------------------------------------------------

_FIELD_KEYS = r"email|e_mail|mail|password|passwd|pwd|secret|token|api_key|apikey|authorization|auth|card_no|pan|id_number|idcard|id_card|mobile|phone"

# Quoted:  "email":"a@b.c"   and escaped  \"email\":\"a@b.c\"
# Bare:    email=a@b.c        token: xyz        password xyz
_FIELD_QUOTED = (
    r'(?P<qopen>\\?")(?P<key>'
    + _FIELD_KEYS
    + r')(?P<qclose>\\?")\s*(?::|=)\s*'
    r'(?P<vopen>\\?")(?P<value>[^"\\\n]{1,255})(?P<vclose>\\?")'
)
_FIELD_BARE = (
    r"(?<![A-Za-z0-9_])(?P<key>"
    + _FIELD_KEYS
    + r")\s*(?::|=)\s*"
    r"(?P<value>[A-Za-z0-9._@\-]{2,255}?)(?=[\s;,]|$)"
)

_API_TOKEN = r"\b(?:sk|pk|tok|api|key)_(?:live|test|syn)?_?[A-Za-z0-9]{12,48}\b"
_SECRET_HEX = r"\b[A-Fa-f0-9]{32,128}\b"
_BANK_CARD = r"(?<![0-9])(?:62[0-9]{14,17}|4[0-9]{15,18}|5[1-5][0-9]{14,17})(?![0-9])"
_CN_MOBILE = r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])"
_EMAIL = r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,64}\.[A-Za-z]{2,24}"
_CN_ID = r"(?<![0-9])\d{17}[\dXx](?![0-9A-Za-z])"
_BEARER = r"\bBearer\s+[A-Za-z0-9._\-]{12,255}"
_JWTISH = r"\beyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{2,}\b"


def _build_standard_rules() -> list[Rule]:
    return [
        Rule(
            rule_id="std.field.quoted",
            kind=RuleKind.FIELD,
            label="field",
            pattern=_FIELD_QUOTED,
            priority=40,
            max_len=600,
        ),
        Rule(
            rule_id="std.field.bare",
            kind=RuleKind.FIELD,
            label="field",
            pattern=_FIELD_BARE,
            priority=38,
            max_len=300,
        ),
        Rule(
            rule_id="std.pattern.api_token",
            kind=RuleKind.PATTERN,
            label="api_token",
            pattern=_API_TOKEN,
            priority=30,
            max_len=64,
        ),
        Rule(
            rule_id="std.pattern.secret_hex",
            kind=RuleKind.PATTERN,
            label="secret",
            pattern=_SECRET_HEX,
            priority=26,
            max_len=128,
        ),
        Rule(
            rule_id="std.pattern.bank_card",
            kind=RuleKind.PATTERN,
            label="bank_card",
            pattern=_BANK_CARD,
            priority=24,
            max_len=20,
            validator=luhn_ok,
            validator_name="luhn",
            uncertainty_on_fail=True,
        ),
        Rule(
            rule_id="std.pattern.cn_mobile",
            kind=RuleKind.PATTERN,
            label="cn_mobile",
            pattern=_CN_MOBILE,
            priority=22,
            max_len=11,
        ),
        Rule(
            rule_id="std.pattern.email",
            kind=RuleKind.PATTERN,
            label="email",
            pattern=_EMAIL,
            max_len=120,
            priority=20,
        ),
    ]


def _build_strict_rules() -> list[Rule]:
    rules = _build_standard_rules()
    rules += [
        Rule(
            rule_id="strict.pattern.cn_id",
            kind=RuleKind.PATTERN,
            label="cn_id_card",
            pattern=_CN_ID,
            priority=23,
            max_len=18,
            validator=cn_id_ok,
            validator_name="cn_id_checksum",
            uncertainty_on_fail=True,
        ),
        Rule(
            rule_id="strict.pattern.bearer",
            kind=RuleKind.PATTERN,
            label="bearer_token",
            pattern=_BEARER,
            priority=29,
            max_len=264,
        ),
        Rule(
            rule_id="strict.pattern.jwt",
            kind=RuleKind.PATTERN,
            label="jwt",
            pattern=_JWTISH,
            max_len=2048,
            priority=28,
        ),
    ]
    return rules


@dataclass(frozen=True)
class RuleSet:
    profile: str
    version: str
    rules: tuple[Rule, ...]
    fingerprint: str

    @property
    def max_len(self) -> int:
        return max(r.max_len for r in self.rules)

    def rule(self, rule_id: str) -> Optional[Rule]:
        for r in self.rules:
            if r.rule_id == rule_id:
                return r
        return None


def _fingerprint(profile: str, rules: list[Rule]) -> str:
    spec = [
        {
            "rule_id": r.rule_id,
            "kind": r.kind.value,
            "pattern": r.pattern,
            "priority": r.priority,
            "validator": r.validator_name,
        }
        for r in rules
    ]
    digest = hashlib.sha256(
        json.dumps(
            {"profile": profile, "rules": spec}, sort_keys=True
        ).encode()
    ).hexdigest()
    return digest[:16]


def build_ruleset(profile: str) -> RuleSet:
    if profile == "standard":
        rules = _build_standard_rules()
        version = "standard-v1"
    elif profile == "strict":
        rules = _build_strict_rules()
        version = "strict-v1"
    else:
        raise UnknownProfileError(f"unknown rule profile: {profile!r}")
    rules = tuple(
        sorted(rules, key=lambda r: (-r.priority, r.rule_id))
    )
    return RuleSet(
        profile=profile,
        version=version,
        rules=rules,
        fingerprint=_fingerprint(profile, list(rules)),
    )


class UnknownProfileError(ValueError):
    """Category ``unknown_profile``."""
