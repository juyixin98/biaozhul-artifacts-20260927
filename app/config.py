"""配置层。

所有运行参数集中在此；路径与密钥均通过环境变量/默认值注入，
不允许在业务代码中散落硬编码配置。

密钥策略
--------
* ``ANON_ENCRYPTION_KEY`` —— Fernet 对称密钥，用于静态数据加密。
  未设置时：
  - 若 ``ANON_ALLOW_EPHEMERAL_KEY=1``（测试/本地合成环境），生成一次性
    进程内密钥（重启后旧密文不可解，状态隔离）；
  - 否则拒绝启动，避免"无密钥也启动"悄悄进入不安全状态。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ANON_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- 路径 ---
    database_path: str = Field(
        default="data/anon.db", description="SQLite 数据库文件（相对路径基于工作目录）"
    )
    audit_log_path: str = Field(
        default="logs/audit.jsonl", description="只追加审计日志（JSONL，带 HMAC 链）"
    )
    app_log_path: str = Field(default="", description="应用结构化日志；空=stderr")

    # --- 密钥 ---
    encryption_key: str = Field(default="", description="Fernet 密钥；空=按 allow_ephemeral_key 处理")
    audit_signing_key: str = Field(default="", description="审计链 HMAC 密钥（可派生）")
    allow_ephemeral_key: bool = Field(default=False, description="允许一次性进程密钥（仅限测试/本地）")

    # --- 服务 ---
    host: str = "127.0.0.1"
    port: int = 8000

    # --- 输入上限（防滥用，合成数据也保持有界） ---
    max_rows: int = 1000
    max_columns: int = 32
    max_qi: int = 8
    max_hierarchy_height: int = 8
    max_classes_returned: int = 500

    @field_validator("max_rows", "max_columns", "max_qi", "max_hierarchy_height")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("limit must be positive")
        return v

    @property
    def database_file(self) -> Path:
        return Path(self.database_path)

    @property
    def audit_log_file(self) -> Path:
        return Path(self.audit_log_path)


def load_settings() -> Settings:
    """加载配置并准备目录。"""
    s = Settings()
    if s.database_path != ":memory:":
        Path(s.database_path).parent.mkdir(parents=True, exist_ok=True)
    Path(s.audit_log_path).parent.mkdir(parents=True, exist_ok=True)
    if s.app_log_path:
        Path(s.app_log_path).parent.mkdir(parents=True, exist_ok=True)
    return s
