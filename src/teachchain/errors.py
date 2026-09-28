"""错误分类。

三类诊断口径（README 有对照表）：

* :class:`Rejected`        —— 确定性拒绝：格式、签名、nonce、余额等准入检查失败，
                              交易不执行、不入块、不扣费。
* :class:`Halt`            —— 已准入交易在 VM 内异常中止（exceptional halt），
                              状态回滚但 gas 全部消耗。
* :class:`Revert`          —— 程序主动 REVERT，状态回滚，剩余 gas 退还。
"""

from __future__ import annotations


class TeachChainError(Exception):
    """所有 teachchain 领域错误的基类。"""


class Rejected(TeachChainError):
    """交易在准入阶段被确定性拒绝（不执行、不入块）。"""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message or code
        super().__init__(f"{code}: {self.message}")


class Halt(TeachChainError):
    """VM 异常中止：消耗全部剩余 gas，帧内状态写回滚。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class Revert(TeachChainError):
    """程序执行 REVERT：帧内状态回滚，剩余 gas 正常退还。"""

    def __init__(self, output: list[int]) -> None:
        self.output = output
        super().__init__("revert")


class ReplayMismatch(TeachChainError):
    """离线回放结果与已存收据/状态不一致。"""

    def __init__(self, height: int, tx_hash: str, field: str, stored: object, computed: object) -> None:
        self.height = height
        self.tx_hash = tx_hash
        self.field = field
        self.stored = stored
        self.computed = computed
        super().__init__(
            f"replay mismatch at height={height} tx={tx_hash[:16]} "
            f"field={field}: stored={stored!r} computed={computed!r}"
        )
