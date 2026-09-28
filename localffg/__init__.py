"""localffg — local consensus double/surround-vote evidence detector.

Layers (see README.md):
    encoding/crypto  -> protocol canonicalization + Ed25519
    models / epochs  -> vote model, validator set epoch snapshots
    kernel           -> chain voting state machine + conflict classification
    storage          -> SQLite-backed indexed journal
    replay / checker -> deterministic offline replay, independent re-verification
    service / api     -> FastAPI wiring
"""

__version__ = "1.0.0"
PROTOCOL_VERSION = "1.0"
