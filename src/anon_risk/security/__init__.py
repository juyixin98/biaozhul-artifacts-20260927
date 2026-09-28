"""安全原语（cryptography）：主密钥装载、每次运行独立派生子密钥、加密与 HMAC 脱敏。

设计要点
--------
- 主密钥为标准 Fernet 密钥（urlsafe-base64 编码的 32 字节）。未配置时
  仅在 ``allow_ephemeral_key`` 下生成进程内临时密钥，并显式标记
  ``ephemeral=True``（重启后旧运行不可解密，由配置/健康接口暴露）。
- 每个运行 ID 通过 HKDF-SHA256 派生子密钥，实现状态隔离：拿到一个运行的
  数据文件也无法在不知道主密钥时解密，且不同运行密钥相互独立。
- 等价类原始键（含原始 QI 值）在任何出站内容中只以 HMAC-SHA256 截断指纹
  出现，用于“同一类”的关联而不泄露取值。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..errors import ErrorCode, RiskError
from ..logging_setup import get_logger

log = get_logger("crypto")


class KeyManager:
    """持有主密钥并派生每次运行的加密器。"""

    def __init__(self, master_key: bytes, ephemeral: bool = False):
        self._master = master_key
        self.ephemeral = ephemeral
        # 主密钥本身也作为 Fernet 可用（保护审计索引中的小密文）
        self._master_fernet = Fernet(master_key)

    @classmethod
    def from_settings(cls, settings) -> "KeyManager":
        import os as _os

        raw = _os.environ.get(settings.security.master_key_env)
        if raw:
            key = raw.strip().encode("ascii")
            try:
                Fernet(key)  # 校验格式
            except (ValueError, TypeError) as exc:
                raise RiskError(
                    "主密钥不是合法的 Fernet 密钥",
                    code=ErrorCode.STATE_ERROR,
                    http_status=500,
                ) from exc
            log.info("已从环境变量装载主密钥",
                     extra={"event": {"key_source": "env", "ephemeral": False}})
            return cls(key, ephemeral=False)

        if settings.security.allow_ephemeral_key:
            key = Fernet.generate_key()
            log.warning(
                "未配置主密钥，已生成进程内临时密钥；重启后历史运行将无法解密",
                extra={"event": {"key_source": "ephemeral", "ephemeral": True}},
            )
            return cls(key, ephemeral=True)

        raise RiskError(
            "未配置主密钥且未允许临时密钥",
            code=ErrorCode.STATE_ERROR,
            http_status=500,
        )

    def fernet_for_run(self, run_id: str, salt: str) -> Fernet:
        """HKDF 派生运行子密钥，包成 Fernet。"""
        info = f"run:{run_id}:{salt}".encode("utf-8")
        derived = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt.encode("utf-8"),
            info=info,
        ).derive(self._master)
        return Fernet(base64.urlsafe_b64encode(derived))

    def master_fernet(self) -> Fernet:
        return self._master_fernet

    # HMAC 用的独立 32 字节（从主密钥与固定用途标签派生），用于类指纹
    def hmac_for_run(self, run_id: str) -> "_RunHmac":
        derived = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"anon-risk-class-hmac-v1",
            info=f"hmac:{run_id}".encode("utf-8"),
        ).derive(self._master)
        return _RunHmac(derived)


class _RunHmac:
    def __init__(self, key: bytes):
        self._key = key

    def fingerprint(self, parts: list[str]) -> str:
        """对等价类键做键控哈希，返回十六进制截断指纹（只用于关联，不可逆）。"""
        msg = b"\x1f".join(p.encode("utf-8") for p in parts)
        digest = hmac.new(self._key, msg, hashlib.sha256).hexdigest()
        return digest[:32]


def encrypt_rowset(fernet: Fernet, payload: bytes) -> bytes:
    return fernet.encrypt(payload)


def decrypt_rowset(fernet: Fernet, token: bytes) -> bytes:
    try:
        return fernet.decrypt(token)
    except InvalidToken as exc:
        raise RiskError(
            "运行数据解密失败：密钥不匹配或数据已损坏",
            code=ErrorCode.RUN_FORBIDDEN,
            http_status=403,
        ) from exc


def new_run_id() -> str:
    """随机运行身份（128 位熵）。"""
    return base64.urlsafe_b64encode(os.urandom(18)).decode("ascii").rstrip("=")


def new_access_token() -> str:
    """访问令牌（256 位熵）；只在创建响应里出现一次。"""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
