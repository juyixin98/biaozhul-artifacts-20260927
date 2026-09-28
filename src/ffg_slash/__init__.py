"""ffg_slash: local FFG double-vote / surround-vote evidence detector.

Layered layout:
    encoding/crypto -> models -> registry -> state (pure finality kernel)
                              -> evidence -> detector (ingest pipeline)
                              -> storage (SQLite index) -> replay / api
"""

__version__ = "1.0.0"
EVIDENCE_VERSION = 1
SCHEMA_VERSION = 1
