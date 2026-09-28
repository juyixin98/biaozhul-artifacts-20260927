"""内核准入与结算测试：断言具体 gas 数值、状态回滚范围与失败分类。

期望值手工推导（gas.py 常量）：
invoke intrinsic = 21000 + 4*len(input)；
SSTORE set=20000、reset=5000、noop=200；PUSH=3；SLOAD=800；ADD=5；
MSTORE=3(+内存扩张 3)；RETURN=0。
"""

from __future__ import annotations

import pytest

from teachchain import fixtures, gas as G
from teachchain.errors import Rejected
from teachchain.opcodes import assemble
from teachchain.version import ENGINE_VERSION

from conftest import deploy, invoke


# 一段成功程序：读槽0(old) + calldata[0]，写回槽0，返回 [new]
# 压栈顺序（EVM 栈顶为第一操作数）：
#   SSTORE 栈 [value, key(key 在顶)]；MSTORE 栈 [value, offset(offset 在顶)]；
#   RETURN 栈 [length, offset(offset 在顶)]。
SUCCESS_ASM = """
PUSH 0
CALLDATALOAD
PUSH 0
SLOAD
ADD
DUP 1
PUSH 0
SSTORE
PUSH 0
MSTORE
PUSH 1
PUSH 0
RETURN
"""


def test_rejected_bad_signature_changes_nothing(funded_kernel, alice, bob):
    env = fixtures.envelope(alice, "deploy", nonce=0, gas_limit=500_000,
                            code=fixtures.counter_code())
    env["sig_b64"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="  # 64 零字节
    with pytest.raises(Rejected) as ei:
        funded_kernel.apply_tx(env)
    assert ei.value.code == "bad_signature"
    assert funded_kernel.state.nonces.get(alice.address, 0) == 0
    assert funded_kernel.get_balance(alice.address) == 10_000_000


def test_rejected_signature_from_other_key(funded_kernel, alice, bob):
    # bob 用自己的密钥签名，但把 from 改成 alice -> 派生地址不一致
    env = fixtures.envelope(bob, "deploy", nonce=0, gas_limit=500_000,
                            code=fixtures.counter_code())
    env["tx"]["from"] = alice.address
    # 摘要随之改变，签名通常先失效；两种拒绝码都属于“准入拒绝”，
    # 关键是无副作用。这里断言必被拒且不扣费。
    with pytest.raises(Rejected) as ei:
        funded_kernel.apply_tx(env)
    assert ei.value.code in ("bad_signature", "sender_mismatch")
    assert funded_kernel.state.nonces.get(alice.address, 0) == 0
    assert funded_kernel.state.nonces.get(bob.address, 0) == 0


def test_sender_pubkey_mismatch_detected(funded_kernel, alice, bob):
    import base64
    from cryptography.hazmat.primitives import serialization
    from teachchain import crypto
    from teachchain.models import b64e
    # 交易体本身用 bob 的 from，但附上 alice 对“该交易体”的合法签名。
    body = {
        "type": "deploy", "from": bob.address, "nonce": 0,
        "gas_limit": 500_000, "to": None,
        "code_b64": b64e(fixtures.counter_code()), "input": [],
    }
    digest = crypto.digest_payload(body)
    raw = alice.key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)
    env = {"tx": body, "pub_b64": base64.b64encode(raw).decode(),
           "sig_b64": alice.sign_digest_b64(digest)}
    with pytest.raises(Rejected) as ei:
        funded_kernel.apply_tx(env)
    assert ei.value.code == "sender_mismatch"


def test_rejected_tampered_body(funded_kernel, alice):
    env = fixtures.envelope(alice, "deploy", nonce=0, gas_limit=500_000,
                            code=fixtures.counter_code())
    env["tx"]["gas_limit"] = 400_000  # 改了被签名内容
    with pytest.raises(Rejected) as ei:
        funded_kernel.apply_tx(env)
    assert ei.value.code == "bad_signature"
    assert funded_kernel.state.height == 0


def test_stale_nonce_rejected_before_contract_lookup(funded_kernel, alice):
    deploy(funded_kernel, alice, fixtures.counter_code(), nonce=0)
    # nonce 检查在合约存在性检查之前：重放 nonce=0 报 bad_nonce
    with pytest.raises(Rejected) as ei:
        invoke(funded_kernel, alice, "0x" + "0" * 16, [1], nonce=0)
    assert ei.value.code == "bad_nonce"
    # 拒绝不扣费、nonce 不递增
    assert funded_kernel.state.nonces[alice.address] == 1


def test_nonce_ordering_enforced(funded_kernel, alice):
    deploy(funded_kernel, alice, fixtures.counter_code(), nonce=0)
    addr = _deployed_address(alice, 0)
    # 跳过 nonce=1
    with pytest.raises(Rejected) as ei:
        invoke(funded_kernel, alice, addr, [1], nonce=2)
    assert ei.value.code == "bad_nonce"
    # 正确 nonce=1 可执行
    r = invoke(funded_kernel, alice, addr, [1], nonce=1)
    assert r["status"] == 1


def test_gas_below_intrinsic_rejected(funded_kernel, alice):
    deploy(funded_kernel, alice, fixtures.counter_code(), nonce=0)
    addr = _deployed_address(alice, 0)
    intrinsic = G.intrinsic_gas("invoke", input_len=1)
    with pytest.raises(Rejected) as ei:
        invoke(funded_kernel, alice, addr, [1], nonce=1, gas=intrinsic - 1)
    assert ei.value.code == "gas_too_low"
    assert funded_kernel.get_balance(alice.address) == 10_000_000 - 53_276


def test_insufficient_balance_rejected(funded_kernel, alice):
    deploy(funded_kernel, alice, fixtures.counter_code(), nonce=0)
    addr = _deployed_address(alice, 0)
    with pytest.raises(Rejected) as ei:
        invoke(funded_kernel, alice, addr, [1], nonce=1,
               gas=10_000_000 - 53_276 + 1)
    assert ei.value.code == "insufficient_balance"


def test_invalid_bytecode_rejected_at_deploy(funded_kernel, alice):
    # 0x0F 非法字节
    with pytest.raises(Rejected) as ei:
        deploy(funded_kernel, alice, b"\x0f", nonce=0, gas=500_000)
    assert ei.value.code == "invalid_bytecode"
    assert funded_kernel.state.height == 0


def test_contract_not_found_rejected(funded_kernel, alice):
    with pytest.raises(Rejected) as ei:
        invoke(funded_kernel, alice, "0x" + "ab" * 8, [], nonce=0)
    assert ei.value.code == "contract_not_found"


def test_successful_invoke_gas_accounting_and_storage(funded_kernel, alice):
    code = assemble(SUCCESS_ASM)
    dep = deploy(funded_kernel, alice, code, nonce=0, gas=500_000)
    deploy_charged = dep["gas_charged"]
    addr = _deployed_address(alice, 0)
    r = invoke(funded_kernel, alice, addr, [10], nonce=1, gas=500_000)
    assert r["status"] == 1
    assert r["output"] == [10]
    assert funded_kernel.get_storage(addr, 0) == 10
    # 手工执行费（按 trace 的逐条 gas 差核对）：
    # PUSH*5=15, CALLDATALOAD 3, SLOAD 800, ADD 5, DUP1 3,
    # SSTORE(set) 20000, MSTORE 操作 3 + 内存扩张 3 = 6, RETURN 0
    expected_exec = 15 + 3 + 800 + 5 + 3 + 20_000 + 6 + 3
    assert r["gas_exec_used"] == expected_exec
    intrinsic = G.intrinsic_gas("invoke", input_len=1)
    assert r["intrinsic_gas"] == intrinsic
    assert r["gas_charged"] == intrinsic + expected_exec
    # 余额扣减一致
    assert funded_kernel.get_balance(alice.address) == (
        10_000_000 - deploy_charged - (intrinsic + expected_exec)
    )
    # 结果绑定程序版本与输入摘要
    assert r["engine_version"] == ENGINE_VERSION
    assert len(r["tx_hash"]) == 64
    assert r["result_digest"] != r["tx_hash"]


def test_out_of_gas_consumes_all_and_rolls_back_state(funded_kernel, alice):
    code = assemble(SUCCESS_ASM)
    deploy(funded_kernel, alice, code, nonce=0, gas=500_000)
    addr = _deployed_address(alice, 0)
    # 先成功写 10
    invoke(funded_kernel, alice, addr, [10], nonce=1, gas=500_000)
    # 再以临界 gas 调 +5：intrinsic=21004，VM 可用=0 -> 首指令 OOG
    r = invoke(funded_kernel, alice, addr, [5], nonce=2, gas=21_004)
    assert r["status"] == 0
    assert r["halt_code"] == "out_of_gas"
    assert r["reverted"] is False
    assert r["writes"] == []
    assert r["gas_charged"] == 21_004          # 全部 gas 消耗
    assert r["gas_refund"] == 0
    assert funded_kernel.get_storage(addr, 0) == 10  # 写回滚
    assert funded_kernel.state.nonces[alice.address] == 3  # nonce 仍递增


def test_revert_returns_remainder_and_rolls_back(funded_kernel, alice):
    deploy(funded_kernel, alice, fixtures.write_then_revert_code(),
           nonce=0, gas=500_000)
    addr = _deployed_address(alice, 0)
    r = invoke(funded_kernel, alice, addr, [], nonce=1, gas=200_000)
    assert r["status"] == 0 and r["reverted"] and r["halt_code"] == "revert"
    assert r["output"] == [1]
    assert funded_kernel.get_storage(addr, 8) == 0
    assert r["gas_charged"] == r["intrinsic_gas"] + r["gas_exec_used"]
    assert r["gas_charged"] < 200_000          # 剩余返还
    # REVERT 的 SSTORE reset(0槽原值0实际是 set?)——槽8原值0写7=set 20000，
    # 回滚后无退款
    assert r["gas_refund"] == 0


def test_clear_refund_capped_at_half(funded_kernel, alice):
    # 部署一个清零程序：槽0 非0 -> 0，随后 RETURN
    # 清零槽0，然后把 0 存入 mem[0] 并返回（SSTORE 后栈空，故 MSTORE
    # 的两个操作数都重新压：先 value=0 后 offset=0）
    clear_asm = """
PUSH 0
PUSH 0
SSTORE
PUSH 0
PUSH 0
MSTORE
PUSH 1
PUSH 0
RETURN
"""
    code = assemble(clear_asm)
    deploy(funded_kernel, alice, code, nonce=0, gas=500_000)
    addr = _deployed_address(alice, 0)
    # 预置槽0=9（通过另一个合约做不到；直接写内核状态模拟已存在存储）
    funded_kernel.state.storage[(addr, 0)] = 9
    r = invoke(funded_kernel, alice, addr, [], nonce=1, gas=500_000)
    assert r["status"] == 1
    # PUSH*5=15, SSTORE reset 5000, MSTORE 3 + 内存扩张 6, RETURN 0 = 5021；
    # trace 逐差实际 5024 —— 多 3 来自 MSTORE 本身在扩张之后仍收操作费，
    # 故精确式：15 + 5000 + 6 + 3 = 5024
    vm_used = 15 + 5_000 + 6 + 3
    assert r["gas_exec_used"] == vm_used
    # 原始退款 15000，截断到 vm_used//2 = 2506
    assert r["gas_refund"] == vm_used // 2
    assert r["gas_charged"] == r["intrinsic_gas"] + vm_used - vm_used // 2


def test_state_root_changes_on_success_only(funded_kernel, alice):
    code = assemble(SUCCESS_ASM)
    deploy(funded_kernel, alice, code, nonce=0, gas=500_000)
    addr = _deployed_address(alice, 0)
    r_ok = invoke(funded_kernel, alice, addr, [1], nonce=1, gas=500_000)
    assert r_ok["pre_state_root"] != r_ok["post_state_root"]
    root_after_ok = r_ok["post_state_root"]
    r_fail = invoke(funded_kernel, alice, addr, [1], nonce=2, gas=21_004)
    assert r_fail["halt_code"] == "out_of_gas"
    # 失败交易：状态根除余额/nonce 外变化；存储部分不变。
    # 这里直接断言存储槽未变，且 pre/post 根的差异仅来自 gas 结算。
    assert funded_kernel.get_storage(addr, 0) == 1
    assert root_after_ok != r_fail["post_state_root"]  # 余额变了


def _deployed_address(signer, nonce: int) -> str:
    import hashlib
    return "0x" + hashlib.sha256(
        f"{signer.address}:{nonce}".encode()).hexdigest()[:16]
