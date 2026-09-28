#!/usr/bin/env bash
# 本地一键验证：密钥 → 全套独立测试 → 端到端演示 → 实时 HTTP 冒烟。
# 不访问任何外部服务；退出码非零即表示验证未通过。
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python

echo "== [1/4] 检查/生成本地合成密钥 =="
if [ ! -f fixtures/keys/submitter_public.pem ]; then
  $PY fixtures/gen_keys.py fixtures/keys
else
  echo "fixtures/keys/submitter_public.pem 已存在，跳过生成"
fi

echo "== [2/4] 运行独立测试（断言具体结果与失败类别） =="
$PY -m pytest -v

echo "== [3/4] 端到端演示（受限空间穷举 + 见证 + 证据对账） =="
$PY scripts/demo.py > /tmp/poldiff_demo.json
$PY - <<'PYEOF'
import json
d = json.load(open("/tmp/poldiff_demo.json"))
s = d["summary"]
assert s["default_deny"] is True
assert s["explicit_deny_precedence"] is True
assert s["verdict"] == "WIDENED_WITH_UNKNOWN", s["verdict"]
assert s["widened_requests"] > 0 and s["new_unknown_requests"] > 0
w = d["widened_witness"][0]["request"]
assert w["resource"] == "logs-secret/0", w
ev = d["evidence_report"]
assert ev["consistent"] >= 1 and ev["contradictions"] == 0, ev
print("demo 断言通过：",
      f"verdict={s['verdict']}, widened={s['widened_requests']},",
      f"new_unknown={s['new_unknown_requests']},",
      f"evidence_consistent={ev['consistent']}")
PYEOF

echo "== [4/4] 实时 HTTP 服务冒烟 =="
DIFF_DB_PATH="data/smoke.db" $PY -m uvicorn diffanalyzer.main:app \
  --host 127.0.0.1 --port 8088 >/tmp/poldiff_http.log 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do
  curl -sf http://127.0.0.1:8088/health >/dev/null 2>&1 && break
  sleep 0.2
done
curl -sf http://127.0.0.1:8088/health | $PY -m json.tool
$PY scripts/http_smoke.py
echo "ALL VERIFICATION STEPS PASSED"
