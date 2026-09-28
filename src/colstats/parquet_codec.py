"""最小化的 Thrift Compact Protocol 编解码器（仅覆盖 Parquet 元数据所需）。

PyArrow 不暴露 *页级* 统计（DataPageHeader.statistics），也不允许改写
RowGroup.sorting_columns。审计需要读取这些字段、并在构造"坏统计"夹具时
就地重写它们，因此这里实现一个自包含的 Thrift compact 解析/序列化器，
不依赖 fastparquet 或 thrift 库。

设计：
- 解析结果是通用节点树 ``TNode(fid, value)``，value 为
  int/float/bool/bytes、``list[TNode]``（struct 列表）或 ``ScalarList``；
- 未知字段被原样保留，round-trip 后字节级等价；
- 顶层（FileMetaData）返回顶层 struct 的节点列表。

参考：parquet.thrift 与 thrift compact protocol 规范。
"""
from __future__ import annotations

from dataclasses import dataclass

# Compact protocol 类型标识
_CT_STOP = 0
_CT_TRUE = 1
_CT_FALSE = 2
_CT_BYTE = 3
_CT_I16 = 4
_CT_I32 = 5
_CT_I64 = 6
_CT_DOUBLE = 7
_CT_BINARY = 8
_CT_LIST = 9
_CT_STRUCT = 12

MASK64 = (1 << 64) - 1


@dataclass
class ScalarList:
    """scalar 元素列表，保留 compact 元素类型以便无损 round-trip。"""

    items: list[object]
    etype: int


@dataclass
class StructList:
    """struct 元素列表（与单个 struct 的裸 list[TNode] 区分）。"""

    items: list[list["TNode"]]


@dataclass
class TNode:
    """一个 Thrift 字段。fid 为字段号；单个 struct 的值是裸 ``list[TNode]``。"""

    fid: int | None
    value: object  # int|float|bool|bytes|list[TNode]|ScalarList|StructList
    # 解析时记录的 compact wire 类型，保证无损重写（i16/i32/i64 区分）
    ctype: int | None = None

    def get(self, fid: int) -> "TNode | None":
        if isinstance(self.value, list) and (
            not self.value or isinstance(self.value[0], TNode)
        ):
            for child in self.value:
                if child.fid == fid:
                    return child
        return None

    def require(self, fid: int) -> "TNode":
        node = self.get(fid)
        if node is None:
            raise KeyError(f"缺少字段 {fid}")
        return node

    def children(self) -> list["TNode"]:
        return list(self.value) if isinstance(self.value, list) else []

    def scalars(self) -> list[object]:
        if isinstance(self.value, ScalarList):
            return self.value.items
        raise ValueError(f"字段 {self.fid} 不是 scalar 列表")

    def structs(self) -> list[list["TNode"]]:
        if isinstance(self.value, StructList):
            return self.value.items
        raise ValueError(f"字段 {self.fid} 不是 struct 列表")


def _zz_enc(n: int) -> int:
    # thrift 按 64 位定宽做 zigzag
    n &= MASK64
    if n >= 1 << 63:
        n -= 1 << 64
    return ((n << 1) ^ (n >> 63)) & MASK64


def _zz_dec(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


class _Reader:
    def __init__(self, buf: bytes, pos: int = 0):
        self.buf = buf
        self.pos = pos

    def byte(self) -> int:
        v = self.buf[self.pos]
        self.pos += 1
        return v

    def varint(self) -> int:
        shift = 0
        result = 0
        while True:
            b = self.byte()
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7

    def binary(self) -> bytes:
        length = self.varint()
        data = self.buf[self.pos : self.pos + length]
        self.pos += length
        return data

    def double(self) -> float:
        import struct

        data = self.buf[self.pos : self.pos + 8]
        self.pos += 8
        return struct.unpack("<d", data)[0]

    def struct(self) -> list[TNode]:
        nodes: list[TNode] = []
        last = 0
        while True:
            b = self.byte()
            if b == _CT_STOP:
                return nodes
            ctype = b & 0x0F
            delta = (b & 0xF0) >> 4
            fid = last + delta if delta else _zz_dec(self.varint())
            last = fid
            nodes.append(TNode(fid, self.value(ctype), ctype=ctype))

    def value(self, ctype: int) -> object:
        if ctype == _CT_TRUE:
            return True
        if ctype == _CT_FALSE:
            return False
        if ctype == _CT_BYTE:
            v = self.buf[self.pos]
            self.pos += 1
            return v - 256 if v >= 128 else v
        if ctype in (_CT_I16, _CT_I32, _CT_I64):
            return _zz_dec(self.varint())
        if ctype == _CT_DOUBLE:
            return self.double()
        if ctype == _CT_BINARY:
            return self.binary()
        if ctype == _CT_STRUCT:
            return self.struct()
        if ctype == _CT_LIST:
            header = self.byte()
            size = (header & 0xF0) >> 4
            etype = header & 0x0F
            if size == 15:
                size = self.varint()
            items = [self.value(etype) for _ in range(size)]
            if etype == _CT_STRUCT:
                return StructList(items)
            return ScalarList(items, etype)
        raise ValueError(f"不支持的 compact 类型 {ctype}")


def decode_struct(buf: bytes, pos: int = 0) -> tuple[list[TNode], int]:
    """解析一个 struct，返回 (节点列表, 结束位置)。"""
    reader = _Reader(buf, pos)
    nodes = reader.struct()
    return nodes, reader.pos


# ---------------------------------------------------------------- 序列化


class _Writer:
    def __init__(self) -> None:
        self.parts: list[bytes] = []

    def varint(self, n: int) -> None:
        n &= MASK64
        while True:
            b = n & 0x7F
            n >>= 7
            self.parts.append(bytes([b | 0x80]) if n else bytes([b]))
            if not n:
                return

    def binary(self, data: bytes) -> None:
        self.varint(len(data))
        self.parts.append(data)

    def double(self, v: float) -> None:
        import struct

        self.parts.append(struct.pack("<d", v))

    @staticmethod
    def _scalar_type(value: object) -> int:
        if isinstance(value, bool):
            return _CT_TRUE if value else _CT_FALSE
        if isinstance(value, int):
            return _CT_I64
        if isinstance(value, float):
            return _CT_DOUBLE
        if isinstance(value, (bytes, bytearray)):
            return _CT_BINARY
        raise ValueError(f"无法推断 scalar 类型: {type(value)}")

    def _scalar(self, value: object, ctype: int) -> None:
        if ctype in (_CT_TRUE, _CT_FALSE):
            return
        if ctype == _CT_BYTE:
            self.parts.append(bytes([int(value) & 0xFF]))
        elif ctype in (_CT_I16, _CT_I32, _CT_I64):
            self.varint(_zz_enc(int(value)))
        elif ctype == _CT_DOUBLE:
            self.double(float(value))
        elif ctype == _CT_BINARY:
            self.binary(bytes(value))
        else:
            raise ValueError(f"不支持写出 scalar 类型 {ctype}")

    def struct(self, nodes: list[TNode]) -> None:
        last = 0
        for node in nodes:
            assert node.fid is not None
            ctype = self._field_type(node)
            delta = node.fid - last
            if 0 < delta <= 15:
                self.parts.append(bytes([(delta << 4) | ctype]))
            else:
                self.parts.append(bytes([ctype]))
                self.varint(_zz_enc(node.fid))
            last = node.fid
            self._write_field(node, ctype)
        self.parts.append(bytes([_CT_STOP]))

    @staticmethod
    def _field_type(node: TNode) -> int:
        if node.ctype is not None:
            return node.ctype
        value = node.value
        if isinstance(value, (ScalarList, StructList)):
            return _CT_LIST
        if isinstance(value, list):
            return _CT_STRUCT
        return _Writer._scalar_type(value)

    def _write_field(self, node: TNode, ctype: int) -> None:
        value = node.value
        if ctype in (_CT_TRUE, _CT_FALSE):
            return
        if ctype == _CT_STRUCT:
            self.struct(value)  # type: ignore[arg-type]
            return
        if ctype == _CT_LIST:
            if isinstance(value, StructList):
                etype = _CT_STRUCT
                items = [(it, etype) for it in value.items]
            else:
                assert isinstance(value, ScalarList)
                etype = value.etype
                # 元素级 ctype 允许覆盖（当前与列表 ctype 一致，保留扩展点）
                items = [(it, getattr(it, "ctype", etype) if isinstance(it, TNode) else etype)
                         for it in value.items]
            size = len(items)
            if size >= 15:
                self.parts.append(bytes([0xF0 | etype]))
                self.varint(size)
            else:
                self.parts.append(bytes([(size << 4) | etype]))
            for item, et in items:
                if et == _CT_STRUCT:
                    self.struct(item)
                else:
                    self._scalar(item, et)
            return
        self._scalar(value, ctype)


def encode_struct(nodes: list[TNode]) -> bytes:
    writer = _Writer()
    writer.struct(nodes)
    return b"".join(writer.parts)


def clone_nodes(nodes: list[TNode]) -> list[TNode]:
    """深拷贝一棵解析出来的节点树（重写时避免污染原始 FileModel）。"""
    out: list[TNode] = []
    for n in nodes:
        if isinstance(n.value, list):
            value = clone_nodes(n.value)
        elif isinstance(n.value, ScalarList):
            value = ScalarList(list(n.value.items), n.value.etype)
        elif isinstance(n.value, StructList):
            value = StructList([clone_nodes(item) for item in n.value.items])
        else:
            value = n.value
        out.append(TNode(n.fid, value, ctype=n.ctype))
    return out
