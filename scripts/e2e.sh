#!/usr/bin/env bash
# e2e.sh — 端到端验证脚本：构建 → 夹具 → 独立 Python 验证 → HTTP 服务实测。
#
# 不依赖任何外部服务或账号；数据目录与端口均使用临时/本地资源，退出时清理。
#
# 用法：
#   ./scripts/e2e.sh
#   RB_PORT=18099 RB_BIND=127.0.0.1 ./scripts/e2e.sh
#
# 退出码：0 全部通过；非 0 表示有断言失败（失败行会以 FAIL 开头并列出期望/实际）。

set -u
set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${RB_PORT:-18099}"
HOST="${RB_HOST:-127.0.0.1}"
BASE="http://${HOST}:${PORT}"
WORK="$(mktemp -d -t rbs-e2e-XXXXXX)"
DATA_DIR="$WORK/data"
mkdir -p "$DATA_DIR"

PASS=0
FAIL=0
SERVER_PID=""

cleanup() {
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    rm -rf "$WORK"
}
trap cleanup EXIT

log()  { printf '\033[0;36m[step]\033[0m %s\n' "$*"; }
ok()   { PASS=$((PASS + 1)); printf '\033[0;32mPASS\033[0m %s\n' "$*"; }
bad()  { FAIL=$((FAIL + 1)); printf '\033[0;31mFAIL\033[0m %s\n' "$*"; }

# assert_eq <描述> <期望> <实际>
assert_eq() {
    local desc="$1" want="$2" got="$3"
    if [ "$want" = "$got" ]; then
        ok "$desc (= $got)"
    else
        bad "$desc: expected [$want], got [$got]"
    fi
}

# jget <json> <jq 表达式>
jget() { printf '%s' "$1" | jq -r "$2" ; }

# curl_json METHOD PATH [JSON_BODY] [REQUEST_ID]
curl_json() {
    local method="$1" path="$2" body="${3:-}" rid="${4:-e2e-rid-001}"
    if [ -n "$body" ]; then
        curl -s -o "$WORK/resp.json" -w '%{http_code}' \
            -H "content-type: application/json" -H "x-request-id: $rid" \
            -X "$method" --data "$body" "$BASE$path"
    else
        curl -s -o "$WORK/resp.json" -w '%{http_code}' \
            -H "x-request-id: $rid" -X "$method" "$BASE$path"
    fi
}

# ---------------- 1. 构建与单元/集成测试 ----------------

log "cargo build (offline)"
cargo build --offline --workspace >"$WORK/build.log" 2>&1 \
    && ok "workspace builds offline" \
    || { bad "cargo build failed (see $WORK/build.log)"; tail -20 "$WORK/build.log"; exit 1; }

log "cargo test (workspace)"
cargo test --offline --workspace --all-features -- --skip set_operations_match_oracle_all_fixture_pairs >"$WORK/test.log" 2>&1
if [ $? -eq 0 ]; then
    ok "cargo test workspace"
else
    bad "cargo test failed (see $WORK/test.log)"; tail -40 "$WORK/test.log"
fi
# 重夹具配对测试单独运行（约 1 分钟），不跳过。
cargo test --offline -p rb-format --all-features set_operations_match_oracle_all_fixture_pairs >>"$WORK/test.log" 2>&1 \
    && ok "fixture-pair oracle test" \
    || bad "fixture-pair oracle test failed"

# ---------------- 2. 夹具与独立 Python 验证 ----------------

log "generate fixtures with rbtool"
FIXDIR="fixtures"
cargo run -q --offline -p rbtool -- gen-fixtures "$FIXDIR" >"$WORK/fix.log" 2>&1 \
    && ok "fixtures regenerated" \
    || { bad "rbtool gen-fixtures failed"; cat "$WORK/fix.log"; }

log "independent python verifier (RBS parser + rank/select + json cross-check)"
if python3 scripts/verify.py "$FIXDIR" >"$WORK/verify.log" 2>&1; then
    n=$(grep -c '^PASS' "$WORK/verify.log")
    ok "python verifier: $n fixture files"
else
    bad "python verifier reported failures"; cat "$WORK/verify.log"
fi

log "python corruption-classifier tests"
if python3 scripts/test_corruption.py >"$WORK/corrupt.log" 2>&1; then
    n=$(grep -c '^PASS' "$WORK/corrupt.log")
    ok "python corruption tests: $n cases"
else
    bad "python corruption tests failed"; cat "$WORK/corrupt.log"
fi

# rbtool inspect 与损坏拒绝（Rust 工具侧错误码）
log "rbtool inspect good vs corrupt"
RBTOOL="cargo run -q --offline -p rbtool --"
if $RBTOOL inspect "$FIXDIR/bitmap_just_above.rbs" >"$WORK/inspect.log" 2>&1; then
    grep -q 'kind=bitmap' "$WORK/inspect.log" \
        && ok "rbtool inspect reports bitmap container" \
        || bad "rbtool inspect missing bitmap kind"
else
    bad "rbtool inspect rejected a valid file"
fi
cp "$FIXDIR/sparse_tiny.rbs" "$WORK/broken.rbs"
printf '\xff' | dd of="$WORK/broken.rbs" bs=1 seek=0 count=1 conv=notrunc status=none
if $RBTOOL inspect "$WORK/broken.rbs" 2>"$WORK/inspect-err.log"; then
    bad "rbtool inspect accepted corrupted magic"
else
    grep -q 'corrupt_bad_magic' "$WORK/inspect-err.log" \
        && ok "rbtool inspect rejects corrupt magic with code" \
        || bad "rbtool inspect error missing stable code: $(cat "$WORK/inspect-err.log")"
fi

# ---------------- 3. 启动 HTTP 服务 ----------------

log "start rb-server on $BASE (data=$DATA_DIR)"
RB_DATA_DIR="$DATA_DIR" RB_BIND="$HOST:$PORT" RB_LOG=info \
    cargo run -q --offline -p rb-server >"$WORK/server.log" 2>&1 &
SERVER_PID=$!

for i in $(seq 1 50); do
    if curl -s -o /dev/null "$BASE/healthz"; then break; fi
    sleep 0.2
done
STATUS=$(curl_json GET /healthz "" "e2e-health")
[ "$STATUS" = "200" ] && ok "healthz 200" || { bad "healthz status $STATUS"; cat "$WORK/resp.json"; exit 1; }
RID_HDR=$(curl -s -D - -o /dev/null -H 'x-request-id: trace-xyz' "$BASE/healthz" | tr -d '\r' | awk -F': ' 'tolower($1)=="x-request-id"{print $2}')
assert_eq "request id echoed as header" "trace-xyz" "$RID_HDR"

# ---------------- 4. 阈值容器与具体结果 ----------------

log "create sets straddling the 4096 threshold"
ARR_BODY=$(jq -nc '{values: [range(0;4096)]}')
STATUS=$(curl_json POST "/v1/sets?name=arr" "$ARR_BODY")
assert_eq "create arr(4096)" "201" "$STATUS"
assert_eq "arr container kind" "array" "$(jget "$(cat "$WORK/resp.json")" '.result.container_kinds | keys_unsorted[] as $k | select(.[$k]==1) | $k')"

BIT_BODY=$(jq -nc '{values: [range(0;5000) | (. * 11) % 65536]}')
STATUS=$(curl_json POST "/v1/sets?name=bit" "$BIT_BODY")
assert_eq "create bit(5000)" "201" "$STATUS"
assert_eq "bit cardinality" "5000" "$(jget "$(cat "$WORK/resp.json")" '.result.cardinality')"
assert_eq "bit container kind" "bitmap" "$(jget "$(cat "$WORK/resp.json")" '.result.container_kinds | keys_unsorted[] as $k | select(.[$k]==1) | $k')"

# 交错两个集合，断言并/交/差具体基数
A='{"values":[1,2,3,10,100,65536]}'
B='{"values":[2,3,4,200,65536,65537]}'
curl_json POST "/v1/sets?name=a" "$A" >/dev/null
curl_json POST "/v1/sets?name=b" "$B" >/dev/null

curl_json POST "/v1/sets/a/union" '{"with":"b"}' >/dev/null
assert_eq "|a∪b|" "9" "$(jget "$(cat "$WORK/resp.json")" '.result.cardinality')"
curl_json POST "/v1/sets/a/intersect" '{"with":"b"}' >/dev/null
assert_eq "|a∩b|" "3" "$(jget "$(cat "$WORK/resp.json")" '.result.cardinality')"
curl_json POST "/v1/sets/a/difference" '{"with":"b"}' >/dev/null
assert_eq "|a∖b|" "3" "$(jget "$(cat "$WORK/resp.json")" '.result.cardinality')"

# rank/select 具体值
curl_json GET "/v1/sets/a/rank/65536" >/dev/null
assert_eq "rank_a(65536)" "5" "$(jget "$(cat "$WORK/resp.json")" '.result.rank')"
curl_json GET "/v1/sets/a/select/5" >/dev/null
assert_eq "select_a(5)" "65536" "$(jget "$(cat "$WORK/resp.json")" '.result.value')"

# ---------------- 5. 失败类别与请求身份 ----------------

log "error categories"
STATUS=$(curl_json GET "/v1/sets/missing")
assert_eq "missing set status" "404" "$STATUS"
assert_eq "missing set code" "set_not_found" "$(jget "$(cat "$WORK/resp.json")" '.errors[0].code')"
assert_eq "error body request id" "e2e-rid-001" "$(jget "$(cat "$WORK/resp.json")" '.request_id')"

STATUS=$(curl_json POST "/v1/sets?name=arr" '{"values":[],"expect_new":true}')
assert_eq "expect_new conflict" "409" "$STATUS"
assert_eq "conflict code" "already_exists" "$(jget "$(cat "$WORK/resp.json")" '.errors[0].code')"

STATUS=$(curl_json PUT "/v1/sets/bad%2Fname" '{"values":[]}')
assert_eq "invalid name status" "400" "$STATUS"
assert_eq "invalid name code" "invalid_name" "$(jget "$(cat "$WORK/resp.json")" '.errors[0].code')"

# ---------------- 6. 磁盘损坏 → 422 + 细分类别（无缓存新进程视角） ----------------

log "corrupt persisted file under the running server"
cp "$FIXDIR/dense_container.rbs" "$DATA_DIR/dense.rbs"
STATUS=$(curl_json GET "/v1/sets/dense")
assert_eq "dense loads initially" "200" "$STATUS"

# 截断文件后再用新 request id（避开内存缓存：该文件此前未经 API 加载）
head -c 100 "$DATA_DIR/dense.rbs" > "$DATA_DIR/dense2.rbs"
STATUS=$(curl_json GET "/v1/sets/dense2" "" "e2e-corrupt-trunc")
assert_eq "truncated file status" "422" "$STATUS"
assert_eq "truncated file code" "corrupt_truncated" "$(jget "$(cat "$WORK/resp.json")" '.errors[0].code')"
assert_eq "corrupt response request id" "e2e-corrupt-trunc" "$(jget "$(cat "$WORK/resp.json")" '.request_id')"

# ---------------- 汇总 ----------------

echo
echo "================ E2E SUMMARY ================"
echo "PASS=$PASS FAIL=$FAIL"
if [ "$FAIL" -ne 0 ]; then
    echo "Server log tail:"
    tail -20 "$WORK/server.log" || true
    exit 1
fi
echo "All end-to-end checks passed. Artifacts kept in: $WORK (auto-removed on exit)"
