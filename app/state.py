"""应用状态装配：配置 → 加密盒 → 存储 → 审计 → 服务。

集中构造，便于测试以独立临时目录实例化整套组件（状态隔离）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, load_settings
from app.security.audit import AuditLog
from app.security.crypto import build_crypto_box, derive_signing_key
from app.service import RiskService
from app.store.db import Store


@dataclass
class AppState:
    settings: Settings
    service: RiskService
    store: Store
    audit: AuditLog

    def audit_verify(self) -> dict:
        return self.audit.verify_chain()


def build_state(settings: Settings | None = None) -> AppState:
    settings = settings or load_settings()
    crypto = build_crypto_box(settings.encryption_key, settings.allow_ephemeral_key)

    if settings.audit_signing_key:
        from app.security.crypto import normalize_key

        signing_key = derive_signing_key(normalize_key(settings.audit_signing_key))
    elif settings.encryption_key:
        signing_key = derive_signing_key(settings.encryption_key.encode("utf-8"))
    else:
        # 临时密钥模式：审计链密钥也一次性（重放旧日志会报断链，符合隔离预期）
        import base64
        import os

        signing_key = derive_signing_key(base64.urlsafe_b64encode(os.urandom(32)))

    store = Store(settings.database_path, crypto)
    audit = AuditLog(settings.audit_log_path, signing_key)
    service = RiskService(settings, store, crypto, audit)
    return AppState(settings=settings, service=service, store=store, audit=audit)
