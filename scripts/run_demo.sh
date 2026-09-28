#!/usr/bin/env bash
# 本地演示脚本：启动真实服务，用合成夹具复现各类发布场景，
# 打印逐步副本约束、最终版本与失败类别。无需任何外部账号。
#
# 用法:
#   scripts/run_demo.sh [happy|start-failure|jitter|capacity|rollback|restart]
#
# 依赖: bash, go(构建), curl, python3(JSON 解析，标准库)。
set -uo pipefail

SCENARIO="${1:-happy}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# 每个场景默认用独立端口，避免上一个场景保留运行的服务占用端口；
# 用户仍可用 PORT=xxxx 覆盖。
if [ -z "${PORT:-}" ]; then
  case "$SCENARIO" in
    happy)         PORT=18090;;
    start-failure) PORT=18091;;
    jitter)        PORT=18092;;
    capacity)      PORT=18093;;
    rollback)      PORT=18094;;
    restart)       PORT=18095;;
    *)             PORT=18099;;
  esac
fi
WORK="$(mktemp -d -t rctl-demo-XXXXXX)"
BASE="http://127.0.0.1:${PORT}"
REQ="demo-$$"
trap 'kill "${SERVER_PID:-0}" 2>/dev/null || true' EXIT

log() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
say() { printf '   %s\n' "$*"; }

# jget <python-expr on json from stdin>
jget() { python3 -c 'import json,sys
d=json.load(sys.stdin)
try:
    v='"$1"'
except Exception:
    v=""
print("" if v is None else v)'; }

build_config() {
  local cap="$1"; shift
  local beh="$1" # python dict literal
  cat > "$WORK/config.json" <<JSON
{
  "http_addr": "127.0.0.1:${PORT}",
  "tick_interval": "250ms",
  "data_dir": "${WORK}/data",
  "default_max_surge": 1,
  "default_max_unavailable": 0,
  "default_ready_threshold": 2,
  "default_failure_limit": 2,
  "default_progress_ticks": 80,
  "fixture": {"capacity": ${cap}, "behaviors": ${beh}}
}
JSON
}

start_server() {
  log "构建并启动服务 (data=$WORK)"
  go build -o "$WORK/server" ./cmd/server >/dev/null
  "$WORK/server" --config "$WORK/config.json" >"$WORK/server.log" 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 1 100); do
    curl -sf "$BASE/healthz" >/dev/null 2>&1 && break
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      echo "服务进程提前退出（端口 ${PORT} 可能被占用），日志见 $WORK/server.log"; cat "$WORK/server.log"; exit 1
    fi
    sleep 0.1
  done
  if ! curl -sf "$BASE/healthz" >/dev/null; then
    echo "服务未就绪，日志见 $WORK/server.log"; cat "$WORK/server.log"; exit 1
  fi
  say "服务就绪 pid=$SERVER_PID，完整日志: $WORK/server.log"
}

create_app() { # name version replicas [extra json fields]
  local extra="${4:-}"
  curl -s -X POST "$BASE/api/apps" -H "X-Request-Id: ${REQ}-create" \
    -H 'Content-Type: application/json' \
    -d "{\"name\":\"$1\",\"version\":\"$2\",\"replicas\":$3${extra:+,$extra}}"
}
deploy() { # name version [extra json fields]
  local extra="${3:-}"
  curl -s -X POST "$BASE/api/apps/$1/deployments" -H "X-Request-Id: ${REQ}-deploy" \
    -H 'Content-Type: application/json' \
    -d "{\"version\":\"$2\"${extra:+,$extra}}"
}
rollback() { curl -s -X POST "$BASE/api/apps/$1/rollback" -H "X-Request-Id: ${REQ}-rb"; }

wait_terminal() { # name
  local name="$1" i st
  for i in $(seq 1 200); do
    st="$(curl -s "$BASE/api/apps/$name" | jget 'd["status"]')"
    case "$st" in succeeded|failed) echo "$st"; return;; esac
    sleep 0.25
  done
  echo "TIMEOUT"
}

print_app() { # name
  curl -s "$BASE/api/apps/$1" | python3 -c '
import json,sys
d=json.load(sys.stdin)
s=d["snapshot"]
print("   当前版本:", d.get("current_version"), "| 状态:", d["status"],
      "| 失败类别:", d.get("failure_category") or "-")
print("   快照: 活跃=%d (新=%d 旧=%d) 可用=%d (新=%d 旧=%d) 上界=%d 最小可用=%d" % (
  s["total_active"], s["new_active"], s["old_active"], s["available"],
  s["new_available"], s["old_available"], s["max_total"], s["min_available"]))
for i in d["instances"]:
    print("     - %-22s version=%-4s phase=%-10s available=%s" % (
      i["id"], i["version"], i["phase"], i["available"]))
if d.get("failure_reason"):
    print("   失败原因:", d["failure_reason"])'
}

print_timeline() { # rollout_id
  curl -s "$BASE/api/rollouts/$1/events" | python3 -c '
import json,sys
for e in json.load(sys.stdin):
    s=e["snapshot"]
    if s.get("tick",0)==0:
        print("   [初始化] %-18s %s" % (e["kind"], e["note"][:70])); continue
    print("   tick=%-3d %-18s 活跃=%d 可用=%d(新%d/旧%d) req=%s %s" % (
        s["tick"], e["kind"], s["total_active"], s["available"],
        s["new_available"], s["old_available"], e["request_id"], e["note"][:48]))'
}

last_rollout() { curl -s "$BASE/api/apps/$1" | jget 'd["last_rollout"]["id"]'; }

case "$SCENARIO" in
  happy)
    build_config 0 '{"v1":{"behavior":"always_ok"},"v2":{"behavior":"always_ok"}}'
    start_server
    log "1) 创建应用 demo v1, 3 副本"
    create_app demo v1 3 >/dev/null
    [ "$(wait_terminal demo)" = succeeded ] || { echo "v1 上线失败"; exit 1; }
    print_app demo
    log "2) 滚动发布 v2（先扩新、持续 2 次探针就绪后再缩旧）"
    deploy demo v2 >/dev/null
    st="$(wait_terminal demo)"; say "终态: $st"
    print_app demo
    print_timeline "$(last_rollout demo)"
    ;;
  start-failure)
    build_config 0 '{"v1":{"behavior":"always_ok"},"v2":{"behavior":"fail_start"}}'
    start_server
    log "1) 创建应用 sf v1, 3 副本"
    create_app sf v1 3 '"failure_limit":2,"ready_threshold":1' >/dev/null
    wait_terminal sf >/dev/null
    log "2) 发布 v2（夹具: 启动即失败）"
    deploy sf v2 '"failure_limit":2,"ready_threshold":1' >/dev/null
    st="$(wait_terminal sf)"; say "终态: $st（期望 failed / start_failure）"
    print_app sf
    print_timeline "$(last_rollout sf)"
    ;;
  jitter)
    build_config 0 '{"v1":{"behavior":"always_ok"},"v2":{"behavior":"flaky","parameter":3}}'
    start_server
    log "1) 创建应用 jt v1, 2 副本"
    create_app jt v1 2 '"failure_limit":9,"ready_threshold":2' >/dev/null
    wait_terminal jt >/dev/null
    log "2) 发布 v2（夹具: 每 3 次探针抖动 1 次，需连续 2 次成功就绪）"
    deploy jt v2 '"failure_limit":9,"ready_threshold":2,"progress_ticks":120' >/dev/null
    st="$(wait_terminal jt)"; say "终态: $st（期望最终 succeeded，中途有 ready_demoted）"
    print_app jt
    print_timeline "$(last_rollout jt)" | sed -n '1,40p'
    ;;
  capacity)
    build_config 3 '{"v1":{"behavior":"always_ok"},"v2":{"behavior":"always_ok"}}'
    start_server
    log "1) 创建应用 cap v1, 3 副本（容量池上限=3）"
    create_app cap v1 3 '"ready_threshold":1,"failure_limit":9,"progress_ticks":12' >/dev/null
    wait_terminal cap >/dev/null
    log "2) 发布 v2（容量池已满且 maxUnavailable=0，无法回收旧实例）"
    deploy cap v2 '"ready_threshold":1,"failure_limit":9,"progress_ticks":12' >/dev/null
    st="$(wait_terminal cap)"; say "终态: $st（期望 failed / insufficient_capacity）"
    print_app cap
    print_timeline "$(last_rollout cap)"
    ;;
  rollback)
    build_config 0 '{"v1":{"behavior":"always_ok"},"v2":{"behavior":"fail_start"}}'
    start_server
    log "1) v1 上线"
    create_app rb v1 2 '"failure_limit":1,"ready_threshold":1' >/dev/null
    wait_terminal rb >/dev/null
    log "2) v2 发布失败"
    deploy rb v2 '"failure_limit":1,"ready_threshold":1' >/dev/null
    wait_terminal rb >/dev/null
    print_app rb
    log "3) 回退（作为一次全新 rollout，历史保留）"
    rollback rb | jget '"rollback=%s op=%s" % (d["rollout_id"], d["op"])'
    st="$(wait_terminal rb)"; say "回退终态: $st"
    print_app rb
    say "发布历史（create / rollout:failed / rollback:succeeded 全部保留）:"
    curl -s "$BASE/api/apps/rb/rollouts" | python3 -c '
import json,sys
for r in json.load(sys.stdin):
    print("     - %-14s %-9s version=%-4s status=%-9s category=%s" % (
        r["op"], r["id"], r["version"], r["status"], r.get("failure_category") or "-"))'
    ;;
  restart)
    build_config 0 '{"v1":{"behavior":"always_ok"},"v2":{"behavior":"always_ok"}}'
    start_server
    log "1) v1 上线，随后发起 v2 发布"
    create_app rs v1 3 >/dev/null
    wait_terminal rs >/dev/null
    deploy rs v2 >/dev/null
    sleep 0.8
    before_active="$(curl -s "$BASE/api/apps/rs" | jget 'd["snapshot"]["total_active"]')"
    before_procs="$(curl -s "$BASE/api/debug/procman/list" | jget 'len(d)')"
    log "2) 杀掉控制器进程 ($SERVER_PID)，模拟控制器重启"
    kill "$SERVER_PID"; wait "$SERVER_PID" 2>/dev/null || true
    say "重启前 活跃实例=$before_active 模拟进程=$before_procs"
    "$WORK/server" --config "$WORK/config.json" >"$WORK/server2.log" 2>&1 &
    SERVER_PID=$!
    for _ in $(seq 1 100); do curl -sf "$BASE/healthz" >/dev/null 2>&1 && break; sleep 0.1; done
    after_active="$(curl -s "$BASE/api/apps/rs" | jget 'd["snapshot"]["total_active"]')"
    after_procs="$(curl -s "$BASE/api/debug/procman/list" | jget 'len(d)')"
    say "重启后 活跃实例=$after_active 模拟进程=$after_procs（应与重启前一致，无重复创建）"
    st="$(wait_terminal rs)"; say "继续推进终态: $st"
    print_app rs
    print_timeline "$(last_rollout rs)" | sed -n '1,40p'
    ;;
  *)
    echo "未知场景: $SCENARIO"; echo "可选: happy | start-failure | jitter | capacity | rollback | restart"
    exit 2;;
esac

log "完成。停止服务；数据与日志保留在: $WORK"
kill "${SERVER_PID:-0}" 2>/dev/null || true
wait "${SERVER_PID:-0}" 2>/dev/null || true
trap - EXIT
