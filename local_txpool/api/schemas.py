"""HTTP 请求/响应模型。

交易提交接收 **已签名的 RLP 原始交易**（0x 十六进制），服务端只负责解码验签，
永远不接受"替调用方签名"——这样编码/验签模块是交易进入系统的唯一关口。
合成密钥由 fixtures/scripts 在本地生成，与生产账户无关。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class AccountCreateRequest(BaseModel):
    address: str = Field(..., description="20 字节 EVM 风格地址（0x 前缀）")
    balance: int = Field(..., ge=0)
    credit: bool = Field(
        True, description="账户已存在时是否累加余额；False 则重设为给定余额"
    )


class RawTxSubmitRequest(BaseModel):
    raw_tx: str = Field(
        ..., description="RLP 编码的已签名交易（0x 前缀十六进制）"
    )

    @field_validator("raw_tx")
    @classmethod
    def _is_hex(cls, v: str) -> str:
        if not isinstance(v, str) or not v.startswith("0x"):
            raise ValueError("raw_tx 必须是 0x 前缀十六进制字符串")
        return v


class RawTxsBlockRequest(BaseModel):
    external_raw_txs: list[str] = Field(
        default_factory=list,
        description="随区块到达的外部已签名交易（未在池中的尝试准入）",
    )


class RollbackRequest(BaseModel):
    target_number: int = Field(..., ge=0)


class ApiError(BaseModel):
    error: str = Field(..., description="稳定错误码（ErrorCode 值）")
    message: str
    request_id: str
    service_version: str
    details: dict[str, Any] = Field(default_factory=dict)
    # 与硬失败分开列出的"不确定结论"（例如回滚后无法重入池的交易）。
    uncertainties: list[dict[str, Any]] = Field(default_factory=list)
    audit_query: str = Field(
        ..., description="查询本请求完整处理轨迹的相对路径"
    )
