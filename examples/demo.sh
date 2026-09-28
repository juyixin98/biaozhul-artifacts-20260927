#!/usr/bin/env bash
# End-to-end demonstration of the local infrastructure planner against a
# running server. Starts the server itself if one is not reachable.
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8080}"
DB="${DB:-./data/demo.db}"
LOGDIR="${LOGDIR:-./data/demo-logs}"

started_here=0
if ! curl -sf "$BASE/healthz" >/dev/null 2>&1; then
  echo "[demo] building and starting server on $BASE"
  rm -f "$DB"
  BIN=./data/planner-demo
  go build -o "$BIN" ./cmd/server
  "$BIN" --addr "${BASE#http://}" --db "$DB" --logdir "$LOGDIR" --echo=false \
    >./data/demo-server.out 2>&1 &
  SRV_PID=$!
  started_here=1
  trap 'kill '"$SRV_PID"' 2>/dev/null || true' EXIT
  for _ in $(seq 1 50); do
    curl -sf "$BASE/healthz" >/dev/null 2>&1 && break
    sleep 0.2
  done
fi

echo; echo "== 0. reset the simulated world =="
curl -s -X POST "$BASE/v1/admin/reset" | jq .

echo; echo "== 1. plan + apply a fresh stack (dependency-ordered create) =="
PLAN=$(curl -s "$BASE/v1/plans" -d @examples/create.json)
echo "$PLAN" | jq '{run_id, ops: [.operations[] | {seq,type,key:.key.name}]}'
RID=$(echo "$PLAN" | jq -r .run_id)
curl -s -X POST "$BASE/v1/runs/$RID/apply" \
  | jq '{state, completed: .completed_ops, ids: [.results[] | {key:.key.name, id:.physical_id}]}'

echo; echo "== 2. immutable change (vpc cidr) cascades replacement; mutable change updates =="
PLAN=$(curl -s "$BASE/v1/plans" -d @examples/replace_and_update.json)
echo "$PLAN" | jq '{ops: [.operations[] | {seq,type,key:.key.name}]}'
RID=$(echo "$PLAN" | jq -r .run_id)
curl -s -X POST "$BASE/v1/runs/$RID/apply" | jq '{state, completed: .completed_ops}'

echo; echo "== 3. critical-resource guard: deleting a protected vpc is blocked =="
curl -s -X POST "$BASE/v1/admin/seed" -d '{
  "resources":[{"key":{"kind":"vpc","name":"critical"},"id":"id-critical",
    "attrs":{"cidr":"10.9/16","region":"east"},"protected":true}]}' >/dev/null
echo "-- plan that deletes 'critical' (empty resources), no release:"
curl -s "$BASE/v1/plans" -d '{"resources":[]}' | jq '.error // .'

echo; echo "== 4. same plan with explicit guard release -> deletes succeed =="
PLAN=$(curl -s "$BASE/v1/plans" -d '{
  "resources":[],
  "release_guards":[{"kind":"vpc","name":"critical"}]}')
RID=$(echo "$PLAN" | jq -r .run_id)
curl -s -X POST "$BASE/v1/runs/$RID/apply" | jq '{state, completed: .completed_ops}'

echo; echo "== 5. drift between plan and apply is rejected =="
curl -s -X POST "$BASE/v1/admin/reset" >/dev/null
PLAN=$(curl -s "$BASE/v1/plans" -d @examples/create.json); RID=$(echo "$PLAN" | jq -r .run_id)
# external actor mutates the world after planning, before applying
curl -s -X POST "$BASE/v1/admin/seed" -d '{
  "resources":[{"key":{"kind":"vpc","name":"intruder"},"id":"id-intruder",
    "attrs":{"cidr":"10.255/16","region":"north"}}]}' >/dev/null
curl -s -X POST "$BASE/v1/runs/$RID/apply" | jq '.error // {state}'

echo; echo "== 6. create success-response lost -> adopted, never duplicated =="
curl -s -X POST "$BASE/v1/admin/reset" >/dev/null
PLAN=$(curl -s "$BASE/v1/plans" -d '{"resources":[
  {"kind":"vpc","name":"lonely","attrs":{"cidr":"10.5/16","region":"east"}}]}')
RID=$(echo "$PLAN" | jq -r .run_id)
curl -s -X POST "$BASE/v1/admin/faults" -d '{
  "kind":"create_commit_response_lost",
  "target":{"kind":"vpc","name":"lonely"},"remaining":1}' >/dev/null
curl -s -X POST "$BASE/v1/runs/$RID/apply" \
  | jq '{state, completed: .completed_ops, id: .results[0].physical_id}'
echo "-- live world must contain exactly ONE vpc/lonely:"
curl -s "$BASE/v1/live" | jq '[.resources[] | select(.key.name=="lonely")] | length'
echo "-- recovery evidence (adopt decision):"
curl -s "$BASE/v1/runs/$RID/evidence" | jq '[.evidence[] | select(.kind=="decision")]'

echo; echo "== 7. replay the run log (run sequence + decisions) =="
ls -1 "$LOGDIR/$RID.jsonl" 2>/dev/null && jq -r '[.run_seq,.stage,.msg] | @tsv' "$LOGDIR/$RID.jsonl" | tail -8 || true

if [ "$started_here" = "1" ]; then
  echo; echo "[demo] stopping server"
  kill "${SRV_PID:-}" 2>/dev/null || true
fi
