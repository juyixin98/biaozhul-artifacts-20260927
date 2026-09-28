"""Security kernel — the only place raw secret values are handled.

Hard guarantees enforced here:

* Raw values live only inside :class:`Secret` objects during a scan. They are
  never written to the database, never returned by the API and never handed to
  a logger.
* Persistence stores a **mask** (``ghp_1eAo…Eika`` style) plus a keyed
  **fingerprint** (HMAC-SHA256 with the configured pepper). Fingerprints let us
  re-identify a secret across scans (moved file / deleted candidate) without
  storing it and cannot be reversed without the pepper.
* :class:`RedactingFilter` scrubs any raw value that accidentally reaches the
  logging subsystem, and :class:`Secret` renders as its mask even in f-strings.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass

from cryptography.hazmat.primitives import constant_time

# Bounds on the evidence preview stored next to a candidate (masked anyway).
PREVIEW_RADIUS = 12
PREVIEW_MAX = 48


def mask_value(value: str) -> str:
    """Return a non-reversible human-recognisable mask of ``value``.

    Short values collapse entirely (the ends alone would give most of a short
    password away). Long values keep 4 chars at each end. PEM blocks keep
    their BEGIN/END header lines (public structure) and replace the key body
    wholesale.
    """
    n = len(value)
    if n == 0:
        return ""
    if "PRIVATE KEY-----" in value:
        lines = value.splitlines()
        out: list[str] = []
        for line in lines:
            if line.startswith("-----BEGIN") or line.startswith("-----END"):
                out.append(line)
            else:
                out.append("<private-key-body-redacted:{} chars>".format(
                    len(line.strip())))
        return "\n".join(out)
    if n <= 12:
        return "*" * n
    keep = min(4, (n - 4) // 2)
    middle = n - 2 * keep
    return f"{value[:keep]}{'*' * middle}{value[-keep:]}"


def mask_evidence(secret: str, evidence: str) -> str:
    """Mask the secret inside a surrounding evidence snippet."""
    masked_secret = mask_value(secret)
    # ``count`` stays 1: evidence snippets are short, single-occurrence windows.
    return evidence.replace(secret, masked_secret, 1)


def build_preview(text: str, start: int, end: int) -> str:
    """Extract a short single-line context window around ``[start, end)``."""
    lo = max(0, start - PREVIEW_RADIUS)
    hi = min(len(text), end + PREVIEW_RADIUS)
    snippet = text[lo:hi]
    snippet = snippet.replace("\n", "␊").replace("\r", "␍").replace("\t", "␉")
    if len(snippet) > PREVIEW_MAX:
        snippet = snippet[: PREVIEW_MAX - 1] + "…"
    return snippet


def redacted_preview(text: str, start: int, end: int) -> str:
    """Context window around ``[start, end)`` with the span replaced by '*'.

    Unlike :func:`mask_evidence`, this works for multi-line matches whose
    full value cannot appear in a short window (PEM blocks): only the bytes
    actually visible in the window are masked, by position.
    """
    lo = max(0, start - PREVIEW_RADIUS)
    hi = min(len(text), end + PREVIEW_RADIUS)
    overlap_lo = max(start, lo)
    overlap_hi = min(end, hi)
    chars = list(text[lo:hi])
    for i in range(overlap_lo - lo, overlap_hi - lo):
        chars[i] = "␊" if chars[i] == "\n" else (
            "␍" if chars[i] == "\r" else (
                "␉" if chars[i] == "\t" else "*"))
    # Render separators outside the secret too, so every window stays 1 line.
    for i, ch in enumerate(chars):
        if ch == "\n":
            chars[i] = "␊"
        elif ch == "\r":
            chars[i] = "␍"
        elif ch == "\t":
            chars[i] = "␉"
    snippet = "".join(chars)
    if len(snippet) > PREVIEW_MAX:
        snippet = snippet[: PREVIEW_MAX - 1] + "…"
    return snippet


@dataclass(frozen=True)
class Secret:
    """A raw candidate value wrapped so it cannot leak by accident.

    ``str(secret)`` / f-strings / repr all yield the **mask**, never the value.
    The raw value is only reachable through the explicit :meth:`expose` call,
    and callers must not persist or log what it returns.
    """

    _value: str

    @property
    def length(self) -> int:
        return len(self._value)

    @property
    def mask(self) -> str:
        return mask_value(self._value)

    def expose(self) -> str:
        """Intentionally return the raw value (scanner-internal use only)."""
        return self._value

    def __str__(self) -> str:
        return self.mask

    def __repr__(self) -> str:
        return f"Secret({self.mask!r})"


class Fingerprinter:
    """Keyed content fingerprints binding baseline exemptions to content."""

    def __init__(self, pepper: str):
        if not pepper:
            raise ValueError("fingerprint pepper must not be empty")
        self._key = pepper.encode("utf-8")
        self.pepper_id = hashlib.sha256(self._key).hexdigest()[:12]

    def fingerprint(self, value: str) -> str:
        """HMAC-SHA256 hex digest of the exact secret bytes."""
        return hmac.new(self._key, value.encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def matches(self, value: str, fingerprint: str) -> bool:
        """Constant-time comparison against a stored fingerprint."""
        return constant_time.bytes_eq(
            self.fingerprint(value).encode("ascii"),
            fingerprint.encode("ascii"))


def file_sha256(data: bytes) -> str:
    """Content hash of a whole file — the identity used for move detection."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Log redaction
# ---------------------------------------------------------------------------

class RedactingFilter(logging.Filter):
    """Logging filter that replaces registered raw secret strings with masks.

    Scanners register the values found in the current request; the filter is
    belt-and-braces protection for log statements that never *should* contain a
    secret. Values are matched longest-first so a substring of another secret
    cannot leave residue.
    """

    def __init__(self) -> None:
        super().__init__()
        self._values: set[str] = set()

    def register(self, values) -> None:
        self._values.update(v for v in values if len(v) >= 8)

    def clear(self) -> None:
        self._values.clear()

    def filter(self, record: logging.LogRecord) -> bool:
        if self._values and record.args:
            # Format first, then scrub the rendered message only.
            rendered = record.getMessage()
            for value in sorted(self._values, key=len, reverse=True):
                if value in rendered:
                    rendered = rendered.replace(value, mask_value(value))
            record.msg = rendered
            record.args = ()
        return True
