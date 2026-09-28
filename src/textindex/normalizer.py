"""Text canonicalization (NFC / NFD / NFKC / NFKD / none).

The canonical form is what gets indexed and stored; all positions — byte,
codepoint and grapheme — refer to the canonical text.  Normalization is
explicit: callers pick the form when creating a document and it is recorded
in document metadata, so "same bytes, different form" can never silently
happen across versions.

Only NFC and "none" are required by the service flow, but the full set of
standard forms is accepted and passed through :func:`unicodedata.normalize`.
"""

from __future__ import annotations

import unicodedata

from . import encoding
from .errors import UnsupportedNormalization

SUPPORTED_FORMS = ("NFC", "NFD", "NFKC", "NFKD", "NONE")
DEFAULT_FORM = "NFC"


def canonicalize(text: str, form: str = DEFAULT_FORM) -> str:
    """Return the canonical form of ``text``.

    Surrogates are checked before normalization (``normalize`` itself leaves
    lone surrogates untouched, and we never want them in canonical text).
    """
    form = _validate_form(form)
    encoding.ensure_scalar_value(text)
    if form == "NONE":
        return text
    return unicodedata.normalize(form, text)


def _validate_form(form: str) -> str:
    normalized = form.upper()
    if normalized not in SUPPORTED_FORMS:
        raise UnsupportedNormalization(form, list(SUPPORTED_FORMS))
    return normalized
