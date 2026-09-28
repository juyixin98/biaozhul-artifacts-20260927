"""切换中断测试：在"撤旧完成、加新之前"注入故障，验证事务整体回滚，
派生索引仍完整呈现旧链版本（查询只见完整链，无混合中间态）。
"""

import pytest

from reorgindex import reference

from .conftest import make_tx


def _fork_with_injection(env, make, genesis, keys):
    storage, kernel, _ = env
    kernel.ingest(genesis)
    _, alice_pub, alice = keys["alice"]
    _, bob_pub, bob = keys["bob"]

    a1 = make(genesis["header"]["block_hash"], 1,
              [make_tx(keys["alice"][0], alice_pub, alice, 10, 0)], 1)
    a2 = make(a1["header"]["block_hash"], 2,
              [make_tx(keys["alice"][0], alice_pub, alice, 20, 1)], 1)
    kernel.ingest(a1)
    kernel.ingest(a2)
    f2 = make(a1["header"]["block_hash"], 2,
              [make_tx(keys["alice"][0], alice_pub, bob, 5, 2)], 3)
    return storage, kernel, (a1, a2, f2), (alice, bob)


def test_crash_after_disconnect_rolls_back_to_old_chain(env, make, genesis, keys):
    storage, kernel, (a1, a2, f2), (alice, bob) = _fork_with_injection(env, make, genesis, keys)

    call_count = {"n": 0}

    def crash_hook(conn, disconnected, connected):
        call_count["n"] += 1
        # 此刻旧链贡献已撤、新链未加；强制事务回滚
        raise RuntimeError("模拟磁盘故障：切换中断")

    kernel.mid_switch_hook = crash_hook
    with pytest.raises(RuntimeError, match="切换中断"):
        kernel.ingest(f2)
    assert call_count["n"] == 1

    # 链尖仍是旧块 a2；权威链与余额完全是旧链版本
    assert kernel.tip_hash() == a2["header"]["block_hash"]
    assert kernel.canonical_chain() == [
        genesis["header"]["block_hash"], a1["header"]["block_hash"], a2["header"]["block_hash"]]
    assert kernel.get_balance(alice) == 30
    assert kernel.get_balance(bob) == 0
    # 新块 f2 的事务整体回滚：blocks / 派生表都没有它
    assert storage.get_block(f2["header"]["block_hash"]) is None
    assert storage.contribution_count() == 2  # a1、a2 两笔
    # 没有留下半切换的 reorg 记录
    assert storage.recent_reorgs() == []


def test_retry_after_crash_succeeds_and_switches(env, make, genesis, keys):
    storage, kernel, (a1, a2, f2), (alice, bob) = _fork_with_injection(env, make, genesis, keys)

    def flaky_hook(conn, disconnected, connected):
        if flaky_hook.calls == 0:
            flaky_hook.calls += 1
            raise RuntimeError("第一次切换中断")

    flaky_hook.calls = 0
    kernel.mid_switch_hook = flaky_hook
    with pytest.raises(RuntimeError):
        kernel.ingest(f2)

    # 第二次提交同一区块，移除故障：应正常切换
    kernel.mid_switch_hook = None
    out = kernel.ingest(f2)
    assert out.status == "switched"
    assert kernel.tip_hash() == f2["header"]["block_hash"]
    assert kernel.get_balance(alice) == 10
    assert kernel.get_balance(bob) == 5


def test_no_derived_rows_visible_mid_switch(env, make, genesis, keys):
    """切换进行中通过同一连接观察：撤旧后、加新前，余额处于"空窗口"，
    但该窗口只存在于未提交事务内；事务回滚后外部读者只见旧链。"""

    storage, kernel, (a1, a2, f2), (alice, bob) = _fork_with_injection(env, make, genesis, keys)
    observed = {}

    def observe_hook(conn, disconnected, connected):
        # 用同一事务连接读取：旧链 a2 贡献刚撤回；分叉点 a1 的贡献保留。
        rows = conn.execute("SELECT COUNT(*) FROM derived_contributions").fetchone()
        observed["inside_txn_contributions"] = rows[0]
        alice_bal = conn.execute(
            "SELECT balance FROM derived_balances WHERE address=?", (alice,)).fetchone()
        observed["inside_txn_alice"] = alice_bal[0] if alice_bal else 0
        raise RuntimeError("中断")

    kernel.mid_switch_hook = observe_hook
    with pytest.raises(RuntimeError):
        kernel.ingest(f2)
    # 事务内确实经历了"撤旧未加新"的窗口（只剩分叉点 a1 的 10），证明先撤后加；
    # 但该窗口从未对外可见，事务回滚后恢复旧链。
    assert observed["inside_txn_contributions"] == 1
    assert observed["inside_txn_alice"] == 10
    assert storage.contribution_count() == 2
    assert kernel.get_balance(alice) == 30
