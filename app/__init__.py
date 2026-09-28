"""Arrow zero-copy view service.

Layered package:
    app/core/     - execution kernels: buffer layouts, views, concat, validity math
    app/adapters/ - format adaptation: raw JSON/IPC bytes <-> pyarrow buffers
    app/validation/ - independent structural checks (validity / offsets / data)
    app/store/    - metadata transactions (SQLite)
    app/api/      - FastAPI verification interface
"""

__version__ = "0.1.0"
