#!/usr/bin/env bash
# 端到端演示：创建管理器 -> 构建（文本/AST/等价语法）-> apply -> restrict
# -> evaluate -> 等价查询（等价/见证/重命名）-> GC 保留根 -> 跨管理器拒绝
# -> 敏感请求脱敏。输出为真实响应，供 README 引用。
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"
j() { python3 -m json.tool --no-ensure-ascii; }

echo "== 1) health =="
curl -s "$BASE/healthz" | j
echo

echo "== 2) create manager(a,b,c) =="
MID=$(curl -s -X POST "$BASE/v1/managers" \
  -H 'content-type: application/json' \
  -d '{"variable_order":["a","b","c"]}' | tee /tmp/r2.json | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["manager_id"])')
cat /tmp/r2.json | j
echo "manager_id=$MID"
echo

echo "== 3) build  mux = (a & b) | (!a & c)  as root 'mux' =="
curl -s -X POST "$BASE/v1/managers/$MID/build" -H 'content-type: application/json' \
  -d '{"expr":"(a & b) | (!a & c)","root_name":"mux"}' | j
echo

echo "== 4) same function, different syntax (!a|b)&(a|c) — canonical edge must be equal =="
curl -s -X POST "$BASE/v1/managers/$MID/build" -H 'content-type: application/json' \
  -d '{"expr":"(!a | b) & (a | c)"}' | j
echo

echo "== 5) apply: mux xor a  (root 'mux_xor_a') =="
MUX=$(curl -s "$BASE/v1/managers/$MID/roots?include_edge=true" | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["roots"][0]["edge"])')
AEDGE=$(curl -s -X POST "$BASE/v1/managers/$MID/build" -H 'content-type: application/json' -d '{"expr":"a"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["edge"])')
curl -s -X POST "$BASE/v1/managers/$MID/apply" -H 'content-type: application/json' \
  -d "{\"op\":\"xor\",\"a\":\"$MUX\",\"b\":\"$AEDGE\",\"root_name\":\"mux_xor_a\"}" | j
echo

echo "== 6) restrict mux with a=true  =>  c =="
curl -s -X POST "$BASE/v1/managers/$MID/restrict" -H 'content-type: application/json' \
  -d "{\"edge\":\"$MUX\",\"values\":{\"a\":true}}" | j
echo

echo "== 7) evaluate mux at a=0,b=0,c=1 (expect true: !a&c) =="
curl -s -X POST "$BASE/v1/managers/$MID/evaluate" -H 'content-type: application/json' \
  -d "{\"edge\":\"$MUX\",\"assignment\":{\"a\":false,\"b\":false,\"c\":true}}" | j
echo

echo "== 8) equivalence: equivalent pair, oracle + kernel cross-check =="
curl -s -X POST "$BASE/v1/equivalence" -H 'content-type: application/json' \
  -d @samples/equiv_equivalent.json | j
echo

echo "== 9) equivalence: non-equivalent pair returns concrete witness =="
curl -s -X POST "$BASE/v1/equivalence" -H 'content-type: application/json' \
  -d @samples/equiv_witness.json | j
echo

echo "== 10) equivalence: rename x,y,z -> p,q,r =="
curl -s -X POST "$BASE/v1/equivalence" -H 'content-type: application/json' \
  -d @samples/equiv_renamed.json | j
echo

echo "== 11) garbage collection keeps roots =="
curl -s -X POST "$BASE/v1/managers/$MID/build" -H 'content-type: application/json' \
  -d '{"expr":"a ^ b ^ c"}' >/dev/null
curl -s -X POST "$BASE/v1/managers/$MID/gc" | j
echo

echo "== 12) sensitive build with unknown variable: raw secret must not appear =="
curl -s -X POST "$BASE/v1/managers/$MID/build" -H 'content-type: application/json' \
  -d @samples/sensitive_build.json | j
echo

echo "== 13) cross-manager edge rejection =="
MID2=$(curl -s -X POST "$BASE/v1/managers" -H 'content-type: application/json' \
  -d '{"variable_order":["a","b"]}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["manager_id"])')
E2=$(curl -s -X POST "$BASE/v1/managers/$MID2/build" -H 'content-type: application/json' -d '{"expr":"a & b"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["edge"])')
curl -s -X POST "$BASE/v1/managers/$MID/apply" -H 'content-type: application/json' \
  -d "{\"op\":\"and\",\"a\":\"$MUX\",\"b\":\"$E2\"}" | j
echo
echo "demo complete"
