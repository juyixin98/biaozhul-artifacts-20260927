"""Local dev entry point: python run_server.py"""
from __future__ import annotations

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.api:app",
        host="127.0.0.1",
        port=8088,
        reload=False,
        log_level="warning",
    )
