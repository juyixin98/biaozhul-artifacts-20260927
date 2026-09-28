#!/usr/bin/env bash
# 本地示例请求：假设服务已在 127.0.0.1:8000 运行（scripts/run_dev.sh）。
set -euo pipefail
B="${ABI_BASE_URL:-http://127.0.0.1:8000}"
j() { python3 -m json.tool; }

echo "### 1) 健康检查（含版本与 run_id）"
curl -s "$B/healthz"; echo

echo "### 2) 知名 ERC-20 选择器（Keccak 正确性）"
curl -s "$B/abi/selector" -H 'content-type: application/json' \
  -d '{"signature":"transfer(address,uint256)"}'; echo

echo "### 3) 编码嵌套动态类型 (string[],uint256)"
curl -s "$B/abi/encode" -H 'content-type: application/json' \
  -d '{"types":["(string[],uint256)"],"values":[[["a","bb"],7]]}'; echo

echo "### 4) 解码（空 bytes / 负整数）：bytes + int256"
curl -s "$B/abi/encode" -H 'content-type: application/json' \
  -d '{"types":["bytes","int256"],"values":[{"bytes":"0x"},-1]}' >/tmp/enc.json
BLOB=$(python3 -c "import json;print(json.load(open('/tmp/enc.json'))['data'])")
curl -s "$B/abi/decode" -H 'content-type: application/json' \
  -d "{\"types\":[\"bytes\",\"int256\"],\"data\":\"$BLOB\"}"; echo

echo "### 5) 链：给 0xaa.. 铸造 1000"
curl -s "$B/chain/transact" -H 'content-type: application/json' -d '{
  "signature":"mint(bytes20,uint256)",
  "args":[{"bytes":"0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},1000]
}'; echo

echo "### 6) 链：余额不足转账（必须 ok=false + 明确回滚类别）"
curl -s "$B/chain/transact" -H 'content-type: application/json' -d '{
  "signature":"transfer(bytes20,bytes20,uint256)",
  "args":[
    {"bytes":"0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
    {"bytes":"0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
    999999
  ]
}'; echo

echo "### 7) 恶意输入：bytes 声明 2^256-1 长度（必须 400 allocation_limit）"
curl -s -o /tmp/bad.json -w "http_status=%{http_code}\n" "$B/abi/decode" \
  -H 'content-type: application/json' -d '{"types":["bytes"],
  "data":"0x0000000000000000000000000000000000000000000000000000000000000020ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"}'
cat /tmp/bad.json; echo

echo "### 8) 链状态"
curl -s "$B/chain/state" | j
