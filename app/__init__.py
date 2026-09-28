"""Local audit batch: field-level commitments and selective disclosure.

Package layout::

    app.core      pure cryptographic kernel (no I/O, no framework)
    app.parsing   rule/evidence parsing and synthetic fixture generation
    app.security  security kernel: algorithm policy, salt policy, redaction
    app.storage   SQLite persistence (private vs public state)
    app.audit     audit event sink + run-correlated logging
    app.api       FastAPI wiring
"""

SERVICE_NAME = "local-audit-commitment-service"
SERVICE_VERSION = "1.0.0"
