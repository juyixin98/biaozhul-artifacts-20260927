"""Local MPEG-TS analysis backend.

Package layout:

* ``app.config``       -- environment-driven configuration
* ``app.core``         -- media parsing, continuity/PSI/PES/timing kernels, diagnostics
* ``app.jobs``         -- SQLite-backed asynchronous job state
* ``app.api``          -- FastAPI validation / job interfaces
"""

__version__ = "1.0.0"
