"""标量值类型保真编解码。

SQLite 的动态类型与 Python 绑定存在两处失真：bool 被存成 INTEGER 0/1，
亲和性还会把文本/字节串互相改造。为保证“源是什么类型，目标读回就是什么类型”，
所有目标表数据列统一声明为自定义类型名 ``MERGEVAL``，写入时把标量编码成
带 1 字节类型标记的 BLOB，读出时用 PARSE_DECLTYPES 转换器精确还原。

物理上全部是 BLOB，因此不受任何列亲和性影响；快照指纹/日志/JSON 另走 JSON 路径
（bytes 用 base64 标记对象），不经过本编解码。
"""
from __future__ import annotations

import struct

VALUE_TYPE = "MERGEVAL"

_TAG_BOOL = b"b"
_TAG_INT = b"i"
_TAG_FLOAT = b"f"
_TAG_STR = b"s"
_TAG_BYTES = b"x"
_TAG_NULL = b"n"


def encode(value) -> bytes:
    if value is None:
        return _TAG_NULL
    t = type(value)
    if t is bool:
        return _TAG_BOOL + (b"1" if value else b"0")
    if t is int:
        return _TAG_INT + str(value).encode("ascii")
    if t is float:
        return _TAG_FLOAT + struct.pack(">d", value)
    if t is str:
        return _TAG_STR + value.encode("utf-8")
    if t is bytes:
        return _TAG_BYTES + value
    raise TypeError(f"MERGEVAL cannot store {t.__name__}")


def decode(raw):
    if raw is None:
        # 仅理论分支：转换器一般收到 BLOB；保留以兼容被外部工具置 NULL 的列
        return None
    if isinstance(raw, memoryview):
        raw = bytes(raw)
    if not isinstance(raw, (bytes, bytearray)):
        # 被外部工具直接写入的非编码值：原样返回，避免读取崩溃
        return raw
    tag, payload = raw[:1], bytes(raw[1:])
    if tag == _TAG_NULL:
        return None
    if tag == _TAG_BOOL:
        return payload == b"1"
    if tag == _TAG_INT:
        return int(payload.decode("ascii"))
    if tag == _TAG_FLOAT:
        return struct.unpack(">d", payload)[0]
    if tag == _TAG_STR:
        return payload.decode("utf-8")
    if tag == _TAG_BYTES:
        return payload
    # 未识别标记：返回原始字节，不静默篡改
    return bytes(raw)


def register_codec() -> None:
    """注册转换器（幂等）。

    刻意 **不** 注册 str/int/bool 等进程级适配器：那会改变所有 SQL 参数绑定
    （连 sqlite_master 的表名绑定都会被编码）。目标表写路径在 store 层
    显式调用 :func:`encode`；转换器只作用于声明为 MERGEVAL 的列，
    不影响元数据表与普通文本参数。
    """
    import sqlite3

    sqlite3.register_converter(VALUE_TYPE, decode)
