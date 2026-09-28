"""UTXO 测试资产账本 —— 包边界与公共导出。

模块边界：
    errors    错误分类契约（4 类，稳定 code）
    encoding  固定二进制/JSON 编码、签名摘要、根计算
    crypto    成熟库验签（secp256k1/ECDSA DER、确定性测试密钥）
    store     SQLite 索引存储（单事务原子提交）
    kernel    链状态内核（逐笔规划、拓扑、守恒；不碰存储写入）
    journal   运行日志（run_id、中间状态、判定理由）
    replay    离线回放（夹具 + 独立 oracle 对照）
    fab       本地合成夹具构造器
    api       FastHTTP 边界
"""
from .errors import (
    AmountOutOfRangeError,
    AmountOverflowError,
    BadGenesisError,
    BlockConflictError,
    ConservationMismatchError,
    DoubleSpendError,
    ErrorCategory,
    ForwardReferenceError,
    IllegalIssuanceError,
    InternalError,
    InvalidFeeError,
    LedgerError,
    MalformedEncodingError,
    MerkleRootMismatchError,
    ReferenceCycleError,
    ResourceLimitError,
    SignatureError,
    StorageFailureError,
    TxidDuplicateError,
    UnknownOutpointError,
    WitnessCountMismatchError,
    ZeroValueError,
)
from .kernel import BlockPlan, Kernel, Limits, MAX_MONEY, PlannedTx
from .store import ChainView, SqliteStore, Utxo

__all__ = [
    "ErrorCategory",
    "LedgerError",
    "MalformedEncodingError",
    "ZeroValueError",
    "AmountOutOfRangeError",
    "AmountOverflowError",
    "WitnessCountMismatchError",
    "TxidDuplicateError",
    "IllegalIssuanceError",
    "InvalidFeeError",
    "ConservationMismatchError",
    "BadGenesisError",
    "DoubleSpendError",
    "UnknownOutpointError",
    "ForwardReferenceError",
    "ReferenceCycleError",
    "BlockConflictError",
    "ResourceLimitError",
    "SignatureError",
    "MerkleRootMismatchError",
    "StorageFailureError",
    "InternalError",
    "Kernel",
    "Limits",
    "BlockPlan",
    "PlannedTx",
    "MAX_MONEY",
    "SqliteStore",
    "Utxo",
    "ChainView",
]
