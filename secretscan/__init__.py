"""Offline secret-candidate scanner for code repository snapshots.

Package layout (split by responsibility, see docs/BOUNDARIES.md):

- :mod:`secretscan.config`   — rule / evidence parsing (versioned TOML packs)
- :mod:`secretscan.security` — the safety kernel: masking, fingerprints, log
                               redaction (no full secret is ever emitted)
- :mod:`secretscan.entropy`  — Shannon entropy threshold primitive
- :mod:`secretscan.media`    — text vs binary classification
- :mod:`secretscan.scanner`  — pure scan engine: snapshot -> candidates
- :mod:`secretscan.baseline` — content-fingerprint-bound baseline exemptions
- :mod:`secretscan.state`    — isolated per-workspace SQLite state
- :mod:`secretscan.audit`    — structured audit log + request identity
- :mod:`secretscan.service`  — orchestration: scan + diff + lifecycle states
- :mod:`secretscan.api`      — local read-only audit HTTP interface (FastAPI)
- :mod:`secretscan.cli`      — command line front end

Nothing in this package performs network calls. Credentials are never
validated against any external service; every hit is a *candidate*, never a
confirmed leak.
"""

__version__ = "1.0.0"
