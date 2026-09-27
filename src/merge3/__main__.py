"""Allow ``python -m merge3`` style invocation entry point.

Running the package starts the uvicorn server using local configuration
(MERGE3_* environment variables override defaults).
"""

from __future__ import annotations

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "merge3.api:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
