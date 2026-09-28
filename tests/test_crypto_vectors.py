"""密码学编码/验签的固定向量与拒绝用例。

向量来自标准以太坊私钥/消息行为（地址、keccak、EIP-155 v），
可独立用外部工具复核；不依赖被测内核的任何输出。
"""

from __future__ import annotations

import pytest

from local_txpool.core import crypto
from local_txpool.core.models import ErrorCode, TxError

# 标准测试私钥（ethereum/tests 常用）
PRIV_HEX = "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
EXPECTED_ADDR = "0x2c7536E3605D9C16a7a3D7b1898e529396a65c23"


def test_address_vector():
    pk = crypto.private_key_from_hex(PRIV_HEX)
    assert crypto.address_for_private_key(pk) == EXPECTED_ADDR


def test_sign_decode_roundtrip():
    pk = crypto.private_key_from_hex(PRIV_HEX)
    tx = crypto.sign_transaction(
        private_key=pk,
        nonce=9,
        gas_price=20_000_000_000,
        gas_limit=21_000,
        to="0x3535353535353535353535353535353535353535",
        value=1_000_000_000_000_000_000,
        data=b"",
        chain_id=1,  # EIP-155 主网链 id，v 应为 37/38
    )
    # EIP-155: v = chain_id*2 + 35 + recovery_id
    assert tx.v in (37, 38)
    dec = crypto.decode_signed_transaction(tx.raw, expected_chain_id=1)
    assert dec == tx
    assert dec.sender == EXPECTED_ADDR
    assert dec.nonce == 9
    assert dec.value == 10**18
    assert len(tx.tx_hash) == 66


def test_chain_id_separation_rejected():
    pk = crypto.private_key_from_hex(PRIV_HEX)
    tx = crypto.sign_transaction(
        private_key=pk, nonce=0, gas_price=1, gas_limit=21_000,
        to="0x" + "11" * 20, value=0, chain_id=31337,
    )
    with pytest.raises(TxError) as exc:
        crypto.decode_signed_transaction(tx.raw, expected_chain_id=1)
    assert exc.value.code is ErrorCode.WRONG_CHAIN_ID


def test_tampered_payload_changes_recovered_sender():
    """篡改负载后，公钥恢复出的是另一个发送者（而非原签名者）。

    secp256k1 恢复对任意 (h, v, r, s) 都可能给出某个公钥，因此系统的安全
    依赖"发送者地址由恢复结果决定"——篡改消息必然改变归属账户，
    从而落不到原账户的 nonce/余额上。这里锁定这一性质。
    """
    import rlp

    pk = crypto.private_key_from_hex(PRIV_HEX)
    tx = crypto.sign_transaction(
        private_key=pk, nonce=0, gas_price=1, gas_limit=21_000,
        to="0x" + "11" * 20, value=0, chain_id=31337,
    )
    decoded = rlp.decode(tx.raw)
    tampered_fields = list(decoded)
    tampered_fields[1] = b"\x02"  # gas_price 1 -> 2
    tampered = rlp.encode(tampered_fields)
    recovered = crypto.decode_signed_transaction(
        tampered, expected_chain_id=31337
    )
    assert recovered.sender != EXPECTED_ADDR
    assert recovered.sender != tx.sender
    assert recovered.gas_price == 2


def test_zero_r_rejected():
    """退化签名（r=0）必须被拒绝。"""
    import rlp

    pk = crypto.private_key_from_hex(PRIV_HEX)
    tx = crypto.sign_transaction(
        private_key=pk, nonce=0, gas_price=1, gas_limit=21_000,
        to="0x" + "11" * 20, value=0, chain_id=31337,
    )
    decoded = list(rlp.decode(tx.raw))
    decoded[7] = (0).to_bytes(32, "big")
    with pytest.raises(TxError) as exc:
        crypto.decode_signed_transaction(
            rlp.encode(decoded), expected_chain_id=31337
        )
    assert exc.value.code is ErrorCode.INVALID_SIGNATURE


def test_garbage_inputs_rejected():
    for garbage in (b"", b"\x00", b"notrlp", b"\xc8" + b"\x80" * 8):
        with pytest.raises(TxError):
            crypto.decode_signed_transaction(garbage, expected_chain_id=31337)


def test_deterministic_synthetic_key_for_fixtures():
    # fixtures 用 keccak("local-txpool/synthetic:alice") 派生，
    # 这里锁定 alice 地址，防止意外改动派生源。
    from eth_hash.auto import keccak

    raw = keccak(b"local-txpool/synthetic:alice")
    addr = crypto.address_for_private_key(raw)
    assert addr == "0x37EE732164845939341B20944566B18e6D77712b"


def test_intrinsic_gas_formula():
    pk = crypto.private_key_from_hex(PRIV_HEX)
    tx = crypto.sign_transaction(
        private_key=pk, nonce=0, gas_price=1, gas_limit=100_000,
        to="0x" + "11" * 20, value=0, data=b"\x00" * 10, chain_id=31337,
    )
    assert crypto.intrinsic_gas(tx, base_gas=21_000, per_data_byte=16) == 21_160


def test_raw_size_has_rlp_structure():
    pk = crypto.private_key_from_hex(PRIV_HEX)
    tx = crypto.sign_transaction(
        private_key=pk, nonce=0, gas_price=1, gas_limit=21_000,
        to="0x" + "11" * 20, value=0, chain_id=31337,
    )
    import rlp

    decoded = rlp.decode(tx.raw)
    assert len(decoded) == 9
