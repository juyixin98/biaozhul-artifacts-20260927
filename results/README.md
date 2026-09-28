# 可复核运行结果

由 `scripts/run_all.sh` 及其中的 CLI 命令生成；任何复核者都可删除本目录后重新生成。

- `pytest.txt` — `pytest -v` 完整输出（62 passed）。
- `<scenario>.replay.txt` — 离线回放 stdout（逐块结果、回滚区间、最终余额）。
- `<scenario>.report.json` — 结构化回放报告（决策、派生事件、账户快照）。
- `verify_short_fork.txt` — 在线库与全量重建库一致性比对（consistent: true）。

对应夹具：`fixtures/{short_fork,deep_fork,interrupt}/`。
