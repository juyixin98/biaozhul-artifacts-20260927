"""archguard — local archive inspection and controlled extraction service.

Layered package layout::

    errors      classification of every non-success outcome (no silent success)
    config      configuration loading and validation
    budget      extraction budget accounting
    archiveio   format detection + uniform reader interface for zip / tar
    paths       canonical target-path graph construction (the security core)
    kernel      orchestration: scan -> plan -> extract -> verify
    isolation   per-run isolated directories
    audit       JSONL hash-chained event log, HMAC signature, signed manifest
    store       SQLite state / audit query interface
    api         FastAPI HTTP surface
"""

__version__ = "1.0.0"
SERVICE_NAME = "archguard"
