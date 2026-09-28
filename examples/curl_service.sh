#!/usr/bin/env bash
# 服务调用示例：health → bootstrap → verify(dry-run) → submit → 双花被拒 → utxos → 离线 replay。
# 用法：先启动服务（.venv/bin/rsv-serve 或 python -m rsv.api.app），再运行本脚本。
set -u
BASE=${RSV_BASE:-http://127.0.0.1:8791}

echo "== health =="
curl -s "$BASE/health" | python3 -m json.tool

echo "== bootstrap =="
curl -s -X POST "$BASE/bootstrap" \
  -H 'content-type: application/json' \
  --data @fixtures/genesis.json | python3 -m json.tool

echo "== dry-run verify（不改状态，可重复）=="
RSV_BASE="$BASE" python3 - <<'PY'
import json, os, urllib.request
base = os.environ["RSV_BASE"]
tx = json.load(open("fixtures/bundles/double_spend.json"))["transactions"][0]
req = urllib.request.Request(base + "/tx/verify",
                             data=json.dumps(tx).encode(),
                             headers={"content-type": "application/json"})
print(json.dumps(json.load(urllib.request.urlopen(req)), indent=2, ensure_ascii=False))
PY

echo "== submit：同一 outpoint 连交两次（先接受，后 state.already_spent）=="
RSV_BASE="$BASE" python3 - <<'PY'
import json, os, urllib.request
base = os.environ["RSV_BASE"]
b = json.load(open("fixtures/bundles/double_spend.json"))
for i in (0, 1):
    req = urllib.request.Request(base + "/tx/submit",
                                 data=json.dumps(b["transactions"][i]).encode(),
                                 headers={"content-type": "application/json"})
    resp = json.load(urllib.request.urlopen(req))
    print(f"tx#{i+1}: accepted={resp['accepted']} run_id={resp.get('run_id')} "
          f"failure={resp.get('failure')}")
PY

echo "== utxos / state root =="
curl -s "$BASE/utxos" | python3 -c "import json,sys; d=json.load(sys.stdin); print('count=',d['count'],'root=',d['state_root'])"

echo "== 离线 replay（不经服务，内存状态）=="
PYTHONPATH=src .venv/bin/python -m rsv.scripts_cli fixtures/bundles/failure_catalog.json \
  --runs-dir runs/example-replay > /tmp/rsv_replay_out.json
python3 - <<'PY'
import json
d = json.load(open("/tmp/rsv_replay_out.json"))
print("accepted:", d["accepted_count"], "rejected:", d["rejected_count"])
print("final root:", d["final_state_root"])
for r in d["results"]:
    if "failure" in r:
        print(" seq", r["seq"], "->", r["failure"]["category"], r["failure"]["code"])
    elif r.get("phase") == "bootstrap":
        print(" seq 0 -> bootstrap", r.get("genesis_id", "")[:12])
    else:
        print(" seq", r["seq"], "-> accepted")
PY
