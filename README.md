# opp221-b — ribd

IPv4/IPv6 路由表后端：压缩前缀树 + 版本化原子批次 + 递归下一跳解析 + SQLite 事件回放。

- 设计、模块划分、接口与已知限制：`docs/README.md`
- 示例调用：`examples/calls.sh`
- 快速开始：

```bash
GOPROXY=off go test -race -count=1 ./...   # 全部测试（离线）
go run ./cmd/ribd -config testdata/config/example.json
BASE=http://127.0.0.1:8080 bash examples/calls.sh
```
