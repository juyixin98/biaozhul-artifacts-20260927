"""进程配置。

所有配置均可通过环境变量覆盖；不依赖任何外部账号或在线服务。
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path
    cursor_secret: bytes
    default_page_size: int = 200
    max_page_size: int = 10_000
    # 诊断事件中是否脱敏载荷（默认始终脱敏；关闭时仅保留前 16 字节 hex 预览）
    redact_payloads: bool = True
    host: str = "127.0.0.1"
    port: int = 8080

    @classmethod
    def load(cls) -> "Settings":
        db_path = Path(os.environ.get("AC_DB_PATH", "data/acstream.db"))
        db_path.parent.mkdir(parents=True, exist_ok=True)

        secret_env = os.environ.get("AC_CURSOR_SECRET")
        if secret_env:
            cursor_secret = secret_env.encode("utf-8")
        else:
            # 游标 HMAC 密钥：进程重启后旧游标仍需可验签，故持久化到本地文件（0600）。
            key_file = db_path.parent / "cursor_secret.key"
            if key_file.exists():
                cursor_secret = key_file.read_bytes()
            else:
                cursor_secret = secrets.token_bytes(32)
                key_file.write_bytes(cursor_secret)
                key_file.chmod(0o600)

        return cls(
            db_path=db_path,
            cursor_secret=cursor_secret,
            default_page_size=int(os.environ.get("AC_DEFAULT_PAGE_SIZE", "200")),
            max_page_size=int(os.environ.get("AC_MAX_PAGE_SIZE", "10000")),
            redact_payloads=os.environ.get("AC_REDACT_PAYLOADS", "true").lower()
            not in ("0", "false", "no"),
            host=os.environ.get("AC_HOST", "127.0.0.1"),
            port=int(os.environ.get("AC_PORT", "8080")),
        )
