"""Baseline exemptions bound to content fingerprints.

A baseline is an explicit, reviewed list of fingerprints whose findings are
accepted up front (legacy demo keys, deliberately seeded test secrets). The
exemption binds the **exact secret content** via its HMAC fingerprint — renaming
or moving the file does not change the exemption, while editing one character of
the secret produces a new fingerprint and a fresh candidate. This deliberately
avoids filename-only allowlists.

Baseline file format (TOML)::

    [meta]
    version = "1"
    pepper_id = "sha256(pepper)[:12]"   # must match the running pepper

    [[exemptions]]
    fingerprint = "<hmac-sha256 hex>"
    mask = "ghp_1eAo…Eika"             # human aid only, not used for matching
    rule_id = "github-classic-pat"     # optional narrowing
    note = "rotated legacy demo key"

``tools/make_baseline.py`` generates fingerprints with an independent
stdlib-only implementation (it does not import the scan core), so reference
answers are not produced by the code under test.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .security import Fingerprinter


class BaselineError(ValueError):
    """A baseline file is malformed or was built with a different pepper."""


@dataclass(frozen=True)
class BaselineEntry:
    fingerprint: str
    mask: str
    rule_id: str | None
    note: str


@dataclass(frozen=True)
class Baseline:
    path: Path
    pepper_id: str
    entries: tuple[BaselineEntry, ...]

    def lookup(self, fingerprint: str, rule_id: str) -> BaselineEntry | None:
        """Return the matching exemption, or None. Match is on content only.

        An entry without ``rule_id`` exempts the fingerprint under every rule;
        an entry with ``rule_id`` only exempts that rule.
        """
        for entry in self.entries:
            if entry.fingerprint != fingerprint:
                continue
            if entry.rule_id is None or entry.rule_id == rule_id:
                return entry
        return None


def load_baseline(path: str | Path, fingerprinter: Fingerprinter) -> Baseline:
    """Parse and validate a baseline against the configured pepper."""
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BaselineError(f"baseline not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise BaselineError(f"{path}: invalid TOML: {exc}") from exc
    meta = data.get("meta")
    if not meta or "pepper_id" not in meta:
        raise BaselineError(f"{path}: [meta] pepper_id is required")
    pepper_id = str(meta["pepper_id"])
    if pepper_id != fingerprinter.pepper_id:
        raise BaselineError(
            f"{path}: baseline pepper_id {pepper_id!r} does not match the "
            f"configured pepper {fingerprinter.pepper_id!r}; refusing to apply "
            f"a baseline whose fingerprints cannot be reproduced")
    raw_entries = data.get("exemptions", [])
    entries: list[BaselineEntry] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_entries):
        fp = raw.get("fingerprint")
        if not fp or not isinstance(fp, str):
            raise BaselineError(f"{path}: exemption #{i} needs a fingerprint")
        if fp in seen:
            raise BaselineError(f"{path}: duplicate fingerprint at exemption #{i}")
        seen.add(fp)
        rule_id = raw.get("rule_id")
        entries.append(BaselineEntry(
            fingerprint=fp,
            mask=str(raw.get("mask", "")),
            rule_id=str(rule_id) if rule_id else None,
            note=str(raw.get("note", ""))))
    return Baseline(path=path, pepper_id=pepper_id, entries=tuple(entries))
