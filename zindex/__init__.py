"""Morton (Z-order) clustered columnar range-query backend.

Modules:
    encoding   - fixed-width signed Morton code + conservative interval decomposition
    chunkstore - local columnar PyArrow IPC chunk format
    catalog    - SQLite metadata / transactions
    kernel     - execution engine (prune, pushdown, exact residual filter)
    ingest     - schema management, ingestion, rewrite/compaction
    fixtures   - deterministic synthetic data generator
    api        - FastAPI validation interface
"""

__version__ = "1.0.0"
ENGINE_VERSION = "morton-z-v1"
