"""Threshold secret-sharing service (Shamir over GF(p)) package.

Module responsibilities (kept deliberately separate):

* :mod:`app.parsing`  - rule/evidence parsing: request schemas and the rule
  engine that binds a share to its collection identity / threshold / field.
* :mod:`app.core`     - the security kernel: finite-field Shamir, the
  *independent* integrity check, share envelopes and orchestration.
* :mod:`app.state`    - state isolation: SQLite-backed persistence with one
  row per collection and strict cross-collection separation.
* :mod:`app.audit`    - the audit interface: fingerprint-only event logging.
* :mod:`app.api`      - FastAPI wiring (thin; no security decisions live here).
"""

__version__ = "1.0.0"
