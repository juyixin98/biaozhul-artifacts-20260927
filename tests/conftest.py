"""pytest 共享夹具：确定性密钥、内存服务、签名助手。

密钥由固定标签经 keccak 派生，与 fixtures/scenarios 中的账户一致；
测试不依赖网络、文件或墙钟（统一 VirtualClock）。
"""

from __future__ import annotations

import pytest

from eth_hash.auto import keccak
from eth_keys import keys as ek_keys

from localtxpool.clock import VirtualClock
from localtxpool.config import (
    ApiConfig, ChainConfig, Config, LogConfig, PoolConfig, StorageConfig,
)
from localtxpool.service import Service
from localtxpool.encoding import sign_transaction

CHAIN_ID = 31337


def key_for(label: str) -> bytes:
    return keccak(b"local-txpool-fixture-key:" + label.encode())


def addr_for(label: str) -> bytes:
    return ek_keys.PrivateKey(key_for(label)).public_key.to_canonical_address()


def make_config(**pool_overrides) -> Config:
    return Config(
        chain=ChainConfig(chain_id=CHAIN_ID, block_gas_limit=10_000_000),
        pool=PoolConfig(**pool_overrides),
        storage=StorageConfig(path=":memory:"),
        api=ApiConfig(),
        log=LogConfig(level="WARNING", json=True),
    )


@pytest.fixture
def start_time() -> int:
    return 1000


@pytest.fixture
def service(start_time) -> Service:
    s = Service.in_memory(make_config(), clock=VirtualClock(start_time))
    yield s
    s.close()


@pytest.fixture
def service_factory(start_time):
    """返回 (service, fund, sign, submit) 四元组的工厂，全部绑定到同一实例。"""
    created: list[Service] = []

    def _factory(**pool_overrides):
        s = Service.in_memory(make_config(**pool_overrides),
                              clock=VirtualClock(start_time))
        created.append(s)

        def fund(label: str, balance: int, nonce: int = 0) -> str:
            addr = "0x" + addr_for(label).hex()
            ts = s.clock.now()
            with s.repo.transaction():
                s.repo.ensure_account(addr, ts)
                s.repo.adjust_balance(addr, balance, ts)
                if nonce:
                    row = s.repo.get_account(addr)
                    s.repo.set_account(addr, int(row["balance"]), nonce, ts)
            return addr

        def submit(tx, request_id: str = "test"):
            return s.submit_raw(tx.to_rlp(), request_id=request_id)

        return s, fund, submit

    yield _factory
    for s in created:
        s.close()


@pytest.fixture
def fund(service):
    """给标签账户注资并返回其地址（小写 0x）。"""
    def _fund(label: str, balance: int, nonce: int = 0) -> str:
        addr = "0x" + addr_for(label).hex()
        ts = service.clock.now()
        with service.repo.transaction():
            service.repo.ensure_account(addr, ts)
            service.repo.adjust_balance(addr, balance, ts)
            if nonce:
                row = service.repo.get_account(addr)
                service.repo.set_account(addr, int(row["balance"]), nonce, ts)
        return addr
    return _fund


@pytest.fixture
def sign(service):
    """构造并（可选）提交一笔签名交易。"""
    def _sign(label: str, *, nonce: int, gas_price: int = 10, gas_limit: int = 21000,
              to: bytes | None = None, value: int = 0, data: bytes = b""):
        if to is None:
            to = addr_for("bob")
        return sign_transaction(
            key_for(label), nonce=nonce, gas_price=gas_price, gas_limit=gas_limit,
            to=to, value=value, data=data, chain_id=CHAIN_ID)
    return _sign


@pytest.fixture
def submit(service):
    def _submit(tx, request_id: str = "test"):
        return service.submit_raw(tx.to_rlp(), request_id=request_id)
    return _submit
