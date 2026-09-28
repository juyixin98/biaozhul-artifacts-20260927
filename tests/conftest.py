"""共享 pytest fixtures：内存库 + FakeClock + 确定性合成密钥。"""

from __future__ import annotations

import pytest

from local_txpool.core import crypto
from local_txpool.core.clock import FakeClock
from local_txpool.core.config import Config
from local_txpool.core.kernel import Kernel
from local_txpool.storage.repository import Repository, connect, init_schema


@pytest.fixture
def config() -> Config:
    cfg = Config()
    # 测试用较快 TTL 与较小容量，便于覆盖边界
    cfg.pool.pending_ttl_seconds = 300
    cfg.pool.max_transactions = 32
    return cfg


@pytest.fixture
def kernel(config: Config) -> Kernel:
    conn = connect(":memory:")
    init_schema(conn)
    clk = FakeClock(1_700_000_000_000)
    return Kernel(Repository(conn), config, clk)


@pytest.fixture
def keys() -> dict[str, bytes]:
    # "aa"*32 = 64 个十六进制字符 = 32 字节私钥
    return {
        name: crypto.private_key_from_hex("0x" + (ch * 32))
        for name, ch in (
            ("alice", "aa"),
            ("bob", "bb"),
            ("carol", "cc"),
            ("dave", "dd"),
        )
    }


@pytest.fixture
def addresses(keys) -> dict[str, str]:
    return {n: crypto.address_for_private_key(pk) for n, pk in keys.items()}


def make_tx(
    private_key: bytes,
    *,
    nonce: int,
    gas_price: int = 2_000_000_000,
    gas_limit: int = 21_000,
    value: int = 0,
    data: bytes = b"",
    to: str = "0x" + "11" * 20,
    chain_id: int = 31337,
):
    return crypto.sign_transaction(
        private_key=private_key,
        nonce=nonce,
        gas_price=gas_price,
        gas_limit=gas_limit,
        to=to,
        value=value,
        data=data,
        chain_id=chain_id,
    )
