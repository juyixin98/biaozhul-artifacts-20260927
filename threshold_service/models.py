"""领域模型与份额信封（规则/证据的可解析表示）。

份额信封是恢复接口提交的"证据"：
- 字段参数、门限、集合身份与份额值全部参与认证标签计算（canonical JSON）；
- 信封同时携带一个非空标签，服务端用主密钥重算并比对（恒定时间比较）。

信封本身是公开可传输的结构；安全性来自 HMAC 标签，而非"不看它"。
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass

from .gf import FieldParams

ENVELOPE_VERSION = 1


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


@dataclass(frozen=True)
class ShareEnvelope:
    """绑定集合身份、门限、字段参数的单份份额。"""

    version: int
    set_id: str
    x: int
    y: str  # base64 的份额字节
    threshold: int
    field: dict  # {"bits": 8, "generator": 283}
    tag: str  # base64 的 HMAC-SHA256 标签

    def field_params(self) -> FieldParams:
        return FieldParams.from_dict(self.field)

    def y_bytes(self) -> bytes:
        return b64d(self.y)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ShareEnvelope":
        required = {"version", "set_id", "x", "y", "threshold", "field", "tag"}
        missing = required - set(data)
        if missing:
            raise MalformedEnvelope(f"envelope missing fields: {sorted(missing)}")
        if data["version"] != ENVELOPE_VERSION:
            raise MalformedEnvelope(f"unsupported envelope version: {data['version']}")
        if not isinstance(data["field"], dict) or {"bits", "generator"} - set(data["field"]):
            raise MalformedEnvelope("field must contain bits and generator")
        if not isinstance(data["set_id"], str) or not data["set_id"]:
            raise MalformedEnvelope("set_id must be a non-empty string")
        x = data["x"]
        if not isinstance(x, int) or isinstance(x, bool) or not 1 <= x <= 255:
            raise MalformedEnvelope("x must be an integer in [1,255]")
        threshold = data["threshold"]
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 2:
            raise MalformedEnvelope("threshold must be an integer >= 2")
        # base64 提前验证，给出明确失败类别而非后续隐式异常。
        try:
            y = b64d(data["y"])
            if not y:
                raise MalformedEnvelope("share y must be non-empty")
            b64d(data["tag"])
        except (ValueError, TypeError) as exc:
            raise MalformedEnvelope(f"invalid base64 payload: {exc}") from exc
        return cls(
            version=int(data["version"]),
            set_id=data["set_id"],
            x=int(x),
            y=data["y"],
            threshold=int(threshold),
            field={"bits": int(data["field"]["bits"]),
                   "generator": int(data["field"]["generator"])},
            tag=data["tag"],
        )


class MalformedEnvelope(ValueError):
    """信封结构不合法（缺字段、类型错、base64 错等）。"""


def canonical_envelope_bytes(env: ShareEnvelope) -> bytes:
    """份额内容的规范序列化：字段固定顺序、紧凑分隔、无多余空白。

    标签只覆盖内容字段（不含 tag 自身）。HMAC 输入的确定性格式
    对实现版本固定，不依赖 dict 顺序。
    """
    payload = {
        "version": env.version,
        "set_id": env.set_id,
        "x": env.x,
        "y": env.y,
        "threshold": env.threshold,
        "field": {"bits": env.field["bits"], "generator": env.field["generator"]},
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def envelope_fingerprint(env: ShareEnvelope) -> str:
    """份额指纹：SHA-256 规范信封（含 tag），用于日志去敏与去重。

    同一物理份额重复提交 -> 同指纹；任意字节不同 -> 指纹不同。
    """
    digest = hashlib.sha256(
        json.dumps(env.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest[:16]}"
