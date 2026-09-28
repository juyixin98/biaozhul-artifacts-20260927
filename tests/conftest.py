"""共享测试夹具。

期望值在各测试里**手工计算后硬编码**；本文件只负责搭环境，不生成“参考答案”。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from teachchain import fixtures  # noqa: E402
from teachchain.kernel import ChainState, Kernel  # noqa: E402
from teachchain.storage import IndexStore  # noqa: E402
from teachchain.service import ChainService  # noqa: E402


@pytest.fixture
def alice():
    return fixtures.signer("alice")


@pytest.fixture
def bob():
    return fixtures.signer("bob")


@pytest.fixture
def funded_kernel(alice):
    k = Kernel(ChainState())
    k.credit(alice.address, 10_000_000)
    return k


@pytest.fixture
def funded_service(alice, tmp_path):
    db = str(tmp_path / "chain.db")
    svc = ChainService(IndexStore(db))
    svc.seed(alice.address, 10_000_000)
    return svc


def deploy(kernel, signer, code, nonce, gas=500_000):
    env = fixtures.envelope(signer, "deploy", nonce=nonce, gas_limit=gas, code=code)
    return kernel.apply_tx(env)


def invoke(kernel, signer, to, words, nonce, gas=500_000):
    env = fixtures.envelope(signer, "invoke", nonce=nonce, gas_limit=gas,
                            to=to, words=words)
    return kernel.apply_tx(env)
