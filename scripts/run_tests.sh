#!/usr/bin/env bash
# 执行全部测试并打印结果：
#   1) 主模块: go test ./...（含故障注入 faulttests 与模型不变量测试）
#   2) 独立黑盒模块 tests/blackbox: 构建真实服务二进制，仅通过 HTTP/JSON 断言
#      （独立模块、独立 go.mod，不 import 任何被测包）
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== [1/2] 主模块测试（单元 + 故障注入，真实 SQLite + 模拟进程管理器）==="
go test -count=1 ./...
RC1=$?

echo
echo "=== [2/2] 独立黑盒测试（tests/blackbox，真实服务进程 + HTTP）==="
( cd tests/blackbox && go test -count=1 ./... )
RC2=$?

echo
if [ "$RC1" -eq 0 ] && [ "$RC2" -eq 0 ]; then
  echo "全部测试通过。"
else
  echo "存在失败: 主模块 rc=$RC1 黑盒 rc=$RC2"
fi
exit $(( RC1 != 0 || RC2 != 0 ))
