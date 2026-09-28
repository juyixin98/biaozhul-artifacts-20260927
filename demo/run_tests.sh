#!/usr/bin/env bash
# 执行全部测试并把结果落盘到 demo/out/test_report.txt。
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p demo/out
REPORT="$ROOT/demo/out/test_report.txt"

echo "弱迹包含检查 — 测试报告" | tee "$REPORT"
echo "时间：$(date -Is)" | tee -a "$REPORT"
echo "rustc：$(rustc --version)" | tee -a "$REPORT"
echo "cargo：$(cargo --version)" | tee -a "$REPORT"
echo | tee -a "$REPORT"

# --nocapture 让 panic 详情也进报告；RUST_BACKTRACE 便于复现。
RUST_BACKTRACE=1 cargo test -- --nocapture 2>&1 | tee -a "$REPORT"
status=${PIPESTATUS[0]}

echo | tee -a "$REPORT"
if [ "$status" -eq 0 ]; then
  echo "结论：全部测试通过" | tee -a "$REPORT"
else
  echo "结论：存在失败测试（退出码 $status）" | tee -a "$REPORT"
fi
exit "$status"
