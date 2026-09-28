"""分类错误体系。

所有解码/编码失败都带有明确的失败类别（error category），
API 层据此返回确定的错误码，禁止把异常统一吞成成功。

错误类别名称刻意稳定，测试和日志直接引用它们做判定依据。
"""

from __future__ import annotations


class ABIError(Exception):
    """所有 ABI 相关错误的根。category 是机器可读的稳定标识。"""

    category: str = "abi_error"


# ---- 编码侧 ----


class ABIEncodeError(ABIError):
    category = "encode_error"


class ABIValueError(ABIEncodeError):
    """Python 值与声明类型不符（例如 int 传入负数给 uint）。"""

    category = "value_error"


class LengthMismatchError(ABIEncodeError):
    """固定长度数组长度不符、或类型与值个数不符。"""

    category = "length_mismatch"


# ---- 类型系统 ----


class InvalidTypeError(ABIError):
    category = "invalid_type"


class UnsupportedTypeError(ABIError):
    """类型语法合法，但不在本受限后端支持范围内（address/bool/fixed...）。"""

    category = "unsupported_type"


# ---- 解码侧 ----


class ABIDecodeError(ABIError):
    category = "decode_error"


class OffsetOutOfBoundsError(ABIDecodeError):
    """偏移/长度越界，或相对容器基准为负。"""

    category = "offset_out_of_bounds"


class OverlapError(ABIDecodeError):
    """两个动态子块的字节区间相交，或头/尾区间相交。"""

    category = "overlap"


class NonCanonicalPaddingError(ABIDecodeError):
    """整数符号扩展/字节尾部填充不符合规范。"""

    category = "non_canonical_padding"


class NonCanonicalLayoutError(ABIDecodeError):
    """动态子块未紧贴排列（出现非预期间隙）、排序错乱或顶层有多余尾字节。"""

    category = "non_canonical_layout"


class AllocationLimitError(ABIDecodeError):
    """声明长度超过本后端的硬上限，防止巨大分配。"""

    category = "allocation_limit"


class DepthLimitError(ABIDecodeError):
    """嵌套深度超过硬上限。"""

    category = "depth_limit"
