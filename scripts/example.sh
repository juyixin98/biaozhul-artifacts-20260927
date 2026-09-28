#!/usr/bin/env bash
# 端到端示例：启动服务后运行本脚本，演示
#   注资 -> 签名提交(多账户费用竞争) -> 候选预览 -> 提议 -> 确认 -> 查账/流水 -> 回滚
#
# 依赖：bash、curl、已激活的虚拟环境（提供 localtxpool 用于本地签名合成）。
# 用法：./scripts/example.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://127.0.0.1:8000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=(python3); [ -x "$HERE/.venv/bin/python" ] && PY=("$HERE/.venv/bin/python")
export PYTHONPATH="$HERE/src${PYTHONPATH:+:$PYTHONPATH}"

jget() { "${PY[@]}" -c "import sys,json;d=json.load(sys.stdin);print(eval('d'+sys.argv[1]))" "$1"; }

# 1) 用确定性合成密钥生成已签名的原始交易
eval "$("${PY[@]}" - <<'PYEOF'
from eth_hash.auto import keccak
from eth_keys import keys as k
from localtxpool.encoding import sign_transaction
def pk(l): return keccak(b"local-txpool-fixture-key:"+l.encode())
def ad(l): return k.PrivateKey(pk(l)).public_key.to_canonical_address()
def raw(label, nonce, gp, value=0, to="bob"):
    t = sign_transaction(pk(label), nonce=nonce, gas_price=gp, gas_limit=21000,
                         to=ad(to), value=value, chain_id=31337)
    return "0x"+t.to_rlp().hex()
print(f'ALICE=0x{ad("alice").hex()}')
print(f'BOB=0x{ad("bob").hex()}')
print(f'MINER=0x{ad("miner").hex()}')
print(f'R_A_LOW={raw("alice",0,10,1000,"bob")}')
print(f'R_B_HIGH={raw("bob",0,99,2000,"alice")}')
PYEOF
)"

echo ">> health"
curl -s "$BASE/health" | jget "['data']"

echo ">> fund"
curl -s -X POST "$BASE/admin/fund" -H 'Content-Type: application/json' \
  -H 'X-Request-ID: demo-fund-alice' \
  -d "{\"address\":\"$ALICE\",\"amount\":\"1000000000000000000\"}" | jget "['data']['account']"
curl -s -X POST "$BASE/admin/fund" -H 'Content-Type: application/json' \
  -d "{\"address\":\"$BOB\",\"amount\":\"1000000000000000000\"}" >/dev/null

echo ">> submit alice(gp=10) 与 bob(gp=99)"
curl -s -X POST "$BASE/transactions" -H 'Content-Type: application/json' \
  -H 'X-Request-ID: demo-tx-a' -d "{\"raw\":\"$R_A_LOW\"}"  | jget "['data']"
curl -s -X POST "$BASE/transactions" -H 'Content-Type: application/json' \
  -H 'X-Request-ID: demo-tx-b' -d "{\"raw\":\"$R_B_HIGH\"}" | jget "['data']"

echo ">> 候选顺序（价高者在前，但受 nonce 连续前缀约束）"
curl -s "$BASE/pool/candidate" | jget "['data']['order']"

echo ">> 提议区块（手续费给 miner）"
PROPOSE=$(curl -s -X POST "$BASE/blocks/propose" -H 'Content-Type: application/json' \
  -H 'X-Request-ID: demo-propose' -d "{\"coinbase\":\"$MINER\"}")
echo "$PROPOSE" | jget "['data']"
BNUM=$(echo "$PROPOSE" | jget "['data']['number']")

echo ">> 确认区块 $BNUM"
curl -s -X POST "$BASE/blocks/confirm" -H 'Content-Type: application/json' \
  -H 'X-Request-ID: demo-confirm' -d "{\"block_number\":$BNUM}" | jget "['data']"

echo ">> 账户余额/nonce"
curl -s "$BASE/accounts/$ALICE" | jget "['data']"
curl -s "$BASE/accounts/$BOB"   | jget "['data']"
curl -s "$BASE/accounts/$MINER" | jget "['data']"

echo ">> 该提议请求的完整流水（每步移入移出与理由）"
curl -s "$BASE/explain/journals?request_id=demo-propose" | jget "['data']"

echo ">> 回滚区块 $BNUM（状态恢复，交易重回池）"
curl -s -X POST "$BASE/blocks/rollback" -H 'Content-Type: application/json' \
  -H 'X-Request-ID: demo-rollback' -d '{"n":1}' | jget "['data']"
curl -s "$BASE/pool/status" | jget "['data']"
