"""签名/验签与汇编器的单元测试。"""
from __future__ import annotations

import pytest

from teaching_chain import encoding
from teaching_chain.vm import assemble, disassemble
from teaching_chain.vm.opcodes import validate_bytecode
from teaching_chain.vm.opcodes import InvalidBytecode


def test_sign_and_verify_roundtrip(alice):
    tx = {"chain": "teaching-chain-local", "nonce": 1,
          "code": "00", "gas_limit": 1000}
    signed = encoding.sign_transaction(alice, tx)
    signed["pubkey"] = alice.public_bytes().hex()
    assert encoding.verify_transaction(signed) == alice.address_hex()
    # 签名恰好 64 字节（Ed25519）
    assert len(bytes.fromhex(signed["signature"])) == 64


def test_verify_rejects_tampered_chain(alice):
    signed = encoding.sign_transaction(
        alice, {"chain": "a", "nonce": 1, "code": "00", "gas_limit": 1}
    )
    signed["pubkey"] = alice.public_bytes().hex()
    signed["chain"] = "b"
    with pytest.raises(encoding.SignatureError):
        encoding.verify_transaction(signed)


def test_verify_rejects_foreign_signature(alice, bob):
    signed = encoding.sign_transaction(
        alice, {"chain": "a", "nonce": 1, "code": "00", "gas_limit": 1}
    )
    signed["pubkey"] = bob.public_bytes().hex()  # 公钥换成 bob
    with pytest.raises(encoding.SignatureError):
        encoding.verify_transaction(signed)


def test_assemble_disassemble_roundtrip():
    text = """
    PUSH8 -5
    PUSH1 200
    ADD
    CALL
    {
      PUSH1 1
      PUSH1 2
      ADD
      POP
    }
    STOP
    """
    code = assemble(text)
    validate_bytecode(code)  # 汇编结果必须通过静态校验
    out = disassemble(code)
    assert "PUSH8 -5" in out
    assert "PUSH1 200" in out
    assert "CALL" in out


def test_assemble_rejects_bad_mnemonic():
    with pytest.raises(ValueError):
        assemble("FOOBAR 1\n")


def test_assemble_call_requires_block():
    with pytest.raises(ValueError):
        assemble("CALL\nSTOP\n")


def test_deterministic_fixture_keys():
    # 同种子每次重建同一密钥（夹具可复现）
    import hashlib
    seed = hashlib.sha256(b"teaching-chain-fixture-0").digest()
    k1 = encoding.KeyPair.from_seed(seed)
    k2 = encoding.KeyPair.from_seed(seed)
    assert k1.address_hex() == k2.address_hex()
    assert k1.public_bytes() == k2.public_bytes()


def test_validate_rejects_unknown_opcode():
    with pytest.raises(InvalidBytecode):
        validate_bytecode(b"\x01\xee")
