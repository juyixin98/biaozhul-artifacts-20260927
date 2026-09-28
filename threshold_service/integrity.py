"""独立完整性层（原始 Shamir 不提供的东西）。

两层独立校验，均基于 `cryptography` 库的 HMAC-SHA256（恒定时间比较）：

1. 份额标签（share tag）：信封创建时对规范内容计算 HMAC，
   与集合身份/门限/字段参数绑定。恢复时重算比对，
   用于拒绝被篡改或跨集合搬运的份额。
2. 秘密承诺（secret commitment）：分片时对原秘密计算带集合身份的
   HMAC 并保存；重组出候选秘密后核对，给出"恢复是否真的成功"的依据。

信任边界（必须明确）：
- HMAC 是**对称**认证：持有主密钥的服务端才能签发/校验。
  它证明"份额由本服务（或任何持有主密钥者）签发且未被篡改"，
  不提供参与者之间的不可否认性，也不能防止服务端自身作恶。
- 若要抵抗服务端/分发者作恶，需要门限签名、可验证秘密分享（VSS，
  如 Feldman/Pedersen 承诺）或公钥 witness，**超出本服务范围**。
- 承诺是 HMAC 而非公开哈希，避免低熵测试秘密被离线字典枚举。
"""
from __future__ import annotations

import hmac

from cryptography.hazmat.primitives import hashes, hmac as crypto_hmac

SHARE_TAG_DOMAIN = b"tss-share-tag/v1/"
COMMIT_DOMAIN = b"tss-secret-commitment/v1/"
COMMIT_TAG_LEN = 16  # 截断到 128 bit，足够区分错误恢复且节省存储


def _hmac(key: bytes, message: bytes) -> bytes:
    signer = crypto_hmac.HMAC(key, hashes.SHA256())
    signer.update(message)
    return signer.finalize()


def tag_share(master_key: bytes, content: bytes) -> bytes:
    """对份额信封规范内容打标签。"""
    return _hmac(master_key, SHARE_TAG_DOMAIN + content)


def verify_share_tag(master_key: bytes, content: bytes, tag: bytes) -> bool:
    """恒定时间校验份额标签。"""
    expected = tag_share(master_key, content)
    return hmac.compare_digest(expected, tag)


def secret_commitment(master_key: bytes, set_id: str, secret: bytes) -> str:
    """生成秘密承诺（hex，128 bit 截断）。"""
    digest = _hmac(master_key, COMMIT_DOMAIN + set_id.encode("utf-8") + b"|" + secret)
    return digest[:COMMIT_TAG_LEN].hex()


def verify_commitment(master_key: bytes, set_id: str, secret: bytes, commit: str) -> bool:
    """恒定时间核对候选秘密与承诺。"""
    return hmac.compare_digest(secret_commitment(master_key, set_id, secret), commit)
