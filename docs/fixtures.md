# 夹具（合成期望状态）格式

所有输入为本地 JSON，结构刻意贴近 namespace/pod/networkpolicy 的线格式，
便于将来用真实适配器替换 `internal/source`。

```json
{
  "namespaces": [
    { "name": "prod", "labels": {"env": "prod"} }
  ],
  "endpoints": [
    {
      "uid": "u-web-a",
      "name": "web-a",
      "namespace": "prod",
      "labels": {"app": "web", "role": "frontend"},
      "ports": [
        {"name": "http", "number": 8080, "protocol": "TCP"},
        {"name": "metrics", "number": 9090}
      ]
    }
  ],
  "policies": [
    {
      "name": "web-from-api",
      "namespace": "prod",
      "podSelector": {"matchLabels": {"app": "web"}},
      "policyTypes": ["Ingress"],
      "ingress": [
        {
          "from": [
            {"podSelector": {"matchLabels": {"app": "api"}}}
          ],
          "ports": ["http"]
        }
      ],
      "egress": []
    }
  ]
}
```

## 选择器（selector）

- 缺省（字段省略）= 该侧选择器不存在；`{}` = 匹配一切。二者不同。
- `matchLabels`：全键值相等。
- `matchExpressions`：`In` / `NotIn` / `Exists` / `DoesNotExist`
  （`NotIn` 与 k8s 一致：键不存在也算满足）。

## 规则对端（peer，ingress.from / egress.to）

- 两个选择器都省略：只匹配策略**同命名空间**内的端点；
- 只有 `namespaceSelector`（含 `{}`）：按命名空间标签跨命名空间匹配；
- 只有 `podSelector`（含 `{}`）：限同命名空间并按端点标签匹配；
- 两者都有：命名空间标签与端点标签同时满足。

策略只选择其 `namespace` 内的端点，`podSelector: {}` 不会越出本命名空间。

## 端口

- 规则端口可写裸数字（8080）、裸名字符串（"http"）或对象
  `{"name":"dns","protocol":"UDP"}` / `{"number":8080}`。
- 规则省略 protocol 时按 TCP 处理。
- 规则不写 ports = 所有端口/协议。
- 命名端口在求值时按**目标端点**的声明解析；目标端同名端口映射到多个不同
  （协议,数字）时该判定为 UNDECIDABLE；端点上完全相同的
  （名字,数字,协议）重复声明会在载入期被判为 `port_name_duplicate`。

## policyTypes 缺省

省略 `policyTypes` 时：无 egress 规则 → 仅 `Ingress`；有 egress 规则 →
`Ingress` + `Egress`（顺序固定 Ingress 在前）。显式写出则不做缺省。

## 载入期校验（失败类别）

重复命名空间、端点重复 UID/缺 UID/引用不存在命名空间、标签键值非法、
端口号越界/协议非法、策略重名/缺命名空间/引用未知命名空间、非法 policyType、
非法选择器或算符、规则端口非法，以及夹具文件缺失或 JSON 语法错——
均以稳定错误类别返回（见 `internal/domain/errors.go` 与 docs/api.md）。
