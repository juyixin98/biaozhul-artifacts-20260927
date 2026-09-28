#!/usr/bin/env bash
# 离线复现（不需要网络/服务）：对每个夹具用 CLI 回放并重建核对，
# 把可复核结果写入 test-results/。正常场景退出码 0，重建不一致退出码 2。
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p test-results data
export PYTHONPATH=src
PY=.venv/bin/python

echo ">> 重新生成确定性夹具（密钥固定，结果应与仓库内 fixtures 逐字节一致）"
$PY -m reorgindex.cli generate-fixtures fixtures > test-results/regenerate.json

for name in short_fork_wins deep_fork_rejected duplicate_tx_across_fork pending_drain; do
  echo ">> 离线回放: ${name}"
  db="data/${name}.sqlite3"
  rm -f "$db" "$db-wal" "$db-shm"
  set +e
  $PY -m reorgindex.cli replay "$db" "fixtures/${name}.jsonl" \
      > "test-results/${name}.replay.json" 2> "test-results/${name}.diag.log"
  code=$?
  set -e
  echo "   replay exit=${code} (rebuild 不一致时为 2，正常为 0)"
done

echo; echo ">> 运行完整 pytest，结果同时写入 test-results/pytest.txt"
set +e
$PY -m pytest tests/ -q 2>&1 | tee test-results/pytest.txt
code=${PIPESTATUS[0]}
set -e
echo "pytest exit=${code}"
exit "$code"
