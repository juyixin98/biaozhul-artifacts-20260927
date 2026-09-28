"""teachchain: 教学链的确定性状态机 + gas 计费内核。

模块职责见 README。执行语义入口在 :mod:`teachchain.kernel` 与 :mod:`teachchain.vm`。
"""

from .version import ENGINE_VERSION

__all__ = ["ENGINE_VERSION"]
__version__ = "1.0.0"
