#!/usr/bin/env bash
# 本地合成夹具演示：嵌套 + 重叠 + 跨块 + 二进制 + 版本边界 + 分页。
# 用法：bash scripts/demo.sh（要求服务已启动于 127.0.0.1:${AC_PORT:-8080}）
set -euo pipefail
BASE="http://127.0.0.1:${AC_PORT:-8080}"
j() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d$1)"; }

echo "▶ 版本 v1：嵌套/重叠模式 a, aa, aaa（base64 传输）"
P_A=$(printf 'a'   | base64); P_AA=$(printf 'aa'  | base64); P_AAA=$(printf 'aaa' | base64)
V1=$(curl -s -X POST "$BASE/versions" -H 'Content-Type: application/json' \
  -d "{\"encoding\":\"base64\",\"patterns\":[{\"id\":\"a\",\"data\":\"$P_A\"},{\"id\":\"aa\",\"data\":\"$P_AA\"},{\"id\":\"aaa\",\"data\":\"$P_AAA\"}]}" \
  | j '["version_id"]')
echo "  v1=$V1"

echo "▶ 打开流会话，按 1 字节块喂入 'aaaa'"
SID=$(curl -s -X POST "$BASE/sessions" -H 'Content-Type: application/json' \
  -d "{\"version_id\":\"$V1\"}" | j '["sid"]')
for ch in a a a a; do
  curl -s -X POST "$BASE/sessions/$SID/feed" -H 'Content-Type: application/json' \
    -d "{\"data\":\"$ch\"}" >/dev/null
done

echo "▶ 分页读取（limit=4，共 9 条命中），逐页跟随 next_cursor"
BASE="$BASE" SID="$SID" python3 - <<'PY'
import json, os, urllib.request
base, sid = os.environ["BASE"], os.environ["SID"]
cursor, total, pages = None, 0, 0
while True:
    url = f"{base}/sessions/{sid}/hits?limit=4"
    if cursor:
        url += f"&cursor={cursor}"
    page = json.load(urllib.request.urlopen(url))
    total += len(page["items"])
    pages += 1
    cursor = page["next_cursor"]
    if not cursor:
        break
print(f"  聚合命中数={total}，共 {pages} 页（朴素期望 9）")
assert total == 9
PY

echo "▶ 二进制一次性匹配：在 00 ff de ad be ef 00 中找 deadbeef 与 00"
H=$(printf '\xde\xad\xbe\xef' | base64); N=$(printf '\x00' | base64); D=$(printf '\x00\xff\xde\xad\xbe\xef\x00' | base64)
curl -s -X POST "$BASE/match" -H 'Content-Type: application/json' \
  -d "{\"encoding\":\"base64\",\"data\":\"$D\",\"patterns\":[{\"id\":\"hdr\",\"data\":\"$H\"},{\"id\":\"nul\",\"data\":\"$N\"}]}" \
  | python3 -c 'import sys,json;print(" ",sorted((h["pattern_id"],h["start"],h["end"]) for h in json.load(sys.stdin)["hits"]))'

echo "▶ 空模式必须被拒绝（EMPTY_PATTERN / reject）"
curl -s -X POST "$BASE/versions" -H 'Content-Type: application/json' \
  -d '{"patterns":[{"id":"empty","data":""}]}' \
  | python3 -c 'import sys,json;e=json.load(sys.stdin)["error"];print(" ",e["code"],e["outcome"])'

echo "▶ 诊断：该会话的 accept/reject 记录条数"
curl -s "$BASE/diagnostics?sid=$SID" | python3 -c 'import sys,json;print(" ",json.load(sys.stdin)["total"]," 条")'
echo "✔ 演示完成"
