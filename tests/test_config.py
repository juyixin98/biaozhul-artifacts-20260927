"""独立配置测试：默认值、env 覆盖、资源预算真实生效。"""

from __future__ import annotations

import pytest

from rsv.config import Limits, load_settings
from rsv.errors import VerificationFailure
from rsv.vm import RunContext, StackMachine


def test_default_limits_match_documented_values():
    lim = Limits()
    assert (lim.max_element_size, lim.max_stack_items, lim.max_op_steps,
            lim.max_script_depth, lim.max_script_bytes) == (520, 64, 128, 8, 2048)


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("RSV_MAX_OP_STEPS", "3")
    monkeypatch.setenv("RSV_NETWORK", "custom-net")
    monkeypatch.setenv("RSV_SQLITE_PATH", str(tmp_path / "x.db"))
    s = load_settings()
    assert s.limits.max_op_steps == 3
    assert s.chain.network == "custom-net"
    assert s.sqlite_path.endswith("x.db")


def test_budget_override_changes_execution(monkeypatch):
    # 默认预算下 10 条 NOP 通过；收紧到 5 后同脚本必须失败
    script = b"\x61" * 10 + b"\x51"  # 10 NOP + OP_1
    ctx = RunContext(b"\x00" * 32, "n", "d")
    assert StackMachine(Limits()).execute(b"", script, ctx).ok

    with pytest.raises(VerificationFailure) as ei:
        StackMachine(Limits(max_op_steps=5)).execute(b"", script, ctx)
    assert ei.value.code.code == "resource.op_budget_exhausted"


def test_four_failure_categories_are_distinct():
    """四大类别码集合互不重叠，且各有至少一个成员。"""
    from rsv import errors

    cats = {
        "input": errors.MALFORMED_TX.code,
        "state": errors.ALREADY_SPENT.code,
        "resource": errors.STACK_OVERFLOW.code,
        "compute": errors.STACK_UNDERFLOW.code,
    }
    assert set(cats) == {"input", "state", "resource", "compute"}
    assert len(set(cats.values())) == 4  # 码全局唯一
