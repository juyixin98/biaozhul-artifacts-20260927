#!/usr/bin/env bash
# 端到端演示：启动 routed（临时内存→文件 SQLite + 种子夹具），
# 逐项验证默认路由/主机路由/重叠前缀/IPv6 压缩/递归环/批量原子可见/回放。
# 仅依赖本机 curl 与 jq（缺失时退回 python3 做 JSON 美化）。
set -euo pipefail

cd "$(dirname "$0")/.."
DB="data/demo-$$.db"
ADDR="127.0.0.1:18080"
BIN="./bin/routed"
SEED="configs/seed.example.json"
trap 'kill "${PID:-0}" 2>/dev/null || true; rm -f "$DB" "$DB-wal" "$DB-shm"' EXIT

RIB_SQLITE_DSN="file:${DB}?cache=shared&_pragma=busy_timeout(5000)" \
RIB_LISTEN_ADDR="$ADDR" RIB_LOG_LEVEL=warn \
  "$BIN" -seed "$SEED" >/tmp/rib-demo.log 2>&1 &
PID=$!

# 等待健康检查就绪。
for _ in $(seq 1 50); do
  if curl -fsS "http://$ADDR/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
req() { # method path [json body]
  local m="$1" p="$2" b="${3:-}"
  if [ -n "$b" ]; then
    curl -fsS -X "$m" -H 'Content-Type: application/json' -H 'X-Request-ID: demo' \
      --data "$b" "http://$ADDR$p"
  else
    curl -fsS -H 'X-Request-ID: demo' "http://$ADDR$p"
  fi
}
pp() { if command -v jq >/dev/null 2>&1; then jq .; else python3 -m json.tool; fi; }

say "1) 健康检查"
req GET /healthz | pp

say "2) 主机路由（AD=200 很差）压过默认路由（AD=255），并递归到直连"
req GET "/v1/lookup?target=10.10.10.1" | pp

say "3) 重叠前缀：/16 命中并递归到同一出口"
req GET "/v1/lookup?target=10.10.20.20" | pp

say "4) 默认路由兜底（无更具体覆盖）"
req GET "/v1/lookup?target=198.51.100.77" | pp

say "5) IPv6 非压缩/大写目标等价命中，匹配链以压缩形式打印"
req GET "/v1/lookup?target=2001:DB8:0009:0000:0000:0000:0000:0001" | pp

say "6) IPv6 主机路由黑洞（终止类别）"
req GET "/v1/lookup?target=2001:db8:2::1" | pp

say "7) 制造三跳递归环：10.0.0.0/8 -> 10.1.0.0/16 -> 10.1.2.0/24 -> 10.0.0.1"
req POST /v1/routes '{"id":"lp1","prefix":"10.0.0.0/8","admin_distance":5,"metric":0,"protocol":"static","nexthop":{"kind":"address","address":"10.1.0.1"}}' >/dev/null
req POST /v1/routes '{"id":"lp2","prefix":"10.1.0.0/16","admin_distance":5,"metric":0,"protocol":"static","nexthop":{"kind":"address","address":"10.1.2.1"}}' >/dev/null
req POST /v1/routes '{"id":"lp3","prefix":"10.1.2.0/24","admin_distance":5,"metric":0,"protocol":"static","nexthop":{"kind":"address","address":"10.0.0.1"}}' >/dev/null
req GET "/v1/lookup?target=10.1.2.3" | pp

say "8) 跨族下一跳必须被拒绝（地址族隔离，返回具体失败类别）"
curl -sS -o /tmp/rib-af.json -w 'http_status=%{http_code}\n' -X POST \
  -H 'Content-Type: application/json' \
  --data '{"id":"bad","prefix":"10.0.0.0/8","admin_distance":1,"nexthop":{"kind":"address","address":"2001:db8::1"}}' \
  "http://$ADDR/v1/routes"
pp </tmp/rib-af.json

say "9) 批量替换：同一表版本原子可见（一次 replace_all 事件）"
req POST /v1/routes/replace '{"v4":[
  {"id":"bulk-a","prefix":"172.16.0.0/12","admin_distance":0,"protocol":"connected","nexthop":{"kind":"connected","interface":"bond0"}},
  {"id":"bulk-b","prefix":"0.0.0.0/0","admin_distance":1,"protocol":"static","nexthop":{"kind":"connected","interface":"wan0"}}
],"v6":[]}' | pp

say "10) 旧表条目已被整批替换清除"
req GET "/v1/lookup?target=10.10.10.1" | pp

say "11) 事件日志回放并与当前表逐项对照"
req POST /v1/replay '{}' | pp

say "全部演示调用完成（见上）。"
