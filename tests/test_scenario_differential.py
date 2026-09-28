"""独立夹具 + 独立预言机的差分测试入口。

每个 ``fixtures/*.yaml`` 都被加载执行：
* 手写 ``expect_*`` 检查点断言具体状态/顺序/错误类别；
* 每一步与不共享核心代码的 :mod:`offline.oracle` 差分比对。
这层不允许读取被测核心的输出来"自己证明自己"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_txpool.offline.scenario import (
    ScenarioAssertionError,
    ScenarioRunner,
    load_scenario,
)

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"
SCENARIO_FILES = sorted(FIXTURE_DIR.glob("*.yaml"))


@pytest.mark.parametrize(
    "scenario_path",
    SCENARIO_FILES,
    ids=[p.stem for p in SCENARIO_FILES],
)
def test_scenario_matches_handwritten_expectations_and_oracle(scenario_path):
    scenario = load_scenario(scenario_path)
    try:
        run = ScenarioRunner(scenario).execute()
    except ScenarioAssertionError:
        pytest.fail(f"夹具 {scenario_path.name} 检查点失败", pytrace=False)
    assert len(run.reports) == len(scenario["steps"])


def test_oracle_and_kernel_disagree_would_be_caught():
    """护栏测试：人为篡改期望时差分/检查点必须报错（验证测试本身有效）。"""
    scenario = {
        "name": "negative_guard",
        "config": {"block_gas_limit": 3_000_000},
        "transactions": {
            "x0": {"signer": "x", "nonce": 0, "gas_price": 2_000_000_000},
        },
        "steps": [
            {"step": "fund", "account": "x", "balance": 10**18},
            {"step": "submit", "tx": "x0", "expect_accepted": False},
        ],
    }
    with pytest.raises(ScenarioAssertionError):
        ScenarioRunner(scenario).execute()
