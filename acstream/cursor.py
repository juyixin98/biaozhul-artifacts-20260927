"""分页游标：keyset 定位 + HMAC 防篡改。

游标签入会话 id，保证不能跨会话复用；用 HMAC-SHA256 签名，密钥只在服务端。
游标不依赖当前查询行是否仍存在（它编码的是排序键），因此“可恢复”。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from .errors import ApiError, ErrorCode


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def encode_cursor(
    secret: bytes,
    *,
    session_id: str,
    end: int,
    start: int,
    pattern_id: str,
    seq: int,
) -> str:
    body = json.dumps(
        {
            "sid": session_id,
            "end": end,
            "start": start,
            "pid": pattern_id,
            "seq": seq,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    mac = hmac.new(secret, body, hashlib.sha256).digest()
    return _b64e(body) + "." + _b64e(mac)


def decode_cursor(secret: bytes, token: str, *, session_id: str) -> dict:
    try:
        body_b64, mac_b64 = token.split(".", 1)
        body = _b64d(body_b64)
        mac = _b64d(mac_b64)
    except (ValueError, TypeError) as exc:
        raise ApiError(
            ErrorCode.CURSOR_INVALID, 400, "分页游标格式非法", outcome="reject"
        ) from exc

    expected = hmac.new(secret, body, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise ApiError(
            ErrorCode.CURSOR_INVALID,
            400,
            "分页游标签名校验失败，可能被篡改或使用了不同密钥",
            outcome="reject",
        )

    try:
        key = json.loads(body.decode("utf-8"))
        assert isinstance(key["end"], int)
        assert isinstance(key["start"], int)
        assert isinstance(key["pid"], str)
        assert isinstance(key["seq"], int)
    except (ValueError, KeyError, UnicodeDecodeError, AssertionError) as exc:
        raise ApiError(
            ErrorCode.CURSOR_INVALID, 400, "分页游标内容无法解析", outcome="reject"
        ) from exc

    if key["sid"] != session_id:
        raise ApiError(
            ErrorCode.CURSOR_INVALID,
            400,
            "分页游标属于其他会话，不能跨会话使用",
            outcome="reject",
        )
    return key
