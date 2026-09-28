#!/usr/bin/env bash
# 服务调用示例（curl）。先运行: python scripts/serve.py
#
# 代理只绑定 127.0.0.1:8080，本机测试源站只绑定 127.0.0.1:18080。
# 所有"外部主机名"都由受控 DNS 夹具解析，不触公网。

set -u
BASE="${BASE:-http://127.0.0.1:8080}"

echo "== 1) 健康检查 =="
curl -s "$BASE/health"; echo

echo "== 2) 正常：抓取允许的本机源站（200）=="
curl -s -o /tmp/sp_ok.json -w "HTTP %{http_code}\n" \
  -X POST "$BASE/v1/fetch" -H 'Content-Type: application/json' \
  -d '{"url":"http://127.0.0.1:18080/ok"}'
python3 -c "import json;d=json.load(open('/tmp/sp_ok.json'));print('verdict=',d['final_verdict'],'peer=',d['connected_peer'],'run_id=',d['run_id'])"

echo "== 3) 攻击：直连云元数据地址（403，禁止地址从未连接）=="
curl -s -o /tmp/sp_meta.json -w "HTTP %{http_code}\n" \
  -X POST "$BASE/v1/fetch" -H 'Content-Type: application/json' \
  -d '{"url":"http://169.254.169.254/latest/meta-data/"}'
python3 -c "import json;d=json.load(open('/tmp/sp_meta.json'))['detail'];print('code=',d['error']['code'],'rule=',[h['matched']['rule_id'] for h in d['hops'] if h.get('matched')])"

echo "== 4) 攻击：DNS 重绑定（403，混合集整组拒绝、零连接）=="
curl -s -X POST "$BASE/v1/fetch" -H 'Content-Type: application/json' \
  -d '{"url":"http://rebind.example:18080/loophole"}' | python3 -m json.tool

echo "== 5) 攻击：重定向到元数据（第一跳允许、第二跳 403）=="
curl -s -o /tmp/sp_redir.json -w "HTTP %{http_code}\n" \
  -X POST "$BASE/v1/fetch" -H 'Content-Type: application/json' \
  -d '{"url":"http://127.0.0.1:18080/redirect-meta"}'

echo "== 6) 异常：重定向环（409 状态冲突）与预算耗尽（429）=="
curl -s -o /dev/null -w "loop   HTTP %{http_code}\n" -X POST "$BASE/v1/fetch" \
  -H 'Content-Type: application/json' -d '{"url":"http://127.0.0.1:18080/loop-a"}'
curl -s -o /dev/null -w "budget HTTP %{http_code}\n" -X POST "$BASE/v1/fetch" \
  -H 'Content-Type: application/json' -d '{"url":"http://127.0.0.1:18080/many"}'

echo "== 7) 审计：列出运行 / 取某条 / 验证哈希链 =="
curl -s "$BASE/v1/audit/runs?limit=3" | python3 -m json.tool
curl -s "$BASE/v1/audit/verify" | python3 -m json.tool
