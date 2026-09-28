# HTTP API

所有请求/响应均为 JSON。判定接口：

- `GET /healthz` — 存活探针。
- `POST /v1/check` — 单点判定。
- `POST /v1/matrix` — 穷举连通矩阵（所有有序端点对）。
- `GET /v1/status` — 当前在服 revision、对象计数、最近一次协调记录。
- `GET /v1/snapshots` — 历史 revision 元数据与最近协调记录。
- `GET /v1/snapshots/{revision}` — 取某个历史版本的完整快照。
- `POST /internal/refresh` — 立即触发一次协调。

每个响应都带 `X-Request-ID`；请求可自带该头做关联。错误体：

```json
{ "error": {"code": "invalid_protocol", "message": "..."}, "requestId": "req-..." }
```

## POST /v1/check

请求：

```json
{
  "sourceUid": "u-api",
  "destUid":   "u-web-a",
  "protocol":  "TCP",
  "port":      8080,
  "pinRevision": 0
}
```

`protocol` 取 `TCP`/`UDP`（默认 TCP）。`port` 为数字（真实报文总是数字；
命名端口只存在于策略里并按目标端解析）。`pinRevision` 可选，非零时要求在
恰好该版本上判定，否则返回 `UNDECIDABLE/revision_conflict`。

响应（节选）：

```json
{
  "verdict": "ALLOW",
  "allowed": true,
  "reason":  "allowed_both_sides",
  "revision": 2,
  "sourceUid": "u-api", "destUid": "u-web-a",
  "protocol": "TCP", "port": 8080,
  "ingress": {
    "isolated": true, "allowed": true,
    "selectedPolicies": ["prod/web-from-api", "prod/web-from-billing"],
    "matches": [{"policyNamespace":"prod","policyName":"web-from-api","direction":"ingress","ruleIndex":0,...}]
  },
  "egress": {
    "isolated": true, "allowed": true,
    "selectedPolicies": ["prod/api-egress-web"],
    "matches": [{"policyNamespace":"prod","policyName":"api-egress-web","direction":"egress","ruleIndex":0,...}]
  }
}
```

`isolated`：该侧是否被至少一条策略选中（false 即该侧默认允许）。
`selectedPolicies`：在该侧隔离本端点的全部策略（即使其内部无规则命中，也能
据此解释为何被拒）。`matches`：真正放行的策略与规则下标。`hints`：差一点
命中（如 peer 匹配但目标端不提供该命名端口）。`ambiguities`：无法判定原因。

## POST /v1/matrix

二选一：给定 `{protocol, port}`，或 `{"allDeclaredPorts": true}`（对每个被任
一端点声明过的协议/端口各扫一张矩阵；这保证覆盖命名端口对应的真实数字，
而不是把名字当全局数字）。可带 `pinRevision`。单矩阵响应：

```json
{ "revision": 2, "protocol": "TCP", "port": 8080,
  "cells": [ {"sourceUid":"...","destUid":"...","decision": { /* 同 /v1/check */ } } ] }
```

`allDeclaredPorts` 响应：`{"revision":2,"matrices":[ <上面的矩阵>, ... ]}`。
端点顺序固定为命名空间/名称排序，矩阵穷举所有有序对（含自身对），输出确定。

## 原因码（reason）

允许：

- `allowed_both_sides` — 两侧都被策略隔离且都有规则命中；
- `ingress_unselected_default_allow` — 出口侧命中，目标未被任何入口策略选中；
- `egress_unselected_default_allow` — 入口侧命中，源未被任何出口策略选中；
- `ingress_unselected_default_allow+egress_unselected_default_allow` — 两侧都未被选中。

拒绝：

- `ingress_selected_no_rule_matched` — 目标入口侧隔离且无规则命中；
- `egress_selected_no_rule_matched` — 源出口侧隔离且无规则命中；
- `both_sides_selected_no_rule_matched` — 两侧都隔离且都无命中。

无法判定：

- `endpoint_unknown` — 源或目标不在该 revision 快照内；
- `protocol_unsupported` — 非 TCP/UDP；
- `port_out_of_range` — 端口不在 1..65535；
- `revision_conflict` — 请求固定的版本与在服版本不一致；
- `named_port_ambiguous` — 引用的命名端口在目标端解析到多个（协议,端口）。

## 协调状态（GET /v1/status, /v1/snapshots）

`lastRun.status` 取值：

- `applied` — 拉取成功且内容变化，写入新版本；
- `unchanged` — 内容哈希与已有版本相同，不新增版本；
- `fetch_failed` — 夹具缺失/不可读/JSON 语法错（errorKind 如
  `source_not_found`、`source_syntax`）；
- `validation_failed` — 解析成功但违反资源模型（errorKind 为具体类别，如
  `endpoint_duplicate_uid`、`endpoint_unknown_namespace`、
  `port_number_invalid`、`selector_invalid` 等）；
- `store_failed` — 快照无法落库。

协调失败不会替换在服引擎：`status.revision` 保持上一可用版本。
