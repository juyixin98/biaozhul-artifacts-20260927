"""Log redaction.

Defense in depth: even if some code path accidentally formats a candidate
value into a log message, the redactor masks anything that looks like one of
the tracked structural patterns. Entropy-only tokens are *not* masked here,
because matching arbitrary high-entropy substrings in free-form log output
would corrupt legitimate data; candidate values are already never logged by
application code (covered by a test that scans emitted log lines).
"""

from __future__ import annotations

import re

from .mask import mask_value

_BUILTIN_PATTERNS = [
    rb"AKIA[0-9A-Z]{16}",
    rb"gh[pousr]_[A-Za-z0-9]{36}",
    rb"(?i:bearer)\s+([A-Za-z0-9\-\._~+/]{12,})",
    rb"-----BEGIN PGP PRIVATE KEY BLOCK-----[\x20-\x7e\r\n]*?-----END PGP PRIVATE KEY BLOCK-----",
]


class Redactor:
    def __init__(self, extra_patterns: tuple[str, ...] | list[str] = ()):
        pats = list(_BUILTIN_PATTERNS)
        for p in extra_patterns:
            pats.append(p.encode("utf-8"))
        self._patterns = [re.compile(p) for p in pats]

    def redact(self, text: str) -> str:
        data = text.encode("utf-8", errors="replace")
        for rx in self._patterns:
            def repl(m: re.Match) -> bytes:
                # For bearer keep the scheme word, mask only the credential.
                if m.groups():
                    whole = m.group(0)
                    cred = m.group(1)
                    masked = mask_value(cred)
                    return whole.replace(cred, masked.encode("utf-8"), 1)
                return mask_value(m.group(0)).encode("utf-8")

            data = rx.sub(repl, data)
        return data.decode("utf-8", errors="replace")
