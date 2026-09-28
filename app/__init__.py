"""Cache-key and Vary configuration audit backend.

Modules and their contracts:

- ``errors``   : error taxonomy shared by every module (input / state /
  resource / computation). Modules never raise bare exceptions across
  boundaries; everything is an ``AuditError`` subclass.
- ``models``   : immutable data contracts (requests, responses, policy,
  findings) exchanged between modules.
- ``parsing``  : turns untrusted JSON documents (policy, evidence) into
  those contracts. Structural problems -> ``InputError``.
- ``kernel``   : the security kernel. Pure computation, no I/O: derives
  cache keys, analyses Vary, detects collisions. Canonicalisation
  failures -> ``ComputationError``; finding overflow ->
  ``ResourceExhaustedError``.
- ``store``    : SQLite-backed state, isolated per run id. Lifecycle and
  lookup conflicts -> ``StateConflictError``.
- ``service``  : orchestrates parsing + kernel + store, signs reports.
- ``api``      : FastAPI surface mapping the error taxonomy to HTTP.
"""

__version__ = "0.1.0"
