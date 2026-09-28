"""文本规范化层（版本固定）。

设计约定：
- ``NORMALIZER_VERSION`` 是固定常量。任何改变规范化语义的修改都必须提升版本号，
  存储层据此拒绝用旧版本构建的索引（报 E_NORMALIZER_VERSION_MISMATCH），
  绝不静默使用语义错误的索引。
- 规范化结果只用于索引键与排序；词条的显示原文 ``surface`` 始终原样保留，
  查询结果返回原文。
- 规范化是确定性的纯函数，不依赖任何外部服务或数据。
"""
from __future__ import annotations

import unicodedata

# 提升规则：NFKC 规则、大小写折叠策略、空白策略任一改变都必须递增。
NORMALIZER_VERSION = "norm-1.0.0"


def normalize(text: str) -> str:
    """把原始文本规范化为索引键。

    步骤：
    1. NFKC：全角拉丁字母/数字/标点折叠为兼容形式（例如 ``ＡＢＣ -> ABC``）；
    2. ``casefold``：Unicode 大小写折叠（比 ``lower`` 更彻底，例如 ß→ss）；
    3. 把所有 Unicode 空白运行折叠为单个 ASCII 空格，并去除首尾空白。

    空串或纯空白输入规范化后为 ``""``，由上层判定为非法输入（空键不允许入索引），
    但空前缀 ``""`` 在查询侧是合法的（表示全量词头查询）。
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(folded.split())
