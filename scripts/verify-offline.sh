#!/usr/bin/env bash
# 本地离线验证脚本：构建、全量测试（含竞态检测）、夹具端到端回放。
# 不访问网络：GOPROXY=off 强制只使用本机模块缓存，GOSUMDB 关闭。
set -euo pipefail

cd "$(dirname "$0")/.."

export GOPROXY=off
export GOSUMDB=off
export GOFLAGS=-mod=readonly
# 用绝对路径，保证 go test 各包工作目录下都汇聚到工程根的 test-results/。
PROJECT_ROOT="$(pwd)"
export REASM_TEST_REPORT_DIR="${REASM_TEST_REPORT_DIR:-$PROJECT_ROOT/test-results}"
mkdir -p "$REASM_TEST_REPORT_DIR"

echo "== [1/5] go version =="
go version

echo "== [2/5] go vet =="
go vet ./...

echo "== [3/5] 全量测试（-race，可重复运行）=="
go test ./... -race -count=1 -v 2>&1 | tee "$REASM_TEST_REPORT_DIR/go-test-verbose.log" \
  | grep -E '^(=== RUN|--- (PASS|FAIL|SKIP)|PASS|FAIL|ok )' || true

echo "== [4/5] 构建 CLI =="
go build -o /tmp/reasm ./cmd/reasm
/tmp/reasm version

echo "== [5/5] 合成夹具端到端回放（不发送任何网络报文）=="
rm -rf /tmp/reasm-fixtures
SCENARIOS="ordered permutation duplicate overlap conflict-last timeout-reuse"
for s in $SCENARIOS; do
  /tmp/reasm fixture -outdir "/tmp/reasm-fixtures/$s" -scenario "$s" -size 60 >/dev/null
done

check_kind() { # scenario expected_kind
  local scenario="$1" want="$2"
  local got
  got=$(/tmp/reasm replay -pcap "/tmp/reasm-fixtures/$scenario/capture.pcap" -timeout 500ms -after 2s \
    | python3 -c 'import sys,json; r=json.load(sys.stdin); ks=[x.get("error_kind") for x in r["results"] if x.get("error_kind")]; print(ks[0] if ks else "COMPLETE")')
  if [ "$got" != "$want" ]; then
    echo "FAIL: $scenario 期望 $want 实际 $got"; exit 1
  fi
  echo "  OK  $scenario -> $got"
}

check_state() { # scenario expected_terminal_state
  local scenario="$1" want="$2"
  local got
  got=$(/tmp/reasm replay -pcap "/tmp/reasm-fixtures/$scenario/capture.pcap" -timeout 500ms -after 2s \
    | python3 -c 'import sys,json; r=json.load(sys.stdin); st=[x.get("state") for x in r["results"] if x.get("state")]; print(st[-1] if st else "NONE")')
  if [ "$got" != "$want" ]; then
    echo "FAIL: $scenario 期望末态 $want 实际 $got"; exit 1
  fi
  echo "  OK  $scenario -> $got"
}

check_state ordered complete
check_state permutation complete
check_state duplicate complete
check_kind overlap overlap_group_rejected
check_kind conflict-last conflicting_last_fragment

# timeout-reuse：第一轮必须被记录为超时回收，第二轮完成。
/tmp/reasm replay -pcap /tmp/reasm-fixtures/timeout-reuse/capture.pcap -timeout 500ms -after 2s \
  | python3 -c '
import sys,json
r=json.load(sys.stdin)
assert len(r.get("sweep_timed_out",[]))==1, r.get("sweep_timed_out")
states=[x.get("state") for x in r["results"] if x.get("state")]
assert states[-1]=="complete", states
print("  OK  timeout-reuse -> timed_out once then complete")
'

echo
echo "全部本地验证通过。JSONL 判定报告见 $REASM_TEST_REPORT_DIR/*.jsonl"
