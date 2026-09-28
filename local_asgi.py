"""Local ASGI entrypoint: ``uvicorn local_asgi:app``.

State directory and config path come from environment variables so no secrets
or paths are hard-coded.
"""

import os

from secretscan.api import create_app

STATE_DIR = os.environ.get("SECRETSCAN_STATE", "/tmp/secretscan_state")
CONFIG = os.environ.get("SECRETSCAN_CONFIG", "config/rules.yaml")

app = create_app(STATE_DIR, CONFIG)
