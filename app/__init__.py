"""Non-overlapping replacement planning service.

Package layout (module contracts, see README "Architecture")::

    app.errors        -- failure taxonomy shared by every layer
    app.textspec      -- text normalization, SHA-256 digests, UTF-8 byte maps
    app.engine        -- RE2 wrapper (compile, validate) and the codepoint-aware
                         zero-width-safe scanner
    app.template      -- restricted capture templates ($0..$9 / ${name})
    app.planner       -- rule set model, priority overlap resolution, plan build
    app.storage       -- SQLite version store, plans, applications, diagnostics
    app.api           -- FastAPI HTTP boundary translating errors to status codes
"""

__version__ = "1.0.0"
