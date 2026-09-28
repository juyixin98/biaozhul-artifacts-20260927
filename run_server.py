"""本地服务入口：python run_server.py

环境变量：
  AUDIT_DB           SQLite 路径（默认 data/audit.sqlite3）
  AUDIT_MASTER_KEY   32 字节主密钥（不传时仅用本地开发默认值，禁止用于生产）
  AUDIT_HOST/AUDIT_PORT  监听地址（默认 127.0.0.1:8000）
"""
from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.api:app",
        host=os.environ.get("AUDIT_HOST", "127.0.0.1"),
        port=int(os.environ.get("AUDIT_PORT", "8000")),
        reload=False,
    )
