"""logsafe: field/pattern based log redaction service.

Module layout (each layer does real work, no demo stubs):

- app.rules        rule/evidence parsing, rule-set profiles
- app.kernel       security core: spans, overlap resolution, streaming state
- app.crypto       authenticated encryption for audit originals
- app.audit        encrypted SQLite audit store + service layer
- app.api          FastAPI routes, error categories, request identity
"""

__version__ = "1.0.0"
