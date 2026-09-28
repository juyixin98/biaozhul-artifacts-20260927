"""本地开发入口：python -m lake_txn.main"""

from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run(
        "lake_txn.api:create_app",
        factory=True,
        host="127.0.0.1",
        port=8000,
        reload=False,
    )


if __name__ == "__main__":
    main()
