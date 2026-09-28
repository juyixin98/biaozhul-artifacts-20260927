"""教学链：确定性小型状态机与 gas 计费。

模块职责：

* ``encoding`` —— 规范二进制编码、SHA-256、Ed25519 签名验签；
* ``vm`` —— 栈式虚拟机、操作码、gas 表、汇编器；
* ``kernel`` —— 交易校验、执行、收据、区块、纯内存链状态；
* ``store`` —— SQLite 区块 / 交易 / 收据索引；
* ``node`` —— 内核与索引的服务装配；
* ``api`` —— FastAPI HTTP 接口；
* ``replay`` —— 离线确定性回放与核验；
* ``diagnostics`` —— 带请求标识与脱敏的结构化诊断。
"""
from __future__ import annotations

from .config import PROGRAM_VERSION

__version__ = "1.0.0"
__all__ = ["PROGRAM_VERSION", "__version__"]
