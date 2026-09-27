"""直接运行：python -m mp4timeline.api 启动服务（默认 127.0.0.1:8321）。"""

import uvicorn

from ..config import load_settings
from .app import create_app

if __name__ == "__main__":
    app = create_app(load_settings())
    uvicorn.run(app, host="127.0.0.1", port=8321)
