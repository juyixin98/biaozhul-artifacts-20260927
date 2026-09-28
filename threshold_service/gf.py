"""有限域参数与运算。

本服务的 Shamir 分享运行在 GF(2^8) 上（每字节独立成一个多项式），
使用 AES/Rijndael 不可约多项式 x^8+x^4+x^3+x+1（0x11B）。

字段参数（位宽 8、生成多项式 0x11B）是**集合身份的一部分**：
份额在创建时与参数绑定，恢复时若参数不一致必须拒绝（防止跨参数混集）。

运算委托给成熟库 pyfinite（ffield.FField），并在此集中封装，
便于在审计与拒绝理由中引用具体参数。
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from pyfinite import ffield

FIELD_BITS = 8
AES_GENERATOR = 0x11B  # x^8 + x^4 + x^3 + x + 1
# GF(2^8) 最多容纳 256 个元素，0 预留给截距（秘密），故横坐标范围 1..255。
MAX_SHARES = 255


@dataclass(frozen=True)
class FieldParams:
    """绑定到份额集合的字段参数。"""

    bits: int = FIELD_BITS
    generator: int = AES_GENERATOR

    def canonical(self) -> str:
        """规范字符串，用于份额信封与策略比对。"""
        return f"GF(2^{self.bits})/0x{self.generator:03X}"

    def to_dict(self) -> dict:
        return {"bits": self.bits, "generator": self.generator}

    @classmethod
    def from_dict(cls, data: dict) -> "FieldParams":
        return cls(bits=int(data["bits"]), generator=int(data["generator"]))


@lru_cache(maxsize=4)
def _field(bits: int, gen: int) -> ffield.FField:
    # useLUT=0：不读写工作目录下的查找表缓存，保证可重复、环境隔离。
    return ffield.FField(bits, gen=gen, useLUT=0)


def get_field(params: FieldParams) -> ffield.FField:
    """当前版本只支持 GF(2^8)/0x11B；其它参数显式拒绝。"""
    if params.bits != FIELD_BITS or params.generator != AES_GENERATOR:
        raise UnsupportedField(params)
    return _field(params.bits, params.generator)


class UnsupportedField(ValueError):
    """提交了本服务不支持的字段参数。"""

    def __init__(self, params: FieldParams):
        super().__init__(f"unsupported field parameters: {params.canonical()}")
        self.params = params


def add(params: FieldParams, a: int, b: int) -> int:
    return get_field(params).Add(a, b)


def mul(params: FieldParams, a: int, b: int) -> int:
    return get_field(params).Multiply(a, b)


def div(params: FieldParams, a: int, b: int) -> int:
    """a / b（b != 0）。横坐标互不相同保证分母非零。"""
    if b == 0:
        raise ZeroDivisionError("division by zero in GF(2^8)")
    return get_field(params).Divide(a, b)


def inverse(params: FieldParams, a: int) -> int:
    if a == 0:
        raise ZeroDivisionError("zero has no multiplicative inverse")
    return get_field(params).Inverse(a)
