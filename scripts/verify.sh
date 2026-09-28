#!/usr/bin/env bash
# 一键验证：格式 → clippy → 单元/集成测试 → 独立夹具复核 → HTTP 冒烟。
# 任何一步失败立即以非零码退出；输出带步骤标题，便于定位。
set -euo pipefail
cd "$(dirname "$0")/.."

step() { printf '\n========== %s ==========\n' "$1"; }

step "1/5 cargo fmt --check"
cargo fmt --all -- --check

step "2/5 cargo clippy (all targets, -D warnings)"
cargo clippy --all-targets -- -D warnings 2>&1 | tail -5
cargo clippy --all-targets --tests -- -D warnings 2>&1 | tail -5

step "3/5 cargo test"
cargo test --no-fail-fast 2>&1 | grep -E "test result|running [0-9]+ test"

step "4/5 独立夹具复核 (Python 稀疏映射全扫描)"
python3 scripts/check_fixtures.py

step "5/5 HTTP 端到端冒烟 (真 TCP)"
bash scripts/http_smoke.sh 127.0.0.1:18080 > /tmp/pr2d-smoke.log 2>&1 || {
  echo "smoke failed; tail of log:"; tail -40 /tmp/pr2d-smoke.log; exit 1;
}
tail -3 /tmp/pr2d-smoke.log

step "测试复现日志位置"
echo "target/pr2d-test-logs/<run-id>/*.jsonl（每次 cargo test 一个 run id）"
echo "保留失败临时数据目录: PR2D_KEEP_TMP=1 cargo test"

printf '\nVERIFY OK\n'
