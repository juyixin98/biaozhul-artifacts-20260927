#!/usr/bin/env bash
# Local, dependency-free demo of the rolling-release controller.
#
# It builds the single binary, starts it in MANUAL tick mode against a fresh
# SQLite file, and drives every scenario over HTTP with curl. Logical time
# advances only when the script calls POST /admin/tick, so every step is
# reproducible and inspectable.
#
# Usage:
#   scripts/demo.sh [happy|startfail|flap|capacity|restart|all]
#
# Requires: go (toolchain), curl. jq is optional (used for pretty-printing).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="$(mktemp -d)"
BIN="$WORK_DIR/rollctl"
PORT="${PORT:-18080}"
BASE="http://127.0.0.1:$PORT"
DB="$WORK_DIR/demo.db"   # reset per scenario (capacity slots are process-global)
PID=""

c_blue=$'\033[0;34m'; c_green=$'\033[0;32m'; c_red=$'\033[0;31m'; c_yellow=$'\033[0;33m'; c_off=$'\033[0m'
say()  { printf '%s\n' "${c_blue}== $*${c_off}"; }
ok()   { printf '%s\n' "${c_green}OK $*${c_off}"; }
warn() { printf '%s\n' "${c_yellow}>> $*${c_off}"; }
err()  { printf '%s\n' "${c_red}!! $*${c_off}"; }

pp() { if command -v jq >/dev/null 2>&1; then jq .; else cat; fi; }

# req METHOD PATH [JSON BODY] [REQUEST ID]
req() {
  local method="$1" path="$2" body="${3:-}" rid="${4:-demo-req}"
  local args=(-sS -X "$method" "$BASE$path" -H "X-Request-Id: $rid")
  if [[ -n "$body" ]]; then
    args+=(-H "Content-Type: application/json" -d "$body")
  fi
  curl "${args[@]}"
}

build() {
  say "building binary"
  (cd "$ROOT" && go build -o "$BIN" ./cmd/rollctl)
  ok "built $BIN"
}

start_server() {
  local capacity="${1:-16}"
  say "starting server on $BASE (db=$DB, sim capacity=$capacity, manual ticks)"
  "$BIN" -manual -http "127.0.0.1:$PORT" -db "$DB" -sim-capacity "$capacity" \
      >"$WORK_DIR/server.log" 2>&1 &
  PID=$!
  trap cleanup EXIT
  for _ in $(seq 1 50); do
    if curl -sf "$BASE/healthz" >/dev/null 2>&1; then ok "server up"; return; fi
    sleep 0.1
  done
  err "server failed to start; log:"; cat "$WORK_DIR/server.log"; exit 1
}

stop_server() {
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    say "stopping server (pid $PID)"
    kill -INT "$PID" 2>/dev/null || true
    for _ in $(seq 1 30); do kill -0 "$PID" 2>/dev/null || break; sleep 0.1; done
    kill -0 "$PID" 2>/dev/null && kill -9 "$PID" || true
  fi
  PID=""
}
cleanup() { stop_server; }

tick() { req POST /admin/tick '' "tick-$RANDOM" >/dev/null; }

# json_field FIELD < json  (tiny grep/sed extractor, no jq dependency)
json_field() { grep -o "\"$1\"[^,}]*" | head -1 | sed 's/^"[^"]*"[[:space:]]*:[[:space:]]*//; s/^"//; s/"$//'; }

wait_field() { # METHOD PATH FIELD EXPECT MAX-TICKS
  local method="$1" path="$2" field="$3" want="$4" max="${5:-120}"
  for _ in $(seq 1 "$max"); do
    tick
    local val; val="$(req "$method" "$path" '' 'wait' | json_field "$field")"
    [[ "$val" == "$want" ]] && return 0
  done
  err "timed out waiting for $field=$want at $path"; return 1
}

behavior() { # NAME REV JSON
  req PUT "/admin/simulator/workloads/$1/revisions/$2/behavior" "$3" 'behave' >/dev/null
}

POLICY='{"maxSurge":1,"maxUnavailable":0,"readyThresholdTicks":2,"deadlineTicks":60,"maxStartFailures":0}'

settle() { # NAME REPLICAS
  say "ticking until $1 has $2 available replicas (bootstrap/steady state)"
  wait_field GET "/api/v1/workloads/$1" available "$2"
  req GET "/api/v1/workloads/$1" '' 'status' | pp
}

rollout_until() { # NAME RELEASE-ID EXPECTED-STATE
  say "ticking release $2 to terminal state (expect $3)"
  for _ in $(seq 1 160); do
    tick
    local state; state="$(req GET "/api/v1/releases/$2" '' 'poll' | json_field state)"
    if [[ "$state" == "succeeded" || "$state" == "failed" ]]; then
      echo
      req GET "/api/v1/workloads/$1/events" '' 'events' | pp
      echo
      req GET "/api/v1/releases/$2" '' 'final' | pp
      [[ "$state" == "$3" ]] || { err "expected $3, got $state"; return 1; }
      return 0
    fi
  done
  err "release never terminated"; return 1
}

scenario_happy() {
  say "scenario: healthy rolling update (maxSurge=1, maxUnavailable=0)"
  behavior demo v1 '{"mode":"normal","readyDelayTicks":1}'
  behavior demo v2 '{"mode":"normal","readyDelayTicks":1}'
  req POST /api/v1/workloads \
    "{\"name\":\"demo\",\"replicas\":3,\"revision\":\"v1\",\"policy\":$POLICY}" 'create' | pp
  settle demo 3
  local rid; rid="$(req POST /api/v1/workloads/demo/releases '{"revision":"v2"}' 'roll-v2' | json_field id)"
  ok "created release $rid — a created instance is NOT ready until 2 consecutive ready ticks"
  rollout_until demo "$rid" succeeded
  ok "workload fully on v2; check the event stream: starts, readiness, then one old removal at a time"
}

scenario_startfail() {
  say "scenario: NEW INSTANCE START FAILURE (start rejected by process manager)"
  behavior sf v1 '{"mode":"normal"}'
  behavior sf bad '{"mode":"start_rejected"}'
  req POST /api/v1/workloads \
    "{\"name\":\"sf\",\"replicas\":2,\"revision\":\"v1\",\"policy\":$POLICY}" 'create-sf' >/dev/null
  settle sf 2
  local rid; rid="$(req POST /api/v1/workloads/sf/releases '{"revision":"bad"}' 'roll-bad' | json_field id)"
  warn "the manager rejects every start; maxStartFailures=0 => release must fail as start_failed"
  rollout_until sf "$rid" failed
  warn "note failureCategory=start_failed and currentRevision stays v1 (old revision keeps serving)"
}

scenario_flap() {
  say "scenario: READINESS JITTER (ready oscillates, never holds for threshold)"
  behavior fl v1 '{"mode":"normal"}'
  behavior fl v2 '{"mode":"flap","readyDelayTicks":1,"flapReadyTicks":1,"flapDownTicks":1}'
  req POST /api/v1/workloads \
    '{"name":"fl","replicas":2,"revision":"v1","policy":{"maxSurge":1,"maxUnavailable":0,"readyThresholdTicks":3,"deadlineTicks":8}}' 'create-fl' >/dev/null
  settle fl 2
  local rid; rid="$(req POST /api/v1/workloads/fl/releases '{"revision":"v2"}' 'roll-flap' | json_field id)"
  warn "readiness streak keeps resetting; after the deadline the release fails as readiness_flapping"
  rollout_until fl "$rid" failed
}

scenario_capacity() {
  say "scenario: INSUFFICIENT CAPACITY fixture (manager holds 3 slots, baseline=3, wants surge)"
  req PUT /admin/simulator/capacity '{"capacity":3}' 'setcap3' >/dev/null
  warn "global process-manager capacity lowered to 3 for this fixture"
  behavior cap v1 '{"mode":"normal"}'
  behavior cap v2 '{"mode":"normal"}'
  req POST /api/v1/workloads \
    "{\"name\":\"cap\",\"replicas\":3,\"revision\":\"v1\",\"policy\":$POLICY}" 'create-cap' >/dev/null
  settle cap 3
  local rid; rid="$(req POST /api/v1/workloads/cap/releases '{"revision":"v2"}' 'roll-cap' | json_field id)"
  warn "manager refuses the 4th process; removing an old one would break maxUnavailable=0"
  rollout_until cap "$rid" failed
  warn "failureCategory=insufficient_capacity; blocked_capacity events are certain=false (transient)"
  req PUT /admin/simulator/capacity '{"capacity":16}' 'setcap16' >/dev/null
  ok "capacity restored to 16"
}

scenario_restart() {
  say "scenario: CONTROLLER RESTART mid rollout (same SQLite file)"
  behavior rt v1 '{"mode":"normal","readyDelayTicks":1}'
  behavior rt v2 '{"mode":"normal","readyDelayTicks":1}'
  req POST /api/v1/workloads \
    "{\"name\":\"rt\",\"replicas\":3,\"revision\":\"v1\",\"policy\":$POLICY}" 'create-rt' >/dev/null
  settle rt 3
  local rid; rid="$(req POST /api/v1/workloads/rt/releases '{"revision":"v2"}' 'roll-rt' | json_field id)"
  say "advancing 4 ticks, then killing the process mid-release"
  for _ in 1 2 3 4; do tick; done
  req GET "/api/v1/releases/$rid" '' 'pre-restart' | pp
  req GET /api/v1/workloads/rt '' 'pre-restart-status' | pp
  stop_server
  warn "process killed; state is only in $DB"
  start_server 16
  ok "server reattached; same release is still active and tick continues"
  req GET "/api/v1/workloads/rt" '' 'post-restart' | pp
  rollout_until rt "$rid" succeeded
  ok "rollout resumed after restart and converged on v2"
  say "release history survived the restart:"
  req GET /api/v1/workloads/rt/releases '' 'history' | pp
}

main() {
  local which="${1:-all}"
  build

  # run_scenario NAME CAPACITY FUNC: fresh SQLite file per scenario, because
  # simulated process slots are process-global and a failed fixture would
  # otherwise leave slots occupied for later scenarios.
  run_scenario() {
    local name="$1" capacity="$2" fn="$3"
    say "──────── scenario database: $WORK_DIR/$name.db ────────"
    stop_server
    DB="$WORK_DIR/$name.db"
    rm -f "$DB" "$DB-wal" "$DB-shm"
    start_server "$capacity"
    "$fn"
    stop_server
    say "state for [$name] preserved at $DB (open it with sqlite3 to inspect)"
  }

  case "$which" in
    happy)     run_scenario happy     16 scenario_happy ;;
    startfail) run_scenario startfail 16 scenario_startfail ;;
    flap)      run_scenario flap      16 scenario_flap ;;
    capacity)  run_scenario capacity  16 scenario_capacity ;;
    restart)   run_scenario restart   16 scenario_restart ;;
    all)
      run_scenario happy     16 scenario_happy
      run_scenario startfail 16 scenario_startfail
      run_scenario flap      16 scenario_flap
      run_scenario capacity  16 scenario_capacity
      run_scenario restart   16 scenario_restart
      ;;
    *) echo "unknown scenario: $which" >&2; exit 2 ;;
  esac
  say "demo complete; databases and server logs kept in $WORK_DIR"
  trap - EXIT
}

main "$@"
