"""EIP-155 签名、验签与链/交易级拒绝测试。"""

from __future__ import annotations

import pytest

from localtxpool.encoding import (
    decode_signed,
    sign_transaction,
    intrinsic_gas,
    to_checksum_address,
    InvalidSignature,
    WrongChainId,
    IntrinsicGasTooLow,
    TxDecodeError,
)
from tests.conftest import CHAIN_ID, addr_for, key_for


def test_sign_recover_sender():
    tx = sign_transaction(key_for("alice"), nonce=0, gas_price=10, gas_limit=21000,
                          to=addr_for("bob"), value=100, chain_id=CHAIN_ID)
    raw = tx.to_rlp()
    got = decode_signed(raw, expected_chain_id=CHAIN_ID)
    assert got.sender == to_checksum_address(addr_for("alice"))
    assert got.chain_id == CHAIN_ID
    # 哈希稳定
    assert got.hash() == tx.hash()


def test_wrong_chain_id_rejected():
    tx = sign_transaction(key_for("alice"), nonce=0, gas_price=10, gas_limit=21000,
                          to=addr_for("bob"), value=1, chain_id=CHAIN_ID)
    with pytest.raises(WrongChainId):
        decode_signed(tx.to_rlp(), expected_chain_id=1)


def _eip155_v(chain_id, recid):
    return chain_id * 2 + 35 + recid


def test_non_eip155_v_rejected():
    # v=27/28 是无 chain id 的旧式签名：必须拒绝，避免跨链重放
    tx = sign_transaction(key_for("alice"), nonce=0, gas_price=10, gas_limit=21000,
                          to=addr_for("bob"), value=1, chain_id=CHAIN_ID)
    # 构造字段列表（r,s 合法但 v=27）
    from localtxpool.encoding import rlp_encode, _int_to_min_bytes
    raw = rlp_encode([
        _int_to_min_bytes(0), _int_to_min_bytes(10), _int_to_min_bytes(21000),
        addr_for("bob"), _int_to_min_bytes(1), b"",
        _int_to_min_bytes(27), _int_to_min_bytes(tx.r), _int_to_min_bytes(tx.s),
    ])
    with pytest.raises(InvalidSignature):
        decode_signed(raw, expected_chain_id=CHAIN_ID)


def test_high_s_rejected():
    # 高 s（延展性）必须被拒绝
    from localtxpool.encoding import (
        SECP256K1_N, SECP256K1_HALF_N, rlp_encode, _int_to_min_bytes,
    )
    tx = sign_transaction(key_for("alice"), nonce=0, gas_price=10, gas_limit=21000,
                          to=addr_for("bob"), value=1, chain_id=CHAIN_ID)
    high_s = SECP256K1_HALF_N + 1
    raw = rlp_encode([
        _int_to_min_bytes(0), _int_to_min_bytes(10), _int_to_min_bytes(21000),
        addr_for("bob"), _int_to_min_bytes(1), b"",
        _int_to_min_bytes(_eip155_v(CHAIN_ID, 0)),
        _int_to_min_bytes(tx.r), _int_to_min_bytes(high_s),
    ])
    with pytest.raises(InvalidSignature):
        decode_signed(raw, expected_chain_id=CHAIN_ID)


def test_rs_out_of_range_rejected():
    from localtxpool.encoding import SECP256K1_N, rlp_encode, _int_to_min_bytes
    tx = sign_transaction(key_for("alice"), nonce=0, gas_price=10, gas_limit=21000,
                          to=addr_for("bob"), value=1, chain_id=CHAIN_ID)
    raw = rlp_encode([
        _int_to_min_bytes(0), _int_to_min_bytes(10), _int_to_min_bytes(21000),
        addr_for("bob"), _int_to_min_bytes(1), b"",
        _int_to_min_bytes(_eip155_v(CHAIN_ID, 0)),
        _int_to_min_bytes(SECP256K1_N), _int_to_min_bytes(tx.s),  # r == N 非法
    ])
    with pytest.raises(InvalidSignature):
        decode_signed(raw, expected_chain_id=CHAIN_ID)


def test_flipped_recovery_id_recovers_different_address():
    """翻转 recovery id 一般恢复到另一个合法公钥——解码本身成功，但发送者不再是签名者。

    这正是交易池以**恢复出的地址**记账而非信任任何申报地址的原因。
    """
    tx = sign_transaction(key_for("alice"), nonce=0, gas_price=10, gas_limit=21000,
                          to=addr_for("bob"), value=1, chain_id=CHAIN_ID)
    from localtxpool.encoding import rlp_encode, _int_to_min_bytes
    wrong_recid = 1 - (tx.v - 35) % 2
    raw = rlp_encode([
        _int_to_min_bytes(0), _int_to_min_bytes(10), _int_to_min_bytes(21000),
        addr_for("bob"), _int_to_min_bytes(1), b"",
        _int_to_min_bytes(CHAIN_ID * 2 + 35 + wrong_recid),
        _int_to_min_bytes(tx.r), _int_to_min_bytes(tx.s),
    ])
    got = decode_signed(raw, expected_chain_id=CHAIN_ID)
    assert got.from_address != addr_for("alice")


def test_arbitrary_valid_rs_attributes_to_other_key_not_forgery():
    """r=s=1 在 secp256k1 上可恢复出某个曲线上的点——这不是对 Alice 的伪造：

    恢复出的地址是另一个随机密钥，交易将由该地址承担余额/nonce 校验。
    本测试固化这一安全语义：签名从不信任任何申报发送者。
    """
    from localtxpool.encoding import rlp_encode, _int_to_min_bytes
    raw = rlp_encode([
        _int_to_min_bytes(0), _int_to_min_bytes(10), _int_to_min_bytes(21000),
        addr_for("bob"), _int_to_min_bytes(1), b"",
        _int_to_min_bytes(_eip155_v(CHAIN_ID, 0)),
        _int_to_min_bytes(1), _int_to_min_bytes(1),
    ])
    got = decode_signed(raw, expected_chain_id=CHAIN_ID)
    assert got.from_address != addr_for("alice")


def test_wrong_field_count_rejected():
    from localtxpool.encoding import rlp_encode, _int_to_min_bytes
    # 8 个字段而非 9 个
    raw = rlp_encode([b""] * 8)
    with pytest.raises(TxDecodeError):
        decode_signed(raw, expected_chain_id=CHAIN_ID)


def test_bad_to_length_rejected():
    tx = sign_transaction(key_for("alice"), nonce=0, gas_price=10, gas_limit=21000,
                          to=addr_for("bob"), value=1, chain_id=CHAIN_ID)
    from localtxpool.encoding import rlp_encode, _int_to_min_bytes
    raw = rlp_encode([
        _int_to_min_bytes(0), _int_to_min_bytes(10), _int_to_min_bytes(21000),
        b"\x00" * 19,  # 地址 19 字节
        _int_to_min_bytes(1), b"",
        _int_to_min_bytes(tx.v), _int_to_min_bytes(tx.r), _int_to_min_bytes(tx.s),
    ])
    with pytest.raises(TxDecodeError):
        decode_signed(raw, expected_chain_id=CHAIN_ID)


def test_trailing_bytes_rejected():
    tx = sign_transaction(key_for("alice"), nonce=0, gas_price=10, gas_limit=21000,
                          to=addr_for("bob"), value=1, chain_id=CHAIN_ID)
    with pytest.raises(TxDecodeError):
        decode_signed(tx.to_rlp() + b"\x00", expected_chain_id=CHAIN_ID)


def test_intrinsic_gas_pricing():
    assert intrinsic_gas(b"") == 21000
    assert intrinsic_gas(b"\x00" * 10) == 21000 + 10 * 4
    assert intrinsic_gas(b"\xff" * 10) == 21000 + 10 * 16
    assert intrinsic_gas(b"\x00\xff" * 5) == 21000 + 5 * 4 + 5 * 16


def test_gas_limit_below_intrinsic_rejected_at_signing():
    with pytest.raises(IntrinsicGasTooLow):
        sign_transaction(key_for("alice"), nonce=0, gas_price=10, gas_limit=21000,
                         to=addr_for("bob"), value=1, data=b"\xff",
                         chain_id=CHAIN_ID)  # 21016 needed
