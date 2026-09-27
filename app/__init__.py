"""Synthetic EBU R128 loudness measurement backend.

Modules:
- config: environment-driven configuration
- media: PCM/WAV parsing into normalized float samples
- kernel: time/signal core (K-weighting, gating, LRA)
- jobs: SQLite-backed job state
- validation: typed input validation
- api: FastAPI validation/measurement interface
"""

__version__ = "1.0.0"
