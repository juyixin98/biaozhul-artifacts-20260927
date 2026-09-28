"""pytest 配置：把可重放运行日志写到 test_runs.jsonl。

每条收敛记录含 run_id、输入、关键中间状态、判定与理由，失败时
AssertionError 内附 run_id，可据此重放。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_LOG_PATH = os.environ.get("OT_TEST_LOG", "test_runs.jsonl")


def pytest_configure(config):
    # 每个测试会话开始时清空日志，避免多次运行混淆
    if os.environ.get("OT_TEST_LOG_KEEP") != "1":
        Path(_LOG_PATH).write_text("", encoding="utf-8")
